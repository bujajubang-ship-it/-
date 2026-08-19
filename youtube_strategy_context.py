"""Live, read-only YouTube evidence for strategy and feedback surfaces.

This connector never makes up unavailable metrics. It combines OAuth-backed
Data/Analytics API rows with the last successfully collected Reporting Reach
snapshot and always returns a UI-safe retrieval summary.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from analytics_repository import AnalyticsRepository
from analytics_service import AnalyticsApiError, AnalyticsService, response_rows


KNOWLEDGE_FILES = {
    "business_pt": "business_pt.md",
    "low_data": "low_data_rules.md",
    "brand_strategy": "brand_strategy.md",
    "video_feedback": "video_feedback_principles.md",
}
_CACHE: dict[str, tuple[float, "StrategyDataContext"]] = {}
_CACHE_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class StrategyDataContext:
    evidence: dict[str, Any]
    retrieval_summary: dict[str, Any]
    knowledge_text: str


def load_strategy_knowledge() -> tuple[str, dict[str, bool], list[str]]:
    root = Path(__file__).resolve().parent / "data" / "knowledge"
    sections: list[str] = []
    flags: dict[str, bool] = {}
    missing: list[str] = []
    for key, filename in KNOWLEDGE_FILES.items():
        path = root / filename
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            content = ""
        flags[key] = bool(content)
        if content:
            sections.append(content)
        else:
            missing.append(f"knowledge:{filename}")
    return "\n\n".join(sections), flags, missing


def _channel_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    rows = response_rows(payload)
    totals = {
        "views": 0,
        "estimatedMinutesWatched": 0.0,
        "likes": 0,
        "comments": 0,
        "shares": 0,
        "subscribersGained": 0,
    }
    weighted_duration = 0.0
    weighted_percentage = 0.0
    weight = 0.0
    for row in rows:
        views = float(row.get("views") or 0)
        for key in totals:
            totals[key] += float(row.get(key) or 0)
        weighted_duration += float(row.get("averageViewDuration") or 0) * views
        weighted_percentage += float(row.get("averageViewPercentage") or 0) * views
        weight += views
    return {
        **{key: int(value) if key != "estimatedMinutesWatched" else round(value, 2) for key, value in totals.items()},
        "averageViewDuration": round(weighted_duration / weight, 2) if weight else None,
        "averageViewPercentage": round(weighted_percentage / weight, 2) if weight else None,
        "sample_size": len(rows),
    }


def _missing_entry(source: str, reason: str, detail: str | None = None) -> dict[str, str]:
    row = {"source": source, "reason": reason}
    if detail:
        row["detail"] = detail
    return row


class YouTubeStrategyContextService:
    def __init__(
        self,
        *,
        analytics: AnalyticsService | None = None,
        repository: AnalyticsRepository | None = None,
    ) -> None:
        self.analytics = analytics or AnalyticsService()
        self.repository = repository or AnalyticsRepository()
        self._owns_analytics = analytics is None

    async def collect(
        self,
        *,
        video_id: str | None = None,
        recent_limit: int = 30,
        use_cache: bool = True,
    ) -> StrategyDataContext:
        cache_key = f"{video_id or '-'}:{recent_limit}"
        cached = _CACHE.get(cache_key)
        if use_cache and cached and time.monotonic() - cached[0] <= _CACHE_TTL_SECONDS:
            return cached[1]

        knowledge_text, knowledge_flags, missing = load_strategy_knowledge()
        missing_sources = [_missing_entry(item, "missing_seed") for item in missing]
        applied_sources = [
            f"knowledge:{filename}"
            for key, filename in KNOWLEDGE_FILES.items()
            if knowledge_flags.get(key)
        ]
        status = "available"
        channel: dict[str, Any] = {}
        recent: list[dict[str, Any]] = []
        retention: list[dict[str, Any]] = []
        ctr_available = False
        config = self.analytics.configuration_status()

        if not config["configured"]:
            status = "setup_required"
            missing_sources.append(
                _missing_entry(
                    "youtube_analytics_api_v2",
                    "missing_env",
                    ",".join(config["missing"]),
                )
            )
        else:
            end_date = date.today().isoformat()
            start_date = (date.today() - timedelta(days=90)).isoformat()
            try:
                uploads = await self.analytics.get_recent_upload_videos(limit=recent_limit)
                overview_task = asyncio.create_task(
                    self.analytics.get_channel_snapshot(
                        start_date=start_date, end_date=end_date
                    )
                )
                video_task = asyncio.create_task(
                    self.analytics.get_video_analytics(
                        start_date=start_date,
                        end_date=end_date,
                        video_ids=[row["video_id"] for row in uploads],
                    )
                ) if uploads else None
                overview_payload = await overview_task
                metrics = await video_task if video_task else []
                channel = _channel_snapshot(overview_payload)
                metric_by_id = {row["video_id"]: row for row in metrics}
                reach = self.repository.get_reach_for_videos(
                    [row["video_id"] for row in uploads]
                )
                for upload in uploads:
                    row = {**upload, **(metric_by_id.get(upload["video_id"]) or {})}
                    reach_row = reach.get(upload["video_id"])
                    if reach_row:
                        row["reach"] = reach_row
                        ctr_available = ctr_available or (
                            (reach_row.get("thumbnail_ctr") or {}).get("value") is not None
                        )
                    recent.append(row)
                applied_sources.extend(
                    ["youtube_data_api_v3:oauth_uploads", "youtube_analytics_api_v2"]
                )
                if reach:
                    applied_sources.append("youtube_reporting_api:cached_reach_snapshot")
                if not channel.get("sample_size"):
                    missing_sources.append(_missing_entry("channel_snapshot", "no_data"))
                if not recent:
                    missing_sources.append(_missing_entry("recent_videos", "no_data"))
                if video_id:
                    retention_row = await self.analytics.get_video_retention(
                        video_id, start_date=start_date, end_date=end_date
                    )
                    if retention_row.get("points"):
                        retention.append(retention_row)
                        applied_sources.append("youtube_analytics_api_v2:retention")
                    else:
                        missing_sources.append(
                            _missing_entry("retention", "no_data", str(retention_row.get("status") or "no_data"))
                        )
                else:
                    missing_sources.append(_missing_entry("retention", "video_id_required"))
                if not ctr_available:
                    missing_sources.append(_missing_entry("ctr", "no_data"))
                if not channel.get("sample_size") and not recent:
                    status = "no_data"
            except AnalyticsApiError as exc:
                status = exc.code
                missing_sources.append(
                    _missing_entry("youtube_analytics_api_v2", exc.code)
                )
            except Exception:
                status = "api_error"
                missing_sources.append(
                    _missing_entry("youtube_analytics_api_v2", "api_error")
                )
            finally:
                if self._owns_analytics:
                    await self.analytics.close()

        summary = {
            "provider": "openai",
            "youtube_analytics_applied": bool(channel.get("sample_size") or recent),
            "youtube_analytics_status": status,
            "channel_snapshot_sample_size": int(channel.get("sample_size") or 0),
            "recent_video_sample_size": len(recent),
            "retention_sample_size": len(retention),
            "ctr_available": ctr_available,
            "business_pt_applied": bool(knowledge_flags.get("business_pt")),
            "low_data_applied": bool(knowledge_flags.get("low_data")),
            "brand_strategy_applied": bool(knowledge_flags.get("brand_strategy")),
            "applied_sources": list(dict.fromkeys(applied_sources)),
            "missing_sources": missing_sources,
        }
        evidence = {
            "channel_snapshot_90d": channel,
            "recent_videos_90d": recent[:recent_limit],
            "retention": retention,
            "retrieval_summary": summary,
        }
        result = StrategyDataContext(evidence, summary, knowledge_text)
        _CACHE[cache_key] = (time.monotonic(), result)
        return result


def format_strategy_data_context(context: StrategyDataContext) -> str:
    return (
        "[실시간 YouTube/브랜드 전략 context]\n"
        + json.dumps(context.evidence, ensure_ascii=False, default=str)[:30000]
        + "\n\n[고정 전략 지식]\n"
        + context.knowledge_text[:16000]
    )
