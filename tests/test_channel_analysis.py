import asyncio
import inspect
import unittest
from types import SimpleNamespace

from channel_analysis import (
    CHANNEL_ANALYSIS_SCHEMA,
    analyze_channel_with_fallback,
    build_channel_prompt,
    fetch_retention_sample,
    select_retention_videos,
)


def video(index: int, views: int) -> dict:
    return {
        "id": f"video-{index}",
        "title": f"영상 {index}",
        "view_count": views,
        "published_at": f"2026-08-{index + 1:02d}",
        "publish_day": "월",
        "publish_hour": index,
        "duration_sec": 600,
        "avg_view_percentage": None,
        "avg_view_percentage_status": "unavailable",
        "watch_minutes": None,
        "watch_minutes_status": "unavailable",
        "ctr": None,
        "ctr_status": "unavailable",
    }


class RetentionSelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_selection_mixes_top_and_recent_without_duplicates(self):
        videos = [video(index, views) for index, views in enumerate([5, 4, 100, 80, 3, 2])]
        selected = select_retention_videos(videos, limit=4)
        self.assertEqual([row["id"] for row in selected], ["video-2", "video-3", "video-0", "video-1"])

    async def test_retention_queries_are_bounded_and_partial(self):
        class FakeAnalytics:
            active = 0
            max_active = 0

            async def get_video_retention(self, video_id, *, start_date, end_date):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                await asyncio.sleep(0.01)
                self.active -= 1
                if video_id == "video-3":
                    raise RuntimeError("one unavailable video")
                return {
                    "video_id": video_id,
                    "status": "available",
                    "data_through": end_date,
                    "points": [
                        {"elapsed_video_time_ratio": 0.0, "audience_watch_ratio": 1.0},
                        {"elapsed_video_time_ratio": 0.1, "audience_watch_ratio": 0.6},
                    ],
                    "source": "youtube_analytics_api_v2",
                }

        analytics = FakeAnalytics()
        rows, failures = await fetch_retention_sample(
            analytics,
            [video(index, 100 - index) for index in range(6)],
            period_start="2020-01-01",
            period_end="2026-08-18",
            limit=6,
            concurrency=2,
        )
        self.assertEqual(len(rows), 5)
        self.assertEqual(failures, 1)
        self.assertLessEqual(analytics.max_active, 2)
        self.assertTrue(any(row["retention_30s_estimate"] is not None for row in rows))


class ChannelAIOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = {
            "title": "테스트 채널",
            "subscriber_count": 1000,
            "video_count": 10,
            "view_count": 100000,
        }
        self.videos = [video(0, 100), video(1, 10)]

    async def test_gpt_56_sol_structured_path_has_no_tool_loop(self):
        report = {key: [] for key in CHANNEL_ANALYSIS_SCHEMA["required"]}
        report.update(
            channel_summary="요약",
            optimal_video_length="10분",
            growth_bottleneck="병목",
            next_video_strategy="전략",
        )
        captured = {}

        class FakeProvider:
            async def generate(self, request):
                captured["request"] = request
                return SimpleNamespace(parsed=report, text="")

        def provider_factory(settings):
            captured["settings"] = settings
            return FakeProvider()

        result, provider, error = await analyze_channel_with_fallback(
            self.channel,
            self.videos,
            [],
            provider_factory=provider_factory,
            fallback_factory=lambda: self.fail("fallback must not run"),
        )
        self.assertIs(result, report)
        self.assertEqual(provider, "gpt-5.6-sol")
        self.assertIsNone(error)
        self.assertEqual(captured["settings"].openai_model, "gpt-5.6-sol")
        self.assertEqual(captured["request"].tools, [])
        self.assertEqual(captured["request"].reasoning_effort, "low")
        self.assertTrue(captured["request"].output_schema["additionalProperties"] is False)

    async def test_claude_fallback_is_preserved_and_bounded(self):
        class BrokenProvider:
            async def generate(self, request):
                raise RuntimeError("OpenAI unavailable")

        class FakeClaude:
            async def analyze_channel(self, channel, videos):
                return {"channel_summary": "fallback"}

        result, provider, error = await analyze_channel_with_fallback(
            self.channel,
            self.videos,
            [],
            provider_factory=lambda settings: BrokenProvider(),
            fallback_factory=FakeClaude,
        )
        self.assertEqual(result["channel_summary"], "fallback")
        self.assertEqual(provider, "claude-opus-5-fallback")
        self.assertEqual(error, "RuntimeError")

    def test_prompt_marks_missing_metrics_as_unavailable_not_zero(self):
        prompt = build_channel_prompt(self.channel, self.videos, [])
        self.assertIn("평균시청률 unavailable", prompt)
        self.assertIn("unavailable/pending/not_reported는 0이 아니다", prompt)
        self.assertIn("Reporting", prompt)

    def test_channel_button_only_reads_cached_reporting_data(self):
        from main import channel_analyze

        source = inspect.getsource(channel_analyze)
        self.assertIn("get_reach_for_videos", source)
        self.assertNotIn("ensure_reach_job", source)
        self.assertNotIn("download_report", source)


if __name__ == "__main__":
    unittest.main()
