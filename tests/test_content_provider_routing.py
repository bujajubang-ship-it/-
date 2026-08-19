import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from strategy_brain import BrainResult, StrategyBrain
from strategy_brain.context_builder import StrategyIntent
from video_feedback_service import VideoFeedbackService
from worksheet_ai_service import WORKSHEET_SCHEMA, WorksheetAIService
from youtube_strategy_context import StrategyDataContext


def _worksheet_payload():
    return {key: f"{key} 결과" for key in WORKSHEET_SCHEMA["required"]}


def _video_feedback_payload():
    return {
        "overall_score": 82,
        "hook_analysis": {"score": 80, "first_30s": "현장", "hook_strength": "강함", "improvement": "결과 선공개"},
        "content_flow": {"score": 78, "summary": "문제 해결", "key_message": "동선", "pacing": "중간 잡담 축약"},
        "edit_guide": {"cuts": [], "emphasis": [], "pacing_fix": "단락 유지", "knowledge_applied": "고객 문제 우선"},
        "ctr_prediction": {"score": 75, "analysis": "CTR 데이터 없음", "title_suggestion": ["좁은 주방"]},
        "retention_risk": {"score": 70, "weak_points": ["도입"], "suggestion": "완성 결과 선공개"},
        "strengths": ["현장 증거"],
        "improvements": ["CTA 뒤로"],
    }


class FakeStrategyContextService:
    async def collect(self):
        summary = {
            "provider": "openai",
            "youtube_analytics_applied": True,
            "channel_snapshot_sample_size": 1,
            "recent_video_sample_size": 20,
            "retention_sample_size": 0,
            "ctr_available": False,
            "business_pt_applied": True,
            "low_data_applied": True,
            "brand_strategy_applied": True,
            "applied_sources": ["youtube_analytics_api_v2"],
            "missing_sources": [{"source": "retention", "reason": "video_id_required"}],
        }
        return StrategyDataContext(
            {"channel_snapshot_90d": {"views": 1000}, "retrieval_summary": summary},
            summary,
            "고객 문제 → 해결 → 현장 증거 → CTA",
        )


class CapturingProvider:
    def __init__(self, parsed):
        self.parsed = parsed
        self.requests = []

    async def generate(self, request, _tool_executor=None):
        self.requests.append(request)
        return BrainResult(text="ok", parsed=self.parsed, response_id="test-response")


class FakeTranscriptions:
    async def create(self, **_kwargs):
        return {
            "text": "초음파세척기 설치 전 배수와 동선을 확인합니다.",
            "segments": [
                {"start": 0, "end": 4, "text": "설치 전 배수와 동선을 확인합니다."}
            ],
        }


class ContentProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_worksheet_uses_openai_brain_without_cnmaker(self):
        provider = CapturingProvider(_worksheet_payload())
        service = WorksheetAIService(
            brain_factory=lambda registry: StrategyBrain(provider, registry),
            legacy_factory=lambda: self.fail("legacy provider must not be called"),
        )
        intent = StrategyIntent("topic_plan", "기획", "초음파세척기")
        with (
            patch.dict(
                os.environ,
                {"CNMAKER_BASE": "", "CNMAKER_SECRET": ""},
                clear=False,
            ),
            patch(
                "worksheet_ai_service.prefetch_strategy_evidence",
                new=AsyncMock(return_value=(intent, {})),
            ),
        ):
            result = await service.generate(
                "초음파세척기",
                ref_videos=[{"script": "인건비와 배수 위치를 먼저 확인한다."}],
                knowledge=[
                    {
                        "category": "비즈니스PT",
                        "title": "고객 문제 우선",
                        "content": "제품보다 고객 손실을 먼저 보여준다.",
                    },
                    {
                        "category": "low data",
                        "title": "작은 표본은 가설",
                        "content": "없는 수치는 만들지 않는다.",
                    },
                ],
            )

        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.retrieval_summary["provider"], "openai")
        self.assertTrue(result.retrieval_summary["business_pt_applied"])
        self.assertTrue(result.retrieval_summary["low_data_applied"])
        self.assertTrue(result.retrieval_summary["brand_strategy_applied"])
        self.assertEqual(set(result.data), set(WORKSHEET_SCHEMA["required"]))
        request_text = str(provider.requests[0].input)
        self.assertIn("인건비와 배수", request_text)
        self.assertIn("비즈니스PT", request_text)
        self.assertIn("low data", request_text)
        self.assertEqual(provider.requests[0].reasoning_effort, "low")

    async def test_video_transcription_prefers_openai_without_cnmaker(self):
        client = SimpleNamespace(
            audio=SimpleNamespace(transcriptions=FakeTranscriptions())
        )
        service = VideoFeedbackService(openai_client=client)
        with tempfile.NamedTemporaryFile(suffix=".mp3") as audio:
            audio.write(b"test-audio")
            audio.flush()
            with patch.dict(
                os.environ,
                {"CNMAKER_BASE": "", "CNMAKER_SECRET": ""},
                clear=False,
            ):
                result = await service.transcribe(audio.name)

        self.assertEqual(result.provider, "openai")
        self.assertIn("[0:00]", result.timed_text)

    async def test_video_feedback_uses_youtube_and_brand_context(self):
        provider = CapturingProvider(_video_feedback_payload())
        service = VideoFeedbackService(
            brain_factory=lambda registry: StrategyBrain(provider, registry),
            strategy_context_service=FakeStrategyContextService(),
            legacy_factory=lambda: self.fail("legacy provider must not be called"),
        )
        intent = StrategyIntent("general", "피드백", "이태원 주방")
        with patch(
            "video_feedback_service.prefetch_strategy_evidence",
            new=AsyncMock(return_value=(intent, {})),
        ):
            result = await service.analyze(
                "[0:00] 좁은 주방의 배수와 동선을 설명합니다.",
                topic="이태원 갈빗집",
            )

        self.assertEqual(result.provider, "openai")
        self.assertTrue(result.retrieval_summary["youtube_analytics_applied"])
        request_text = str(provider.requests[0].input)
        self.assertIn("youtube_analytics_api_v2", request_text)
        self.assertIn("고객 문제", request_text)


if __name__ == "__main__":
    unittest.main()
