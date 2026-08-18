import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from analytics_repository import AnalyticsRepository
from analytics_service import ANALYTICS_METRICS, ANALYTICS_SOURCE, STATUS_AVAILABLE, metric_value
from strategy_brain import BrainSettings, StrategyBrain, StrategyMode
from strategy_brain.chat_service import StrategyChatService, build_openai_input
from strategy_brain.providers import OpenAIResponsesProvider
from strategy_brain.retrieval import StrategyRetrieval, build_strategy_tool_registry
from strategy_repository import StrategyRepository


def create_established_tables(connection):
    connection.executescript(
        """
        CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, keyword TEXT NOT NULL, report TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE pipeline (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'planning', content_type TEXT DEFAULT '미드폼', editor TEXT DEFAULT '', planned_date TEXT DEFAULT '', notes TEXT DEFAULT '', sort_order INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE optimize_videos (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL);
        CREATE TABLE worksheet_rows (id INTEGER PRIMARY KEY AUTOINCREMENT, sort_order INTEGER DEFAULT 0, data TEXT DEFAULT '{}', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE chat_session (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT DEFAULT '', messages TEXT DEFAULT '[]', created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE knowledge (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT DEFAULT '', category TEXT DEFAULT '키컨텐츠', summary TEXT DEFAULT '', content TEXT DEFAULT '', active INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """
    )
    connection.commit()


def analytics_row(video_id="video-a", views=100):
    values = {
        "views": views,
        "likes": 10,
        "comments": 2,
        "shares": 1,
        "subscribersGained": 3,
        "subscribersLost": 1,
        "estimatedMinutesWatched": 500.0,
        "averageViewDuration": 120.0,
        "averageViewPercentage": 55.0,
    }
    return {
        "video_id": video_id,
        "period_start": "2026-08-01",
        "period_end": "2026-08-18",
        "data_through": "2026-08-16",
        "source": ANALYTICS_SOURCE,
        "sample_size": 1,
        "published_at": "2026-08-01",
        "metrics": {
            name: metric_value(values[name], STATUS_AVAILABLE, source=ANALYTICS_SOURCE, data_through="2026-08-16")
            for name in ANALYTICS_METRICS
        },
    }


class RepositoryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "strategy.db"

        def connect():
            return sqlite3.connect(self.path)

        self.connect = connect
        with sqlite3.connect(self.path) as connection:
            create_established_tables(connection)
        self.analytics = AnalyticsRepository(connect)
        self.analytics.init_schema()
        self.strategies = StrategyRepository(connect)
        self.strategies.init_schema()

    def tearDown(self):
        self.temp.cleanup()


