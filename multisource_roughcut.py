"""Deterministic, restart-safe primitives for multi-source rough cuts.

The expensive media/GPT work lives in the durable edit pipeline.  This module
keeps source/chunk state, duplicate selection and timeline validation pure so a
retry can resume without re-reading successful source material.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from copy import deepcopy
from typing import Any

from edit_project_store import utc_now


SOURCE_STAGES = (
    "UPLOAD_COMPLETE", "AUDIO_EXTRACTED", "TRANSCRIBED", "SEGMENTED",
    "SOURCE_ANALYZED",
)
STORY_ROLE_ORDER = (
    "hook", "problem", "product_intro", "principle", "usage", "proof",
    "benefit", "drawback", "purchase_caution", "maintenance", "after_service",
    "cost", "recommended_for", "not_recommended_for", "conclusion",
)

ROLE_KEYWORDS = {
    "problem": ("문제", "힘들", "불편", "인건비", "설거지", "놓치"),
    "product_intro": ("제품", "세척기", "소개", "기계"),
    "principle": ("원리", "작동", "초음파", "방식"),
    "usage": ("사용", "넣고", "버튼", "세팅", "방법"),
    "proof": ("실제로", "써보", "년", "현장", "후기", "줄었", "결과"),
    "benefit": ("장점", "좋", "절약", "편해", "효율"),
    "drawback": ("단점", "아쉽", "불편", "주의"),
    "purchase_caution": ("구매", "사기 전", "주의", "확인", "용량"),
    "maintenance": ("관리", "세척", "청소", "교체", "물"),
    "after_service": ("A/S", "AS", "수리", "고장", "서비스"),
    "cost": ("가격", "비용", "만원", "원", "투자"),
    "recommended_for": ("추천", "이런 분", "업장", "식당"),
    "not_recommended_for": ("추천하지", "필요 없", "맞지 않"),
    "conclusion": ("결론", "정리", "마지막", "문의"),
}


def source_id() -> str:
    return uuid.uuid4().hex


def new_source(
    *, filename: str, storage_key: str = "", size_bytes: int = 0,
    speaker: str = "", recorded_at: str | None = None,
) -> dict[str, Any]:
    now = utc_now()
    return {
        "source_id": source_id(), "filename": str(filename)[:240],
        "duration": 0.0, "speaker": str(speaker or "")[:160],
        "recorded_at": recorded_at, "status": "UPLOADING",
        "storage_key": storage_key, "size_bytes": max(0, int(size_bytes or 0)),
        "media": {}, "checkpoints": [], "transcript_chunks": [],
        "transcript": {}, "signals": {"silences": [], "scene_changes": []},
        "visual_analysis": {"status": "not_run", "fallback_used": False},
        "segments": [], "error": None, "retry_count": 0,
        "created_at": now, "updated_at": now,
    }


def ensure_multisource(project: dict[str, Any]) -> dict[str, Any]:
    """Add a source collection while preserving every legacy project field."""

    payload = project
    sources = payload.setdefault("sources", [])
    legacy = payload.get("source") or {}
    if not sources and legacy.get("filename"):
        item = new_source(
            filename=legacy.get("filename") or "source.mp4",
            storage_key=legacy.get("object_key") or legacy.get("storage_name") or "",
            size_bytes=legacy.get("size_bytes") or 0,
        )
        item.update({
            "source_id": "legacy-source", "status": (
                "SOURCE_ANALYZED" if payload.get("transcript") else "UPLOAD_COMPLETE"
            ), "media": deepcopy(legacy.get("media") or {}),
            "duration": float((legacy.get("media") or {}).get("duration") or 0),
            "transcript": deepcopy(payload.get("transcript") or {}),
            "signals": deepcopy(payload.get("analysis_signals") or {}),
            "visual_analysis": deepcopy(payload.get("visual_analysis") or {}),
        })
        sources.append(item)
    payload.setdefault("project_mode", "single_source")
    payload.setdefault("uploads_finalized", False)
    payload.setdefault("duplicate_groups", [])
    payload.setdefault("story_plan_state", "not_requested")
    payload.setdefault("analysis_cache_version", 1)
    return payload


def find_source(project: dict[str, Any], wanted: str) -> dict[str, Any]:
    ensure_multisource(project)
    for item in project.get("sources") or []:
        if str(item.get("source_id")) == str(wanted):
            return item
    raise KeyError("영상 소스를 찾지 못했습니다.")


def checkpoint(item: dict[str, Any], stage: str, **detail: Any) -> None:
    if stage not in SOURCE_STAGES:
        raise ValueError("unsupported source checkpoint")
    history = item.setdefault("checkpoints", [])
    if not any(row.get("stage") == stage for row in history):
        history.append({"stage": stage, "completed_at": utc_now(), **detail})
    item["status"] = stage
    item["updated_at"] = utc_now()
    item["error"] = None


def chunk_fingerprint(source_id_value: str, start: float, end: float) -> str:
    raw = f"{source_id_value}:{start:.3f}:{end:.3f}:v1".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def plan_transcript_chunks(item: dict[str, Any], *, chunk_seconds: int = 600) -> list[dict[str, Any]]:
    duration = max(0.0, float((item.get("media") or {}).get("duration") or item.get("duration") or 0))
    size = max(300, min(900, int(chunk_seconds)))
    existing = {row.get("fingerprint"): row for row in item.get("transcript_chunks") or []}
    chunks = []
    cursor = 0.0
    index = 0
    while cursor < duration - 0.01:
        end = min(duration, cursor + size)
        fingerprint = chunk_fingerprint(str(item["source_id"]), cursor, end)
        row = existing.get(fingerprint) or {
            "chunk_index": index, "start_time": round(cursor, 3),
            "end_time": round(end, 3), "fingerprint": fingerprint,
            "status": "pending", "attempt": 0, "transcript": {},
            "segments": [], "error": None,
        }
        row["chunk_index"] = index
        chunks.append(row)
        cursor = end
        index += 1
    item["transcript_chunks"] = chunks
    return chunks


def _tokens(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[0-9A-Za-z가-힣]{2,}", text.lower())
        if token not in {"그리고", "그래서", "그런데", "이거", "저거", "합니다", "있습니다"}
    }


def classify_role(text: str, *, index: int = 0) -> str:
    scores = {
        role: sum(1 for keyword in keywords if keyword.lower() in text.lower())
        for role, keywords in ROLE_KEYWORDS.items()
    }
    best = max(scores, key=scores.get) if scores else "product_intro"
    if scores.get(best, 0) == 0:
        return "hook" if index == 0 else "product_intro"
    return best


def semantic_segments(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Create auditable candidates from timestamp transcript boundaries."""

    raw = (item.get("transcript") or {}).get("segments") or []
    output = []
    for index, row in enumerate(raw):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start = max(0.0, float(row.get("start") or 0))
        end = max(start, float(row.get("end") or start))
        role = classify_role(text, index=index)
        evidence_bonus = 0.18 if role == "proof" else 0
        concise = min(1.0, 6.0 / max(2.0, end - start))
        quality = min(1.0, 0.48 + evidence_bonus + concise * 0.24)
        output.append({
            "segment_id": f"{item['source_id']}:{index}",
            "source_id": item["source_id"], "start_time": round(start, 3),
            "end_time": round(end, 3), "speaker": item.get("speaker") or "",
            "transcript": text[:3000], "topic": role, "role": role,
            "importance": round(min(1.0, quality + (0.1 if role in {"problem", "proof", "purchase_caution"} else 0)), 3),
            "quality": round(quality, 3), "duplicate_group": None,
            "confidence": 0.72, "speech_boundary_ok": True,
            "selected": False, "selection_reason": "",
        })
    item["segments"] = output
    return output


