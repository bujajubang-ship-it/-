from __future__ import annotations

from dataclasses import dataclass

from .contracts import StrategyMode


COMMON_STRATEGY_POLICY = """당신은 부자주방의 공통 콘텐츠 전략 두뇌다.

- 사용자의 의견에 자동으로 동의하지 말고 데이터가 반대하면 분명하게 설명한다.
- 데이터 → 패턴 → 원인 가설 → 의미 → 선택지 → 반대 근거 → 추천 행동 순으로 판단한다.
- 조회 가능한 사실을 추측하지 말고 현재 mode에 허용된 read-only 도구를 사용한다.
- 관련 없는 데이터를 습관적으로 조회하지 않는다.
- 없는 숫자는 0으로 만들지 말고 unavailable 또는 null로 명시한다.
- AI 제안, 사용자 결정, 측정된 사실을 서로 다른 것으로 취급한다.
- 근거에는 가능한 한 출처, 수집일, 측정 기간, 최신성, 표본수를 함께 제시한다.
- 제목, 썸네일, 훅, 본문, 워크시트는 하나의 전략 가설로 연결한다.
- 모르는 것은 만들어내지 말고 판단을 위해 추가로 필요한 데이터를 말한다.
"""


@dataclass(frozen=True)
class ModeSpec:
    mode: StrategyMode
    purpose: str
    allowed_tools: tuple[str, ...] = ()
    structured_output: bool = True
    streaming: bool = False


PERFORMANCE_TOOLS = (
    "get_recent_videos",
    "get_channel_performance",
    "compare_recent_videos",
    "compare_similar_videos",
    "get_video_metrics",
)
CONTENT_TOOLS = (
    "get_content_project",
    "search_past_plans",
    "get_worksheet",
    "get_video_feedback",
    "get_pipeline_items",
)
KNOWLEDGE_TOOLS = (
    "search_businesspt_knowledge",
    "search_knowledge",
    "search_strategy_decisions",
    "search_previous_conversations",
)
MARKET_TOOLS = (
    "search_youtube",
    "get_youtube_comments",
    "search_naver_cafe",
    "search_market_trends",
)


MODE_REGISTRY: dict[StrategyMode, ModeSpec] = {
    StrategyMode.RESEARCH: ModeSpec(StrategyMode.RESEARCH, "시장과 시청자 문제를 조사한다.", MARKET_TOOLS),
    StrategyMode.PLANNING: ModeSpec(StrategyMode.PLANNING, "하나의 콘텐츠 전략과 촬영 전 기획을 만든다.", PERFORMANCE_TOOLS + CONTENT_TOOLS + KNOWLEDGE_TOOLS + MARKET_TOOLS),
    StrategyMode.INTRO: ModeSpec(StrategyMode.INTRO, "제목 약속을 회수하는 초반 훅과 도입을 설계한다.", CONTENT_TOOLS + KNOWLEDGE_TOOLS + PERFORMANCE_TOOLS),
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
    StrategyMode.SNS_CONVERSION: ModeSpec(StrategyMode.SNS_CONVERSION, "원 콘텐츠의 전략을 유지하며 채널별 형식으로 변환한다.", CONTENT_TOOLS + KNOWLEDGE_TOOLS),
    StrategyMode.DETAIL_PAGE: ModeSpec(StrategyMode.DETAIL_PAGE, "시청자 문제와 제품 근거를 연결한 상세페이지를 기획한다.", KNOWLEDGE_TOOLS + MARKET_TOOLS),
    StrategyMode.BLOG: ModeSpec(StrategyMode.BLOG, "입력 자료를 근거로 검색·전환 목적의 글을 만든다.", CONTENT_TOOLS + KNOWLEDGE_TOOLS + MARKET_TOOLS),
    StrategyMode.STRATEGY_CHAT: ModeSpec(StrategyMode.STRATEGY_CHAT, "사용자와 장기 콘텐츠 방향을 함께 판단한다.", PERFORMANCE_TOOLS + CONTENT_TOOLS + KNOWLEDGE_TOOLS + MARKET_TOOLS, structured_output=False, streaming=True),
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