class SnapshotIdempotencyTests(RepositoryFixture):
    def test_raw_data_api_video_shape_is_normalized(self):
        self.analytics.upsert_videos(
            [{"id": "raw-video", "title": "원본", "published_at": "2026-08-18", "duration_sec": 321}],
            collected_at="2026-08-18T00:00:00Z",
        )
        with self.connect() as connection:
            row = connection.execute(
                "SELECT video_id, duration_seconds FROM youtube_videos WHERE video_id='raw-video'"
            ).fetchone()
        self.assertEqual(row, ("raw-video", 321))

    def test_duplicate_metric_and_retention_snapshots_are_not_saved(self):
        collected = "2026-08-18T00:00:00Z"
        self.analytics.upsert_videos(
            [{"video_id": "video-a", "title": "냉장고 선택", "published_at": "2026-08-01", "duration_seconds": 240}],
            collected_at=collected,
        )
        first_run = self.analytics.begin_sync_run("metric", ANALYTICS_SOURCE)
        second_run = self.analytics.begin_sync_run("metric", ANALYTICS_SOURCE)
        self.assertEqual(self.analytics.save_metric_snapshots([analytics_row()], sync_run_id=first_run, collected_at=collected), 1)
        delayed_duplicate = analytics_row()
        delayed_duplicate["period_end"] = "2026-08-19"
        self.assertEqual(self.analytics.save_metric_snapshots([delayed_duplicate], sync_run_id=second_run, collected_at="2026-08-19T06:00:00Z"), 0)

        retention = {
            "video_id": "video-a", "period_start": "2026-08-01", "period_end": "2026-08-18",
            "data_through": "2026-08-16", "source": ANALYTICS_SOURCE, "status": "available",
            "points": [{"elapsed_video_time_ratio": i / 99, "audience_watch_ratio": 1 - i / 150, "relative_retention_performance": 0.5} for i in range(100)],
        }
        first_id = self.analytics.save_retention_snapshot(retention, duration_seconds=240, estimate=0.8, estimate_metadata={}, sync_run_id=first_run, collected_at=collected)
        delayed_retention = dict(retention)
        delayed_retention["period_end"] = "2026-08-19"
        second_id = self.analytics.save_retention_snapshot(delayed_retention, duration_seconds=240, estimate=0.8, estimate_metadata={}, sync_run_id=second_run, collected_at="2026-08-19T06:00:00Z")
        self.assertIsInstance(first_id, int)
        self.assertIsNone(second_id)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM video_metric_snapshots").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM video_retention_snapshots").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM video_retention_points").fetchone()[0], 100)

    def test_collection_lease_prevents_duplicate_scheduler_runs(self):
        self.assertTrue(self.analytics.acquire_collection_lease("one", now="2026-08-18T00:00:00Z", lock_until="2026-08-18T02:00:00Z"))
        self.assertFalse(self.analytics.acquire_collection_lease("two", now="2026-08-18T01:00:00Z", lock_until="2026-08-18T03:00:00Z"))
        self.analytics.finish_collection("one", status="success", completed_at="2026-08-18T01:30:00Z", data_through="2026-08-16")
        self.assertTrue(self.analytics.acquire_collection_lease("two", now="2026-08-18T01:31:00Z", lock_until="2026-08-18T03:31:00Z"))


class FeedbackLoopTests(RepositoryFixture):
    def test_strategy_links_to_uploaded_video_and_latest_checkpoint(self):
        collected = "2026-08-18T00:00:00Z"
        self.analytics.upsert_videos(
            [{"video_id": "video-a", "title": "업로드된 영상", "published_at": "2026-08-01", "duration_seconds": 240}],
            collected_at=collected,
        )
        run = self.analytics.begin_sync_run("metric", ANALYTICS_SOURCE)
        row = analytics_row()
        row["snapshot_label"] = "D7"
        self.analytics.save_metric_snapshots([row], sync_run_id=run, collected_at=collected)
        strategy_id = self.strategies.create(
            topic="냉장고 선택",
            content_type="미드폼",
            strategy={"recommended_title": "냉장고, 이것부터 보세요"},
            evidence=[{"source": "youtube_analytics_api_v2"}],
        )

        self.strategies.link_video(
            strategy_id,
            "video-a",
            title_at_upload="실제 업로드 제목",
            thumbnail_text="절대 먼저 사지 마세요",
        )
        self.assertEqual(self.strategies.refresh_performance_checkpoints(), 1)
        linked = self.strategies.get(strategy_id)
        self.assertEqual(linked["videos"][0]["video_id"], "video-a")
        self.assertEqual(linked["checkpoints"][0]["checkpoint_label"], "D7")
        feedback = StrategyRetrieval(
            self.connect, analytics=self.analytics, strategies=self.strategies
        ).search_feedback_history({"query": "냉장고", "limit": 5})
        outcome = feedback.data["performance_checkpoints"][0]
        self.assertEqual(outcome["planned_title"], "냉장고, 이것부터 보세요")
        self.assertEqual(outcome["title_at_upload"], "실제 업로드 제목")
        self.assertEqual(outcome["views"], 100)
        with self.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT content_id FROM youtube_videos WHERE video_id='video-a'").fetchone()[0],
                str(strategy_id),
            )


