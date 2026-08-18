"""Durable, compact memories for the Bujajubang strategy partner.

Chat transcripts remain the audit trail.  This table stores only decisions and
reusable learnings so a new conversation does not need to replay every turn.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable


MEMORY_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS strategy_memories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        memory_type TEXT NOT NULL,
        content TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        related_json TEXT NOT NULL DEFAULT '{}',
        confidence REAL NOT NULL DEFAULT 0.7,
        source_session_id INTEGER,
        fingerprint TEXT NOT NULL UNIQUE,
        superseded_by INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(superseded_by) REFERENCES strategy_memories(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_strategy_memory_active ON strategy_memories(superseded_by, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_memory_type ON strategy_memories(memory_type, updated_at DESC)",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _fingerprint(memory_type: str, content: str, related: dict[str, Any]) -> str:
    normalized = re.sub(r"\s+", " ", content.strip().lower())
    raw = f"{memory_type}|{normalized}|{_dump(related)}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class StrategyMemoryRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection] | None = None) -> None:
        if connect is None:
            from database import get_db

            connect = get_db
        self._connect = connect

    def init_schema(self) -> None:
        with closing(self._connect()) as connection:
            for statement in MEMORY_SCHEMA:
                connection.execute(statement)
            connection.commit()

    @staticmethod
    def _normalize(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["evidence"] = _load(item.pop("evidence_json", "[]"), [])
        item["related"] = _load(item.pop("related_json", "{}"), {})
        item["superseded"] = item.get("superseded_by") is not None
        item.pop("fingerprint", None)
        return item

    def record(
        self,
        *,
        memory_type: str,
        content: str,
        evidence: list[dict[str, Any]] | None = None,
        related: dict[str, Any] | None = None,
        confidence: float = 0.7,
        source_session_id: int | None = None,
    ) -> int:
        content = re.sub(r"\s+", " ", content).strip()[:1200]
        if not content:
            raise ValueError("memory content must not be empty")
        related = related or {}
        fingerprint = _fingerprint(memory_type, content, related)
        now = _now()
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            existing = connection.execute(
                "SELECT id FROM strategy_memories WHERE fingerprint=?", (fingerprint,)
            ).fetchone()
            if existing:
                return int(existing["id"])
            cursor = connection.execute(
                """
                INSERT INTO strategy_memories (
                    memory_type, content, evidence_json, related_json, confidence,
                    source_session_id, fingerprint, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    memory_type[:40], content, _dump(evidence or []), _dump(related),
                    max(0.0, min(float(confidence), 1.0)), source_session_id,
                    fingerprint, now, now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def supersede(self, old_id: int, new_id: int) -> bool:
        if old_id == new_id:
            return False
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE strategy_memories SET superseded_by=?, updated_at=?
                WHERE id=? AND superseded_by IS NULL
                """,
                (new_id, _now(), old_id),
            )
            connection.commit()
            return cursor.rowcount == 1

    def search(
        self, query: str = "", *, limit: int = 10, include_superseded: bool = False
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 50))
        terms = [
            term.lower()
            for term in re.findall(r"[0-9A-Za-z가-힣]+", query)
            if len(term) > 1
        ]
        where = "" if include_superseded else "WHERE superseded_by IS NULL"
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    f"SELECT * FROM strategy_memories {where} ORDER BY updated_at DESC, id DESC LIMIT 300"
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc).lower():
                    return []
                raise
        ranked = []
        for row in rows:
            item = self._normalize(row)
            haystack = f"{item['memory_type']} {item['content']} {_dump(item['related'])}".lower()
            score = 1 if not terms else sum(1 for term in terms if term in haystack)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        return [item for _, item in ranked[:limit]]


DECISION_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("decision", ("이걸로", "확정", "결정", "진행하자", "하기로", "선택")),
    ("hypothesis", ("테스트", "가설", "실험", "검증해")),
    ("avoid", ("피하고", "하지 말", "반복하지", "싫어", "제외")),
    ("preference", ("선호", "좋아", "방향", "유지", "원해")),
    ("failure", ("실패", "안 됐", "낮았", "이탈", "문제였")),
    ("success", ("성공", "잘 됐", "높았", "효과 있었")),
)


def remember_interaction(
    message: str,
    answer: str,
    *,
    trace: list[dict[str, Any]] | None = None,
    source_session_id: int | None = None,
    repository: StrategyMemoryRepository | None = None,
) -> int | None:
    """Persist explicit user decisions, not ordinary questions or whole chats."""

    normalized = re.sub(r"\s+", " ", message).strip()
    memory_type = next(
        (kind for kind, markers in DECISION_PATTERNS if any(marker in normalized for marker in markers)),
        None,
    )
    if memory_type == "decision" and any(
        request in normalized
        for request in ("결정해줘", "확정해줘", "골라줘", "추천해줘", "선택해줘")
    ):
        memory_type = None
    if memory_type is None:
        return None
    evidence = [
        {
            "source": item.get("source") or item.get("tool"),
            "sample_size": item.get("sample_size"),
            "freshness": item.get("freshness"),
        }
        for item in (trace or [])[:12]
        if item.get("source") or item.get("tool")
    ]
    conclusion = re.sub(r"\s+", " ", answer).strip()[:360]
    content = f"사용자 결정/학습: {normalized[:700]}"
    if conclusion:
        content += f" | 당시 전략가 결론: {conclusion}"
    repo = repository or StrategyMemoryRepository()
    memory_id = repo.record(
        memory_type=memory_type,
        content=content,
        evidence=evidence,
        confidence=0.85 if memory_type in {"decision", "avoid"} else 0.72,
        source_session_id=source_session_id,
    )
    if any(marker in normalized for marker in ("이전 결정", "취소", "대신", "바꾸", "이제부터", "아니고")):
        previous = [
            item
            for item in repo.search("", limit=30)
            if item["id"] != memory_id and item["memory_type"] == memory_type
        ]
        if previous:
            repo.supersede(previous[0]["id"], memory_id)
    return memory_id


def init_strategy_memory_schema() -> None:
    StrategyMemoryRepository().init_schema()
