import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from strategy_brain import BrainResult, StrategyBrain
from video_feedback_jobs import VideoFeedbackJobManager, VideoFeedbackJobStore
from video_feedback_report import (
    MarkdownFeedbackResult,
    REPORT_SCHEMA,
    generate_markdown_feedback,
)
from video_feedback_service import TranscriptionResult
from youtube_strategy_context import StrategyDataContext


def report_payload():
    return {
        "overall": "현장 증거가 강합니다.",
        "biggest_problem": "도입이 늦습니다.",
        "strongest_scene": "03:40 세척 동선",
        "intro_feedback": "결과를 먼저 보여주세요.",
        "retention_feedback": "실제 retention 없음을 표시합니다.",
        "conversion_feedback": "결과 뒤 CTA를 배치합니다.",
        "visual_feedback": "안정된 현장 화면을 유지합니다.",
        "speech_structure_feedback": "반복 설명을 줄입니다.",
        "timecode_feedback": ["00:00 도입 압축"],
        "must_keep": ["03:40 현장 증거"],
        "safe_to_reduce": ["01:00 반복"],
        "dangerous_to_delete": ["무음 작업 장면"],
        "title_candidates": ["좁은 주방 동선"],
        "thumbnail_copy": ["한 뼘도 안 버렸다"],
        "short_topics": ["배수 설계"],
        "priorities": ["완성 결과 선공개"],
    }


def strategy_context(cache_hit=True):
    summary = {
        "provider": "openai",
        "youtube_analytics_applied": True,
        "youtube_analytics_status": "available",
        "channel_snapshot_sample_size": 1,
        "recent_video_sample_size": 20,
        "retention_sample_size": 0,
        "ctr_available": False,
        "business_pt_applied": True,
        "low_data_applied": True,
        "brand_strategy_applied": True,
        "youtube_cache_hit": cache_hit,
        "applied_sources": ["youtube_analytics_api_v2"],
        "missing_sources": [{"source": "retention", "reason": "video_id_required"}],
    }
    return StrategyDataContext(
        {"channel_snapshot_90d": {"views": 100}, "recent_videos_90d": [], "retrieval_summary": summary},
        summary,
        "compact strategy",
    )


class FakeFeedbackService:
    async def transcribe(self, _path):
        return TranscriptionResult(
            text="배수와 동선을 확인합니다.",
            timed_text="[00:00] 배수와 동선을 확인합니다.",
            provider="openai",
            segments=[{"start": 0, "end": 5, "text": "배수와 동선을 확인합니다."}],
        )


class FakeContextService:
    async def collect(self, **_kwargs):
        return strategy_context()


class CountingProvider:
    def __init__(self):
        self.calls = []

    async def generate(self, request, _tool_executor=None):
        self.calls.append(request)
        return BrainResult(text="{}", parsed=report_payload(), response_id="one-call")


class VideoFeedbackJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = Path(self.temp.name) / "jobs.db"

        def connect():
            connection = sqlite3.connect(db_path, timeout=5)
            connection.row_factory = sqlite3.Row
            return connection

        self.store = VideoFeedbackJobStore(connect)
        self.history = []

    def tearDown(self):
        self.temp.cleanup()

    async def test_job_completes_with_one_gpt_call_cache_and_no_render(self):
        report_calls = []

        async def report_generator(source, **kwargs):
            report_calls.append((source, kwargs))
            return MarkdownFeedbackResult(
                markdown="# feedback",
                retrieval_summary=strategy_context().retrieval_summary,
                provider="openai",
                feedback=report_payload(),
            )

        manager = VideoFeedbackJobManager(
            store=self.store,
            root=Path(self.temp.name) / "media",
            feedback_service_factory=FakeFeedbackService,
            report_generator=report_generator,
            history_writer=lambda kind, keyword, result: self.history.append((kind, keyword, result)) or 7,
        )
        job, source = manager.create_upload(filename="sample.mp4", topic="주방 동선")
        source.write_bytes(b"video")
        manager.finish_upload(job["job_id"])
        frame_summary = {
            "status": "available",
            "candidate_frames_count": 60,
            "selected_frames_count": 30,
            "images_sent_to_gpt": 0,
            "frames": [{"timecode": "00:10", "summary": "배수 장면", "selection_score": 0.9}],
        }
        with (
            patch("video_feedback_jobs.probe_video", return_value={"duration_seconds": 300, "width": 1280, "height": 720}),
            patch("video_feedback_jobs.extract_audio", side_effect=lambda _src, dst: Path(dst).write_bytes(b"audio")),
            patch("video_feedback_jobs.select_representative_frames", return_value=frame_summary),
            patch("video_feedback_jobs.YouTubeStrategyContextService", return_value=FakeContextService()),
        ):
            self.assertTrue(await manager.process_once())

        completed = self.store.get(job["job_id"])
        self.assertEqual(completed["status"], "done")
        self.assertEqual(completed["gpt_call_count"], 1)
        self.assertEqual(completed["selected_frames_count"], 30)
        self.assertTrue(completed["youtube_cache_used"])
        metadata = completed["result"]["analysis_metadata"]
        self.assertEqual(metadata["images_sent_to_gpt"], 0)
        self.assertFalse(metadata["rendering_executed"])
        self.assertEqual(len(report_calls), 1)
        self.assertEqual(len(self.history), 1)
        self.assertFalse(source.exists())

    async def test_gpt_failure_saves_partial_feedback(self):
        async def failed_report(*_args, **_kwargs):
            raise RuntimeError("technical provider detail")

        manager = VideoFeedbackJobManager(
            store=self.store,
            root=Path(self.temp.name) / "media",
            feedback_service_factory=FakeFeedbackService,
            report_generator=failed_report,
            history_writer=lambda *_args: 9,
        )
        job, source = manager.create_upload(filename="sample.mp4", topic="주방")
        source.write_bytes(b"video")
        manager.finish_upload(job["job_id"])
        with (
            patch("video_feedback_jobs.probe_video", return_value={"duration_seconds": 300}),
            patch("video_feedback_jobs.extract_audio", side_effect=lambda _src, dst: Path(dst).write_bytes(b"audio")),
            patch("video_feedback_jobs.select_representative_frames", return_value={"selected_frames_count": 1, "frames": []}),
            patch("video_feedback_jobs.YouTubeStrategyContextService", return_value=FakeContextService()),
        ):
            await manager.process_once()
        completed = self.store.get(job["job_id"])
        self.assertEqual(completed["status"], "partial")
        self.assertEqual(completed["failed_reason"], "openai_feedback_failed")
        self.assertNotIn("technical provider detail", completed["progress_message"])
        self.assertTrue(completed["result"]["markdown"])

    async def test_compact_report_uses_exactly_one_brain_call_and_no_tools(self):
        provider = CountingProvider()
        result = await generate_markdown_feedback(
            {
                "media": {"duration_seconds": 300},
                "compact_transcript": {"window_summaries": [{"range": "00:00-00:30", "summary": "배수"}]},
                "selected_frame_summary": {"selected_frames_count": 1, "frames": [{"summary": "00:10 배수"}]},
            },
            topic="주방 동선",
            brain_factory=lambda registry: StrategyBrain(provider, registry),
            strategy_context=strategy_context(),
        )
        self.assertEqual(result.provider, "openai")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0].tools, [])
        self.assertLess(len(str(provider.calls[0].input)), 30000)


if __name__ == "__main__":
    unittest.main()
