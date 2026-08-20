"""Bounded media ingestion, metadata, transcription, and timeline hints."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from edit_project_store import EditProjectStore
from edit_storage import EditStoragePolicy, EditStorageService


ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


class MediaValidationError(RuntimeError):
    pass


class StorageCapacityError(MediaValidationError):
    pass


class TranscriptionError(RuntimeError):
    pass


class MediaIngestService:
    def __init__(self, store: EditProjectStore | None = None) -> None:
        self.store = store or EditProjectStore()
        self.max_upload_bytes = int(os.getenv("EDIT_MAX_UPLOAD_MB", "2048")) * 1024 * 1024
        self.policy = EditStoragePolicy.from_env()
        self.reserve_bytes = self.policy.reserve_bytes

    def validate_capacity(
        self, expected_size: int, *, target_format: str = "mid_form"
    ) -> dict[str, Any]:
        size = max(0, int(expected_size or 0))
        if size > self.max_upload_bytes:
            raise MediaValidationError(
                f"영상이 업로드 한도({self.max_upload_bytes // 1024 // 1024}MB)를 넘었습니다."
            )
        estimate = EditStorageService(self.store, policy=self.policy).estimate_upload(
            size, target_format=target_format
        )
        if size and not estimate["enough"]:
            required_mb = max(1, (estimate["required_bytes"] + 1024 * 1024 - 1) // (1024 * 1024))
            free_mb = estimate["free_bytes"] // (1024 * 1024)
            raise StorageCapacityError(
                f"이 영상의 안전한 편집에는 약 {required_mb}MB가 필요하지만 현재 {free_mb}MB만 남았습니다. "
                "오래된 편집 결과를 정리하거나 외부 저장소를 연결해주세요."
            )
        return estimate

    async def persist_upload(
        self, upload: Any, project_uuid: str, *, target_format: str = "mid_form"
    ) -> tuple[Path, int, str]:
        filename = str(getattr(upload, "filename", "") or "video.mp4")
        expected_size = int(getattr(upload, "size", 0) or 0)
        self.validate_capacity(expected_size, target_format=target_format)
        return await self.persist_stream(
            self._upload_chunks(upload), filename, project_uuid,
            expected_size=expected_size, target_format=target_format,
        )

    @staticmethod
    async def _upload_chunks(upload: Any):
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                return
            yield chunk

    async def persist_stream(
        self, chunks: Any, filename: str, project_uuid: str, *,
        expected_size: int = 0, target_format: str = "mid_form",
    ) -> tuple[Path, int, str]:
        filename = str(filename or "video.mp4")
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_VIDEO_SUFFIXES:
            raise MediaValidationError("mp4, mov, m4v, avi, mkv, webm 동영상만 지원합니다.")
        if expected_size:
            self.validate_capacity(expected_size, target_format=target_format)
        directory = self.store.project_dir(project_uuid, create=True)
        destination = directory / f"source{suffix}"
        total = 0
        try:
            with open(destination, "wb") as target:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self.max_upload_bytes:
                        raise MediaValidationError(
                            f"영상이 업로드 한도({self.max_upload_bytes // 1024 // 1024}MB)를 넘었습니다."
                        )
                    free = shutil.disk_usage(directory).free
                    if free - len(chunk) < self.reserve_bytes:
                        raise StorageCapacityError(
                            "업로드 중 다른 작업이 저장공간을 사용했습니다. 현재 작업을 보존한 채 업로드를 중단했습니다."
                        )
                    target.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        if total == 0:
            destination.unlink(missing_ok=True)
            raise MediaValidationError("빈 파일은 분석할 수 없습니다.")
        return destination, total, filename[:240]

    @staticmethod
    def probe(path: str | Path) -> dict[str, Any]:
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
    def extract_audio(
        path: str | Path, output: Path, duration: float, *, start: float = 0.0
    ) -> Path:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg가 설치되어 있지 않습니다.")
        timeout = max(120, min(1800, int(duration * 1.5) + 60))
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}", "-i", str(path),
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
    def create_analysis_proxy(path: str | Path, output: Path, duration: float) -> Path:
        """Create a lightweight 1080p review/analysis master from a 4K source."""
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg가 설치되어 있지 않습니다.")
        output.parent.mkdir(parents=True, exist_ok=True)
        part = output.with_name(f".{output.stem}.part{output.suffix}")
        part.unlink(missing_ok=True)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(part), "-y",
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True,
                timeout=max(600, min(10800, int(max(0, duration) * 3) + 300)),
            )
        except subprocess.TimeoutExpired as exc:
            part.unlink(missing_ok=True)
            raise RuntimeError("1080p 분석 proxy 생성 시간이 초과됐습니다.") from exc
        if result.returncode != 0 or not part.is_file() or part.stat().st_size <= 0:
            part.unlink(missing_ok=True)
            raise RuntimeError("1080p 분석 proxy를 만들지 못했습니다.")
        os.replace(part, output)
        return output

    @staticmethod
    def detect_silences(
        path: str | Path, duration: float, *, start_offset: float = 0.0
    ) -> list[dict[str, float]]:
        timeout = max(90, min(900, int(duration) + 60))
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-ss", f"{max(0.0, start_offset):.3f}",
                    "-t", f"{duration:.3f}", "-i", str(path),
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
            output.append({
                "start": round(start + start_offset, 3),
                "end": round(end + start_offset, 3),
                "duration": round(length, 3),
            })
        return output[:500]

    @classmethod
    def detect_silences_chunked(cls, path: str | Path, duration: float) -> list[dict[str, float]]:
        chunk = max(300, min(1200, int(os.getenv("EDIT_SIGNAL_CHUNK_SECONDS", "900"))))
        output = []
        for start in range(0, max(1, int(duration)), chunk):
            length = min(float(chunk), duration - start)
            if length <= 0:
                break
            output.extend(cls.detect_silences(path, length, start_offset=float(start)))
        return output[:1500]

    @staticmethod
    def detect_scenes(path: str | Path, duration: float) -> list[float]:
        chunk = max(300, min(1200, int(os.getenv("EDIT_SCENE_CHUNK_SECONDS", "900"))))
        output = []
        for start in range(0, max(1, int(duration)), chunk):
            scan = min(float(chunk), duration - start)
            if scan <= 0:
                break
            try:
                result = subprocess.run(
                    [
                        "ffmpeg", "-hide_banner", "-ss", f"{float(start):.3f}", "-t", f"{scan:.3f}",
                        "-i", str(path), "-vf",
                        "fps=2,scale=320:-2,select='gt(scene,0.30)',showinfo",
                        "-an", "-f", "null", "-",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=max(90, min(900, int(scan) + 60)),
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            output.extend(float(value) + start for value in re.findall(r"pts_time:([0-9.]+)", result.stderr))
        return [round(value, 3) for value in output[:1500]]

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
        self, path: str | Path, media: dict[str, Any], *, work_dir: Path | None = None,
        existing_chunks: list[dict[str, Any]] | None = None,
        on_progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, float]], list[float]]:
        if not media.get("has_audio"):
            raise TranscriptionError("오디오 트랙이 없어 대화 기반 편집 분석을 진행할 수 없습니다.")
        duration = float(media["duration"])
        directory = work_dir or (path.parent if isinstance(path, Path) else self.store.storage_root)
        directory.mkdir(parents=True, exist_ok=True)
        silence_task = asyncio.to_thread(self.detect_silences_chunked, path, duration)
        scene_task = asyncio.to_thread(self.detect_scenes, path, duration)
        # Ten-minute checkpoints bound both retry cost and the amount of work
        # lost when a media worker is replaced during a long source.
        chunk_seconds = max(300, min(600, int(os.getenv("EDIT_TRANSCRIPT_CHUNK_SECONDS", "600"))))
        chunk_ranges = [
            (index, float(start), min(float(chunk_seconds), duration - start))
            for index, start in enumerate(range(0, max(1, int(duration)), chunk_seconds))
            if min(float(chunk_seconds), duration - start) > 0
        ]
        cached = {
            int(item.get("chunk_index") or 0): item
            for item in (existing_chunks or [])
            if item.get("status") == "completed" and (item.get("transcript") or {}).get("segments")
        }
        transcripts: list[dict[str, Any]] = []

        async def report(**values: Any) -> None:
            if on_progress is not None:
                await on_progress({
                    "total_chunks": len(chunk_ranges),
                    "completed_chunks": len(cached),
                    **values,
                })

        await report(
            stage="transcribing", current_chunk=None,
            pending_operation=None, checkpoint="TRANSCRIPT_PLANNED",
        )
        try:
            for index, start, length in chunk_ranges:
                if index in cached:
                    transcripts.append(dict(cached[index]["transcript"]))
                    continue
                audio_path = directory / f"analysis_audio_{index:03d}.mp3"
                await report(
                    stage="transcribing", current_chunk=index + 1,
                    pending_operation="ffmpeg_audio_extract",
                    checkpoint=None,
                )
                await asyncio.to_thread(self.extract_audio, path, audio_path, length, start=float(start))
                try:
                    await report(
                        stage="transcribing", current_chunk=index + 1,
                        pending_operation="openai_transcription",
                        checkpoint="AUDIO_EXTRACTED",
                    )
                    part = await self.transcribe(audio_path)
                finally:
                    audio_path.unlink(missing_ok=True)
                adjusted = []
                for segment in part.get("segments") or []:
                    adjusted.append({
                        **segment,
                        "start": round(float(segment.get("start") or 0) + start, 3),
                        "end": round(float(segment.get("end") or 0) + start, 3),
                    })
                completed = {**part, "segments": adjusted}
                transcripts.append(completed)
                cached[index] = {
                    "chunk_index": index,
                    "start": round(start, 3),
                    "end": round(start + length, 3),
                    "status": "completed",
                    "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "transcript": completed,
                }
                await report(
                    stage="transcribing", completed_chunks=len(cached), current_chunk=None,
                    pending_operation=None, checkpoint="TRANSCRIPT_CHUNK_COMPLETED",
                    completed_chunk=cached[index],
                )
        finally:
            for temp in directory.glob("analysis_audio_*.mp3"):
                temp.unlink(missing_ok=True)
        await report(
            stage="signal_analysis", completed_chunks=len(cached), current_chunk=None,
            pending_operation="ffmpeg_signal_analysis", checkpoint="TRANSCRIPT_COMPLETE",
        )
        silences, scenes = await asyncio.gather(silence_task, scene_task)
        transcript = {
            "text": " ".join(str(item.get("text") or "") for item in transcripts).strip()[:500_000],
            "segments": [segment for item in transcripts for segment in (item.get("segments") or [])][:15000],
            "provider": "+".join(dict.fromkeys(str(item.get("provider") or "") for item in transcripts)),
            "chunks": len(transcripts),
        }
        return transcript, silences, scenes
