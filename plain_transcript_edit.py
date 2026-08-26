"""Plain-transcript editing guidance with immutable sentence identifiers.

This module does not inspect video, upload media, or render anything. It keeps
the user's original sentences as the only legal source of spoken content and
turns model output into validated editor-facing documents.
"""

from __future__ import annotations

import csv
import difflib
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
    """편집 담당자에게 그대로 넘기는 문서.

    사장님 지시(2026-08-26): 여기에는 **장면 순서와 자막 문장만** 넣는다.
    S코드·이유·근거·화면 지시·분석은 넣지 않는다 — 편집자가 헷갈리기만 한다.
    시작·끝 두 줄만 주는 것도 안 된다. 가운데를 안 보여주면 말이 제대로
    끝난 건지 편집자가 판단할 수 없다(도치문을 잘린 문장으로 오해한다).
    대신 **비슷한 자막이 다른 데도 있어 잘못 자를 수 있는 자리**는 반드시 짚어 준다.
    """

    result = version.get("result") or {}
    metadata = project.get("_project") or project
    title = metadata.get("title") or metadata.get("topic") or "자막 편집 가이드"
    sentences = project.get("sentences") or []

    def texts(row: dict[str, Any]) -> list[str]:
        start_id = str(row.get("sentence_start_id") or "")
        end_id = str(row.get("sentence_end_id") or start_id)
        try:
            return [str(item.get("text") or "").strip()
                    for item in sentence_range(sentences, start_id, end_id)]
        except ValueError:
            # 범위를 못 찾으면 최소한 시작·끝 문장이라도 남긴다.
            return [str(value).strip() for value in
                    (row.get("start_sentence"), row.get("end_sentence")) if value]

    scenes = sorted(result.get("edit_table") or [],
                    key=lambda row: int(row.get("final_order") or 0))
    deletions = result.get("deletions") or []

    # 지울 자막을 미리 모아 둔다 — 남길 자막과 닮은 것을 찾아 경고하기 위해서다.
    deleted_lines: list[tuple[str, int]] = []
    for index, row in enumerate(deletions, 1):
        for line in texts(row):
            if line:
                deleted_lines.append((line, index))

    def warnings_for(scene_lines: list[str]) -> list[str]:
        notes: list[str] = []
        edges = [line for line in (scene_lines[:1] + scene_lines[-1:]) if line]
        for edge in edges:
            for line, number in deleted_lines:
                if edge == line:
                    notes.append(
                        f"⚠️ 똑같은 문장이 삭제 {number}번에도 있습니다. 여기 것만 살립니다.")
                elif difflib.SequenceMatcher(None, edge, line).ratio() >= 0.75:
                    notes.append(
                        f'⚠️ 지울 자막 중에 "{line}"(삭제 {number}번)가 있습니다. '
                        "비슷하니 헷갈리지 마세요.")
        return list(dict.fromkeys(notes))

    divider = "\u2501" * 18
    lines = [
        f"{title} — 편집 순서",
        "",
        "\u203b 아래 순서대로 이어 붙이면 됩니다. 각 장면의 자막을 전부 적었습니다.",
        "\u203b \u26a0\ufe0f 는 비슷한 자막이 다른 데도 있어 헷갈리기 쉬운 곳입니다.",
        "",
    ]
    for number, row in enumerate(scenes, 1):
        scene_lines = [line for line in texts(row) if line]
        lines.append(f"{number}번째 장면  (자막 {len(scene_lines)}줄)")
        lines.extend(f"  {line}" for line in scene_lines)
        lines.extend(f"  {note}" for note in warnings_for(scene_lines))
        lines.append("")

    lines.extend(["", divider, "삭제할 자막", divider, ""])
    if not deletions:
        lines.extend(["삭제할 자막 없음", ""])
    for number, row in enumerate(deletions, 1):
        delete_lines = [line for line in texts(row) if line]
        lines.append(f"{number}.  (자막 {len(delete_lines)}줄)")
        lines.extend(f"  {line}" for line in delete_lines)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_vrew_prompt(project: dict[str, Any], version: dict[str, Any]) -> str:
    """Vrew 에이전트 입력창에 그대로 붙여넣는 지시문.

    Vrew는 자막 한 줄이 클립 하나다. 그래서 사람이 읽는 가이드와 달리
    **자막 문장 그대로**를 지시어에 넣어야 에이전트가 클립을 찾을 수 있다.

    지우기와 순서 바꾸기를 한 덩어리로 주면 에이전트가 절반만 하고 끝나는 일이
    생긴다. 무료 요청 횟수도 제한돼 있으므로 **1단계(삭제) / 2단계(순서)** 로 나눠
    각각 따로 붙여넣게 한다.

    똑같은 자막이 여러 번 나오는 경우(재촬영 테이크)에는 몇 번째 것인지 함께 적는다.
    안 그러면 살려야 할 테이크가 지워진다.
    """

    result = version.get("result") or {}
    sentences = project.get("sentences") or []
    order_by_text: dict[str, list[int]] = {}
    for row in sentences:
        text = str(row.get("text") or "").strip()
        if text:
            order_by_text.setdefault(text, []).append(int(row.get("order") or 0))

    def rows_of(row: dict[str, Any]) -> list[dict[str, Any]]:
        start_id = str(row.get("sentence_start_id") or "")
        end_id = str(row.get("sentence_end_id") or start_id)
        try:
            return sentence_range(sentences, start_id, end_id)
        except ValueError:
            return []

    delete_items: list[dict[str, Any]] = []
    for row in result.get("deletions") or []:
        delete_items.extend(item for item in rows_of(row) if str(item.get("text") or "").strip())
    deleted_orders = {int(item.get("order") or 0) for item in delete_items}

    def occurrence_note(item: dict[str, Any]) -> str:
        """같은 자막이 여러 번 찍혔으면 몇 번째를 지울지 밝힌다.

        전부 지우는 경우에는 번호를 붙이지 않는다 — 같은 줄이 여러 번 나오면
        에이전트가 어느 하나만 지우라는 뜻으로 읽는다.
        """
        text = str(item.get("text") or "").strip()
        orders = order_by_text.get(text) or []
        if len(orders) < 2:
            return ""
        if all(order in deleted_orders for order in orders):
            return f"   ← 똑같은 자막 {len(orders)}개 모두"
        try:
            nth = orders.index(int(item.get("order") or 0)) + 1
        except ValueError:
            return ""
        return f"   ← 똑같은 자막이 {len(orders)}개 있는데 그중 {nth}번째 것만"

    delete_lines: list[str] = []
    seen_all_deleted: set[str] = set()
    for item in delete_items:
        text = str(item.get("text") or "").strip()
        note = occurrence_note(item)
        if note.endswith("모두"):
            if text in seen_all_deleted:
                continue          # 같은 줄을 두 번 적지 않는다
            seen_all_deleted.add(text)
        delete_lines.append(f"- {text}{note}")

    scenes = sorted(result.get("edit_table") or [],
                    key=lambda row: int(row.get("final_order") or 0))
    order_lines: list[str] = []
    for number, row in enumerate(scenes, 1):
        items = [str(item.get("text") or "").strip() for item in rows_of(row)]
        items = [text for text in items if text]
        if not items:
            continue
        if len(items) == 1:
            order_lines.append(f'{number}. "{items[0]}"')
        else:
            order_lines.append(f'{number}. "{items[0]}" 부터 "{items[-1]}" 까지')

    divider = "\u2501" * 18
    lines = [
        "[Vrew 에이전트에 붙여넣을 지시문]",
        "",
        "\u203b 1단계와 2단계를 **따로** 붙여넣으세요. 한 번에 주면 절반만 하고 멈춥니다.",
        "\u203b 1단계를 끝내고 결과를 확인한 뒤 2단계를 주세요.",
        "",
        divider,
        "1단계 — 지울 자막 (여기부터 복사)",
        divider,
        "",
        "아래 자막들을 삭제해 줘. 목록에 적힌 자막만 지우고 나머지는 그대로 둬.",
        "자막 내용을 고치거나 새로 쓰지는 마.",
        "",
    ]
    lines.extend(delete_lines or ["- (지울 자막 없음)"])
    lines.extend([
        "",
        divider,
        "2단계 — 남은 자막 순서 (1단계를 끝낸 뒤 복사)",
        divider,
        "",
        "남은 자막을 아래 순서대로 옮겨 줘. 번호가 최종 순서야.",
        "자막을 새로 지우거나 내용을 고치지는 마.",
        "",
    ])
    lines.extend(order_lines or ["1. (순서 변경 없음)"])
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
