"""Production strategy-chat orchestration with a legacy Claude fallback."""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Callable

from database import list_knowledge

from .brain import StrategyBrain
from .config import BrainSettings
from .contracts import StrategyMode
from .providers import OpenAIResponsesProvider
from .retrieval import build_strategy_tool_registry


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
"""


def _text_message(role: str, text: str) -> dict[str, Any]:
    content_type = "input_text" if role == "user" else "output_text"
    return {"role": role, "content": [{"type": content_type, "text": text}]}


def build_openai_input(
    message: str,
    history: list[dict[str, Any]],
    attachments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for old in history[-20:]:
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
    items.append({"role": "user", "content": current})
    return items


class StrategyChatService:
    def __init__(
        self,
        *,
        settings: BrainSettings | None = None,
        openai_brain: StrategyBrain | None = None,
        legacy_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings or BrainSettings.from_env()
        self._brain = openai_brain
        self._legacy_factory = legacy_factory

    def _openai_brain(self) -> StrategyBrain:
        if self._brain is None:
            self._brain = StrategyBrain(
                OpenAIResponsesProvider(self.settings), build_strategy_tool_registry()
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

    async def stream(
        self,
        message: str,
        history: list[dict[str, Any]],
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(provider, token)`` and fallback only before visible output."""

        prefer_openai = self.settings.provider == "openai"
        openai_ready = bool(os.getenv("OPENAI_API_KEY", "").strip()) or self._brain is not None
        if prefer_openai and openai_ready:
            emitted = False
            try:
                brain = self._openai_brain()
                request = brain.build_request(
                    StrategyMode.STRATEGY_CHAT,
                    build_openai_input(message, history, attachments),
                    CHAT_TASK_INSTRUCTIONS,
                    metadata={"surface": "strategy_chat", "channel": "bujajubang"},
                )
                async for token in brain.stream(request):
                    emitted = True
                    yield "openai", token
                return
            except Exception:
                if emitted or self.settings.fallback_provider != "anthropic":
                    raise
        async for token in self._legacy_stream(message, history, attachments):
            yield "anthropic", token
