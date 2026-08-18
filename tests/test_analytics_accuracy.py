import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import httpx

from analytics_reporting import (
    CTR_UNIT,
    REACH_REPORT_TYPE_ID,
    YouTubeReportingService,
    parse_reach_csv,
    weighted_ctr,
)
from analytics_repository import ANALYTICS_TABLES, AnalyticsRepository
from analytics_service import (
    ANALYTICS_METRICS,
    ANALYTICS_SOURCE,
    STATUS_AVAILABLE,
    STATUS_NOT_REPORTED,
    STATUS_PENDING,
    STATUS_UNAVAILABLE,
    AnalyticsService,
    parse_daily_response,
    parse_retention_response,
    parse_video_aggregate_response,
    retention_30s_estimate,
)
from analytics_sync import AnalyticsSyncCoordinator, ReportingSyncCoordinator


FIXTURES = Path(__file__).parent / "fixtures"


def headers(*names):
    return [{"name": name, "columnType": "METRIC"} for name in names]


class MetricParsingTests(unittest.TestCase):
    def test_missing_metric_is_not_zero_and_actual_zero_is_available(self):
        payload = {
            "columnHeaders": headers("video", "views", "likes"),
            "rows": [["video-a", 0, 0]],
        }
        result = parse_video_aggregate_response(
            payload,
            ["video-a"],
            period_start="2026-08-01",
            period_end="2026-08-10",
            data_through="2026-08-09",
        )[0]
        self.assertEqual(result["views"], 0)
        self.assertEqual(result["views_status"], STATUS_AVAILABLE)
        self.assertIsNone(result["shares"])
        self.assertEqual(result["shares_status"], STATUS_UNAVAILABLE)
        self.assertNotIn("impressions", result)
        self.assertNotIn("ctr", result)

    def test_shares_and_subscribers_lost_are_parsed(self):
        names = ("video",) + ANALYTICS_METRICS
        payload = {
            "columnHeaders": headers(*names),
            "rows": [["video-a", 10, 2, 1, 3, 4, 2, 50.5, 100.0, 55.5]],
        }
        result = parse_video_aggregate_response(
            payload,
            ["video-a"],
            period_start="2026-08-01",
            period_end="2026-08-10",
            data_through="2026-08-09",
        )[0]
        self.assertEqual(result["shares"], 3)
        self.assertEqual(result["subscribers_lost"], 2)
        self.assertEqual(result["shares_status"], STATUS_AVAILABLE)
        self.assertEqual(result["subscribers_lost_status"], STATUS_AVAILABLE)

    def test_missing_video_row_is_not_reported_not_zero(self):
        payload = {
            "columnHeaders": headers("video", *ANALYTICS_METRICS),
            "rows": [],
        }
        result = parse_video_aggregate_response(
            payload,
            ["video-a"],
            period_start="2026-08-01",
            period_end="2026-08-10",
            data_through="2026-08-09",
        )[0]
        self.assertIsNone(result["views"])
        self.assertEqual(result["views_status"], STATUS_NOT_REPORTED)

    def test_latest_unreturned_dates_are_pending(self):
        payload = {
            "columnHeaders": headers("day", "video", *ANALYTICS_METRICS),
            "rows": [
                [
                    "2026-08-14",
                    "video-a",
                    5,
                    0,
                    0,
                    0,
                    0,
                    0,
                    10,
                    20,
                    30,
                ]
            ],
        }
        rows = parse_daily_response(
            payload, ["video-a"], ["2026-08-13", "2026-08-14", "2026-08-15"]
        )
        by_date = {row["metric_date"]: row for row in rows}
        self.assertEqual(by_date["2026-08-13"]["row_status"], STATUS_NOT_REPORTED)
        self.assertEqual(by_date["2026-08-14"]["metrics"]["views"]["value"], 5)
        self.assertEqual(by_date["2026-08-15"]["row_status"], STATUS_PENDING)
        self.assertIsNone(by_date["2026-08-15"]["metrics"]["views"]["value"])


class RetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads((FIXTURES / "retention_curve.json").read_text())
        cls.points = parse_retention_response(cls.payload)

    def test_curve_parser_keeps_official_fields(self):
        self.assertEqual(len(self.points), 4)
        self.assertEqual(self.points[1]["audience_watch_ratio"], 0.8)
        self.assertEqual(self.points[1]["relative_retention_performance"], 0.6)

    def test_exact_30_second_point(self):
        value, metadata = retention_30s_estimate(self.points, 120)
        self.assertAlmostEqual(value, 0.8)
        self.assertTrue(metadata["exact_point"])

    def test_interpolated_30_second_point(self):
        value, metadata = retention_30s_estimate(self.points, 100)
        # target ratio 0.3 lies 20% between 0.25 (0.8) and 0.5 (0.6)
        self.assertAlmostEqual(value, 0.76)
        self.assertFalse(metadata["exact_point"])
        self.assertEqual(metadata["method"], "linear_interpolation_audience_watch_ratio")

    def test_short_video_returns_null(self):
        value, metadata = retention_30s_estimate(self.points, 30)
        self.assertIsNone(value)
        self.assertEqual(metadata["reason"], "video_duration_not_over_30_seconds")

    def test_empty_curve_returns_null(self):
        value, metadata = retention_30s_estimate([], 120)
        self.assertIsNone(value)
        self.assertEqual(metadata["reason"], "insufficient_curve_points")


class ReportingReachTests(unittest.TestCase):
    def test_official_report_type_and_csv_parser(self):
        self.assertEqual(REACH_REPORT_TYPE_ID, "channel_reach_basic_a1")
        rows = parse_reach_csv((FIXTURES / "reach_basic.csv").read_text())
        self.assertEqual(rows[0]["thumbnail_impressions"]["value"], 1000)
        self.assertEqual(rows[0]["thumbnail_ctr"]["value"], 5.0)
        self.assertEqual(rows[0]["ctr_unit"], CTR_UNIT)
        self.assertEqual(rows[1]["thumbnail_impressions"]["value"], 0)
        self.assertEqual(rows[1]["thumbnail_impressions"]["status"], STATUS_AVAILABLE)

    def test_missing_report_row_is_not_reported(self):
        rows = parse_reach_csv(
            (FIXTURES / "reach_basic.csv").read_text(),
            expected_video_ids=["video-a", "video-c"],
            expected_date="2026-08-14",
        )
        missing = next(row for row in rows if row["video_id"] == "video-c")
        self.assertIsNone(missing["thumbnail_impressions"]["value"])
        self.assertEqual(missing["thumbnail_impressions"]["status"], STATUS_NOT_REPORTED)

    def test_ctr_is_impression_weighted_in_percent_units(self):
        rows = [
            {
                "thumbnail_impressions": {"value": 100, "status": STATUS_AVAILABLE},
                "thumbnail_ctr": {"value": 10.0, "status": STATUS_AVAILABLE},
            },
            {
                "thumbnail_impressions": {"value": 900, "status": STATUS_AVAILABLE},
                "thumbnail_ctr": {"value": 2.0, "status": STATUS_AVAILABLE},
            },
        ]
        result = weighted_ctr(rows)
        self.assertAlmostEqual(result["value"], 2.8)
        self.assertEqual(result["unit"], "percent")
        self.assertEqual(
            result["formula"], "sum(impressions * ctr_percent) / sum(impressions)"
        )

    def test_malformed_csv_fails_instead_of_skipping(self):
        with self.assertRaises(ValueError):
            parse_reach_csv("date,video_id\n2026-08-14,video-a\n")


class CapturingAnalyticsService(AnalyticsService):
    def __init__(self):
        self.calls = []
        self._access_token = "test"
        self._owns_http = False
        self.http = None

    async def _query_all_rows(self, params, *, page_size=200):
        self.calls.append(dict(params))
        if params.get("dimensions") == "day":
            return {
                "columnHeaders": headers("day", *ANALYTICS_METRICS),
                "rows": [["2026-08-14", 1, 0, 0, 0, 0, 0, 1, 1, 1]],
            }
        return {
            "columnHeaders": headers("video", *ANALYTICS_METRICS),
            "rows": [
                ["low-view-recent", 1, 0, 0, 0, 0, 0, 1, 1, 1],
                ["high-view-old", 1000, 1, 1, 1, 1, 1, 100, 100, 50],
            ],
        }


class QueryConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_analytics_500_is_retried_without_exposing_body(self):
        attempts = 0

        async def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(500, json={"error": {"message": "temporary"}})
            return httpx.Response(200, json={"columnHeaders": [], "rows": []})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = AnalyticsService(http=client)
        service._access_token = "test-token"
        try:
            result = await service._query({"ids": "channel==MINE"})
        finally:
            await client.aclose()
        self.assertEqual(result["rows"], [])
        self.assertEqual(attempts, 2)

    async def test_existing_youtube_readonly_scope_resolves_owner_channel(self):
        async def handler(request):
            self.assertEqual(request.url.params.get("mine"), "true")
            self.assertEqual(request.headers.get("authorization"), "Bearer test-token")
            return httpx.Response(200, json={"items": [{"id": "UC_OWNER"}]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = AnalyticsService(http=client)
        service._access_token = "test-token"
        try:
            self.assertEqual(await service.get_authenticated_channel_id(), "UC_OWNER")
        finally:
            await client.aclose()

    async def test_recent_low_view_video_is_explicitly_included(self):
        service = CapturingAnalyticsService()
        rows = await service.get_video_analytics(
            "2026-08-01",
            end_date="2026-08-15",
            video_ids=["low-view-recent", "high-view-old"],
        )
        self.assertEqual([row["video_id"] for row in rows], ["low-view-recent", "high-view-old"])
        aggregate_call = next(call for call in service.calls if call["dimensions"] == "video")
        self.assertIn("low-view-recent", aggregate_call["filters"])
        self.assertIn("shares", aggregate_call["metrics"])
        self.assertIn("subscribersLost", aggregate_call["metrics"])
        self.assertNotIn("impression", aggregate_call["metrics"].lower())

    async def test_implicit_top_200_selection_is_disabled(self):
        service = CapturingAnalyticsService()
        with self.assertRaises(ValueError):
            await service.get_video_analytics("2026-08-01")

    async def test_full_channel_freshness_filter_is_chunked(self):
        service = CapturingAnalyticsService()
        video_ids = [f"video-{index}" for index in range(401)]
        await service.get_video_analytics(
            "2020-03-06", end_date="2026-08-18", video_ids=video_ids
        )
        freshness_calls = [
            call for call in service.calls if call["dimensions"] == "day"
        ]
        self.assertEqual(len(freshness_calls), 3)
        filter_sizes = [
            len(call["filters"].removeprefix("video==").split(","))
            for call in freshness_calls
        ]
        self.assertEqual(filter_sizes, [200, 200, 1])

    async def test_daily_query_stays_under_backend_row_budget(self):
        service = CapturingAnalyticsService()
        video_ids = [f"video-{index}" for index in range(25)]
        requested_dates = [f"2026-07-{day:02d}" for day in range(1, 32)] + [
            f"2026-08-{day:02d}" for day in range(1, 15)
        ]
        await service.get_daily_video_metrics(video_ids, requested_dates)
        daily_calls = [
            call for call in service.calls if call["dimensions"] == "day,video"
        ]
        filter_sizes = [
            len(call["filters"].removeprefix("video==").split(","))
            for call in daily_calls
        ]
        self.assertEqual(filter_sizes, [4, 4, 4, 4, 4, 4, 1])

    async def test_retention_query_uses_single_video_filter(self):
        service = CapturingAnalyticsService()

        async def query(params):
            service.calls.append(dict(params))
            return json.loads((FIXTURES / "retention_curve.json").read_text())

        service._query = query
        result = await service.get_video_retention(
            "video-a", start_date="2026-08-01", end_date="2026-08-15"
        )
        call = service.calls[-1]
        self.assertEqual(call["dimensions"], "elapsedVideoTimeRatio")
        self.assertEqual(call["filters"], "video==video-a")
        self.assertEqual(
            call["metrics"], "audienceWatchRatio,relativeRetentionPerformance"
        )
        self.assertEqual(result["status"], STATUS_AVAILABLE)

    async def test_empty_retention_is_pending(self):
        service = CapturingAnalyticsService()

        async def query(params):
            return {"columnHeaders": headers("elapsedVideoTimeRatio", "audienceWatchRatio"), "rows": []}

        service._query = query
        result = await service.get_video_retention(
            "video-a", start_date="2026-08-01", end_date="2026-08-15"
        )
        self.assertEqual(result["status"], STATUS_PENDING)


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "analytics.db"

        def connect():
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            return connection

        self.connect = connect
        with closing(connect()) as connection:
            for name in (
                "history",
                "pipeline",
                "optimize_videos",
                "worksheet_rows",
                "chat_session",
                "knowledge",
            ):
                connection.execute(f'CREATE TABLE "{name}" (id INTEGER PRIMARY KEY)')
            connection.commit()
        self.repository = AnalyticsRepository(connect)
        with closing(connect()) as connection:
            self.core_schema_before = dict(
                connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE name IN (?,?,?,?,?,?)",
                    (
                        "history",
                        "pipeline",
                        "optimize_videos",
                        "worksheet_rows",
                        "chat_session",
                        "knowledge",
                    ),
                ).fetchall()
            )
        self.repository.init_schema()

    def tearDown(self):
        self.temp.cleanup()

    def test_schema_is_idempotent_and_preserves_six_existing_tables(self):
        self.repository.init_schema()
        with closing(self.connect()) as connection:
            actual_tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            core_after = dict(
                connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE name IN (?,?,?,?,?,?)",
                    tuple(self.core_schema_before),
                ).fetchall()
            )
        self.assertTrue(ANALYTICS_TABLES.issubset(actual_tables))
        self.assertEqual(core_after, self.core_schema_before)

    async def test_sync_failure_does_not_overwrite_good_snapshot(self):
        records = [
            {
                "video_id": "video-a",
                "title": "A",
                "published_at": "2026-08-01",
                "duration_seconds": 120,
            }
        ]
        collected = "2026-08-15T00:00:00Z"
        self.repository.upsert_videos(records, collected_at=collected)
        run_id = self.repository.begin_sync_run(
            "video_metric_snapshot",
            ANALYTICS_SOURCE,
            period_start="2026-08-01",
            period_end="2026-08-14",
            started_at=collected,
        )
        metrics = {
            name: {
                "value": 10 if name in {"views", "shares"} else 1,
                "status": STATUS_AVAILABLE,
            }
            for name in ANALYTICS_METRICS
        }
        self.repository.save_metric_snapshots(
            [
                {
                    "video_id": "video-a",
                    "metrics": metrics,
                    "period_start": "2026-08-01",
                    "period_end": "2026-08-14",
                    "data_through": "2026-08-14",
                    "source": ANALYTICS_SOURCE,
                    "sample_size": 1,
                }
            ],
            sync_run_id=run_id,
            collected_at=collected,
        )
        self.repository.finish_sync_run(run_id, status="success", row_count=1)

        class FailingService:
            async def get_video_analytics(self, *args, **kwargs):
                raise RuntimeError("fixture API failure")

        coordinator = AnalyticsSyncCoordinator(FailingService(), self.repository)
        with self.assertRaises(RuntimeError):
            await coordinator.sync_video_snapshots(
                [{"id": "video-a", "title": "A", "published_at": "2026-08-01"}],
                period_start="2026-08-01",
                period_end="2026-08-15",
                collected_at="2026-08-16T00:00:00Z",
            )
        with closing(self.connect()) as connection:
            snapshot_count = connection.execute(
                "SELECT COUNT(*) FROM video_metric_snapshots"
            ).fetchone()[0]
            last_status = connection.execute(
                "SELECT status FROM youtube_sync_runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(snapshot_count, 1)
        self.assertEqual(last_status, "error")

    def test_pending_daily_row_cannot_downgrade_available_row(self):
        video = {
            "video_id": "video-a",
            "title": "A",
            "published_at": "2026-08-01",
            "duration_seconds": 120,
        }
        self.repository.upsert_videos([video], collected_at="2026-08-15T00:00:00Z")

        def save(row_status, views_value, collected_at):
            run_id = self.repository.begin_sync_run(
                "video_daily_metrics",
                ANALYTICS_SOURCE,
                period_start="2026-08-14",
                period_end="2026-08-14",
                started_at=collected_at,
            )
            metrics = {
                name: {
                    "value": views_value if name == "views" else None,
                    "status": STATUS_AVAILABLE if name == "views" and views_value is not None else row_status,
                }
                for name in ANALYTICS_METRICS
            }
            self.repository.save_daily_metrics(
                [
                    {
                        "video_id": "video-a",
                        "metric_date": "2026-08-14",
                        "metrics": metrics,
                        "row_status": row_status,
                        "data_through": "2026-08-14" if row_status == STATUS_AVAILABLE else None,
                        "source": ANALYTICS_SOURCE,
                    }
                ],
                sync_run_id=run_id,
                collected_at=collected_at,
            )

        save(STATUS_AVAILABLE, 12, "2026-08-15T00:00:00Z")
        save(STATUS_PENDING, None, "2026-08-16T00:00:00Z")
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT views, row_status FROM video_daily_metrics WHERE video_id='video-a'"
            ).fetchone()
        self.assertEqual(tuple(row), (12, STATUS_AVAILABLE))


class ReportingSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_newest_report_for_same_period_is_imported(self):
        self.assertFalse(hasattr(YouTubeReportingService, "create_job"))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reporting.db"

            def connect():
                connection = sqlite3.connect(path)
                connection.row_factory = sqlite3.Row
                return connection

            repository = AnalyticsRepository(connect)
            repository.init_schema()

            class FakeReportingService:
                async def list_jobs(self):
                    return [
                        {
                            "id": "job-1",
                            "reportTypeId": REACH_REPORT_TYPE_ID,
                            "name": "reach",
                        }
                    ]

                async def list_reports(self, job_id, *, created_after=None):
                    return [
                        {
                            "id": "old-report",
                            "startTime": "2026-08-14T00:00:00Z",
                            "endTime": "2026-08-15T00:00:00Z",
                            "createTime": "2026-08-16T00:00:00Z",
                            "downloadUrl": "https://example.invalid/old",
                        },
                        {
                            "id": "new-report",
                            "startTime": "2026-08-14T00:00:00Z",
                            "endTime": "2026-08-15T00:00:00Z",
                            "createTime": "2026-08-17T00:00:00Z",
                            "downloadUrl": "https://example.invalid/new",
                        },
                    ]

                async def download_report(self, url):
                    return (FIXTURES / "reach_basic.csv").read_text(), "fixture-sha256"

            result = await ReportingSyncCoordinator(
                FakeReportingService(), repository
            ).sync_existing_reach_reports(
                [
                    {"id": "video-a", "title": "A", "published_at": "2026-08-01"},
                    {"id": "video-b", "title": "B", "published_at": "2026-08-02"},
                ],
                collected_at="2026-08-17T00:00:00Z",
            )
            self.assertEqual(result["imported"], 1)
            with closing(connect()) as connection:
                statuses = dict(
                    connection.execute(
                        "SELECT report_id, status FROM youtube_reporting_files"
                    ).fetchall()
                )
                reach_count = connection.execute(
                    "SELECT COUNT(*) FROM video_reach_metrics"
                ).fetchone()[0]
            self.assertEqual(statuses["old-report"], "superseded")
            self.assertEqual(statuses["new-report"], "imported")
            self.assertEqual(reach_count, 2)


if __name__ == "__main__":
    unittest.main()
