"""Generate and persist one end-to-end content strategy context."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from strategy_brain import BrainSettings, StrategyBrain, StrategyMode
from strategy_brain.providers import OpenAIResponsesProvider
from strategy_brain.retrieval import build_strategy_tool_registry
from strategy_brain.context_builder import format_prefetched_evidence, prefetch_strategy_evidence


STRATEGY_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "target_audience": {"type": "string"},
        "core_problem": {"type": "string"},
        "content_promise": {"type": "string"},
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
        "selected_title": {"type": "string"},
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
        "thumbnail_direction": {"type": "string"},
        "hook_5_15s": {"type": "string"},
        "hook": {"type": "string"},
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
        "shoot_list": {"type": "array", "items": {"type": "string"}},
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
        "source_videos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "measured_evidence": {"type": "string"},
                },
                "required": ["video_id", "title", "reason", "measured_evidence"],
                "additionalProperties": False,
            },
        },
        "source_knowledge": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "principle": {"type": "string"},
                    "why_applied": {"type": "string"},
                },
                "required": ["title", "principle", "why_applied"],
                "additionalProperties": False,
            },
        },
        "created_at": {"type": "string"},
        "next_action": {"type": "string"},
    },
    "required": [
        "topic", "target_audience", "core_problem", "content_promise", "why_now",
        "core_message", "evidence", "title_candidates", "recommended_title",
        "selected_title", "thumbnail", "thumbnail_direction", "hook_5_15s",
        "hook", "structure", "shots", "shoot_list", "worksheet", "kpis",
        "counterargument_and_risks", "source_videos", "source_knowledge",
        "created_at", "next_action",
    ],
    "additionalProperties": False,
}


def merge_strategy_revision(
    existing: dict[str, Any], generated: dict[str, Any], prompt: str
) -> dict[str, Any]:
    """Keep one strategy context and update only the requested dependency slice."""

    lowered = prompt.replace(" ", "")
    title = "제목" in lowered and not any(
        marker in lowered for marker in ("제목은유지", "제목유지", "제목은그대로")
    )
    thumbnail = "썸네일" in lowered and not any(
        marker in lowered for marker in ("썸네일은유지", "썸네일유지", "썸네일은그대로")
    )
    hook = any(marker in lowered for marker in ("훅", "후킹", "첫5초", "오프닝"))
    worksheet = any(marker in lowered for marker in ("워크시트", "촬영컷", "구조"))
    focused = title or thumbnail or hook or worksheet
    if not focused:
        return generated
    allowed = {"evidence", "source_videos", "source_knowledge", "next_action", "created_at"}
    if title:
        allowed.update({"title_candidates", "recommended_title", "selected_title", "content_promise"})
    if thumbnail:
        allowed.update({"thumbnail", "thumbnail_direction"})
    if hook:
        allowed.update({"hook_5_15s", "hook", "structure"})
    if worksheet:
        allowed.update({"structure", "shots", "shoot_list", "worksheet"})
    merged = dict(existing)
    for key in allowed:
        if key in generated:
            merged[key] = generated[key]
    return merged


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
    registry = build_strategy_tool_registry()
    intent, prefetched = await prefetch_strategy_evidence(prompt, [], registry)
    evidence_context = format_prefetched_evidence(intent, prefetched)
    brain = StrategyBrain(OpenAIResponsesProvider(settings), registry)
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    user_input = (
        f"콘텐츠 형식: {content_type}\n생성 시각: {now}\n사용자 요청: {prompt.strip()}"
        + (
            "\n\n기존 공통 전략 context를 유지하며 요청한 부분만 일관되게 개선하세요:\n"
            + json.dumps(existing, ensure_ascii=False)
            if existing
            else ""
        )
        + f"\n\n{evidence_context}"
    )
    request = brain.build_request(
        StrategyMode.PLANNING,
        user_input,
        "관련 데이터를 직접 조회하고 하나의 촬영·업로드 전략으로 완성한다. 근거 없는 숫자는 만들지 않는다. "
        "source_videos에는 실제 비교한 영상만, source_knowledge에는 실제 적용한 관련 지식과 적용 이유만 쓴다. "
        "제목 후보는 3~5개, 추천은 하나로 확정한다. selected_title은 recommended_title과 같게 한다.",
        output_schema=STRATEGY_CONTEXT_SCHEMA,
        output_schema_name="bujajubang_strategy_context",
        metadata={"surface": "integrated_planning", "content_type": content_type[:40]},
    )
    request = replace(
        request,
        tools=[tool for tool in request.tools if tool.get("name") not in prefetched],
    )
    result = await brain.run(request)
    if not isinstance(result.parsed, dict):
        raise RuntimeError("GPT strategy context was not valid structured output")
    strategy = result.parsed
    strategy["created_at"] = now
    strategy["selected_title"] = strategy.get("recommended_title") or strategy.get("selected_title")
    strategy["hook"] = strategy.get("hook_5_15s") or strategy.get("hook")
    strategy["shoot_list"] = strategy.get("shots") or strategy.get("shoot_list") or []
    strategy["retrieval_trace"] = list(registry.trace)
    if existing:
        strategy = merge_strategy_revision(existing, strategy, prompt)
    return strategy
