"""Question-scoped retrieval over channel data and the existing application DB."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone
from typing import Any, Callable

from analytics_repository import AnalyticsRepository
from database import get_db
from strategy_repository import StrategyRepository

from .contracts import EvidenceEnvelope
from .tools import ReadOnlyToolRegistry, ToolDefinition


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
            row.pop("derivation_metadata", None)
        return EvidenceEnvelope(
            data=_compact(selected),
            source="youtube_analytics_similarity",
            sample_size=len(selected),
            unavailable_reason=None if selected else "no channel videos are stored yet",
        )

    def get_retention_patterns(self, args: dict[str, Any]) -> EvidenceEnvelope:
        video_id = str(args.get("video_id") or "").strip()
        limit = max(1, min(int(args.get("limit") or 10), 30))
        if video_id:
            candidates = [{"video_id": video_id}]
        else:
            candidates = self.analytics.get_recent_video_metrics(limit=limit)
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

    async def get_recent_trends(self, args: dict[str, Any]) -> EvidenceEnvelope:
        from youtube_service import YouTubeService

        key = os.getenv("YOUTUBE_API_KEY", "").strip()
        if not key:
            return EvidenceEnvelope(data=None, source="youtube_data_api_v3", unavailable_reason="YOUTUBE_API_KEY is not configured")
        query = str(args.get("query") or "").strip()
        days = max(1, min(int(args.get("days") or 90), 730))
        limit = max(1, min(int(args.get("limit") or 12), 25))
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
        return EvidenceEnvelope(data=data, source="youtube_data_api_v3:current_market", collected_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), freshness="live", sample_size=len(data))


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_INT = {"type": ["integer", "null"]}


def build_strategy_tool_registry(retrieval: StrategyRetrieval | None = None) -> ReadOnlyToolRegistry:
    service = retrieval or StrategyRetrieval()
    registry = ReadOnlyToolRegistry()
    definitions = [
        ("get_recent_channel_performance", "최근 부자주방 영상의 조회수·CTR·시청시간·평균시청률·구독 전환과 데이터 최신성을 조회한다. 채널 방향이나 다음 주제를 판단할 때 가장 먼저 사용한다.", _object_schema({"days": NULLABLE_INT, "limit": {"type": "integer"}}), service.get_recent_channel_performance),
        ("get_video_performance", "영상 ID 또는 제목으로 한 영상의 과거 snapshot 변화, CTR, retention 100-point curve를 조회한다.", _object_schema({"video_id": NULLABLE_STRING, "query": NULLABLE_STRING}), service.get_video_performance),
        ("compare_similar_videos", "새 주제와 제목이 비슷한 과거 부자주방 영상들을 찾아 성과를 비교한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.compare_similar_videos),
        ("get_retention_patterns", "최근 영상 또는 지정 영상의 초반 유지율, 급락 구간, audience/relative retention 패턴을 조회한다.", _object_schema({"video_id": NULLABLE_STRING, "limit": {"type": "integer"}}), service.get_retention_patterns),
        ("search_knowledge", "지식 저장소에서 이번 판단에 관련된 원칙과 강의만 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_knowledge),
        ("search_business_pt_knowledge", "비즈니스PT에서 공부해 저장한 지식 중 이번 주제와 관련된 것만 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_business_pt_knowledge),
        ("search_previous_plans", "과거 미드폼·숏폼·기획과 공통 전략 context를 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_previous_plans),
        ("search_previous_worksheets", "과거 촬영 워크시트에서 비슷한 주제와 실행안을 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_previous_worksheets),
        ("get_content_pipeline", "현재 콘텐츠 파이프라인을 조회해 중복 기획과 제작 병목을 확인한다.", _object_schema({"status": NULLABLE_STRING, "limit": {"type": "integer"}}), service.get_content_pipeline),
        ("search_feedback_history", "영상·편집 피드백에서 반복된 성공/실패 요인을 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_feedback_history),
        ("search_chat_memory", "이전 AI 상담에서 사용자가 내린 결정과 가설을 검색한다.", _object_schema({"query": {"type": "string"}, "limit": {"type": "integer"}}), service.search_chat_memory),
        ("get_recent_trends", "필요할 때만 현재 YouTube 검색/트렌드 데이터를 조회한다. 채널 내부 근거가 먼저다.", _object_schema({"query": NULLABLE_STRING, "days": {"type": "integer"}, "limit": {"type": "integer"}}), service.get_recent_trends),
    ]
    for name, description, schema, handler in definitions:
        registry.register(ToolDefinition(name=name, description=description, parameters=schema, handler=handler))
    return registry
