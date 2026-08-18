"""Persistence for shared strategy contexts and upload feedback loops."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
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


def _worksheet_from_strategy(
    strategy_id: int, topic: str, strategy: dict[str, Any]
) -> dict[str, Any]:
    thumbnail = strategy.get("thumbnail") or {}
    if not isinstance(thumbnail, dict):
        thumbnail = {"text": str(thumbnail)}
    structure = strategy.get("structure") or []
    body_lines = []
    for section in structure:
        if isinstance(section, dict):
            body_lines.append(
                "\n".join(
                    value
                    for value in (
                        str(section.get("section") or "").strip(),
                        str(section.get("purpose") or "").strip(),
                        str(section.get("content") or "").strip(),
                    )
                    if value
                )
            )
        elif section:
            body_lines.append(str(section))
    evidence = strategy.get("evidence") or []
    evidence_lines = []
    for item in evidence:
        if isinstance(item, dict):
            claim = str(item.get("claim") or "").strip()
            source = str(item.get("source") or "").strip()
            if claim or source:
                evidence_lines.append(f"{claim} ({source})".strip())
        elif item:
            evidence_lines.append(str(item))
    kpis = strategy.get("kpis") or []
    kpi_lines = []
    for item in kpis:
        if isinstance(item, dict):
            kpi_lines.append(
                " · ".join(
                    str(item.get(key) or "").strip()
                    for key in ("checkpoint", "metric", "target", "decision_rule")
                    if str(item.get(key) or "").strip()
                )
            )
        elif item:
            kpi_lines.append(str(item))
    title_candidates = strategy.get("title_candidates") or []
    recommended = str(strategy.get("recommended_title") or topic).strip()
    return {
        "name": recommended or topic,
        "keyword": str(strategy.get("topic") or topic),
        "viewerTalk": "\n".join(
            value
            for value in (
                f"타깃: {strategy.get('target_audience')}" if strategy.get("target_audience") else "",
                f"왜 지금: {strategy.get('why_now')}" if strategy.get("why_now") else "",
                f"핵심 메시지: {strategy.get('core_message')}" if strategy.get("core_message") else "",
            )
            if value
        ),
        "empathy": "\n".join(evidence_lines),
        "titleCopy": "\n".join(
            [recommended] + [str(item) for item in title_candidates if str(item) != recommended]
        ),
        "thumbCopy": str(thumbnail.get("text") or ""),
        "thumbDesign": "\n".join(
            value
            for value in (
                str(thumbnail.get("composition") or "").strip(),
                str(thumbnail.get("shooting_direction") or "").strip(),
            )
            if value
        ),
        "introScript": str(strategy.get("hook_5_15s") or ""),
        "bodyScript": "\n\n".join(line for line in body_lines if line),
        "memo": "\n".join(
            [f"공통 전략 #{strategy_id}"]
            + (["KPI"] + kpi_lines if kpi_lines else [])
            + [str(item) for item in strategy.get("counterargument_and_risks") or []]
        ),
        "strategyId": strategy_id,
    }


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

    def activate(self, strategy_id: int) -> dict[str, int]:
        """Idempotently materialize one strategy in pipeline and worksheet."""

        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM content_strategies WHERE id=?", (strategy_id,)
            ).fetchone()
            if not row:
                connection.rollback()
                raise KeyError("strategy not found")
            pipeline_id = row["pipeline_id"]
            worksheet_id = row["worksheet_id"]
            if pipeline_id is not None and not connection.execute(
                "SELECT 1 FROM pipeline WHERE id=?", (pipeline_id,)
            ).fetchone():
                pipeline_id = None
            if worksheet_id is not None and not connection.execute(
                "SELECT 1 FROM worksheet_rows WHERE id=?", (worksheet_id,)
            ).fetchone():
                worksheet_id = None
            strategy = _load(row["strategy_json"], {})
            title = str(strategy.get("recommended_title") or row["topic"]).strip()
            if pipeline_id is None:
                cursor = connection.execute(
                    """
                    INSERT INTO pipeline (
                        title, stage, content_type, editor, planned_date, notes
                    ) VALUES (?, 'pick', ?, '', '', ?)
                    """,
                    (
                        title,
                        row["content_type"],
                        f"공통 전략 #{strategy_id} · {strategy.get('why_now') or ''}"[:1000],
                    ),
                )
                pipeline_id = int(cursor.lastrowid)
            if worksheet_id is None:
                worksheet = _worksheet_from_strategy(
                    strategy_id, str(row["topic"]), strategy
                )
                worksheet["videoId"] = pipeline_id
                cursor = connection.execute(
                    "INSERT INTO worksheet_rows (data) VALUES (?)", (_dump(worksheet),)
                )
                worksheet_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE content_strategies
                SET pipeline_id=?, worksheet_id=?, status='active', updated_at=?
                WHERE id=?
                """,
                (pipeline_id, worksheet_id, _now(), strategy_id),
            )
            connection.commit()
        return {"pipeline_id": int(pipeline_id), "worksheet_id": int(worksheet_id)}

    def refresh_performance_checkpoints(self) -> int:
        """Build reliable D1/D3/D7/D14/D30 and long outcome checkpoints.

        Milestones are reconstructed from daily rows so Analytics reporting lag
        cannot make the collector skip a day. Exact aggregate labels remain a
        fallback, and the latest aggregate snapshot is always the long view.
        """

        milestones = (1, 3, 7, 14, 30)
        additive_columns = (
            "views", "likes", "comments", "shares", "subscribers_gained",
            "subscribers_lost", "estimated_minutes_watched",
        )
        saved = 0

        def retention_id(
            connection: sqlite3.Connection, video_id: str, cutoff: str | None
        ) -> int | None:
            if cutoff:
                row = connection.execute(
                    """
                    SELECT id FROM video_retention_snapshots
                    WHERE video_id=? AND data_through IS NOT NULL AND data_through<=?
                    ORDER BY data_through DESC, collected_at DESC, id DESC LIMIT 1
                    """,
                    (video_id, cutoff),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT id FROM video_retention_snapshots WHERE video_id=?
                    ORDER BY collected_at DESC, id DESC LIMIT 1
                    """,
                    (video_id,),
                ).fetchone()
            return int(row[0]) if row else None

        def upsert(
            connection: sqlite3.Connection,
            *,
            strategy_id: int,
            video_id: str,
            label: str,
            metric_snapshot_id: int | None,
            retention_snapshot_id: int | None,
            measured_at: str,
            analysis: dict[str, Any],
        ) -> None:
            nonlocal saved
            existing = connection.execute(
                """
                SELECT analysis_json FROM performance_checkpoints
                WHERE strategy_id=? AND video_id=? AND checkpoint_label=?
                """,
                (strategy_id, video_id, label),
            ).fetchone()
            merged = _load(existing[0], {}) if existing else {}
            merged.update(analysis)
            cursor = connection.execute(
                """
                INSERT INTO performance_checkpoints (
                    strategy_id, video_id, checkpoint_label,
                    metric_snapshot_id, retention_snapshot_id, measured_at,
                    analysis_json
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(strategy_id, video_id, checkpoint_label) DO UPDATE SET
                    metric_snapshot_id=excluded.metric_snapshot_id,
                    retention_snapshot_id=excluded.retention_snapshot_id,
                    measured_at=excluded.measured_at,
                    analysis_json=excluded.analysis_json
                """,
                (
                    strategy_id, video_id, label, metric_snapshot_id,
                    retention_snapshot_id, measured_at, _dump(merged),
                ),
            )
            saved += max(cursor.rowcount, 0)

        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            links = connection.execute(
                """
                SELECT l.strategy_id, l.video_id, v.published_at
                FROM strategy_video_links l
                LEFT JOIN youtube_videos v ON v.video_id=l.video_id
                """
            ).fetchall()
            for link in links:
                strategy_id = int(link["strategy_id"])
                video_id = str(link["video_id"])

                # Exact aggregate milestones are a fallback for databases that
                # predate daily collection.
                exact_rows = connection.execute(
                    """
                    SELECT s.* FROM video_metric_snapshots s
                    WHERE s.video_id=? AND s.snapshot_label IS NOT NULL
                      AND s.id=(SELECT s2.id FROM video_metric_snapshots s2
                                WHERE s2.video_id=s.video_id
                                  AND s2.snapshot_label=s.snapshot_label
                                ORDER BY s2.collected_at DESC, s2.id DESC LIMIT 1)
                    """,
                    (video_id,),
                ).fetchall()
                for snapshot in exact_rows:
                    label = str(snapshot["snapshot_label"])
                    upsert(
                        connection,
                        strategy_id=strategy_id,
                        video_id=video_id,
                        label=label,
                        metric_snapshot_id=int(snapshot["id"]),
                        retention_snapshot_id=retention_id(
                            connection, video_id, snapshot["data_through"]
                        ),
                        measured_at=str(snapshot["collected_at"]),
                        analysis={
                            "source": "video_metric_snapshots",
                            "period": {
                                "start": snapshot["period_start"],
                                "end": snapshot["data_through"],
                            },
                            "metrics": {
                                column: snapshot[column]
                                for column in additive_columns
                            }
                            | {
                                "average_view_duration": snapshot["average_view_duration"],
                                "average_view_percentage": snapshot["average_view_percentage"],
                            },
                        },
                    )

                published_at = str(link["published_at"] or "")[:10]
                try:
                    published = date.fromisoformat(published_at)
                except ValueError:
                    published = None
                if published:
                    for day_number in milestones:
                        target = (published + timedelta(days=day_number)).isoformat()
                        rows = connection.execute(
                            """
                            SELECT * FROM video_daily_metrics
                            WHERE video_id=? AND metric_date BETWEEN ? AND ?
                            ORDER BY metric_date
                            """,
                            (video_id, published.isoformat(), target),
                        ).fetchall()
                        # The collector persists an explicit daily matrix,
                        # including not-reported rows. Missing calendar rows
                        # therefore mean this milestone cannot be reconstructed.
                        if len(rows) < day_number + 1:
                            continue
                        reported_through = max(
                            (str(row["data_through"]) for row in rows if row["data_through"]),
                            default="",
                        )
                        if reported_through < target:
                            continue
                        metrics: dict[str, Any] = {}
                        for column in additive_columns:
                            values = [row[column] for row in rows if row[column] is not None]
                            metrics[column] = sum(values) if values else None
                        for column in ("average_view_duration", "average_view_percentage"):
                            weighted = [
                                (float(row[column]), int(row["views"]))
                                for row in rows
                                if row[column] is not None and row["views"] not in (None, 0)
                            ]
                            metrics[column] = (
                                sum(value * weight for value, weight in weighted)
                                / sum(weight for _, weight in weighted)
                                if weighted else None
                            )
                        measured_at = max(str(row["collected_at"]) for row in rows)
                        upsert(
                            connection,
                            strategy_id=strategy_id,
                            video_id=video_id,
                            label=f"D{day_number}",
                            metric_snapshot_id=None,
                            retention_snapshot_id=retention_id(
                                connection, video_id, target
                            ),
                            measured_at=measured_at,
                            analysis={
                                "source": "video_daily_metrics:cumulative",
                                "period": {"start": published.isoformat(), "end": target},
                                "data_through": reported_through,
                                "sample_size": len(rows),
                                "metrics": metrics,
                            },
                        )

                latest = connection.execute(
                    """
                    SELECT * FROM video_metric_snapshots WHERE video_id=?
                    ORDER BY collected_at DESC, id DESC LIMIT 1
                    """,
                    (video_id,),
                ).fetchone()
                if latest:
                    upsert(
                        connection,
                        strategy_id=strategy_id,
                        video_id=video_id,
                        label="long",
                        metric_snapshot_id=int(latest["id"]),
                        retention_snapshot_id=retention_id(connection, video_id, None),
                        measured_at=str(latest["collected_at"]),
                        analysis={
                            "source": "video_metric_snapshots",
                            "period": {
                                "start": latest["period_start"],
                                "end": latest["data_through"],
                            },
                            "metrics": {
                                column: latest[column] for column in additive_columns
                            }
                            | {
                                "average_view_duration": latest["average_view_duration"],
                                "average_view_percentage": latest["average_view_percentage"],
                            },
                        },
                    )
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
