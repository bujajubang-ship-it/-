"""SQLite-backed durable jobs using additive history rows.

The production DB schema is intentionally unchanged: job rows use the existing
history audit table, so deployment is backward-compatible and rollback-safe.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import socket
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from database import get_db
from edit_project_store import utc_now


JOB_TYPE = "edit_job"
JOB_TYPES = {
    "ingest", "proxy", "transcription", "analysis", "rendering",
    "preview_rendering", "final_rendering",
    "source_analysis", "story_planning", "rough_cut_rendering",
    "short_render", "storage_upload", "cleanup", "performance_sync",
}
TERMINAL = {"completed", "failed", "cancelled", "stale_rendering"}


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class EditJobQueue:
    def __init__(self, connect=None) -> None:
        self._connect = connect or get_db

    @staticmethod
    def _load(row: Any) -> dict[str, Any]:
        item = dict(row)
        try:
            report = json.loads(item.get("report") or "{}")
        except (TypeError, json.JSONDecodeError):
            report = {}
        report["job_id"] = int(item["id"])
        return report

    def enqueue(
        self, project_id: int, job_type: str, *, payload: dict[str, Any] | None = None,
        idempotency_key: str, max_attempts: int = 3, priority: int = 100,
        defer_seconds: float = 0,
    ) -> dict[str, Any]:
        if job_type not in JOB_TYPES:
            raise ValueError("unsupported edit job type")
        key = str(idempotency_key or "").strip()[:300]
        if not key:
            raise ValueError("idempotency_key is required")
        now = utc_now()
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM history WHERE type=? ORDER BY id DESC LIMIT 3000",
                (JOB_TYPE,),
            ).fetchall()
            for row in rows:
                existing = self._load(row)
                if existing.get("idempotency_key") == key and existing.get("status") != "cancelled":
                    connection.commit()
                    return existing
            job = {
                "schema_version": 1, "project_id": int(project_id), "type": job_type,
                "status": "queued", "attempt": 0, "max_attempts": max(1, min(int(max_attempts), 10)),
                "priority": int(priority), "queued_at": now, "started_at": None,
                "heartbeat_at": None, "finished_at": None, "error": None,
                "next_retry_at": (
                    (datetime.now(timezone.utc) + timedelta(seconds=max(0, float(defer_seconds)))).isoformat().replace("+00:00", "Z")
                    if defer_seconds > 0 else None
                ), "worker_id": None, "idempotency_key": key,
                "payload": payload or {}, "timings": {},
            }
            cursor = connection.execute(
                "INSERT INTO history(type,keyword,report) VALUES(?,?,?)",
                (JOB_TYPE, f"{job_type}:{project_id}", json.dumps(job, ensure_ascii=False, sort_keys=True)),
            )
            job["job_id"] = int(cursor.lastrowid)
            # Worker payloads are self-contained audit artifacts. Injecting the
            # durable id after INSERT avoids a second caller-side mutation.
            job["payload"].setdefault("job_id", job["job_id"])
            self._save(connection, job)
            connection.commit()
            return job

    def _save(self, connection, job: dict[str, Any]) -> None:
        job_id = int(job["job_id"])
        payload = {key: value for key, value in job.items() if key != "job_id"}
        connection.execute(
            "UPDATE history SET report=? WHERE id=? AND type=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), job_id, JOB_TYPE),
        )

    def get(self, job_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM history WHERE id=? AND type=?", (int(job_id), JOB_TYPE)
            ).fetchone()
        return self._load(row) if row else None

    def list(self, *, project_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM history WHERE type=? ORDER BY id DESC LIMIT ?",
                (JOB_TYPE, max(1, min(int(limit), 3000))),
            ).fetchall()
        jobs = [self._load(row) for row in rows]
        return [job for job in jobs if project_id is None or int(job.get("project_id") or 0) == int(project_id)]

    def recover_stale(self, *, stale_seconds: int = 180) -> list[int]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(30, stale_seconds))
        recovered = []
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM history WHERE type=? ORDER BY id", (JOB_TYPE,)
            ).fetchall()
            for row in rows:
                job = self._load(row)
                if job.get("status") not in {"queued", "running"}:
                    continue
                attempt = int(job.get("attempt") or 0)
                max_attempts = int(job.get("max_attempts") or 1)
                if job.get("status") == "queued" and attempt < max_attempts:
                    continue
                heartbeat = _parse(job.get("heartbeat_at") or job.get("started_at"))
                # Legacy recovery could requeue an interrupted job forever,
                # including project 127/job 129. An impossible overrun is
                # terminal immediately; a normal final attempt is allowed to
                # finish while its heartbeat remains healthy.
                if heartbeat and heartbeat >= cutoff and attempt <= max_attempts:
                    continue
                exhausted = attempt >= max_attempts
                job["status"] = "failed" if exhausted else "queued"
                job["worker_id"] = None
                job["heartbeat_at"] = None
                job["next_retry_at"] = None if exhausted else utc_now()
                job["finished_at"] = utc_now() if exhausted else None
                job["retry_needed"] = exhausted
                job["error"] = (
                    "WorkerInterrupted: retry limit reached; owner retry required"
                    if exhausted else "WorkerInterrupted: stale heartbeat recovered"
                )
                self._save(connection, job)
                recovered.append(int(job["job_id"]))
            connection.commit()
        return recovered

    def claim(self, worker_id: str, *, allowed_types: set[str] | None = None) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc)
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM history WHERE type=? ORDER BY id", (JOB_TYPE,)
            ).fetchall()
            candidates = []
            for row in rows:
                job = self._load(row)
                if job.get("status") != "queued":
                    continue
                if allowed_types and job.get("type") not in allowed_types:
                    continue
                retry = _parse(job.get("next_retry_at"))
                if retry and retry > now:
                    continue
                candidates.append(job)
            if not candidates:
                connection.commit()
                return None
            candidates.sort(key=lambda job: (int(job.get("priority") or 100), int(job["job_id"])))
            job = candidates[0]
            job["status"] = "running"
            job["attempt"] = int(job.get("attempt") or 0) + 1
            job["started_at"] = utc_now()
            job["heartbeat_at"] = job["started_at"]
            job["worker_id"] = worker_id[:120]
            job["error"] = None
            self._save(connection, job)
            connection.commit()
            return job

    def heartbeat(self, job_id: int) -> None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM history WHERE id=? AND type=?", (int(job_id), JOB_TYPE)
            ).fetchone()
            job = self._load(row) if row else None
            if not job or job.get("status") != "running":
                connection.commit()
                return
            job["heartbeat_at"] = utc_now()
            self._save(connection, job)
            connection.commit()

    def finish(self, job_id: int, *, timings: dict[str, Any] | None = None) -> None:
        job = self.get(job_id)
        if not job:
            return
        if job.get("status") != "running":
            return
        job.update({
            "status": "completed", "finished_at": utc_now(), "heartbeat_at": utc_now(),
            "error": None, "retry_needed": False,
        })
        job["timings"] = {**(job.get("timings") or {}), **(timings or {})}
        with closing(self._connect()) as connection:
            self._save(connection, job)
            connection.commit()

    def fail(self, job_id: int, exc: Exception, *, retryable: bool = True) -> dict[str, Any] | None:
        job = self.get(job_id)
        if not job:
            return None
        if job.get("status") != "running":
            return job
        attempt = int(job.get("attempt") or 0)
        can_retry = retryable and attempt < int(job.get("max_attempts") or 1)
        job["status"] = "queued" if can_retry else "failed"
        job["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        job["retry_needed"] = bool(getattr(exc, "retry_needed", False)) and not can_retry
        job["worker_id"] = None
        job["finished_at"] = None if can_retry else utc_now()
        job["next_retry_at"] = (
            (datetime.now(timezone.utc) + timedelta(seconds=min(900, 15 * (2 ** max(0, attempt - 1))))).isoformat().replace("+00:00", "Z")
            if can_retry else None
        )
        with closing(self._connect()) as connection:
            self._save(connection, job)
            connection.commit()
        return job

    def retry(self, job_id: int) -> dict[str, Any]:
        job = self.get(job_id)
        if not job:
            raise KeyError("작업을 찾지 못했습니다.")
        if job.get("status") not in {"failed", "cancelled"}:
            raise RuntimeError("실패하거나 취소된 작업만 다시 시도할 수 있습니다.")
        job.update({
            "status": "queued", "attempt": 0, "next_retry_at": utc_now(),
            "finished_at": None, "worker_id": None, "heartbeat_at": None,
            "error": None, "retry_needed": False,
        })
        with closing(self._connect()) as connection:
            self._save(connection, job)
            connection.commit()
        return job

    def mark_stale_rendering(self, job_id: int, *, stale_seconds: int = 600) -> dict[str, Any]:
        """Stop an abandoned preview job without making it claimable again."""
        job = self.get(job_id)
        if not job:
            raise KeyError("작업을 찾지 못했습니다.")
        if job.get("type") != "preview_rendering":
            raise RuntimeError("preview 렌더 작업만 중단할 수 있습니다.")
        if job.get("status") == "running":
            heartbeat = _parse(job.get("heartbeat_at") or job.get("started_at"))
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(600, int(stale_seconds)))
            if heartbeat and heartbeat >= cutoff:
                raise RuntimeError("preview worker heartbeat가 아직 정상입니다.")
            job.update({
                "status": "stale_rendering", "worker_id": None,
                "finished_at": utc_now(), "next_retry_at": None,
                "retry_needed": True,
                "error": "StalePreviewRendering: heartbeat expired; proxy restart required",
            })
        elif job.get("status") == "failed":
            job["retry_needed"] = True
        elif job.get("status") != "stale_rendering":
            raise RuntimeError("멈춘 preview 렌더 작업이 아닙니다.")
        with closing(self._connect()) as connection:
            self._save(connection, job)
            connection.commit()
        return job

    def mark_replaced(self, job_id: int) -> dict[str, Any]:
        job = self.get(job_id)
        if not job:
            raise KeyError("작업을 찾지 못했습니다.")
        if job.get("status") not in {"queued", "running", "failed", "stale_rendering"}:
            raise RuntimeError("중단하고 교체할 수 없는 작업입니다.")
        job.update({
            "status": "cancelled", "worker_id": None, "finished_at": utc_now(),
            "next_retry_at": None, "retry_needed": True, "replaced": True,
            "error": "OwnerRestarted: replaced by a new proxy workflow job",
        })
        with closing(self._connect()) as connection:
            self._save(connection, job)
            connection.commit()
        return job

    def snapshot(self) -> dict[str, Any]:
        jobs = self.list(limit=3000)
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.get("status") or "unknown"] = counts.get(job.get("status") or "unknown", 0) + 1
        active = [job for job in reversed(jobs) if job.get("status") in {"queued", "running"}]
        for position, job in enumerate([job for job in active if job.get("status") == "queued"], start=1):
            job["queue_position"] = position
        return {"counts": counts, "active": active[:100], "collected_at": utc_now()}


class EditJobWorker:
    def __init__(
        self, queue: EditJobQueue, handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]],
        *, allowed_types: set[str] | None = None, poll_seconds: float = 2.0,
        heartbeat_seconds: float = 15.0,
    ) -> None:
        self.queue = queue
        self.handlers = handlers
        self.allowed_types = allowed_types
        self.poll_seconds = max(0.1, poll_seconds)
        self.heartbeat_seconds = max(0.01, heartbeat_seconds)
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:media"
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self.queue.recover_stale(stale_seconds=int(os.getenv("EDIT_JOB_STALE_SECONDS", "180")))
            self._task = asyncio.create_task(self._loop(), name="edit-durable-worker")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        stale_seconds = int(os.getenv("EDIT_JOB_STALE_SECONDS", "180"))
        recovery_interval = max(15.0, min(60.0, stale_seconds / 2))
        last_recovery = 0.0
        while True:
            # A replacement process can start before the previous worker's
            # heartbeat has crossed the stale threshold.  Startup-only
            # recovery would then leave the durable row marked ``running``
            # forever.  Recheck while this worker is idle; an actively handled
            # job never reaches this branch and its heartbeat remains the
            # source of truth.
            now = asyncio.get_running_loop().time()
            if now - last_recovery >= recovery_interval:
                await asyncio.to_thread(
                    self.queue.recover_stale, stale_seconds=stale_seconds
                )
                last_recovery = now
            job = await asyncio.to_thread(self.queue.claim, self.worker_id, allowed_types=self.allowed_types)
            if not job:
                await asyncio.sleep(self.poll_seconds)
                continue
            handler = self.handlers.get(str(job.get("type") or ""))
            if handler is None:
                await asyncio.to_thread(self.queue.fail, int(job["job_id"]), RuntimeError("job handler unavailable"), retryable=False)
                continue
            heartbeat = asyncio.create_task(self._heartbeat(int(job["job_id"])))
            started = asyncio.get_running_loop().time()
            try:
                result = await handler(job) or {}
                await asyncio.to_thread(
                    self.queue.finish, int(job["job_id"]),
                    timings={"worker_seconds": round(asyncio.get_running_loop().time() - started, 3), **result},
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retryable = getattr(exc, "retryable", True)
                await asyncio.to_thread(self.queue.fail, int(job["job_id"]), exc, retryable=retryable)
            finally:
                heartbeat.cancel()
                try:
                    await heartbeat
                except asyncio.CancelledError:
                    pass

    async def _heartbeat(self, job_id: int) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_seconds)
            try:
                await asyncio.to_thread(self.queue.heartbeat, job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A short SQLite lock or disk hiccup must not permanently kill
                # the liveness task.  The next interval repairs the heartbeat,
                # preventing a healthy long render from being recovered twice
                # after a deploy.
                continue
