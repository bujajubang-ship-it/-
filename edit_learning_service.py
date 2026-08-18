"""Measured edit feedback and durable video-specific editing learnings."""

from __future__ import annotations

import statistics
from typing import Any

from analytics_repository import AnalyticsRepository
from edit_project_store import EditProjectStore, utc_now
from strategy_memory import StrategyMemoryRepository


def _numbers(values: list[Any]) -> list[float]:
    output = []
    for value in values:
        try:
            if value is not None:
                output.append(float(value))
        except (TypeError, ValueError):
            continue
    return output


def _median(values: list[Any]) -> float | None:
    normalized = _numbers(values)
    return round(float(statistics.median(normalized)), 4) if normalized else None


def build_editing_benchmarks(evidence: dict[str, Any]) -> dict[str, Any]:
    """Turn raw retrieval envelopes into explicit, non-invented edit rules."""

    retention_rows = ((evidence.get("retention") or {}).get("data") or [])
    similar_rows = ((evidence.get("similar_videos") or {}).get("data") or [])
    knowledge_rows = ((evidence.get("business_pt") or {}).get("data") or [])
    channel = (evidence.get("channel_snapshot") or {}).get("data") or {}

    retention_30s = _median(
        [row.get("retention_30s_estimate") for row in retention_rows]
    )
    opening_relative = _median(
        [row.get("opening_relative_retention_median") for row in retention_rows]
    )
    similar_retention = _median(
        [
            (row.get("retention_summary") or {}).get("retention_30s_estimate")
            for row in similar_rows
        ]
    )
    average_view_percentage = (
        (channel.get("baselines") or {}).get("average_view_percentage_median")
        if isinstance(channel, dict) else None
    )

    strongest = sorted(
        [row for row in retention_rows if row.get("retention_30s_estimate") is not None],
        key=lambda row: float(row["retention_30s_estimate"]),
        reverse=True,
    )[:3]
    weakest = sorted(
        [row for row in retention_rows if row.get("retention_30s_estimate") is not None],
        key=lambda row: float(row["retention_30s_estimate"]),
    )[:3]

    knowledge = []
    for row in knowledge_rows[:6]:
        if not isinstance(row, dict):
            continue
        knowledge.append(
            {
                "id": row.get("id"),
                "title": str(row.get("title") or "")[:200],
                "principle": str(row.get("summary") or row.get("content") or "")[:700],
            }
        )

    rules = []
    if retention_30s is not None:
        rules.append(
            {
                "metric": "channel_retention_30s_median",
                "value": retention_30s,
                "decision": "오프닝 컷은 이 기준보다 높은 유사 영상의 구조를 우선하고, 낮은 패턴은 피한다.",
                "source": "youtube_analytics_retention",
            }
        )
    if opening_relative is not None:
        rules.append(
            {
                "metric": "opening_relative_retention_median",
                "value": opening_relative,
                "decision": "첫 구간의 설명·정적을 줄일 때 채널 상대 retention 기준으로 판단한다.",
                "source": "youtube_analytics_retention",
            }
        )
    if average_view_percentage is not None:
        rules.append(
            {
                "metric": "channel_average_view_percentage_median",
                "value": average_view_percentage,
                "decision": "전체 길이와 후반 축약의 채널 기준선으로 사용한다.",
                "source": "youtube_analytics_snapshots",
            }
        )

    return {
        "retention_sample_size": len(retention_rows),
        "similar_video_sample_size": len(similar_rows),
        "business_pt_sample_size": len(knowledge),
        "retention_30s_median": retention_30s,
        "similar_retention_30s_median": similar_retention,
        "opening_relative_retention_median": opening_relative,
        "average_view_percentage_median": average_view_percentage,
        "strong_openings": [
            {
                "video_id": row.get("video_id"),
                "title": row.get("title"),
                "retention_30s_estimate": row.get("retention_30s_estimate"),
            }
            for row in strongest
        ],
        "weak_openings": [
            {
                "video_id": row.get("video_id"),
                "title": row.get("title"),
                "retention_30s_estimate": row.get("retention_30s_estimate"),
            }
            for row in weakest
        ],
        "business_pt_principles": knowledge,
        "decision_rules": rules,
        "limitations": [
            message
            for condition, message in (
                (not retention_rows, "채널 retention 표본이 없어 초반 컷 기준을 확정할 수 없음"),
                (not similar_rows, "유사 영상 표본이 없어 주제별 비교가 제한됨"),
                (not knowledge, "현재 질문과 일치하는 비즈니스PT 지식이 없음"),
            )
            if condition
        ],
    }