def deduplicate_segments(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group semantic repetitions and choose the strongest source quote."""

    candidates = [segment for source in sources for segment in (source.get("segments") or [])]
    groups: list[dict[str, Any]] = []
    for segment in candidates:
        tokens = _tokens(segment.get("transcript") or "")
        match = None
        best_overlap = 0.0
        for group in groups:
            if group["role"] != segment.get("role"):
                continue
            union = tokens | group["tokens"]
            overlap = len(tokens & group["tokens"]) / max(1, len(union))
            if (overlap >= 0.30 or len(tokens & group["tokens"]) >= 3) and overlap > best_overlap:
                match, best_overlap = group, overlap
        if match is None:
            match = {
                "duplicate_group": f"dup-{len(groups) + 1}",
                "role": segment.get("role"), "tokens": set(tokens), "segments": [],
            }
            groups.append(match)
        match["tokens"].update(tokens)
        match["segments"].append(segment)
    public = []
    for group in groups:
        ranked = sorted(
            group["segments"],
            key=lambda row: (
                1 if row.get("role") == "proof" and row.get("speaker") else 0,
                float(row.get("quality") or 0), float(row.get("importance") or 0),
                -(float(row.get("end_time") or 0) - float(row.get("start_time") or 0)),
            ),
            reverse=True,
        )
        chosen = ranked[0]
        for row in ranked:
            row["duplicate_group"] = group["duplicate_group"]
            row["selected"] = row is chosen
            row["selection_reason"] = (
                "같은 의미 발언 중 실제 경험·명확성·정보 밀도가 가장 높음"
                if row is chosen else "더 강한 동일 의미 발언이 선택됨"
            )
        public.append({
            "duplicate_group": group["duplicate_group"], "role": group["role"],
            "selected_segment_id": chosen["segment_id"],
            "candidate_segment_ids": [row["segment_id"] for row in ranked],
        })
    return public


def apply_visual_quality(item: dict[str, Any]) -> None:
    """Blend bounded frame evidence into quote selection without inventing vision."""

    visual = item.get("visual_analysis") or {}
    if visual.get("status") != "succeeded":
        for segment in item.get("segments") or []:
            segment["visual_score"] = None
            segment["visual_evidence_status"] = "audio_only_fallback"
        return
    windows = visual.get("segments") or []
    for segment in item.get("segments") or []:
        overlaps = [
            row for row in windows
            if float(row.get("end_time") or 0) > float(segment.get("start_time") or 0)
            and float(row.get("start_time") or 0) < float(segment.get("end_time") or 0)
        ]
        if not overlaps:
            segment["visual_score"] = None
            segment["visual_evidence_status"] = "no_sample"
            continue
        score = sum(float(row.get("visual_score") or 0.5) for row in overlaps) / len(overlaps)
        harmful = sum(1 for row in overlaps if row.get("edit_decision") == "cut") / len(overlaps)
        segment["visual_score"] = round(score, 3)
        segment["visual_evidence_status"] = "sampled"
        segment["visual_frame_ids"] = [
            frame for row in overlaps for frame in (row.get("frame_ids") or [])
        ][:20]
        segment["quality"] = round(max(0.0, min(
            1.0, float(segment.get("quality") or 0.5) * 0.68 + score * 0.32 - harmful * 0.18,
        )), 3)


def _natural_handles(segment: dict[str, Any], duration: float) -> tuple[float, float]:
    start = max(0.0, float(segment.get("start_time") or 0) - 0.18)
    end = min(duration, float(segment.get("end_time") or 0) + 0.55)
    return round(start, 3), round(end, 3)


def build_story_plan(
    project: dict[str, Any], *, target_length_seconds: float = 0,
) -> dict[str, Any]:
    """Build a conservative coherent plan from cached selected statements."""

    ensure_multisource(project)
    source_by_id = {str(source["source_id"]): source for source in project.get("sources") or []}
    selected = [
        segment for source in source_by_id.values()
        for segment in (source.get("segments") or []) if segment.get("selected")
    ]
    role_rank = {role: index for index, role in enumerate(STORY_ROLE_ORDER)}
    selected.sort(key=lambda row: (
        role_rank.get(str(row.get("role")), 99),
        -float(row.get("importance") or 0), str(row.get("source_id")),
        float(row.get("start_time") or 0),
    ))
    if selected:
        strongest = max(
            selected,
            key=lambda row: (
                1 if row.get("role") == "proof" else 0,
                float(row.get("importance") or 0), float(row.get("quality") or 0),
            ),
        )
        selected.remove(strongest)
        selected.insert(0, strongest)
    timeline = []
    total = 0.0
    limit = max(0.0, float(target_length_seconds or (project.get("settings") or {}).get("target_length_seconds") or 0))
    for order, segment in enumerate(selected, start=1):
        source = source_by_id[str(segment["source_id"])]
        duration = float((source.get("media") or {}).get("duration") or source.get("duration") or 0)
        start, end = _natural_handles(segment, duration)
        if limit and total >= limit:
            break
        if limit and total + end - start > limit:
            end = max(start, start + limit - total)
        if end - start < 0.2:
            continue
        timeline.append({
            "order": order, "source_id": segment["source_id"],
            "filename": source.get("filename"), "source_start": start,
            "source_end": round(end, 3), "role": "hook" if order == 1 else segment.get("role"),
            "speaker": segment.get("speaker") or source.get("speaker") or "",
            "reason": segment.get("selection_reason") or "문맥상 가장 명확한 발언",
            "segment_id": segment.get("segment_id"), "speech_boundary_ok": True,
        })
        total += end - start
    return {
        "recommended_direction": "문제와 실제 증거를 먼저 제시하고 설명·주의사항·관리/A/S로 이어지는 러프컷",
        "timeline": timeline, "render_timeline": timeline,
        "estimated_output_duration": round(total, 3),
        "target_length_seconds": limit, "source_count": len(source_by_id),
        "channel_evidence_confidence": _channel_confidence(project.get("evidence_trace") or []),
        "editor_notes": [
            "중복 발언은 실제 경험·명확성·정보 밀도가 높은 구간을 우선했습니다.",
            "문장 경계 앞뒤에 최소 natural handle을 남겼습니다.",
            "자막·음악·효과·B-roll 합성은 적용하지 않습니다.",
        ],
    }


def apply_story_reasoning(
    project: dict[str, Any], reasoning: dict[str, Any], *, target_length_seconds: float = 0,
) -> dict[str, Any]:
    """Ground model ordering to cached selected segments and real timecodes."""

    fallback = build_story_plan(project, target_length_seconds=target_length_seconds)
    selected = {
        str(segment.get("segment_id")): segment
        for source in project.get("sources") or []
        for segment in (source.get("segments") or []) if segment.get("selected")
    }
    source_by_id = {str(source.get("source_id")): source for source in project.get("sources") or []}
    timeline = []
    total = 0.0
    limit = max(0.0, float(target_length_seconds or fallback.get("target_length_seconds") or 0))
    used: set[str] = set()
    for decision in reasoning.get("ordered_segments") or []:
        segment_id_value = str(decision.get("segment_id") or "")
        segment = selected.get(segment_id_value)
        if not segment or segment_id_value in used or not decision.get("keep", True):
            continue
        source = source_by_id[str(segment["source_id"])]
        duration = float((source.get("media") or {}).get("duration") or source.get("duration") or 0)
        start, end = _natural_handles(segment, duration)
        if limit and total + end - start > limit:
            end = max(start, start + limit - total)
        if end - start < 0.2:
            continue
        used.add(segment_id_value)
        timeline.append({
            "order": len(timeline) + 1, "source_id": segment["source_id"],
            "filename": source.get("filename"), "source_start": start,
            "source_end": round(end, 3), "role": decision.get("role") or segment.get("role"),
            "speaker": segment.get("speaker") or source.get("speaker") or "",
            "reason": str(decision.get("reason") or segment.get("selection_reason") or "문맥상 선택")[:1000],
            "segment_id": segment_id_value, "speech_boundary_ok": True,
        })
        total += end - start
        if limit and total >= limit - 0.05:
            break
    if not timeline:
        return fallback
    fallback.update({
        "recommended_direction": str(reasoning.get("recommended_direction") or fallback["recommended_direction"])[:2000],
        "timeline": timeline, "render_timeline": timeline,
        "estimated_output_duration": round(total, 3),
        "channel_evidence_confidence": reasoning.get("channel_evidence_confidence") or fallback["channel_evidence_confidence"],
        "editor_notes": list(reasoning.get("editor_notes") or [])[:20] + fallback["editor_notes"],
    })
    return fallback


def _channel_confidence(trace: list[dict[str, Any]]) -> str:
    sample = sum(int(item.get("sample_size") or 0) for item in trace if not item.get("unavailable"))
    return "high" if sample >= 20 else "medium" if sample >= 5 else "low"


def validate_timeline(timeline: list[dict[str, Any]], sources: list[dict[str, Any]]) -> None:
    if not timeline:
        raise ValueError("승인된 러프컷 구간이 없습니다.")
    if len(timeline) > 500:
        raise ValueError("러프컷 구간이 너무 많습니다.")
    source_by_id = {str(source.get("source_id")): source for source in sources}
    for row in timeline:
        source = source_by_id.get(str(row.get("source_id")))
        if source is None:
            raise ValueError("러프컷에 존재하지 않는 원본이 포함됐습니다.")
        duration = float((source.get("media") or {}).get("duration") or source.get("duration") or 0)
        start, end = float(row.get("source_start") or -1), float(row.get("source_end") or -1)
        if start < 0 or end <= start or end > duration + 0.05:
            raise ValueError("승인된 러프컷 타임코드가 원본 범위를 벗어났습니다.")
