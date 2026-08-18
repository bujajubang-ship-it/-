"""Fast, bounded production channel-analysis orchestration."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import replace
from typing import Any, Callable, Sequence

from analyzer import Analyzer
from analytics_service import retention_30s_estimate
from strategy_brain import BrainRequest, BrainSettings, StrategyMode
from strategy_brain.modes import build_instructions
from strategy_brain.providers import OpenAIResponsesProvider


CHANNEL_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "channel_summary": {"type": "string"},
        "top_performing_topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "reason": {"type": "string"},
                    "avg_views": {"type": "number"},
                    "example": {"type": "string"},
                },
                "required": ["topic", "reason", "avg_views", "example"],
                "additionalProperties": False,
            },
        },
        "underperforming_topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "reason": {"type": "string"},
                    "avg_views": {"type": "number"},
                },
                "required": ["topic", "reason", "avg_views"],
                "additionalProperties": False,
            },
        },
        "best_upload_days": {"type": "array", "items": {"type": "string"}},
        "worst_upload_days": {"type": "array", "items": {"type": "string"}},
        "best_upload_hours": {"type": "array", "items": {"type": "string"}},
        "optimal_video_length": {"type": "string"},
        "successful_title_patterns": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "example": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["pattern", "example", "why"],
                "additionalProperties": False,
            },
        },
        "growth_bottleneck": {"type": "string"},
        "channel_recommendations": {
            "type": "array",
            "items": {"type": "string"},
        },
        "next_video_strategy": {"type": "string"},
    },
    "required": [
        "channel_summary",
        "top_performing_topics",
        "underperforming_topics",
        "best_upload_days",
        "worst_upload_days",
        "best_upload_hours",
        "optimal_video_length",
        "successful_title_patterns",
        "growth_bottleneck",
        "channel_recommendations",
        "next_video_strategy",
    ],
    "additionalProperties": False,
}


def select_retention_videos(
    videos: Sequence[dict[str, Any]], limit: int = 6
) -> list[dict[str, Any]]:
    """Use a small mixed cohort so a button click never launches 100 queries."""

    recent = list(videos[: max(1, limit // 2)])
    top = sorted(videos, key=lambda row: int(row.get("view_count") or 0), reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    # The cohorts can overlap heavily (for example when the newest videos are
    # also the most viewed), so use the remainder as a deterministic fill set.
    candidates = top[: max(1, limit // 2)] + recent + top + list(videos)
    for video in candidates:
        video_id = str(video.get("id") or video.get("video_id") or "")
        if video_id and video_id not in seen:
            selected.append(video)
            seen.add(video_id)
        if len(selected) >= limit:
            break
    return selected


async def fetch_retention_sample(
    analytics: Any,
    videos: Sequence[dict[str, Any]],
    *,
    period_start: str,
    period_end: str,
    limit: int = 6,
    concurrency: int = 3,
    timeout_seconds: float = 35.0,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch independent retention curves concurrently and tolerate partial failure."""

    selected = select_retention_videos(videos, limit=limit)
    semaphore = asyncio.Semaphore(concurrency)

    async def fetch(video: dict[str, Any]) -> dict[str, Any] | None:
        video_id = str(video.get("id") or video.get("video_id") or "")
        try:
            async with semaphore:
                result = await asyncio.wait_for(
                    analytics.get_video_retention(
                        video_id, start_date=period_start, end_date=period_end
                    ),
                    timeout=timeout_seconds,
                )
            estimate, metadata = retention_30s_estimate(
                result.get("points") or [],
                video.get("duration_sec", video.get("duration_seconds")),
            )
            return {
                "video_id": video_id,
                "title": video.get("title") or "",
                "status": result.get("status", "unavailable"),
                "data_through": result.get("data_through"),
                "point_count": len(result.get("points") or []),
                "retention_30s_estimate": estimate,
                "retention_30s_metadata": metadata,
                "source": result.get("source", "youtube_analytics_api_v2"),
            }
        except Exception:
            return None

    results = await asyncio.gather(*(fetch(video) for video in selected))
    available = [result for result in results if result is not None]
    return available, len(selected) - len(available)


def _average_groups(
    videos: Sequence[dict[str, Any]], key: Callable[[dict[str, Any]], str | None]
) -> str:
    groups: dict[str, list[int]] = defaultdict(list)
    for video in videos:
        group = key(video)
        if group:
            groups[group].append(int(video.get("view_count") or 0))
    rows = sorted(groups.items(), key=lambda item: -sum(item[1]) / len(item[1]))
    return " / ".join(
        f"{name}: 평균 {int(sum(values) / len(values)):,}회({len(values)}개)"
        for name, values in rows
    )


