from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol, Union


class StrategyMode(str, Enum):
    RESEARCH = "research"
    PLANNING = "planning"
    INTRO = "intro"
    SCRIPT = "script"
    MIDFORM_PLANNING = "midform_planning"
    SHORTFORM_PLANNING = "shortform_planning"
    TOPIC_DISCOVERY = "topic_discovery"
    YOUTUBE_SEARCH_ANALYSIS = "youtube_search_analysis"
    CHANNEL_ANALYSIS = "channel_analysis"
    UPLOAD_DECISION = "upload_decision"
    EDIT_FEEDBACK = "edit_feedback"
    VIDEO_FEEDBACK = "video_feedback"
    WORKSHEET = "worksheet"
    JJACHI = "jjachi"
    SNS_CONVERSION = "sns_conversion"
    DETAIL_PAGE = "detail_page"
    BLOG = "blog"
    STRATEGY_CHAT = "strategy_chat"
    POSTMORTEM = "postmortem"


@dataclass(frozen=True)
class EvidenceEnvelope:
    data: Any
    source: str
    collected_at: str | None = None
    period: dict[str, str | None] | None = None
    freshness: str | None = None
    sample_size: int | None = None
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class BrainRequest:
    mode: StrategyMode
    instructions: str
    input: str | list[dict[str, Any]]
    tools: list[dict[str, Any]] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    output_schema_name: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    reasoning_effort: str | None = None


@dataclass
class BrainResult:
    text: str
    parsed: Any = None
    response_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: int = 0
    raw_response: Any = None


ToolExecutor = Callable[
    [str, dict[str, Any]], Awaitable[Union[EvidenceEnvelope, Any]]
]


class BrainProvider(Protocol):
    async def generate(
        self, request: BrainRequest, tool_executor: ToolExecutor | None = None
    ) -> BrainResult: ...

    def stream(
        self, request: BrainRequest, tool_executor: ToolExecutor | None = None
    ) -> AsyncIterator[str]: ...