class EditFeedbackService:
    def __init__(
        self,
        *,
        analytics: AnalyticsRepository | None = None,
        memories: StrategyMemoryRepository | None = None,
    ) -> None:
        self.analytics = analytics or AnalyticsRepository()
        self.memories = memories or StrategyMemoryRepository()

    def evaluate(self, project_id: int, project: dict[str, Any]) -> dict[str, Any]:
        link = project.get("upload_feedback") or {}
        video_id = str(link.get("video_id") or "").strip()
        if not video_id:
            return {"status": "not_linked", "message": "업로드한 YouTube video ID가 연결되지 않았습니다."}

        rows = self.analytics.compare_video_performance([video_id])
        retention = self.analytics.get_video_retention(video_id)
        reach = self.analytics.get_reach_for_videos([video_id]).get(video_id)
        if not rows and not retention and not reach:
            return {
                "status": "pending",
                "video_id": video_id,
                "message": "아직 Analytics/retention 데이터가 수집되지 않았습니다. 이전 결과는 유지합니다.",
                "checked_at": utc_now(),
            }

        metrics = rows[0] if rows else {}
        actual_retention = retention.get("retention_30s_estimate") if retention else None
        benchmarks = (project.get("evidence_snapshot") or {}).get("editing_benchmarks") or {}
        baseline_retention = benchmarks.get("retention_30s_median")
        baseline_avp = benchmarks.get("average_view_percentage_median")
        actual_avp = metrics.get("average_view_percentage")

        versions = project.get("plan_versions") or []
        approved = int(project.get("approved_version") or 0)
        version_row = next(
            (row for row in versions if int(row.get("version") or 0) == approved),
            versions[-1] if versions else {},
        )
        plan = version_row.get("plan") or {}
        actions = {str(item.get("action") or "") for item in plan.get("segments") or []}
        outcomes = []
        if actions & {"use_as_hook", "move"}:
            status = "unavailable"
            if actual_retention is not None and baseline_retention is not None:
                status = "effective" if float(actual_retention) >= float(baseline_retention) else "needs_revision"
            outcomes.append(
                {
                    "decision": "opening_hook_move",
                    "status": status,
                    "actual": actual_retention,
                    "baseline": baseline_retention,
                    "reason": "승인안의 훅 이동을 실제 30초 retention과 채널 기준으로 비교",
                }
            )
        if actions & {"cut", "trim", "shorten"}:
            status = "unavailable"
            if actual_avp is not None and baseline_avp is not None:
                status = "effective" if float(actual_avp) >= float(baseline_avp) else "needs_revision"
            outcomes.append(
                {
                    "decision": "pace_and_length_reduction",
                    "status": status,
                    "actual": actual_avp,
                    "baseline": baseline_avp,
                    "reason": "컷·축약 판단을 실제 평균시청률과 채널 기준으로 비교",
                }
            )

        positives = sum(item["status"] == "effective" for item in outcomes)
        risks = sum(item["status"] == "needs_revision" for item in outcomes)
        summary = (
            f"승인 편집 판단 {len(outcomes)}개 중 효과 확인 {positives}개, 재검토 {risks}개."
            if outcomes else "비교 가능한 편집 판단이 아직 없습니다."
        )
        measured = {
            "status": "measured",
            "video_id": video_id,
            "checked_at": utc_now(),
            "source_as_of": metrics.get("data_through") or (retention or {}).get("data_through") or (reach or {}).get("source_as_of"),
            "actual": {
                "views": metrics.get("views"),
                "average_view_percentage": actual_avp,
                "retention_30s_estimate": actual_retention,
                "thumbnail_ctr": ((reach or {}).get("thumbnail_ctr") or {}).get("value"),
                "thumbnail_impressions": ((reach or {}).get("thumbnail_impressions") or {}).get("value"),
            },
            "baseline": {
                "average_view_percentage": baseline_avp,
                "retention_30s_estimate": baseline_retention,
            },
            "decision_outcomes": outcomes,
            "summary": summary,
        }
        related = {
            "edit_project_id": project_id,
            "video_id": video_id,
            "content_strategy_id": (project.get("settings") or {}).get("content_strategy_id"),
            "approved_version": approved,
            "source_as_of": measured["source_as_of"],
        }
        memory_id = self.memories.record(
            memory_type="edit_learning",
            content=f"영상 {video_id} 편집 성과 학습: {summary}",
            evidence=[
                {"source": "youtube_analytics_retention", "source_as_of": measured["source_as_of"]},
                {"source": "edit_project", "project_id": project_id, "version": approved},
            ],
            related=related,
            confidence=0.9 if outcomes and all(item["status"] != "unavailable" for item in outcomes) else 0.65,
        )
        measured["memory_id"] = memory_id
        return measured


def record_approved_edit_memory(project_id: int, project: dict[str, Any]) -> int | None:
    versions = project.get("plan_versions") or []
    approved = int(project.get("approved_version") or 0)
    row = next((item for item in versions if int(item.get("version") or 0) == approved), None)
    if not row:
        return None
    plan = row.get("plan") or {}
    topic = str((project.get("settings") or {}).get("topic") or "편집 프로젝트")
    actions = sorted({str(item.get("action") or "") for item in plan.get("segments") or [] if item.get("action")})
    return StrategyMemoryRepository().record(
        memory_type="edit_decision",
        content=f"{topic} 편집안 v{approved} 승인: {plan.get('recommended_direction') or ''} | actions={','.join(actions)}",
        evidence=(project.get("evidence_trace") or [])[:12],
        related={
            "edit_project_id": project_id,
            "content_strategy_id": (project.get("settings") or {}).get("content_strategy_id"),
            "approved_version": approved,
            "video_type": (project.get("settings") or {}).get("video_type"),
        },
        confidence=0.85,
    )


def refresh_linked_edit_feedback(*, limit: int = 100) -> dict[str, int]:
    store = EditProjectStore()
    service = EditFeedbackService()
    measured = pending = errors = 0
    for row in store.linked_projects(limit=limit):
        project = row["report"]
        try:
            result = service.evaluate(int(row["id"]), project)
            previous = project.get("upload_feedback") or {}
            history = previous.get("comparisons") or []
            fingerprint = (result.get("source_as_of"), result.get("status"))
            if not history or (history[-1].get("source_as_of"), history[-1].get("status")) != fingerprint:
                history = history + [result]
            project["upload_feedback"] = {
                **previous,
                "latest_comparison": result,
                "comparisons": history[-40:],
            }
            store.save(int(row["id"]), project)
            if result.get("status") == "measured": measured += 1
            else: pending += 1
        except Exception:
            errors += 1
    return {"measured": measured, "pending": pending, "errors": errors}
