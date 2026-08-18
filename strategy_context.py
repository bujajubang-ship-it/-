"""Generate and persist one end-to-end content strategy context."""

from __future__ import annotations

import json
from typing import Any

from strategy_brain import BrainSettings, StrategyBrain, StrategyMode
from strategy_brain.providers import OpenAIResponsesProvider
from strategy_brain.retrieval import build_strategy_tool_registry


STRATEGY_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "target_audience": {"type": "string"},
        "why_now": {"type": "string"},
        "core_message": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "source": {"type": "string"},
                    "as_of": {"type": ["string", "null"]},
                    "interpretation": {"type": "string"},
                },
                "required": ["claim", "source", "as_of", "interpretation"],
                "additionalProperties": False,
            },
        },
        "title_candidates": {"type": "array", "items": {"type": "string"}},
        "recommended_title": {"type": "string"},
        "thumbnail": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "composition": {"type": "string"},
                "shooting_direction": {"type": "string"},
            },
            "required": ["text", "composition", "shooting_direction"],
            "additionalProperties": False,
        },
        "hook_5_15s": {"type": "string"},
        "structure": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "section": {"type": "string"},
                    "purpose": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["section", "purpose", "content"],
                "additionalProperties": False,
            },
        },
        "shots": {"type": "array", "items": {"type": "string"}},
        "worksheet": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string"},
                    "goal": {"type": "string"},
                    "talking_points": {"type": "string"},
                    "b_roll": {"type": "string"},
                    "props_and_checks": {"type": "string"},
                },
                "required": ["scene", "goal", "talking_points", "b_roll", "props_and_checks"],
                "additionalProperties": False,
            },
        },
        "kpis": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "checkpoint": {"type": "string"},
                    "metric": {"type": "string"},
                    "target": {"type": "string"},
                    "decision_rule": {"type": "string"},
                },
                "required": ["checkpoint", "metric", "target", "decision_rule"],
                "additionalProperties": False,
            },
        },
        "counterargument_and_risks": {"type": "array", "items": {"type": "string"}},
        "next_action": {"type": "string"},
    },
    "required": [
        "topic", "target_audience", "why_now", "core_message", "evidence",
        "title_candidates", "recommended_title", "thumbnail", "hook_5_15s",
        "structure", "shots", "worksheet", "kpis", "counterargument_and_risks",
        "next_action",
    ],
    "additionalProperties": False,
}


async def generate_strategy_context(
    prompt: str,
    *,
    content_type: str = "미드폼",
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = BrainSettings.from_env()
    if settings.provider != "openai":
        settings = BrainSettings(
            provider="openai",
            fallback_provider=settings.fallback_provider,
            openai_model=settings.openai_model,
            reasoning_effort="max",
            store_responses=settings.store_responses,
            max_tool_rounds=settings.max_tool_rounds,
        )
    brain = StrategyBrain(
        OpenAIResponsesProvider(settings), build_strategy_tool_registry()
    )
    user_input = (
        f"콘텐츠 형식: {content_type}\n사용자 요청: {prompt.strip()}"
        + (
            "\n\n기존 공통 전략 context를 유지하며 요청한 부분만 일관되게 개선하세요:\n"
            + json.dumps(existing, ensure_ascii=False)
            if existing
            else ""
        )
    )
    request = brain.build_request(
        StrategyMode.PLANNING,
        user_input,
        "관련 데이터를 직접 조회하고 하나의 촬영·업로드 전략으로 완성한다. 근거 없는 숫자는 만들지 않는다.",
        output_schema=STRATEGY_CONTEXT_SCHEMA,
        output_schema_name="bujajubang_strategy_context",
        metadata={"surface": "integrated_planning", "content_type": content_type[:40]},
    )
    result = await brain.run(request)
    if not isinstance(result.parsed, dict):
        raise RuntimeError("GPT strategy context was not valid structured output")
    return result.parsed
