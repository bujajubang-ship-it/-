"""Idempotent YouTube collection and the in-process Render scheduler."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from analytics_reporting import YouTubeReportingService
from analytics_repository import AnalyticsRepository, utc_now
from analytics_service import AnalyticsService
from analytics_sync import AnalyticsSyncCoordinator, ReportingSyncCoordinator
from youtube_service import YouTubeService


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _safe_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {str(exc)[:240]}"


@dataclass
class CollectionResult:
    status: str
    videos_seen: int = 0
    snapshots_saved: int = 0
    retention_saved: int = 0
    reach_imported: int = 0
    data_through: str | None = None
    warnings: list[str] | None = None

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class YouTubeCollectionService:
    """Collect full-channel snapshots without replacing previously valid rows."""

    def __init__(self, repository: AnalyticsRepository | None = None) -> None:
        self.repository = repository or AnalyticsRepository()

    async def run_once(self, *, trigger: str = "manual") -> CollectionResult:
        owner = str(uuid.uuid4())
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat().replace("+00:00", "Z")
        lock_until = (now_dt + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        if not self.repository.acquire_collection_lease(
            owner, now=now, lock_until=lock_until
        ):
            return CollectionResult(status="already_running")

        youtube: YouTubeService | None = None
        analytics: AnalyticsService | None = None
        reporting: YouTubeReportingService | None = None
        result = CollectionResult(status="error", warnings=[])
        try:
            api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
            if not api_key:
                raise RuntimeError("YOUTUBE_API_KEY is not configured")
            analytics = AnalyticsService()
            if not analytics.is_configured():
                raise RuntimeError("YouTube OAuth environment is not configured")

            authenticated_channel_id = await analytics.get_authenticated_channel_id()
            configured_channel_id = os.getenv("MY_CHANNEL_ID", "").strip()
            if configured_channel_id and configured_channel_id != authenticated_channel_id:
                raise RuntimeError("OAuth channel does not match MY_CHANNEL_ID")

            youtube = YouTubeService(api_key)
            max_videos = _env_int("YT_COLLECTION_MAX_VIDEOS", 1000, 1, 5000)
            _channel, videos = await youtube.get_channel_videos(
                authenticated_channel_id, max_videos=max_videos
            )
            if not videos:
                raise RuntimeError("Owned YouTube channel returned no videos")
            result.videos_seen = len(videos)

            # Metadata is a successful Data API result in its own right. Preserve it
            # even when a later Analytics API request is temporarily unavailable.
            self.repository.upsert_videos(videos, collected_at=now)

            period_start = min(
                (str(video.get("published_at"))[:10] for video in videos if video.get("published_at")),
                default="2000-01-01",
            )
            period_end = date.today().isoformat()
            coordinator = AnalyticsSyncCoordinator(analytics, self.repository)
            snapshots = await coordinator.sync_video_snapshots(
                videos, period_start=period_start, period_end=period_end, collected_at=now
            )
            result.snapshots_saved = coordinator.last_persisted_count
            through = [row.get("data_through") for row in snapshots if row.get("data_through")]
            result.data_through = max(through) if through else None

            lookback = _env_int("YT_DAILY_LOOKBACK_DAYS", 45, 7, 365)
            requested_dates = [
                (date.today() - timedelta(days=offset)).isoformat()
                for offset in range(lookback - 1, -1, -1)
            ]
            try:
                await coordinator.sync_daily_metrics(
                    videos, requested_dates, collected_at=now
                )
            except Exception as exc:
                result.warnings.append("daily_metrics: " + _safe_error(exc))

            retention_limit = _env_int("YT_RETENTION_PER_RUN", 20, 0, 200)
            for target in self.repository.videos_needing_retention(limit=retention_limit):
                try:
                    retention = await coordinator.sync_retention(
                        target,
                        period_start=str(target.get("published_at") or period_start)[:10],
                        period_end=period_end,
                        collected_at=now,
                    )
                    if retention.get("persisted"):
                        result.retention_saved += 1
                except Exception as exc:
                    result.warnings.append(
                        f"retention:{target.get('video_id')}: {_safe_error(exc)}"
                    )

            reporting = YouTubeReportingService(analytics.get_access_token)
            try:
                if _env_bool("YT_REPORTING_AUTO_CREATE", True):
                    job = await reporting.ensure_reach_job()
                    self.repository.upsert_reporting_job(job, checked_at=now)
                reach = await ReportingSyncCoordinator(
                    reporting, self.repository
                ).sync_existing_reach_reports(videos, collected_at=now)
                result.reach_imported = int(reach.get("imported") or 0)
            except Exception as exc:
                result.warnings.append("reach: " + _safe_error(exc))

            result.status = "success"
            try:
                from strategy_repository import StrategyRepository

                StrategyRepository().refresh_performance_checkpoints()
            except Exception as exc:
                result.warnings.append("feedback_loop: " + _safe_error(exc))
            self.repository.finish_collection(
                owner,
                status="success",
                completed_at=utc_now(),
                data_through=result.data_through,
                videos_seen=result.videos_seen,
                snapshots_saved=result.snapshots_saved,
                retention_saved=result.retention_saved,
                reach_imported=result.reach_imported,
                metadata={"trigger": trigger, "warnings": result.warnings},
            )
            return result
        except Exception as exc:
            self.repository.finish_collection(
                owner,
                status="error",
                completed_at=utc_now(),
                error_message=_safe_error(exc),
                metadata={"trigger": trigger, "warnings": result.warnings or []},
            )
            raise
        finally:
            if reporting is not None:
                await reporting.close()
            if analytics is not None:
                await analytics.close()
            if youtube is not None:
                await youtube.close()


class YouTubeCollectionScheduler:
    def __init__(self, collector: YouTubeCollectionService | None = None) -> None:
        self.collector = collector or YouTubeCollectionService()
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        default = bool(os.getenv("RENDER", "").strip())
        return _env_bool("YT_AUTO_SYNC_ENABLED", default)

    def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="youtube-collection-scheduler")

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
        initial_delay = _env_int("YT_AUTO_SYNC_INITIAL_DELAY_SECONDS", 90, 0, 3600)
        interval = _env_int("YT_AUTO_SYNC_INTERVAL_SECONDS", 21600, 900, 86400)
        await asyncio.sleep(initial_delay)
        while True:
            try:
                await self.collector.run_once(trigger="scheduler")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[youtube-collection] {_safe_error(exc)}", flush=True)
            await asyncio.sleep(interval)
