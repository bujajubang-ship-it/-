"""Safe ffmpeg renderer for approved collaborative edit timelines."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from edit_plan_service import validate_approved_timeline
from edit_project_store import utc_now


class EditRenderError(RuntimeError):
    pass


class EditRenderService:
    def __init__(self, ffmpeg: str | None = None) -> None:
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""

    @staticmethod
    def advisory_log(plan: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "id": item.get("id"),
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "type": item.get("type"),
                "instruction": item.get("instruction"),
                "asset_requirements": item.get("asset_requirements") or [],
                "overlay_text": item.get("overlay_text") or "",
                "priority": item.get("priority"),
                "confidence": item.get("confidence"),
                "reason": item.get("reason"),
                "applied": False,
                "render_mode": "suggestion_only",
            }
            for item in plan.get("enhancements") or []
        ]

    @staticmethod
    def _thread_count() -> int:
        try:
            return max(1, min(4, int(os.getenv("EDIT_FFMPEG_THREADS", "1"))))
        except ValueError:
            return 1

    @staticmethod
    def _filter(timeline: list[dict[str, Any]], *, has_audio: bool) -> str:
        """Join seek-bounded inputs without buffering the full source."""
        filters = []
        video_labels = []
        audio_labels = []
        for index, _item in enumerate(timeline):
            filters.append(f"[{index}:v]setpts=PTS-STARTPTS[v{index}]")
            video_labels.append(f"[v{index}]")
            if has_audio:
                filters.append(f"[{index}:a]asetpts=PTS-STARTPTS[a{index}]")
                audio_labels.append(f"[a{index}]")
        if has_audio:
            inputs = "".join(
                value
                for pair in zip(video_labels, audio_labels)
                for value in pair
            )
            filters.append(f"{inputs}concat=n={len(timeline)}:v=1:a=1[vout][aout]")
        else:
            filters.append(f"{''.join(video_labels)}concat=n={len(timeline)}:v=1:a=0[vout]")
        return ";".join(filters)

    def render_timeline(
        self,
        *,
        source: Path,
        output: Path,
        timeline: list[dict[str, Any]],
        duration: float,
        has_audio: bool,
    ) -> dict[str, Any]:
        if not self.ffmpeg:
            raise EditRenderError("ffmpeg가 설치되어 있지 않습니다.")
        validate_approved_timeline(timeline, duration)
        output.parent.mkdir(parents=True, exist_ok=True)
        output_duration = sum(
            float(item["source_end"]) - float(item["source_start"])
            for item in timeline
        )
        if self._valid_existing_output(output):
            return {
                "storage_name": output.name,
                "filename": output.name,
                "duration": round(output_duration, 3),
                "size_bytes": output.stat().st_size,
                "created_at": utc_now(),
                "codec": "h264+aac" if has_audio else "h264",
                "reused": True,
            }
        part = output.with_name(f".{output.stem}.part{output.suffix}")
        part.unlink(missing_ok=True)
        filter_complex = self._filter(timeline, has_audio=has_audio)
        threads = str(self._thread_count())
        command = self._command(
            source=str(source), timeline=timeline, has_audio=has_audio,
            threads=threads, output=["-movflags", "+faststart", str(part), "-y"],
        )
        timeout = max(300, min(21600, int(output_duration * 8) + 300))
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            part.unlink(missing_ok=True)
            raise EditRenderError("편집 렌더링 시간이 초과됐습니다.") from exc
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise EditRenderError("ffmpeg 실행 파일을 시작하지 못했습니다.") from exc
        if result.returncode != 0 or not part.exists() or part.stat().st_size == 0:
            part.unlink(missing_ok=True)
            detail = (result.stderr or "ffmpeg render failed")[-600:]
            raise EditRenderError(f"편집본을 만들지 못했습니다: {detail}")
        os.replace(part, output)
        return {
            "storage_name": output.name,
            "filename": output.name,
            "duration": round(output_duration, 3),
            "size_bytes": output.stat().st_size,
            "created_at": utc_now(),
            "codec": "h264+aac" if has_audio else "h264",
        }

    def _command(
        self, *, source: str, timeline: list[dict[str, Any]], has_audio: bool,
        threads: str, output: list[str],
    ) -> list[str]:
        filter_complex = self._filter(timeline, has_audio=has_audio)
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-filter_complex_threads", threads,
        ]
        for item in timeline:
            start = float(item["source_start"])
            segment_duration = float(item["source_end"]) - start
            command.extend(
                [
                    "-threads:v", threads,
                    "-ss", f"{start:.3f}",
                    "-t", f"{segment_duration:.3f}",
                    "-i", source,
                ]
            )
        command.extend(["-filter_complex", filter_complex, "-map", "[vout]"])
        if has_audio:
            command.extend(["-map", "[aout]"])
        command.extend(
            [
                "-c:v", "libx264", "-threads:v", threads,
                "-preset", "veryfast", "-crf", "22",
                "-pix_fmt", "yuv420p",
            ]
        )
        if has_audio:
            command.extend(["-c:a", "aac", "-b:a", "160k"])
        command.extend(output)
        return command

    def render_timeline_to_object(
        self, *, source_url: str, backend: Any, object_key: str,
        timeline: list[dict[str, Any]], duration: float, has_audio: bool,
    ) -> dict[str, Any]:
        if not self.ffmpeg:
            raise EditRenderError("ffmpeg가 설치되어 있지 않습니다.")
        validate_approved_timeline(timeline, duration)
        output_duration = sum(float(item["source_end"]) - float(item["source_start"]) for item in timeline)
        try:
            existing = backend.head(object_key)
        except Exception:
            existing = None
        if existing and int(existing.get("size_bytes") or 0) > 0:
            return {
                "object_key": object_key, "filename": object_key.rsplit("/", 1)[-1],
                "duration": round(output_duration, 3), "size_bytes": existing["size_bytes"],
                "created_at": utc_now(), "codec": "h264+aac" if has_audio else "h264",
                "storage_backend": "object", "reused": True,
            }
        threads = str(self._thread_count())
        command = self._command(
            source=source_url, timeline=timeline, has_audio=has_audio, threads=threads,
            output=["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"],
        )
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            if process.stdout is None:
                raise EditRenderError("ffmpeg 출력 스트림을 시작하지 못했습니다.")
            backend.client.upload_fileobj(
                process.stdout, backend.bucket, object_key,
                ExtraArgs={"ContentType": "video/mp4"},
            )
            stderr = (process.stderr.read() if process.stderr else b"")[-1200:]
            return_code = process.wait(timeout=max(300, min(21600, int(output_duration * 8) + 300)))
        except Exception as exc:
            process.terminate()
            try:
                process.wait(timeout=10)
            except Exception:
                process.kill()
            try:
                backend.delete(object_key)
            except Exception:
                pass
            if isinstance(exc, EditRenderError):
                raise
            raise EditRenderError("Object Storage 렌더 전송에 실패했습니다.") from exc
        if return_code != 0:
            try:
                backend.delete(object_key)
            except Exception:
                pass
            raise EditRenderError(f"편집본을 만들지 못했습니다: {stderr.decode('utf-8', 'replace')[-600:]}")
        metadata = backend.head(object_key)
        if int(metadata.get("size_bytes") or 0) <= 0:
            raise EditRenderError("Object Storage 편집본이 비어 있습니다.")
        return {
            "object_key": object_key, "filename": object_key.rsplit("/", 1)[-1],
            "duration": round(output_duration, 3), "size_bytes": metadata["size_bytes"],
            "created_at": utc_now(), "codec": "h264+aac" if has_audio else "h264",
            "storage_backend": "object",
        }

    def render_project_object(
        self, *, source_url: str, backend: Any, project_uuid: str,
        plan: dict[str, Any], media: dict[str, Any], version: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        outputs: dict[str, Any] = {}
        edit_log: list[dict[str, Any]] = []
        duration = float(media.get("duration") or 0)
        has_audio = bool(media.get("has_audio"))
        full_name = f"edited-v{version}.mp4"
        outputs["full"] = self.render_timeline_to_object(
            source_url=source_url, backend=backend,
            object_key=backend.key(project_uuid, full_name),
            timeline=plan.get("render_timeline") or [], duration=duration, has_audio=has_audio,
        )
        for order, item in enumerate(plan.get("render_timeline") or [], start=1):
            edit_log.append({"order": order, "source_start": item["source_start"], "source_end": item["source_end"], "action": item.get("action") or "keep", "reason": item.get("reason") or "", "output": "full"})
        short_timeline = plan.get("short_timeline") or []
        if plan.get("create_short_highlight") and short_timeline:
            short_name = f"short-v{version}.mp4"
            outputs["short"] = self.render_timeline_to_object(
                source_url=source_url, backend=backend,
                object_key=backend.key(project_uuid, short_name),
                timeline=short_timeline, duration=duration, has_audio=has_audio,
            )
            for order, item in enumerate(short_timeline, start=1):
                edit_log.append({"order": order, "source_start": item["source_start"], "source_end": item["source_end"], "action": item.get("action") or "short_highlight", "reason": item.get("reason") or "", "output": "short"})
        decision_name = f"edit-decision-v{version}.json"
        decision_payload = {
            "version": version, "approved_plan": plan, "applied_edit_log": edit_log,
            "advisory_edit_log": self.advisory_log(plan),
            "exports": {key: value for key, value in outputs.items()},
        }
        decision_key = backend.upload_bytes(
            json.dumps(decision_payload, ensure_ascii=False, indent=2).encode("utf-8"),
            project_uuid=project_uuid, filename=decision_name, content_type="application/json",
        )
        decision_meta = backend.head(decision_key)
        outputs["decision"] = {
            "object_key": decision_key, "filename": decision_name,
            "size_bytes": decision_meta["size_bytes"], "created_at": utc_now(),
            "content_type": "application/json", "storage_backend": "object",
        }
        return outputs, edit_log

    @staticmethod
    def _valid_existing_output(path: Path) -> bool:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return False
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            return result.returncode == 0 and float(result.stdout.strip() or 0) > 0
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return False

    def render_project(
        self,
        *,
        source: Path,
        directory: Path,
        plan: dict[str, Any],
        media: dict[str, Any],
        version: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        outputs: dict[str, Any] = {}
        edit_log = []
        duration = float(media.get("duration") or 0)
        has_audio = bool(media.get("has_audio"))
        full_name = f"edited-v{version}.mp4"
        outputs["full"] = self.render_timeline(
            source=source,
            output=directory / full_name,
            timeline=plan.get("render_timeline") or [],
            duration=duration,
            has_audio=has_audio,
        )
        for order, item in enumerate(plan.get("render_timeline") or [], start=1):
            edit_log.append(
                {
                    "order": order,
                    "source_start": item["source_start"],
                    "source_end": item["source_end"],
                    "action": item.get("action") or "keep",
                    "reason": item.get("reason") or "",
                    "output": "full",
                }
            )
        short_timeline = plan.get("short_timeline") or []
        if plan.get("create_short_highlight") and short_timeline:
            short_name = f"short-v{version}.mp4"
            outputs["short"] = self.render_timeline(
                source=source,
                output=directory / short_name,
                timeline=short_timeline,
                duration=duration,
                has_audio=has_audio,
            )
            for order, item in enumerate(short_timeline, start=1):
                edit_log.append(
                    {
                        "order": order,
                        "source_start": item["source_start"],
                        "source_end": item["source_end"],
                        "action": item.get("action") or "short_highlight",
                        "reason": item.get("reason") or "",
                        "output": "short",
                    }
                )
        decision_name = f"edit-decision-v{version}.json"
        decision_path = directory / decision_name
        decision_part = directory / f".{decision_name}.part"
        decision_payload = {
            "version": version,
            "approved_plan": plan,
            "applied_edit_log": edit_log,
            "advisory_edit_log": self.advisory_log(plan),
            "exports": {
                key: {k: v for k, v in value.items() if k != "storage_name"}
                for key, value in outputs.items()
            },
        }
        try:
            decision_part.write_text(
                json.dumps(decision_payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.replace(decision_part, decision_path)
        finally:
            decision_part.unlink(missing_ok=True)
        outputs["decision"] = {
            "storage_name": decision_name,
            "filename": decision_name,
            "size_bytes": decision_path.stat().st_size,
            "created_at": utc_now(),
            "content_type": "application/json",
        }
        return outputs, edit_log
