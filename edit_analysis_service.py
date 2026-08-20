"""Channel-aware AI diagnosis and collaborative edit-plan revision."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from strategy_brain.brain import StrategyBrain
from strategy_brain.config import BrainSettings
from strategy_brain.contracts import StrategyMode
from strategy_brain.providers import OpenAIResponsesProvider
from strategy_brain.retrieval import StrategyRetrieval, build_strategy_tool_registry
from strategy_repository import StrategyRepository
from edit_learning_service import build_editing_benchmarks
from edit_visual_service import (
    VISUAL_FALLBACK_MESSAGE,
    build_audio_visual_segments,
    fuse_plan_with_visual,
    public_frame_manifest,
    transcript_context,
)


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
                "use_as_short_clip", "add_broll", "add_caption_emphasis", "highlight",
            ],
        },
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "expected_effect": {"type": "string"},
        "destination": {"type": "string"},
        "audio_score": {"type": "number"},
        "visual_score": {"type": ["number", "null"]},
        "context_score": {"type": "number"},
        "visual_evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "id", "start_time", "end_time", "action", "reason", "confidence",
        "expected_effect", "destination",
        "audio_score", "visual_score", "context_score", "visual_evidence",
    ],
}

VISUAL_FRAME_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "frame_id": {"type": "string"},
        "timecode_seconds": {"type": "number"},
        "description": {"type": "string"},
        "shaking_score": {"type": "number"},
        "focus_score": {"type": "number"},
        "brightness_score": {"type": "number"},
        "occlusion_score": {"type": "number"},
        "site_value_tags": {"type": "array", "items": {"type": "string"}},
        "speech_alignment_score": {"type": "number"},
        "thumbnail_candidate": {"type": "boolean"},
        "visual_score": {"type": "number"},
        "edit_decision": {"type": "string", "enum": ["keep", "cut", "shorten", "highlight"]},
        "reason": {"type": "string"},
    },
    "required": [
        "frame_id", "timecode_seconds", "description", "shaking_score", "focus_score",
        "brightness_score", "occlusion_score", "site_value_tags", "speech_alignment_score",
        "thumbnail_candidate", "visual_score", "edit_decision", "reason",
    ],
}

VISUAL_BATCH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"frames": {"type": "array", "items": VISUAL_FRAME_SCHEMA}},
    "required": ["frames"],
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

MULTISOURCE_CLASSIFICATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "segments": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "segment_id": {"type": "string"},
                    "topic": {"type": "string"},
                    "role": {"type": "string", "enum": [
                        "hook", "problem", "product_intro", "principle", "usage",
                        "proof", "benefit", "drawback", "purchase_caution",
                        "maintenance", "after_service", "cost", "recommended_for",
                        "not_recommended_for", "conclusion",
                    ]},
                    "importance": {"type": "number"}, "quality": {"type": "number"},
                    "confidence": {"type": "number"}, "reason": {"type": "string"},
                },
                "required": ["segment_id", "topic", "role", "importance", "quality", "confidence", "reason"],
            },
        },
    },
    "required": ["segments"],
}

MULTISOURCE_STORY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "recommended_direction": {"type": "string"},
        "ordered_segments": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "segment_id": {"type": "string"}, "role": {"type": "string"},
                    "reason": {"type": "string"}, "keep": {"type": "boolean"},
                },
                "required": ["segment_id", "role", "reason", "keep"],
            },
        },
        "editor_notes": {"type": "array", "items": {"type": "string"}},
        "channel_evidence_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["recommended_direction", "ordered_segments", "editor_notes", "channel_evidence_confidence"],
}


BUSINESS_REVIEW_SEGMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "start_time": {"type": "number"},
        "end_time": {"type": "number"},
        "current_role": {
            "type": "string",
            "enum": ["hook", "context", "explanation", "broll", "proof", "transition", "ending"],
        },
        "audio_summary": {"type": "string"},
        "visual_summary": {"type": "string"},
        "business_context_value": {"type": "number"},
        "viewer_value": {"type": "number"},
        "pacing_score": {"type": "number"},
        "trust_score": {"type": "number"},
        "visual_score": {"type": "number"},
        "edit_decision": {
            "type": "string",
            "enum": [
                "keep", "cut_candidate", "shorten_candidate", "highlight", "broll",
                "thumbnail_candidate", "needs_user_review",
            ],
        },
        "secondary_labels": {"type": "array", "items": {"type": "string"}},
        "suggested_action": {"type": "string"},
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
        "requires_user_review": {"type": "boolean"},
        "visual_evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "start_time", "end_time", "current_role", "audio_summary", "visual_summary",
        "business_context_value", "viewer_value", "pacing_score", "trust_score",
        "visual_score", "edit_decision", "secondary_labels", "suggested_action", "reason",
        "confidence", "requires_user_review", "visual_evidence",
    ],
}

BUSINESS_REVIEW_EDL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "start_time": {"type": "number"},
        "end_time": {"type": "number"},
        "action": {
            "type": "string",
            "enum": ["keep", "cut_candidate", "shorten_candidate", "highlight"],
        },
        "suggested_keep_seconds": {"type": "number"},
        "reason": {"type": "string"},
        "requires_user_review": {"type": "boolean"},
        "source_segment_indexes": {"type": "array", "items": {"type": "number"}},
    },
    "required": [
        "start_time", "end_time", "action", "suggested_keep_seconds", "reason",
        "requires_user_review", "source_segment_indexes",
    ],
}

BUSINESS_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "business_completeness_score": {"type": "number"},
        "improvement_potential_score": {"type": "number"},
        "overall_diagnosis": {"type": "string"},
        "good_points": {"type": "array", "items": {"type": "string"}},
        "weak_flow_points": {"type": "array", "items": {"type": "string"}},
        "boring_for_founders": {"type": "array", "items": {"type": "string"}},
        "messages_to_emphasize": {"type": "array", "items": {"type": "string"}},
        "must_keep": {"type": "array", "items": {"type": "string"}},
        "safe_to_reduce": {"type": "array", "items": {"type": "string"}},
        "dangerous_to_delete": {"type": "array", "items": {"type": "string"}},
        "applied_business_principles": {"type": "array", "items": {"type": "string"}},
        "data_limitations": {"type": "array", "items": {"type": "string"}},
        "segments": {"type": "array", "items": BUSINESS_REVIEW_SEGMENT_SCHEMA},
        "revised_edl": {"type": "array", "items": BUSINESS_REVIEW_EDL_SCHEMA},
        "estimated_final_length_seconds": {"type": "number"},
        "human_review_priorities": {"type": "array", "items": {"type": "string"}},
        "next_recommendation": {"type": "string"},
    },
    "required": [
        "business_completeness_score", "improvement_potential_score", "overall_diagnosis",
        "good_points", "weak_flow_points", "boring_for_founders", "messages_to_emphasize",
        "must_keep", "safe_to_reduce", "dangerous_to_delete", "applied_business_principles",
        "data_limitations", "segments", "revised_edl", "estimated_final_length_seconds",
        "human_review_priorities", "next_recommendation",
    ],
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
        if hasattr(self.retrieval, "search_knowledge"):
            calls["low_data_brand_knowledge"] = (
                self.retrieval.search_knowledge,
                {"query": query + " low data 브랜드 전략 문제 해결 증거 CTA", "limit": 6},
            )
        if hasattr(self.retrieval, "get_ctr_performance"):
            calls["ctr_performance"] = (self.retrieval.get_ctr_performance, {"limit": 30})

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

    async def classify_multisource_chunk(
        self, *, source: dict[str, Any], segments: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Classify one cached transcript chunk; callers persist it before continuing."""

        compact = [{
            "segment_id": row.get("segment_id"), "start_time": row.get("start_time"),
            "end_time": row.get("end_time"), "speaker": row.get("speaker"),
            "transcript": row.get("transcript"),
        } for row in segments]
        return await self._structured(
            prompt="[source]\n" + _compact_json({
                "filename": source.get("filename"), "speaker": source.get("speaker"),
            }, 2000) + "\n[segments]\n" + _compact_json(compact, 30000),
            instructions="""부자주방 멀티소스 러프컷의 발언 분류기다.
각 segment_id를 그대로 유지하고 문제·원리·사용법·실사용 증거·주의사항·관리/A/S·결론 역할을 분류한다.
실제 사용자 경험과 구체적 비용/문제 증거는 importance를 높인다. 반복 여부나 최종 순서는 여기서 결정하지 않는다.
제공되지 않은 발언이나 장면을 만들지 말고 JSON만 반환한다.""",
            schema=MULTISOURCE_CLASSIFICATION_SCHEMA,
            schema_name="multisource_chunk_classification",
            reasoning_effort="medium", allow_anthropic=True,
        )

    async def plan_multisource_story(
        self, *, candidates: list[dict[str, Any]], evidence: dict[str, Any],
        strategy: dict[str, Any] | None, settings: dict[str, Any],
        user_request: str = "", current_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One bounded reasoning call over cached candidates, never raw media."""

        compact_candidates = [{
            key: row.get(key) for key in (
                "segment_id", "source_id", "filename", "speaker", "start_time",
                "end_time", "transcript", "role", "importance", "quality",
                "duplicate_group", "selection_reason",
            )
        } for row in candidates]
        for row in compact_candidates:
            row["transcript"] = str(row.get("transcript") or "")[:700]
        return await self._structured(
            prompt=f"""[설정]\n{_compact_json(settings, 5000)}
[사용자 수정 요청]\n{user_request[:3000] or '최초 구성'}
[현재 구성안]\n{_compact_json(current_plan or {'none': True}, 20000)}
[중복 제거 후 후보]\n{_compact_json(compact_candidates, 50000)}
[채널/retention/Business PT/low data 근거]\n{_compact_json(evidence, 35000)}
[연결 전략/워크시트]\n{_compact_json(strategy or {'unavailable': True}, 12000)}""",
            instructions="""당신은 부자주방 멀티소스 러프컷 스토리 프로듀서다.
선택 후보의 segment_id만 사용한다. 강한 실사용 증거나 고객 문제를 훅으로 시작하고, 문제→원리/사용법→실제 증거→구매 주의→관리/A/S→추천/결론처럼 문맥을 만든다.
같은 의미 발언을 되살리지 않는다. 실제 사용자 발언이 일반 설명보다 강한 증거면 우선하고, 필요할 때만 설명 발언을 보완한다.
채널 데이터가 부족하면 channel_evidence_confidence=low로 표시하고 Business PT와 영상 자체 문맥을 쓴다.
사용자 수정 요청은 관련 순서/길이만 바꾸며 전체 소스를 다시 분석했다고 주장하지 않는다. JSON만 반환한다.""",
            schema=MULTISOURCE_STORY_SCHEMA,
            schema_name="multisource_story_plan",
            reasoning_effort="high" if not user_request else "medium",
            allow_anthropic=True,
        )

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
        prompt: str | list[dict[str, Any]],
        instructions: str,
        schema: dict[str, Any],
        schema_name: str,
        reasoning_effort: str = "high",
        allow_anthropic: bool = True,
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
        if allow_anthropic and isinstance(prompt, str) and os.getenv("ANTHROPIC_API_KEY", "").strip():
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

    async def analyze_visual_frames(
        self, *, manifest: dict[str, Any], transcript: dict[str, Any], media: dict[str, Any],
    ) -> dict[str, Any]:
        frames = list(manifest.get("frames") or [])
        if not frames:
            raise RuntimeError("추출된 visual frame이 없습니다.")
        batch_size = max(4, min(16, int(os.getenv("EDIT_VISUAL_BATCH_SIZE", "12"))))
        semaphore = asyncio.Semaphore(max(1, min(3, int(os.getenv("EDIT_VISUAL_CONCURRENCY", "2")))))

        async def analyze_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
            frame_summary = [
                {
                    "frame_id": frame["frame_id"], "timecode_seconds": frame["timecode_seconds"],
                    "timecode": frame["timecode"],
                    "speech_context": transcript_context(transcript, float(frame["timecode_seconds"])),
                }
                for frame in batch
            ]
            content: list[dict[str, Any]] = [{
                "type": "input_text",
                "text": """부자주방 현장 영상의 타임코드 프레임을 평가하라.
각 프레임마다 흔들림·초점·밝기·가려짐, 현장 정보 가치(작업 장면/주방기구/바닥공사/철거/배수/동선/Before-After), 인접 대사와 화면 일치도, 썸네일 후보 여부를 판단한다.
shaking_score와 occlusion_score는 문제가 심할수록 1, focus_score와 brightness_score는 좋을수록 1이다.
visual_score는 화면 품질과 부자주방 현장 가치를 합친 0~1 점수다. 의미 없는 이동·바닥·천장·흔들림은 cut/shorten, 강한 공사·문제·Before-After는 keep/highlight로 판단한다.
이미지에 보이지 않는 장면이나 기구를 추측하지 마라.

[프레임 타임코드/인접 대사]\n""" + _compact_json(frame_summary, 10000),
            }]
            for frame in batch:
                image_bytes = await asyncio.to_thread(Path(str(frame["path"])).read_bytes)
                encoded = base64.b64encode(image_bytes).decode("ascii")
                content.append({"type": "input_text", "text": f"FRAME {frame['frame_id']} @ {frame['timecode']}"})
                content.append({
                    "type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}", "detail": "low",
                })
            async with semaphore:
                parsed = await self._structured(
                    prompt=[{"role": "user", "content": content}],
                    instructions="당신은 부자주방 전담 영상 편집자의 visual frame 분석기다. 반드시 제공된 프레임만 보고 엄격한 JSON으로 평가한다.",
                    schema=VISUAL_BATCH_SCHEMA, schema_name="edit_visual_frames",
                    reasoning_effort="medium", allow_anthropic=False,
                )
            source_by_id = {str(frame["frame_id"]): frame for frame in batch}
            output = []
            for item in parsed.get("frames") or []:
                source = source_by_id.get(str(item.get("frame_id") or ""))
                if source is None:
                    continue
                grounded = dict(item)
                grounded["timecode_seconds"] = source["timecode_seconds"]
                grounded["timecode"] = source["timecode"]
                output.append(grounded)
            return output

        groups = [frames[index:index + batch_size] for index in range(0, len(frames), batch_size)]
        results = [item for group in await asyncio.gather(*(analyze_batch(batch) for batch in groups)) for item in group]
        if not results:
            raise RuntimeError("visual frame AI 응답이 비어 있습니다.")
        interval = float(manifest.get("effective_interval_seconds") or 2.0)
        segments = build_audio_visual_segments(
            results, transcript, duration=float(media.get("duration") or 0), interval_seconds=interval,
        )
        return {
            **public_frame_manifest(manifest), "status": "succeeded", "fallback_used": False,
            "analyzed_frame_count": len(results), "frame_results": results, "segments": segments,
            "decision_basis": "audio_transcript+visual_frames",
        }

    @staticmethod
    def _ground_diagnosis(
        diagnosis: dict[str, Any], evidence: dict[str, Any], strategy: dict[str, Any] | None,
        visual_analysis: dict[str, Any] | None = None,
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
        if (visual_analysis or {}).get("status") != "succeeded" and VISUAL_FALLBACK_MESSAGE not in limitations:
            limitations.append(VISUAL_FALLBACK_MESSAGE)
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
        visual_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = float(settings.get("target_length_seconds") or 0)
        instructions = """당신은 부자주방 전담 AI 편집 디렉터다. 지금은 편집을 실행하지 않고 사용자가 검토할 진단과 제안만 만든다.

- transcript의 실제 타임코드와 silence/scene 힌트뿐 아니라 visual_analysis의 실제 프레임 판단을 함께 근거로 제안한다.
- 화면이 선명하고 현장 가치와 설명 일치도가 높은 공사·기구·배수·동선·Before/After 구간은 keep/highlight를 우선한다.
- 흔들림·이동·초점 불량·가려짐·의미 없는 바닥/천장 화면은 cut/shorten을 우선한다.
- plan.segments마다 audio_score, visual_score, context_score와 사용한 frame_id를 visual_evidence에 반드시 쓴다.
- raw_footage는 말실수·반복·대기·정적을 적극 찾고, rough_cut은 이미 만든 리듬과 의도를 존중한다.
- 제목·썸네일·훅 전략이 있으면 본문이 그 약속을 회수하는지 확인한다.
- editing_benchmarks의 실제 retention 중앙값과 강/약 오프닝 표본을 편집 길이·첫 훅 판단에 우선 적용한다.
- Reporting Reach CTR 근거가 있으면 클릭을 만든 제목·썸네일 약속을 첫 5~30초가 실제로 회수하는지 교차 판단한다.
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

[visual frame 분석]
{_compact_json(visual_analysis or {'status': 'failed', 'message': VISUAL_FALLBACK_MESSAGE}, 60000)}

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
        grounded = self._ground_diagnosis(diagnosis, evidence, strategy, visual_analysis)
        if (visual_analysis or {}).get("status") == "succeeded":
            grounded["plan"] = fuse_plan_with_visual(grounded.get("plan") or {}, visual_analysis or {})
        return grounded

    async def review_rough_cut_business(
        self,
        *,
        transcript: dict[str, Any],
        media: dict[str, Any],
        visual_analysis: dict[str, Any],
        evidence: dict[str, Any],
        target_min_seconds: float = 180,
        target_max_seconds: float = 240,
    ) -> dict[str, Any]:
        """Conservative business review for an already human-edited mid-form cut."""

        instructions = """당신은 부자주방의 비즈니스 관점 영상 편집 디렉터다.
이 입력은 원본 러프 영상이 아니라 이미 사람이 편집한 약 5분짜리 영상이다. 숏폼으로 만들지 말고 3~4분의 신뢰·문의 전환형 미드폼으로 다듬는다.

- 시청자는 외식창업자, 예비창업자, 식당 사장, 프랜차이즈 담당자다.
- 부자주방은 기구 판매만 하는 곳이 아니라 설계·동선·납품·시공·A/S까지 책임지는 현장형 주방 솔루션 브랜드다.
- 비즈니스PT/low data/브랜드 지식은 현재 영상에 관련된 원칙만 적용하고 applied_business_principles에 근거와 적용 결과를 쓴다.
- visual_analysis에 실제로 보이는 것만 말한다. 없는 장면을 추측하지 않는다.
- 무음은 삭제 이유가 아니다. 공사, 동선, 장비, 바닥, 배수, 전기, 가스, 후드, 덕트, Before/After 등 화면 정보가 강하면 broll/highlight/keep한다.
- 같은 말·같은 화면 반복, 정보 없는 이동, 화면과 설명 불일치, 과한 여백, 내부자용 설명만 cut_candidate/shorten_candidate로 제안한다.
- cut이라고 확정하지 않는다. 삭제 제안은 반드시 cut_candidate다.
- 전문가 신뢰나 현장 증거가 조금이라도 걸린 삭제 후보는 requires_user_review=true 또는 needs_user_review로 둔다.
- 중요한 현장 장면과 visual score가 높은 장면은 함부로 줄이지 않는다.
- 전체 영상을 시간순으로 빠짐없이 segments에 평가하되 인접한 동일 역할 구간은 합쳐 12~30초 단위로 정리한다.
- 점수는 모두 0~1, confidence도 0~1이다.
- revised_edl은 원본 타임코드 기준의 보수적 제안이다. 사용자 검토가 필요한 항목은 preview 자동 삭제 대상이 아니다.
- 예상 길이는 180~240초를 목표로 하되 근거 있는 컷만 제안한다. 숫자를 맞추려고 마지막 부분을 임의로 자르지 않는다."""
        prompt = f"""[미디어]
{_compact_json(media, 8000)}
목표 길이: {target_min_seconds:.0f}~{target_max_seconds:.0f}초

[부자주방 채널·retention·비즈니스PT·브랜드 근거]
{_compact_json(evidence, 80000)}

[타임코드 visual frame 분석]
{_compact_json(visual_analysis, 100000)}

[타임코드 transcript]
{self.transcript_for_prompt(transcript)}

현재 편집본의 사업적 설득력부터 진단한 뒤, 각 구간 평가와 보수적인 revised EDL을 작성하라."""
        result = await self._structured(
            prompt=prompt,
            instructions=instructions,
            schema=BUSINESS_REVIEW_SCHEMA,
            schema_name="edit_business_rough_cut_review",
            reasoning_effort="high",
        )
        result["review_basis"] = (
            "audio_transcript+visual_frames+channel_business_knowledge"
            if visual_analysis.get("status") == "succeeded"
            else "audio_transcript+channel_business_knowledge"
        )
        limitations = list(result.get("data_limitations") or [])
        if visual_analysis.get("status") != "succeeded" and VISUAL_FALLBACK_MESSAGE not in limitations:
            limitations.append(VISUAL_FALLBACK_MESSAGE)
        result["data_limitations"] = limitations
        return result

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
        visual_analysis: dict[str, Any] | None = None,
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

[승인 전 visual frame 분석]
{_compact_json(visual_analysis or {'status': 'unavailable'}, 30000)}

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
