"""SQLite persistence and high-level reads for trustworthy YouTube performance data."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Sequence

from analytics_reporting import aggregate_reach_by_video
from analytics_service import ANALYTICS_METRICS, FLAT_NAMES, STATUS_AVAILABLE


ANALYTICS_TABLES = frozenset(
    {
        "youtube_videos",
        "youtube_sync_runs",
        "video_metric_snapshots",
        "video_daily_metrics",
        "video_reach_metrics",
        "video_retention_snapshots",
        "video_retention_points",
        "youtube_reporting_jobs",
        "youtube_reporting_files",
        "youtube_collection_state",
    }
)


SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS youtube_videos (
        video_id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        published_at TEXT,
        duration_seconds INTEGER,
        content_id TEXT,
        source TEXT NOT NULL DEFAULT 'youtube_data_api_v3',
        collected_at TEXT NOT NULL,
        last_synced_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS youtube_sync_runs (
        sync_run_id TEXT PRIMARY KEY,
        sync_type TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        period_start TEXT,
        period_end TEXT,
        data_through TEXT,
        row_count INTEGER,
        error_message TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS video_metric_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT NOT NULL,
        snapshot_label TEXT,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        data_through TEXT,
        collected_at TEXT NOT NULL,
        source TEXT NOT NULL,
        sync_run_id TEXT NOT NULL,
        views INTEGER,
        likes INTEGER,
        comments INTEGER,
        shares INTEGER,
        subscribers_gained INTEGER,
        subscribers_lost INTEGER,
        estimated_minutes_watched REAL,
        average_view_duration REAL,
        average_view_percentage REAL,
        view_growth_per_day REAL,
        subscriber_conversion REAL,
        metric_statuses_json TEXT NOT NULL,
        derivation_metadata_json TEXT NOT NULL DEFAULT '{}',
        sample_size INTEGER NOT NULL DEFAULT 0,
        UNIQUE(video_id, period_start, period_end, collected_at),
        FOREIGN KEY(video_id) REFERENCES youtube_videos(video_id),
        FOREIGN KEY(sync_run_id) REFERENCES youtube_sync_runs(sync_run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS video_daily_metrics (
        video_id TEXT NOT NULL,
        metric_date TEXT NOT NULL,
        views INTEGER,
        likes INTEGER,
        comments INTEGER,
        shares INTEGER,
        subscribers_gained INTEGER,
        subscribers_lost INTEGER,
        estimated_minutes_watched REAL,
        average_view_duration REAL,
        average_view_percentage REAL,
        row_status TEXT NOT NULL,
        metric_statuses_json TEXT NOT NULL,
        collected_at TEXT NOT NULL,
        data_through TEXT,
        source TEXT NOT NULL,
        sync_run_id TEXT NOT NULL,
        PRIMARY KEY(video_id, metric_date, source),
        FOREIGN KEY(video_id) REFERENCES youtube_videos(video_id),
        FOREIGN KEY(sync_run_id) REFERENCES youtube_sync_runs(sync_run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS video_reach_metrics (
        video_id TEXT NOT NULL,
        metric_date TEXT NOT NULL,
        channel_id TEXT,
        thumbnail_impressions INTEGER,
        thumbnail_ctr_percent REAL,
        impressions_status TEXT NOT NULL,
        ctr_status TEXT NOT NULL,
        collected_at TEXT NOT NULL,
        report_generated_at TEXT,
        source_as_of TEXT,
        data_through TEXT,
        source TEXT NOT NULL,
        report_id TEXT,
        sync_run_id TEXT NOT NULL,
        PRIMARY KEY(video_id, metric_date, source),
        FOREIGN KEY(video_id) REFERENCES youtube_videos(video_id),
        FOREIGN KEY(sync_run_id) REFERENCES youtube_sync_runs(sync_run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS video_retention_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id TEXT NOT NULL,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        data_through TEXT,
        collected_at TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        duration_seconds INTEGER,
        retention_30s_estimate REAL,
        estimate_metadata_json TEXT NOT NULL,
        point_count INTEGER NOT NULL,
        sync_run_id TEXT NOT NULL,
        error_message TEXT,
        FOREIGN KEY(video_id) REFERENCES youtube_videos(video_id),
        FOREIGN KEY(sync_run_id) REFERENCES youtube_sync_runs(sync_run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS video_retention_points (
        snapshot_id INTEGER NOT NULL,
        elapsed_video_time_ratio REAL NOT NULL,
        audience_watch_ratio REAL NOT NULL,
        relative_retention_performance REAL,
        PRIMARY KEY(snapshot_id, elapsed_video_time_ratio),
        FOREIGN KEY(snapshot_id) REFERENCES video_retention_snapshots(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS youtube_reporting_jobs (
        job_id TEXT PRIMARY KEY,
        report_type_id TEXT NOT NULL,
        job_name TEXT,
        status TEXT NOT NULL,
        create_time TEXT,
        expire_time TEXT,
        last_checked_at TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS youtube_reporting_files (
        report_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        start_time TEXT,
        end_time TEXT,
        create_time TEXT,
        downloaded_at TEXT,
        imported_at TEXT,
        status TEXT NOT NULL,
        sha256 TEXT,
        row_count INTEGER,
        error_message TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(job_id) REFERENCES youtube_reporting_jobs(job_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS youtube_collection_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        status TEXT NOT NULL DEFAULT 'idle',
        lock_owner TEXT,
        lock_until TEXT,
        last_started_at TEXT,
        last_completed_at TEXT,
        last_success_at TEXT,
        data_through TEXT,
        videos_seen INTEGER,
        snapshots_saved INTEGER,
        retention_saved INTEGER,
        reach_imported INTEGER,
        error_message TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_video_snapshots_video_collected ON video_metric_snapshots(video_id, collected_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_daily_metrics_date ON video_daily_metrics(metric_date, video_id)",
    "CREATE INDEX IF NOT EXISTS idx_reach_metrics_date ON video_reach_metrics(metric_date, video_id)",
    "CREATE INDEX IF NOT EXISTS idx_retention_video_collected ON video_retention_snapshots(video_id, collected_at DESC)",
)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_connect() -> sqlite3.Connection:
    from database import get_db

    return get_db()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _status_map(metrics: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    return {name: str(value.get("status")) for name, value in metrics.items()}


def _flat_metric_values(metrics: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    for api_name, column_name in FLAT_NAMES.items():
        metric = metrics.get(api_name) or {}
        values[column_name] = (
            metric.get("value") if metric.get("status") == STATUS_AVAILABLE else None
        )
    return values


class AnalyticsRepository:
    def __init__(self, connect: Callable[[], sqlite3.Connection] | None = None):
        self._connect = connect or _default_connect

    def init_schema(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for statement in SCHEMA_STATEMENTS:
                connection.execute(statement)
            # Additive migrations only. Existing application tables and rows are
            # deliberately untouched so this can run safely against /data/history.db.
            metric_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(video_metric_snapshots)"
                ).fetchall()
            }
            if "snapshot_fingerprint" not in metric_columns:
                connection.execute(
                    "ALTER TABLE video_metric_snapshots ADD COLUMN snapshot_fingerprint TEXT"
                )
            retention_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(video_retention_snapshots)"
                ).fetchall()
            }
            if "snapshot_fingerprint" not in retention_columns:
                connection.execute(
                    "ALTER TABLE video_retention_snapshots ADD COLUMN snapshot_fingerprint TEXT"
                )
            reach_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(video_reach_metrics)"
                ).fetchall()
            }
            if "report_generated_at" not in reach_columns:
                connection.execute(
                    "ALTER TABLE video_reach_metrics ADD COLUMN report_generated_at TEXT"
                )
            if "source_as_of" not in reach_columns:
                connection.execute(
                    "ALTER TABLE video_reach_metrics ADD COLUMN source_as_of TEXT"
                )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_video_metric_snapshot_fingerprint
                ON video_metric_snapshots(video_id, snapshot_fingerprint)
                WHERE snapshot_fingerprint IS NOT NULL
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_video_retention_snapshot_fingerprint
                ON video_retention_snapshots(video_id, snapshot_fingerprint)
                WHERE snapshot_fingerprint IS NOT NULL
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO youtube_collection_state(singleton) VALUES (1)"
            )
            connection.commit()

    def begin_sync_run(
        self,
        sync_type: str,
        source: str,
        *,
        period_start: str | None = None,
        period_end: str | None = None,
        metadata: Dict[str, Any] | None = None,
        started_at: str | None = None,
    ) -> str:
        sync_run_id = str(uuid.uuid4())
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO youtube_sync_runs (
                    sync_run_id, sync_type, source, status, started_at,
                    period_start, period_end, metadata_json
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    sync_run_id,
                    sync_type,
                    source,
                    started_at or utc_now(),
                    period_start,
                    period_end,
                    _json(metadata or {}),
                ),
            )
            connection.commit()
        return sync_run_id

    def finish_sync_run(
        self,
        sync_run_id: str,
        *,
        status: str,
        data_through: str | None = None,
        row_count: int | None = None,
        error_message: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE youtube_sync_runs
                SET status=?, completed_at=?, data_through=?, row_count=?, error_message=?
                WHERE sync_run_id=?
                """,
                (
                    status,
                    completed_at or utc_now(),
                    data_through,
                    row_count,
                    error_message,
                    sync_run_id,
                ),
            )
            connection.commit()

    def upsert_videos(self, videos: Iterable[Dict[str, Any]], *, collected_at: str) -> None:
        rows = list(videos)
        if not rows:
            return
        with closing(self._connect()) as connection:
            for video in rows:
                video_id = video.get("video_id") or video.get("id")
                if not video_id:
                    continue
                connection.execute(
                    """
                    INSERT INTO youtube_videos (
                        video_id, title, published_at, duration_seconds, content_id,
                        source, collected_at, last_synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(video_id) DO UPDATE SET
                        title=excluded.title,
                        published_at=excluded.published_at,
                        duration_seconds=excluded.duration_seconds,
                        content_id=COALESCE(excluded.content_id, youtube_videos.content_id),
                        source=excluded.source,
                        last_synced_at=excluded.last_synced_at
                    """,
                    (
                        video_id,
                        video.get("title") or "",
                        video.get("published_at"),
                        video.get("duration_seconds", video.get("duration_sec")),
                        video.get("content_id"),
                        video.get("source") or "youtube_data_api_v3",
                        collected_at,
                        collected_at,
                    ),
                )
            connection.commit()

    def save_metric_snapshots(
        self,
        rows: Sequence[Dict[str, Any]],
        *,
        sync_run_id: str,
        collected_at: str,
    ) -> int:
        if not rows:
            return 0
        saved = 0
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                for row in rows:
                    metrics = row["metrics"]
                    values = _flat_metric_values(metrics)
                    views = values["views"]
                    gained = values["subscribers_gained"]
                    period_start = datetime.fromisoformat(row["period_start"])
                    published_at = (
                        datetime.fromisoformat(str(row["published_at"])[:10])
                        if row.get("published_at")
                        else period_start
                    )
                    effective_start = max(period_start, published_at)
                    period_days = max(
                        1,
                        (datetime.fromisoformat(row["period_end"]) - effective_start).days
                        + 1,
                    )
                    growth = (float(views) / period_days) if views is not None else None
                    conversion = (
                        float(gained) / float(views)
                        if gained is not None and views not in (None, 0)
                        else None
                    )
                    derivation_metadata = {
                        "view_growth_per_day": {
                            "formula": "views / inclusive_period_days",
                            "effective_period_start": effective_start.date().isoformat(),
                            "inclusive_period_days": period_days,
                        },
                        "subscriber_conversion": {
                            "formula": "subscribers_gained / views",
                        },
                    }
                    snapshot_fingerprint = _fingerprint(
                        {
                            "period_start": row["period_start"],
                            "data_through": row.get("data_through"),
                            "values": values,
                            "statuses": _status_map(metrics),
                        }
                    )
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO video_metric_snapshots (
                            video_id, snapshot_label, period_start, period_end,
                            data_through, collected_at, source, sync_run_id,
                            views, likes, comments, shares, subscribers_gained,
                            subscribers_lost, estimated_minutes_watched,
                            average_view_duration, average_view_percentage,
                            view_growth_per_day, subscriber_conversion,
                            metric_statuses_json, derivation_metadata_json, sample_size,
                            snapshot_fingerprint
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            row["video_id"],
                            row.get("snapshot_label"),
                            row["period_start"],
                            row["period_end"],
                            row.get("data_through"),
                            collected_at,
                            row["source"],
                            sync_run_id,
                            values["views"],
                            values["likes"],
                            values["comments"],
                            values["shares"],
                            values["subscribers_gained"],
                            values["subscribers_lost"],
                            values["watch_minutes"],
                            values["avg_view_duration_sec"],
                            values["avg_view_percentage"],
                            growth,
                            conversion,
                            _json(_status_map(metrics)),
                            _json(derivation_metadata),
                            int(row.get("sample_size") or 0),
                            snapshot_fingerprint,
                        ),
                    )
                    saved += max(cursor.rowcount, 0)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return saved

    def save_daily_metrics(
        self,
        rows: Sequence[Dict[str, Any]],
        *,
        sync_run_id: str,
        collected_at: str,
    ) -> None:
        if not rows:
            return
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                for row in rows:
                    metrics = row["metrics"]
                    values = _flat_metric_values(metrics)
                    connection.execute(
                        """
                        INSERT INTO video_daily_metrics (
                            video_id, metric_date, views, likes, comments, shares,
                            subscribers_gained, subscribers_lost,
                            estimated_minutes_watched, average_view_duration,
                            average_view_percentage, row_status, metric_statuses_json,
                            collected_at, data_through, source, sync_run_id
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(video_id, metric_date, source) DO UPDATE SET
                            views=excluded.views,
                            likes=excluded.likes,
                            comments=excluded.comments,
                            shares=excluded.shares,
                            subscribers_gained=excluded.subscribers_gained,
                            subscribers_lost=excluded.subscribers_lost,
                            estimated_minutes_watched=excluded.estimated_minutes_watched,
                            average_view_duration=excluded.average_view_duration,
                            average_view_percentage=excluded.average_view_percentage,
                            row_status=excluded.row_status,
                            metric_statuses_json=excluded.metric_statuses_json,
                            collected_at=excluded.collected_at,
                            data_through=excluded.data_through,
                            sync_run_id=excluded.sync_run_id
                        WHERE video_daily_metrics.row_status != 'available'
                           OR excluded.row_status = 'available'
                        """,
                        (
                            row["video_id"],
                            row["metric_date"],
                            values["views"],
                            values["likes"],
                            values["comments"],
                            values["shares"],
                            values["subscribers_gained"],
                            values["subscribers_lost"],
                            values["watch_minutes"],
                            values["avg_view_duration_sec"],
                            values["avg_view_percentage"],
                            row["row_status"],
                            _json(_status_map(metrics)),
                            collected_at,
                            row.get("data_through"),
                            row["source"],
                            sync_run_id,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save_reach_metrics(
        self,
        rows: Sequence[Dict[str, Any]],
        *,
        sync_run_id: str,
        collected_at: str,
    ) -> None:
        if not rows:
            return
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                for row in rows:
                    impressions = row["thumbnail_impressions"]
                    ctr = row["thumbnail_ctr"]
                    connection.execute(
                        """
                        INSERT INTO video_reach_metrics (
                            video_id, metric_date, channel_id,
                            thumbnail_impressions, thumbnail_ctr_percent,
                            impressions_status, ctr_status, collected_at,
                            report_generated_at, source_as_of, data_through,
                            source, report_id, sync_run_id
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(video_id, metric_date, source) DO UPDATE SET
                            channel_id=excluded.channel_id,
                            thumbnail_impressions=excluded.thumbnail_impressions,
                            thumbnail_ctr_percent=excluded.thumbnail_ctr_percent,
                            impressions_status=excluded.impressions_status,
                            ctr_status=excluded.ctr_status,
                            collected_at=excluded.collected_at,
                            report_generated_at=COALESCE(
                                excluded.report_generated_at,
                                video_reach_metrics.report_generated_at
                            ),
                            source_as_of=COALESCE(
                                excluded.source_as_of,
                                video_reach_metrics.source_as_of
                            ),
                            data_through=excluded.data_through,
                            report_id=excluded.report_id,
                            sync_run_id=excluded.sync_run_id
                        WHERE video_reach_metrics.impressions_status != 'available'
                           OR excluded.impressions_status = 'available'
                        """,
                        (
                            row["video_id"],
                            row["metric_date"],
                            row.get("channel_id"),
                            impressions.get("value"),
                            ctr.get("value"),
                            impressions["status"],
                            ctr["status"],
                            collected_at,
                            row.get("report_generated_at"),
                            row.get("source_as_of") or row.get("data_through"),
                            row.get("data_through"),
                            row["source"],
                            row.get("report_id"),
                            sync_run_id,
                        ),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def save_retention_snapshot(
        self,
        result: Dict[str, Any],
        *,
        duration_seconds: int | None,
        estimate: float | None,
        estimate_metadata: Dict[str, Any],
        sync_run_id: str,
        collected_at: str,
    ) -> int | None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                snapshot_fingerprint = _fingerprint(
                    {
                        "period_start": result["period_start"],
                        "data_through": result.get("data_through"),
                        "status": result["status"],
                        "points": result.get("points") or [],
                    }
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO video_retention_snapshots (
                        video_id, period_start, period_end, data_through,
                        collected_at, source, status, duration_seconds,
                        retention_30s_estimate, estimate_metadata_json,
                        point_count, sync_run_id, error_message,
                        snapshot_fingerprint
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        result["video_id"],
                        result["period_start"],
                        result["period_end"],
                        result.get("data_through"),
                        collected_at,
                        result["source"],
                        result["status"],
                        duration_seconds,
                        estimate,
                        _json(estimate_metadata),
                        len(result.get("points") or []),
                        sync_run_id,
                        result.get("error_message"),
                        snapshot_fingerprint,
                    ),
                )
                if cursor.rowcount == 0:
                    connection.commit()
                    return None
                snapshot_id = int(cursor.lastrowid)
                for point in result.get("points") or []:
                    connection.execute(
                        """
                        INSERT INTO video_retention_points (
                            snapshot_id, elapsed_video_time_ratio,
                            audience_watch_ratio, relative_retention_performance
                        ) VALUES (?,?,?,?)
                        """,
                        (
                            snapshot_id,
                            point["elapsed_video_time_ratio"],
                            point["audience_watch_ratio"],
                            point.get("relative_retention_performance"),
                        ),
                    )
                connection.commit()
                return snapshot_id
            except Exception:
                connection.rollback()
                raise

    def upsert_reporting_job(
        self, job: Dict[str, Any], *, status: str = "active", checked_at: str | None = None
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO youtube_reporting_jobs (
                    job_id, report_type_id, job_name, status, create_time,
                    expire_time, last_checked_at, metadata_json
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    report_type_id=excluded.report_type_id,
                    job_name=excluded.job_name,
                    status=excluded.status,
                    create_time=excluded.create_time,
                    expire_time=excluded.expire_time,
                    last_checked_at=excluded.last_checked_at,
                    metadata_json=excluded.metadata_json
                """,
                (
                    job["id"],
                    job["reportTypeId"],
                    job.get("name"),
                    status,
                    job.get("createTime"),
                    job.get("expireTime"),
                    checked_at or utc_now(),
                    _json({"system_managed": bool(job.get("systemManaged"))}),
                ),
            )
            connection.commit()

    def record_reporting_file(
        self,
        report: Dict[str, Any],
        *,
        job_id: str,
        status: str,
        sha256: str | None = None,
        row_count: int | None = None,
        error_message: str | None = None,
        downloaded_at: str | None = None,
        imported_at: str | None = None,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO youtube_reporting_files (
                    report_id, job_id, start_time, end_time, create_time,
                    downloaded_at, imported_at, status, sha256, row_count,
                    error_message, metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(report_id) DO UPDATE SET
                    job_id=excluded.job_id,
                    start_time=excluded.start_time,
                    end_time=excluded.end_time,
                    create_time=excluded.create_time,
                    downloaded_at=COALESCE(excluded.downloaded_at, youtube_reporting_files.downloaded_at),
                    imported_at=COALESCE(excluded.imported_at, youtube_reporting_files.imported_at),
                    status=excluded.status,
                    sha256=COALESCE(excluded.sha256, youtube_reporting_files.sha256),
                    row_count=COALESCE(excluded.row_count, youtube_reporting_files.row_count),
                    error_message=excluded.error_message,
                    metadata_json=excluded.metadata_json
                """,
                (
                    report["id"],
                    job_id,
                    report.get("startTime"),
                    report.get("endTime"),
                    report.get("createTime"),
                    downloaded_at,
                    imported_at,
                    status,
                    sha256,
                    row_count,
                    error_message,
                    _json({"download_url_present": bool(report.get("downloadUrl"))}),
                ),
            )
            connection.commit()

    def get_reporting_file_status(self, report_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status FROM youtube_reporting_files WHERE report_id=?",
                (report_id,),
            ).fetchone()
        return str(row[0]) if row else None

    def get_reach_for_videos(self, video_ids: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        if not video_ids:
            return {}
        placeholders = ",".join("?" for _ in video_ids)
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT * FROM video_reach_metrics
                WHERE video_id IN ({placeholders})
                ORDER BY metric_date
                """,
                tuple(video_ids),
            ).fetchall()
        normalized = []
        for raw in rows:
            row = dict(raw)
            normalized.append(
                {
                    "video_id": row["video_id"],
                    "metric_date": row["metric_date"],
                    "thumbnail_impressions": {
                        "value": row["thumbnail_impressions"],
                        "status": row["impressions_status"],
                    },
                    "thumbnail_ctr": {
                        "value": row["thumbnail_ctr_percent"],
                        "status": row["ctr_status"],
                    },
                    "source_as_of": row.get("source_as_of") or row["metric_date"],
                    "report_generated_at": row.get("report_generated_at"),
                    "collected_at": row.get("collected_at"),
                    "data_through": row.get("data_through"),
                }
            )
        return aggregate_reach_by_video(normalized)

    def get_reach_history(
        self, video_ids: Sequence[str] | None = None, *, limit: int = 50_000
    ) -> List[Dict[str, Any]]:
        """Return official daily Reach rows without manufacturing missing metrics."""

        params: list[Any] = []
        where = ""
        unique_ids = list(dict.fromkeys(video_ids or []))
        if unique_ids:
            placeholders = ",".join("?" for _ in unique_ids)
            where = f"WHERE r.video_id IN ({placeholders})"
            params.extend(unique_ids)
        params.append(max(1, min(int(limit), 200_000)))
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT r.*, v.title, v.published_at,
                       d.views AS daily_views,
                       d.average_view_percentage AS daily_average_view_percentage
                FROM video_reach_metrics r
                LEFT JOIN youtube_videos v ON v.video_id=r.video_id
                LEFT JOIN video_daily_metrics d
                  ON d.video_id=r.video_id AND d.metric_date=r.metric_date
                 AND d.row_status='available'
                {where}
                ORDER BY r.metric_date DESC, r.video_id
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_reporting_created_after(self) -> str | None:
        """Use a small overlap so newly generated backfills are still discovered."""

        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT MAX(CASE WHEN status<>'error' THEN create_time END),
                       MIN(CASE WHEN status='error' THEN create_time END)
                FROM youtube_reporting_files
                """
            ).fetchone()
        candidates = []
        for raw, overlap in ((row[0] if row else None, timedelta(days=2)), (row[1] if row else None, timedelta(minutes=1))):
            if not raw:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            candidates.append(parsed - overlap)
        if not candidates:
            return None
        return min(candidates).isoformat().replace(
            "+00:00", "Z"
        )

    def get_reporting_status(self) -> Dict[str, Any]:
        """Public operational status; report URLs and OAuth material are excluded."""

        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            job = connection.execute(
                """
                SELECT report_type_id, job_name, status, create_time, expire_time,
                       last_checked_at
                FROM youtube_reporting_jobs
                WHERE report_type_id='channel_reach_basic_a1'
                ORDER BY last_checked_at DESC LIMIT 1
                """
            ).fetchone()
            files = connection.execute(
                """
                SELECT COUNT(*) AS discovered,
                       COALESCE(SUM(CASE WHEN status='imported' THEN 1 ELSE 0 END),0) AS imported,
                       COALESCE(SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),0) AS errors,
                       MAX(imported_at) AS last_imported_at,
                       MAX(end_time) AS latest_report_end
                FROM youtube_reporting_files
                """
            ).fetchone()
            reach = connection.execute(
                """
                SELECT COUNT(*) AS row_count,
                       COUNT(DISTINCT video_id) AS video_count,
                       MIN(metric_date) AS period_start,
                       MAX(metric_date) AS source_as_of,
                       MAX(collected_at) AS last_collected_at,
                       MAX(report_generated_at) AS last_report_generated_at
                FROM video_reach_metrics
                WHERE impressions_status='available' AND ctr_status='available'
                """
            ).fetchone()
        return {
            "report_type_id": "channel_reach_basic_a1",
            "job": dict(job) if job else None,
            "reports": dict(files) if files else {},
            "reach": dict(reach) if reach else {},
        }

    def get_recent_video_metrics(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT v.*, s.views, s.likes, s.comments, s.shares,
                       s.subscribers_gained, s.subscribers_lost,
                       s.estimated_minutes_watched, s.average_view_duration,
                       s.average_view_percentage, s.view_growth_per_day,
                       s.subscriber_conversion, s.metric_statuses_json,
                       s.derivation_metadata_json,
                       s.period_start, s.period_end, s.data_through,
                       s.collected_at AS metrics_collected_at, s.sample_size
                FROM youtube_videos v
                LEFT JOIN video_metric_snapshots s ON s.id = (
                    SELECT s2.id FROM video_metric_snapshots s2
                    WHERE s2.video_id=v.video_id
                    ORDER BY s2.collected_at DESC, s2.id DESC LIMIT 1
                )
                ORDER BY v.published_at DESC, v.video_id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metric_statuses"] = json.loads(item.pop("metric_statuses_json") or "{}")
            item["derivation_metadata"] = json.loads(
                item.pop("derivation_metadata_json") or "{}"
            )
            result.append(item)
        return result

    def get_video_retention(self, video_id: str) -> Dict[str, Any] | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            snapshot = connection.execute(
                """
                SELECT * FROM video_retention_snapshots
                WHERE video_id=? ORDER BY collected_at DESC, id DESC LIMIT 1
                """,
                (video_id,),
            ).fetchone()
            if not snapshot:
                return None
            points = connection.execute(
                """
                SELECT elapsed_video_time_ratio, audience_watch_ratio,
                       relative_retention_performance
                FROM video_retention_points
                WHERE snapshot_id=? ORDER BY elapsed_video_time_ratio
                """,
                (snapshot["id"],),
            ).fetchall()
        result = dict(snapshot)
        result["estimate_metadata"] = json.loads(result.pop("estimate_metadata_json"))
        result["points"] = [dict(row) for row in points]
        return result

    def compare_video_performance(self, video_ids: Sequence[str]) -> List[Dict[str, Any]]:
        unique_ids = list(dict.fromkeys(video_ids))
        if not unique_ids:
            return []
        placeholders = ",".join("?" for _ in unique_ids)
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT v.*, s.views, s.likes, s.comments, s.shares,
                       s.subscribers_gained, s.subscribers_lost,
                       s.estimated_minutes_watched, s.average_view_duration,
                       s.average_view_percentage, s.view_growth_per_day,
                       s.subscriber_conversion, s.metric_statuses_json,
                       s.derivation_metadata_json,
                       s.period_start, s.period_end, s.data_through,
                       s.collected_at AS metrics_collected_at, s.sample_size
                FROM youtube_videos v
                LEFT JOIN video_metric_snapshots s ON s.id = (
                    SELECT s2.id FROM video_metric_snapshots s2
                    WHERE s2.video_id=v.video_id
                    ORDER BY s2.collected_at DESC, s2.id DESC LIMIT 1
                )
                WHERE v.video_id IN ({placeholders})
                """,
                tuple(unique_ids),
            ).fetchall()
        by_id: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            item["metric_statuses"] = json.loads(item.pop("metric_statuses_json") or "{}")
            item["derivation_metadata"] = json.loads(
                item.pop("derivation_metadata_json") or "{}"
            )
            by_id[item["video_id"]] = item
        return [by_id[video_id] for video_id in unique_ids if video_id in by_id]

    def get_channel_performance(self, *, limit: int = 50) -> Dict[str, Any]:
        rows = self.get_recent_video_metrics(limit=limit)
        available_views = [row["views"] for row in rows if row.get("views") is not None]
        data_through_values = [row["data_through"] for row in rows if row.get("data_through")]
        return {
            "videos": rows,
            "sample_size": len(available_views),
            "views_total": sum(available_views) if available_views else None,
            "data_through": max(data_through_values) if data_through_values else None,
            "source": "video_metric_snapshots",
        }

    def get_collection_status(self) -> Dict[str, Any]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM youtube_collection_state WHERE singleton=1"
            ).fetchone()
        if not row:
            return {"status": "not_initialized"}
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        item.pop("lock_owner", None)
        return item

    def acquire_collection_lease(
        self, owner: str, *, now: str, lock_until: str
    ) -> bool:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE youtube_collection_state
                SET status='running', lock_owner=?, lock_until=?,
                    last_started_at=?, error_message=NULL
                WHERE singleton=1
                  AND (lock_until IS NULL OR lock_until < ? OR lock_owner=?)
                """,
                (owner, lock_until, now, now, owner),
            )
            connection.commit()
            return cursor.rowcount == 1

    def finish_collection(
        self,
        owner: str,
        *,
        status: str,
        completed_at: str,
        data_through: str | None = None,
        videos_seen: int | None = None,
        snapshots_saved: int | None = None,
        retention_saved: int | None = None,
        reach_imported: int | None = None,
        error_message: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE youtube_collection_state
                SET status=?, lock_owner=NULL, lock_until=NULL,
                    last_completed_at=?,
                    last_success_at=CASE WHEN ?='success' THEN ? ELSE last_success_at END,
                    data_through=COALESCE(?, data_through),
                    videos_seen=COALESCE(?, videos_seen),
                    snapshots_saved=COALESCE(?, snapshots_saved),
                    retention_saved=COALESCE(?, retention_saved),
                    reach_imported=COALESCE(?, reach_imported),
                    error_message=?, metadata_json=?
                WHERE singleton=1 AND lock_owner=?
                """,
                (
                    status,
                    completed_at,
                    status,
                    completed_at,
                    data_through,
                    videos_seen,
                    snapshots_saved,
                    retention_saved,
                    reach_imported,
                    error_message,
                    _json(metadata or {}),
                    owner,
                ),
            )
            connection.commit()

    def videos_needing_retention(self, *, limit: int = 20) -> List[Dict[str, Any]]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT v.*,
                       MAX(r.collected_at) AS retention_collected_at,
                       COUNT(r.id) AS retention_snapshot_count
                FROM youtube_videos v
                LEFT JOIN video_retention_snapshots r ON r.video_id=v.video_id
                GROUP BY v.video_id
                ORDER BY CASE WHEN COUNT(r.id)=0 THEN 0 ELSE 1 END,
                         MAX(r.collected_at), v.published_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_video_metric_history(
        self, video_id: str, *, limit: int = 30
    ) -> List[Dict[str, Any]]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM video_metric_snapshots
                WHERE video_id=?
                ORDER BY collected_at DESC, id DESC LIMIT ?
                """,
                (video_id, limit),
            ).fetchall()
        result = []
        for raw in rows:
            item = dict(raw)
            item["metric_statuses"] = json.loads(
                item.pop("metric_statuses_json") or "{}"
            )
            item["derivation_metadata"] = json.loads(
                item.pop("derivation_metadata_json") or "{}"
            )
            item.pop("snapshot_fingerprint", None)
            result.append(item)
        return result


def init_analytics_schema() -> None:
    AnalyticsRepository().init_schema()
