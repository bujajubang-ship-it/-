"""Timecoded visual evidence extraction and deterministic audio/visual fusion."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


VISUAL_FALLBACK_MESSAGE = "visual analysis failed, audio-only fallback used"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _timecode(seconds: float) -> str:
    value = max(0, int(round(seconds * 1000)))
    whole, millis = divmod(value, 1000)
    hours, remainder = divmod(whole, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


class FrameExtractionError(RuntimeError):
    pass


class TimecodedFrameExtractor:
    """Extract bounded JPEG evidence from a proxy without modifying the video."""

    def __init__(self, ffmpeg: str | None = None) -> None:
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""

    @staticmethod
    def _effective_interval(duration: float, requested: float, max_frames: int) -> float:
        requested = max(0.5, requested)
        if duration <= 0:
            return requested
        return max(requested, duration / max(1, max_frames))

    def extract(
        self, *, source: Path, output_dir: Path, duration: float,
        scene_times: list[float] | None = None, interval_seconds: float | None = None,
        max_frames: int | None = None,
    ) -> dict[str, Any]:
        if not self.ffmpeg:
            raise FrameExtractionError("ffmpeg가 설치되어 있지 않습니다.")
        requested = max(0.5, _number(
            interval_seconds if interval_seconds is not None else os.getenv("EDIT_VISUAL_FRAME_INTERVAL_SECONDS", "2"),
            2.0,
        ))
        max_frames = max(12, min(300, int(
            max_frames if max_frames is not None else os.getenv("EDIT_VISUAL_MAX_FRAMES", "120")
        )))
        configured_extra = max(0, min(30, int(os.getenv("EDIT_VISUAL_SCENE_EXTRA_FRAMES", "20"))))
        scene_reserve = min(configured_extra, max_frames // 4)
        periodic_limit = max(8, max_frames - scene_reserve)
        interval = self._effective_interval(duration, requested, periodic_limit)
        output_dir.mkdir(parents=True, exist_ok=True)
        pattern = output_dir / "periodic-%06d.jpg"
        command = [
            self.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-vf", f"fps=1/{interval:.6f},scale=640:-2", "-q:v", "4",
            str(pattern), "-y",
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                timeout=max(120, min(3600, int(max(0, duration) * 1.5) + 120)),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FrameExtractionError("타임코드 프레임 추출이 실패했습니다.") from exc
        periodic = sorted(output_dir.glob("periodic-*.jpg"))
        if result.returncode != 0 or not periodic:
            raise FrameExtractionError("타임코드 프레임을 추출하지 못했습니다.")

        frames: list[dict[str, Any]] = []
        occupied: list[float] = []
        for index, path in enumerate(periodic[:periodic_limit]):
            seconds = min(max(0.0, duration - 0.001), index * interval) if duration > 0 else index * interval
            renamed = output_dir / f"frame-{index + 1:04d}-{int(seconds * 1000):010d}.jpg"
            path.replace(renamed)
            frames.append({
                "frame_id": f"frame-{index + 1:04d}", "timecode_seconds": round(seconds, 3),
                "timecode": _timecode(seconds), "kind": "periodic", "path": str(renamed),
            })
            occupied.append(seconds)

        extra_limit = min(configured_extra, max(0, max_frames - len(frames)))
        extras = []
        for seconds in sorted({_number(value) for value in (scene_times or []) if 0 <= _number(value) < duration}):
            if len(extras) >= extra_limit:
                break
            if any(abs(seconds - existing) < min(1.0, interval * 0.35) for existing in occupied):
                continue
            path = output_dir / f"scene-{len(extras) + 1:04d}-{int(seconds * 1000):010d}.jpg"
            scene_command = [
                self.ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}",
                "-i", str(source), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4",
                str(path), "-y",
            ]
            try:
                scene_result = subprocess.run(scene_command, capture_output=True, text=True, timeout=45)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if scene_result.returncode == 0 and path.is_file() and path.stat().st_size > 0:
                extras.append({
                    "frame_id": f"scene-{len(extras) + 1:04d}", "timecode_seconds": round(seconds, 3),
                    "timecode": _timecode(seconds), "kind": "scene_change", "path": str(path),
                })
                occupied.append(seconds)
        frames.extend(extras)
        frames.sort(key=lambda item: (item["timecode_seconds"], item["kind"], item["frame_id"]))
        return {
            "schema_version": 1, "status": "extracted", "source": "analysis_proxy_or_bounded_source",
            "requested_interval_seconds": requested, "effective_interval_seconds": round(interval, 3),
            "duration_seconds": round(duration, 3), "frame_count": len(frames), "frames": frames,
        }


def public_frame_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    value = json.loads(json.dumps(manifest, ensure_ascii=False))
    for frame in value.get("frames") or []:
        frame.pop("path", None)
    return value


def transcript_context(transcript: dict[str, Any], at: float, radius: float = 2.5) -> str:
    values = []
    for segment in transcript.get("segments") or []:
        if _number(segment.get("end")) < at - radius or _number(segment.get("start")) > at + radius:
            continue
        text = str(segment.get("text") or "").strip()
        if text:
            values.append(text)
    return " ".join(values)[:500]


def build_audio_visual_segments(
    frame_results: list[dict[str, Any]], transcript: dict[str, Any], *,
    duration: float, interval_seconds: float,
) -> list[dict[str, Any]]:
    """Turn frame judgments into auditable original-timecode edit windows."""
    windows = []
    for index, frame in enumerate(sorted(frame_results, key=lambda item: _number(item.get("timecode_seconds")))):
        start = max(0.0, _number(frame.get("timecode_seconds")))
        next_time = (
            _number(sorted(frame_results, key=lambda item: _number(item.get("timecode_seconds")))[index + 1].get("timecode_seconds"))
            if index + 1 < len(frame_results) else min(duration, start + interval_seconds)
        )
        end = min(duration, max(start + 0.25, next_time))
        speech = transcript_context(transcript, (start + end) / 2, radius=max(1.0, interval_seconds))
        audio_score = 0.72 if speech else 0.22
        visual_score = max(0.0, min(1.0, _number(frame.get("visual_score"), 0.5)))
        alignment = max(0.0, min(1.0, _number(frame.get("speech_alignment_score"), 0.5 if speech else 0.2)))
        context_score = round(visual_score * 0.5 + audio_score * 0.25 + alignment * 0.25, 3)
        decision = str(frame.get("edit_decision") or "keep")
        if decision not in {"keep", "cut", "shorten", "highlight"}:
            decision = "keep"
        reason = str(frame.get("reason") or "시각 근거 평가")[:900]
        if speech:
            reason = f"{reason} · 설명 맥락: {speech[:180]}"
        windows.append({
            "start_time": round(start, 3), "end_time": round(end, 3),
            "audio_score": round(audio_score, 3), "visual_score": round(visual_score, 3),
            "context_score": context_score, "edit_decision": decision, "reason": reason,
            "frame_ids": [str(frame.get("frame_id") or "")],
            "thumbnail_candidate": bool(frame.get("thumbnail_candidate")),
            "site_value_tags": list(frame.get("site_value_tags") or [])[:8],
        })
    merged: list[dict[str, Any]] = []
    for item in windows:
        if (
            merged and merged[-1]["edit_decision"] == item["edit_decision"]
            and item["start_time"] <= merged[-1]["end_time"] + 0.15
        ):
            previous = merged[-1]
            count = len(previous["frame_ids"])
            previous["end_time"] = item["end_time"]
            for field in ("audio_score", "visual_score", "context_score"):
                previous[field] = round((previous[field] * count + item[field]) / (count + 1), 3)
            previous["frame_ids"].extend(item["frame_ids"])
            previous["thumbnail_candidate"] = previous["thumbnail_candidate"] or item["thumbnail_candidate"]
            previous["site_value_tags"] = list(dict.fromkeys(previous["site_value_tags"] + item["site_value_tags"]))[:8]
            if item["reason"] not in previous["reason"]:
                previous["reason"] = (previous["reason"] + " / " + item["reason"])[:1200]
        else:
            merged.append(dict(item))
    return merged


def fuse_plan_with_visual(plan: dict[str, Any], visual_analysis: dict[str, Any]) -> dict[str, Any]:
    """Ensure the approved/rendered plan carries and uses multimodal scores."""
    output = dict(plan or {})
    proposals = [dict(item) for item in output.get("segments") or []]
    visual_segments = visual_analysis.get("segments") or []
    for proposal in proposals:
        overlaps = [
            item for item in visual_segments
            if _number(item.get("end_time")) > _number(proposal.get("start_time"))
            and _number(item.get("start_time")) < _number(proposal.get("end_time"))
        ]
        if not overlaps:
            proposal.setdefault("audio_score", 0.5)
            proposal.setdefault("visual_score", None)
            proposal.setdefault("context_score", proposal.get("audio_score"))
            proposal.setdefault("visual_evidence", [])
            continue
        visual_score = sum(_number(item.get("visual_score"), 0.5) for item in overlaps) / len(overlaps)
        visual_context = sum(_number(item.get("context_score"), 0.5) for item in overlaps) / len(overlaps)
        # The diagnosis model scored transcript meaning/repetition.  Preserve
        # that semantic audio judgment instead of replacing it with the visual
        # sampler's coarse speech-presence estimate.
        audio_score = max(0.0, min(1.0, _number(
            proposal.get("audio_score"),
            sum(_number(item.get("audio_score"), 0.5) for item in overlaps) / len(overlaps),
        )))
        proposal["audio_score"] = round(audio_score, 3)
        proposal["visual_score"] = round(visual_score, 3)
        proposal["context_score"] = round(audio_score * 0.4 + visual_context * 0.6, 3)
        proposal["visual_evidence"] = [frame for item in overlaps for frame in item.get("frame_ids") or []][:20]
        best = max(overlaps, key=lambda item: _number(item.get("context_score")))
        if proposal.get("action") in {"cut", "trim"} and _number(proposal.get("visual_score")) >= 0.72:
            proposal["action"] = "shorten"
            proposal["reason"] = (str(proposal.get("reason") or "") + " · 강한 현장 화면을 보존하도록 축약으로 완화")[:1000]
        elif proposal.get("action") == "keep" and best.get("edit_decision") == "highlight":
            proposal["action"] = "highlight"
        proposal["reason"] = (str(proposal.get("reason") or "") + " · 시각 근거: " + str(best.get("reason") or ""))[:1000]

    for index, item in enumerate(visual_segments):
        decision = str(item.get("edit_decision") or "keep")
        if decision not in {"cut", "shorten", "highlight"} or _number(item.get("context_score")) < 0.58:
            continue
        if any(
            _number(existing.get("end_time")) > _number(item.get("start_time"))
            and _number(existing.get("start_time")) < _number(item.get("end_time"))
            for existing in proposals
        ):
            continue
        proposals.append({
            "id": f"visual-{index + 1}", "start_time": item.get("start_time"),
            "end_time": item.get("end_time"), "action": decision,
            "reason": item.get("reason"), "confidence": item.get("context_score"),
            "expected_effect": "현장 화면 가치와 설명 일치도를 반영한 리듬 개선",
            "destination": "", "audio_score": item.get("audio_score"),
            "visual_score": item.get("visual_score"), "context_score": item.get("context_score"),
            "visual_evidence": item.get("frame_ids") or [],
        })
    output["segments"] = sorted(proposals, key=lambda item: (_number(item.get("start_time")), _number(item.get("end_time"))))
    output["decision_basis"] = "audio_transcript+visual_frames"
    return output
