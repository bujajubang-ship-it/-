"""Production strategy-chat orchestration with a legacy Claude fallback."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import replace
from typing import Any, AsyncIterator, Callable

from database import list_knowledge

from .brain import StrategyBrain
from .config import BrainSettings
from .contracts import StrategyMode
from .providers import OpenAIResponsesProvider
from .context_builder import format_prefetched_evidence, prefetch_strategy_evidence
from .retrieval import build_strategy_tool_registry
from .tools import ReadOnlyToolRegistry


logger = logging.getLogger(__name__)


CHAT_TASK_INSTRUCTIONS = """사용자의 질문에 직접 답한다.

전략·기획 요청이면 반드시 관련 내부 데이터를 도구로 먼저 확인한다. 답변은 결론을 앞에 두고 다음을 포함한다.
1) 추천 주제와 한 문장 판단
2) 측정된 근거(영상/기간/data_through)와 해석
3) 반대 근거 또는 리스크
4) 타깃 시청자와 왜 지금 해야 하는지
5) 핵심 메시지
6) 제목 후보와 최종 추천 제목
7) 썸네일 문구와 실제 촬영 구도
8) 첫 5~15초 훅
9) 전체 영상 구조
10) 필요한 촬영 컷
11) 촬영 워크시트
12) 업로드 후 확인할 KPI(1일/3일/7일/장기)

짧은 조회 질문에는 필요한 항목만 간결하게 답한다. 사용자가 특정 칸만 요청하면 공통 전략과 충돌 여부를 확인한 뒤 그 칸을 집중적으로 작성한다.

답변 기본 구조:
1. 결론
2. 데이터 근거
3. 내가 놓쳤을 가능성이 있는 점
4. 추천 전략
5. 바로 실행할 다음 행동

