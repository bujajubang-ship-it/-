import unittest

from youtube_strategy_context import (
    YouTubeStrategyContextService,
    load_strategy_knowledge,
)


class FakeAnalytics:
    def configuration_status(self):
        return {
            "configured": True,
            "missing": [],
            "analytics_scope_declared": True,
            "youtube_readonly_scope_declared": True,
        }

    async def get_recent_upload_videos(self, *, limit):
        return [
            {
                "video_id": "video-1",
                "title": "좁은 주방 동선",
                "published_at": "2026-08-01",
                "duration_seconds": 300,
            }
        ]

    async def get_channel_snapshot(self, *, start_date, end_date):
        return {
            "columnHeaders": [
                {"name": "views"},
                {"name": "estimatedMinutesWatched"},
                {"name": "averageViewDuration"},
                {"name": "averageViewPercentage"},
                {"name": "likes"},
                {"name": "comments"},
                {"name": "shares"},
                {"name": "subscribersGained"},
            ],
            "rows": [[1000, 5000, 180, 60, 30, 4, 2, 8]],
        }

    async def get_video_analytics(self, **_kwargs):
        return [{"video_id": "video-1", "views": 1000, "sample_size": 1}]

    async def get_video_retention(self, video_id, **_kwargs):
        return {
            "video_id": video_id,
            "status": "available",
            "points": [{"elapsed_video_time_ratio": 0.1, "audience_watch_ratio": 0.8}],
        }


class MissingAnalytics(FakeAnalytics):
    def configuration_status(self):
        return {
            "configured": False,
            "missing": ["refresh_token"],
            "analytics_scope_declared": False,
            "youtube_readonly_scope_declared": False,
        }


class FakeRepository:
    def get_reach_for_videos(self, _video_ids):
        return {
            "video-1": {
                "thumbnail_impressions": 12000,
                "thumbnail_ctr": {"value": 7.2, "status": "available"},
            }
        }


class YouTubeStrategyContextTests(unittest.IsolatedAsyncioTestCase):
    def test_required_strategy_seeds_load(self):
        text, flags, missing = load_strategy_knowledge()
        self.assertFalse(missing)
        self.assertTrue(flags["business_pt"])
        self.assertTrue(flags["low_data"])
        self.assertTrue(flags["brand_strategy"])
        self.assertIn("문제", text)
        self.assertIn("Before/After", text)

    async def test_live_rows_and_cached_ctr_are_reported_without_invention(self):
        service = YouTubeStrategyContextService(
            analytics=FakeAnalytics(), repository=FakeRepository()
        )
        result = await service.collect(video_id="video-1", use_cache=False)
        summary = result.retrieval_summary
        self.assertTrue(summary["youtube_analytics_applied"])
        self.assertEqual(summary["channel_snapshot_sample_size"], 1)
        self.assertEqual(summary["recent_video_sample_size"], 1)
        self.assertEqual(summary["retention_sample_size"], 1)
        self.assertTrue(summary["ctr_available"])
        self.assertTrue(summary["business_pt_applied"])
        self.assertTrue(summary["low_data_applied"])
        self.assertTrue(summary["brand_strategy_applied"])

    async def test_missing_environment_is_setup_required_not_exception(self):
        service = YouTubeStrategyContextService(
            analytics=MissingAnalytics(), repository=FakeRepository()
        )
        result = await service.collect(use_cache=False)
        summary = result.retrieval_summary
        self.assertEqual(summary["youtube_analytics_status"], "setup_required")
        self.assertEqual(summary["recent_video_sample_size"], 0)
        self.assertEqual(
            summary["missing_sources"][-1]["reason"], "missing_env"
        )


if __name__ == "__main__":
    unittest.main()
