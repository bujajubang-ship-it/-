import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import httpx

from analytics_reporting import REACH_REPORT_TYPE_ID, YouTubeReportingService
from analytics_repository import AnalyticsRepository
from analytics_service import ANALYTICS_METRICS, ANALYTICS_SOURCE, STATUS_AVAILABLE, metric_value
from analytics_sync import AnalyticsSyncCoordinator, ReportingSyncCoordinator
from strategy_brain.chat_service import StrategyChatService
from strategy_brain.config import BrainSettings
from strategy_brain.contracts import BrainRequest, StrategyMode
from strategy_brain.context_builder import prefetch_strategy_evidence
from strategy_brain.retrieval import StrategyRetrieval, build_strategy_tool_registry
from strategy_memory import StrategyMemoryRepository
from strategy_repository import StrategyRepository


def _connect_factory(path: Path):
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    return connect


def _core_schema(connection):
    connection.executescript(
        """
        CREATE TABLE history (id INTEGER PRIMARY KEY, type TEXT, keyword TEXT, report TEXT, created_at TEXT);
        CREATE TABLE pipeline (id INTEGER PRIMARY KEY, title TEXT, stage TEXT, content_type TEXT, editor TEXT, planned_date TEXT, notes TEXT, sort_order INTEGER, created_at TEXT, updated_at TEXT);
        CREATE TABLE worksheet_rows (id INTEGER PRIMARY KEY, sort_order INTEGER, data TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE chat_session (id INTEGER PRIMARY KEY, title TEXT, messages TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE knowledge (id INTEGER PRIMARY KEY, title TEXT, category TEXT, summary TEXT, content TEXT, active INTEGER, created_at TEXT);
        """
    )
    connection.commit()


class ReportingClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_creation_is_idempotent(self):
        posts = 0

        async def handler(request):
            nonlocal posts
            self.assertEqual(request.headers.get("authorization"), "Bearer test-token")
            if request.method == "GET":
                return httpx.Response(200, json={"jobs": []})
            posts += 1
            return httpx.Response(
                200,
                json={
                    "id": "reach-job",
                    "name": "bujajubang-reach",
                    "reportTypeId": REACH_REPORT_TYPE_ID,
                    "createTime": "2026-08-18T00:00:00Z",
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = YouTubeReportingService(lambda: asyncio.sleep(0, result="test-token"), http=client)
        try:
            created = await service.ensure_reach_job()
        finally:
            await client.aclose()
        self.assertEqual(created["reportTypeId"], REACH_REPORT_TYPE_ID)
        self.assertEqual(posts, 1)

    async def test_existing_job_is_reused_without_post(self):
        async def handler(request):
            self.assertEqual(request.method, "GET")
            return httpx.Response(
                200,
                json={"jobs": [{"id": "existing", "reportTypeId": REACH_REPORT_TYPE_ID}]},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = YouTubeReportingService(lambda: asyncio.sleep(0, result="test-token"), http=client)
        try:
            job = await service.ensure_reach_job()
        finally:
            await client.aclose()
        self.assertEqual(job["id"], "existing")

    async def test_report_download_returns_utf8_and_digest(self):
        csv_text = "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n2026-08-18,c,v,10,2.5\n"

        async def handler(request):
            return httpx.Response(200, content=csv_text.encode())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        service = YouTubeReportingService(lambda: asyncio.sleep(0, result="test-token"), http=client)
        try:
            downloaded, digest = await service.download_report("https://example.invalid/report")
        finally:
            await client.aclose()
        self.assertEqual(downloaded, csv_text)
        self.assertEqual(len(digest), 64)


class ReportingPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "reporting.db"
        self.connect = _connect_factory(self.path)
        with closing(self.connect()) as connection:
            _core_schema(connection)
        self.repository = AnalyticsRepository(self.connect)
        self.repository.init_schema()
        self.videos = [
            {"id": "video-a", "title": "9평 주방 절대 이렇게 하지 마세요", "published_at": "2026-08-14", "duration_seconds": 300},
            {"id": "video-b", "title": "냉장고 3가지 비교", "published_at": "2026-08-14", "duration_seconds": 300},
        ]

    def tearDown(self):
        self.temp.cleanup()

    async def test_no_report_and_report_delay_are_nonfatal(self):
        class Delayed:
            async def list_jobs(self):
                return [{"id": "job", "reportTypeId": REACH_REPORT_TYPE_ID}]

            async def list_reports(self, job_id, *, created_after=None):
                return []

        result = await ReportingSyncCoordinator(Delayed(), self.repository).sync_existing_reach_reports(self.videos)
        self.assertEqual(result, {"jobs": 1, "imported": 0, "skipped": 0, "errors": 0})
        self.assertEqual(self.repository.get_reporting_status()["reach"]["video_count"], 0)

    async def test_duplicate_import_is_idempotent_and_tracks_generated_time(self):
        csv_text = (
            "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
            "2026-08-14,c,video-a,1000,8.0\n2026-08-14,c,video-b,2000,3.0\n"
        )

        class Service:
            async def list_jobs(self):
                return [{"id": "job", "reportTypeId": REACH_REPORT_TYPE_ID}]

            async def list_reports(self, job_id, *, created_after=None):
                return [{"id": "report", "startTime": "2026-08-14T00:00:00Z", "endTime": "2026-08-15T00:00:00Z", "createTime": "2026-08-16T02:00:00Z", "downloadUrl": "https://example.invalid/report"}]

            async def download_report(self, url):
                return csv_text, "digest"

        coordinator = ReportingSyncCoordinator(Service(), self.repository)
        first = await coordinator.sync_existing_reach_reports(self.videos, collected_at="2026-08-16T03:00:00Z")
        second = await coordinator.sync_existing_reach_reports(self.videos, collected_at="2026-08-16T04:00:00Z")
        self.assertEqual(first["imported"], 1)
        self.assertEqual(second["skipped"], 1)
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT report_generated_at,source_as_of FROM video_reach_metrics").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[0] == "2026-08-16T02:00:00Z" for row in rows))
        self.assertTrue(all(row[1] == "2026-08-14" for row in rows))

    async def test_malformed_new_report_keeps_previous_snapshot(self):
        self.repository.upsert_videos(self.videos, collected_at="2026-08-16T00:00:00Z")
        run = self.repository.begin_sync_run("reach", "youtube_reporting_api")
        AnalyticsSyncCoordinator(None, self.repository).import_reach_csv(
            "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n2026-08-14,c,video-a,1000,8.0\n",
            self.videos,
            report_id="good",
            report_date="2026-08-14",
            collected_at="2026-08-16T00:00:00Z",
        )

        class Malformed:
            async def list_jobs(self):
                return [{"id": "job", "reportTypeId": REACH_REPORT_TYPE_ID}]

            async def list_reports(self, job_id, *, created_after=None):
                return [{"id": "bad", "startTime": "2026-08-14T00:00:00Z", "endTime": "2026-08-15T00:00:00Z", "createTime": "2026-08-17T00:00:00Z", "downloadUrl": "bad"}]

            async def download_report(self, url):
                return "date,video_id\n2026-08-14,video-a\n", "bad"

        result = await ReportingSyncCoordinator(Malformed(), self.repository).sync_existing_reach_reports(self.videos)
        self.assertEqual(result["errors"], 1)
        reach = self.repository.get_reach_for_videos(["video-a"])["video-a"]
        self.assertEqual(reach["thumbnail_ctr"]["value"], 8.0)


class CtrStrategyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "strategy.db"
        self.connect = _connect_factory(self.path)
        with closing(self.connect()) as connection:
            _core_schema(connection)
        self.analytics = AnalyticsRepository(self.connect)
        self.analytics.init_schema()
        self.strategies = StrategyRepository(self.connect)
        self.strategies.init_schema()
        StrategyMemoryRepository(self.connect).init_schema()
        videos = [
            {"video_id": "high", "title": "9평 주방 절대 이렇게 하지 마세요", "published_at": "2026-08-14", "duration_seconds": 300},
            {"video_id": "low", "title": "냉장고 3가지 비교", "published_at": "2026-08-14", "duration_seconds": 300},
        ]
        self.analytics.upsert_videos(videos, collected_at="2026-08-18T00:00:00Z")
        run = self.analytics.begin_sync_run("metric", ANALYTICS_SOURCE)
        rows = []
        for video_id, views, avp in (("high", 1000, 30.0), ("low", 5000, 60.0)):
            values = {name: 1 for name in ANALYTICS_METRICS}
            values.update({"views": views, "averageViewPercentage": avp})
            rows.append({
                "video_id": video_id, "period_start": "2026-08-14", "period_end": "2026-08-18", "data_through": "2026-08-17", "source": ANALYTICS_SOURCE, "sample_size": 1,
                "metrics": {name: metric_value(values[name], STATUS_AVAILABLE, source=ANALYTICS_SOURCE, data_through="2026-08-17") for name in ANALYTICS_METRICS},
            })
        self.analytics.save_metric_snapshots(rows, sync_run_id=run, collected_at="2026-08-18T00:00:00Z")
        csv_text = (
            "date,channel_id,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
            "2026-08-14,c,high,1000,10.0\n2026-08-14,c,low,10000,2.0\n"
        )
        AnalyticsSyncCoordinator(None, self.analytics).import_reach_csv(
            csv_text, videos, report_id="reach", report_date="2026-08-14", collected_at="2026-08-18T00:00:00Z"
        )
        self.retrieval = StrategyRetrieval(self.connect, analytics=self.analytics, strategies=self.strategies)

    def tearDown(self):
        self.temp.cleanup()

    async def test_ctr_retrieval_and_ai_routing_use_reporting_trace(self):
        ctr = self.retrieval.get_ctr_performance({"limit": 20})
        self.assertEqual(ctr.sample_size, 2)
        self.assertAlmostEqual(ctr.data["channel_weighted_ctr_percent"], 2.727, places=3)
        matrix = self.retrieval.compare_impression_to_click_performance({})
        self.assertEqual(matrix.data["segments"]["high_impressions_low_ctr"][0]["video_id"], "low")
        registry = build_strategy_tool_registry(self.retrieval)
        intent, evidence = await prefetch_strategy_evidence(
            "CTR은 높은데 초반 이탈이 심한 영상은 어떤 게 있어?", [], registry
        )
        self.assertEqual(intent.name, "ctr_analysis")
        self.assertIn("find_high_ctr_low_retention", evidence)
        self.assertTrue(any("youtube_reporting_api" in str(item.get("source")) for item in registry.trace))

    async def test_ctr_tools_share_one_cached_reach_scan(self):
        calls = 0
        original = self.analytics.get_reach_history

        def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original(*args, **kwargs)

        self.analytics.get_reach_history = counted
        await asyncio.gather(
            asyncio.to_thread(self.retrieval.get_ctr_performance, {"limit": 100}),
            asyncio.to_thread(self.retrieval.compare_title_patterns, {"query": None}),
            asyncio.to_thread(self.retrieval.compare_thumbnail_patterns, {}),
            asyncio.to_thread(self.retrieval.find_high_ctr_low_retention, {}),
            asyncio.to_thread(self.retrieval.compare_impression_to_click_performance, {}),
        )
        self.assertEqual(calls, 1)

    async def test_ctr_chat_uses_low_reasoning_after_deterministic_prefetch(self):
        captured = {}

        class Brain:
            def build_request(self, mode, input_value, instructions, metadata=None):
                return BrainRequest(
                    mode=StrategyMode.STRATEGY_CHAT,
                    instructions=instructions,
                    input=input_value,
                    tools=[{"name": "get_ctr_performance"}],
                    reasoning_effort="medium",
                )

            async def stream(self, request):
                captured["request"] = request
                yield "완료"

        service = StrategyChatService(
            settings=BrainSettings(provider="openai"),
            openai_brain=Brain(),
            tool_registry=build_strategy_tool_registry(self.retrieval),
            enable_prefetch=True,
        )
        from unittest.mock import patch

        with patch.dict("os.environ", {"OPENAI_API_KEY": "configured"}):
            events = [
                event async for event in service.stream_events(
                    "조회수가 높은데 CTR이 낮은 영상 찾아줘.", []
                )
            ]
        self.assertTrue(any(event.get("type") == "trace" for event in events))
        self.assertEqual(captured["request"].reasoning_effort, "low")
        self.assertEqual(captured["request"].tools, [])


if __name__ == "__main__":
    unittest.main()
