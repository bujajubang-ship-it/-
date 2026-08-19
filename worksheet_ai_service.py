"""OpenAI-first worksheet generation for the content research surface.

This module intentionally has no dependency on the optional CNMAKER service.
The legacy Analyzer is accepted only as an injected fallback factory.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Callable

from strategy_brain import BrainSettings, StrategyBrain, StrategyMode
from strategy_brain.context_builder import (
    format_prefetched_evidence,
    prefetch_strategy_evidence,
)
from strategy_brain.providers import OpenAIResponsesProvider
from strategy_brain.retrieval import build_strategy_tool_registry
from youtube_strategy_context import (
    YouTubeStrategyContextService,
    format_strategy_data_context,
)


WORKSHEET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "keyword": {"type": "string"},
        "viewerTalk": {"type": "string"},
        "thumbA": {"type": "string"},
        "introA": {"type": "string"},
        "empathy": {"type": "string"},
        "titleCopy": {"type": "string"},
        "thumbCopy": {"type": "string"},
        "thumbDesign": {"type": "string"},
        "introScript": {"type": "string"},
        "bodyScript": {"type": "string"},
        "memo": {"type": "string"},
    },
    "required": [
        "name", "keyword", "viewerTalk", "thumbA", "introA", "empathy",
        "titleCopy", "thumbCopy", "thumbDesign", "introScript", "bodyScript", "memo",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class WorksheetGenerationResult:
    data: dict[str, Any]
    provider: str
    retrieval_trace: list[dict[str, Any]]
    retrieval_summary: dict[str, Any]


def _openai_settings() -> BrainSettings:
    configured = BrainSettings.from_env()
    return BrainSettings(
        provider="openai",
        fallback_provider=configured.fallback_provider,
        openai_model=configured.openai_model,
        reasoning_effort="max",
        store_responses=configured.store_responses,
        max_tool_rounds=configured.max_tool_rounds,
    )


def _compact(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…[compacted]"


class WorksheetAIService:
    """Generate the existing worksheet card contract with GPT by default."""

    def __init__(
        self,
        *,
        brain_factory: Callable[[Any], StrategyBrain] | None = None,
        legacy_factory: Callable[[], Any] | None = None,
        strategy_context_service: Any | None = None,
    ) -> None:
        self._brain_factory = brain_factory
        self._legacy_factory = legacy_factory
        self._strategy_context_service = (
            strategy_context_service or YouTubeStrategyContextService()
        )

    async def generate(
        self,
        keyword: str,
        ref_videos: list[dict[str, Any]] | None = None,
        naver: list[dict[str, Any]] | None = None,
        viewtrap_refs: dict[str, Any] | None = None,
        knowledge: list[dict[str, Any]] | None = None,
        brief: str = "",
    ) -> WorksheetGenerationResult:
        ref_videos = ref_videos or []
        knowledge = knowledge or []
        registry = build_strategy_tool_registry()
        retrieval_query = (
            f"{keyword} 영상 기획. 외식창업자 고객 문제, low data 검증, "
            "비즈니스PT 원칙, 부자주방 브랜드 전략, 과거 유사 영상과 워크시트"
        )

        strategy_context = await self._strategy_context_service.collect()
        live_context = format_strategy_data_context(strategy_context)

        try:
            intent, prefetched = await prefetch_strategy_evidence(
                retrieval_query, [], registry
            )
            evidence_context = format_prefetched_evidence(intent, prefetched)
            brain = (
                self._brain_factory(registry)
                if self._brain_factory
                else StrategyBrain(OpenAIResponsesProvider(_openai_settings()), registry)
            )
            references = []
            image_content: list[dict[str, Any]] = []
            for index, video in enumerate(ref_videos[:3], 1):
                references.append(
                    {
                        "index": index,
                        "id": video.get("id"),
                        "title": video.get("title"),
                        "url": video.get("url"),
                        "view_count": video.get("view_count"),
                        "comments": (video.get("comments") or [])[:15],
                        "script": str(video.get("script") or "")[:6000],
                    }
                )
                thumbnail = str(video.get("thumbnail_url") or "").strip()
                if thumbnail.startswith(("https://", "http://")):
                    image_content.append(
                        {"type": "input_image", "image_url": thumbnail, "detail": "low"}
                    )

            supplied_knowledge = [
                {
                    "title": row.get("title"),
                    "category": row.get("category"),
                    "summary": row.get("summary"),
                    "content": str(row.get("content") or "")[:3000],
                }
                for row in knowledge[:12]
            ]
            task_text = f"""주제/키워드: {keyword}
