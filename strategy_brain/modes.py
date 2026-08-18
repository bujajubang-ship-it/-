from __future__ import annotations

from dataclasses import dataclass

from .contracts import StrategyMode


COMMON_STRATEGY_POLICY = """당신은 부자주방 전담 콘텐츠 전략 파트너다.

- 데이터를 단순 요약하지 않는다. 데이터 → 패턴 → 원인 가설 → 의미 → 선택지 → 반대 근거 → 추천 행동 순으로 연결한다.
- 사용자의 의견에 자동으로 동의하지 않는다. 실제 채널 데이터와 다르면 숫자를 들어 명확히 반박한다.
- 일반적인 YouTube 조언보다 부자주방의 실제 성과, 과거 기획, 피드백, 워크시트, 파이프라인, 상담 기억, 저장 지식과 비즈니스PT 지식을 우선한다.
- 성공 패턴과 실패 패턴을 구분하고, 사용자가 놓친 반복 패턴을 먼저 알린다.
- 조회 가능한 사실은 추측하지 말고 현재 mode에 허용된 read-only 도구를 직접 사용한다. 질문과 무관한 데이터는 조회하지 않는다.
- 숫자(측정 사실)와 해석·가설·추천을 분리한다. 없는 숫자는 0으로 만들지 말고 unavailable/null로 표시한다.
- 모든 핵심 근거에 영상 제목/ID, 지식 제목, 측정 기간, source_as_of 또는 collected_at, 데이터 지연 여부와 표본수를 가능한 범위에서 표시한다.
- 근거가 부족하거나 오래됐으면 부족하다고 말하고, 그래도 지금 가능한 최선의 판단과 확인할 KPI를 제시한다.
- 한 번 주제가 결정되면 주제 → 타깃 시청자 → 왜 지금 → 핵심 메시지 → 제목 후보 → 최종 제목 → 썸네일 문구·구도 → 첫 5~15초 훅 → 전체 구조 → 촬영 컷 → 촬영 워크시트 → 업로드 후 KPI를 하나의 전략 가설로 끝까지 연결한다.
- 워크시트는 촬영자가 그대로 실행할 수 있게 장면, 대사 목적, 소품, 앵글, 확인사항을 구체적으로 쓴다.
- 과거 기획과 업로드 영상이 연결되어 있으면 "당시 판단"과 "실제 결과"를 비교해 다음 기획에 반영한다.
- 답변 기본 순서는 1) 결론 2) 데이터 근거 3) 놓쳤을 가능성 4) 추천 전략 5) 바로 실행할 다음 행동이다. 근거가 충분하면 애매하게 말하지 말고 하나를 1순위로 확정한다.
- 비즈니스PT 지식은 장식용 인용이 아니다. 관련 원칙만 골라 topic→제목→썸네일→훅→구조에 같은 논리로 적용하고, 필요할 때 '적용한 지식'과 '왜 적용했는지'를 짧게 밝힌다.
- 다음 영상 추천은 최대 3개이며 반드시 '내 추천은 이것'으로 1순위를 고른다. 각 후보에 지금 해야 하는 이유, 과거 근거, 타깃, 제목·썸네일·훅, 기대 KPI, 위험을 포함한다.
- 제목·썸네일 요청은 후보를 3~5개로 제한하고 과거 제목 구조·CTR/조회수/시청률·반복 표현을 비교한 뒤 1순위 제목과 1순위 썸네일을 각각 확정한다.

도구 선택 원칙:
- "다음 영상 뭐 찍을까?"라면 최근 채널 성과 → 비슷한 과거 영상 → retention → 관련 저장 지식/비즈니스PT → 현재 pipeline 순으로 확인하고, 외부 트렌드는 실제로 필요할 때만 본다.
- "채널 방향이 맞아?"라면 최근 성과와 과거 snapshot 변화, 성공/실패 주제, retention, 이전 결정과 pipeline을 비교한다.
- 특정 주제의 제목·썸네일 요청이라면 비슷한 과거 영상, retention, 관련 지식, 필요 시 최근 트렌드를 확인한다.
- 이전 tool 결과에 답이 있으면 같은 tool을 반복 호출하지 않는다.
"""


@dataclass(frozen=True)
class ModeSpec:
    mode: StrategyMode
    purpose: str
    allowed_tools: tuple[str, ...] = ()
    structured_output: bool = True
    streaming: bool = False
    reasoning_effort: str = "max"


PERFORMANCE_TOOLS = (
    "get_channel_strategy_snapshot",
    "get_recent_channel_performance",
    "compare_similar_videos",
    "get_video_performance",
    "get_retention_patterns",
    "analyze_title_thumbnail_patterns",
)
CONTENT_TOOLS = (
    "search_previous_plans",
    "search_previous_worksheets",
    "search_feedback_history",
    "get_content_pipeline",
)
KNOWLEDGE_TOOLS = (
    "search_knowledge",
    "search_business_pt_knowledge",
    "search_chat_memory",
    "search_long_term_memory",
)
MARKET_TOOLS = (
    "get_recent_trends",
)


