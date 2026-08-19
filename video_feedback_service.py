"""Internal OpenAI-first pipeline for the legacy video-feedback screen.

CNMAKER is isolated here as an optional transcription fallback. Its absence or
failure is never exposed as a provider-specific message to the UI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

import httpx

from strategy_brain import BrainSettings, StrategyBrain, StrategyMode
from strategy_brain.context_builder import format_prefetched_evidence, prefetch_strategy_evidence
from strategy_brain.providers import OpenAIResponsesProvider
from strategy_brain.retrieval import build_strategy_tool_registry
from youtube_strategy_context import (
    YouTubeStrategyContextService,
    format_strategy_data_context,
)


VIDEO_FEEDBACK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overall_score": {"type": "integer"},
        "hook_analysis": {
            "type": "object",
            "properties": {
                "score": {"type": "integer"}, "first_30s": {"type": "string"},
                "hook_strength": {"type": "string"}, "improvement": {"type": "string"},
            },
            "required": ["score", "first_30s", "hook_strength", "improvement"],
            "additionalProperties": False,
        },
        "content_flow": {
            "type": "object",
            "properties": {
                "score": {"type": "integer"}, "summary": {"type": "string"},
                "key_message": {"type": "string"}, "pacing": {"type": "string"},
            },
            "required": ["score", "summary", "key_message", "pacing"],
            "additionalProperties": False,
        },
        "edit_guide": {
            "type": "object",
            "properties": {
                "cuts": {"type": "array", "items": {"type": "object", "properties": {"time": {"type": "string"}, "why": {"type": "string"}}, "required": ["time", "why"], "additionalProperties": False}},
                "emphasis": {"type": "array", "items": {"type": "object", "properties": {"time": {"type": "string"}, "how": {"type": "string"}}, "required": ["time", "how"], "additionalProperties": False}},
                "pacing_fix": {"type": "string"}, "knowledge_applied": {"type": "string"},
            },
            "required": ["cuts", "emphasis", "pacing_fix", "knowledge_applied"],
            "additionalProperties": False,
        },
        "ctr_prediction": {
            "type": "object",
            "properties": {"score": {"type": "integer"}, "analysis": {"type": "string"}, "title_suggestion": {"type": "array", "items": {"type": "string"}}},
            "required": ["score", "analysis", "title_suggestion"], "additionalProperties": False,
        },
        "retention_risk": {
            "type": "object",
            "properties": {"score": {"type": "integer"}, "weak_points": {"type": "array", "items": {"type": "string"}}, "suggestion": {"type": "string"}},
            "required": ["score", "weak_points", "suggestion"], "additionalProperties": False,
        },
        "strengths": {"type": "array", "items": {"type": "string"}},
        "improvements": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overall_score", "hook_analysis", "content_flow", "edit_guide", "ctr_prediction", "retention_risk", "strengths", "improvements"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    timed_text: str
    provider: str


@dataclass(frozen=True)
class VideoFeedbackResult:
    feedback: dict[str, Any]
    provider: str
    retrieval_trace: list[dict[str, Any]]
    retrieval_summary: dict[str, Any]


def _mmss(seconds: Any) -> str:
    value = int(float(seconds or 0))
    return f"{value // 60}:{value % 60:02d}"


def _normalise_transcription(payload: Any, provider: str) -> TranscriptionResult:
    if hasattr(payload, "model_dump"):
        payload = payload.model_dump()
    if not isinstance(payload, dict):
        raise ValueError("invalid transcription payload")
    text = str(payload.get("text") or "").strip()
    segments = payload.get("segments") or []
    timed = "\n".join(
        f"[{_mmss(segment.get('start'))}] {str(segment.get('text') or '').strip()}"
        for segment in segments
        if str(segment.get("text") or "").strip()
    )
    if not text and timed:
        text = " ".join(line.split("] ", 1)[-1] for line in timed.splitlines())
    if not text:
        raise ValueError("empty transcription")
    return TranscriptionResult(text=text, timed_text=timed or text, provider=provider)


class VideoFeedbackService:
    """Use internal OpenAI transcription + business review, with optional fallbacks."""

    def __init__(
        self,
        *,
        openai_client: Any = None,
        http_client_factory: Callable[..., Any] | None = None,
        brain_factory: Callable[[Any], StrategyBrain] | None = None,
        legacy_factory: Callable[[], Any] | None = None,
        strategy_context_service: Any | None = None,
    ) -> None:
        self._openai_client = openai_client
        self._http_client_factory = http_client_factory or httpx.AsyncClient
        self._brain_factory = brain_factory
        self._legacy_factory = legacy_factory
        self._strategy_context_service = (
            strategy_context_service or YouTubeStrategyContextService()
        )

    async def transcribe(self, audio_path: str | Path) -> TranscriptionResult:
        direct_error: Exception | None = None
        if os.getenv("OPENAI_API_KEY", "").strip() or self._openai_client is not None:
            try:
                client = self._openai_client
                if client is None:
                    from openai import AsyncOpenAI
                    client = AsyncOpenAI(timeout=600, max_retries=1)
                with open(audio_path, "rb") as source:
                    payload = await client.audio.transcriptions.create(
                        model=os.getenv("OPENAI_TRANSCRIBE_MODEL", "whisper-1"),
                        file=source,
                        language="ko",
                        response_format="verbose_json",
                        timestamp_granularities=["segment"],
                    )
                return _normalise_transcription(payload, "openai")
            except Exception as exc:
                direct_error = exc

        base = os.getenv("CNMAKER_BASE", "").strip().rstrip("/")
        secret = os.getenv("CNMAKER_SECRET", "").strip()
        if base and secret:
            try:
                with open(audio_path, "rb") as source:
                    content = source.read()
                async with self._http_client_factory(timeout=600) as client:
                    response = await client.post(
                        f"{base}/transcribe",
                        headers={"x-secret": secret, "Content-Type": "application/octet-stream"},
                        content=content,
                    )
                if response.status_code != 200:
                    raise RuntimeError("external transcription failed")
                return _normalise_transcription(response.json(), "cnmaker_optional_fallback")
            except Exception:
                pass

        raise RuntimeError(
            "영상 음성을 분석하지 못했습니다. 잠시 후 다시 시도해 주세요."
        ) from direct_error

    async def analyze(
        self,
        timed_transcript: str,
        *,
        topic: str = "",
        knowledge: list[dict[str, Any]] | None = None,
    ) -> VideoFeedbackResult:
        registry = build_strategy_tool_registry()
        query = (
            f"{topic or '업로드 영상'} 영상 피드백. 부자주방 브랜드 신뢰, 첫 30초 retention, "
            "과거 유사 영상, 비즈니스PT 지식, 제목 썸네일 약속"
        )
        strategy_context = await self._strategy_context_service.collect()
        live_context = format_strategy_data_context(strategy_context)
        try:
            intent, prefetched = await prefetch_strategy_evidence(query, [], registry)
            evidence_context = format_prefetched_evidence(intent, prefetched)
            if self._brain_factory:
                brain = self._brain_factory(registry)
            else:
                configured = BrainSettings.from_env()
                settings = BrainSettings(
                    provider="openai", fallback_provider=configured.fallback_provider,
                    openai_model=configured.openai_model, reasoning_effort="high",
                    store_responses=configured.store_responses,
                    max_tool_rounds=configured.max_tool_rounds,
                )
                brain = StrategyBrain(OpenAIResponsesProvider(settings), registry)
            supplied = [
                {"title": row.get("title"), "category": row.get("category"), "summary": row.get("summary"), "content": str(row.get("content") or "")[:2200]}
                for row in (knowledge or [])[:10]
            ]
            task = f"""영상 주제: {topic or '(미입력)'}

