import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from strategy_brain import BrainResult, StrategyBrain
from strategy_brain.context_builder import StrategyIntent
from video_feedback_service import VideoFeedbackService
from worksheet_ai_service import WORKSHEET_SCHEMA, WorksheetAIService


def _worksheet_payload():
    return {key: f"{key} 결과" for key in WORKSHEET_SCHEMA["required"]}


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


if __name__ == "__main__":
    unittest.main()