이번 영상 핵심 내용: {brief or '(미입력 — 확인된 근거 범위에서 설계)'}

[사용자가 선택한 레퍼런스 영상·실제 스크립트]
{_compact(references, 18000)}

[수집된 실제 고객 반응]
네이버: {_compact((naver or [])[:20], 9000)}
ViewTrap: {_compact(viewtrap_refs or {}, 7000)}

[워크시트 화면에서 선택된 저장 지식]
{_compact(supplied_knowledge, 12000)}

{evidence_context}

{live_context}

확인된 자료만 근거로 부자주방 촬영 워크시트를 작성하세요. 부자주방은 단순 제품 판매가 아니라
주방 설계·동선·납품·시공·A/S까지 보는 현장형 주방 솔루션 브랜드입니다.
low data 원칙에 따라 표본이 작거나 데이터가 없으면 그 사실을 memo에 명시하고 숫자를 만들지 마세요.
레퍼런스 스크립트가 있으면 도입 beat와 심리 기제를 실제 문장에 근거해 분해하고, 없으면 추정이라고 표시하세요.
비즈니스PT 지식은 관련 원칙만 골라 고객 문제→제목→썸네일→훅→본문에 일관되게 적용하고,
memo 끝에 '적용한 지식: 원칙 — 적용 이유/위치'를 남기세요.
titleCopy는 줄바꿈으로 3~5개, thumbCopy는 줄바꿈으로 3~4개만 제시하고 각각 1순위를 첫 줄에 두세요.
introScript는 첫 30초 안에 제목·썸네일 약속을 회수하고, bodyScript는 문제→비용/위험→현장 증거→해결→문의 행동으로 작성하세요."""
            input_value: str | list[dict[str, Any]]
            if image_content:
                input_value = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": task_text},
                            *image_content,
                        ],
                    }
                ]
            else:
                input_value = task_text
            request = brain.build_request(
                StrategyMode.WORKSHEET,
                input_value,
                "기존 워크시트 카드 필드를 빠짐없이 채운다. 일반론보다 조회된 부자주방 성과, "
                "과거 기획, 고객 반응, 비즈니스PT 지식과 사용자가 제공한 레퍼런스 스크립트를 우선한다.",
                output_schema=WORKSHEET_SCHEMA,
                output_schema_name="bujajubang_worksheet_autofill",
                metadata={"surface": "worksheet_autofill", "keyword": keyword[:80]},
            )
            request = replace(
                request,
                # A 12-field worksheet with max reasoning can exceed the
                # interactive provider timeout. Retrieval and output fields
                # stay intact; only the mode-specific reasoning budget is bounded.
                reasoning_effort="low",
                tools=[tool for tool in request.tools if tool.get("name") not in prefetched],
            )
            response = await brain.run(request)
            if not isinstance(response.parsed, dict):
                raise RuntimeError("GPT worksheet output was not valid structured data")
            data = dict(response.parsed)
            data["keyword"] = data.get("keyword") or keyword
            return WorksheetGenerationResult(
                data,
                "openai",
                list(registry.trace),
                strategy_context.retrieval_summary,
            )
        except Exception:
            if self._legacy_factory is None:
                raise RuntimeError("워크시트 AI 작성에 실패했습니다. 잠시 후 다시 시도해 주세요.")
            legacy = self._legacy_factory()
            data = await legacy.autofill_worksheet(
                keyword, ref_videos or None, naver or None, viewtrap_refs,
                knowledge or None, brief,
            )
            summary = dict(strategy_context.retrieval_summary)
            summary["provider"] = "anthropic_legacy_fallback"
            return WorksheetGenerationResult(
                data,
                "anthropic_legacy_fallback",
                list(registry.trace),
                summary,
            )
