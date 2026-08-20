"""Restart-safe handlers for the durable AI editing job queue."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from edit_analysis_service import EditAnalysisService
from edit_learning_service import EditFeedbackService
from edit_plan_service import prepare_plan
from edit_project_store import EditProjectStore, public_project, transition_project, utc_now
from edit_quality_service import EditQualityService
from edit_render_service import EditRenderService
from edit_storage import object_storage_from_env
from edit_visual_service import VISUAL_FALLBACK_MESSAGE, TimecodedFrameExtractor
from media_ingest import MediaIngestService
from multisource_roughcut import (
    apply_story_reasoning, apply_visual_quality, bounded_story_candidates, checkpoint, deduplicate_segments, ensure_multisource,
    find_source, plan_transcript_chunks, semantic_segments, validate_timeline,
)


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
        object_meta: dict[str, Any] | None = None,
    ) -> tuple[tempfile.TemporaryDirectory[str], Path, float]:
        """Download one bounded working copy instead of repeatedly seeking R2."""

        source = object_meta or project.get("source") or {}
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
            await asyncio.to_thread(backend.download, str(source["object_key"]), destination)
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

    async def _ensure_analysis_proxy(
        self, project_id: int, project: dict[str, Any], *, backend: Any,
        local_source: Path, temporary: tempfile.TemporaryDirectory[str],
        media: dict[str, Any], ingest: MediaIngestService,
    ) -> Path:
        if (project.get("proxy") or {}).get("object_key"):
            return local_source
        if int(media.get("width") or 0) <= 1920 and int(media.get("height") or 0) <= 1080:
            return local_source
        proxy_started = time.perf_counter()
        proxy_path = Path(temporary.name) / "analysis-proxy-1080p.mp4"
        try:
            await asyncio.to_thread(
                ingest.create_analysis_proxy, local_source, proxy_path,
                float(media.get("duration") or 0),
            )
            proxy_key = await asyncio.to_thread(
                backend.upload, proxy_path,
                project_uuid=str(project["project_uuid"]),
                filename="analysis-proxy-1080p.mp4", content_type="video/mp4",
            )
            proxy_storage = await asyncio.to_thread(backend.head, proxy_key)
        except Exception as exc:
            # Proxy is an optimization. Preserve the proven original-based
            # analysis path if local CPU/storage is temporarily constrained.
            project["proxy"] = {
                "profile": "preview_1080p", "status": "failed",
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "attempted_at": utc_now(),
            }
            self.store.save(project_id, project)
            return local_source
        project["proxy"] = {
            "storage_backend": "object", "object_key": proxy_key,
            "filename": "analysis-proxy-1080p.mp4",
            "size_bytes": proxy_storage["size_bytes"],
            "profile": "preview_1080p", "created_at": utc_now(),
        }
        project.setdefault("timings", {})["analysis_proxy_create_seconds"] = round(time.perf_counter() - proxy_started, 3)
        self.store.save(project_id, project)
        return proxy_path

    async def _run_visual_analysis(
        self, *, source: str | Path, transcript: dict[str, Any], media: dict[str, Any],
        scenes: list[float], analysis: EditAnalysisService, max_frames: int | None = None,
    ) -> dict[str, Any]:
        temporary = tempfile.TemporaryDirectory(prefix="edit-visual-frames-")
        started = time.perf_counter()
        try:
            manifest = await asyncio.to_thread(
                TimecodedFrameExtractor().extract,
                source=Path(source), output_dir=Path(temporary.name),
                duration=float(media.get("duration") or 0), scene_times=scenes,
                max_frames=max_frames,
            )
            result = await analysis.analyze_visual_frames(
                manifest=manifest, transcript=transcript, media=media,
            )
            result["analysis_seconds"] = round(time.perf_counter() - started, 3)
            return result
        except Exception as exc:
            return {
                "schema_version": 1, "status": "failed", "fallback_used": True,
                "message": VISUAL_FALLBACK_MESSAGE,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "frame_count": 0, "frame_results": [], "segments": [],
                "analysis_seconds": round(time.perf_counter() - started, 3),
                "decision_basis": "audio_transcript_only",
            }
        finally:
            await asyncio.to_thread(temporary.cleanup)

    async def analysis(self, job: dict[str, Any]) -> dict[str, Any]:
        project_id = int(job["project_id"])
        _, project = self._row(project_id)
        started = time.perf_counter()

        def save_progress(
            *, stage: str, label: str, percent: int,
            pending_operation: str | None = None,
            checkpoint_name: str | None = None,
            total_chunks: int | None = None,
            completed_chunks: int | None = None,
            current_chunk: int | None = None,
            last_error: str | None = None,
        ) -> None:
            previous = dict(project.get("analysis_progress") or {})
            progress = {
                **previous,
                "stage": stage,
                "label": label,
                "percent": max(0, min(100, int(percent))),
                "pending_operation": pending_operation,
                "updated_at": utc_now(),
                "last_error": last_error,
            }
            if total_chunks is not None:
                progress["total_chunks"] = max(0, int(total_chunks))
            if completed_chunks is not None:
                progress["completed_chunks"] = max(0, int(completed_chunks))
            progress["current_chunk"] = current_chunk
            if checkpoint_name:
                progress["last_success_checkpoint"] = checkpoint_name
                progress["last_checkpoint_at"] = progress["updated_at"]
            project["analysis_progress"] = progress
            self.store.save(project_id, project)

        try:
            project = transition_project(project, "transcribing", lifecycle="ANALYZING", reason="durable analysis started", job_id=int(job["job_id"]))
            save_progress(
                stage="source_download", label="원본 작업 사본 준비", percent=2,
                pending_operation="r2_download",
                checkpoint_name="UPLOAD_COMPLETE",
            )
            self.store.save(project_id, project)
            ingest = MediaIngestService(self.store)
            analysis = EditAnalysisService()
            media = (project.get("source") or {}).get("media") or {}
            transcript = project.get("transcript") or {}
            signals = project.get("analysis_signals") or {}
            visual_analysis = project.get("visual_analysis") or {}
            visual_attempted = visual_analysis.get("status") == "succeeded"
            if not media or not transcript.get("segments"):
                source, backend = self._source(project)
                temporary = None
                try:
                    if backend is not None:
                        proxy_meta = project.get("proxy") or {}
                        temporary, local_source, download_seconds = await self._working_source(
                            project, backend, output_ratio=1.25,
                            object_meta=proxy_meta if proxy_meta.get("object_key") else None,
                        )
                        source = str(local_source)
                        project.setdefault("timings", {})[
                            "analysis_proxy_download_seconds" if proxy_meta.get("object_key") else "analysis_source_download_seconds"
                        ] = download_seconds
                        save_progress(
                            stage="media_inspection", label="미디어 정보 확인", percent=5,
                            checkpoint_name="SOURCE_DOWNLOADED",
                        )
                    if not media:
                        media = await asyncio.to_thread(ingest.probe, source)
                        project.setdefault("source", {})["media"] = media
                        self.store.save(project_id, project)
                    if backend is not None:
                        source = str(await self._ensure_analysis_proxy(
                            project_id, project, backend=backend,
                            local_source=Path(source), temporary=temporary,
                            media=media, ingest=ingest,
                        ))
                    if not transcript.get("segments"):
                        media_started = time.perf_counter()
                        work_dir = self.store.project_dir(str(project["project_uuid"]), create=True)

                        async def transcript_progress(update: dict[str, Any]) -> None:
                            completed_chunk = update.pop("completed_chunk", None)
                            chunks = list(project.get("transcript_chunks") or [])
                            if completed_chunk:
                                index = int(completed_chunk["chunk_index"])
                                chunks = [
                                    row for row in chunks
                                    if int(row.get("chunk_index") or 0) != index
                                ]
                                chunks.append(completed_chunk)
                                chunks.sort(key=lambda row: int(row.get("chunk_index") or 0))
                                project["transcript_chunks"] = chunks
                            total = max(0, int(update.get("total_chunks") or 0))
                            completed = max(0, int(update.get("completed_chunks") or 0))
                            transcript_percent = round((completed / total) * 100) if total else 0
                            checkpoint_name = str(update.get("checkpoint") or "")
                            save_progress(
                                stage=str(update.get("stage") or "transcribing"),
                                label="음성 분석" if update.get("stage") == "transcribing" else "장면·정적 분석",
                                percent=transcript_percent,
                                pending_operation=update.get("pending_operation"),
                                checkpoint_name=checkpoint_name or None,
                                total_chunks=total,
                                completed_chunks=completed,
                                current_chunk=update.get("current_chunk"),
                            )

                        transcript, silences, scenes = await ingest.inspect_and_transcribe(
                            source, media, work_dir=work_dir,
                            existing_chunks=project.get("transcript_chunks") or [],
                            on_progress=transcript_progress,
                        )
                        project["transcript"] = transcript
                        project["analysis_signals"] = {"silences": silences, "scene_changes": scenes}
                        project.setdefault("timings", {})["media_and_transcript_seconds"] = round(time.perf_counter() - media_started, 3)
                        save_progress(
                            stage="visual_analysis", label="대표 화면 분석", percent=100,
                            checkpoint_name="TRANSCRIPT_AND_SIGNALS_COMPLETE",
                        )
                        self.store.save(project_id, project)
                    else:
                        silences = signals.get("silences") or []
                        scenes = signals.get("scene_changes") or []
                    if not visual_attempted:
                        save_progress(
                            stage="visual_analysis", label="대표 화면 분석", percent=100,
                            pending_operation="visual_frame_analysis",
                            checkpoint_name="TRANSCRIPT_AND_SIGNALS_COMPLETE",
                        )
                        visual_analysis = await self._run_visual_analysis(
                            source=source, transcript=transcript, media=media,
                            scenes=scenes, analysis=analysis,
                        )
                        visual_attempted = True
                        project["visual_analysis"] = visual_analysis
                        project.setdefault("timings", {})["visual_analysis_seconds"] = visual_analysis.get("analysis_seconds")
                        self.store.save(project_id, project)
                finally:
                    await self._cleanup_working_copy(temporary)
            else:
                silences = signals.get("silences") or []
                scenes = signals.get("scene_changes") or []
            if not visual_attempted:
                source, backend = self._source(project)
                temporary = None
                try:
                    if backend is not None:
                        proxy_meta = project.get("proxy") or {}
                        temporary, local_source, _ = await self._working_source(
                            project, backend, output_ratio=1.1,
                            object_meta=proxy_meta if proxy_meta.get("object_key") else None,
                        )
                        source = str(local_source)
                    visual_analysis = await self._run_visual_analysis(
                        source=source, transcript=transcript, media=media,
                        scenes=scenes, analysis=analysis,
                    )
                    project["visual_analysis"] = visual_analysis
                    project.setdefault("timings", {})["visual_analysis_seconds"] = visual_analysis.get("analysis_seconds")
                    self.store.save(project_id, project)
                finally:
                    await self._cleanup_working_copy(temporary)
            evidence = project.get("evidence_snapshot") or {}
            if not evidence:
                retrieval_started = time.perf_counter()
                project = transition_project(project, "retrieving_context", lifecycle="ANALYZING", reason="channel evidence retrieval", job_id=int(job["job_id"]))
                save_progress(
                    stage="retrieving_context", label="채널·비즈니스 근거 비교", percent=100,
                    pending_operation="retrieval",
                    checkpoint_name="VISUAL_ANALYSIS_COMPLETE",
                )
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
            trace = list(project.get("evidence_trace") or [])
            if not any(item.get("tool") == "visual_frame_analysis" for item in trace):
                trace.append({
                    "tool": "visual_frame_analysis", "source": "proxy_timecoded_frames",
                    "sample_size": int((visual_analysis or {}).get("analyzed_frame_count") or 0),
                    "freshness": None, "unavailable": (visual_analysis or {}).get("status") != "succeeded",
                })
                project["evidence_trace"] = trace
                self.store.save(project_id, project)
            project = transition_project(project, "diagnosing", lifecycle="ANALYZING", reason="channel-grounded AI diagnosis", job_id=int(job["job_id"]))
            save_progress(
                stage="diagnosing", label="AI 편집 제안 작성", percent=100,
                pending_operation="openai_diagnosis",
                checkpoint_name="EVIDENCE_RETRIEVAL_COMPLETE",
            )
            self.store.save(project_id, project)
            diagnosis_started = time.perf_counter()
            diagnosis = await analysis.diagnose(
                transcript=project["transcript"], media=media,
                silences=(project.get("analysis_signals") or {}).get("silences") or [],
                scenes=(project.get("analysis_signals") or {}).get("scene_changes") or [],
                settings=project.get("settings") or {}, evidence=project.get("evidence_snapshot") or {},
                strategy=project.get("strategy_snapshot"),
                visual_analysis=visual_analysis,
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
            save_progress(
                stage="completed", label="분석 완료", percent=100,
                checkpoint_name="DIAGNOSIS_COMPLETE",
            )
            self.store.save(project_id, project)
            return {"analysis_seconds": project["timings"]["analysis_total_seconds"]}
        except Exception as exc:
            _, latest = self._row(project_id)
            latest = transition_project(latest, "analysis_failed", lifecycle="FAILED_ANALYSIS", reason=type(exc).__name__, job_id=int(job["job_id"]))
            latest["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            progress = dict(latest.get("analysis_progress") or {})
            progress.update({
                "last_error": latest["error"],
                "pending_operation": None,
                "updated_at": utc_now(),
            })
            latest["analysis_progress"] = progress
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
            project["outputs"] = {**(project.get("outputs") or {}), **outputs}
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

    async def preview_rendering(self, job: dict[str, Any]) -> dict[str, Any]:
        """Create a bounded HD review copy without changing final state/output."""

        project_id = int(job["project_id"])
        _, project = self._row(project_id)
        approved = int(project.get("approved_version") or 0)
        version_row = next(
            (
                item for item in project.get("plan_versions") or []
                if int(item.get("version") or 0) == approved
            ),
            None,
        )
        if not approved or not version_row:
            raise PermanentEditJobError("승인된 immutable edit plan이 없습니다.")
        started = time.perf_counter()
        project["preview_state"] = "rendering"
        project["preview_error"] = None
        self.store.save(project_id, project)
        temporary = None
        try:
            source, backend = self._source(project)
            if backend is not None:
                temporary, local_source, download_seconds = await self._working_source(
                    project, backend, output_ratio=1.5,
                    object_meta=(project.get("proxy") or None),
                )
                source = str(local_source)
                project.setdefault("timings", {})["preview_source_download_seconds"] = download_seconds
                source = str(await self._ensure_analysis_proxy(
                    project_id, project, backend=backend,
                    local_source=Path(source), temporary=temporary,
                    media=((project.get("source") or {}).get("media") or {}),
                    ingest=MediaIngestService(self.store),
                ))
                directory = Path(temporary.name) / "preview"
            else:
                directory = self.store.project_dir(str(project["project_uuid"]), create=True)
            renderer = EditRenderService()
            preview_name = f"preview-v{approved}.mp4"
            output = await asyncio.to_thread(
                renderer.render_timeline,
                source=Path(source), output=directory / preview_name,
                timeline=version_row["plan"].get("render_timeline") or [],
                duration=float(((project.get("source") or {}).get("media") or {}).get("duration") or 0),
                has_audio=bool(((project.get("source") or {}).get("media") or {}).get("has_audio")),
                profile=str((job.get("payload") or {}).get("profile") or "preview_1080p"),
            )
            media = ((project.get("source") or {}).get("media") or {})
            qa = await asyncio.to_thread(
                EditQualityService().validate,
                source=str(directory / preview_name), plan=version_row["plan"],
                output_kind="full",
                expected_duration=float(version_row["plan"].get("estimated_output_duration") or 0),
                require_audio=bool(media.get("has_audio")),
            )
            if backend is not None:
                object_key = await asyncio.to_thread(
                    backend.upload, directory / preview_name,
                    project_uuid=str(project["project_uuid"]), filename=preview_name,
                    content_type="video/mp4",
                )
                metadata = await asyncio.to_thread(backend.head, object_key)
                output = {
                    **{key: value for key, value in output.items() if key != "storage_name"},
                    "object_key": object_key, "storage_backend": "object",
                    "size_bytes": metadata["size_bytes"],
                }
            project.setdefault("outputs", {})["preview"] = output
            project.setdefault("quality_assurance", {})["preview"] = qa
            project["preview_state"] = "succeeded"
            project["preview_error"] = None
            project.setdefault("timings", {})["preview_render_seconds"] = round(time.perf_counter() - started, 3)
            self.store.save(project_id, project)
            return {"preview_render_seconds": project["timings"]["preview_render_seconds"]}
        except Exception as exc:
            _, latest = self._row(project_id)
            latest["preview_state"] = "failed"
            latest["preview_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.store.save(project_id, latest)
            raise
        finally:
            await self._cleanup_working_copy(temporary)

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

    async def _multisource_local_copy(
        self, project: dict[str, Any], source: dict[str, Any], temporary: tempfile.TemporaryDirectory[str],
    ) -> Path:
        key = str(source.get("storage_key") or "")
        suffix = Path(str(source.get("filename") or "source.mp4")).suffix.lower() or ".mp4"
        destination = Path(temporary.name) / f"{source['source_id']}{suffix}"
        if source.get("storage_backend") == "object" or key:
            backend = object_storage_from_env()
            if backend is None:
                raise RuntimeError("Object Storage 연결이 설정되지 않았습니다.")
            await asyncio.to_thread(backend.download, key, destination)
        else:
            storage_name = str(source.get("storage_name") or key)
            project_dir = self.store.project_dir(str(project.get("project_uuid") or ""))
            local = (project_dir / storage_name).resolve()
            if local.parent != project_dir or not local.is_file():
                raise FileNotFoundError("원본 영상을 찾지 못했습니다.")
            destination = local
        return destination

    async def source_analysis(self, job: dict[str, Any]) -> dict[str, Any]:
        """Resume one source from its last durable transcript/classification checkpoint."""

        project_id = int(job["project_id"])
        source_id_value = str((job.get("payload") or {}).get("source_id") or "")
        _, project = self._row(project_id)
        ensure_multisource(project)
        source = find_source(project, source_id_value)
        if source.get("status") == "SOURCE_ANALYZED":
            return {"source_reused": 1}
        started = time.perf_counter()
        source["status"] = "ANALYZING"
        project = transition_project(
            project, "transcribing", lifecycle="ANALYZING",
            reason=f"source analysis {source_id_value[:8]}", job_id=int(job["job_id"]),
        )
        self.store.save(project_id, project)
        temporary = tempfile.TemporaryDirectory(prefix=f"rough-source-{source_id_value[:8]}-")
        try:
            local = await self._multisource_local_copy(project, source, temporary)
            ingest = MediaIngestService(self.store)
            if not source.get("media"):
                media = await asyncio.to_thread(ingest.probe, local)
                source["media"] = media
                source["duration"] = float(media.get("duration") or 0)
                checkpoint(source, "UPLOAD_COMPLETE", media_probed=True)
                self.store.save(project_id, project)
            media = source["media"]
            chunks = plan_transcript_chunks(
                source, chunk_seconds=int(os.getenv("EDIT_TRANSCRIPT_CHUNK_SECONDS", "600")),
            )
            all_segments = []
            analysis = EditAnalysisService()
            for chunk in chunks:
                start = float(chunk["start_time"])
                end = float(chunk["end_time"])
                if chunk.get("transcript_status") != "completed":
                    chunk["attempt"] = int(chunk.get("attempt") or 0) + 1
                    audio = Path(temporary.name) / f"audio-{int(chunk['chunk_index']):04d}.mp3"
                    try:
                        await asyncio.to_thread(ingest.extract_audio, local, audio, end - start, start=start)
                        chunk["audio_extracted_at"] = utc_now()
                        checkpoint(source, "AUDIO_EXTRACTED", first_chunk=int(chunk["chunk_index"]))
                        self.store.save(project_id, project)
                        part = await ingest.transcribe(audio)
                        adjusted = []
                        for row in part.get("segments") or []:
                            adjusted.append({
                                **row,
                                "start": round(float(row.get("start") or 0) + start, 3),
                                "end": round(float(row.get("end") or 0) + start, 3),
                            })
                        chunk["transcript"] = {**part, "segments": adjusted}
                        chunk["transcript_status"] = "completed"
                        chunk["status"] = "transcribed"
                        chunk["error"] = None
                        self.store.save(project_id, project)
                    except Exception as exc:
                        chunk["status"] = "failed"
                        chunk["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                        source["error"] = chunk["error"]
                        self.store.save(project_id, project)
                        raise
                    finally:
                        audio.unlink(missing_ok=True)
                all_segments.extend((chunk.get("transcript") or {}).get("segments") or [])
            source["transcript"] = {
                "text": " ".join(
                    str((chunk.get("transcript") or {}).get("text") or "") for chunk in chunks
                )[:500_000],
                "segments": all_segments[:20_000],
                "provider": "+".join(dict.fromkeys(
                    str((chunk.get("transcript") or {}).get("provider") or "") for chunk in chunks
                )),
                "chunks": len(chunks),
            }
            checkpoint(source, "TRANSCRIBED", completed_chunks=len(chunks), total_chunks=len(chunks))
            self.store.save(project_id, project)

            candidates = semantic_segments(source)
            by_id = {str(row["segment_id"]): row for row in candidates}
            for chunk in chunks:
                if chunk.get("status") == "completed":
                    for saved in chunk.get("segments") or []:
                        if str(saved.get("segment_id")) in by_id:
                            by_id[str(saved["segment_id"])].update(saved)
                    continue
                start, end = float(chunk["start_time"]), float(chunk["end_time"])
                within = [
                    row for row in candidates
                    if float(row.get("end_time") or 0) > start and float(row.get("start_time") or 0) < end
                ]
                try:
                    classified = await analysis.classify_multisource_chunk(source=source, segments=within)
                    for result in classified.get("segments") or []:
                        grounded = by_id.get(str(result.get("segment_id") or ""))
                        if grounded is None:
                            continue
                        grounded.update({
                            "topic": str(result.get("topic") or grounded["topic"])[:200],
                            "role": result.get("role") or grounded["role"],
                            "importance": max(0.0, min(1.0, float(result.get("importance") or grounded["importance"]))),
                            "quality": max(0.0, min(1.0, float(result.get("quality") or grounded["quality"]))),
                            "confidence": max(0.0, min(1.0, float(result.get("confidence") or grounded["confidence"]))),
                            "classification_reason": str(result.get("reason") or "")[:600],
                        })
                    chunk["segments"] = [deepcopy(row) for row in within]
                    chunk["status"] = "completed"
                    chunk["error"] = None
                    self.store.save(project_id, project)
                except Exception as exc:
                    chunk["status"] = "failed"
                    chunk["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                    source["error"] = chunk["error"]
                    self.store.save(project_id, project)
                    raise
            source["segments"] = candidates
            checkpoint(source, "SEGMENTED", segment_count=len(candidates))

            if not (source.get("signals") or {}).get("scanned"):
                silences, scenes = await asyncio.gather(
                    asyncio.to_thread(ingest.detect_silences_chunked, local, float(media["duration"])),
                    asyncio.to_thread(ingest.detect_scenes, local, float(media["duration"])),
                )
                source["signals"] = {"silences": silences, "scene_changes": scenes, "scanned": True}
                self.store.save(project_id, project)
            if (source.get("visual_analysis") or {}).get("status") not in {"succeeded", "failed"}:
                source["visual_analysis"] = await self._run_visual_analysis(
                    source=local, transcript=source["transcript"], media=media,
                    scenes=(source.get("signals") or {}).get("scene_changes") or [], analysis=analysis,
                    max_frames=30,
                )
                self.store.save(project_id, project)
            apply_visual_quality(source)
            checkpoint(source, "SOURCE_ANALYZED", segment_count=len(candidates))
            source["analysis_seconds"] = round(time.perf_counter() - started, 3)
            project["analysis_cache_version"] = int(project.get("analysis_cache_version") or 1) + 1
            self.store.save(project_id, project)
            await self._queue_story_if_ready(project_id, project)
            return {"source_analysis_seconds": source["analysis_seconds"], "chunks": len(chunks)}
        except Exception as exc:
            _, latest = self._row(project_id)
            failed_source = find_source(latest, source_id_value)
            failed_source["retry_count"] = int(job.get("attempt") or failed_source.get("retry_count") or 0)
            failed_source["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            failed_source["status"] = (
                "FAILED_ANALYSIS"
                if int(job.get("attempt") or 0) >= int(job.get("max_attempts") or 1)
                else "ANALYZING"
            )
            self.store.save(project_id, latest)
            if failed_source["status"] == "FAILED_ANALYSIS":
                await self._queue_story_if_ready(project_id, latest)
            raise
        finally:
            await asyncio.to_thread(temporary.cleanup)

    async def _queue_story_if_ready(self, project_id: int, project: dict[str, Any]) -> None:
        sources = project.get("sources") or []
        if not project.get("uploads_finalized") or not sources:
            return
        if any(source.get("status") not in {"SOURCE_ANALYZED", "FAILED_ANALYSIS"} for source in sources):
            return
        from edit_job_queue import EditJobQueue
        queue = EditJobQueue(getattr(self.store, "_connect", None))
        job = queue.enqueue(
            project_id, "story_planning", payload={"source_count": len(sources)},
            idempotency_key=f"story:{project_id}:sources:{len(sources)}:cache:{project.get('analysis_cache_version', 1)}",
            max_attempts=3, priority=52,
        )
        project["jobs"] = sorted(set((project.get("jobs") or []) + [int(job["job_id"])]))
        project["story_plan_state"] = "queued"
        self.store.save(project_id, project)

    async def story_planning(self, job: dict[str, Any]) -> dict[str, Any]:
        project_id = int(job["project_id"])
        _, project = self._row(project_id)
        ensure_multisource(project)
        ready = [source for source in project.get("sources") or [] if source.get("status") == "SOURCE_ANALYZED"]
        if not ready:
            raise PermanentEditJobError("분석이 완료된 원본이 없습니다.")
        started = time.perf_counter()
        project["story_plan_state"] = "planning"
        project = transition_project(
            project, "diagnosing", lifecycle="ANALYZING",
            reason="multi-source story planning", job_id=int(job["job_id"]),
        )
        self.store.save(project_id, project)
        project["duplicate_groups"] = deduplicate_segments(ready)
        analysis = EditAnalysisService()
        if not project.get("evidence_snapshot"):
            settings = project.get("settings") or {}
            evidence, trace, strategy = await analysis.collect_evidence(
                topic=str(settings.get("topic") or ""), purpose=str(settings.get("purpose") or ""),
                strategy_id=settings.get("content_strategy_id"),
            )
            project["evidence_snapshot"], project["evidence_trace"] = evidence, trace
            project["strategy_snapshot"] = strategy
            self.store.save(project_id, project)
        candidates = bounded_story_candidates(ready)
        reasoning = await analysis.plan_multisource_story(
            candidates=candidates, evidence=project.get("evidence_snapshot") or {},
            strategy=project.get("strategy_snapshot"), settings=project.get("settings") or {},
        )
        plan = apply_story_reasoning(
            project, reasoning,
            target_length_seconds=float((project.get("settings") or {}).get("target_length_seconds") or 0),
        )
        versions = project.get("plan_versions") or []
        next_version = int(versions[-1].get("version") or 0) + 1 if versions else 1
        versions.append({
            "version": next_version, "status": "proposed", "created_at": utc_now(),
            "source": "multisource_story_planner", "user_request": "",
            "revision_summary": (
                "복구된 원본 분석을 포함해 러프컷 구성안을 갱신"
                if next_version > 1 else "중복 발언 제거와 채널 근거를 적용한 최초 러프컷 구성안"
            ),
            "diff": [], "plan": plan,
        })
        project["plan_versions"] = versions
        project["approved_version"], project["approved_at"] = None, None
        project["story_plan_state"] = "awaiting_approval"
        project = transition_project(project, "proposed", lifecycle="AWAITING_REVIEW", reason="multi-source story ready", job_id=int(job["job_id"]))
        project.setdefault("timings", {})["story_planning_seconds"] = round(time.perf_counter() - started, 3)
        self.store.save(project_id, project)
        return {"story_planning_seconds": project["timings"]["story_planning_seconds"]}

    async def rough_cut_rendering(self, job: dict[str, Any]) -> dict[str, Any]:
        """Render only an approved immutable multi-source timeline."""

        project_id = int(job["project_id"])
        _, project = self._row(project_id)
        ensure_multisource(project)
        approved = int(project.get("approved_version") or 0)
        snapshot = project.get("approved_plan_snapshot") or {}
        if not approved or int(snapshot.get("version") or 0) != approved:
            raise PermanentEditJobError("승인된 immutable 러프컷 구성안이 없습니다.")
        plan = snapshot.get("plan") or {}
        timeline = plan.get("timeline") or plan.get("render_timeline") or []
        validate_timeline(timeline, project.get("sources") or [])
        started = time.perf_counter()
        project = transition_project(
            project, "rendering", lifecycle="ROUGH_CUT_RENDERING",
            reason="approved multi-source rough cut claimed", job_id=int(job["job_id"]),
        )
        self.store.save(project_id, project)
        temporary = tempfile.TemporaryDirectory(prefix=f"rough-render-{project_id}-")
        backend = object_storage_from_env()
        try:
            needed = {str(row["source_id"]) for row in timeline}
            paths: dict[str, Path] = {}
            for source in project.get("sources") or []:
                if str(source.get("source_id")) not in needed:
                    continue
                paths[str(source["source_id"])] = await self._multisource_local_copy(project, source, temporary)
            output_dir = Path(temporary.name) / "output"
            output = output_dir / f"rough-cut-v{approved}.mp4"
            renderer = EditRenderService()
            rendered = await asyncio.to_thread(
                renderer.render_multisource_timeline,
                sources=paths, source_rows=project.get("sources") or [], output=output,
                timeline=timeline, profile="preview_1080p",
            )
            media = next((source.get("media") or {} for source in project.get("sources") or [] if str(source.get("source_id")) in needed), {})
            qa = await asyncio.to_thread(
                EditQualityService().validate,
                source=str(output), plan={"render_timeline": timeline}, output_kind="full",
                expected_duration=float(plan.get("estimated_output_duration") or 0),
                require_audio=bool(media.get("has_audio", True)),
            )
            if qa.get("status") == "failed":
                raise RuntimeError("러프컷 자동 품질 검사를 통과하지 못했습니다.")
            if backend is not None:
                final_key = backend.key(str(project["project_uuid"]), output.name)
                staging_key = final_key + f".job-{int(job['job_id'])}.staging"
                await asyncio.to_thread(backend.upload_key, output, staging_key, content_type="video/mp4")
                try:
                    published = await asyncio.to_thread(
                        backend.publish_staged,
                        {"rough_cut": {"staging_key": staging_key, "final_key": final_key, "content_type": "video/mp4"}},
                    )
                except Exception:
                    try:
                        await asyncio.to_thread(backend.delete, staging_key)
                    except Exception:
                        pass
                    raise
                rendered = {
                    **{key: value for key, value in rendered.items() if key != "storage_name"},
                    "object_key": final_key, "storage_backend": "object",
                    "size_bytes": int(published["rough_cut"].get("size_bytes") or rendered.get("size_bytes") or 0),
                }
            else:
                directory = self.store.project_dir(str(project["project_uuid"]), create=True)
                final = directory / output.name
                await asyncio.to_thread(shutil.copy2, output, final)
                rendered["storage_name"] = final.name
            edit_log = [{
                "order": index, "source_id": row.get("source_id"),
                "filename": row.get("filename"), "source_start": row.get("source_start"),
                "source_end": row.get("source_end"), "role": row.get("role"),
                "reason": row.get("reason"), "output": "rough_cut",
            } for index, row in enumerate(timeline, 1)]
            project.setdefault("outputs", {})["rough_cut"] = rendered
            project["applied_edit_log"] = edit_log
            project.setdefault("quality_assurance", {})["rough_cut"] = qa
            project.setdefault("render_runs", []).append({
                "version": approved, "created_at": utc_now(), "output": rendered,
                "edit_log": edit_log, "quality_assurance": qa, "job_id": int(job["job_id"]),
            })
            project.setdefault("timings", {})["rough_cut_render_seconds"] = round(time.perf_counter() - started, 3)
            project = transition_project(
                project, "completed", lifecycle="ROUGH_CUT_COMPLETED",
                reason="approved multi-source rough cut completed", job_id=int(job["job_id"]),
            )
            project["error"] = None
            self.store.save(project_id, project)
            return {"rough_cut_render_seconds": project["timings"]["rough_cut_render_seconds"]}
        except Exception as exc:
            _, latest = self._row(project_id)
            latest = transition_project(
                latest, "render_failed", lifecycle="FAILED_RENDER",
                reason=type(exc).__name__, job_id=int(job["job_id"]),
            )
            latest["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.store.save(project_id, latest)
            raise
        finally:
            await asyncio.to_thread(temporary.cleanup)
