"""Restart-safe handlers for the durable AI editing job queue."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from edit_analysis_service import EditAnalysisService
from edit_learning_service import EditFeedbackService
from edit_plan_service import prepare_plan
from edit_project_store import EditProjectStore, public_project, transition_project, utc_now
from edit_quality_service import EditQualityService
from edit_render_service import EditRenderService
from edit_storage import object_storage_from_env
from media_ingest import MediaIngestService


class PermanentEditJobError(RuntimeError):
    retryable = False


class EditPipeline:
    def __init__(self, store: EditProjectStore | None = None) -> None:
        self.store = store or EditProjectStore()

    def _row(self, project_id: int) -> tuple[dict[str, Any], dict[str, Any]]:
        row = self.store.get(project_id)
        if not row:
            raise PermanentEditJobError("편집 프로젝트를 찾지 못했습니다.")
        return row, row["report"]

    def _source(self, project: dict[str, Any], *, expires: int = 43200) -> tuple[str, Any | None]:
        source = project.get("source") or {}
        if source.get("storage_backend") == "object" and source.get("object_key"):
            backend = object_storage_from_env()
            if backend is None:
                raise RuntimeError("Object Storage 연결이 설정되지 않았습니다.")
            # Workers materialize a bounded local copy; do not create or retain
            # a signed source URL that ffmpeg would reopen for every segment.
            return str(source["object_key"]), backend
        return str(self.store.resolve_media_path(project, "source")), None

    async def _working_source(
        self, project: dict[str, Any], backend: Any, *, output_ratio: float,
    ) -> tuple[tempfile.TemporaryDirectory[str], Path, float]:
        """Download one bounded working copy instead of repeatedly seeking R2."""

        source = project.get("source") or {}
        size = max(0, int(source.get("size_bytes") or 0))
        reserve = 256 * 1024 * 1024
        required = int(size * max(1.0, output_ratio)) + reserve
        free = shutil.disk_usage(tempfile.gettempdir()).free
        if free < required:
            raise RuntimeError(
                f"LocalWorkingSpaceInsufficient: required={required} free={free}"
            )
        temporary = tempfile.TemporaryDirectory(
            prefix=f"edit-work-{str(project.get('project_uuid') or '')[:8]}-"
        )
        suffix = Path(str(source.get("filename") or "source.mp4")).suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
            suffix = ".mp4"
        destination = Path(temporary.name) / f"source{suffix}"
        started = time.perf_counter()
        try:
            await asyncio.to_thread(
                backend.download, str(source["object_key"]), destination
            )
            if not destination.is_file() or (size and destination.stat().st_size != size):
                raise RuntimeError("Object Storage working copy size mismatch")
            return temporary, destination, round(time.perf_counter() - started, 3)
        except Exception:
            await asyncio.to_thread(temporary.cleanup)
            raise

    @staticmethod
    async def _cleanup_working_copy(temporary: tempfile.TemporaryDirectory[str] | None) -> None:
        if temporary is not None:
            await asyncio.to_thread(temporary.cleanup)

    async def analysis(self, job: dict[str, Any]) -> dict[str, Any]:
        project_id = int(job["project_id"])
        _, project = self._row(project_id)
        started = time.perf_counter()
        try:
            project = transition_project(project, "transcribing", lifecycle="ANALYZING", reason="durable analysis started", job_id=int(job["job_id"]))
            self.store.save(project_id, project)
            ingest = MediaIngestService(self.store)
            media = (project.get("source") or {}).get("media") or {}
            transcript = project.get("transcript") or {}
            signals = project.get("analysis_signals") or {}
            if not media or not transcript.get("segments"):
                source, backend = self._source(project)
                temporary = None
                try:
                    if backend is not None:
                        temporary, local_source, download_seconds = await self._working_source(
                            project, backend, output_ratio=1.25
                        )
                        source = str(local_source)
                        project.setdefault("timings", {})["analysis_source_download_seconds"] = download_seconds
                    if not media:
                        media = await asyncio.to_thread(ingest.probe, source)
                        project.setdefault("source", {})["media"] = media
                        self.store.save(project_id, project)
                    if not transcript.get("segments"):
                        media_started = time.perf_counter()
                        work_dir = self.store.project_dir(str(project["project_uuid"]), create=True)
                        transcript, silences, scenes = await ingest.inspect_and_transcribe(source, media, work_dir=work_dir)
                        project["transcript"] = transcript
                        project["analysis_signals"] = {"silences": silences, "scene_changes": scenes}
                        project.setdefault("timings", {})["media_and_transcript_seconds"] = round(time.perf_counter() - media_started, 3)
                        self.store.save(project_id, project)
                    else:
                        silences = signals.get("silences") or []
                        scenes = signals.get("scene_changes") or []
                finally:
                    await self._cleanup_working_copy(temporary)
            else:
                silences = signals.get("silences") or []
                scenes = signals.get("scene_changes") or []
            analysis = EditAnalysisService()
            evidence = project.get("evidence_snapshot") or {}
            if not evidence:
                retrieval_started = time.perf_counter()
                project = transition_project(project, "retrieving_context", lifecycle="ANALYZING", reason="channel evidence retrieval", job_id=int(job["job_id"]))
                self.store.save(project_id, project)
                settings = project.get("settings") or {}
                evidence, trace, strategy = await analysis.collect_evidence(
                    topic=str(settings.get("topic") or ""), purpose=str(settings.get("purpose") or ""),
                    strategy_id=settings.get("content_strategy_id"),
                )
                project["evidence_snapshot"] = evidence
                project["evidence_trace"] = trace
                project["strategy_snapshot"] = strategy
                project.setdefault("timings", {})["retrieval_seconds"] = round(time.perf_counter() - retrieval_started, 3)
                self.store.save(project_id, project)
            project = transition_project(project, "diagnosing", lifecycle="ANALYZING", reason="channel-grounded AI diagnosis", job_id=int(job["job_id"]))
            self.store.save(project_id, project)
            diagnosis_started = time.perf_counter()
            diagnosis = await analysis.diagnose(
                transcript=project["transcript"], media=media,
                silences=(project.get("analysis_signals") or {}).get("silences") or [],
                scenes=(project.get("analysis_signals") or {}).get("scene_changes") or [],
                settings=project.get("settings") or {}, evidence=project.get("evidence_snapshot") or {},
                strategy=project.get("strategy_snapshot"),
            )
            project.setdefault("timings", {})["gpt_diagnosis_seconds"] = round(time.perf_counter() - diagnosis_started, 3)
            plan = prepare_plan(
                diagnosis.get("plan") or {}, float(media["duration"]),
                target_format=(project.get("settings") or {}).get("target_format"),
            )
            project["diagnosis"] = {key: value for key, value in diagnosis.items() if key != "plan"}
            project["plan_versions"] = [{
                "version": 1, "status": "proposed", "created_at": utc_now(),
                "source": "ai_diagnosis", "user_request": "",
                "revision_summary": "AI 최초 분석 제안", "diff": [], "plan": plan,
            }]
            project.setdefault("timings", {})["analysis_total_seconds"] = round(time.perf_counter() - started, 3)
            project = transition_project(project, "proposed", lifecycle="AWAITING_REVIEW", reason="diagnosis ready", job_id=int(job["job_id"]))
            project["error"] = None
            self.store.save(project_id, project)
            return {"analysis_seconds": project["timings"]["analysis_total_seconds"]}
        except Exception as exc:
            _, latest = self._row(project_id)
            latest = transition_project(latest, "analysis_failed", lifecycle="FAILED_ANALYSIS", reason=type(exc).__name__, job_id=int(job["job_id"]))
            latest["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.store.save(project_id, latest)
            raise

    async def rendering(self, job: dict[str, Any]) -> dict[str, Any]:
        project_id = int(job["project_id"])
        _, project = self._row(project_id)
        approved = int(project.get("approved_version") or 0)
        version_row = next((item for item in project.get("plan_versions") or [] if int(item.get("version") or 0) == approved), None)
        if not approved or not version_row:
            raise PermanentEditJobError("승인된 immutable edit plan이 없습니다.")
        started = time.perf_counter()
        try:
            project = transition_project(project, "rendering", lifecycle="RENDERING", reason="durable render claimed", job_id=int(job["job_id"]))
            project.setdefault("storage_state", {})["render_started_at"] = utc_now()
            self.store.save(project_id, project)
            source, backend = self._source(project, expires=43200)
            renderer = EditRenderService()
            qa = {}
            if backend is not None:
                temporary = None
                try:
                    temporary, local_source, download_seconds = await self._working_source(
                        project, backend, output_ratio=3.0
                    )
                    project.setdefault("timings", {})["render_source_download_seconds"] = download_seconds
                    directory = Path(temporary.name) / "outputs"
                    encode_started = time.perf_counter()
                    local_outputs, edit_log = await asyncio.to_thread(
                        renderer.render_project, source=local_source, directory=directory,
                        plan=version_row["plan"], media=(project.get("source") or {}).get("media") or {},
                        version=approved,
                    )
                    project.setdefault("timings", {})["render_encode_seconds"] = round(time.perf_counter() - encode_started, 3)
                    media = (project.get("source") or {}).get("media") or {}
                    qa_started = time.perf_counter()
                    for kind in ("full", "short"):
                        output = local_outputs.get(kind)
                        if not output:
                            continue
                        expected = float(version_row["plan"].get("estimated_short_duration" if kind == "short" else "estimated_output_duration") or 0)
                        qa[kind] = await asyncio.to_thread(
                            EditQualityService().validate,
                            source=str(directory / output["storage_name"]), plan=version_row["plan"],
                            output_kind=kind, expected_duration=expected,
                            require_audio=bool(media.get("has_audio")),
                        )
                    project.setdefault("timings", {})["render_qa_seconds"] = round(time.perf_counter() - qa_started, 3)
                    upload_started = time.perf_counter()
                    outputs = {}
                    for kind in ("full", "short"):
                        output = local_outputs.get(kind)
                        if not output:
                            continue
                        path = directory / output["storage_name"]
                        object_key = await asyncio.to_thread(
                            backend.upload, path,
                            project_uuid=str(project["project_uuid"]), filename=output["filename"],
                            content_type="video/mp4",
                        )
                        metadata = await asyncio.to_thread(backend.head, object_key)
                        outputs[kind] = {
                            **{key: value for key, value in output.items() if key != "storage_name"},
                            "object_key": object_key, "storage_backend": "object",
                            "size_bytes": metadata["size_bytes"],
                        }
                    decision_name = f"edit-decision-v{approved}.json"
                    decision_payload = {
                        "version": approved, "approved_plan": version_row["plan"],
                        "applied_edit_log": edit_log,
                        "advisory_edit_log": renderer.advisory_log(version_row["plan"]),
                        "exports": outputs,
                    }
                    decision_key = await asyncio.to_thread(
                        backend.upload_bytes,
                        json.dumps(decision_payload, ensure_ascii=False, indent=2).encode("utf-8"),
                        project_uuid=str(project["project_uuid"]), filename=decision_name,
                        content_type="application/json",
                    )
                    decision_meta = await asyncio.to_thread(backend.head, decision_key)
                    outputs["decision"] = {
                        "object_key": decision_key, "filename": decision_name,
                        "size_bytes": decision_meta["size_bytes"], "created_at": utc_now(),
                        "content_type": "application/json", "storage_backend": "object",
                    }
                    project.setdefault("timings", {})["render_storage_upload_seconds"] = round(time.perf_counter() - upload_started, 3)
                finally:
                    await self._cleanup_working_copy(temporary)
            else:
                directory = self.store.project_dir(str(project["project_uuid"]), create=True)
                outputs, edit_log = await asyncio.to_thread(
                    renderer.render_project, source=Path(source), directory=directory,
                    plan=version_row["plan"], media=(project.get("source") or {}).get("media") or {},
                    version=approved,
                )
            if backend is None:
                media = (project.get("source") or {}).get("media") or {}
                for kind in ("full", "short"):
                    output = outputs.get(kind)
                    if not output:
                        continue
                    output_source = str(self.store.project_dir(str(project["project_uuid"])) / output["storage_name"])
                    expected = float(version_row["plan"].get("estimated_short_duration" if kind == "short" else "estimated_output_duration") or 0)
                    qa[kind] = await asyncio.to_thread(
                        EditQualityService().validate, source=output_source, plan=version_row["plan"],
                        output_kind=kind, expected_duration=expected, require_audio=bool(media.get("has_audio")),
                    )
            project["outputs"] = outputs
            project["applied_edit_log"] = edit_log
            project["advisory_edit_log"] = renderer.advisory_log(version_row["plan"])
            project["quality_assurance"] = qa
            project["render_runs"] = (project.get("render_runs") or []) + [{
                "version": approved, "created_at": utc_now(), "outputs": outputs,
                "edit_log": edit_log, "quality_assurance": qa, "job_id": int(job["job_id"]),
            }]
            project.setdefault("timings", {})["render_seconds"] = round(time.perf_counter() - started, 3)
            project.setdefault("storage_state", {})["render_completed_at"] = utc_now()
            project = transition_project(project, "completed", lifecycle="COMPLETED", reason="render and QA complete", job_id=int(job["job_id"]))
            project["error"] = None
            self.store.save(project_id, project)
            return {"render_seconds": project["timings"]["render_seconds"]}
        except Exception as exc:
            _, latest = self._row(project_id)
            latest = transition_project(latest, "render_failed", lifecycle="FAILED_RENDER", reason=type(exc).__name__, job_id=int(job["job_id"]))
            latest["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.store.save(project_id, latest)
            raise

    async def performance_sync(self, job: dict[str, Any]) -> dict[str, Any]:
        project_id = int(job["project_id"])
        _, project = self._row(project_id)
        result = await asyncio.to_thread(EditFeedbackService().evaluate, project_id, project)
        feedback = project.get("upload_feedback") or {}
        comparisons = list(feedback.get("comparisons") or [])
        fingerprint = (result.get("source_as_of"), result.get("status"))
        if not comparisons or (comparisons[-1].get("source_as_of"), comparisons[-1].get("status")) != fingerprint:
            comparisons.append(result)
        feedback.update({"latest_comparison": result, "comparisons": comparisons[-40:]})
        project["upload_feedback"] = feedback
        self.store.save(project_id, project)
        return {"sync_completed": 1}
