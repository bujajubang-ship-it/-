"""Question-scoped retrieval over channel data and the existing application DB."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
import threading
import time
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Any, Callable

from analytics_repository import AnalyticsRepository
from database import get_db
from strategy_repository import StrategyRepository
from strategy_memory import StrategyMemoryRepository

from .contracts import EvidenceEnvelope
from .tools import ReadOnlyToolRegistry, ToolDefinition


_TREND_CACHE: dict[tuple[str, int, int], tuple[float, EvidenceEnvelope]] = {}
_TREND_CACHE_TTL_SECONDS = 15 * 60


def _tokens(value: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[0-9A-Za-z가-힣]+", value) if len(token) > 1]


def _score(query: str, *values: Any) -> int:
    if not query.strip():
        return 1
    haystack = " ".join(str(value or "") for value in values).lower()
    score = 10 if query.lower() in haystack else 0
    return score + sum(1 for token in _tokens(query) if token in haystack)


def _load(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return default


def _compact(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return str(value)[:500]
    if isinstance(value, str):
        return value[:2500]
    if isinstance(value, list):
        return [_compact(item, depth=depth + 1) for item in value[:15]]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _compact(item, depth=depth + 1)
            for key, item in list(value.items())[:40]
        }
    return value


def _freshness(data_through: str | None) -> str:
    if not data_through:
        return "unknown"
    try:
        lag = (date.today() - date.fromisoformat(data_through[:10])).days
    except ValueError:
        return "unknown"
    if lag <= 2:
        return "current"
    if lag <= 7:
        return f"delayed_{lag}_days"
    return f"stale_{lag}_days"


def _median(values: list[float | int | None]) -> float | None:
    available = [float(value) for value in values if value is not None]
    return round(statistics.median(available), 3) if available else None


def _weighted_plain(rows: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in rows
        if row.get("thumbnail_impressions") is not None
        and row.get("thumbnail_ctr_percent") is not None
        and int(row.get("thumbnail_impressions") or 0) >= 0
    ]
    impressions = sum(int(row["thumbnail_impressions"]) for row in usable)
    if impressions <= 0:
        return {"ctr_percent": None, "impressions": impressions, "days": len(usable)}
    ctr = sum(
        int(row["thumbnail_impressions"]) * float(row["thumbnail_ctr_percent"])
        for row in usable
    ) / impressions
    return {
        "ctr_percent": round(ctr, 3),
        "impressions": impressions,
        "days": len(usable),
    }


def _title_archetypes(title: str) -> list[str]:
    lowered = title.lower()
    patterns = {
        "problem_conflict": r"절대|후회|실패|문제|모르면|하지\s*마|안\s*되는|망하|낭비",
        "comparison": r"\bvs\b|비교|보다|차이|대신",
        "number": r"\d+(?:평|개|가지|만원|초|분|%)?",
        "result_solution": r"방법|해결|완성|살리는|바꾸|정리|고르는\s*법|동선",
        "field_case": r"현장|설치|실제|주방|식당|베이커리|카페",
        "product_focus": r"냉장고|냉동고|오븐|식기세척기|세척기|후드|싱크|작업대|쇼케이스|제품|기계",
        "question": r"\?|왜|어떻게|뭘|무엇",
    }
    matched = [name for name, pattern in patterns.items() if re.search(pattern, lowered)]
    return matched or ["plain_information"]


def _thumbnail_archetypes(text: str, strategy: Any = None) -> dict[str, Any]:
    """Classify only saved thumbnail direction; never infer from an unseen image."""

    raw = f"{text} {json.dumps(_load(strategy, {}), ensure_ascii=False)}".lower()
    tags: list[str] = []
    patterns = {
        "field_wide_shot": r"현장\s*(전체|전경)|전체컷|주방\s*전체|와이드",
        "product_only": r"제품\s*(단독|만)|제품컷|기계\s*(단독|만)",
        "before_after": r"before|after|비포|애프터|전후|바꾸기\s*전|바꾼\s*후",
        "person_included": r"사람|사장|대표|얼굴|인물|직원",
        "problem_scene": r"문제|막히|좁|실패|불편|엉키|위험|후회",
        "number_emphasis": r"\d+(?:평|개|만원|%|초|분)?",
    }
    for name, pattern in patterns.items():
        if re.search(pattern, raw):
            tags.append(name)
    visible_text = str(text or "").strip()
    text_length = len(re.sub(r"\s+", "", visible_text))
    return {
        "archetypes": tags or ["unclassified"],
        "text_length": text_length,
        "text_length_bucket": (
            "none" if not visible_text else "short" if text_length <= 8 else "medium" if text_length <= 14 else "long"
        ),
        "classification": "estimated_from_saved_text_and_strategy",
        "uncertain": True,
    }


class StrategyRetrieval:
    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection] | None = None,
        *,
        analytics: AnalyticsRepository | None = None,
        strategies: StrategyRepository | None = None,
    ) -> None:
        self._connect = connect or get_db
        self.analytics = analytics or AnalyticsRepository(self._connect)
        self.strategies = strategies or StrategyRepository(self._connect)
        self._ctr_cache: tuple[float, dict[str, Any]] | None = None
        self._ctr_cache_lock = threading.Lock()

    def _rows(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(query, params).fetchall()
            except sqlite3.OperationalError as exc:
                # Optional legacy tables are created lazily in local installs.
                if "no such table" in str(exc).lower():
                    return []
                raise
            return [dict(row) for row in rows]

    def get_recent_channel_performance(self, args: dict[str, Any]) -> EvidenceEnvelope:
        limit = max(1, min(int(args.get("limit") or 20), 100))
        days = args.get("days")
        rows = self.analytics.get_recent_video_metrics(limit=limit * 3)
        if days:
            cutoff = date.today().toordinal() - int(days)
            rows = [
                row for row in rows
                if row.get("published_at")
                and date.fromisoformat(str(row["published_at"])[:10]).toordinal() >= cutoff
            ]
        rows = rows[:limit]
        reach = self.analytics.get_reach_for_videos([row["video_id"] for row in rows])
        for row in rows:
            row["reach"] = reach.get(row["video_id"])
            row.pop("derivation_metadata", None)
        through = max((row.get("data_through") for row in rows if row.get("data_through")), default=None)
        return EvidenceEnvelope(
            data=_compact(rows),
            source="youtube_analytics_snapshots+youtube_reporting_reach",
            collected_at=max((row.get("metrics_collected_at") for row in rows if row.get("metrics_collected_at")), default=None),
            period={"start": min((row.get("published_at") for row in rows if row.get("published_at")), default=None), "end": through},
            freshness=_freshness(through),
            sample_size=len(rows),
        )

    def get_video_performance(self, args: dict[str, Any]) -> EvidenceEnvelope:
        video_id = str(args.get("video_id") or "").strip()
        query = str(args.get("query") or "").strip()
        if not video_id and query:
            candidates = self.analytics.get_recent_video_metrics(limit=500)
            ranked = sorted(
                ((_score(query, row.get("title")), row) for row in candidates),
                key=lambda item: item[0], reverse=True,
            )
            if ranked and ranked[0][0] > 0:
                video_id = str(ranked[0][1]["video_id"])
        if not video_id:
            return EvidenceEnvelope(data=None, source="youtube_analytics_snapshots", unavailable_reason="video_id or matching title is required")
        rows = self.analytics.compare_video_performance([video_id])
        if not rows:
            return EvidenceEnvelope(data=None, source="youtube_analytics_snapshots", unavailable_reason="video not found")
        history = self.analytics.get_video_metric_history(video_id, limit=30)
        retention = self.analytics.get_video_retention(video_id)
        reach = self.analytics.get_reach_for_videos([video_id]).get(video_id)
        latest = rows[0]
        return EvidenceEnvelope(
            data=_compact({"video": latest, "snapshots": history, "retention": retention, "reach": reach}),
            source="youtube_analytics+retention+reporting",
            collected_at=latest.get("metrics_collected_at"),
            period={"start": latest.get("period_start"), "end": latest.get("data_through")},
            freshness=_freshness(latest.get("data_through")),
            sample_size=len(history),
        )

    def compare_similar_videos(self, args: dict[str, Any]) -> EvidenceEnvelope:
        query = str(args.get("query") or "").strip()
        limit = max(2, min(int(args.get("limit") or 8), 20))
        candidates = self.analytics.get_recent_video_metrics(limit=500)
        ranked = sorted(
            ((_score(query, row.get("title")), row) for row in candidates),
            key=lambda item: (item[0], item[1].get("views") or -1), reverse=True,
        )
        selected = [row for score, row in ranked if score > 0][:limit]
        if not selected and query:
            selected = candidates[:limit]
        reach = self.analytics.get_reach_for_videos([row["video_id"] for row in selected])
        for row in selected:
            row["match_score"] = _score(query, row.get("title"))
            row["reach"] = reach.get(row["video_id"])
            retention = self.analytics.get_video_retention(str(row["video_id"]))
            row["retention_summary"] = (
                {
                    "retention_30s_estimate": retention.get("retention_30s_estimate"),
                    "relative_retention_median": _median(
                        [
                            point.get("relative_retention_performance")
                            for point in retention.get("points") or []
                        ]
                    ),
                    "data_through": retention.get("data_through"),
                }
                if retention else None
            )
            row["title_archetypes"] = _title_archetypes(str(row.get("title") or ""))
            row.pop("derivation_metadata", None)
        return EvidenceEnvelope(
            data=_compact(selected),
            source="youtube_analytics_similarity",
            sample_size=len(selected),
            unavailable_reason=None if selected else "no channel videos are stored yet",
        )

    def get_channel_strategy_snapshot(self, args: dict[str, Any]) -> EvidenceEnvelope:
        """Return a decision-ready baseline, not an unranked metric dump."""

        limit = max(10, min(int(args.get("limit") or 20), 100))
        all_rows = self.analytics.get_recent_video_metrics(limit=max(limit, 300))
        rows = all_rows[:limit]
        reach = self.analytics.get_reach_for_videos(
            [str(row["video_id"]) for row in rows]
        )
        for row in rows:
            row["reach"] = reach.get(str(row["video_id"]))
            row["subscriber_net"] = (
                (row.get("subscribers_gained") or 0) - (row.get("subscribers_lost") or 0)
                if row.get("subscribers_gained") is not None
                or row.get("subscribers_lost") is not None
                else None
            )
            row["title_archetypes"] = _title_archetypes(str(row.get("title") or ""))
            row.pop("derivation_metadata", None)
        baselines = {
            "views_median": _median([row.get("views") for row in rows]),
            "average_view_percentage_median": _median(
                [row.get("average_view_percentage") for row in rows]
            ),
            "average_view_duration_median": _median(
                [row.get("average_view_duration") for row in rows]
            ),
            "subscriber_net_median": _median([row.get("subscriber_net") for row in rows]),
            "thumbnail_ctr_median": _median(
                [
                    (row.get("reach") or {}).get("thumbnail_ctr", {}).get("value")
                    for row in rows
                ]
            ),
        }
        ranked = sorted(
            rows,
            key=lambda row: (
                row.get("views") is not None,
                row.get("views") or -1,
                row.get("average_view_percentage") or -1,
            ),
            reverse=True,
        )
        failures = sorted(
            rows,
            key=lambda row: (
                row.get("views") is None,
                row.get("views") if row.get("views") is not None else float("inf"),
                row.get("average_view_percentage") if row.get("average_view_percentage") is not None else float("inf"),
            ),
        )
        stop = {"부자주방", "업소용", "주방", "하는", "이렇게", "그리고", "영상"}
        topic_counts = Counter(
            token
            for row in rows[:20]
            for token in _tokens(str(row.get("title") or ""))
            if token not in stop and not token.isdigit()
        )
        through = max(
            (row.get("data_through") for row in rows if row.get("data_through")),
            default=None,
        )
        current_month = date.today().month
        same_season = []
        for historical in all_rows:
            try:
                published = date.fromisoformat(str(historical.get("published_at") or "")[:10])
            except ValueError:
                continue
            if published.month == current_month and published.year < date.today().year:
                same_season.append(historical)
        same_season.sort(key=lambda row: row.get("views") or -1, reverse=True)
        return EvidenceEnvelope(
            data=_compact(
                {
                    "baseline": baselines,
                    "recent_videos": rows[:20],
                    "top_performers": ranked[:5],
                    "underperformers": failures[:5],
                    "recent_topic_frequency": topic_counts.most_common(12),
                    "seasonality": {
                        "current_month": current_month,
                        "same_month_historical_top": same_season[:5],
                    },
                    "comparison_rule": "최근 표본 중앙값과 같은 수집 시점의 영상 성과를 비교",
                }
            ),
            source="youtube_analytics:channel_strategy_snapshot",
            collected_at=max(
                (row.get("metrics_collected_at") for row in rows if row.get("metrics_collected_at")),
                default=None,
            ),
            period={
                "start": min((row.get("published_at") for row in rows if row.get("published_at")), default=None),
                "end": through,
            },
            freshness=_freshness(through),
            sample_size=len(rows),
            unavailable_reason=None if rows else "no collected channel performance",
        )

    def _ctr_dataset(self, *, limit: int = 300) -> dict[str, Any]:
        """Build one shared CTR/retention dataset per chat turn.

        All CTR tools reuse this result, avoiding repeated SQLite scans when the
        intent-specific prefetch runs them in parallel.
        """

        with self._ctr_cache_lock:
            if self._ctr_cache and time.monotonic() - self._ctr_cache[0] < 120:
                return self._ctr_cache[1]

            metrics = self.analytics.get_recent_video_metrics(limit=limit)
            ids = [str(row["video_id"]) for row in metrics]
            reach_rows = self.analytics.get_reach_history(ids)
            by_video: dict[str, list[dict[str, Any]]] = {}
            for row in reach_rows:
                if (
                    row.get("impressions_status") == "available"
                    and row.get("ctr_status") == "available"
                ):
                    by_video.setdefault(str(row["video_id"]), []).append(row)

            retention_rows = self._rows(
                """
                SELECT r.video_id,r.retention_30s_estimate,r.data_through,
                       r.collected_at,r.point_count
                FROM video_retention_snapshots r
                WHERE r.id=(
                    SELECT r2.id FROM video_retention_snapshots r2
                    WHERE r2.video_id=r.video_id
                    ORDER BY r2.collected_at DESC,r2.id DESC LIMIT 1
                )
                """
            )
            retention = {str(row["video_id"]): row for row in retention_rows}
            videos: list[dict[str, Any]] = []
            milestones = (1, 3, 7, 14, 30)
            for metric in metrics:
                video_id = str(metric["video_id"])
                daily = sorted(by_video.get(video_id, []), key=lambda row: row["metric_date"])
                if not daily:
                    continue
                lifecycle: dict[str, Any] = {}
                published = None
                try:
                    published = date.fromisoformat(str(metric.get("published_at") or "")[:10])
                except ValueError:
                    pass
                if published:
                    aged: list[tuple[int, dict[str, Any]]] = []
                    for row in daily:
                        try:
                            age = (date.fromisoformat(str(row["metric_date"])[:10]) - published).days
                        except ValueError:
                            continue
                        if age >= 0:
                            aged.append((age, row))
                    for day in milestones:
                        window = [row for age, row in aged if age <= day]
                        if window:
                            lifecycle[f"D{day}"] = _weighted_plain(window)
                first_available = _weighted_plain(daily[:3])
                recent = _weighted_plain(daily[-7:])
                long = _weighted_plain(daily)
                retention_item = retention.get(video_id) or {}
                views_in_period = sum(
                    int(row["daily_views"])
                    for row in daily
                    if row.get("daily_views") is not None
                )
                videos.append(
                    {
                        "video_id": video_id,
                        "title": metric.get("title"),
                        "published_at": metric.get("published_at"),
                        "views": metric.get("views"),
                        "views_in_reach_period": views_in_period,
                        "average_view_percentage": metric.get("average_view_percentage"),
                        "retention_30s_estimate": retention_item.get("retention_30s_estimate"),
                        "retention_source_as_of": retention_item.get("data_through"),
                        "impressions": long["impressions"],
                        "ctr_percent": long["ctr_percent"],
                        "estimated_clicks": (
                            round(long["impressions"] * long["ctr_percent"] / 100)
                            if long["ctr_percent"] is not None else None
                        ),
                        "first_available_3_days": first_available,
                        "recent_7_available_days": recent,
                        "ctr_change_percentage_points": (
                            round(recent["ctr_percent"] - first_available["ctr_percent"], 3)
                            if recent["ctr_percent"] is not None
                            and first_available["ctr_percent"] is not None
                            else None
                        ),
                        "lifecycle": lifecycle,
                        "reach_period": {
                            "start": daily[0]["metric_date"],
                            "end": daily[-1]["metric_date"],
                            "available_days": len(daily),
                        },
                        "report_generated_at": max(
                            (row.get("report_generated_at") for row in daily if row.get("report_generated_at")),
                            default=None,
                        ),
                        "collected_at": max(
                            (row.get("collected_at") for row in daily if row.get("collected_at")),
                            default=None,
                        ),
                        "title_archetypes": _title_archetypes(str(metric.get("title") or "")),
                    }
                )
            channel = _weighted_plain(reach_rows)
            result = {
                "status": "available" if videos else "not_ready",
                "channel_weighted_ctr_percent": channel["ctr_percent"],
                "channel_impressions": channel["impressions"],
                "channel_ctr_sample_days": channel["days"],
                "ctr_median_percent": _median([row.get("ctr_percent") for row in videos]),
                "impressions_median": _median([row.get("impressions") for row in videos]),
                "retention_30s_median": _median(
                    [row.get("retention_30s_estimate") for row in videos]
                ),
                "average_view_percentage_median": _median(
                    [row.get("average_view_percentage") for row in videos]
                ),
                "source_as_of": max(
                    (str(row.get("metric_date")) for row in reach_rows if row.get("metric_date")),
                    default=None,
                ),
                "collected_at": max(
                    (str(row.get("collected_at")) for row in reach_rows if row.get("collected_at")),
                    default=None,
                ),
                "video_count": len(videos),
                "videos": videos,
                "calculation_note": (
                    "CTR is the official Reporting percentage weighted by official impressions; "
                    "estimated_clicks is impressions×CTR only, never views/impressions."
                ),
            }
            self._ctr_cache = (time.monotonic(), result)
            return result

    def get_ctr_performance(self, args: dict[str, Any]) -> EvidenceEnvelope:
        limit = max(5, min(int(args.get("limit") or 100), 300))
        dataset = self._ctr_dataset(limit=max(limit, 300))
        rows = dataset["videos"][:limit]
        ranked = sorted(rows, key=lambda row: row.get("ctr_percent") or -1, reverse=True)
        data = {
            key: value for key, value in dataset.items() if key != "videos"
        }
        data["highest_ctr"] = ranked[:10]
        data["lowest_ctr"] = list(reversed(ranked[-10:])) if ranked else []
        data["recent_videos"] = rows[:20]
        return EvidenceEnvelope(
            data=_compact(data),
            source="youtube_reporting_api:channel_reach_basic_a1+youtube_analytics",
            collected_at=dataset.get("collected_at"),
            period={"start": None, "end": dataset.get("source_as_of")},
            freshness=_freshness(dataset.get("source_as_of")),
            sample_size=dataset.get("video_count", 0),
            unavailable_reason=(
                None if dataset.get("video_count") else
                "Reach report has not produced importable CTR rows yet; previous data was not replaced"
            ),
        )

    def compare_title_patterns(self, args: dict[str, Any]) -> EvidenceEnvelope:
        dataset = self._ctr_dataset()
        query = str(args.get("query") or "").strip()
        videos = dataset["videos"]
        if query:
            matched = [row for row in videos if _score(query, row.get("title")) > 0]
            if matched:
                videos = matched
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in videos:
            for pattern in row.get("title_archetypes") or ["plain_information"]:
                groups.setdefault(pattern, []).append(row)
        summaries = []
        for pattern, rows in groups.items():
            summaries.append(
                {
                    "pattern": pattern,
                    "sample_size": len(rows),
                    "ctr_median_percent": _median([row.get("ctr_percent") for row in rows]),
                    "impressions_median": _median([row.get("impressions") for row in rows]),
                    "views_median": _median([row.get("views") for row in rows]),
                    "retention_30s_median": _median([row.get("retention_30s_estimate") for row in rows]),
                    "average_view_percentage_median": _median([row.get("average_view_percentage") for row in rows]),
                    "confidence": "directional" if len(rows) < 5 else "moderate" if len(rows) < 12 else "stronger",
                    "examples": [
                        {"video_id": row["video_id"], "title": row["title"], "ctr_percent": row["ctr_percent"]}
                        for row in sorted(rows, key=lambda item: item.get("ctr_percent") or -1, reverse=True)[:3]
                    ],
                }
            )
        summaries.sort(
            key=lambda item: (item["ctr_median_percent"] is not None, item["ctr_median_percent"] or -1),
            reverse=True,
        )
        return EvidenceEnvelope(
            data=_compact({"patterns": summaries, "channel_ctr_percent": dataset.get("channel_weighted_ctr_percent")}),
            source="youtube_reporting_api+youtube_videos:title_pattern_classification",
            collected_at=dataset.get("collected_at"),
            freshness=_freshness(dataset.get("source_as_of")),
            sample_size=len(videos),
            unavailable_reason=None if videos else "no Reach rows are available for title comparison",
        )

    def compare_thumbnail_patterns(self, args: dict[str, Any]) -> EvidenceEnvelope:
        dataset = self._ctr_dataset()
        by_id = {row["video_id"]: row for row in dataset["videos"]}
        links = self._rows(
            """
            SELECT l.video_id,l.thumbnail_text,l.title_at_upload,l.linked_at,
                   c.strategy_json
            FROM strategy_video_links l
            JOIN content_strategies c ON c.id=l.strategy_id
            WHERE COALESCE(l.thumbnail_text,'')<>''
            ORDER BY l.linked_at DESC LIMIT 300
            """
        )
        classified = []
        for link in links:
            performance = by_id.get(str(link.get("video_id")))
            if not performance:
                continue
            classification = _thumbnail_archetypes(
                str(link.get("thumbnail_text") or ""), link.get("strategy_json")
            )
            classified.append({**link, **classification, "performance": performance})
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in classified:
            for pattern in row["archetypes"]:
                groups.setdefault(pattern, []).append(row)
        patterns = [
            {
                "pattern": pattern,
                "sample_size": len(rows),
                "ctr_median_percent": _median(
                    [row["performance"].get("ctr_percent") for row in rows]
                ),
                "impressions_median": _median(
                    [row["performance"].get("impressions") for row in rows]
                ),
                "retention_30s_median": _median(
                    [row["performance"].get("retention_30s_estimate") for row in rows]
                ),
                "classification": "estimated_from_saved_text_and_strategy",
            }
            for pattern, rows in groups.items()
        ]
        patterns.sort(key=lambda row: row.get("ctr_median_percent") or -1, reverse=True)
        return EvidenceEnvelope(
            data=_compact({"patterns": patterns, "classified_examples": classified[:12], "uncertainty": "No thumbnail pixels were inspected; categories are estimates from saved thumbnail text/direction."}),
            source="strategy_thumbnail_history+youtube_reporting_api:estimated_patterns",
            collected_at=dataset.get("collected_at"),
            freshness=_freshness(dataset.get("source_as_of")),
            sample_size=len(classified),
            unavailable_reason=None if classified else "no saved thumbnail direction is linked to a video with Reach data",
        )

    def _ctr_retention_segment(self, *, high_retention: bool) -> EvidenceEnvelope:
        dataset = self._ctr_dataset()
        ctr_median = dataset.get("ctr_median_percent")
        retention_median = dataset.get("retention_30s_median")
        use_average = retention_median is None
        if use_average:
            retention_median = dataset.get("average_view_percentage_median")
        selected = []
        if ctr_median is not None and retention_median is not None:
            for row in dataset["videos"]:
                retention = (
                    row.get("average_view_percentage") if use_average
                    else row.get("retention_30s_estimate")
                )
                if row.get("ctr_percent") is None or retention is None:
                    continue
                matches_retention = retention >= retention_median if high_retention else retention < retention_median
                if row["ctr_percent"] >= ctr_median and matches_retention:
                    selected.append(row)
        selected.sort(key=lambda row: (row.get("ctr_percent") or -1, row.get("impressions") or -1), reverse=True)
        label = "high_ctr_high_retention" if high_retention else "high_ctr_low_retention"
        return EvidenceEnvelope(
            data=_compact({
                "segment": label,
                "ctr_threshold_percent": ctr_median,
                "retention_threshold": retention_median,
                "retention_metric": "average_view_percentage" if use_average else "retention_30s_estimate",
                "videos": selected[:20],
            }),
            source="youtube_reporting_api+youtube_analytics+retention:ctr_retention_segment",
            collected_at=dataset.get("collected_at"),
            freshness=_freshness(dataset.get("source_as_of")),
            sample_size=len(selected),
            unavailable_reason=None if ctr_median is not None and retention_median is not None else "CTR or retention baseline is not available",
        )

    def find_high_ctr_low_retention(self, args: dict[str, Any]) -> EvidenceEnvelope:
        return self._ctr_retention_segment(high_retention=False)

    def find_high_ctr_high_retention(self, args: dict[str, Any]) -> EvidenceEnvelope:
        return self._ctr_retention_segment(high_retention=True)

    def compare_impression_to_click_performance(self, args: dict[str, Any]) -> EvidenceEnvelope:
        dataset = self._ctr_dataset()
        ctr = dataset.get("ctr_median_percent")
        impressions = dataset.get("impressions_median")
        segments = {
            "high_impressions_low_ctr": [],
            "high_impressions_high_ctr": [],
            "low_impressions_high_ctr": [],
        }
        if ctr is not None and impressions is not None:
            for row in dataset["videos"]:
                if row.get("ctr_percent") is None or row.get("impressions") is None:
                    continue
                high_i = row["impressions"] >= impressions
                high_c = row["ctr_percent"] >= ctr
                key = (
                    "high_impressions_high_ctr" if high_i and high_c else
                    "high_impressions_low_ctr" if high_i else
                    "low_impressions_high_ctr" if high_c else None
                )
                if key:
                    segments[key].append(row)
        for values in segments.values():
            values.sort(key=lambda row: row.get("impressions") or -1, reverse=True)
        return EvidenceEnvelope(
            data=_compact({
                "thresholds": {"ctr_median_percent": ctr, "impressions_median": impressions},
                "segments": {key: values[:15] for key, values in segments.items()},
                "click_note": "estimated_clicks derives from official impressions×official CTR; it is not a reported click field.",
            }),
            source="youtube_reporting_api:impression_ctr_matrix+youtube_analytics",
            collected_at=dataset.get("collected_at"),
            freshness=_freshness(dataset.get("source_as_of")),
            sample_size=dataset.get("video_count", 0),
            unavailable_reason=None if ctr is not None and impressions is not None else "Reach baseline is not ready",
        )

    def analyze_title_thumbnail_patterns(self, args: dict[str, Any]) -> EvidenceEnvelope:
        query = str(args.get("query") or "").strip()
        limit = max(10, min(int(args.get("limit") or 100), 300))
        rows = self.analytics.get_recent_video_metrics(limit=limit)
        if query:
            matched = [row for row in rows if _score(query, row.get("title")) > 0]
            if matched:
                rows = matched
        reach = self.analytics.get_reach_for_videos([str(row["video_id"]) for row in rows])
        pattern_rows: dict[str, list[dict[str, Any]]] = {}
        normalized = []
        for row in rows:
            ctr = (reach.get(str(row["video_id"])) or {}).get("thumbnail_ctr", {}).get("value")
            item = {
                "video_id": row.get("video_id"), "title": row.get("title"),
                "published_at": row.get("published_at"), "views": row.get("views"),
                "average_view_percentage": row.get("average_view_percentage"),
                "thumbnail_ctr_percent": ctr,
                "archetypes": _title_archetypes(str(row.get("title") or "")),
            }
            normalized.append(item)
            for archetype in item["archetypes"]:
                pattern_rows.setdefault(archetype, []).append(item)
        summaries = []
        for name, group in pattern_rows.items():
            summaries.append(
                {
                    "pattern": name,
                    "sample_size": len(group),
                    "views_median": _median([item.get("views") for item in group]),
                    "average_view_percentage_median": _median(
                        [item.get("average_view_percentage") for item in group]
                    ),
                    "thumbnail_ctr_median": _median(
                        [item.get("thumbnail_ctr_percent") for item in group]
                    ),
                }
            )
        summaries.sort(
            key=lambda item: (item["thumbnail_ctr_median"] is not None, item["views_median"] or -1),
            reverse=True,
        )
        recent_tokens = Counter(
            token
            for item in normalized[:20]
            for token in _tokens(str(item.get("title") or ""))
            if not token.isdigit()
        )
        thumbnail_rows = self._rows(
            """
            SELECT l.video_id,l.thumbnail_text,l.title_at_upload,l.linked_at,
                   c.topic,c.strategy_json
            FROM strategy_video_links l JOIN content_strategies c ON c.id=l.strategy_id
            WHERE COALESCE(l.thumbnail_text,'')<>'' ORDER BY l.linked_at DESC LIMIT 100
            """
        )
        return EvidenceEnvelope(
            data=_compact(
                {
                    "pattern_performance": summaries,
                    "best_titles": sorted(normalized, key=lambda item: item.get("views") or -1, reverse=True)[:5],
                    "weak_titles": sorted(
                        [item for item in normalized if item.get("views") is not None],
                        key=lambda item: item["views"],
                    )[:5],
                    "recent_repeated_expressions": [
                        {"expression": token, "count": count}
                        for token, count in recent_tokens.most_common(12)
                        if count >= 2
                    ],
                    "thumbnail_history": thumbnail_rows[:15],
                    "ctr_note": "Reporting Reach가 없으면 CTR은 unavailable이며 조회수·시청률로만 판단",
                }
            ),
            source="youtube_analytics+strategy_thumbnail_history:title_patterns",
            sample_size=len(normalized),
            unavailable_reason=None if normalized else "no title performance data",
        )

    def get_retention_patterns(self, args: dict[str, Any]) -> EvidenceEnvelope:
        video_id = str(args.get("video_id") or "").strip()
        limit = max(1, min(int(args.get("limit") or 10), 30))
        if video_id:
            candidates = [{"video_id": video_id}]
        else:
            candidates = self._rows(
                """
                SELECT v.video_id,v.title,v.published_at
                FROM youtube_videos v
                WHERE EXISTS (
                    SELECT 1 FROM video_retention_snapshots r
                    WHERE r.video_id=v.video_id
                )
                ORDER BY v.published_at DESC,v.video_id LIMIT ?
                """,
                (limit,),
            )
        results = []
        for video in candidates[:limit]:
            retention = self.analytics.get_video_retention(str(video["video_id"]))
            if not retention:
                continue
            points = retention.get("points") or []
            dips = []
            for left, right in zip(points, points[1:]):
                delta = (right.get("audience_watch_ratio") or 0) - (left.get("audience_watch_ratio") or 0)
                if delta <= -0.08:
                    dips.append({"at": right.get("elapsed_video_time_ratio"), "drop": round(delta, 4)})
            results.append(
                {
                    "video_id": video["video_id"],
                    "title": video.get("title"),
                    "data_through": retention.get("data_through"),
                    "retention_30s_estimate": retention.get("retention_30s_estimate"),
                    "point_count": retention.get("point_count"),
                    "relative_retention_median": _median(
                        [point.get("relative_retention_performance") for point in points]
                    ),
                    "opening_relative_retention_median": _median(
                        [
                            point.get("relative_retention_performance")
                            for point in points
                            if float(point.get("elapsed_video_time_ratio") or 0) <= 0.15
                        ]
                    ),
                    "weakest_relative_point": min(
                        (
                            {
                                "at": point.get("elapsed_video_time_ratio"),
                                "relative_retention_performance": point.get("relative_retention_performance"),
                            }
                            for point in points
                            if point.get("relative_retention_performance") is not None
                        ),
                        key=lambda item: item["relative_retention_performance"],
                        default=None,
                    ),
                    "notable_dips": dips[:8],
                    "curve": points if video_id else points[:: max(1, len(points) // 20)],
                }
            )
        through = max((row.get("data_through") for row in results if row.get("data_through")), default=None)
        return EvidenceEnvelope(
            data=_compact(results), source="youtube_analytics_retention",
            freshness=_freshness(through), sample_size=len(results),
            unavailable_reason=None if results else "retention has not been collected yet",
        )

    def _search_knowledge(self, query: str, limit: int, *, business_pt: bool) -> EvidenceEnvelope:
        rows = self._rows(
            "SELECT id,title,category,summary,content,created_at FROM knowledge WHERE active=1 ORDER BY id DESC"
        )
        business_markers = ("비즈니스pt", "비즈니스 pt", "business pt", "사업", "비즈니스")
        ranked = []
        for row in rows:
            joined = " ".join(str(row.get(key) or "") for key in ("title", "category", "summary", "content"))
            if business_pt and not any(marker in joined.lower() for marker in business_markers):
                continue
            score = _score(query, joined)
            if score > 0:
                ranked.append((score, row))
        ranked.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
        selected = [_compact(row) for _, row in ranked[:limit]]
        return EvidenceEnvelope(
            data=selected,
            source="knowledge:business_pt" if business_pt else "knowledge",
            sample_size=len(selected),
            unavailable_reason=None if selected else "no matching active knowledge",
        )

    def search_knowledge(self, args: dict[str, Any]) -> EvidenceEnvelope:
        return self._search_knowledge(str(args.get("query") or ""), max(1, min(int(args.get("limit") or 6), 15)), business_pt=False)

    def search_business_pt_knowledge(self, args: dict[str, Any]) -> EvidenceEnvelope:
        return self._search_knowledge(str(args.get("query") or ""), max(1, min(int(args.get("limit") or 6), 15)), business_pt=True)

    def _search_history(self, args: dict[str, Any], types: tuple[str, ...], source: str) -> EvidenceEnvelope:
        query = str(args.get("query") or "").strip()
        limit = max(1, min(int(args.get("limit") or 6), 15))
        placeholders = ",".join("?" for _ in types)
        rows = self._rows(
            f"SELECT id,type,keyword,report,created_at FROM history WHERE type IN ({placeholders}) ORDER BY created_at DESC LIMIT 300",
            types,
        )
        ranked = []
        for row in rows:
            score = _score(query, row.get("keyword"), row.get("report"))
            if score > 0:
                row["report"] = _compact(_load(row["report"], {}))
                ranked.append((score, row))
        ranked.sort(key=lambda item: (item[0], str(item[1].get("created_at"))), reverse=True)
        selected = [row for _, row in ranked[:limit]]
        return EvidenceEnvelope(data=selected, source=source, sample_size=len(selected), unavailable_reason=None if selected else "no matching records")

    def search_previous_plans(self, args: dict[str, Any]) -> EvidenceEnvelope:
        history = self._search_history(args, ("planning", "midform", "shortform", "jjachi", "topic", "intro", "script"), "history:plans")
        strategy_rows = self.strategies.list(limit=int(args.get("limit") or 6), query=str(args.get("query") or ""))
        return EvidenceEnvelope(data={"strategies": _compact(strategy_rows), "legacy_history": history.data}, source="content_strategies+history:plans", sample_size=len(strategy_rows) + int(history.sample_size or 0))

    def search_previous_worksheets(self, args: dict[str, Any]) -> EvidenceEnvelope:
        query = str(args.get("query") or "")
        limit = max(1, min(int(args.get("limit") or 6), 15))
        rows = self._rows("SELECT * FROM worksheet_rows ORDER BY updated_at DESC, id DESC")
        ranked = []
        for row in rows:
            data = _load(row.get("data"), {})
            score = _score(query, data)
            if score > 0:
                row["data"] = _compact(data)
                ranked.append((score, row))
        ranked.sort(key=lambda item: (item[0], item[1]["id"]), reverse=True)
        selected = [row for _, row in ranked[:limit]]
        return EvidenceEnvelope(data=selected, source="worksheet_rows", sample_size=len(selected), unavailable_reason=None if selected else "no matching worksheets")

    def get_content_pipeline(self, args: dict[str, Any]) -> EvidenceEnvelope:
        status = str(args.get("status") or "").strip()
        limit = max(1, min(int(args.get("limit") or 30), 100))
        if status:
            rows = self._rows("SELECT * FROM pipeline WHERE stage=? ORDER BY sort_order,id LIMIT ?", (status, limit))
        else:
            rows = self._rows("SELECT * FROM pipeline ORDER BY sort_order,id LIMIT ?", (limit,))
        return EvidenceEnvelope(data=_compact(rows), source="pipeline", sample_size=len(rows))

    def search_feedback_history(self, args: dict[str, Any]) -> EvidenceEnvelope:
        query = str(args.get("query") or "").strip()
        limit = max(1, min(int(args.get("limit") or 6), 15))
        legacy = self._search_history(
            args, ("video_feedback", "edit"), "history:feedback"
        )
        checkpoint_rows = self._rows(
            """
            SELECT p.strategy_id, p.video_id, p.checkpoint_label, p.measured_at,
                   p.analysis_json, c.topic, c.content_type, c.strategy_json,
                   l.title_at_upload, l.thumbnail_text,
                   s.data_through, s.views, s.likes, s.comments, s.shares,
                   s.subscribers_gained, s.subscribers_lost,
                   s.estimated_minutes_watched, s.average_view_duration,
                   s.average_view_percentage, s.view_growth_per_day,
                   s.subscriber_conversion,
                   r.retention_30s_estimate, r.point_count AS retention_point_count
            FROM performance_checkpoints p
            JOIN content_strategies c ON c.id=p.strategy_id
            JOIN strategy_video_links l
              ON l.strategy_id=p.strategy_id AND l.video_id=p.video_id
            LEFT JOIN video_metric_snapshots s ON s.id=p.metric_snapshot_id
            LEFT JOIN video_retention_snapshots r ON r.id=p.retention_snapshot_id
            ORDER BY p.measured_at DESC LIMIT 300
            """
        )
        ranked = []
        for row in checkpoint_rows:
            score = _score(
                query,
                row.get("topic"),
                row.get("title_at_upload"),
                row.get("thumbnail_text"),
                row.get("strategy_json"),
                row.get("analysis_json"),
            )
            if score <= 0:
                continue
            strategy = _load(row.pop("strategy_json"), {})
            row["planned_title"] = strategy.get("recommended_title")
            row["planned_core_message"] = strategy.get("core_message")
            row["planned_thumbnail"] = strategy.get("thumbnail")
            row["planned_hook"] = strategy.get("hook_5_15s") or strategy.get("hook")
            row["planned_kpis"] = strategy.get("kpis") or []
            row["analysis"] = _load(row.pop("analysis_json"), {})
            computed_metrics = row["analysis"].get("metrics") or {}
            for metric_name in (
                "views", "likes", "comments", "shares", "subscribers_gained",
                "subscribers_lost", "estimated_minutes_watched",
                "average_view_duration", "average_view_percentage",
            ):
                if row.get(metric_name) is None and metric_name in computed_metrics:
                    row[metric_name] = computed_metrics[metric_name]
            row["measurement_source"] = row["analysis"].get("source")
            row["measurement_period"] = row["analysis"].get("period")
            comparisons = []
            for planned in row["planned_kpis"]:
                if not isinstance(planned, dict):
                    continue
                metric_label = str(planned.get("metric") or "")
                target_text = str(planned.get("target") or "")
                target_match = re.search(r"(\d+(?:\.\d+)?)\s*%", target_text)
                target = float(target_match.group(1)) if target_match else None
                actual = None
                actual_name = None
                if any(marker in metric_label for marker in ("평균시청률", "평균 시청률", "average_view_percentage")):
                    actual = row.get("average_view_percentage")
                    actual_name = "average_view_percentage"
                elif any(marker in metric_label for marker in ("retention", "유지율", "초반 이탈")):
                    estimate = row.get("retention_30s_estimate")
                    actual = float(estimate) * 100 if estimate is not None else None
                    actual_name = "retention_30s_estimate_percent"
                elif "CTR" in metric_label.upper():
                    actual_name = "thumbnail_ctr_percent"
                comparisons.append(
                    {
                        "checkpoint": planned.get("checkpoint"),
                        "metric": actual_name or metric_label,
                        "target": target if target is not None else target_text,
                        "actual": actual,
                        "status": (
                            "met" if target is not None and actual is not None and float(actual) >= target
                            else "missed" if target is not None and actual is not None
                            else "unavailable_or_non_numeric_target"
                        ),
                        "decision_rule": planned.get("decision_rule"),
                    }
                )
            row["planned_vs_actual"] = comparisons
            ranked.append((score, row))
        ranked.sort(
            key=lambda item: (item[0], str(item[1].get("measured_at"))),
            reverse=True,
        )
        checkpoints = [_compact(row) for _, row in ranked[:limit]]
        sample_size = len(checkpoints) + int(legacy.sample_size or 0)
        return EvidenceEnvelope(
            data={
                "performance_checkpoints": checkpoints,
                "legacy_feedback": legacy.data,
            },
            source="strategy_performance_checkpoints+history:feedback",
            collected_at=max(
                (str(row.get("measured_at")) for row in checkpoints),
                default=None,
            ),
            sample_size=sample_size,
            unavailable_reason=None if sample_size else "no matching feedback or measured outcomes",
        )

    def search_chat_memory(self, args: dict[str, Any]) -> EvidenceEnvelope:
        query = str(args.get("query") or "")
        limit = max(1, min(int(args.get("limit") or 6), 15))
        rows = self._rows("SELECT id,title,messages,updated_at FROM chat_session ORDER BY updated_at DESC LIMIT 200")
        ranked = []
        for row in rows:
            score = _score(query, row.get("title"), row.get("messages"))
            if score > 0:
                messages = _load(row.pop("messages"), [])
                row["matching_messages"] = _compact(
                    [message for message in messages if _score(query, message) > 0][-8:]
                    or messages[-4:]
                )
                ranked.append((score, row))
        ranked.sort(key=lambda item: (item[0], str(item[1].get("updated_at"))), reverse=True)
        selected = [row for _, row in ranked[:limit]]
        return EvidenceEnvelope(data=selected, source="chat_session", sample_size=len(selected), unavailable_reason=None if selected else "no matching conversations")

    def search_long_term_memory(self, args: dict[str, Any]) -> EvidenceEnvelope:
        query = str(args.get("query") or "")
        limit = max(1, min(int(args.get("limit") or 8), 20))
        rows = StrategyMemoryRepository(self._connect).search(query, limit=limit)
        return EvidenceEnvelope(
            data=_compact(rows),
            source="strategy_memories:active_decisions",
            sample_size=len(rows),
            unavailable_reason=None if rows else "no matching durable decisions yet",
        )

    async def get_recent_trends(self, args: dict[str, Any]) -> EvidenceEnvelope:
        from youtube_service import YouTubeService

        key = os.getenv("YOUTUBE_API_KEY", "").strip()
        if not key:
            return EvidenceEnvelope(data=None, source="youtube_data_api_v3", unavailable_reason="YOUTUBE_API_KEY is not configured")
        query = str(args.get("query") or "").strip()
        days = max(1, min(int(args.get("days") or 90), 730))
        limit = max(1, min(int(args.get("limit") or 12), 25))
        cache_key = (query.lower(), days, limit)
        cached = _TREND_CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] <= _TREND_CACHE_TTL_SECONDS:
            return cached[1]
        service = YouTubeService(key)
        try:
            if query:
                videos = await service.search_advanced(query, days=days, max_results=limit)
            else:
                videos = await service.get_trending(category="26", max_results=limit)
        finally:
            await service.close()
        fields = ("id", "title", "channel", "published_at", "view_count", "like_count", "comment_count", "views_per_day", "view_per_sub", "engage_rate", "duration_sec", "url")
        data = [{key: video.get(key) for key in fields} for video in videos[:limit]]
        result = EvidenceEnvelope(data=data, source="youtube_data_api_v3:current_market", collected_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), freshness="live", sample_size=len(data))
        _TREND_CACHE[cache_key] = (time.monotonic(), result)
        if len(_TREND_CACHE) > 64:
            oldest = min(_TREND_CACHE, key=lambda key: _TREND_CACHE[key][0])
            _TREND_CACHE.pop(oldest, None)
        return result


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_INT = {"type": ["integer", "null"]}


def build_strategy_tool_registry(retrieval: StrategyRetrieval | None = None) -> ReadOnlyToolRegistry:
    service = retrieval or StrategyRetrieval()
    registry = ReadOnlyToolRegistry()
    definitions = [
        ("get_channel_strategy_snapshot", "최근 10~20개 영상 baseline, 성공·실패 영상, 구독자 순증감, 최근 주제 편중을 한 번에 조회한다. 다음 영상과 채널 방향 질문의 첫 근거다.", _object_schema({"limit": {"type": "integer"}}), service.get_channel_strategy_snapshot),
        ("get_recent_channel_performance", "최근 부자주방 영상의 조회수·CTR·시청시간·평균시청률·구독 전환과 데이터 최신성을 조회한다. 채널 방향이나 다음 주제를 판단할 때 가장 먼저 사용한다.", _object_schema({"days": NULLABLE_INT, "limit": {"type": "integer"}}), service.get_recent_channel_performance),
        ("get_video_performance", "영상 ID 또는 제목으로 한 영상의 과거 snapshot 변화, CTR, retention 100-point curve를 조회한다.", _object_schema({"video_id": NULLABLE_STRING, "query": NULLABLE_STRING}), service.get_video_performance),
        ("compare_similar_videos", "새 주제와 제목이 비슷한 과거 부자주방 영상들을 찾아 성과를 비교한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.compare_similar_videos),
        ("get_retention_patterns", "최근 영상 또는 지정 영상의 초반 유지율, 급락 구간, audience/relative retention 패턴을 조회한다.", _object_schema({"video_id": NULLABLE_STRING, "limit": {"type": "integer"}}), service.get_retention_patterns),
        ("analyze_title_thumbnail_patterns", "과거 제목 구조별 조회수·시청률·CTR, 최근 반복 표현과 실제 썸네일 히스토리를 비교한다.", _object_schema({"query": NULLABLE_STRING, "limit": {"type": "integer"}}), service.analyze_title_thumbnail_patterns),
        ("get_ctr_performance", "공식 Reporting Reach의 영상별 impressions·가중 CTR·초기/최근 변화·D1/D3/D7/D14/D30 snapshot을 Analytics 조회수와 함께 조회한다.", _object_schema({"limit": {"type": "integer"}}), service.get_ctr_performance),
        ("compare_title_patterns", "부자주방 제목을 문제형·결과형·숫자형·비교형·현장형·제품형으로 분류하고 실제 CTR·impressions·retention 성과를 비교한다.", _object_schema({"query": NULLABLE_STRING}), service.compare_title_patterns),
        ("compare_thumbnail_patterns", "저장된 실제 썸네일 문구·기획 방향을 분류해 CTR 성과를 비교한다. 이미지 미확인 분류는 추정값임을 명시한다.", _object_schema({}), service.compare_thumbnail_patterns),
        ("find_high_ctr_low_retention", "채널 CTR 중앙값 이상이지만 초반 retention이 낮아 클릭 약속을 회수하지 못한 영상을 찾는다.", _object_schema({}), service.find_high_ctr_low_retention),
        ("find_high_ctr_high_retention", "CTR과 retention이 모두 채널 기준 이상인 재사용 가치가 높은 영상을 찾는다.", _object_schema({}), service.find_high_ctr_high_retention),
        ("compare_impression_to_click_performance", "impressions가 높은데 CTR이 낮은 영상, CTR은 높지만 노출이 작은 영상, 둘 다 높은 영상을 구분한다.", _object_schema({}), service.compare_impression_to_click_performance),
        ("search_knowledge", "지식 저장소에서 이번 판단에 관련된 원칙과 강의만 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_knowledge),
        ("search_business_pt_knowledge", "비즈니스PT에서 공부해 저장한 지식 중 이번 주제와 관련된 것만 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_business_pt_knowledge),
        ("search_previous_plans", "과거 미드폼·숏폼·기획과 공통 전략 context를 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_previous_plans),
        ("search_previous_worksheets", "과거 촬영 워크시트에서 비슷한 주제와 실행안을 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_previous_worksheets),
        ("get_content_pipeline", "현재 콘텐츠 파이프라인을 조회해 중복 기획과 제작 병목을 확인한다.", _object_schema({"status": NULLABLE_STRING, "limit": {"type": "integer"}}), service.get_content_pipeline),
        ("search_feedback_history", "영상·편집 피드백에서 반복된 성공/실패 요인을 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_feedback_history),
        ("search_chat_memory", "이전 AI 상담에서 사용자가 내린 결정과 가설을 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_chat_memory),
        ("search_long_term_memory", "전체 채팅 대신 장기 보존된 사용자 결정·선호·성공·실패·가설만 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_long_term_memory),
        ("get_recent_trends", "필요할 때만 현재 YouTube 검색/트렌드 데이터를 조회한다. 채널 내부 근거가 먼저다.", _object_schema({"query": NULLABLE_STRING, "days": {"type": "integer"}, "limit": {"type": "integer"}}), service.get_recent_trends),
    ]
    for name, description, schema, handler in definitions:
        registry.register(ToolDefinition(name=name, description=description, parameters=schema, handler=handler))
    return registry
