"""Channel-aware AI diagnosis and collaborative edit-plan revision."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, replace
from typing import Any

from strategy_brain.brain import StrategyBrain
from strategy_brain.config import BrainSettings
from strategy_brain.contracts import StrategyMode
from strategy_brain.providers import OpenAIResponsesProvider
from strategy_brain.retrieval import StrategyRetrieval, build_strategy_tool_registry
from strategy_repository import StrategyRepository
from edit_learning_service import build_editing_benchmarks


SEGMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "start_time": {"type": "number"},
        "end_time": {"type": "number"},
        "action": {
            "type": "string",
            "enum": [
                "keep", "trim", "cut", "move", "shorten", "use_as_hook",
                "use_as_short_clip", "add_broll", "add_caption_emphasis",
            ],
        },
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "expected_effect": {"type": "string"},
        "destination": {"type": "string"},
    },
    "required": [
        "id", "start_time", "end_time", "action", "reason", "confidence",
        "expected_effect", "destination",
    ],
}

ENHANCEMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string"},
        "start_time": {"type": "number"},
        "end_time": {"type": "number"},
        "type": {"type": "string", "enum": ["broll", "caption_emphasis"]},
        "instruction": {"type": "string"},
        "asset_requirements": {"type": "array", "items": {"type": "string"}},
        "overlay_text": {"type": "string"},
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "render_mode": {"type": "string", "enum": ["suggestion_only"]},
    },
    "required": [
        "id", "start_time", "end_time", "type", "instruction",
        "asset_requirements", "overlay_text", "priority", "confidence",
        "reason", "render_mode",
    ],
}

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "recommended_direction": {"type": "string"},
        "target_length_seconds": {"type": "number"},
        "create_short_highlight": {"type": "boolean"},
        "short_target_seconds": {"type": "number"},
        "editor_notes": {"type": "array", "items": {"type": "string"}},
        "segments": {"type": "array", "items": SEGMENT_SCHEMA},
        "enhancements": {"type": "array", "items": ENHANCEMENT_SCHEMA},
    },
    "required": [
        "recommended_direction", "target_length_seconds", "create_short_highlight",
        "short_target_seconds", "editor_notes", "segments", "enhancements",
    ],
}

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "overall_summary": {"type": "string"},
        "strong_points": {"type": "array", "items": {"type": "string"}},
        "weak_points": {"type": "array", "items": {"type": "string"}},
        "recommended_direction": {"type": "string"},
        "estimated_problems": {"type": "array", "items": {"type": "string"}},
        "suggested_final_length": {"type": "number"},
        "suggested_hook_range": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"start_time": {"type": "number"}, "end_time": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["start_time", "end_time", "reason"],
        },
        "channel_basis": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source": {"type": "string"},
                    "insight": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["source", "insight", "confidence"],
            },
        },
        "data_limitations": {"type": "array", "items": {"type": "string"}},
        "strategy_alignment": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "enum": ["aligned", "partial", "conflict", "unavailable"]},
                "matched_promises": {"type": "array", "items": {"type": "string"}},
                "conflicts": {"type": "array", "items": {"type": "string"}},
                "worksheet_priorities": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["status", "matched_promises", "conflicts", "worksheet_priorities"],
        },
        "plan": PLAN_SCHEMA,
    },
    "required": [
        "overall_summary", "strong_points", "weak_points", "recommended_direction",
        "estimated_problems", "suggested_final_length", "suggested_hook_range",
        "channel_basis", "data_limitations", "strategy_alignment", "plan",
    ],
}

REVISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "revision_summary": {"type": "string"},
        "plan": PLAN_SCHEMA,
    },
    "required": ["revision_summary", "plan"],
}


def _compact_json(value: Any, limit: int = 18_000) -> str:
    raw = json.dumps(value, ensure_ascii=False, default=str)
    return raw if len(raw) <= limit else raw[:limit] + "…(truncated)"


def _timecode(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class EditAnalysisService:
    def __init__(
        self,
        *,
        retrieval: StrategyRetrieval | None = None,
        strategies: StrategyRepository | None = None,
        brain: StrategyBrain | None = None,
    ) -> None:
        self.retrieval = retrieval or StrategyRetrieval()
        self.strategies = strategies or StrategyRepository()
        self._brain = brain

    async def collect_evidence(
        self, *, topic: str, purpose: str, strategy_id: int | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any] | None]:
        query = " ".join(value for value in (topic, purpose, "영상 편집 오프닝 훅 이탈 반복 설명") if value).strip()
        calls = {
            "similar_videos": (self.retrieval.compare_similar_videos, {"query": query, "limit": 8}),
            "retention": (self.retrieval.get_retention_patterns, {"video_id": None, "limit": 12}),
            "channel_snapshot": (self.retrieval.get_channel_strategy_snapshot, {"limit": 20}),
            "business_pt": (self.retrieval.search_business_pt_knowledge, {"query": query, "limit": 6}),
            "feedback": (self.retrieval.search_feedback_history, {"query": query, "limit": 6}),
            "worksheets": (self.retrieval.search_previous_worksheets, {"query": query, "limit": 5}),
            "memory": (self.retrieval.search_long_term_memory, {"query": query, "limit": 6}),
        }

        async def run_one(name: str, method: Any, args: dict[str, Any]) -> tuple[str, Any]:
            try:
                return name, await asyncio.to_thread(method, args)
            except Exception as exc:
                return name, {
                    "data": None,
                    "source": name,
                    "unavailable_reason": f"{type(exc).__name__}: retrieval unavailable",
                }

        strategy_task = (
            asyncio.create_task(
                asyncio.to_thread(self.strategies.get_execution_context, strategy_id)
            )
            if strategy_id is not None else None
        )
        results = await asyncio.gather(
            *(run_one(name, method, args) for name, (method, args) in calls.items())
        )
        evidence: dict[str, Any] = {}
        trace = []
        for name, envelope in results:
            value = asdict(envelope) if hasattr(envelope, "__dataclass_fields__") else envelope
            evidence[name] = value
            trace.append(
                {
                    "tool": name,
                    "source": value.get("source"),
                    "sample_size": value.get("sample_size"),
                    "freshness": value.get("freshness"),
                    "unavailable": bool(value.get("unavailable_reason")),
                }
            )
        strategy = None
        if strategy_id is not None:
            try:
                strategy = await strategy_task if strategy_task else None
            except Exception:
                strategy = None
            trace.append(
                {
                    "tool": "content_strategy",
                    "source": f"content_strategies:{strategy_id}",
                    "sample_size": 1 if strategy else 0,
                    "freshness": None,
                    "unavailable": strategy is None,
                }
            )
        evidence["editing_benchmarks"] = build_editing_benchmarks(evidence)
        trace.append(
            {
                "tool": "build_editing_benchmarks",
                "source": "youtube_analytics_retention+knowledge:business_pt",
                "sample_size": evidence["editing_benchmarks"].get("retention_sample_size", 0),
                "freshness": (evidence.get("retention") or {}).get("freshness"),
                "unavailable": not bool(evidence["editing_benchmarks"].get("decision_rules")),
            }
        )
        return evidence, trace, strategy

    @staticmethod
    def transcript_for_prompt(transcript: dict[str, Any]) -> str:
        lines = []
        for segment in transcript.get("segments") or []:
            lines.append(
                f"[{_timecode(segment.get('start') or 0)}-{_timecode(segment.get('end') or 0)}] {segment.get('text') or ''}"
            )
        if lines:
            return "\n".join(lines)[:160_000]
        return str(transcript.get("text") or "")[:160_000]

    def _brain_instance(self) -> StrategyBrain:
        if self._brain is None:
            settings = BrainSettings.from_env()
            self._brain = StrategyBrain(
                OpenAIResponsesProvider(settings), build_strategy_tool_registry()
            )
        return self._brain

    async def _structured(
        self,
        *,
        prompt: str,
        instructions: str,
        schema: dict[str, Any],
        schema_name: str,
        reasoning_effort: str = "high",
    ) -> dict[str, Any]:
        openai_error: Exception | None = None
        if os.getenv("OPENAI_API_KEY", "").strip() or self._brain is not None:
            try:
                brain = self._brain_instance()
                request = brain.build_request(
                    StrategyMode.EDIT_DIRECTOR,
                    prompt,
                    instructions,
                    output_schema=schema,
                    output_schema_name=schema_name,
                    metadata={"surface": "edit_director", "channel": "bujajubang"},
                )
                request = replace(request, tools=[], reasoning_effort=reasoning_effort)
                result = await brain.run(request)
                if not isinstance(result.parsed, dict):
                    raise RuntimeError("AI 편집 응답 형식이 올바르지 않습니다.")
                return result.parsed
            except Exception as exc:
                openai_error = exc
        if os.getenv("ANTHROPIC_API_KEY", "").strip():
            from analyzer import Analyzer, EFFORT_MID, _msg_text, _safe_json

            analyzer = Analyzer()
            message = await analyzer._create(
                effort=EFFORT_MID,
                max_tokens=7000,
                system=instructions + "\n반드시 요청된 JSON 구조만 출력하세요.",
                messages=[{"role": "user", "content": prompt}],
            )
            result = _safe_json(_msg_text(message), message)
            if isinstance(result, dict):
                return result
        raise RuntimeError("AI 편집 분석에 실패했습니다.") from openai_error

    @staticmethod
    def _ground_diagnosis(
        diagnosis: dict[str, Any], evidence: dict[str, Any], strategy: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Guarantee that displayed evidence is traceable to retrieved data."""

        diagnosis = dict(diagnosis or {})
        basis = list(diagnosis.get("channel_basis") or [])
        benchmarks = evidence.get("editing_benchmarks") or {}
        sources = " ".join(str(item.get("source") or "").lower() for item in basis)
        retention = benchmarks.get("retention_30s_median")
        if retention is not None and "retention" not in sources:
            basis.append(
                {
                    "source": "youtube_analytics_retention",
                    "insight": f"채널 retention 표본 {benchmarks.get('retention_sample_size', 0)}개의 30초 중앙값 {float(retention) * 100:.1f}%를 오프닝 판단 기준으로 사용",
                    "confidence": "high" if benchmarks.get("retention_sample_size", 0) >= 5 else "medium",
                }
            )
        principles = benchmarks.get("business_pt_principles") or []
        if principles and "business" not in sources and "knowledge" not in sources:
            basis.append(
                {
                    "source": "knowledge:business_pt",
                    "insight": f"{principles[0].get('title') or '비즈니스PT 원칙'}: {principles[0].get('principle') or ''}"[:900],
                    "confidence": "medium",
                }
            )
        diagnosis["channel_basis"] = basis[:12]
        limitations = list(diagnosis.get("data_limitations") or [])
        for item in benchmarks.get("limitations") or []:
            if item not in limitations:
                limitations.append(item)
        diagnosis["data_limitations"] = limitations[:12]
        if not isinstance(diagnosis.get("strategy_alignment"), dict):
            diagnosis["strategy_alignment"] = {
                "status": "unavailable" if not strategy else "partial",
                "matched_promises": [],
                "conflicts": [],
                "worksheet_priorities": [],
            }
        plan = dict(diagnosis.get("plan") or {})
        plan.setdefault("enhancements", [])
        diagnosis["plan"] = plan
        return diagnosis

    async def diagnose(
        self,
        *,
        transcript: dict[str, Any],
        media: dict[str, Any],
        silences: list[dict[str, Any]],
        scenes: list[float],
        settings: dict[str, Any],
        evidence: dict[str, Any],
        strategy: dict[str, Any] | None,
    ) -> dict[str, Any]:
        target = float(settings.get("target_length_seconds") or 0)
        instructions = """당신은 부자주방 전담 AI 편집 디렉터다. 지금은 편집을 실행하지 않고 사용자가 검토할 진단과 제안만 만든다.

- transcript의 실제 타임코드와 silence/scene 힌트를 근거로 제안한다.
- raw_footage는 말실수·반복·대기·정적을 적극 찾고, rough_cut은 이미 만든 리듬과 의도를 존중한다.
- 제목·썸네일·훅 전략이 있으면 본문이 그 약속을 회수하는지 확인한다.
- editing_benchmarks의 실제 retention 중앙값과 강/약 오프닝 표본을 편집 길이·첫 훅 판단에 우선 적용한다.
- 비즈니스PT 지식은 단순 인용하지 말고 해당 원칙이 바꾸는 컷·훅·B-roll·자막 결정을 명시한다.
- 연결 strategy의 제목·썸네일 약속, worksheet 촬영 우선순위, pipeline 목적과 충돌 여부를 strategy_alignment에 쓴다.
- 데이터가 없으면 일반론을 채널 사실처럼 말하지 말고 data_limitations에 적는다.
- cut/trim은 해당 구간 전체 제거, shorten은 해당 구간의 뒷부분 축약, use_as_hook은 해당 구간을 오프닝으로 이동한다.
- 실제로 잘라야 할 구간만 구체적으로 쓰고 영상 전체를 촘촘히 재서술하지 않는다.
- confidence는 0~1이다. 지나치게 공격적인 컷은 낮은 confidence로 표시한다.
- B-roll/자막은 enhancements에 정확한 타임코드·필요 소스·화면 문구·우선순위를 쓰고 render_mode는 suggestion_only로 둔다.
- 사용자가 승인하기 전에는 어떤 편집도 실행되지 않는다."""
        prompt = f"""[입력 설정]
{_compact_json(settings)}

[미디어]
{_compact_json(media)}
목표 길이: {target if target else 'AI 추천'}초

[정적 구간]
{_compact_json(silences, 12000)}

[장면 전환 시각]
{_compact_json(scenes, 8000)}

[연결된 콘텐츠 전략]
{_compact_json(strategy or {'unavailable': True}, 18000)}

[부자주방 채널·지식 근거]
{_compact_json(evidence, 70000)}

[타임코드 transcript]
{self.transcript_for_prompt(transcript)}

intro 지연, 반복, 늘어짐, 정적, B-roll/자막, 제목 약속 회수, 초반 이탈, 핵심 메시지, 후반 길이, 쇼츠 후보, CTA를 모두 점검하고 구조화된 진단과 최초 edit plan을 작성하라."""
        # Short/rough-cut inputs have bounded evidence and do not benefit from a
        # long deliberation pass.  Long raw footage keeps high reasoning.
        effort = (
            "medium"
            if settings.get("video_type") == "rough_cut"
            or float(media.get("duration") or 0) <= 180
            else "high"
        )
        diagnosis = await self._structured(
            prompt=prompt,
            instructions=instructions,
            schema=DIAGNOSIS_SCHEMA,
            schema_name="edit_diagnosis",
            reasoning_effort=effort,
        )
        return self._ground_diagnosis(diagnosis, evidence, strategy)

    async def revise(
        self,
        *,
        current_plan: dict[str, Any],
        user_request: str,
        transcript: dict[str, Any],
        media: dict[str, Any],
        settings: dict[str, Any],
        evidence: dict[str, Any],
        strategy: dict[str, Any] | None,
    ) -> dict[str, Any]:
        instructions = """당신은 사용자와 합의안을 만드는 부자주방 AI 편집 디렉터다.
현재 plan을 기반으로 사용자의 수정 요청에 해당하는 부분만 바꾼다. 사용자가 살리라고 한 구간은 cut하지 않는다. 현장 분위기 보존, 설명 축약, B-roll, 쇼츠 요청을 정확히 반영한다. 채널 근거와 콘텐츠 전략에 충돌하면 revision_summary에 짧게 알리되 사용자 의도를 최종 우선한다. 승인은 서버의 별도 단계이므로 스스로 approved라고 선언하지 않는다. JSON만 출력한다."""
        prompt = f"""[사용자 수정 요청]
{user_request.strip()[:4000]}

[현재 edit plan]
{_compact_json(current_plan, 50000)}

[미디어/설정]
{_compact_json({'media': media, 'settings': settings}, 12000)}

[연결 전략]
{_compact_json(strategy or {'unavailable': True}, 12000)}

[근거]
{_compact_json(evidence, 35000)}

[transcript]
{self.transcript_for_prompt(transcript)}

수정된 전체 plan과 무엇을 바꿨는지 한 문장 revision_summary를 반환하라."""
        revised = await self._structured(
            prompt=prompt,
            instructions=instructions,
            schema=REVISION_SCHEMA,
            schema_name="edit_plan_revision",
            reasoning_effort="medium",
        )
        plan = dict(revised.get("plan") or {})
        plan.setdefault("enhancements", current_plan.get("enhancements") or [])
        revised["plan"] = plan
        return revised
