"""Deterministic conversion of AI suggestions into an auditable edit timeline."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


EDIT_ACTIONS = {
    "keep",
    "trim",
    "cut",
    "move",
    "shorten",
    "use_as_hook",
    "use_as_short_clip",
    "add_broll",
    "add_caption_emphasis",
}

ENHANCEMENT_TYPES = {"broll", "caption_emphasis"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if end - start < 0.08:
            continue
        if not merged or start > merged[-1][1] + 0.03:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(round(start, 3), round(end, 3)) for start, end in merged]


def normalize_proposals(segments: list[dict[str, Any]], duration: float) -> list[dict[str, Any]]:
    output = []
    for index, raw in enumerate(segments or []):
        action = str(raw.get("action") or "keep")
        if action not in EDIT_ACTIONS:
            action = "keep"
        start = max(0.0, min(duration, _number(raw.get("start_time"))))
        end = max(start, min(duration, _number(raw.get("end_time"), start)))
        if end - start < 0.08:
            continue
        confidence = max(0.0, min(1.0, _number(raw.get("confidence"), 0.5)))
        output.append(
            {
                "id": str(raw.get("id") or f"segment-{index + 1}"),
                "start_time": round(start, 3),
                "end_time": round(end, 3),
                "action": action,
                "reason": str(raw.get("reason") or "").strip()[:1000],
                "confidence": round(confidence, 3),
                "expected_effect": str(raw.get("expected_effect") or "").strip()[:600],
                "destination": str(raw.get("destination") or "").strip()[:80],
            }
        )
    return sorted(output, key=lambda item: (item["start_time"], item["end_time"], item["id"]))


def normalize_enhancements(
    enhancements: list[dict[str, Any]], duration: float
) -> list[dict[str, Any]]:
    output = []
    for index, raw in enumerate(enhancements or []):
        kind = str(raw.get("type") or "")
        if kind not in ENHANCEMENT_TYPES:
            continue
        start = max(0.0, min(duration, _number(raw.get("start_time"))))
        end = max(start, min(duration, _number(raw.get("end_time"), start)))
        if end - start < 0.08:
            continue
        priority = str(raw.get("priority") or "medium")
        if priority not in {"high", "medium", "low"}:
            priority = "medium"
        output.append(
            {
                "id": str(raw.get("id") or f"enhancement-{index + 1}"),
                "start_time": round(start, 3),
                "end_time": round(end, 3),
                "type": kind,
                "instruction": str(raw.get("instruction") or "").strip()[:1000],
                "asset_requirements": [
                    str(item).strip()[:300]
                    for item in (raw.get("asset_requirements") or [])[:10]
                    if str(item).strip()
                ],
                "overlay_text": str(raw.get("overlay_text") or "").strip()[:160],
                "priority": priority,
                "confidence": round(max(0.0, min(1.0, _number(raw.get("confidence"), 0.5))), 3),
                "reason": str(raw.get("reason") or "").strip()[:700],
                "render_mode": "suggestion_only",
            }
        )
    return sorted(output, key=lambda item: (item["start_time"], item["end_time"], item["id"]))


def _subtract(duration: float, removals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    cursor = 0.0
    kept = []
    for start, end in _merge(removals):
        if start > cursor:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        kept.append((cursor, duration))
    return [(start, end) for start, end in kept if end - start >= 0.15]


def _fit_target(
    timeline: list[dict[str, Any]], target: float
) -> list[dict[str, Any]]:
    total = sum(item["source_end"] - item["source_start"] for item in timeline)
    if target <= 0 or total <= target * 1.03:
        return timeline
    remaining = target
    fitted = []
    # The user sees and approves this exact derived timeline.  Preserve the
    # opening hook and final CTA while reducing the middle from the tail.
    for index, item in enumerate(timeline):
        available = item["source_end"] - item["source_start"]
        future_min = sum(
            min(8.0 if i == len(timeline) - 1 else 0.35, other["source_end"] - other["source_start"])
            for i, other in enumerate(timeline[index + 1 :], start=index + 1)
        )
        take = min(available, max(0.0, remaining - future_min))
        if index == len(timeline) - 1:
            take = min(available, remaining)
        if take >= 0.15:
            adjusted = dict(item)
            adjusted["source_end"] = round(adjusted["source_start"] + take, 3)
            if take < available - 0.05:
                adjusted["action"] = "target_length_trim"
                adjusted["reason"] = "사용자가 지정한 목표 길이에 맞춰 승인 전에 계산된 축약"
            fitted.append(adjusted)
            remaining -= take
        if remaining <= 0.05:
            break
    return fitted


def build_render_timeline(plan: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    proposals = normalize_proposals(plan.get("segments") or [], duration)
    removals: list[tuple[float, float]] = []
    hook = None
    for item in proposals:
        start, end, action = item["start_time"], item["end_time"], item["action"]
        if action in {"cut", "trim"}:
            removals.append((start, end))
        elif action == "shorten":
            keep_for = max(0.8, (end - start) * 0.55)
            removals.append((start + keep_for, end))
        elif action == "use_as_hook" or (action == "move" and item.get("destination") == "opening"):
            if hook is None or item["confidence"] > hook["confidence"]:
                hook = item

    timeline: list[dict[str, Any]] = []
    if hook:
        removals.append((hook["start_time"], hook["end_time"]))
        timeline.append(
            {
                "source_start": hook["start_time"],
                "source_end": hook["end_time"],
                "action": "move_to_hook",
                "reason": hook["reason"] or "가장 강한 문제·결과 장면을 오프닝으로 이동",
            }
        )
    for start, end in _subtract(duration, removals):
        timeline.append(
            {
                "source_start": round(start, 3),
                "source_end": round(end, 3),
                "action": "keep",
                "reason": "승인된 컷 지시를 제외한 원본 흐름 유지",
            }
        )
    target = _number(plan.get("target_length_seconds"))
    return _fit_target(timeline, target)


def build_short_timeline(plan: dict[str, Any], duration: float) -> list[dict[str, Any]]:
    proposals = normalize_proposals(plan.get("segments") or [], duration)
    selected = [item for item in proposals if item["action"] == "use_as_short_clip"]
    if not selected:
        selected = [item for item in proposals if item["action"] == "use_as_hook"]
    selected.sort(key=lambda item: (-item["confidence"], item["start_time"]))
    target = min(90.0, max(10.0, _number(plan.get("short_target_seconds"), 45.0)))
    output = []
    total = 0.0
    for item in selected:
        available = item["end_time"] - item["start_time"]
        take = min(available, target - total)
        if take < 0.15:
            continue
        output.append(
            {
                "source_start": item["start_time"],
                "source_end": round(item["start_time"] + take, 3),
                "action": "short_highlight",
                "reason": item["reason"] or "승인된 쇼츠 하이라이트",
            }
        )
        total += take
        if total >= target - 0.05:
            break
    return output


def prepare_plan(
    raw_plan: dict[str, Any], duration: float, *, target_format: str | None = None
) -> dict[str, Any]:
    plan = deepcopy(raw_plan or {})
    plan["segments"] = normalize_proposals(plan.get("segments") or [], duration)
    plan["enhancements"] = normalize_enhancements(
        plan.get("enhancements") or [], duration
    )
    plan["target_length_seconds"] = max(0.0, _number(plan.get("target_length_seconds")))
    plan["short_target_seconds"] = max(10.0, min(90.0, _number(plan.get("short_target_seconds"), 45.0)))
    # A short-reel project promises both the approved full cut and a separately
    # addressable highlight export.  Do not let a model-produced false value
    # silently remove that deliverable from the renderer.
    plan["create_short_highlight"] = bool(
        plan.get("create_short_highlight") or target_format == "short_reel"
    )
    plan["render_timeline"] = build_render_timeline(plan, duration)
    plan["estimated_output_duration"] = round(
        sum(item["source_end"] - item["source_start"] for item in plan["render_timeline"]), 3
    )
    plan["short_timeline"] = (
        build_short_timeline(plan, duration) if plan["create_short_highlight"] else []
    )
    plan["estimated_short_duration"] = round(
        sum(item["source_end"] - item["source_start"] for item in plan["short_timeline"]), 3
    )
    return plan


def plan_diff(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, str]]:
    changes = []
    fields = (
        ("recommended_direction", "추천 방향"),
        ("target_length_seconds", "목표 길이"),
        ("create_short_highlight", "쇼츠 추출"),
        ("estimated_output_duration", "예상 편집본 길이"),
    )
    for key, label in fields:
        if previous.get(key) != current.get(key):
            changes.append(
                {"field": label, "before": str(previous.get(key)), "after": str(current.get(key))}
            )
    before = {
        (item.get("start_time"), item.get("end_time"), item.get("action"), item.get("reason"))
        for item in previous.get("segments") or []
    }
    after = {
        (item.get("start_time"), item.get("end_time"), item.get("action"), item.get("reason"))
        for item in current.get("segments") or []
    }
    if before != after:
        changes.append(
            {
                "field": "타임코드 제안",
                "before": f"{len(before)}개",
                "after": f"{len(after)}개",
            }
        )
    if previous.get("enhancements") != current.get("enhancements"):
        changes.append(
            {
                "field": "B-roll·자막 지시",
                "before": f"{len(previous.get('enhancements') or [])}개",
                "after": f"{len(current.get('enhancements') or [])}개",
            }
        )
    return changes


def validate_approved_timeline(timeline: list[dict[str, Any]], duration: float) -> None:
    if not timeline:
        raise ValueError("승인된 출력 구간이 없습니다.")
    if len(timeline) > 250:
        raise ValueError("편집 구간이 너무 많습니다.")
    for item in timeline:
        start = _number(item.get("source_start"), -1)
        end = _number(item.get("source_end"), -1)
        if start < 0 or end <= start or end > duration + 0.05:
            raise ValueError("승인된 편집 구간에 잘못된 타임코드가 있습니다.")
