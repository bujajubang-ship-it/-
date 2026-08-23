"""Deterministic, transcript-grounded conservative rough-cut helpers.

This module never calls an AI provider.  It turns timestamped transcript rows
into sentence/topic blocks, rejects unsafe cut proposals, and applies explicit
owner deletions to an already-reviewed timeline.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any


ROUGH_CUT_MODE = "conservative_rough_cut"
_SENTENCE_END = re.compile(r"(?:[.!?。！？]|(?:습니다|입니다|합니다|됩니다|했어요|해요|예요|이에요|죠|네요)[.!?]?)\s*$")
_TOPIC_KEYWORDS = {
    "opening": ("오늘", "현장", "소개", "왔습니다"),
    "problem": ("문제", "불편", "좁", "비용", "손해", "주의"),
    "design": ("도면", "설계", "배치", "치수"),
    "installation": ("납품", "설치", "시공", "공사", "철거"),
    "product": ("제품", "기계", "세척기", "장비", "사용법"),
    "flow": ("동선", "배수", "후드", "덕트", "가스", "전기", "바닥"),
    "result": ("완성", "결과", "전과", "후", "바뀌"),
    "closing": ("정리", "문의", "상담", "추천", "감사"),
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _topic(text: str) -> str:
    lowered = text.lower()
    ranked = [
        (sum(1 for word in words if word.lower() in lowered), name)
        for name, words in _TOPIC_KEYWORDS.items()
    ]
    score, name = max(ranked, default=(0, "explanation"))
    return name if score else "explanation"


def _stable_id(source_id: str, start: float, end: float, text: str) -> str:
    value = f"{source_id}|{start:.3f}|{end:.3f}|{text}".encode("utf-8")
    return "script-" + hashlib.sha1(value).hexdigest()[:16]


def transcript_blocks(
    transcript: dict[str, Any], *, source_id: str = "source-1",
    filename: str = "원본 영상",
) -> list[dict[str, Any]]:
    """Merge timestamp rows into conservative sentence/explanation blocks."""

    rows = []
    for raw in transcript.get("segments") or []:
        text = str(raw.get("text") or raw.get("transcript") or "").strip()
        start = max(0.0, _number(raw.get("start_time", raw.get("start"))))
        end = max(start, _number(raw.get("end_time", raw.get("end")), start))
        if text and end - start >= 0.05:
            rows.append({"start": start, "end": end, "text": text})
    rows.sort(key=lambda row: (row["start"], row["end"]))
    if not rows:
        return []

    blocks: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        pending.append(row)
        following = rows[index + 1] if index + 1 < len(rows) else None
        text = " ".join(item["text"] for item in pending).strip()
        gap_after = max(0.0, (following["start"] - row["end"])) if following else 99.0
        duration = row["end"] - pending[0]["start"]
        sentence_end = bool(_SENTENCE_END.search(text))
        topic_now = _topic(text)
        next_topic = _topic(following["text"]) if following else None
        topic_shift = bool(following and next_topic != topic_now and next_topic != "explanation")
        boundary = (
            following is None
            or gap_after >= 0.65
            or (sentence_end and duration >= 2.0)
            or (topic_shift and sentence_end)
            or duration >= 24.0
        )
        if not boundary:
            continue
        speech_complete = bool(sentence_end or gap_after >= 0.65 or following is None)
        topic_complete = bool(following is None or gap_after >= 1.2 or topic_shift or sentence_end)
        needs_review = not speech_complete or (duration >= 24.0 and not sentence_end)
        start, end = pending[0]["start"], pending[-1]["end"]
        blocks.append({
            "segment_id": _stable_id(source_id, start, end, text),
            "source_video_id": source_id,
            "source_filename": filename,
            "original_start_time": round(start, 3),
            "original_end_time": round(end, 3),
            "text": text[:6000],
            "block_type": "topic_block" if topic_shift or gap_after >= 1.2 else "sentence_block",
            "topic": topic_now,
            "speech_complete": speech_complete,
            "topic_complete": topic_complete,
            "silence_after": round(gap_after if following else 0.0, 3),
            "context_continuity": bool(following and gap_after < 0.8 and next_topic == topic_now),
            "viewer_confusion_risk": needs_review,
            "needs_review": needs_review,
        })
        pending = []

    # A topic block is the maximal adjacent run of the same inferred topic.
    group = 0
    previous_topic = None
    previous_end = None
    for block in blocks:
        if (
            previous_topic != block["topic"]
            or previous_end is None
            or block["original_start_time"] - previous_end >= 1.2
        ):
            group += 1
        block["topic_block_id"] = f"{source_id}:topic-{group}"
        previous_topic = block["topic"]
        previous_end = block["original_end_time"]
    return blocks


def _subtract(duration: float, removals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(removals):
        start, end = max(0.0, start), min(duration, end)
        if end - start < 0.2:
            continue
        if merged and start <= merged[-1][1] + 0.08:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    kept = []
    cursor = 0.0
    for start, end in merged:
        if start - cursor >= 0.2:
            kept.append((round(cursor, 3), round(start, 3)))
        cursor = max(cursor, end)
    if duration - cursor >= 0.2:
        kept.append((round(cursor, 3), round(duration, 3)))
    return kept


def build_conservative_timeline(
    proposals: list[dict[str, Any]], duration: float,
    transcript: dict[str, Any], *, source_id: str = "source-1",
    filename: str = "원본 영상", target_duration: float = 0.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Apply only whole, context-safe removal blocks and never target-fit."""

    blocks = transcript_blocks(transcript, source_id=source_id, filename=filename)
    if not blocks:
        evaluated = []
        rejected = 0
        for raw in proposals:
            item = deepcopy(raw)
            if str(item.get("action") or "keep") in {"cut", "trim", "shorten"}:
                item["action"] = "keep"
                item["reason"] = "문장 경계를 확인할 transcript가 없어 보수적으로 유지"
                rejected += 1
            item.update({
                "speech_complete": False, "topic_complete": False,
                "silence_after": 0.0, "context_continuity": True,
                "viewer_confusion_risk": True, "cut_applied": False,
            })
            evaluated.append(item)
        timeline = [{
            "source_id": source_id, "filename": filename,
            "source_start": 0.0, "source_end": round(duration, 3),
            "action": "keep", "reason": "transcript 경계 미확인으로 원본 흐름 유지",
            "speech_boundary_ok": False,
        }] if duration > 0 else []
        return timeline, evaluated, {
            "mode": ROUGH_CUT_MODE, "original_duration": round(duration, 3),
            "rough_cut_duration": round(duration, 3), "compression_ratio": 1.0,
            "target_duration_seconds": round(max(0.0, target_duration), 3),
            "target_duration_used_as_soft_guide": True, "auto_duration_selected": True,
            "preserved_topic_blocks": 0, "removed_blocks": 0, "user_deleted_blocks": 0,
            "rejected_cuts_due_to_mid_sentence": rejected,
            "rejected_cuts_due_to_context_break": 0,
            "viewer_confusion_risk_count": rejected,
            "needs_review_count": rejected,
        }
    topic_members: dict[str, set[str]] = {}
    for block in blocks:
        topic_members.setdefault(block["topic_block_id"], set()).add(block["segment_id"])
    removals: list[tuple[float, float]] = []
    removed_ids: set[str] = set()
    rejected_mid = rejected_context = confusion = needs_review = 0
    evaluated = []
    for raw in proposals:
        item = deepcopy(raw)
        action = str(item.get("action") or "keep")
        start = max(0.0, _number(item.get("start_time")))
        end = min(duration, max(start, _number(item.get("end_time"), start)))
        item.update({
            "speech_complete": True, "topic_complete": True,
            "silence_after": 0.0, "context_continuity": False,
            "viewer_confusion_risk": False, "cut_applied": False,
        })
        if action not in {"cut", "trim", "shorten"} or end - start < 0.2:
            evaluated.append(item)
            continue
        overlaps = [
            block for block in blocks
            if block["original_end_time"] > start and block["original_start_time"] < end
        ]
        if not overlaps:
            removals.append((start, end))
            item["cut_applied"] = True
            item["reason"] = (str(item.get("reason") or "") + " · 비발화 구간 전체 제거").strip(" ·")
            evaluated.append(item)
            continue
        partial = any(
            start > block["original_start_time"] + 0.12
            or end < block["original_end_time"] - 0.12
            for block in overlaps
        )
        item["speech_complete"] = all(block["speech_complete"] for block in overlaps) and not partial
        overlap_ids = {block["segment_id"] for block in overlaps}
        complete_topics = all(
            topic_members.get(block["topic_block_id"], set()).issubset(overlap_ids)
            for block in overlaps
        )
        item["topic_complete"] = complete_topics
        item["silence_after"] = max((block["silence_after"] for block in overlaps), default=0.0)
        item["context_continuity"] = any(block["context_continuity"] for block in overlaps)
        item["viewer_confusion_risk"] = bool(
            partial or not item["speech_complete"]
            or (not complete_topics and item["context_continuity"])
        )
        if item["viewer_confusion_risk"]:
            confusion += 1
            needs_review += 1
        if partial or not item["speech_complete"]:
            rejected_mid += 1
            item["action"] = "keep"
            item["reason"] = "문장 중간 컷 위험이 있어 보수적 러프컷에서 유지"
        elif not complete_topics and item["context_continuity"]:
            rejected_context += 1
            item["action"] = "keep"
            item["reason"] = "설명·주제 문맥이 이어져 보수적 러프컷에서 유지"
        elif item["viewer_confusion_risk"]:
            item["action"] = "keep"
            item["reason"] = "화면 전환 시 혼란 가능성이 있어 유지하고 사용자 검토 표시"
        else:
            cut_start = min(block["original_start_time"] for block in overlaps)
            cut_end = max(block["original_end_time"] for block in overlaps)
            removals.append((cut_start, cut_end))
            removed_ids.update(overlap_ids)
            item["cut_applied"] = True
        evaluated.append(item)

    timeline = [{
        "source_id": source_id,
        "filename": filename,
        "source_start": start,
        "source_end": end,
        "action": "keep",
        "reason": "문장·주제 경계를 보존한 원본 순서 러프컷",
        "speech_boundary_ok": True,
    } for start, end in _subtract(duration, removals)]
    rough_duration = round(sum(row["source_end"] - row["source_start"] for row in timeline), 3)
    log = {
        "mode": ROUGH_CUT_MODE,
        "original_duration": round(duration, 3),
        "rough_cut_duration": rough_duration,
        "compression_ratio": round(rough_duration / duration, 4) if duration else 1.0,
        "target_duration_seconds": round(max(0.0, target_duration), 3),
        "target_duration_used_as_soft_guide": True,
        "auto_duration_selected": True,
        "preserved_topic_blocks": len({b["topic_block_id"] for b in blocks if b["segment_id"] not in removed_ids}),
        "removed_blocks": len(removed_ids),
        "user_deleted_blocks": 0,
        "rejected_cuts_due_to_mid_sentence": rejected_mid,
        "rejected_cuts_due_to_context_break": rejected_context,
        "viewer_confusion_risk_count": confusion,
        "needs_review_count": needs_review + sum(1 for block in blocks if block["needs_review"]),
    }
    return timeline, evaluated, log


