"""Bounded media ingestion, metadata, transcription, and timeline hints."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import httpx

from edit_project_store import EditProjectStore


ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


class MediaValidationError(RuntimeError):
    pass


class TranscriptionError(RuntimeError):
    pass


class MediaIngestService:
    def __init__(self, store: EditProjectStore | None = None) -> None:
        self.store = store or EditProjectStore()
        self.max_upload_bytes = int(os.getenv("EDIT_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
        self.reserve_bytes = int(os.getenv("EDIT_DISK_RESERVE_MB", "512")) * 1024 * 1024

    async def persist_upload(self, upload: Any, project_uuid: str) -> tuple[Path, int, str]:
        filename = str(getattr(upload, "filename", "") or "video.mp4")
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_VIDEO_SUFFIXES:
            raise MediaValidationError("mp4, mov, m4v, avi, mkv, webm 동영상만 지원합니다.")
        directory = self.store.project_dir(project_uuid, create=True)
        destination = directory / f"source{suffix}"
        total = 0
        try:
            with open(destination, "wb") as target:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.max_upload_bytes:
                        raise MediaValidationError(
                            f"영상이 업로드 한도({self.max_upload_bytes // 1024 // 1024}MB)를 넘었습니다."
                        )
                    free = shutil.disk_usage(directory).free
                    if free - len(chunk) < self.reserve_bytes:
                        raise MediaValidationError("편집 저장소 여유 공간이 부족합니다.")
                    target.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if total == 0:
            destination.unlink(missing_ok=True)
            raise MediaValidationError("빈 파일은 분석할 수 없습니다.")
        return destination, total, filename[:240]

    @staticmethod
    def probe(path: Path) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error", "-print_format", "json",
                    "-show_format", "-show_streams", str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MediaValidationError("동영상 메타데이터를 읽지 못했습니다.") from exc
        if result.returncode != 0:
            raise MediaValidationError("손상되었거나 지원하지 않는 동영상입니다.")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MediaValidationError("동영상 메타데이터 응답이 올바르지 않습니다.") from exc
        streams = payload.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not video:
            raise MediaValidationError("영상 트랙이 없는 파일입니다.")
        try:
            duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        if duration < 2:
            raise MediaValidationError("2초보다 짧은 영상은 편집 분석에 충분하지 않습니다.")
        rate = str(video.get("avg_frame_rate") or "0/1")
        try:
            left, right = rate.split("/", 1)
            fps = float(left) / max(float(right), 1.0)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        return {
            "duration": round(duration, 3),
            "width": int(video.get("width") or 0),
            "height": int(video.get("height") or 0),
            "fps": round(fps, 3),
            "video_codec": video.get("codec_name"),
            "audio_codec": audio.get("codec_name") if audio else None,
            "has_audio": bool(audio),
            "format": (payload.get("format") or {}).get("format_name"),
        }

    @staticmethod
    def extract_audio(path: Path, output: Path, duration: float) -> Path:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg가 설치되어 있지 않습니다.")
        timeout = max(120, min(1800, int(duration * 1.5) + 60))
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
                    "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(output), "-y",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("오디오 추출 시간이 초과됐습니다.") from exc
        if result.returncode != 0 or not output.exists():
            raise RuntimeError("오디오를 추출하지 못했습니다.")
        if output.stat().st_size > 24 * 1024 * 1024:
            raise RuntimeError("받아쓰기 한도를 넘었습니다. 영상을 여러 프로젝트로 나눠주세요.")
        return output

    @staticmethod
    def detect_silences(path: Path, duration: float) -> list[dict[str, float]]:
        timeout = max(90, min(900, int(duration) + 60))
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-i", str(path),
                    "-af", "silencedetect=noise=-35dB:d=0.6", "-f", "null", "-",
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", result.stderr)]
        ends = [
            (float(end), float(length))
            for end, length in re.findall(
                r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)",
                result.stderr,
            )
        ]
        output = []
        for index, start in enumerate(starts):
            end, length = ends[index] if index < len(ends) else (duration, max(0.0, duration - start))
            output.append({"start": round(start, 3), "end": round(end, 3), "duration": round(length, 3)})
        return output[:500]

    @staticmethod
    def detect_scenes(path: Path, duration: float) -> list[float]:
        scan_limit = min(duration, float(os.getenv("EDIT_SCENE_SCAN_MAX_SECONDS", "1200")))
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-ss", "0", "-t", f"{scan_limit:.3f}",
                    "-i", str(path), "-vf",
                    "fps=2,scale=320:-2,select='gt(scene,0.30)',showinfo",
                    "-an", "-f", "null", "-",
                ],
                capture_output=True,
                text=True,
                timeout=max(90, min(600, int(scan_limit) + 60)),
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        values = [float(value) for value in re.findall(r"pts_time:([0-9.]+)", result.stderr)]
        return [round(value, 3) for value in values[:500]]

    async def transcribe(self, audio_path: Path) -> dict[str, Any]:
        proxy_base = os.getenv("CNMAKER_BASE", "").rstrip("/")
        proxy_secret = os.getenv("CNMAKER_SECRET", "")
        if proxy_base and proxy_secret:
            async with httpx.AsyncClient(timeout=900) as client:
                with open(audio_path, "rb") as source:
                    response = await client.post(
                        f"{proxy_base}/transcribe",
                        headers={"x-secret": proxy_secret, "Content-Type": "application/octet-stream"},
                        content=source.read(),
                    )
            if response.status_code != 200:
                raise TranscriptionError("받아쓰기 서비스가 영상을 처리하지 못했습니다.")
            payload = response.json()
            return self._normalize_transcript(payload, provider="proxy")

        if not os.getenv("OPENAI_API_KEY", "").strip():
            raise TranscriptionError("받아쓰기 연결이 설정되어 있지 않습니다.")
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(timeout=900, max_retries=1)
            with open(audio_path, "rb") as source:
                response = await client.audio.transcriptions.create(
                    model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1"),
                    file=source,
                    language="ko",
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                )
            payload = response.model_dump() if hasattr(response, "model_dump") else dict(response)
            return self._normalize_transcript(payload, provider="openai")
        except TranscriptionError:
            raise
        except Exception as exc:
            raise TranscriptionError("받아쓰기에 실패했습니다. 원본은 보존되어 다시 시도할 수 있습니다.") from exc

    @staticmethod
    def _normalize_transcript(payload: dict[str, Any], *, provider: str) -> dict[str, Any]:
        text = str(payload.get("text") or "").strip()
        segments = []
        for item in payload.get("segments") or []:
            try:
                start = max(0.0, float(item.get("start") or 0))
                end = max(start, float(item.get("end") or start))
            except (TypeError, ValueError):
                continue
            content = str(item.get("text") or "").strip()
            if content:
                segments.append({"start": round(start, 3), "end": round(end, 3), "text": content})
        if not text and segments:
            text = " ".join(item["text"] for item in segments)
        if not text:
            raise TranscriptionError("음성을 찾지 못했습니다. 무음 영상은 현재 자동 편집 분석 대상이 아닙니다.")
        return {"text": text[:200_000], "segments": segments[:5000], "provider": provider}

    async def inspect_and_transcribe(
        self, path: Path, media: dict[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, float]], list[float]]:
        if not media.get("has_audio"):
            raise TranscriptionError("오디오 트랙이 없어 대화 기반 편집 분석을 진행할 수 없습니다.")
        audio_path = path.parent / "analysis_audio.mp3"
        audio_task = asyncio.to_thread(self.extract_audio, path, audio_path, float(media["duration"]))
        silence_task = asyncio.to_thread(self.detect_silences, path, float(media["duration"]))
        scene_task = asyncio.to_thread(self.detect_scenes, path, float(media["duration"]))
        _, silences, scenes = await asyncio.gather(audio_task, silence_task, scene_task)
        try:
            transcript = await self.transcribe(audio_path)
        finally:
            audio_path.unlink(missing_ok=True)
        return transcript, silences, scenes
