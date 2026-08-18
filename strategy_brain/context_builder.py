"""Deterministic, parallel evidence prefetch for strategy questions.

The model may still call read-only tools for follow-ups, but the evidence that
defines a high-quality answer is no longer left to probabilistic tool choice.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from .tools import ReadOnlyToolRegistry


@dataclass(frozen=True)
class StrategyIntent:
    name: str
    label: str
    query: str


def _topic_query(message: str, history: list[dict[str, Any]]) -> str:
    contextual = message
    if re.search(r"이\s*(주제|영상|기획)|제목|썸네일", message):
        prior = [
            str(item.get("content") or "")
            for item in history[-8:]
            if item.get("role") == "user"
        ]
        if prior:
            contextual = f"{prior[-1]} {message}"
    noise = (
        "다음 영상 뭐 찍을까 요즘 우리 채널 방향 맞는 것 같아 어떻게 기획할까 "
        "이 주제로 제목이랑 썸네일 최종 결정해줘 최근 영상들에서 내가 놓치고 있는 "
        "공통 문제가 뭐야 영상 찍으려고 하는데"
    ).split()
    query = contextual
    for token in noise:
        if len(token) > 1:
            query = query.replace(token, " ")
    query = re.sub(r"\s+", " ", query).strip()
    return query[:180] or contextual[:180]


def classify_strategy_intent(
    message: str, history: list[dict[str, Any]] | None = None
) -> StrategyIntent:
    history = history or []
    lowered = message.replace(" ", "").lower()
    query = _topic_query(message, history)
    if any(marker in lowered for marker in ("다음영상", "뭐찍", "주제추천")):
        return StrategyIntent("next_video", "다음 영상 후보를 고르는 중", query)
    if any(marker in lowered for marker in ("채널방향", "방향이맞", "방향맞")):
        return StrategyIntent("channel_direction", "최근 성공·실패 패턴을 비교하는 중", query)
    if any(marker in lowered for marker in ("ctr", "클릭률", "노출", "조회가안", "조회수가높은데")):
        return StrategyIntent("ctr_analysis", "공식 Reach·CTR과 retention을 교차 분석하는 중", query)
    if any(marker in lowered for marker in ("제목", "썸네일")):
        return StrategyIntent("title_thumbnail", "과거 제목·썸네일 성과를 비교하는 중", query)
    if any(marker in lowered for marker in ("공통문제", "놓치고", "실패패턴", "최근영상들")):
        return StrategyIntent("common_problems", "반복되는 성과·retention 문제를 찾는 중", query)
    if any(marker in lowered for marker in ("기획", "찍으려고", "주제평가", "아이디어")):
        return StrategyIntent("topic_plan", "비슷한 과거 영상과 지식 원칙을 연결하는 중", query)
    return StrategyIntent("general", "관련 채널 근거와 장기 기억을 찾는 중", query)


def _tool_plan(intent: StrategyIntent) -> list[tuple[str, dict[str, Any]]]:
    query = intent.query
    memory_query = "" if intent.name in {"next_video", "channel_direction", "common_problems"} else query
    common_memory = [
        ("search_long_term_memory", {"query": memory_query, "limit": 8}),
        ("search_chat_memory", {"query": memory_query, "limit": 5}),
    ]
    if intent.name == "next_video":
        return [
            ("get_channel_strategy_snapshot", {"limit": 20}),
            ("get_retention_patterns", {"video_id": None, "limit": 10}),
            ("get_ctr_performance", {"limit": 120}),
            ("compare_title_patterns", {"query": None}),
            ("compare_thumbnail_patterns", {}),
            ("search_previous_plans", {"query": "", "limit": 8}),
            ("search_feedback_history", {"query": "", "limit": 8}),
            ("get_content_pipeline", {"status": None, "limit": 40}),
            ("search_business_pt_knowledge", {"query": "고객 문제 제목 훅 구매 상황 콘텐츠", "limit": 6}),
            ("get_recent_trends", {"query": "업소용 주방 식당 창업", "days": 90, "limit": 10}),
            *common_memory,
        ]
    if intent.name == "channel_direction":
        return [
            ("get_channel_strategy_snapshot", {"limit": 30}),
            ("get_retention_patterns", {"video_id": None, "limit": 12}),
            ("get_ctr_performance", {"limit": 120}),
            ("compare_title_patterns", {"query": None}),
            ("compare_thumbnail_patterns", {}),
            ("search_feedback_history", {"query": "", "limit": 10}),
            ("search_previous_plans", {"query": "", "limit": 8}),
            ("get_content_pipeline", {"status": None, "limit": 40}),
            *common_memory,
        ]
    if intent.name in {"topic_plan", "title_thumbnail"}:
        plan = [
            ("compare_similar_videos", {"query": query, "limit": 10}),
            ("get_retention_patterns", {"video_id": None, "limit": 10}),
            ("get_ctr_performance", {"limit": 120}),
            ("compare_title_patterns", {"query": query}),
            ("compare_thumbnail_patterns", {}),
            ("compare_impression_to_click_performance", {}),
            ("search_previous_plans", {"query": query, "limit": 8}),
            ("search_feedback_history", {"query": query, "limit": 8}),
            ("search_business_pt_knowledge", {"query": f"{query} 고객 문제 제목 썸네일 훅 구매 상황", "limit": 6}),
            ("search_knowledge", {"query": query, "limit": 6}),
            ("get_content_pipeline", {"status": None, "limit": 40}),
            *common_memory,
        ]
        if intent.name == "topic_plan":
            plan.extend(
                [
                    ("search_previous_worksheets", {"query": query, "limit": 6}),
                    ("get_recent_trends", {"query": query, "days": 180, "limit": 10}),
                ]
            )
        return plan
    if intent.name == "ctr_analysis":
        return [
            ("get_ctr_performance", {"limit": 150}),
            ("compare_title_patterns", {"query": query}),
            ("compare_thumbnail_patterns", {}),
            ("find_high_ctr_low_retention", {}),
            ("find_high_ctr_high_retention", {}),
            ("compare_impression_to_click_performance", {}),
            ("get_retention_patterns", {"video_id": None, "limit": 15}),
            *common_memory,
        ]
    if intent.name == "common_problems":
        return [
            ("get_channel_strategy_snapshot", {"limit": 20}),
            ("get_retention_patterns", {"video_id": None, "limit": 15}),
            ("search_feedback_history", {"query": "", "limit": 12}),
            ("get_ctr_performance", {"limit": 100}),
            ("compare_impression_to_click_performance", {}),
            ("search_previous_plans", {"query": "", "limit": 8}),
            *common_memory,
        ]
    return [
        ("get_channel_strategy_snapshot", {"limit": 20}),
        ("search_business_pt_knowledge", {"query": f"{query} 고객 문제 제목 훅", "limit": 5}),
        ("get_content_pipeline", {"status": None, "limit": 30}),
        *common_memory,
    ]


async def prefetch_strategy_evidence(
    message: str,
    history: list[dict[str, Any]],
    registry: ReadOnlyToolRegistry,
    *,
    timeout_seconds: float = 20.0,
) -> tuple[StrategyIntent, dict[str, Any]]:
    intent = classify_strategy_intent(message, history)
    plan = _tool_plan(intent)

    async def fetch(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            result = await asyncio.wait_for(
                registry.execute(name, arguments), timeout=timeout_seconds
            )
        except TimeoutError:
            result = {
                "data": None,
                "source": f"tool:{name}",
                "unavailable_reason": f"bounded retrieval timeout ({timeout_seconds:g}s)",
            }
            registry.trace.append(
                {"tool": name, "source": result["source"], "unavailable": True, "duration_ms": round(timeout_seconds * 1000)}
            )
        return name, result

    pairs = await asyncio.gather(*(fetch(name, arguments) for name, arguments in plan))
    return intent, {name: result for name, result in pairs}


def format_prefetched_evidence(intent: StrategyIntent, evidence: dict[str, Any]) -> str:
    sections = []
    for name, result in evidence.items():
        serialized = json.dumps(
            result, ensure_ascii=False, default=str, separators=(",", ":")
        )
        if len(serialized) > 6_000:
            serialized = serialized[:6_000] + "…[tool result compacted]"
        sections.append(f"\n<{name}>{serialized}</{name}>")
    payload = "".join(sections)
    return (
        "[서버가 질문 의도에 맞춰 병렬 조회한 필수 근거]\n"
        f"intent={intent.name}; query={intent.query}\n"
        "아래 근거는 이미 조회됐다. 같은 도구를 반복 호출하지 말고, 부족한 세부사항만 추가 도구로 확인한다. "
        "답변의 데이터 근거에는 사용한 영상/지식/source/as_of를 명시한다.\n"
        f"{payload}"
    )
