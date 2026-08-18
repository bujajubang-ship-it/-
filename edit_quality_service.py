"""Post-render structural and media validation for approved edit plans."""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from edit_project_store import utc_now


class EditQualityError(RuntimeError):
    retryable = False


class EditQualityService:
    @staticmethod
    def _probe(source: str) -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", source],
                capture_output=True, text=True, timeout=120,
            )
            payload = json.loads(result.stdout or "{}") if result.returncode == 0 else {}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            raise EditQualityError("렌더 결과를 ffprobe로 검증하지 못했습니다.") from exc
        streams = payload.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        try:
            duration = float((payload.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        return {"duration": duration, "video": video, "audio": audio, "format": payload.get("format") or {}}

    @staticmethod
    def _sample_warnings(source: str, duration: float) -> list[dict[str, Any]]:
        scan = min(max(duration, 0), 45.0)
        if scan <= 0:
            return []
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-t", f"{scan:.3f}", "-i", source,
                    "-vf", "scale=160:-2,blackdetect=d=1.5:pix_th=0.08",
                    "-af", "silencedetect=noise=-45dB:d=3", "-f", "null", "-",
                ],
                capture_output=True, text=True, timeout=max(120, int(scan * 4)),
            )
        except (OSError, subprocess.TimeoutExpired):
            return [{"code": "sample_scan_unavailable", "severity": "info", "message": "black/audio gap 표본 검사를 완료하지 못했습니다."}]
        warnings = []
        if re.search(r"black_duration:([2-9]|[1-9][0-9])", result.stderr):
            warnings.append({"code": "black_frames", "severity": "warning", "message": "초반 표본에서 긴 black frame 구간이 감지됐습니다."})
        if re.search(r"silence_duration:([4-9]|[1-9][0-9])", result.stderr):
            warnings.append({"code": "audio_gap", "severity": "warning", "message": "초반 표본에서 긴 audio gap이 감지됐습니다."})
        return warnings

    def validate(
        self, *, source: str, plan: dict[str, Any], output_kind: str,
        expected_duration: float, require_audio: bool,
    ) -> dict[str, Any]:
        probe = self._probe(source)
        fatal = []
        warnings = []
        actual = float(probe["duration"] or 0)
        tolerance = max(1.2, float(expected_duration) * 0.02)
        if not probe["video"]:
            fatal.append("video_stream_missing")
        if require_audio and not probe["audio"]:
            fatal.append("audio_stream_missing")
        if actual <= 0 or abs(actual - float(expected_duration)) > tolerance:
            fatal.append("duration_mismatch")
        timeline_key = "short_timeline" if output_kind == "short" else "render_timeline"
        timeline = plan.get(timeline_key) or []
        tiny = [item for item in timeline if float(item.get("source_end") or 0) - float(item.get("source_start") or 0) < 0.3]
        if tiny:
            warnings.append({"code": "very_short_cuts", "severity": "warning", "message": f"0.3초 미만 컷 {len(tiny)}개가 있습니다."})
        target = float(plan.get("target_length_seconds") or 0) if output_kind == "full" else float(plan.get("short_target_seconds") or 0)
        if target and actual > target * 1.08:
            warnings.append({"code": "target_length_over", "severity": "warning", "message": "승인 목표 길이보다 결과가 깁니다."})
        warnings.extend(self._sample_warnings(source, actual))
        result = {
            "status": "failed" if fatal else ("warning" if warnings else "passed"),
            "output": output_kind, "checked_at": utc_now(),
            "expected_duration": round(float(expected_duration), 3), "actual_duration": round(actual, 3),
            "video_codec": (probe["video"] or {}).get("codec_name"),
            "audio_codec": (probe["audio"] or {}).get("codec_name"),
            "fatal": fatal, "warnings": warnings,
        }
        if fatal:
            raise EditQualityError("렌더 결과 검증 실패: " + ", ".join(fatal))
        return result
