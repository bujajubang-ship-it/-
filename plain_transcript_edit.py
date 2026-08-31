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
import os
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


def _is_subsequence(polished: str, original: str) -> bool:
    """다듬은 자막이 원문에서 '빼기만' 한 것인지 본다.

    Vrew 자막은 이미 찍은 영상을 받아 적은 것이라, 원문에 없는 글자가 자막에 들어가면
    사장님이 하지 않은 말이 화면에 뜬다. 그래서 맞춤법 교정조차 허용하지 않는다.
    띄어쓰기만 자유롭게 둔다(줄 넘김·붙여쓰기는 말을 바꾸지 않는다).
    """
    source = re.sub(r"\s+", "", original)
    target = re.sub(r"\s+", "", polished)
    position = 0
    for char in target:
        position = source.find(char, position)
        if position < 0:
            return False
        position += 1
    return True


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

CAPTION_POLISH_SCHEMA = _object({
    "sentence_id": {"type": "string"},
    "original_text": {"type": "string"},
    "polished_text": {"type": "string"},
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
    "caption_polish": {"type": "array", "items": CAPTION_POLISH_SCHEMA},
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


try:
    from analyzer import CHANNEL_GOALS, CONTENT_CREATION_RULES
except Exception:                       # 분석기를 못 불러와도 가이드는 만들어야 한다
    CHANNEL_GOALS = CONTENT_CREATION_RULES = ""

# 첫 장면을 이런 말로 시작하면 도입부가 말토막처럼 들린다.
def can_open(text: str) -> bool:
    """이 문장으로 영상을 시작해도 말이 되나. 검증과 프롬프트가 같은 잣대를 쓴다."""
    text = str(text or "").strip()
    if len(text) < 6:
        return False
    if any(text.startswith(word) for word in OPENING_STOPWORDS):
        return False
    words = text.split()
    return not any(words[i] == words[i + 1] for i in range(len(words) - 1))


OPENING_STOPWORDS = (
    "그리고", "그래서", "그러면", "그럼", "그런데", "근데", "또한", "또", "그러니까",
    "마지막으로", "다음으로", "이제", "아니", "자 그럼", "여기서",
)

SYSTEM_INSTRUCTIONS = f"""당신은 부자주방 편집 담당 직원에게 전달할 대본 기반 영상 흐름 설계자다.
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
결과는 편집자가 그대로 실행할 수 있게 구체적으로 작성한다.

【첫 장면(도입부)을 고르는 규칙 — 가장 중요하다】
- 첫 장면의 **맨 첫 문장은 그 자체로 말이 되는 완결된 문장**이어야 한다.
- 다음으로 시작하는 문장은 첫 장면의 시작으로 쓰지 않는다:
  '그리고', '그래서', '그러면', '그런데', '근데', '또', '마지막으로', '다음으로', '이제'.
  앞 내용을 받는 말이라 처음 보는 시청자에게는 말이 끊긴 것처럼 들린다.
- 같은 낱말을 더듬어 되풀이한 문장('중앙 중앙은 중앙에는')도 첫 장면의 시작으로 쓰지 않는다.
- 좋은 시작: 시청자의 문제를 말하는 문장, 결과·완성 장면을 가리키는 문장, 숫자가 든 문장.
- 쓰고 싶은 구간의 첫 문장이 위 조건에 안 맞으면, **그 구간 안에서 조건에 맞는 문장부터**
  시작하도록 sentence_start_id 를 뒤로 옮긴다. 구간 자체를 포기하지 말고 시작점을 고쳐라.

【채널 목표】
{CHANNEL_GOALS}
첫 30초에 시청자가 '내 문제구나' 또는 '결과가 저거구나'를 알아야 이탈률 목표를 지킨다.

{CONTENT_CREATION_RULES}

caption_polish 는 화면에 뜨는 자막에서 군더더기를 '빼기만' 하는 목록이다.
이 자막은 이미 찍은 영상을 받아 적은 것이다. 원문에 없는 글자가 자막에 들어가면
사장님이 하지 않은 말이 화면에 뜬다. 그러므로 다음을 반드시 지킨다.
- **낱말을 빼는 것만 한다.** 글자를 새로 넣거나 다른 글자로 바꾸지 않는다.
  맞춤법 교정도 하지 않는다(원문에 없는 글자가 되기 때문이다). 띄어쓰기만 자유롭게 고친다.
- 뺄 수 있는 것: 군더더기('어', '음', '자', '아', '뭐', '이제'), 더듬어 되풀이한 말,
  문장 끝에 붙은 의미 없는 꼬리말.
- 뺀 뒤에도 말이 되는 문장이어야 한다. 뜻이 달라지면 그 문장은 목록에 넣지 않는다.
- 최종 사용하는 문장 중 뺄 것이 있는 것만 넣는다. 삭제할 문장은 넣지 않는다.
- polished_text 가 original_text 와 같으면 안 된다.
- original_text 는 해당 문장 원문과 글자까지 정확히 같아야 한다."""


class PlainTranscriptEditService:
    def __init__(self, analysis: EditAnalysisService | None = None) -> None:
        self.analysis = analysis or EditAnalysisService()

    async def analyze(
        self, *, request: dict[str, Any], sentences: list[dict[str, Any]],
        duplicates: list[dict[str, Any]], evidence: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = json.dumps({
            "project": request,
            # 도입부로 쓸 수 있는 문장인지 미리 표시해 준다. 말로만 규칙을 주면
            # 좋은 구간을 고르고도 그 구간의 첫 줄이 말토막인 걸 놓친다.
            "sentences": [
                {"id": row["id"], "text": row["text"], "can_open": can_open(row["text"])}
                for row in sentences
            ],
            "opening_rule": "첫 장면의 sentence_start_id 는 can_open 이 true 인 문장이어야 한다.",
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
            reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "high").strip() or "high",
            allow_anthropic=False,
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
    # 축약·중복 목록은 참고 자료다. 편집자에게 나가는 지시문에는 쓰이지 않는다.
    # 여기에 없는 문장 번호가 하나 섞였다고 몇 분짜리 분석을 통째로 버리지 않는다.
    # 모르는 번호는 조용히 빼고, 실제로 편집을 움직이는 표(edit_table·deletions)만
    # 위에서 엄격하게 본다.
    condensed_keep: set[str] = set()
    condensed_delete: set[str] = set()
    kept_condensations = []
    for row in result.get("condensations") or []:
        keep_ids = {str(v) for v in row.get("keep_sentence_ids") or []} & set(by_id)
        delete_ids = {str(v) for v in row.get("delete_sentence_ids") or []} & set(by_id)
        delete_ids -= keep_ids                 # 같은 문장을 둘 다 지정하면 살리는 쪽을 따른다
        if not (keep_ids or delete_ids):
            continue
        row["keep_sentence_ids"] = sorted(keep_ids)
        row["delete_sentence_ids"] = sorted(delete_ids)
        kept_condensations.append(row)
        condensed_keep.update(keep_ids)
        condensed_delete.update(delete_ids)
    if "condensations" in result:
        result["condensations"] = kept_condensations
    for row in result.get("duplicates") or []:
        row["candidates"] = [str(v) for v in row.get("candidates") or [] if str(v) in by_id]
    if deleted & condensed_keep:
        raise ValueError("완전 삭제와 축약 유지가 동시에 지정된 문장이 있습니다.")
    # 같은 문장을 '살릴 것'과 '지울 것'에 동시에 넣는 일이 잦다(긴 대본일수록 심하다).
    # 최종 배치가 편집자가 따라가는 표이므로 그쪽을 살리고, 삭제 목록에서 뺀다.
    # 예전에는 여기서 통째로 거부해 몇 분짜리 분석을 두 번 버렸다.
    conflict = deleted & final_used
    if conflict:
        result["_delete_conflicts"] = sorted(conflict)[:50]
        deleted -= conflict
    # 첫 장면이 접속사나 더듬은 말로 시작하면 도입부가 말토막처럼 들린다.
    # 모델이 규칙을 어겼는지 코드로도 본다 — 결과는 살리되 눈에 띄게 남긴다.
    opening = sorted(
        (row for row in result.get("edit_table") or [] if row.get("action") != "삭제"),
        key=lambda row: int(row.get("final_order") or 0))
    if opening:
        first_id = str(opening[0].get("sentence_start_id") or "")
        first_text = str((by_id.get(first_id) or {}).get("text") or "").strip()
        problems = []
        if any(first_text.startswith(word) for word in OPENING_STOPWORDS):
            problems.append("앞 내용을 받는 말로 시작합니다")
        words = first_text.split()
        if any(words[i] == words[i + 1] for i in range(len(words) - 1)):
            problems.append("같은 말을 더듬어 되풀이합니다")
        if not problems and not can_open(first_text):
            problems.append("도입부로 쓰기엔 너무 짧습니다")
        if problems:
            result["_opening_warnings"] = {
                "sentence_id": first_id, "text": first_text, "problems": problems,
            }

    # 자막 다듬기는 '화면 글자만' 손대는 것이다. 말에 없던 낱말이 자막으로 새로 생기면
    # 시청자에게는 하지 않은 말을 한 것이 되므로, 원문에서 멀어지면 통째로 막는다.
    polished_ids: set[str] = set()
    for row in result.get("caption_polish") or []:
        sentence_id = str(row.get("sentence_id") or "")
        if sentence_id not in by_id:
            raise ValueError(f"자막 다듬기에 존재하지 않는 문장 ID: {sentence_id}")
        if sentence_id in polished_ids:
            raise ValueError(f"같은 문장을 두 번 다듬도록 지정했습니다: {sentence_id}")
        polished_ids.add(sentence_id)
        original = by_id[sentence_id]["text"]
        if str(row.get("original_text") or "") != original:
            raise ValueError(f"자막 다듬기 원문 불일치: {sentence_id}")
        polished = str(row.get("polished_text") or "").strip()
        if not polished:
            raise ValueError(f"다듬은 자막이 비어 있습니다: {sentence_id}")
        if polished == original:
            raise ValueError(f"다듬기 전후가 같습니다: {sentence_id}")
        if not _is_subsequence(polished, original):
            raise ValueError(
                f"자막에 원문에 없는 글자가 들어갔습니다: {sentence_id} "
                f"(빼는 것만 됩니다. 원문 {original!r} → {polished!r})"
            )
    if polished_ids & deleted:
        raise ValueError(f"삭제할 문장을 다듬도록 지정했습니다: {sorted(polished_ids & deleted)}")
    # 설명 문장 안에 엉뚱한 번호를 한 번 쓴 것만으로 분석을 버리지 않는다.
    # 편집을 움직이는 구간은 위에서 이미 문장 단위로 확인했다.
    unknown = {ref for ref in _SENTENCE_REF.findall(_all_text(result)) if ref not in by_id}
    if unknown:
        result["_id_warnings"] = sorted(unknown)[:20]
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


def render_vrew_prompt(project: dict[str, Any], version: dict[str, Any]) -> str:
    """Vrew 에이전트 입력창에 통째로 붙여넣는 지시문.

    Vrew는 자막 한 줄이 클립 하나다. 그래서 문장 ID가 아니라 **자막 글자 그대로**를
    지시어에 넣어야 에이전트가 클립을 찾는다.

    사장님 확인(2026-08-27): 삭제·재배치·자막 다듬기를 한 번에 줘도 에이전트가
    소화한다. 대신 표기 규칙을 앞에서 설명해 줘야 한다 — 같은 말을 여러 번 찍은
    테이크에서 어느 것을 지울지, '부터~까지'가 묶음이라는 것을 모르면 엉뚱하게 자른다.
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

    scenes_for_gap = sorted(result.get("edit_table") or [],
                            key=lambda row: int(row.get("final_order") or 0))
    kept_orders: set[int] = set()
    for row in scenes_for_gap:
        for item in rows_of(row):
            kept_orders.add(int(item.get("order") or 0))

    # 지울 것은 따로 받지 않고 **남길 것의 빈자리**로 정한다.
    # 모델이 같은 문장을 살릴 것과 지울 것에 동시에 넣는 일이 잦은데,
    # 이렇게 만들면 그런 모순이 생길 수 없다.
    delete_runs: list[list[dict[str, Any]]] = []
    run: list[dict[str, Any]] = []
    for item in sentences:
        order = int(item.get("order") or 0)
        text = str(item.get("text") or "").strip()
        if order in kept_orders or not text:
            if run:
                delete_runs.append(run); run = []
            continue
        run.append(item)
    if run:
        delete_runs.append(run)
    delete_items = [item for group in delete_runs for item in group]

    def where_note(item: dict[str, Any]) -> str:
        """같은 자막이 여러 번 찍혔으면 몇 번째인지 밝힌다. 경계를 잘못 잡으면 엉뚱한 데가 잘린다."""
        text = str(item.get("text") or "").strip()
        orders = order_by_text.get(text) or []
        if len(orders) < 2:
            return ""
        try:
            nth = orders.index(int(item.get("order") or 0)) + 1
        except ValueError:
            return ""
        return f" (똑같은 자막 {len(orders)}개 중 {nth}번째)"

    delete_lines: list[str] = []
    for number, group in enumerate(delete_runs, 1):
        first, last = group[0], group[-1]
        if len(group) == 1:
            delete_lines.append(
                f'{number}. "{str(first.get("text")).strip()}"{where_note(first)}  — 1줄')
        else:
            delete_lines.append(
                f'{number}. "{str(first.get("text")).strip()}"{where_note(first)}\n'
                f'   부터 "{str(last.get("text")).strip()}"{where_note(last)} 까지  — {len(group)}줄')

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

    polish_lines = [
        f'- "{str(row.get("original_text") or "").strip()}"'
        f' → "{str(row.get("polished_text") or "").strip()}"'
        for row in result.get("caption_polish") or []
        if str(row.get("polished_text") or "").strip()
    ]

    total_delete = len(delete_items)
    total_keep = sum(len(rows_of(row)) for row in scenes)

    lines = [
        "이 영상은 편집 전 촬영 원본이야. NG 테이크와 촬영 중 나눈 잡담이 섞여 있어.",
        "아래 세 가지를 순서대로 해줘. 한 단계를 끝내고 다음 단계로 넘어가.",
        "",
        "★ 규칙: 영상에서 이미 말한 것 말고 새로운 말을 만들지 마.",
        "  지우기, 순서 바꾸기, 낱말 빼기만 해.",
        "",
        f"[1단계] 아래 {len(delete_lines)}개 덩어리를 삭제해 줘. (자막 {total_delete}줄이 지워져야 해)",
        '- 각 줄은 「"시작자막" 부터 "끝자막" 까지」 형식이야.',
        "  그 두 자막과 사이에 있는 자막을 한 덩어리로 통째로 지워.",
        '- 따옴표가 하나만 있는 줄은 그 자막 한 개만 지우면 돼.',
        "- 끝에 적힌 줄 수(— N줄)와 실제로 지워진 줄 수가 같은지 확인해 줘.",
        '- "똑같은 자막 N개 중 M번째"라고 적힌 것은 조심해. 앞에서부터 세서 그 자리를 잡아.',
        "",
    ]
    lines.extend(delete_lines or ["1. (지울 자막 없음)"])
    lines.extend([
        "",
        f"[2단계] 남은 자막을 아래 {len(order_lines)}개 덩어리 순서대로 다시 배열해 줘. "
        f"(자막 {total_keep}줄이 남아야 해)",
        '- 각 줄은 「"시작자막" 부터 "끝자막" 까지」 형식이야. 덩어리 안의 순서는 바꾸지 마.',
        "- 이 단계에서는 자막을 지우거나 글자를 고치지 마. 위치만 옮겨.",
        "",
    ])
    lines.extend(order_lines or ["1. (순서 변경 없음)"])
    if polish_lines:
        lines.extend([
            "",
            f"[3단계] 아래 {len(polish_lines)}개 자막에서 군더더기 말을 빼 줘.",
            "- 화면에 뜨는 자막 글자만 고치는 거야. 영상은 자르지 마.",
            '- 「원래 자막」 → 「바꿀 자막」 형식이야. 오른쪽은 왼쪽에서 낱말을 뺀 것뿐이야.',
            "- 목록에 없는 자막은 그대로 둬. 알아서 더 다듬지 마.",
            "",
        ])
        lines.extend(polish_lines)
    lines.extend([
        "",
        "다 끝나면 자막이 몇 줄 남았는지 알려줘.",
    ])
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