[타임코드 자막]
{timed_transcript[:100000]}

[화면에서 선택된 저장 지식]
{json.dumps(supplied, ensure_ascii=False, default=str)}

{evidence_context}

{live_context}

이 화면은 기존 영상 피드백 탭이므로 현재 제공된 자막과 확인된 채널 근거만 사용하세요.
없는 화면을 봤다고 주장하지 말고, 화면 확인이 필요한 판단은 명시하세요.
일반적인 조언보다 실제 부자주방 retention, 유사 영상 성과, CTR/제목 약속,
비즈니스PT 및 브랜드 전략을 우선해 타임코드별 실행 피드백을 작성하세요.
cuts는 삭제 확정이 아니라 검토할 축약 후보로 표현하고, title_suggestion은 최대 3개로 제한하세요."""
            request = brain.build_request(
                StrategyMode.VIDEO_FEEDBACK,
                task,
                "부자주방의 현장형 주방 솔루션 브랜드 관점에서 사업 신뢰와 시청 지속을 함께 평가한다. "
                "근거가 없으면 없다고 밝히며 기존 영상 피드백 화면 JSON 계약을 지킨다.",
                output_schema=VIDEO_FEEDBACK_SCHEMA,
                output_schema_name="bujajubang_video_feedback",
                metadata={"surface": "video_feedback", "topic": topic[:80]},
            )
            request = replace(request, tools=[tool for tool in request.tools if tool.get("name") not in prefetched])
            response = await brain.run(request)
            if not isinstance(response.parsed, dict):
                raise RuntimeError("invalid structured feedback")
            return VideoFeedbackResult(
                response.parsed,
                "openai",
                list(registry.trace),
                strategy_context.retrieval_summary,
            )
        except Exception:
            if self._legacy_factory is None:
                raise RuntimeError("AI 영상 피드백 생성에 실패했습니다. 잠시 후 다시 시도해 주세요.")
            legacy = self._legacy_factory()
            feedback = await legacy.analyze_video_feedback(timed_transcript, knowledge or None)
            summary = dict(strategy_context.retrieval_summary)
            summary["provider"] = "anthropic_legacy_fallback"
            return VideoFeedbackResult(
                feedback,
                "anthropic_legacy_fallback",
                list(registry.trace),
                summary,
            )
