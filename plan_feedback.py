"""기획 피드백 — 촬영 전에 기획안을 검사한다.

편집 피드백은 이미 찍은 대본을 손보는 것이라 늦다. 촬영을 망치면 편집으로 못 살린다.
그래서 찍기 전에 기획을 먼저 본다.

여기서는 사장님 채널 수치를 쓰지 않는다. 친구 채널에 부자주방 숫자를 갖다 대면
틀린 조언이 나온다. 대신 지식에 저장해 둔 **일반 영상 제작 원칙**만 근거로 쓴다.
"""

from __future__ import annotations

import json
from typing import Any

from edit_analysis_service import EditAnalysisService


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }


SCORE_ITEM_SCHEMA = _object({
    "name": {"type": "string"},
    "score": {"type": "integer"},
    "verdict": {"type": "string"},
    "why": {"type": "string"},
})

FIX_SCHEMA = _object({
    "priority": {"type": "integer"},
    "problem": {"type": "string"},
    "fix": {"type": "string"},
    "example": {"type": "string"},
})

PLAN_FEEDBACK_SCHEMA: dict[str, Any] = _object({
    "total_score": {"type": "integer"},
    "one_line": {"type": "string"},
    "scores": {"type": "array", "items": SCORE_ITEM_SCHEMA},
    "biggest_problem": {"type": "string"},
    "fixes": {"type": "array", "items": FIX_SCHEMA},
    "title_ideas": {"type": "array", "items": {"type": "string"}},
    "opening_15s": {"type": "string"},
    "shoot_checklist": {"type": "array", "items": {"type": "string"}},
    "basis_note": {"type": "string"},
})

# 점수 항목은 고정한다. 매번 다른 잣대로 재면 지난번과 견줄 수 없다.
SCORE_ITEMS = (
    "주제 선명함", "시청자 문제", "훅(첫 15초)", "구성 흐름",
    "증거·구체성", "제목·썸네일 가능성", "길이 적정",
)

SYSTEM_INSTRUCTIONS = """당신은 유튜브 영상 기획을 촬영 전에 검사하는 사람이다.
기획안을 읽고 이대로 찍으면 어떻게 될지 정직하게 말한다. 좋게 포장하지 않는다.

점수는 항목마다 0~20점, 합계 100점 만점으로 매긴다. 항목은 주어진 일곱 가지를 그대로 쓴다.
- 60점 미만이면 이대로 찍지 말라고 분명히 말한다.
- 점수를 후하게 주지 않는다. 근거 없이 높은 점수를 주면 기획자가 그대로 찍는다.

fixes 는 고칠 것을 중요한 순서로 적는다. priority 1이 가장 급한 것이다.
- problem 은 무엇이 문제인지, fix 는 어떻게 바꾸는지, example 은 바꾼 예시 문장이다.
- '더 구체적으로 하세요' 같은 말은 쓰지 않는다. 실제로 쓸 수 있는 문장을 준다.

opening_15s 는 첫 15초에 할 말을 직접 써 준다. 시청자가 왜 계속 봐야 하는지가 담겨야 한다.
title_ideas 는 3~5개를 준다. 낚시성 제목은 쓰지 않는다.
shoot_checklist 는 촬영장에서 빠뜨리기 쉬운 것을 적는다(화면·소품·증거 장면).

근거로 쓸 수 있는 것은 함께 주어지는 '일반 영상 제작 원칙'뿐이다.
- 조회수·CTR·유지율 같은 수치를 만들어 내지 않는다. 주어지지 않은 숫자는 쓰지 않는다.
- 특정 채널의 실적을 아는 척하지 않는다.
- basis_note 에는 어떤 원칙을 근거로 삼았는지 한두 문장으로 적는다.
  근거로 쓸 원칙이 없으면 '일반적인 영상 구성 원칙으로 판단했습니다'라고만 쓴다.
  데이터 표본이나 통계가 부족하다는 말은 쓰지 않는다."""


class PlanFeedbackService:
    def __init__(self, analysis: EditAnalysisService | None = None) -> None:
        self.analysis = analysis or EditAnalysisService()

    async def review(
        self, *, keyword: str, plan_text: str, principles: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        prompt = json.dumps({
            "keyword": keyword,
            "plan": plan_text,
            "score_items": list(SCORE_ITEMS),
            "general_principles": principles or [],
        }, ensure_ascii=False, default=str)
        return await self.analysis._structured(
            prompt=prompt, instructions=SYSTEM_INSTRUCTIONS,
            schema=PLAN_FEEDBACK_SCHEMA, schema_name="plan_feedback",
            reasoning_effort="high", allow_anthropic=True,
        )


def validate_feedback(result: dict[str, Any]) -> dict[str, Any]:
    """점수가 앞뒤로 맞는지 본다. 합계만 높고 항목은 낮으면 기획자가 오해한다."""
    if not isinstance(result, dict):
        raise ValueError("기획 피드백 결과가 객체가 아닙니다.")
    scores = result.get("scores") or []
    names = [str(row.get("name") or "") for row in scores]
    missing = [name for name in SCORE_ITEMS if name not in names]
    if missing:
        raise ValueError(f"점수 항목이 빠졌습니다: {', '.join(missing)}")
    for row in scores:
        value = int(row.get("score") or 0)
        if not 0 <= value <= 20:
            raise ValueError(f"항목 점수는 0~20점이어야 합니다: {row.get('name')} {value}")
    item_sum = sum(int(row.get("score") or 0) for row in scores)
    total = int(result.get("total_score") or 0)
    if abs(total - item_sum) > 5:
        raise ValueError(f"합계 점수와 항목 합이 다릅니다: 합계 {total} · 항목 합 {item_sum}")
    if not (result.get("fixes") or []):
        raise ValueError("고칠 점이 하나도 없습니다.")
    return result
