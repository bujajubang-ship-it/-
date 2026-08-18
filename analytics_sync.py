"""Transactional sync orchestration for YouTube performance data."""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Sequence

from analytics_reporting import (
    REACH_REPORT_TYPE_ID,
    REACH_SOURCE,
    YouTubeReportingService,
    parse_reach_csv,
)
from analytics_repository import AnalyticsRepository, utc_now
from analytics_service import (
    ANALYTICS_SOURCE,
    AnalyticsService,
    retention_30s_estimate,
)


SNAPSHOT_MILESTONES = (1, 3, 7, 14, 30)


def snapshot_label(published_at: str | None, data_through: str | None) -> str | None:
    if not published_at or not data_through:
        return None
    try:
        age_days = (
            date.fromisoformat(data_through[:10]) - date.fromisoformat(published_at[:10])
        ).days
    except ValueError:
        return None
    return f"D{age_days}" if age_days in SNAPSHOT_MILESTONES else None


def _video_records(videos: Sequence[Dict[str, Any]]) -> list[Dict[str, Any]]:
    return [
        {
            "video_id": video.get("video_id") or video.get("id"),
            "title": video.get("title") or "",
            "published_at": video.get("published_at"),
            "duration_seconds": video.get("duration_seconds", video.get("duration_sec")),
            "content_id": video.get("content_id"),
            "source": "youtube_data_api_v3",
        }
        for video in videos
        if video.get("video_id") or video.get("id")
    ]


def _safe_error(exc: Exception) -> str:
    # API wrappers already sanitize messages; keep persistence bounded and token-free.
    return f"{type(exc).__name__}: {str(exc)[:300]}"


class AnalyticsSyncCoordinator:
    def __init__(
        self,
        service: AnalyticsService | None,
        repository: AnalyticsRepository,
    ):
        self.service = service
        self.repository = repository
        self.last_persisted_count = 0

    def _require_service(self) -> AnalyticsService:
        if self.service is None:
            raise RuntimeError("Analytics service is required for this sync operation")
        return self.service

    async def sync_video_snapshots(
        self,
        videos: Sequence[Dict[str, Any]],
        *,
        period_start: str,
        period_end: str,
        collected_at: str | None = None,
    ) -> list[Dict[str, Any]]:
        collected = collected_at or utc_now()
        records = _video_records(videos)
        run_id = self.repository.begin_sync_run(
            "video_metric_snapshot",
            ANALYTICS_SOURCE,
            period_start=period_start,
            period_end=period_end,
            metadata={"requested_video_count": len(records)},
            started_at=collected,
        )
        try:
            results = await self._require_service().get_video_analytics(
                period_start,
                end_date=period_end,
                video_ids=[row["video_id"] for row in records],
            )
            published = {row["video_id"]: row.get("published_at") for row in records}
            for row in results:
                row["published_at"] = published.get(row["video_id"])
                row["snapshot_label"] = snapshot_label(
                    published.get(row["video_id"]), row.get("data_through")
                )
            # Persist metadata and snapshots only after the complete API result succeeds.
            self.repository.upsert_videos(records, collected_at=collected)
            self.last_persisted_count = self.repository.save_metric_snapshots(
                results, sync_run_id=run_id, collected_at=collected
            )
            data_through_values = [
                row["data_through"] for row in results if row.get("data_through")
            ]
            self.repository.finish_sync_run(
                run_id,
                status="success",
                data_through=max(data_through_values) if data_through_values else None,
                row_count=len(results),
                completed_at=collected,
            )
            return results
        except Exception as exc:
            self.repository.finish_sync_run(
                run_id,
                status="error",
                error_message=_safe_error(exc),
                completed_at=collected,
            )
            raise

    async def sync_daily_metrics(
        self,
        videos: Sequence[Dict[str, Any]],
        requested_dates: Sequence[str],
        *,
        collected_at: str | None = None,
    ) -> list[Dict[str, Any]]:
        if not requested_dates:
            return []
        collected = collected_at or utc_now()
        records = _video_records(videos)
        run_id = self.repository.begin_sync_run(
            "video_daily_metrics",
            ANALYTICS_SOURCE,
            period_start=min(requested_dates),
            period_end=max(requested_dates),
            metadata={"requested_video_count": len(records)},
            started_at=collected,
        )
        try:
            rows = await self._require_service().get_daily_video_metrics(
                [row["video_id"] for row in records], requested_dates
            )
            self.repository.upsert_videos(records, collected_at=collected)
            self.repository.save_daily_metrics(
                rows, sync_run_id=run_id, collected_at=collected
            )
            data_through_values = [
                row["data_through"] for row in rows if row.get("data_through")
            ]
            self.repository.finish_sync_run(
                run_id,
                status="success",
                data_through=max(data_through_values) if data_through_values else None,
                row_count=len(rows),
                completed_at=collected,
            )
            return rows
        except Exception as exc:
            self.repository.finish_sync_run(
                run_id,
                status="error",
                error_message=_safe_error(exc),
                completed_at=collected,
            )
            raise

    async def sync_retention(
        self,
        video: Dict[str, Any],
        *,
        period_start: str,
        period_end: str,
        collected_at: str | None = None,
    ) -> Dict[str, Any]:
        collected = collected_at or utc_now()
        records = _video_records([video])
        if not records:
            raise ValueError("video_id is required")
        record = records[0]
        run_id = self.repository.begin_sync_run(
            "video_retention",
            ANALYTICS_SOURCE,
            period_start=period_start,
            period_end=period_end,
            metadata={"video_count": 1},
            started_at=collected,
        )
        try:
            result = await self._require_service().get_video_retention(
                record["video_id"], start_date=period_start, end_date=period_end
            )
            estimate, metadata = retention_30s_estimate(
                result.get("points") or [], record.get("duration_seconds")
            )
            self.repository.upsert_videos(records, collected_at=collected)
            snapshot_id = self.repository.save_retention_snapshot(
                result,
                duration_seconds=record.get("duration_seconds"),
                estimate=estimate,
                estimate_metadata=metadata,
                sync_run_id=run_id,
                collected_at=collected,
            )
            self.repository.finish_sync_run(
                run_id,
                status="success",
                data_through=result.get("data_through"),
                row_count=len(result.get("points") or []),
                completed_at=collected,
            )
            result["retention_30s_estimate"] = estimate
            result["retention_30s_metadata"] = metadata
            result["persisted"] = snapshot_id is not None
            return result
        except Exception as exc:
            self.repository.finish_sync_run(
                run_id,
                status="error",
                error_message=_safe_error(exc),
                completed_at=collected,
            )
            raise

    def import_reach_csv(
        self,
        csv_text: str,
        videos: Sequence[Dict[str, Any]],
        *,
        report_id: str,
        report_date: str,
        collected_at: str | None = None,
    ) -> list[Dict[str, Any]]:
        collected = collected_at or utc_now()
        records = _video_records(videos)
        run_id = self.repository.begin_sync_run(
            "reach_report_import",
            REACH_SOURCE,
            period_start=report_date,
            period_end=report_date,
            metadata={"report_id": report_id},
            started_at=collected,
        )
        try:
            rows = parse_reach_csv(
                csv_text,
                expected_video_ids=[row["video_id"] for row in records],
                expected_date=report_date,
                report_id=report_id,
            )
            self.repository.upsert_videos(records, collected_at=collected)
            self.repository.save_reach_metrics(
                rows, sync_run_id=run_id, collected_at=collected
            )
            self.repository.finish_sync_run(
                run_id,
                status="success",
                data_through=report_date,
                row_count=len(rows),
                completed_at=collected,
            )
            return rows
        except Exception as exc:
            self.repository.finish_sync_run(
                run_id,
                status="error",
                error_message=_safe_error(exc),
                completed_at=collected,
            )
            raise