서버가 미리 조회한 근거와 추가 tool 결과를 실제 판단에 사용한다. 일반론만으로 답하지 않는다. 비즈니스PT 원칙을 적용했다면 '적용한 지식'과 '왜 적용했는지'를 1~3개만 표시한다.
"""


def _text_message(role: str, text: str) -> dict[str, Any]:
    content_type = "input_text" if role == "user" else "output_text"
    return {"role": role, "content": [{"type": content_type, "text": text}]}


def build_openai_input(
    message: str,
    history: list[dict[str, Any]],
    attachments: list[dict[str, Any]] | None = None,
    evidence_context: str = "",
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    # Recent turns preserve conversational continuity; durable decisions are
    # supplied by search_long_term_memory instead of replaying whole sessions.
    for old in history[-8:]:
        role = old.get("role")
        content = old.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            items.append(_text_message(role, content))

    current: list[dict[str, Any]] = []
    for attachment in attachments or []:
        media_type = str(attachment.get("media_type") or "")
        data = str(attachment.get("data") or "")
        if not data:
            continue
        if media_type.startswith("image/"):
            current.append(
                {
                    "type": "input_image",
                    "image_url": f"data:{media_type};base64,{data}",
                    "detail": "auto",
                }
            )
        elif media_type == "application/pdf":
            current.append(
                {
                    "type": "input_file",
                    "filename": str(attachment.get("name") or "attachment.pdf")[:120],
                    "file_data": f"data:application/pdf;base64,{data}",
                }
            )
    if message.strip():
        current.append({"type": "input_text", "text": message.strip()})
    if evidence_context:
        current.append({"type": "input_text", "text": evidence_context})
    items.append({"role": "user", "content": current})
    return items


class StrategyChatService:
    def __init__(
        self,
        *,
        settings: BrainSettings | None = None,
        openai_brain: StrategyBrain | None = None,
        legacy_factory: Callable[[], Any] | None = None,
        tool_registry: ReadOnlyToolRegistry | None = None,
        enable_prefetch: bool | None = None,
    ) -> None:
        self.settings = settings or BrainSettings.from_env()
        self._brain = openai_brain
        self._legacy_factory = legacy_factory
        self._registry = tool_registry
        self._enable_prefetch = (openai_brain is None) if enable_prefetch is None else enable_prefetch

    def _openai_brain(self) -> StrategyBrain:
        if self._brain is None:
            if self._registry is None:
                self._registry = build_strategy_tool_registry()
            self._brain = StrategyBrain(
                OpenAIResponsesProvider(self.settings), self._registry
            )
        return self._brain

    async def _legacy_stream(
        self,
        message: str,
        history: list[dict[str, Any]],
        attachments: list[dict[str, Any]] | None,
    ) -> AsyncIterator[str]:
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            raise RuntimeError("Claude fallback is not configured")
        if self._legacy_factory is None:
            from analyzer import Analyzer

            analyzer = Analyzer()
        else:
            analyzer = self._legacy_factory()
        knowledge = list_knowledge(active_only=True)
        async for token in analyzer.chat_stream(
            message, history, attachments or [], knowledge
        ):
            yield token

    async def stream_events(
        self,
        message: str,
        history: list[dict[str, Any]],
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield progress, provider, text, and an evidence trace."""

        started = time.perf_counter()
        evidence_context = ""
        intent_name = "general"
        prefetched_names: set[str] = set()
        if self._enable_prefetch:
            if self._registry is None:
                self._registry = build_strategy_tool_registry()
            yield {"type": "progress", "message": "질문 의도를 파악하고 있습니다."}
            prefetch_task = asyncio.create_task(
                prefetch_strategy_evidence(message, history, self._registry)
            )
            wait_messages = (
                "최근 채널 성과와 성공·실패 영상을 비교하고 있습니다.",
                "retention·과거 기획·비즈니스PT 지식을 연결하고 있습니다.",
                "파이프라인과 장기 기억까지 중복 여부를 확인하고 있습니다.",
            )
            wait_index = 0
            while not prefetch_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(prefetch_task), timeout=1.5
                    )
                except TimeoutError:
                    yield {
                        "type": "progress",
                        "message": wait_messages[min(wait_index, len(wait_messages) - 1)],
                    }
                    wait_index += 1
            intent, evidence = prefetch_task.result()
            prefetched_names = set(evidence)
            intent_name = intent.name
            evidence_context = format_prefetched_evidence(intent, evidence)
            available = sum(
                1 for value in evidence.values() if not value.get("unavailable_reason")
            )
            yield {
                "type": "progress",
                "message": f"근거 {available}/{len(evidence)}개를 확보했습니다. 전략을 작성합니다.",
            }

        prefer_openai = self.settings.provider == "openai"
        openai_ready = bool(os.getenv("OPENAI_API_KEY", "").strip()) or self._brain is not None
        if prefer_openai and openai_ready:
            emitted = False
            try:
                brain = self._openai_brain()
                request = brain.build_request(
                    StrategyMode.STRATEGY_CHAT,
                    build_openai_input(message, history, attachments, evidence_context),
                    CHAT_TASK_INSTRUCTIONS,
                    metadata={"surface": "strategy_chat", "channel": "bujajubang"},
                )
                if prefetched_names:
                    request = replace(
                        request,
                        # The intent-specific prefetch has already collected the
                        # complete evidence contract in parallel.  A second,
                        # model-directed retrieval loop made interactive answers
                        # repeat searches and could add minutes without improving
                        # the channel-specific evidence.  General questions keep
                        # their optional tools for genuinely unforeseen lookups.
                        tools=[] if intent_name != "general" else [
                            tool for tool in request.tools
                            if tool.get("name") not in prefetched_names
                        ],
                    )
                yield {"type": "provider", "provider": "openai"}
                async for token in brain.stream(request):
                    emitted = True
                    yield {"type": "token", "token": token}
                yield {
                    "type": "trace",
                    "intent": intent_name,
                    "duration_ms": round((time.perf_counter() - started) * 1000),
                    "sources": list(self._registry.trace) if self._registry else [],
                }
                return
            except Exception as exc:
                # Do not log request bodies, credentials, or response payloads.
                # Exception class/status/code is sufficient to diagnose provider
                # routing safely in production.
                logger.warning(
                    "strategy OpenAI provider failed type=%s status=%s code=%s emitted=%s",
                    type(exc).__name__,
                    getattr(exc, "status_code", None),
                    getattr(exc, "code", None),
                    emitted,
                )
                if emitted or self.settings.fallback_provider != "anthropic":
                    raise
        fallback_message = message
        if evidence_context:
            fallback_message = f"{message}\n\n{evidence_context}"
        yield {"type": "provider", "provider": "anthropic"}
        async with asyncio.timeout(240):
            async for token in self._legacy_stream(fallback_message, history[-8:], attachments):
                yield {"type": "token", "token": token}
        yield {
            "type": "trace",
            "intent": intent_name,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "sources": list(self._registry.trace) if self._registry else [],
        }

    async def stream(
        self,
        message: str,
        history: list[dict[str, Any]],
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[tuple[str, str]]:
        """Backward-compatible provider/token stream used by existing callers."""

        provider = "openai"
        async for event in self.stream_events(message, history, attachments):
            if event["type"] == "provider":
                provider = str(event["provider"])
            elif event["type"] == "token":
                yield provider, str(event["token"])
