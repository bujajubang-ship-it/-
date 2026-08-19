"""Durable, single-worker video feedback jobs with no video rendering."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from database import get_db, save_history
from video_feedback_media import (
    compress_transcript_segments,
    extract_audio,
    probe_video,
    select_representative_frames,
)
from video_feedback_report import (
    generate_markdown_feedback,
    partial_feedback,
)
from video_feedback_service import VideoFeedbackService
from youtube_strategy_context import YouTubeStrategyContextService


TERMINAL_STATUSES = frozenset({"done", "partial", "failed"})
ACTIVE_STATUSES = frozenset({"uploading", "queued", "processing"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_job_root() -> Path:
    configured = os.getenv("VIDEO_FEEDBACK_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    if Path("/data").is_dir():
        return Path("/data/video-feedback-jobs")
    return Path(__file__).resolve().parent / "artifacts" / "video-feedback-jobs"


class VideoFeedbackJobStore:
    def __init__(self, connect: Callable[[], sqlite3.Connection] | None = None) -> None:
        self._connect = connect or get_db
        self.init_schema()

    def init_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS video_feedback_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    topic TEXT NOT NULL DEFAULT '',
                    filename TEXT NOT NULL DEFAULT '',
                    source_path TEXT NOT NULL,
                    progress_step TEXT NOT NULL DEFAULT 'uploading',
                    progress_percent INTEGER NOT NULL DEFAULT 0,
                    progress_message TEXT NOT NULL DEFAULT '',
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    gpt_call_count INTEGER NOT NULL DEFAULT 0,
                    transcription_call_count INTEGER NOT NULL DEFAULT 0,
                    selected_frames_count INTEGER NOT NULL DEFAULT 0,
                    youtube_cache_used INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    failed_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_video_feedback_jobs_status "
                "ON video_feedback_jobs(status,created_at)"
            )
            connection.commit()
        finally:
            connection.close()

    def create(self, *, job_id: str, topic: str, filename: str, source_path: str) -> dict[str, Any]:
        now = utc_now()
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO video_feedback_jobs (
                    job_id,status,topic,filename,source_path,progress_step,
                    progress_percent,progress_message,created_at,updated_at
                ) VALUES (?,?,?,?,?,'uploading',1,'영상을 저장하고 있습니다.',?,?)
                """,
                (job_id, "uploading", topic[:300], filename[:300], source_path, now, now),
            )
            connection.commit()
        finally:
            connection.close()
        return self.get(job_id) or {}

    def update(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status", "progress_step", "progress_percent", "progress_message",
            "attempt", "gpt_call_count", "transcription_call_count",
            "selected_frames_count", "youtube_cache_used", "result_json",
            "failed_reason", "started_at", "finished_at", "heartbeat_at",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = utc_now()
        columns = ",".join(f"{key}=?" for key in values)
        connection = self._connect()
        try:
            connection.execute(
                f"UPDATE video_feedback_jobs SET {columns} WHERE job_id=?",
                (*values.values(), job_id),
            )
            connection.commit()
        finally:
            connection.close()

    def get(self, job_id: str, *, include_private: bool = False) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM video_feedback_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        result = dict(row)
        try:
            result["result"] = json.loads(result.pop("result_json") or "null")
        except json.JSONDecodeError:
            result["result"] = None
            result.pop("result_json", None)
        if not include_private:
            result.pop("source_path", None)
        result["youtube_cache_used"] = bool(result.get("youtube_cache_used"))
        return result

    def mark_uploaded(self, job_id: str) -> None:
        self.update(
            job_id,
            status="queued",
            progress_step="queued",
            progress_percent=10,
            progress_message="업로드 완료. 분석 대기 중입니다.",
        )

    def claim_next(self) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id FROM video_feedback_jobs WHERE status='queued' "
                "ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            now = utc_now()
            connection.execute(
                """
                UPDATE video_feedback_jobs
                SET status='processing',progress_step='validating',progress_percent=15,
                    progress_message='영상 정보를 확인하고 있습니다.',attempt=attempt+1,
                    started_at=COALESCE(started_at,?),heartbeat_at=?,updated_at=?
                WHERE job_id=? AND status='queued'
                """,
                (now, now, now, row["job_id"]),
            )
            connection.commit()
        finally:
            connection.close()
        return self.get(str(row["job_id"]), include_private=True)

    def recover_stale(self, *, stale_seconds: int = 900) -> int:
        cutoff = time.time() - max(60, stale_seconds)
        connection = self._connect()
        recovered = 0
        try:
            rows = connection.execute(
                "SELECT job_id,heartbeat_at,attempt,max_attempts FROM video_feedback_jobs "
                "WHERE status='processing'"
            ).fetchall()
            for row in rows:
                try:
                    heartbeat = datetime.fromisoformat(
                        str(row["heartbeat_at"] or "").replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    heartbeat = 0
                if heartbeat > cutoff:
                    continue
                status = "queued" if int(row["attempt"]) < int(row["max_attempts"]) else "failed"
                message = (
                    "서버 재시작 후 실패 단계부터 다시 대기합니다."
                    if status == "queued"
                    else "분석 작업을 복구하지 못했습니다. 다시 업로드해 주세요."
                )
                connection.execute(
                    "UPDATE video_feedback_jobs SET status=?,progress_step=?,"
                    "progress_message=?,updated_at=? WHERE job_id=?",
                    (status, status, message, utc_now(), row["job_id"]),
                )
                recovered += 1
            connection.commit()
        finally:
            connection.close()
        return recovered


class VideoFeedbackJobManager:
    def __init__(
        self,
        *,
        store: VideoFeedbackJobStore | None = None,
        root: str | Path | None = None,
        feedback_service_factory: Callable[[], VideoFeedbackService] | None = None,
        report_generator: Callable[..., Awaitable[Any]] = generate_markdown_feedback,
        history_writer: Callable[[str, str, dict[str, Any]], int] = save_history,
    ) -> None:
        self.store = store or VideoFeedbackJobStore()
        self.root = Path(root) if root is not None else default_job_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.feedback_service_factory = feedback_service_factory or VideoFeedbackService
        self.report_generator = report_generator
        self.history_writer = history_writer
        self._worker_task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    def create_upload(self, *, filename: str, topic: str) -> tuple[dict[str, Any], Path]:
        job_id = uuid.uuid4().hex
        suffix = Path(filename or "video.mp4").suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
            suffix = ".mp4"
        job_dir = self.root / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        source_path = job_dir / f"source{suffix}"
        job = self.store.create(
            job_id=job_id,
            topic=(topic or "").strip(),
            filename=filename or "영상 피드백",
            source_path=str(source_path),
        )
        return job, source_path

    def finish_upload(self, job_id: str) -> None:
        self.store.mark_uploaded(job_id)
        self._wake.set()

    def fail_upload(self, job_id: str, message: str = "영상 업로드를 완료하지 못했습니다.") -> None:
        self.store.update(
            job_id,
            status="failed",
            progress_step="failed",
            progress_percent=100,
            progress_message=message,
            failed_reason="upload_failed",
            finished_at=utc_now(),
        )

    def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self.store.recover_stale(
            stale_seconds=int(os.getenv("VIDEO_FEEDBACK_STALE_SECONDS", "900"))
        )
        self._worker_task = asyncio.create_task(self._worker_loop())

    async def stop(self) -> None:
        if self._worker_task is None:
            return
        self._worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._worker_task
        self._worker_task = None

    async def _worker_loop(self) -> None:
        while True:
            processed = await self.process_once()
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=3)
            except TimeoutError:
                pass

    async def process_once(self) -> bool:
        job = await asyncio.to_thread(self.store.claim_next)
        if not job:
            return False
        await self._process(job)
        return True

    def _progress(self, job_id: str, step: str, percent: int, message: str, **extra: Any) -> None:
        self.store.update(
            job_id,
            progress_step=step,
            progress_percent=percent,
            progress_message=message,
            heartbeat_at=utc_now(),
            **extra,
        )

    async def _process(self, job: dict[str, Any]) -> None:
        job_id = str(job["job_id"])
        job_dir = self.root / job_id
        source_path = Path(str(job["source_path"]))
        audio_path = job_dir / "audio.mp3"
        started = time.perf_counter()
        purge_source = False
        try:
            media = await asyncio.to_thread(probe_video, source_path)
            self._progress(job_id, "extracting", 25, "피드백용 음성을 준비하고 있습니다.")
            await asyncio.to_thread(extract_audio, source_path, audio_path)

            self._progress(
                job_id, "transcribing", 42,
                "음성을 타임코드별로 요약하고 있습니다.",
                transcription_call_count=1,
            )
            transcription = await self.feedback_service_factory().transcribe(audio_path)
            transcript_summary = compress_transcript_segments(transcription.segments)

            self._progress(
                job_id, "selecting_frames", 58,
                "대표 화면을 코드로 선별하고 있습니다. 이미지 원본은 AI에 보내지 않습니다.",
            )
            frame_summary = await asyncio.to_thread(
                select_representative_frames,
                source_path,
                job_dir,
                duration_seconds=float(media["duration_seconds"]),
                transcript_segments=transcription.segments,
                max_selected=int(os.getenv("VIDEO_FEEDBACK_MAX_FRAMES", "30")),
                absolute_max=40,
            )
            selected_count = min(40, int(frame_summary.get("selected_frames_count") or 0))
            self._progress(
                job_id, "retrieving", 70,
                "오늘의 채널 snapshot과 부자주방 전략을 불러오고 있습니다.",
                selected_frames_count=selected_count,
            )
            strategy_context = await YouTubeStrategyContextService().collect(use_cache=True)
            cache_used = bool(strategy_context.retrieval_summary.get("youtube_cache_hit"))

            compact_source = {
                "media": media,
                "compact_transcript": transcript_summary,
                "selected_frame_summary": frame_summary,
            }
            self._progress(
                job_id, "analyzing", 82,
                "GPT가 한 번의 호출로 글 피드백을 작성하고 있습니다.",
                gpt_call_count=1,
                youtube_cache_used=int(cache_used),
            )
            try:
                report = await self.report_generator(
                    compact_source,
                    topic=str(job.get("topic") or job.get("filename") or "영상 피드백"),
                    strategy_context=strategy_context,
                )
                final_status = "done"
                failed_reason = None
            except Exception:
                report = partial_feedback(
                    topic=str(job.get("topic") or job.get("filename") or "영상 피드백"),
                    transcript_summary=transcript_summary,
                    frame_summary=frame_summary,
                    retrieval_summary=strategy_context.retrieval_summary,
                    failed_reason="AI 피드백 생성이 지연되었습니다. 저장된 분석으로 다시 시도할 수 있습니다.",
                )
                final_status = "partial"
                failed_reason = "openai_feedback_failed"

            result = {
                "feedback": report.feedback,
                "markdown": report.markdown,
                "provider": "openai",
                "retrieval_summary": report.retrieval_summary,
                "transcription_provider": transcription.provider,
                "analysis_metadata": {
                    "selected_frames_count": selected_count,
                    "candidate_frames_count": int(frame_summary.get("candidate_frames_count") or 0),
                    "images_sent_to_gpt": 0,
                    "gpt_call_count": 1,
                    "transcription_call_count": 1,
                    "youtube_cache_used": cache_used,
                    "rendering_executed": False,
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                },
            }
            history_id = await asyncio.to_thread(
                self.history_writer,
                "video_feedback",
                str(job.get("topic") or job.get("filename") or "영상 피드백")[:80],
                result,
            )
            result["history_id"] = history_id
            self.store.update(
                job_id,
                status=final_status,
                progress_step=final_status,
                progress_percent=100,
                progress_message=(
                    "영상 피드백이 완료되었습니다."
                    if final_status == "done"
                    else "기초 분석을 저장했습니다. AI 종합 피드백은 다시 시도할 수 있습니다."
                ),
                result_json=json.dumps(result, ensure_ascii=False, default=str),
                failed_reason=failed_reason,
                finished_at=utc_now(),
                heartbeat_at=utc_now(),
            )
            purge_source = True
        except asyncio.CancelledError:
            self.store.update(
                job_id,
                status="queued",
                progress_step="queued",
                progress_message="서버 재시작 후 분석을 재개합니다.",
                heartbeat_at=utc_now(),
            )
            raise
        except Exception as exc:
            # Technical/provider details stay out of the user-visible row.
            print(
                f"[video-feedback-job] job={job_id[:8]} type={type(exc).__name__}",
                flush=True,
            )
            self.store.update(
                job_id,
                status="failed",
                progress_step="failed",
                progress_percent=100,
                progress_message="영상 피드백 준비에 실패했습니다. 파일을 확인한 뒤 다시 시도해 주세요.",
                failed_reason="media_analysis_failed",
                finished_at=utc_now(),
                heartbeat_at=utc_now(),
            )
            purge_source = True
        finally:
            with suppress(OSError):
                audio_path.unlink()
            # Feedback keeps compact decisions in DB/history; uploaded media and
            # frame candidates are temporary and are removed after processing.
            if purge_source:
                with suppress(OSError):
                    source_path.unlink()
                with suppress(OSError):
                    shutil.rmtree(job_dir)
