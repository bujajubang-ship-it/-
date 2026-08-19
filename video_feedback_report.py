"""Markdown business feedback report from reusable transcript/visual analysis."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from strategy_brain import BrainSettings, StrategyBrain, StrategyMode
from strategy_brain.providers import OpenAIResponsesProvider
from strategy_brain.retrieval import build_strategy_tool_registry
from youtube_strategy_context import (
    YouTubeStrategyContextService,
    format_strategy_data_context,
)


REPORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "overall": {"type": "string"},
        "biggest_problem": {"type": "string"},
        "strongest_scene": {"type": "string"},
        "intro_feedback": {"type": "string"},
        "retention_feedback": {"type": "string"},
        "conversion_feedback": {"type": "string"},
        "visual_feedback": {"type": "string"},
        "speech_structure_feedback": {"type": "string"},
        "timecode_feedback": {"type": "array", "items": {"type": "string"}},
        "must_keep": {"type": "array", "items": {"type": "string"}},
        "safe_to_reduce": {"type": "array", "items": {"type": "string"}},
        "dangerous_to_delete": {"type": "array", "items": {"type": "string"}},
        "title_candidates": {"type": "array", "items": {"type": "string"}},
        "thumbnail_copy": {"type": "array", "items": {"type": "string"}},
        "short_topics": {"type": "array", "items": {"type": "string"}},
        "priorities": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "overall", "biggest_problem", "strongest_scene", "intro_feedback",
        "retention_feedback", "conversion_feedback", "visual_feedback",
        "speech_structure_feedback", "timecode_feedback", "must_keep",
        "safe_to_reduce", "dangerous_to_delete", "title_candidates",
        "thumbnail_copy", "short_topics", "priorities",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class MarkdownFeedbackResult:
    markdown: str
    retrieval_summary: dict[str, Any]
    provider: str


def _compact(value: Any, limit: int) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…[compacted]"


def _items(title: str, values: list[str]) -> str:
    return f"## {title}\n\n" + "\n".join(f"- {value}" for value in values)


def render_markdown(payload: dict[str, Any], summary: dict[str, Any]) -> str:
    header_order = (
        "provider", "youtube_analytics_applied", "channel_snapshot_sample_size",
        "recent_video_sample_size", "retention_sample_size", "ctr_available",
        "business_pt_applied", "low_data_applied", "brand_strategy_applied",
        "applied_sources", "missing_sources",
    )
    lines = ["# 이태원 영상 피드백 — YouTube 데이터 적용", ""]
    for key in header_order:
        value = summary.get(key)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, default=str)
        elif isinstance(value, bool):
            value = str(value).lower()
        lines.append(f"- {key}={value}")
    lines.extend(
        [
            "", "## 총평", "", payload["overall"],
            "", "## 가장 큰 문제", "", payload["biggest_problem"],
            "", "## 가장 강한 장면", "", payload["strongest_scene"],
            "", "## 도입부 피드백", "", payload["intro_feedback"],
            "", "## 유지율 관점 피드백", "", payload["retention_feedback"],
            "", "## 비즈니스 전환 관점 피드백", "", payload["conversion_feedback"],
            "", "## 화면/촬영 피드백", "", payload["visual_feedback"],
            "", "## 말/자막/구성 피드백", "", payload["speech_structure_feedback"],
            "", _items("구간별 타임코드 피드백", payload["timecode_feedback"]),
            "", _items("반드시 살릴 구간", payload["must_keep"]),
            "", _items("줄여도 되는 구간", payload["safe_to_reduce"]),
            "", _items("삭제하면 위험한 구간", payload["dangerous_to_delete"]),
            "", _items("제목 후보 10개", payload["title_candidates"][:10]),
            "", _items("썸네일 문구 10개", payload["thumbnail_copy"][:10]),
            "", _items("쇼츠 분할 주제 5개", payload["short_topics"][:5]),
            "", _items("업로드 전 수정 우선순위 5개", payload["priorities"][:5]),
            "",
        ]
    )
    return "\n".join(lines)


async def generate_markdown_feedback(
    source_analysis: dict[str, Any],
    *,
    topic: str = "이태원 좁은 갈빗집 주방 설계·납품 현장",
    brain_factory: Callable[[Any], StrategyBrain] | None = None,
    strategy_context_service: Any | None = None,
) -> MarkdownFeedbackResult:
    context_service = strategy_context_service or YouTubeStrategyContextService()
    context = await context_service.collect()
    registry = build_strategy_tool_registry()
    if brain_factory:
        brain = brain_factory(registry)
    else:
        configured = BrainSettings.from_env()
        settings = BrainSettings(
            provider="openai",
            fallback_provider=configured.fallback_provider,
            openai_model=configured.openai_model,
            reasoning_effort="high",
            store_responses=configured.store_responses,
            max_tool_rounds=1,
        )
        brain = StrategyBrain(OpenAIResponsesProvider(settings), registry)

    source = {
        "business_review": {
            key: (source_analysis.get("business_review") or {}).get(key)
            for key in (
                "business_completeness_score", "overall_diagnosis", "good_points",
                "weak_flow_points", "boring_for_founders", "messages_to_emphasize",
                "must_keep", "safe_to_reduce", "dangerous_to_delete",
                "applied_business_principles", "data_limitations",
            )
        },
        "transcript": {
            "text": str((source_analysis.get("transcript") or {}).get("text") or "")[:24000],
            "segments": (source_analysis.get("transcript") or {}).get("segments", [])[:80],
        },
        "visual_analysis": {
            "status": (source_analysis.get("visual_analysis") or {}).get("status"),
            "segments": (source_analysis.get("visual_analysis") or {}).get("segments", [])[:50],
            "frame_results": (source_analysis.get("visual_analysis") or {}).get("frame_results", [])[:40],
        },
        "retrieval_trace": source_analysis.get("retrieval_trace") or [],
    }
    source["business_review"]["segments"] = (
        (source_analysis.get("business_review") or {}).get("segments") or []
    )[:45]
    prompt = f"""주제: {topic}

