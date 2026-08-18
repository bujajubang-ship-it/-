"""Persistence for shared strategy contexts and upload feedback loops."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Callable


STRATEGY_TABLES = frozenset(
    {"content_strategies", "strategy_video_links", "performance_checkpoints"}
)


SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS content_strategies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        topic TEXT NOT NULL,
        content_type TEXT NOT NULL DEFAULT '미드폼',
        status TEXT NOT NULL DEFAULT 'draft',
        strategy_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL DEFAULT '[]',
        source_history_id INTEGER,
        pipeline_id INTEGER,
        worksheet_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_video_links (
        strategy_id INTEGER NOT NULL,
        video_id TEXT NOT NULL,
        title_at_upload TEXT,
        thumbnail_text TEXT,
        linked_at TEXT NOT NULL,
        PRIMARY KEY(strategy_id, video_id),
        FOREIGN KEY(strategy_id) REFERENCES content_strategies(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS performance_checkpoints (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id INTEGER NOT NULL,
        video_id TEXT NOT NULL,
        checkpoint_label TEXT NOT NULL,
        metric_snapshot_id INTEGER,
        retention_snapshot_id INTEGER,
        measured_at TEXT NOT NULL,
        analysis_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(strategy_id, video_id, checkpoint_label),
        FOREIGN KEY(strategy_id) REFERENCES content_strategies(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_strategy_updated ON content_strategies(updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_strategy_video ON strategy_video_links(video_id)",
)


def _default_connect() -> sqlite3.Connection:
    from database import get_db

    return get_db()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


class StrategyRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection] | None = None) -> None:
        self._connect = connect or _default_connect

    def init_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for statement in SCHEMA:
                connection.execute(statement)
            connection.commit()

    @staticmethod
    def _normalize(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["strategy"] = _load(item.pop("strategy_json", "{}"), {})
        item["evidence"] = _load(item.pop("evidence_json", "[]"), [])
        return item

    def create(
        self,
        *,
        topic: str,
        content_type: str,
        strategy: dict[str, Any],
        evidence: list[dict[str, Any]] | None = None,
        source_history_id: int | None = None,
        pipeline_id: int | None = None,
        worksheet_id: int | None = None,
        status: str = "draft",
    ) -> int:
        now = _now()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO content_strategies (
                    topic, content_type, status, strategy_json, evidence_json,
                    source_history_id, pipeline_id, worksheet_id, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    topic.strip()[:300],
                    content_type.strip()[:40] or "미드폼",
                    status,
                    _dump(strategy),
                    _dump(evidence or []),
                    source_history_id,
                    pipeline_id,
                    worksheet_id,
                    now,
                    now,
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def get(self, strategy_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM content_strategies WHERE id=?", (strategy_id,)
            ).fetchone()
            if not row:
                return None
            links = connection.execute(
                "SELECT * FROM strategy_video_links WHERE strategy_id=? ORDER BY linked_at",
                (strategy_id,),
            ).fetchall()
            checkpoints = connection.execute(
                """
                SELECT * FROM performance_checkpoints
                WHERE strategy_id=? ORDER BY measured_at
                """,
                (strategy_id,),
            ).fetchall()
        item = self._normalize(row)
        item["videos"] = [dict(link) for link in links]
        item["checkpoints"] = [
            {**dict(point), "analysis": _load(point["analysis_json"], {})}
            for point in checkpoints
        ]
        for point in item["checkpoints"]:
            point.pop("analysis_json", None)
        return item

    def list(self, *, limit: int = 50, query: str = "") -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            if query.strip():
                rows = connection.execute(
                    """
                    SELECT * FROM content_strategies
                    WHERE topic LIKE ? OR strategy_json LIKE ?
                    ORDER BY updated_at DESC LIMIT ?
                    """,
                    (f"%{query.strip()}%", f"%{query.strip()}%", limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM content_strategies ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._normalize(row) for row in rows]

    def update(self, strategy_id: int, fields: dict[str, Any]) -> bool:
        allowed: dict[str, Any] = {}
        for name in ("topic", "content_type", "status", "source_history_id", "pipeline_id", "worksheet_id"):
            if name in fields:
                allowed[name] = fields[name]
        if "strategy" in fields:
            allowed["strategy_json"] = _dump(fields["strategy"])
        if "evidence" in fields:
            allowed["evidence_json"] = _dump(fields["evidence"])
        if not allowed:
            return False
        allowed["updated_at"] = _now()
        sets = ", ".join(f"{key}=?" for key in allowed)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                f"UPDATE content_strategies SET {sets} WHERE id=?",
                (*allowed.values(), strategy_id),
            )
            connection.commit()
            return cursor.rowcount == 1

    def link_video(
        self,
        strategy_id: int,
        video_id: str,
        *,
        title_at_upload: str = "",
        thumbnail_text: str = "",
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO strategy_video_links (
                    strategy_id, video_id, title_at_upload, thumbnail_text, linked_at
                ) VALUES (?,?,?,?,?)
                ON CONFLICT(strategy_id, video_id) DO UPDATE SET
                    title_at_upload=excluded.title_at_upload,
                    thumbnail_text=excluded.thumbnail_text
                """,
                (strategy_id, video_id, title_at_upload, thumbnail_text, _now()),
            )
            connection.execute(
                "UPDATE youtube_videos SET content_id=? WHERE video_id=?",
                (str(strategy_id), video_id),
            )
            connection.commit()

    def refresh_performance_checkpoints(self) -> int:
        """Attach available D1/D3/D7/D14/D30/long snapshots to linked plans."""

        saved = 0
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT l.strategy_id, l.video_id, s.id AS metric_snapshot_id,
                       COALESCE(s.snapshot_label, 'long') AS checkpoint_label,
                       s.collected_at,
                       (SELECT r.id FROM video_retention_snapshots r
                        WHERE r.video_id=l.video_id
                        ORDER BY r.collected_at DESC, r.id DESC LIMIT 1) AS retention_snapshot_id
                FROM strategy_video_links l
                JOIN video_metric_snapshots s ON s.id=(
                    SELECT s2.id FROM video_metric_snapshots s2
                    WHERE s2.video_id=l.video_id
                    ORDER BY s2.collected_at DESC, s2.id DESC LIMIT 1
                )
                """
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    """
                    INSERT INTO performance_checkpoints (
                        strategy_id, video_id, checkpoint_label,
                        metric_snapshot_id, retention_snapshot_id, measured_at
                    ) VALUES (?,?,?,?,?,?)
                    ON CONFLICT(strategy_id, video_id, checkpoint_label) DO UPDATE SET
                        metric_snapshot_id=excluded.metric_snapshot_id,
                        retention_snapshot_id=excluded.retention_snapshot_id,
                        measured_at=excluded.measured_at
                    """,
                    (
                        row["strategy_id"],
                        row["video_id"],
                        row["checkpoint_label"],
                        row["metric_snapshot_id"],
                        row["retention_snapshot_id"],
                        row["collected_at"],
                    ),
                )
                saved += max(cursor.rowcount, 0)
            connection.commit()
        return saved


def init_strategy_schema() -> None:
    StrategyRepository().init_schema()


def capture_legacy_strategy(
    content_type: str,
    topic: str,
    report: dict[str, Any],
    *,
    source_history_id: int,
) -> int:
    """Preserve legacy mid/short/planning output inside the shared context."""

    titles = report.get("titles") or report.get("title_candidates") or []
    normalized_titles = []
    for item in titles:
        if isinstance(item, str):
            normalized_titles.append(item)
        elif isinstance(item, dict):
            normalized_titles.append(
                str(item.get("title") or item.get("text") or "")
            )
    thumbnails = report.get("thumbnails") or report.get("thumbnail_concepts") or report.get("thumbnail")
    hooks = report.get("hooks") or report.get("hook_variations") or report.get("hook")
    context = {
        "topic": topic,
        "target_audience": report.get("target_audience") or "외식업 운영자·창업 준비자",
        "why_now": report.get("why_now") or report.get("market_opportunity") or "legacy 기획에서 생성됨",
        "core_message": report.get("core_message") or report.get("problem_definition") or report.get("strategy") or "",
        "title_candidates": [title for title in normalized_titles if title],
        "recommended_title": normalized_titles[0] if normalized_titles else "",
        "thumbnail": thumbnails or {},
        "hook_5_15s": hooks or "",
        "structure": report.get("structure") or report.get("sections") or report.get("script_structure") or [],
        "shots": report.get("shots") or report.get("shot_list") or [],
        "worksheet": report.get("worksheet") or [],
        "kpis": report.get("kpis") or [
            {"checkpoint": "D1/D3/D7", "metric": "CTR·평균시청률·retention·구독 전환", "target": "채널 최신 baseline과 비교"}
        ],
        "legacy_report": report,
    }
    return StrategyRepository().create(
        topic=topic,
        content_type=content_type,
        strategy=context,
        evidence=[{"source": f"history:{source_history_id}", "type": content_type}],
        source_history_id=source_history_id,
    )