def build_channel_prompt(
    channel_info: dict[str, Any],
    videos: Sequence[dict[str, Any]],
    retention: Sequence[dict[str, Any]],
) -> str:
    top = sorted(videos, key=lambda row: int(row.get("view_count") or 0), reverse=True)[:10]
    bottom = sorted(videos, key=lambda row: int(row.get("view_count") or 0))[:10]
    recent = list(videos[:15])

    def metric(value: Any, status: str | None, suffix: str = "") -> str:
        if value is None or status not in (None, "available"):
            return status or "unavailable"
        return f"{value}{suffix}"

    def line(video: dict[str, Any]) -> str:
        retention_value = next(
            (
                row.get("retention_30s_estimate")
                for row in retention
                if row.get("video_id") == video.get("id")
            ),
            None,
        )
        retention_text = (
            f"{retention_value * 100:.1f}% (파생 추정치)"
            if retention_value is not None
            else "unavailable"
        )
        return (
            f"- {video.get('title', '')} | 조회 {int(video.get('view_count') or 0):,}"
            f" | 평균시청률 {metric(video.get('avg_view_percentage'), video.get('avg_view_percentage_status'), '%')}"
            f" | 시청분 {metric(video.get('watch_minutes'), video.get('watch_minutes_status'))}"
            f" | CTR {metric(video.get('ctr'), video.get('ctr_status'), '%')}"
            f" | 30초 retention {retention_text}"
            f" | {video.get('published_at') or '날짜 없음'} {video.get('publish_day') or ''}요일"
            f" {video.get('publish_hour') if video.get('publish_hour') is not None else '?'}시"
            f" | {int(video.get('duration_sec') or 0) // 60}분"
        )

    duration_text = _average_groups(
        videos,
        lambda video: (
            "3분 미만" if int(video.get("duration_sec") or 0) < 180
            else "3-8분" if int(video.get("duration_sec") or 0) < 480
            else "8-15분" if int(video.get("duration_sec") or 0) < 900
            else "15-30분" if int(video.get("duration_sec") or 0) < 1800
            else "30분 이상"
        ),
    )
    retention_text = "\n".join(
        f"- {row['title']} | 상태 {row['status']} | points {row['point_count']}"
        f" | 30초 추정 {row['retention_30s_estimate'] * 100:.1f}%"
        if row.get("retention_30s_estimate") is not None
        else f"- {row['title']} | 상태 {row['status']} | points {row['point_count']} | 30초 추정 unavailable"
        for row in retention
    ) or "- retention 표본 없음"
    return f"""채널명: {channel_info.get('title', '')}
구독자: {int(channel_info.get('subscriber_count') or 0):,}명
총 영상: {int(channel_info.get('video_count') or 0):,}개
총 조회수: {int(channel_info.get('view_count') or 0):,}회
분석 대상: {len(videos)}개

[조회수 상위 10]
{chr(10).join(line(video) for video in top)}

[조회수 하위 10]
{chr(10).join(line(video) for video in bottom)}

[최근 업로드 15]
{chr(10).join(line(video) for video in recent)}

[요일별 평균]
{_average_groups(videos, lambda video: (video.get('publish_day') or '') + '요일')}

[업로드 시간별 평균]
{_average_groups(videos, lambda video: f"{video.get('publish_hour'):02d}시" if video.get('publish_hour') is not None else None)}

[길이별 평균]
{duration_text}

[YouTube Analytics retention 표본]
{retention_text}

측정값 unavailable/pending/not_reported는 0이 아니다. CTR은 미리 수집된 Reporting
리포트가 있을 때만 사용하고, 없다는 사실 자체를 성과 원인으로 해석하지 않는다.
데이터에서 확인되는 사실, 해석, 실행 권고를 분리해 한국어 리포트를 작성한다.
top_performing_topics 5개, underperforming_topics 3개,
successful_title_patterns 4개, channel_recommendations 5~7개를 작성한다."""


async def analyze_channel_with_fallback(
    channel_info: dict[str, Any],
    videos: list[dict[str, Any]],
    retention: Sequence[dict[str, Any]],
    *,
    openai_timeout_seconds: float = 120.0,
    claude_timeout_seconds: float = 150.0,
    provider_factory: Callable[[BrainSettings], Any] = OpenAIResponsesProvider,
    fallback_factory: Callable[[], Any] = Analyzer,
) -> tuple[dict[str, Any], str, str | None]:
    """Prefer GPT-5.6 Sol and retain a bounded Claude rollback path."""

    openai_error: str | None = None
    settings = replace(
        BrainSettings.from_env(),
        provider="openai",
        openai_model="gpt-5.6-sol",
        # Channel analysis is interactive and all evidence is pre-computed.
        # Low reasoning avoids spending the user's wait time rediscovering facts.
        reasoning_effort="low",
    )
    prompt = build_channel_prompt(channel_info, videos, retention)
    try:
        provider = provider_factory(settings)
        request = BrainRequest(
            mode=StrategyMode.CHANNEL_ANALYSIS,
            instructions=build_instructions(
                StrategyMode.CHANNEL_ANALYSIS,
                "제공된 최신 채널 데이터만 사용해 UI 스키마에 맞는 간결한 분석을 생성한다. "
                "각 근거와 권고는 1~2문장으로 제한한다. 추가 도구를 호출하지 말고 "
                "반드시 유효한 JSON을 반환한다.",
            ),
            input=prompt,
            tools=[],
            output_schema=CHANNEL_ANALYSIS_SCHEMA,
            output_schema_name="channel_analysis_report",
            metadata={"flow": "production_channel_analysis"},
            reasoning_effort="low",
        )
        result = await asyncio.wait_for(
            provider.generate(request), timeout=openai_timeout_seconds
        )
        report = result.parsed
        if not isinstance(report, dict):
            report = json.loads(result.text)
        return report, "gpt-5.6-sol", None
    except Exception as exc:
        openai_error = type(exc).__name__

    fallback = fallback_factory()
    report = await asyncio.wait_for(
        fallback.analyze_channel(channel_info, videos),
        timeout=claude_timeout_seconds,
    )
    return report, "claude-opus-5-fallback", openai_error