MODE_REGISTRY: dict[StrategyMode, ModeSpec] = {
    StrategyMode.RESEARCH: ModeSpec(StrategyMode.RESEARCH, "시장과 시청자 문제를 조사한다.", MARKET_TOOLS, reasoning_effort="high"),
    StrategyMode.PLANNING: ModeSpec(StrategyMode.PLANNING, "하나의 콘텐츠 전략과 촬영 전 기획을 만든다.", PERFORMANCE_TOOLS + CONTENT_TOOLS + KNOWLEDGE_TOOLS + MARKET_TOOLS),
    StrategyMode.INTRO: ModeSpec(StrategyMode.INTRO, "제목 약속을 회수하는 초반 훅과 도입을 설계한다.", CONTENT_TOOLS + KNOWLEDGE_TOOLS + PERFORMANCE_TOOLS, reasoning_effort="high"),
    StrategyMode.SCRIPT: ModeSpec(StrategyMode.SCRIPT, "검증된 기획을 촬영 가능한 원고로 만든다.", CONTENT_TOOLS + KNOWLEDGE_TOOLS),
    StrategyMode.MIDFORM_PLANNING: ModeSpec(StrategyMode.MIDFORM_PLANNING, "미드폼의 주제부터 촬영 구조까지 통합 설계한다.", PERFORMANCE_TOOLS + CONTENT_TOOLS + KNOWLEDGE_TOOLS + MARKET_TOOLS),
    StrategyMode.SHORTFORM_PLANNING: ModeSpec(StrategyMode.SHORTFORM_PLANNING, "숏폼의 첫 3초와 저장·공유 행동을 설계한다.", PERFORMANCE_TOOLS + KNOWLEDGE_TOOLS + MARKET_TOOLS),
    StrategyMode.TOPIC_DISCOVERY: ModeSpec(StrategyMode.TOPIC_DISCOVERY, "다음 콘텐츠 후보를 데이터로 발굴하고 우선순위를 정한다.", PERFORMANCE_TOOLS + CONTENT_TOOLS + KNOWLEDGE_TOOLS + MARKET_TOOLS),
    StrategyMode.YOUTUBE_SEARCH_ANALYSIS: ModeSpec(StrategyMode.YOUTUBE_SEARCH_ANALYSIS, "YouTube 검색 결과에서 재현 가능한 성공 기제를 찾는다.", MARKET_TOOLS + PERFORMANCE_TOOLS),
    StrategyMode.CHANNEL_ANALYSIS: ModeSpec(StrategyMode.CHANNEL_ANALYSIS, "채널 성과 패턴과 병목을 진단한다.", PERFORMANCE_TOOLS + CONTENT_TOOLS),
    StrategyMode.UPLOAD_DECISION: ModeSpec(StrategyMode.UPLOAD_DECISION, "후보 콘텐츠의 업로드 순서와 리스크를 판단한다.", PERFORMANCE_TOOLS + CONTENT_TOOLS + MARKET_TOOLS),
    StrategyMode.EDIT_FEEDBACK: ModeSpec(StrategyMode.EDIT_FEEDBACK, "대본과 시장 약속을 비교해 편집 결정을 제안한다.", CONTENT_TOOLS + KNOWLEDGE_TOOLS + PERFORMANCE_TOOLS),
    StrategyMode.VIDEO_FEEDBACK: ModeSpec(StrategyMode.VIDEO_FEEDBACK, "타임라인 기준으로 편집자가 실행할 피드백을 만든다.", CONTENT_TOOLS + KNOWLEDGE_TOOLS + PERFORMANCE_TOOLS),
    StrategyMode.WORKSHEET: ModeSpec(StrategyMode.WORKSHEET, "승인된 전략을 촬영 가능한 워크시트로 변환한다.", CONTENT_TOOLS + KNOWLEDGE_TOOLS + MARKET_TOOLS),
    StrategyMode.JJACHI: ModeSpec(StrategyMode.JJACHI, "사용자가 제공한 현실을 조작 없이 인간적인 기획으로 정리한다.", KNOWLEDGE_TOOLS + MARKET_TOOLS),
    StrategyMode.SNS_CONVERSION: ModeSpec(StrategyMode.SNS_CONVERSION, "원 콘텐츠의 전략을 유지하며 채널별 형식으로 변환한다.", CONTENT_TOOLS + KNOWLEDGE_TOOLS, reasoning_effort="high"),
    StrategyMode.DETAIL_PAGE: ModeSpec(StrategyMode.DETAIL_PAGE, "시청자 문제와 제품 근거를 연결한 상세페이지를 기획한다.", KNOWLEDGE_TOOLS + MARKET_TOOLS),
    StrategyMode.BLOG: ModeSpec(StrategyMode.BLOG, "입력 자료를 근거로 검색·전환 목적의 글을 만든다.", CONTENT_TOOLS + KNOWLEDGE_TOOLS + MARKET_TOOLS, reasoning_effort="high"),
    # Intent-specific evidence is already assembled deterministically. Medium
    # reasoning preserves comparison quality while leaving enough output budget
    # for an interactive answer instead of spending it all on hidden reasoning.
    StrategyMode.STRATEGY_CHAT: ModeSpec(StrategyMode.STRATEGY_CHAT, "사용자와 장기 콘텐츠 방향을 함께 판단한다.", PERFORMANCE_TOOLS + CONTENT_TOOLS + KNOWLEDGE_TOOLS + MARKET_TOOLS, structured_output=False, streaming=True, reasoning_effort="medium"),
    StrategyMode.POSTMORTEM: ModeSpec(StrategyMode.POSTMORTEM, "기획 가설과 실제 성과를 비교해 재사용할 교훈을 만든다.", PERFORMANCE_TOOLS + CONTENT_TOOLS + KNOWLEDGE_TOOLS),
}


def get_mode_spec(mode: StrategyMode) -> ModeSpec:
    try:
        return MODE_REGISTRY[mode]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy mode: {mode}") from exc


def build_instructions(mode: StrategyMode, task_instructions: str) -> str:
    spec = get_mode_spec(mode)
    return (
        f"{COMMON_STRATEGY_POLICY}\n\n"
        f"[현재 업무 mode: {mode.value}]\n{spec.purpose}\n\n"
        f"[이번 작업 지침]\n{task_instructions.strip()}"
    ).strip()
