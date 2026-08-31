"""Durable background jobs for plain-transcript editing guidance."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Callable

from database import get_db, get_history, save_history, update_history_report
from plain_transcript_edit import (
    PlainTranscriptEditService,
    analyze_duplicates,
    estimate_sentence_seconds,
    revision_sentence_context,
    split_sentences,
    transcript_hash,
    validate_result,
    render_vrew_prompt,
)
from strategy_brain.contracts import EvidenceEnvelope
from strategy_brain.retrieval import StrategyRetrieval


ACTIVE_STATUSES = frozenset({"queued", "processing"})
TERMINAL_STATUSES = frozenset({"done", "failed"})
HISTORY_TYPE = "transcript_edit_guide"
PROJECT_MODE = "transcript_edit_guide"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class PlainTranscriptEditJobStore:
    def __init__(self, connect: Callable[[], sqlite3.Connection] | None = None) -> None:
        self._connect = connect or get_db
        self.init_schema()

    def init_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS plain_transcript_edit_jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    history_id INTEGER,
                    status TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    transcript_hash TEXT NOT NULL DEFAULT '',
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    result_json TEXT,
                    progress_step TEXT NOT NULL,
                    progress_percent INTEGER NOT NULL DEFAULT 0,
                    progress_message TEXT NOT NULL DEFAULT '',
                    sentence_count INTEGER NOT NULL DEFAULT 0,
                    analyzed_sentence_count INTEGER NOT NULL DEFAULT 0,
                    evidence_count INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    retry_state TEXT NOT NULL DEFAULT 'none',
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_plain_transcript_edit_jobs_status "
                "ON plain_transcript_edit_jobs(status,created_at)"
            )
            connection.commit()
        finally:
            connection.close()

    def create(self, *, kind: str, request: dict[str, Any], history_id: int | None = None) -> dict[str, Any]:
        job_id, now = uuid.uuid4().hex, utc_now()
        script_hash = transcript_hash(str(request.get("script") or "")) if kind == "initial" else ""
        connection = self._connect()
        try:
            connection.execute("""
                INSERT INTO plain_transcript_edit_jobs (
                    job_id,kind,history_id,status,request_json,transcript_hash,
                    progress_step,progress_percent,progress_message,created_at,updated_at
                ) VALUES (?,?,?,'queued',?,?, 'queued',2,'분석 작업을 준비하고 있습니다.',?,?)
            """, (job_id, kind, history_id, json.dumps(request, ensure_ascii=False), script_hash, now, now))
            connection.commit()
        finally:
            connection.close()
        return self.get(job_id) or {}

    def cached_checkpoint(self, script_hash: str) -> dict[str, Any]:
        if not script_hash:
            return {}
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT checkpoint_json FROM plain_transcript_edit_jobs "
                "WHERE transcript_hash=? ORDER BY created_at DESC LIMIT 10",
                (script_hash,),
            ).fetchall()
        finally:
            connection.close()
        for row in rows:
            try:
                value = json.loads(row["checkpoint_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if value.get("sentences"):
                return {
                    key: value[key] for key in ("sentences", "duplicates", "evidence")
                    if key in value
                }
        return {}

    def get(self, job_id: str, *, include_checkpoint: bool = False) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM plain_transcript_edit_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return None
        output = dict(row)
        for source, target, default in (
            ("request_json", "request", {}), ("result_json", "result", None),
            ("checkpoint_json", "checkpoint", {}),
        ):
            try:
                output[target] = json.loads(output.pop(source) or json.dumps(default))
            except json.JSONDecodeError:
                output[target] = default
        if not include_checkpoint:
            output.pop("request", None)
            output.pop("checkpoint", None)
        return output

    def update(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "history_id", "status", "transcript_hash", "checkpoint_json", "result_json",
            "progress_step", "progress_percent", "progress_message", "sentence_count",
            "analyzed_sentence_count", "evidence_count", "attempt", "retry_state", "error",
            "started_at", "finished_at", "heartbeat_at",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        if not values:
            return
        values["updated_at"] = utc_now()
        columns = ",".join(f"{key}=?" for key in values)
        connection = self._connect()
        try:
            connection.execute(
                f"UPDATE plain_transcript_edit_jobs SET {columns} WHERE job_id=?",
                (*values.values(), job_id),
            )
            connection.commit()
        finally:
            connection.close()

    def claim_next(self) -> dict[str, Any] | None:
        connection = self._connect()
        row = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id FROM plain_transcript_edit_jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                connection.rollback()
                return None
            now = utc_now()
            connection.execute("""
                UPDATE plain_transcript_edit_jobs
                SET status='processing',attempt=attempt+1,started_at=COALESCE(started_at,?),
                    heartbeat_at=?,updated_at=? WHERE job_id=? AND status='queued'
            """, (now, now, now, row["job_id"]))
            connection.commit()
        finally:
            connection.close()
        return self.get(str(row["job_id"]), include_checkpoint=True)

    def recover_stale(self, *, stale_seconds: int = 600) -> int:
        cutoff = time.time() - max(60, stale_seconds)
        connection = self._connect()
        recovered = 0
        try:
            rows = connection.execute(
                "SELECT job_id,heartbeat_at,attempt,max_attempts FROM plain_transcript_edit_jobs WHERE status='processing'"
            ).fetchall()
            for row in rows:
                try:
                    heartbeat = datetime.fromisoformat(str(row["heartbeat_at"] or "").replace("Z", "+00:00")).timestamp()
                except ValueError:
                    heartbeat = 0
                if heartbeat > cutoff:
                    continue
                can_retry = int(row["attempt"]) < int(row["max_attempts"])
                self_status = "queued" if can_retry else "failed"
                connection.execute(
                    "UPDATE plain_transcript_edit_jobs SET status=?,retry_state=?,progress_message=?,updated_at=? WHERE job_id=?",
                    (
                        self_status, "resumed_from_checkpoint" if can_retry else "retry_exhausted",
                        "서버 재시작 후 완료된 단계부터 이어서 처리합니다." if can_retry else "자동 재시도 횟수를 초과했습니다.",
                        utc_now(), row["job_id"],
                    ),
                )
                recovered += 1
            connection.commit()
        finally:
            connection.close()
        return recovered


def _envelope(value: EvidenceEnvelope) -> dict[str, Any]:
    return {
        "source": value.source, "data": value.data,
        "sample_size": int(value.sample_size or 0),
        "collected_at": value.collected_at, "period": value.period,
        "freshness": value.freshness, "unavailable_reason": value.unavailable_reason,
    }


def collect_evidence(topic: str, retrieval: StrategyRetrieval | None = None) -> dict[str, Any]:
    service = retrieval or StrategyRetrieval()
    query = topic.strip()
    calls = {
        "brand_strategy": lambda: service.get_channel_strategy_snapshot({}),
        "channel_performance": lambda: service.get_recent_channel_performance({"limit": 20}),
        "similar_videos": lambda: service.compare_similar_videos({"query": query, "limit": 8}),
        "retention": lambda: service.get_retention_patterns({"query": query, "limit": 8}),
        "ctr": lambda: service.get_ctr_performance({"query": query, "limit": 8}),
        "business_pt": lambda: service.search_business_pt_knowledge({"query": f"{query} 고객 문제 증거 구매 A/S", "limit": 8}),
        "low_data": lambda: service.search_knowledge({"query": f"{query} Low Data 로우데이터 표본 부족 판단", "limit": 6}),
        "past_plans": lambda: service.search_previous_plans({"query": query, "limit": 6}),
        "worksheets": lambda: service.search_previous_worksheets({"query": query, "limit": 6}),
        "feedback": lambda: service.search_feedback_history({"query": query, "limit": 8}),
        "edit_memory": lambda: service.search_long_term_memory({"query": f"{query} 편집", "limit": 8}),
    }
    output = {}
    for name, call in calls.items():
        try:
            output[name] = _envelope(call())
        except Exception as exc:
            output[name] = {
                "source": name, "data": None, "sample_size": 0,
                "unavailable_reason": type(exc).__name__,
            }
    return output


def numeric_evidence_available(evidence: dict[str, Any]) -> bool:
    return ctr_evidence_available(evidence) or retention_evidence_available(evidence)


def _has_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, dict):
        return any(_has_numeric(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_numeric(item) for item in value)
    return False


def _metric_evidence_available(evidence: dict[str, Any], name: str) -> bool:
    envelope = evidence.get(name) or {}
    return int(envelope.get("sample_size") or 0) > 0 and _has_numeric(envelope.get("data"))


def ctr_evidence_available(evidence: dict[str, Any]) -> bool:
    return _metric_evidence_available(evidence, "ctr")


def retention_evidence_available(evidence: dict[str, Any]) -> bool:
    return _metric_evidence_available(evidence, "retention")


def channel_evidence_available(evidence: dict[str, Any]) -> bool:
    return any(
        int((evidence.get(name) or {}).get("sample_size") or 0) > 0
        for name in ("channel_performance", "similar_videos")
    )


def normalize_estimates(result: dict[str, Any], sentences: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {row["id"]: row for row in sentences}
    for collection in ("overall_flow", "edit_table"):
        for row in result.get(collection) or []:
            start = by_id.get(str(row.get("sentence_start_id") or ""))
            end = by_id.get(str(row.get("sentence_end_id") or ""))
            if not start or not end or start["order"] > end["order"]:
                continue
            rows = sentences[start["order"] - 1:end["order"]]
            row["estimated_seconds"] = round(sum(estimate_sentence_seconds(item["text"]) for item in rows), 1)
    return result


class PlainTranscriptEditJobManager:
    def __init__(
        self, *, store: PlainTranscriptEditJobStore | None = None,
        service_factory: Callable[[], PlainTranscriptEditService] | None = None,
        evidence_collector: Callable[[str], dict[str, Any]] = collect_evidence,
        history_writer: Callable[[str, str, dict[str, Any]], int] = save_history,
        history_reader: Callable[[int], dict[str, Any] | None] = get_history,
        history_updater: Callable[..., bool] = update_history_report,
    ) -> None:
        self.store = store or PlainTranscriptEditJobStore()
        self.service_factory = service_factory or PlainTranscriptEditService
        self.evidence_collector = evidence_collector
        self.history_writer = history_writer
        self.history_reader = history_reader
        self.history_updater = history_updater
        self._worker_task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    def enqueue_initial(self, request: dict[str, Any]) -> dict[str, Any]:
        job = self.store.create(kind="initial", request=request)
        cached = self.store.cached_checkpoint(str(job.get("transcript_hash") or ""))
        if cached:
            self.store.update(
                str(job["job_id"]),
                checkpoint_json=json.dumps(cached, ensure_ascii=False, default=str),
                retry_state="cache_hit",
                sentence_count=len(cached.get("sentences") or []),
                progress_message="같은 전체 대본의 문장·근거 cache를 재사용합니다.",
            )
            job = self.store.get(str(job["job_id"])) or job
        self._wake.set()
        return job

    def enqueue_revision(self, history_id: int, message: str) -> dict[str, Any]:
        job = self.store.create(
            kind="revision", history_id=history_id,
            request={"history_id": history_id, "message": message},
        )
        self._wake.set()
        return job

    def start(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self.store.recover_stale()
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
            try:
                if await self.process_once():
                    continue
            except asyncio.CancelledError:
                raise                      # 서버가 내려가는 것이니 그대로 멈춘다
            except Exception:
                # 여기서 새어 나가면 일꾼이 죽고, 그 뒤 모든 작업이 대기만 한다.
                # 한 건이 잘못돼도 다음 건은 계속 처리한다.
                logging.exception("plain transcript edit worker iteration failed")
                await asyncio.sleep(1)
            # A hard process exit cannot execute the graceful cancellation path.
            # Revisit stale processing rows so they eventually resume from the
            # last persisted checkpoint without requiring another restart.
            await asyncio.to_thread(self.store.recover_stale)
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

    def _progress(
        self, job_id: str, step: str, percent: int, message: str,
        *, checkpoint: dict[str, Any] | None = None, **extra: Any,
    ) -> None:
        fields = {
            "progress_step": step, "progress_percent": percent,
            "progress_message": message, "heartbeat_at": utc_now(), **extra,
        }
        if checkpoint is not None:
            fields["checkpoint_json"] = json.dumps(checkpoint, ensure_ascii=False, default=str)
        self.store.update(job_id, **fields)

    async def _await_with_heartbeat(self, job_id: str, awaitable: Any, *,
                                    step: str = "", start_percent: int = 0,
                                    end_percent: int = 0, label: str = "") -> Any:
        """Keep the persisted heartbeat fresh while a model call is in flight.

        모델이 몇 분씩 생각하는 동안 화면이 한 자리에 멈춰 있으면 멎은 것처럼 보인다.
        그래서 지나간 시간을 함께 적어 보낸다.
        """

        async def pulse() -> None:
            waited = 0
            while True:
                await asyncio.sleep(15)
                waited += 15
                fields: dict[str, Any] = {"heartbeat_at": utc_now()}
                if step:
                    # 끝 지점까지 천천히 차오르게 한다 (2분이면 거의 다 찬다)
                    span = max(0, end_percent - start_percent)
                    grown = min(span, int(span * min(1.0, waited / 120)))
                    fields.update(
                        progress_step=step,
                        progress_percent=start_percent + grown,
                        progress_message=f"{label} ({waited // 60}분 {waited % 60}초 경과)",
                    )
                await asyncio.to_thread(self.store.update, job_id, **fields)

        pulse_task = asyncio.create_task(pulse())
        try:
            return await awaitable
        finally:
            pulse_task.cancel()
            with suppress(asyncio.CancelledError):
                await pulse_task

    async def _process(self, job: dict[str, Any]) -> None:
        job_id = str(job["job_id"])
        checkpoint = dict(job.get("checkpoint") or {})
        started = time.perf_counter()
        try:
            if job["kind"] == "revision":
                await self._process_revision(job, checkpoint, started)
            else:
                await self._process_initial(job, checkpoint, started)
        except asyncio.CancelledError:
            self.store.update(
                job_id, status="queued", retry_state="resumed_from_checkpoint",
                progress_message="서버 재시작 후 완료된 단계부터 이어서 처리합니다.", heartbeat_at=utc_now(),
            )
            raise
        except Exception as exc:
            retry = int(job.get("attempt") or 0) < int(job.get("max_attempts") or 2)
            if retry:
                checkpoint.pop("analysis_result", None)
            self.store.update(
                job_id, status="queued" if retry else "failed",
                retry_state="automatic_retry" if retry else "retry_exhausted",
                checkpoint_json=json.dumps(checkpoint, ensure_ascii=False, default=str),
                progress_step="retrying" if retry else "failed",
                progress_percent=70 if retry else 100,
                progress_message=(
                    "일시 오류가 발생해 완료된 단계부터 자동 재시도합니다."
                    if retry else "분석을 완료하지 못했습니다. 입력과 API 상태를 확인해주세요."
                ),
                error=f"{type(exc).__name__}: {str(exc)[:500]}",
                finished_at=None if retry else utc_now(), heartbeat_at=utc_now(),
            )
            if retry:
                self._wake.set()

    async def _process_initial(self, job: dict[str, Any], checkpoint: dict[str, Any], started: float) -> None:
        job_id, request = str(job["job_id"]), dict(job.get("request") or {})
        if "sentences" not in checkpoint:
            self._progress(job_id, "sentence_splitting", 10, "원문을 문장 단위로 나누고 ID를 생성합니다.")
            sentences = split_sentences(str(request.get("script") or ""))
            if not sentences:
                raise ValueError("분석할 문장이 없습니다.")
            checkpoint["sentences"] = sentences
            self._progress(
                job_id, "structure_analysis", 22, "대본 구조를 분석합니다.", checkpoint=checkpoint,
                sentence_count=len(sentences), analyzed_sentence_count=len(sentences),
            )
        sentences = checkpoint["sentences"]
        if "duplicates" not in checkpoint:
            checkpoint["duplicates"] = analyze_duplicates(sentences)
            self._progress(job_id, "duplicate_analysis", 34, "반복되는 설명 후보를 찾았습니다.", checkpoint=checkpoint)
        if "evidence" not in checkpoint:
            self._progress(job_id, "evidence_retrieval", 46, "채널·Business PT·과거 편집 근거를 조회합니다.")
            evidence = await asyncio.to_thread(self.evidence_collector, str(request.get("topic") or request.get("title") or ""))
            checkpoint["evidence"] = evidence
            evidence_count = sum(int((row or {}).get("sample_size") or 0) for row in evidence.values())
            self._progress(
                job_id, "flow_design", 58, "전체 영상 흐름을 설계합니다.", checkpoint=checkpoint,
                evidence_count=evidence_count,
            )
        evidence = checkpoint["evidence"]
        if "analysis_result" not in checkpoint:
            result = await self._await_with_heartbeat(
                job_id,
                step="analysis", start_percent=58, end_percent=88,
                label="AI가 전체 대본을 읽고 편집안을 설계하는 중입니다",
                awaitable=self.service_factory().analyze(
                    request=request, sentences=sentences,
                    duplicates=checkpoint["duplicates"], evidence=evidence,
                ),
            )
            checkpoint["analysis_result"] = result
            self._progress(job_id, "detail_table", 82, "문장 ID 기준 상세 편집표를 생성했습니다.", checkpoint=checkpoint)
        result = normalize_estimates(checkpoint["analysis_result"], sentences)
        self._progress(job_id, "validation", 88, "문장 원문과 편집 지시를 코드로 검증합니다.", checkpoint=checkpoint)

        def check(candidate: dict[str, Any]) -> None:
            validate_result(
                candidate, sentences,
                numeric_data_available=numeric_evidence_available(evidence),
                channel_data_available=channel_evidence_available(evidence),
                ctr_data_available=ctr_evidence_available(evidence),
                retention_data_available=retention_evidence_available(evidence),
            )

        check(result)

        # 사람이 한 번 읽어보는 자리. 이대로 자르면 영상이 어떻게 보일지 보고,
        # 어색하면 순서를 고쳐 온다. 고친 것이 검증을 통과할 때만 받아들인다.
        if "review" not in checkpoint:
            try:
                review = await self._await_with_heartbeat(
                    job_id,
                    step="review", start_percent=90, end_percent=95,
                    label="설계한 순서를 처음부터 다시 읽어보는 중입니다",
                    awaitable=self.service_factory().review(
                        request=request, sentences=sentences, result=result),
                )
            except Exception as exc:
                logging.warning("plain transcript edit review skipped: %s", exc)
                review = {"verdict": "skipped", "error": str(exc)[:200]}
            checkpoint["review"] = review
        review = checkpoint.get("review") or {}

        revised = review.get("revised_order") or []
        if str(review.get("verdict")) == "fix" and revised:
            candidate = json.loads(json.dumps(result, ensure_ascii=False, default=str))
            by_span = {}
            for row in result.get("edit_table") or []:
                by_span[(row.get("sentence_start_id"), row.get("sentence_end_id"))] = row
            table = []
            for row in sorted(revised, key=lambda item: int(item.get("final_order") or 0)):
                key = (row.get("sentence_start_id"), row.get("sentence_end_id"))
                base = dict(by_span.get(key) or {})
                base.update({
                    "final_order": int(row.get("final_order") or 0),
                    "sentence_start_id": row.get("sentence_start_id"),
                    "sentence_end_id": row.get("sentence_end_id"),
                    "purpose": row.get("purpose") or base.get("purpose") or "",
                })
                base.setdefault("action", "이동")
                base.setdefault("start_sentence", None)
                base.setdefault("end_sentence", None)
                base["start_sentence"] = None       # 원문 대조는 코드가 다시 채운다
                base["end_sentence"] = None
                table.append(base)
            candidate["edit_table"] = table
            try:
                check(candidate)
            except Exception as exc:
                logging.warning("review revision rejected, keeping original: %s", exc)
                result["_review"] = {"verdict": "fix_rejected", "reason": str(exc)[:200],
                                     "problems": review.get("problems") or []}
            else:
                result = candidate
                result["_review"] = {"verdict": "fixed",
                                     "problems": review.get("problems") or [],
                                     "opening_check": review.get("opening_check") or ""}
        else:
            result["_review"] = {"verdict": str(review.get("verdict") or "ok"),
                                 "opening_check": review.get("opening_check") or "",
                                 "flow_check": review.get("flow_check") or ""}
        self._progress(job_id, "validation", 96, "검토를 마쳤습니다.", checkpoint=checkpoint)
        now = utc_now()
        project_metadata = {
            "schema_version": 1, "mode": PROJECT_MODE,
            "title": request.get("title"), "topic": request.get("topic"),
            "target_duration_seconds": request.get("target_duration_seconds"),
            "purpose": request.get("purpose"), "additional_request": request.get("additional_request"),
            "script": request.get("script"), "transcript_hash": transcript_hash(str(request.get("script") or "")),
            "current_version": 1,
        }
        version = {
            "version": 1, "result": result, "user_request": "",
            "revision_summary": result.get("revision_summary") or "최초 전체 대본 분석",
            "used_evidence": result.get("used_evidence") or [],
            "expected_duration_seconds": result.get("recommended_duration_seconds"),
            "created_at": now,
        }
        version["employee_guide_text"] = render_vrew_prompt(
            {"_project": project_metadata, "sentences": sentences}, version,
        )
        project_metadata.update({
            "last_revision_summary": version["revision_summary"],
            "analysis_elapsed_seconds": round(time.perf_counter() - started, 3),
        })
        project = {
            "_project": project_metadata,
            "sentences": sentences, "duplicate_candidates": checkpoint["duplicates"],
            "evidence_cache": evidence, "versions": [version], "current_result": result,
            "conversation": [],
        }
        history_id = await asyncio.to_thread(
            self.history_writer, HISTORY_TYPE, str(request.get("title") or request.get("topic") or "자막 편집 가이드")[:300], project,
        )
        response = {"history_id": history_id, "version": 1, "project": project}
        self.store.update(
            job_id, history_id=history_id, status="done", progress_step="done",
            progress_percent=100, progress_message="대본 기반 편집 지시서가 완성되었습니다.",
            result_json=json.dumps(response, ensure_ascii=False, default=str), retry_state="none",
            finished_at=utc_now(), heartbeat_at=utc_now(),
        )

    async def _process_revision(self, job: dict[str, Any], checkpoint: dict[str, Any], started: float) -> None:
        job_id, request = str(job["job_id"]), dict(job.get("request") or {})
        history_id = int(request.get("history_id") or 0)
        row = await asyncio.to_thread(self.history_reader, history_id)
        if not row or row.get("type") != HISTORY_TYPE:
            raise ValueError("수정할 편집 프로젝트를 찾지 못했습니다.")
        project = dict(row.get("report") or {})
        if (project.get("_project") or {}).get("mode") != PROJECT_MODE:
            raise ValueError("대본 기반 영상 흐름 프로젝트가 아닙니다.")
        sentences = project.get("sentences") or []
        versions = list(project.get("versions") or [])
        current = (versions[-1] if versions else {}).get("result") or project.get("current_result") or {}
        message = str(request.get("message") or "").strip()
        context = revision_sentence_context(sentences, message, current)
        self._progress(
            job_id, "revision_analysis", 45,
            f"기존 결과와 관련 문장 {len(context)}개만 사용해 수정합니다.",
            sentence_count=len(sentences), analyzed_sentence_count=len(context),
            evidence_count=sum(int((value or {}).get("sample_size") or 0) for value in (project.get("evidence_cache") or {}).values()),
        )
        result = await self._await_with_heartbeat(
            job_id,
            step="revision", start_percent=40, end_percent=80,
            label="요청하신 부분을 다시 설계하는 중입니다",
            awaitable=self.service_factory().revise(
                current=current, user_request=message, sentence_context=context,
                evidence_summary={
                    key: {"source": value.get("source"), "sample_size": value.get("sample_size"), "unavailable_reason": value.get("unavailable_reason")}
                    for key, value in (project.get("evidence_cache") or {}).items()
                },
            ),
        )
        result = normalize_estimates(result, sentences)
        self._progress(job_id, "validation", 85, "수정된 문장 배치만 다시 검증합니다.")
        validate_result(
            result, sentences,
            numeric_data_available=numeric_evidence_available(project.get("evidence_cache") or {}),
            channel_data_available=channel_evidence_available(project.get("evidence_cache") or {}),
            ctr_data_available=ctr_evidence_available(project.get("evidence_cache") or {}),
            retention_data_available=retention_evidence_available(project.get("evidence_cache") or {}),
        )
        version_number = int(versions[-1].get("version") or 0) + 1 if versions else 1
        version = {
            "version": version_number, "result": result, "user_request": message,
            "revision_summary": result.get("revision_summary") or "사용자 수정 요청 반영",
            "used_evidence": result.get("used_evidence") or [],
            "expected_duration_seconds": result.get("recommended_duration_seconds"),
            "created_at": utc_now(),
        }
        version["employee_guide_text"] = render_vrew_prompt(project, version)
        versions.append(version)
        project["versions"] = versions
        project["current_result"] = result
        project.setdefault("conversation", []).extend([
            {"role": "user", "content": message, "created_at": utc_now(), "version": version_number},
            {"role": "assistant", "content": version["revision_summary"], "created_at": utc_now(), "version": version_number},
        ])
        project["_project"].update({
            "current_version": version_number,
            "last_revision_summary": version["revision_summary"],
            "last_revision_elapsed_seconds": round(time.perf_counter() - started, 3),
        })
        await asyncio.to_thread(
            self.history_updater, history_id, project,
            keyword=str(project["_project"].get("title") or project["_project"].get("topic") or row.get("keyword") or "대본 편집")[:300],
        )
        response = {"history_id": history_id, "version": version_number, "project": project}
        self.store.update(
            job_id, status="done", progress_step="done", progress_percent=100,
            progress_message=f"수정안 v{version_number}을 저장했습니다.",
            result_json=json.dumps(response, ensure_ascii=False, default=str), retry_state="none",
            finished_at=utc_now(), heartbeat_at=utc_now(),
        )