class RetrievalTests(RepositoryFixture):
    def setUp(self):
        super().setUp()
        collected = "2026-08-18T00:00:00Z"
        self.analytics.upsert_videos([{"video_id": "video-a", "title": "업소용 냉장고 절대 이렇게 고르지 마세요", "published_at": "2026-08-01", "duration_seconds": 240}], collected_at=collected)
        run = self.analytics.begin_sync_run("metric", ANALYTICS_SOURCE)
        self.analytics.save_metric_snapshots([analytics_row()], sync_run_id=run, collected_at=collected)
        with self.connect() as connection:
            connection.execute("INSERT INTO knowledge(title,category,summary,content) VALUES (?,?,?,?)", ("비즈니스PT 고객언어", "비즈니스PT", "고객 언어로 끝점을 설계", "고객이 지인에게 할 말을 먼저 정한다"))
            connection.execute("INSERT INTO history(type,keyword,report) VALUES (?,?,?)", ("midform", "업소용 냉장고", json.dumps({"titles": ["냉장고 후회"]}, ensure_ascii=False)))
            connection.execute("INSERT INTO worksheet_rows(data) VALUES (?)", (json.dumps({"title": "냉장고 촬영", "shot": "문 열기"}, ensure_ascii=False),))
            connection.execute("INSERT INTO pipeline(title,stage) VALUES (?,?)", ("냉장고 비교", "planning"))
            connection.execute("INSERT INTO chat_session(title,messages) VALUES (?,?)", ("냉장고 방향", json.dumps([{"role": "user", "content": "가격보다 AS"}], ensure_ascii=False)))
            connection.commit()
        self.retrieval = StrategyRetrieval(self.connect, analytics=self.analytics, strategies=self.strategies)

    def test_retrieves_only_matching_sources_with_freshness(self):
        recent = self.retrieval.get_recent_channel_performance({"days": None, "limit": 10})
        self.assertEqual(recent.sample_size, 1)
        self.assertEqual(recent.freshness, "current")
        self.assertEqual(recent.data[0]["video_id"], "video-a")
        knowledge = self.retrieval.search_business_pt_knowledge({"query": "고객 언어", "limit": 5})
        self.assertEqual(knowledge.sample_size, 1)
        plans = self.retrieval.search_previous_plans({"query": "냉장고", "limit": 5})
        self.assertEqual(len(plans.data["legacy_history"]), 1)
        worksheet = self.retrieval.search_previous_worksheets({"query": "냉장고", "limit": 5})
        self.assertEqual(worksheet.sample_size, 1)
        pipeline = self.retrieval.get_content_pipeline({"status": None, "limit": 10})
        self.assertEqual(pipeline.sample_size, 1)

    def test_empty_and_missing_retention_are_explicit(self):
        empty = self.retrieval.get_retention_patterns({"video_id": None, "limit": 10})
        self.assertEqual(empty.data, [])
        self.assertIsNotNone(empty.unavailable_reason)

    def test_optional_empty_chat_table_is_reported_without_failure(self):
        with self.connect() as connection:
            connection.execute("DROP TABLE chat_session")
            connection.commit()
        memory = self.retrieval.search_chat_memory({"query": "냉장고", "limit": 5})
        self.assertEqual(memory.data, [])
        self.assertIsNotNone(memory.unavailable_reason)

    def test_registry_exposes_all_required_read_only_tools(self):
        registry = build_strategy_tool_registry(self.retrieval)
        tools = registry.definitions_for(tuple(registry._tools))
        names = {tool["name"] for tool in tools}
        self.assertEqual(
            names,
            {"get_recent_channel_performance", "get_video_performance", "compare_similar_videos", "get_retention_patterns", "search_knowledge", "search_business_pt_knowledge", "search_previous_plans", "search_previous_worksheets", "get_content_pipeline", "search_feedback_history", "search_chat_memory", "get_recent_trends"},
        )
        self.assertTrue(all(tool["strict"] for tool in tools))