def _timeline_script(
    timeline: list[dict[str, Any]], blocks: list[dict[str, Any]],
    *, deleted_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    deleted_ids = deleted_ids or set()
    output = []
    rough_cursor = 0.0
    for row in timeline:
        source_id = str(row.get("source_id") or "source-1")
        start, end = _number(row.get("source_start")), _number(row.get("source_end"))
        for block in blocks:
            if str(block.get("source_video_id") or "source-1") != source_id:
                continue
            bstart, bend = block["original_start_time"], block["original_end_time"]
            if bend <= start + 0.02 or bstart >= end - 0.02:
                continue
            item = deepcopy(block)
            item["rough_cut_start_time"] = round(rough_cursor + max(0.0, bstart - start), 3)
            item["rough_cut_end_time"] = round(rough_cursor + min(end - start, bend - start), 3)
            item["deleted_by_user"] = item["segment_id"] in deleted_ids
            item["keep"] = not item["deleted_by_user"]
            output.append(item)
        rough_cursor += max(0.0, end - start)
    seen = set()
    return [item for item in output if not (item["segment_id"] in seen or seen.add(item["segment_id"]))]


def project_script_blocks(project: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    if project.get("project_mode") == "multisource_roughcut":
        blocks = []
        for source in project.get("sources") or []:
            source_id = str(source.get("source_id") or "")
            filename = str(source.get("filename") or "원본 영상")
            semantic = source.get("segments") or []
            if semantic:
                for row in semantic:
                    text = str(row.get("transcript") or "").strip()
                    start, end = _number(row.get("start_time")), _number(row.get("end_time"))
                    if not text or end <= start:
                        continue
                    blocks.append({
                        "segment_id": str(row.get("segment_id") or _stable_id(source_id, start, end, text)),
                        "source_video_id": source_id, "source_filename": filename,
                        "original_start_time": round(start, 3), "original_end_time": round(end, 3),
                        "text": text[:6000], "block_type": "explanation_block",
                        "topic": str(row.get("topic") or row.get("role") or "explanation"),
                        "topic_block_id": str(row.get("duplicate_group") or row.get("segment_id") or ""),
                        "speech_complete": bool(row.get("speech_boundary_ok", True)),
                        "topic_complete": True, "silence_after": 0.0,
                        "context_continuity": False, "viewer_confusion_risk": False,
                        "needs_review": not bool(row.get("speech_boundary_ok", True)),
                    })
            else:
                blocks.extend(transcript_blocks(source.get("transcript") or {}, source_id=source_id, filename=filename))
        return blocks
    source = project.get("source") or {}
    return transcript_blocks(
        project.get("transcript") or {}, source_id="source-1",
        filename=str(source.get("filename") or "원본 영상"),
    )


def edit_script_markdown(segments: list[dict[str, Any]]) -> str:
    lines = ["# 편집 스크립트", ""]
    for item in segments:
        status = "삭제" if item.get("deleted_by_user") else "유지"
        review = " · 검토 필요" if item.get("needs_review") else ""
        lines.append(
            f"- [{status}{review}] {item.get('source_filename')} "
            f"{item.get('original_start_time'):.3f}-{item.get('original_end_time'):.3f}: "
            f"{item.get('text')}"
        )
    return "\n".join(lines)


def initialize_script_editor(project: dict[str, Any]) -> dict[str, Any]:
    versions = project.get("plan_versions") or []
    if not versions:
        raise ValueError("편집 스크립트를 만들 구성안이 없습니다.")
    latest = versions[-1]
    current = project.get("rough_cut_script_editor") or {}
    if current and int(current.get("base_version") or 0) == int(latest.get("version") or 0):
        return current
    plan = deepcopy(latest.get("plan") or {})
    blocks = project_script_blocks(project, plan)
    script = _timeline_script(plan.get("timeline") or plan.get("render_timeline") or [], blocks)
    state = {
        "mode": ROUGH_CUT_MODE,
        "base_version": int(latest.get("version") or 0),
        "original_edit_plan": deepcopy(plan),
        "user_modified_edit_plan": deepcopy(plan),
        "transcript_segments": script,
        "deleted_segment_ids": [], "restored_segment_ids": [],
        "dirty": False, "saved_version": None,
        "final_rough_cut_path": None,
        "edit_script_markdown": edit_script_markdown(script),
        "edit_script_json": {"segments": deepcopy(script)},
    }
    return state


def apply_script_choices(
    state: dict[str, Any], *, deleted_ids: set[str], restored_ids: set[str],
) -> dict[str, Any]:
    """Rebuild a plan from cached blocks only; no model or transcript call."""

    original = deepcopy(state.get("original_edit_plan") or {})
    timeline = deepcopy(original.get("timeline") or original.get("render_timeline") or [])
    source_segments = state.get("transcript_segments") or []
    deleted = {
        item["segment_id"]: item for item in source_segments
        if item.get("segment_id") in deleted_ids
    }
    modified = []
    for row in timeline:
        source_id = str(row.get("source_id") or "source-1")
        removals = sorted((
            (item["original_start_time"], item["original_end_time"])
            for item in deleted.values()
            if str(item.get("source_video_id") or "source-1") == source_id
            and item["original_end_time"] > _number(row.get("source_start"))
            and item["original_start_time"] < _number(row.get("source_end"))
        ))
        cursor = _number(row.get("source_start"))
        end = _number(row.get("source_end"))
        for cut_start, cut_end in removals:
            cut_start, cut_end = max(cursor, cut_start), min(end, cut_end)
            if cut_start - cursor >= 0.2:
                kept = deepcopy(row); kept["source_start"], kept["source_end"] = round(cursor, 3), round(cut_start, 3)
                kept["reason"] = "사용자 스크립트 삭제를 제외한 문맥 블록 유지"; modified.append(kept)
            cursor = max(cursor, cut_end)
        if end - cursor >= 0.2:
            kept = deepcopy(row); kept["source_start"], kept["source_end"] = round(cursor, 3), round(end, 3)
            kept["reason"] = "사용자 스크립트 삭제를 제외한 문맥 블록 유지"; modified.append(kept)
    blocks = [
        {key: value for key, value in item.items() if key not in {"rough_cut_start_time", "rough_cut_end_time", "deleted_by_user", "keep"}}
        for item in source_segments
    ]
    script = _timeline_script(modified, blocks, deleted_ids=deleted_ids)
    # Deleted rows remain visible in their original order for one-click restore.
    visible = {item["segment_id"]: item for item in script}
    for original_row in source_segments:
        if original_row["segment_id"] not in deleted_ids:
            continue
        row = deepcopy(original_row); row["deleted_by_user"], row["keep"] = True, False
        visible[row["segment_id"]] = row
    ordered = [visible[item["segment_id"]] for item in source_segments if item["segment_id"] in visible]
    rough_duration = round(sum(_number(row.get("source_end")) - _number(row.get("source_start")) for row in modified), 3)
    if "timeline" in original:
        original["timeline"] = modified
    original["render_timeline"] = modified
    original["estimated_output_duration"] = rough_duration
    log = deepcopy(original.get("rough_cut_log") or {})
    original_duration = _number(log.get("original_duration"), rough_duration)
    log.update({
        "mode": ROUGH_CUT_MODE, "rough_cut_duration": rough_duration,
        "compression_ratio": round(rough_duration / original_duration, 4) if original_duration else 1.0,
        "target_duration_used_as_soft_guide": True, "auto_duration_selected": True,
        "user_deleted_blocks": len(deleted_ids),
    })
    original["rough_cut_log"] = log
    return {
        **state,
        "user_modified_edit_plan": original,
        "transcript_segments": ordered,
        "deleted_segment_ids": sorted(deleted_ids),
        "restored_segment_ids": sorted(restored_ids),
        "dirty": True,
        "edit_script_markdown": edit_script_markdown(ordered),
        "edit_script_json": {"segments": deepcopy(ordered)},
    }


def state_json(state: dict[str, Any]) -> str:
    return json.dumps(state.get("edit_script_json") or {}, ensure_ascii=False, sort_keys=True)