class ReportingSyncCoordinator:
    """Import already-generated Reach reports; never creates Reporting jobs."""

    def __init__(
        self,
        service: YouTubeReportingService,
        repository: AnalyticsRepository,
    ):
        self.service = service
        self.repository = repository

    async def sync_existing_reach_reports(
        self,
        videos: Sequence[Dict[str, Any]],
        *,
        created_after: str | None = None,
        collected_at: str | None = None,
    ) -> Dict[str, int]:
        collected = collected_at or utc_now()
        jobs = await self.service.list_jobs()
        imported = 0
        skipped = 0
        for job in jobs:
            if job.get("reportTypeId") != REACH_REPORT_TYPE_ID:
                continue
            self.repository.upsert_reporting_job(job, checked_at=collected)
            reports = await self.service.list_reports(
                str(job["id"]), created_after=created_after
            )
            newest_by_period: Dict[tuple[str, str], Dict[str, Any]] = {}
            for report in reports:
                key = (str(report.get("startTime") or ""), str(report.get("endTime") or ""))
                current = newest_by_period.get(key)
                if current is None or str(report.get("createTime") or "") > str(
                    current.get("createTime") or ""
                ):
                    if current is not None:
                        self.repository.record_reporting_file(
                            current,
                            job_id=str(job["id"]),
                            status="superseded",
                        )
                    newest_by_period[key] = report
                else:
                    self.repository.record_reporting_file(
                        report,
                        job_id=str(job["id"]),
                        status="superseded",
                    )
            for report in sorted(
                newest_by_period.values(), key=lambda item: str(item.get("createTime") or "")
            ):
                report_id = str(report["id"])
                if self.repository.get_reporting_file_status(report_id) == "imported":
                    skipped += 1
                    continue
                self.repository.record_reporting_file(
                    report, job_id=str(job["id"]), status="discovered"
                )
                try:
                    csv_text, digest = await self.service.download_report(
                        str(report["downloadUrl"])
                    )
                    report_date = str(report.get("startTime") or "")[:10]
                    if not report_date:
                        raise ValueError("Reach report is missing startTime")
                    rows = AnalyticsSyncCoordinator(
                        service=None, repository=self.repository
                    ).import_reach_csv(
                        csv_text,
                        videos,
                        report_id=report_id,
                        report_date=report_date,
                        collected_at=collected,
                    )
                    self.repository.record_reporting_file(
                        report,
                        job_id=str(job["id"]),
                        status="imported",
                        sha256=digest,
                        row_count=len(rows),
                        downloaded_at=collected,
                        imported_at=collected,
                    )
                    imported += 1
                except Exception as exc:
                    self.repository.record_reporting_file(
                        report,
                        job_id=str(job["id"]),
                        status="error",
                        error_message=_safe_error(exc),
                    )
                    raise
        return {"jobs": len(jobs), "imported": imported, "skipped": skipped}
