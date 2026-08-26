"""Plain-transcript editing guidance with immutable sentence identifiers.

This module does not inspect video, upload media, or render anything. It keeps
the user's original sentences as the only legal source of spoken content and
turns model output into validated editor-facing documents.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from typing import Any

from edit_analysis_service import EditAnalysisService


ACTIONS = frozenset({"유지", "이동", "축약", "삭제", "다른 구간과 결합"})
_SENTENCE_ID = re.compile(r"^S(\d{3,})$")
_SENTENCE_REF = re.compile(r"S\d{3,}")
_SENTENCE_PART = re.compile(r"[^.!?。！？\n]+(?:[.!?。！？]+|$)")


def split_sentences(script: str) -> list[dict[str, Any]]:
    """Split code-side while preserving every returned source substring."""

    output: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(str(script or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        parts = [match.group(0).strip() for match in _SENTENCE_PART.finditer(line)]
        for text in (part for part in parts if part):
            index = len(output) + 1
            output.append({
                "id": f"S{index:03d}",
                "text": text,
                "line_number": line_number,
                "order": index,
            })
    return output


def transcript_hash(script: str) -> str:
    return hashlib.sha256(str(script or "").encode("utf-8")).hexdigest()


def estimate_sentence_seconds(text: str) -> float:
    # Korean narration is commonly around 4-5 non-space characters/sec. Use a
    # deliberately conservative estimate and never present it as measured time.
    visible = len(re.sub(r"\s+", "", str(text or "")))
    return round(max(1.2, visible / 4.3), 1)


def analyze_duplicates(sentences: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def tokens(text: str) -> set[str]:
        return set(re.findall(r"[가-힣A-Za-z0-9]{2,}", text.lower()))

    groups: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, sentence in enumerate(sentences):
        if sentence["id"] in used:
            continue
        base = tokens(sentence["text"])
        if not base:
            continue
        matches = [sentence["id"]]
        for candidate in sentences[index + 1:]:
            other = tokens(candidate["text"])
            union = base | other
            if union and len(base & other) / len(union) >= 0.72:
                matches.append(candidate["id"])
        if len(matches) > 1:
            used.update(matches)
            groups.append({"candidate_ids": matches, "basis": "token_jaccard>=0.72"})
    return groups


def sentence_range(sentences: list[dict[str, Any]], start_id: str, end_id: str) -> list[dict[str, Any]]:
    by_id = {row["id"]: row for row in sentences}
    if start_id not in by_id or end_id not in by_id:
        raise ValueError(f"존재하지 않는 문장 ID: {start_id}~{end_id}")
    start, end = by_id[start_id]["order"], by_id[end_id]["order"]
    if start > end:
        raise ValueError(f"문장 범위 역전: {start_id}~{end_id}")
    return sentences[start - 1:end]


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }


FLOW_ITEM_SCHEMA = _object({
    "order": {"type": "integer"},
    "title": {"type": "string"},
    "sentence_start_id": {"type": "string"},
    "sentence_end_id": {"type": "string"},
    "action": {"type": "string", "enum": sorted(ACTIONS)},
    "purpose": {"type": "string"},
    "reason": {"type": "string"},
    "evidence_basis": {"type": "array", "items": {"type": "string"}},
    "transition_note": {"type": "string"},
    "estimated_seconds": {"type": "number"},
})

EDIT_ROW_SCHEMA = _object({
    "final_order": {"type": "integer"},
    "sentence_start_id": {"type": "string"},
    "sentence_end_id": {"type": "string"},
    "start_sentence": {"type": "string"},
    "end_sentence": {"type": "string"},
    "action": {"type": "string", "enum": sorted(ACTIONS)},
    "purpose": {"type": "string"},
    "edit_instruction": {"type": "string"},
    "transition_note": {"type": "string"},
    "reason": {"type": "string"},
    "evidence_basis": {"type": "array", "items": {"type": "string"}},
    "broll_note": {"type": "string"},
    "estimated_seconds": {"type": "number"},
})

DELETE_SCHEMA = _object({
    "sentence_start_id": {"type": "string"},
    "sentence_end_id": {"type": "string"},
    "start_sentence": {"type": "string"},
    "end_sentence": {"type": "string"},
    "reason": {"type": "string"},
})

RESULT_SCHEMA: dict[str, Any] = _object({
    "recommended_duration_seconds": {"type": "number"},
    "core_message": {"type": "string"},
    "strongest_opening": {"type": "string"},
    "biggest_problem": {"type": "string"},
    "data_basis_note": {"type": "string"},
    "overall_flow": {"type": "array", "items": FLOW_ITEM_SCHEMA},
    "edit_table": {"type": "array", "items": EDIT_ROW_SCHEMA},
    "deletions": {"type": "array", "items": DELETE_SCHEMA},
    "duplicates": {"type": "array", "items": _object({
        "topic": {"type": "string"},
        "candidates": {"type": "array", "items": {"type": "string"}},
        "selected": {"type": "string"},
        "reason": {"type": "string"},
        "remaining_action": {"type": "string"},
    })},
    "condensations": {"type": "array", "items": _object({
        "delete_sentence_ids": {"type": "array", "items": {"type": "string"}},
        "keep_sentence_ids": {"type": "array", "items": {"type": "string"}},
        "purpose_after_condensing": {"type": "string"},
    })},
    "final_instructions": _object({
        "final_flow": {"type": "array", "items": {"type": "string"}},
        "final_sentence_order": {"type": "array", "items": {"type": "string"}},
        "delete_sentences": {"type": "array", "items": {"type": "string"}},
        "condense_sentences": {"type": "array", "items": {"type": "string"}},
        "move_sentence_groups": {"type": "array", "items": {"type": "string"}},
        "duplicate_decisions": {"type": "array", "items": {"type": "string"}},
        "broll_positions": {"type": "array", "items": {"type": "string"}},
        "caption_emphasis": {"type": "array", "items": {"type": "string"}},
        "connection_lines_needed": {"type": "array", "items": {"type": "string"}},
        "must_keep_statements": {"type": "array", "items": {"type": "string"}},
        "screen_review_required": {"type": "array", "items": {"type": "string"}},
        "expected_duration_seconds": {"type": "number"},
    }),
    "used_evidence": {"type": "array", "items": _object({
        "source": {"type": "string"}, "claim": {"type": "string"},
        "sample_size": {"type": "integer"},
    })},
    "revision_summary": {"type": "string"},
})


SYSTEM_INSTRUCTIONS = """당신은 부자주방 편집 담당 직원에게 전달할 대본 기반 영상 흐름 설계자다.
영상은 보지 못했다. 제공된 문장 ID와 원문만 사용하고 대본에 없는 발언을 만들지 않는다.
단어 또는 문장 중간을 자르지 말고, 이동은 반드시 연속된 완결 문장 묶음으로 한다.
한 장소·화자로 보이는 연속 구간은 가급적 함께 유지한다. 확신할 수 없으면 '불확실'이라고 쓴다.
서로 먼 범위를 연결하면 '화면 연결 확인 필요' 또는 'B-roll로 연결 추천'을 transition_note에 쓴다.
장면 상태 판단에는 반드시 '영상 화면 직접 확인 필요'라고 쓴다.
실사용 후기·현장 증거·문제·결과를 제품 일반 설명보다 앞세울 수 있다.
중복 설명은 가장 짧고 명확한 연속 묶음 하나만 선택한다.
evidence에 실제 CTR/Retention 수치가 없으면 숫자를 만들지 않는다.
데이터 표본이 부족하면 정확히 '채널 데이터 표본이 부족하여 Business PT와 대본의 논리 구조를 중심으로 판단함'이라고 쓴다.
모든 start_sentence/end_sentence는 제공된 원문과 글자까지 정확히 같아야 한다.
결과는 편집자가 그대로 실행할 수 있게 구체적으로 작성한다."""


class PlainTranscriptEditService:
    def __init__(self, analysis: EditAnalysisService | None = None) -> None:
        self.analysis = analysis or EditAnalysisService()

    async def analyze(
        self, *, request: dict[str, Any], sentences: list[dict[str, Any]],
        duplicates: list[dict[str, Any]], evidence: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = json.dumps({
            "project": request,
            "sentences": [{"id": row["id"], "text": row["text"]} for row in sentences],
            "code_duplicate_candidates": duplicates,
            "retrieved_evidence": evidence,
            "required_output_order": [
                "summary", "overall_flow", "edit_table", "deletions",
                "duplicates", "condensations", "final_instructions",
            ],
        }, ensure_ascii=False, default=str)
        return await self.analysis._structured(
            prompt=prompt, instructions=SYSTEM_INSTRUCTIONS,
            schema=RESULT_SCHEMA, schema_name="plain_transcript_edit_flow",
            reasoning_effort="high", allow_anthropic=False,
        )

    async def revise(
        self, *, current: dict[str, Any], user_request: str,
        sentence_context: list[dict[str, Any]], evidence_summary: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = json.dumps({
            "current_result": current,
            "user_revision_request": user_request,
            "only_relevant_sentence_context": sentence_context,
            "cached_evidence_summary": evidence_summary,
            "instruction": "기존 결과를 기준으로 요청된 부분만 판단하되 완전한 수정 결과 JSON을 반환",
        }, ensure_ascii=False, default=str)
        return await self.analysis._structured(
            prompt=prompt, instructions=SYSTEM_INSTRUCTIONS,
            schema=RESULT_SCHEMA, schema_name="plain_transcript_edit_revision",
            reasoning_effort="medium", allow_anthropic=False,
        )


def revision_sentence_context(
    sentences: list[dict[str, Any]], message: str, current: dict[str, Any], limit: int = 80,
) -> list[dict[str, Any]]:
    ids = set(_SENTENCE_REF.findall(message))
    by_order = {row["order"]: row for row in sentences}
    by_id = {row["id"]: row for row in sentences}
    selected: dict[str, dict[str, Any]] = {}
    for sentence_id in ids:
        row = by_id.get(sentence_id)
        if not row:
            continue
        for order in range(max(1, row["order"] - 2), row["order"] + 3):
            if order in by_order:
                selected[by_order[order]["id"]] = by_order[order]
    query_tokens = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", message.lower()))
    ranked = []
    for row in sentences:
        tokens = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", row["text"].lower()))
        ranked.append((len(tokens & query_tokens), row))
    for score, row in sorted(ranked, key=lambda item: (-item[0], item[1]["order"])):
        if score <= 0 or len(selected) >= limit:
            break
        selected[row["id"]] = row
    for edit_row in current.get("edit_table") or []:
        for key in ("sentence_start_id", "sentence_end_id"):
            row = by_id.get(str(edit_row.get(key) or ""))
            if row and len(selected) < limit:
                selected[row["id"]] = row
    return sorted(selected.values(), key=lambda row: row["order"])


def _all_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def validate_result(
    result: dict[str, Any], sentences: list[dict[str, Any]], *,
    numeric_data_available: bool, channel_data_available: bool = True,
    ctr_data_available: bool | None = None,
    retention_data_available: bool | None = None,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ValueError("편집 결과가 객체가 아닙니다.")
    by_id = {row["id"]: row for row in sentences}
    final_used: set[str] = set()
    deleted: set[str] = set()

    def validate_span(row: dict[str, Any], *, final: bool) -> list[dict[str, Any]]:
        start_id = str(row.get("sentence_start_id") or "")
        end_id = str(row.get("sentence_end_id") or "")
        span = sentence_range(sentences, start_id, end_id)
        if row.get("start_sentence") is not None and row["start_sentence"] != span[0]["text"]:
            raise ValueError(f"시작 문장 원문 불일치: {start_id}")
        if row.get("end_sentence") is not None and row["end_sentence"] != span[-1]["text"]:
            raise ValueError(f"끝 문장 원문 불일치: {end_id}")
        if final:
            overlap = final_used & {item["id"] for item in span}
            if overlap:
                raise ValueError(f"최종 배치에서 문장 중복 사용: {sorted(overlap)}")
            final_used.update(item["id"] for item in span)
        return span

    for row in result.get("overall_flow") or []:
        validate_span(row, final=False)
    for row in result.get("edit_table") or []:
        if row.get("action") not in ACTIONS:
            raise ValueError("지원하지 않는 편집 처리입니다.")
        validate_span(row, final=row.get("action") != "삭제")
    for row in result.get("deletions") or []:
        span = validate_span(row, final=False)
        deleted.update(item["id"] for item in span)
    condensed_keep: set[str] = set()
    condensed_delete: set[str] = set()
    for row in result.get("condensations") or []:
        keep_ids = {str(value) for value in row.get("keep_sentence_ids") or []}
        delete_ids = {str(value) for value in row.get("delete_sentence_ids") or []}
        if (keep_ids | delete_ids) - set(by_id):
            raise ValueError("축약 목록에 존재하지 않는 문장 ID가 있습니다.")
        if keep_ids & delete_ids:
            raise ValueError("축약 목록에서 같은 문장을 유지·삭제로 동시에 지정했습니다.")
        condensed_keep.update(keep_ids)
        condensed_delete.update(delete_ids)
    if deleted & condensed_keep:
        raise ValueError("완전 삭제와 축약 유지가 동시에 지정된 문장이 있습니다.")
    conflict = deleted & final_used
    if conflict:
        raise ValueError(f"삭제와 최종 사용이 동시에 지정됨: {sorted(conflict)}")
    unknown = {ref for ref in _SENTENCE_REF.findall(_all_text(result)) if ref not in by_id}
    if unknown:
        raise ValueError(f"존재하지 않는 문장 ID 사용: {sorted(unknown)}")
    ctr_available = numeric_data_available if ctr_data_available is None else ctr_data_available
    retention_available = numeric_data_available if retention_data_available is None else retention_data_available
    result_text = _all_text(result)
    if not ctr_available and re.search(r"CTR[^\n]{0,30}?\d+(?:\.\d+)?%", result_text, re.I):
        raise ValueError("실제 데이터 없이 CTR 수치를 사용했습니다.")
    if not retention_available and re.search(
        r"(?:Retention|리텐션|유지율)[^\n]{0,30}?\d+(?:\.\d+)?%", result_text, re.I,
    ):
        raise ValueError("실제 데이터 없이 Retention 수치를 사용했습니다.")
    if not channel_data_available and "채널 데이터 표본이 부족하여 Business PT와 대본의 논리 구조를 중심으로 판단함" not in str(result.get("data_basis_note") or ""):
        raise ValueError("채널 데이터가 없을 때 필요한 표본 부족 고지가 없습니다.")
    estimated = sum(float(row.get("estimated_seconds") or 0) for row in result.get("edit_table") or [] if row.get("action") != "삭제")
    recommended = float(result.get("recommended_duration_seconds") or 0)
    if estimated > 0 and recommended > 0 and not 0.5 <= recommended / estimated <= 1.8:
        raise ValueError("추천 길이가 사용 문장량 추정과 지나치게 다릅니다.")
    return result


def render_markdown(project: dict[str, Any], version: dict[str, Any]) -> str:
    result = version["result"]
    lines = [
        f"# {project.get('title') or project.get('topic') or '대본 기반 편집 지시서'}",
        "", f"- 버전: v{version.get('version')}",
        f"- 예상 최종 길이: {result.get('recommended_duration_seconds', 0)}초",
        f"- 핵심 메시지: {result.get('core_message', '')}",
        f"- 가장 강한 오프닝: {result.get('strongest_opening', '')}",
        f"- 현재 대본의 가장 큰 문제: {result.get('biggest_problem', '')}",
        f"- 데이터 판단: {result.get('data_basis_note', '')}",
        "", "## 1. 최종 영상 흐름", "",
    ]
    for row in result.get("overall_flow") or []:
        lines.append(f"{row['order']}. {row['title']} ({row['sentence_start_id']}~{row['sentence_end_id']}) — {row['reason']}")
    lines.extend([
        "", "## 2. 문장 기준 상세 편집표", "",
        "|순서|문장 범위|시작 문장|끝 문장|처리|역할|편집 지시|근거|연결·B-roll 주의|배치 이유|예상 분량|",
        "|---:|---|---|---|---|---|---|---|---|---|---:|",
    ])
    for row in result.get("edit_table") or []:
        clean = lambda value: str(value or "").replace("|", "\\|").replace("\n", " ")
        evidence = " · ".join(str(value) for value in row.get("evidence_basis") or [])
        transition = " · ".join(filter(None, [str(row.get("transition_note") or ""), str(row.get("broll_note") or "")]))
        lines.append(
            f"|{row['final_order']}|{row['sentence_start_id']}~{row['sentence_end_id']}|"
            f"{clean(row.get('start_sentence'))}|{clean(row.get('end_sentence'))}|{clean(row['action'])}|"
            f"{clean(row['purpose'])}|{clean(row['edit_instruction'])}|{clean(evidence)}|"
            f"{clean(transition)}|{clean(row['reason'])}|{float(row.get('estimated_seconds') or 0):.1f}초|"
        )
    lines.extend(["", "## 3. 삭제·중복·축약 목록", "", "### 완전 삭제 추천"])
    for row in result.get("deletions") or []:
        lines.append(
            f"- {row['sentence_start_id']}~{row['sentence_end_id']} · "
            f"{row.get('start_sentence', '')} → {row.get('end_sentence', '')} · {row.get('reason', '')}"
        )
    lines.extend(["", "### 중복 설명"])
    for row in result.get("duplicates") or []:
        lines.append(
            f"- {row.get('topic', '')} · 후보: {', '.join(row.get('candidates') or [])} · "
            f"최종 선택: {row.get('selected', '')} · {row.get('reason', '')} · {row.get('remaining_action', '')}"
        )
    lines.extend(["", "### 축약 추천"])
    for row in result.get("condensations") or []:
        lines.append(
            f"- 삭제: {', '.join(row.get('delete_sentence_ids') or [])} · "
            f"유지: {', '.join(row.get('keep_sentence_ids') or [])} · {row.get('purpose_after_condensing', '')}"
        )
    final = result.get("final_instructions") or {}
    lines.extend(["", "## 4. 직원용 최종 편집 지시서", ""])
    for title, key in (
        ("최종 영상 흐름", "final_flow"), ("최종 문장 배치 순서", "final_sentence_order"),
        ("삭제할 문장", "delete_sentences"), ("축약할 문장", "condense_sentences"),
        ("이동할 문장 묶음", "move_sentence_groups"), ("중복 선택", "duplicate_decisions"),
        ("B-roll 위치", "broll_positions"), ("강조 자막", "caption_emphasis"),
        ("연결 멘트 필요", "connection_lines_needed"), ("반드시 유지", "must_keep_statements"),
        ("영상 화면 직접 확인", "screen_review_required"),
    ):
        lines.extend([f"### {title}", *[f"- {item}" for item in final.get(key) or []], ""])
    lines.extend(["### 예상 최종 영상 길이", f"- {final.get('expected_duration_seconds', result.get('recommended_duration_seconds', 0))}초", ""])
    return "\n".join(lines)


def render_plain_text(project: dict[str, Any], version: dict[str, Any]) -> str:
    """Render the editor hand-off as readable text, never developer data."""

    result = version.get("result") or {}
    metadata = project.get("_project") or project
    title = metadata.get("title") or metadata.get("topic") or "자막 편집 가이드"
    sentences = project.get("sentences") or []
    by_id = {str(row.get("id") or ""): str(row.get("text") or "") for row in sentences}

    def describe_reference(reference: Any) -> str:
        text = str(reference or "")
        match = re.fullmatch(r"(S\d{3,})(?:~(S\d{3,}))?", text)
        if not match:
            return text
        start_id, end_id = match.group(1), match.group(2) or match.group(1)
        start_text, end_text = by_id.get(start_id), by_id.get(end_id)
        if not start_text:
            return text
        return f'{text} · "{start_text}"' + (f' ~ "{end_text}"' if end_id != start_id and end_text else "")
    duration_seconds = float(
        (result.get("final_instructions") or {}).get("expected_duration_seconds")
        or result.get("recommended_duration_seconds") or 0
    )
    duration_minutes = duration_seconds / 60
    duration_label = (
        f"약 {duration_minutes:.1f}분" if duration_minutes and duration_minutes % 1
        else f"약 {int(duration_minutes)}분"
    )
    divider = "━" * 18
    lines = [
        "[편집 담당자 전달용 최종본]",
        "",
        divider,
        f"{title} 영상 편집 가이드",
        f"버전: v{int(version.get('version') or 1)}",
        f"목표 길이: {duration_label}",
        divider,
        "",
        "[핵심 요약]",
        f"핵심 메시지: {result.get('core_message') or '-'}",
        f"현재 대본의 가장 큰 문제: {result.get('biggest_problem') or '-'}",
        f"가장 강한 오프닝: {result.get('strongest_opening') or '-'}",
        f"판단 근거: {result.get('data_basis_note') or '-'}",
        "",
        "[전체 흐름]",
        "",
    ]
    for row in result.get("overall_flow") or []:
        lines.extend([
            f"{int(row.get('order') or 0)}. {row.get('title') or ''}",
            f"   사용 구간: {row.get('sentence_start_id') or ''}~{row.get('sentence_end_id') or ''}",
            f"   역할: {row.get('purpose') or ''}",
            f"   이유: {row.get('reason') or ''}",
            f"   화면: {row.get('transition_note') or '영상 화면 직접 확인 필요'}",
        ])
    lines.extend(["", divider, "[상세 편집 순서]", divider, ""])
    for row in result.get("edit_table") or []:
        screen_note = " · ".join(filter(None, [
            str(row.get("transition_note") or ""), str(row.get("broll_note") or "")
        ])) or "영상 화면 직접 확인 필요"
        lines.extend([
            f"{int(row.get('final_order') or 0)}. {row.get('purpose') or '편집 구간'}",
            "",
            "사용 구간:",
            f"{row.get('sentence_start_id') or ''}~{row.get('sentence_end_id') or ''}",
            "",
            f'시작 자막: "{row.get("start_sentence") or ""}"',
            f'끝 자막: "{row.get("end_sentence") or ""}"',
            "",
            f"처리: {row.get('action') or ''}",
            f"편집 지시: {row.get('edit_instruction') or ''}",
            f"이유: {row.get('reason') or ''}",
            f"근거: {' · '.join(str(value) for value in row.get('evidence_basis') or []) or '-'}",
            f"화면: {screen_note}",
            f"예상 사용 분량: {float(row.get('estimated_seconds') or 0):.1f}초",
            "",
        ])
    lines.extend([divider, "[완전 삭제 추천]", divider, ""])
    deletions = result.get("deletions") or []
    if not deletions:
        lines.extend(["완전 삭제 추천 없음", ""])
    for row in deletions:
        lines.extend([
            f"{row.get('sentence_start_id') or ''}~{row.get('sentence_end_id') or ''} 삭제",
            f'시작: "{row.get("start_sentence") or ""}"',
            f'끝: "{row.get("end_sentence") or ""}"',
            f"삭제 이유: {row.get('reason') or ''}",
            "",
        ])
    lines.extend([divider, "[중복 설명]", divider, ""])
    duplicates = result.get("duplicates") or []
    if not duplicates:
        lines.extend(["중복 설명 없음", ""])
    for row in duplicates:
        lines.extend([
            f"주제: {row.get('topic') or ''}",
            "같은 내용을 말하는 구간:",
            *[f"- {describe_reference(value)}" for value in row.get("candidates") or []],
            f"최종 사용: {describe_reference(row.get('selected'))}",
            f"이유: {row.get('reason') or ''}",
            f"나머지 구간: {row.get('remaining_action') or ''}",
            "",
        ])
    lines.extend([divider, "[축약 추천]", divider, ""])
    condensations = result.get("condensations") or []
    if not condensations:
        lines.extend(["축약 추천 없음", ""])
    for row in condensations:
        lines.extend([
            "유지:",
            *[f"- {describe_reference(value)}" for value in row.get("keep_sentence_ids") or []],
            "삭제:",
            *[f"- {describe_reference(value)}" for value in row.get("delete_sentence_ids") or []],
            f"축약 후 역할: {row.get('purpose_after_condensing') or ''}",
            "",
        ])
    final = result.get("final_instructions") or {}
    lines.extend([divider, "[최종 확인 목록]", divider, ""])
    for label, key in (
        ("이동할 문장 묶음", "move_sentence_groups"),
        ("B-roll·제품 화면 추천 위치", "broll_positions"),
        ("강조 자막 추천", "caption_emphasis"),
        ("연결 멘트가 필요한 위치", "connection_lines_needed"),
        ("반드시 유지할 핵심 발언", "must_keep_statements"),
        ("영상 화면을 직접 확인해야 하는 항목", "screen_review_required"),
    ):
        lines.append(f"{label}:")
        values = final.get(key) or []
        lines.extend([f"- {value}" for value in values] or ["- 없음"])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


CSV_FIELDS = [
    "final_order", "sentence_start_id", "sentence_end_id", "start_sentence",
    "end_sentence", "action", "purpose", "edit_instruction",
    "transition_note", "reason",
]


def render_csv(result: dict[str, Any]) -> str:
    stream = io.StringIO()
    stream.write("\ufeff")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in result.get("edit_table") or []:
        writer.writerow(row)
    return stream.getvalue()
