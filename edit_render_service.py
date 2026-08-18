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
    def _filter(timeline: list[dict[str, Any]], *, has_audio: bool) -> str:
        filters = []
        video_labels = []
        audio_labels = []
        for index, item in enumerate(timeline):
            start = float(item["source_start"])
            end = float(item["source_end"])
            filters.append(
                f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]"
            )
            video_labels.append(f"[v{index}]")
            if has_audio:
                filters.append(
                    f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]"
                )
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
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(source),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
        ]
        if has_audio:
            command.extend(["-map", "[aout]"])
        command.extend(
            [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                "-pix_fmt", "yuv420p",
            ]
        )
        if has_audio:
            command.extend(["-c:a", "aac", "-b:a", "160k"])
        command.extend(["-movflags", "+faststart", str(part), "-y"])
        timeout = max(300, min(7200, int(output_duration * 6) + 180))
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