[재사용할 기존 transcript + visual analysis + business review]
{_compact(source, 42000)}

{format_strategy_data_context(context)}

새 영상 렌더링이나 자동편집을 하지 말고 업로드 전 피드백만 작성하세요.
실제 화면 분석이 있는 구간만 화면을 봤다고 표현하고, 없는 CTR/retention 수치는 만들지 마세요.
부자주방을 설계·동선·납품·시공·A/S까지 보는 현장형 주방 솔루션 브랜드로 평가하세요.
외식창업자의 비용 손실 방지, 현장 증거, 상담·견적 전환을 조회수와 함께 판단하세요.
제목 후보와 썸네일 문구는 각각 정확히 10개, 쇼츠 주제와 우선순위는 각각 정확히 5개를 작성하세요.
타임코드 항목은 기존 분석에 실제 존재하는 시간만 사용하세요."""
    request = brain.build_request(
        StrategyMode.VIDEO_FEEDBACK,
        prompt,
        "실제 YouTube 데이터, transcript, visual analysis, business PT와 low data 원칙을 구분해 적용한다.",
        output_schema=REPORT_SCHEMA,
        output_schema_name="youtube_data_video_feedback_report",
        metadata={"surface": "video_feedback_markdown"},
    )
    request = replace(request, reasoning_effort="low", tools=[])
    response = await brain.run(request)
    if not isinstance(response.parsed, dict):
        raise RuntimeError("영상 피드백 리포트를 생성하지 못했습니다.")
    return MarkdownFeedbackResult(
        render_markdown(response.parsed, context.retrieval_summary),
        context.retrieval_summary,
        "openai",
    )


def save_markdown_feedback(result: MarkdownFeedbackResult, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.markdown, encoding="utf-8")
    return target