class FakeBrain:
    def __init__(self, *, fail=False):
        self.fail = fail

    def build_request(self, *args, **kwargs):
        return object()

    async def stream(self, request):
        if self.fail:
            raise RuntimeError("openai unavailable")
        yield "gpt-ok"


class FakeLegacy:
    async def chat_stream(self, message, history, attachments, knowledge):
        yield "claude-ok"


class ProviderFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_failure_before_output_falls_back_to_claude(self):
        service = StrategyChatService(settings=BrainSettings(provider="openai"), openai_brain=FakeBrain(fail=True), legacy_factory=FakeLegacy)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test", "ANTHROPIC_API_KEY": "test"}):
            output = [item async for item in service.stream("질문", [], [])]
        self.assertEqual(output, [("anthropic", "claude-ok")])

    async def test_openai_success_does_not_invoke_fallback(self):
        service = StrategyChatService(settings=BrainSettings(provider="openai"), openai_brain=FakeBrain(), legacy_factory=lambda: (_ for _ in ()).throw(AssertionError("fallback called")))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}):
            output = [item async for item in service.stream("질문", [], [])]
        self.assertEqual(output, [("openai", "gpt-ok")])

    def test_multimodal_input_keeps_history_and_attachments(self):
        items = build_openai_input("현재 질문", [{"role": "user", "content": "이전 질문"}, {"role": "assistant", "content": "이전 답"}], [{"media_type": "image/jpeg", "data": "YWJj", "name": "a.jpg"}])
        self.assertEqual(len(items), 3)
        self.assertEqual(items[-1]["content"][0]["type"], "input_image")
        self.assertEqual(items[-1]["content"][-1]["text"], "현재 질문")


class FakeStream:
    def __init__(self, events):
        self.events = events

    def __aiter__(self):
        self.iterator = iter(self.events)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration


class StreamingResponses:
    def __init__(self, streams):
        self.streams = list(streams)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.streams.pop(0))


class GPTToolStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_continues_after_tool_call(self):
        call = SimpleNamespace(type="function_call", name="get_recent_channel_performance", arguments='{"days":null,"limit":5}', call_id="call_1", model_dump=lambda exclude_none=True: {"type": "function_call", "name": "get_recent_channel_performance", "arguments": '{"days":null,"limit":5}', "call_id": "call_1"})
        completed_tool = SimpleNamespace(output=[call])
        completed_text = SimpleNamespace(output=[])
        responses = StreamingResponses([
            [SimpleNamespace(type="response.completed", response=completed_tool)],
            [SimpleNamespace(type="response.output_text.delta", delta="최종 전략"), SimpleNamespace(type="response.completed", response=completed_text)],
        ])
        provider = OpenAIResponsesProvider(settings=BrainSettings(provider="openai"), client=SimpleNamespace(responses=responses))
        # A minimal registry avoids touching a database in this provider contract test.
        from strategy_brain.tools import ReadOnlyToolRegistry, ToolDefinition
        registry = ReadOnlyToolRegistry()
        registry.register(ToolDefinition(name="get_recent_channel_performance", description="recent", parameters={"type": "object", "properties": {"days": {"type": ["integer", "null"]}, "limit": {"type": "integer"}}, "required": ["days", "limit"], "additionalProperties": False}, handler=lambda _: {"videos": []}))
        brain = StrategyBrain(provider, registry)
        request = brain.build_request(StrategyMode.STRATEGY_CHAT, "질문", "도구 사용")
        output = "".join([token async for token in brain.stream(request)])
        self.assertEqual(output, "최종 전략")
        self.assertEqual(len(responses.calls), 2)
        self.assertTrue(any(item.get("type") == "function_call_output" for item in responses.calls[1]["input"]))


if __name__ == "__main__":
    unittest.main()
