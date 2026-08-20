"""Business-first review and safe HD preview for an already edited mid-form video."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from edit_analysis_service import EditAnalysisService
from edit_render_service import EditRenderService
from edit_visual_service import TimecodedFrameExtractor, VISUAL_FALLBACK_MESSAGE
from media_ingest import MediaIngestService


def _slug(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z가-힣._-]+", "-", value).strip("-.")
    return normalized[:80] or "business-review"


def _preview_plan(
    review: dict[str, Any], duration: float, *, apply_review_candidates: bool = False,
) -> dict[str, Any]:
    """Build an exact candidate timeline without arbitrary target-length trimming."""

    timeline: list[dict[str, Any]] = []
    cursor = 0.0
    candidates = sorted(
        (review.get("revised_edl") or []),
        key=lambda item: (float(item.get("start_time") or 0), float(item.get("end_time") or 0)),
    )
    for index, item in enumerate(candidates):
        start = max(0.0, min(duration, float(item.get("start_time") or 0)))
        end = max(start, min(duration, float(item.get("end_time") or start)))
        start = max(start, cursor)
        if end - start < 0.08:
            continue
        if start > cursor + 0.08:
            timeline.append({
                "source_start": round(cursor, 3), "source_end": round(start, 3),
                "action": "keep_unreviewed_gap", "reason": "EDL이 명시하지 않은 원본 구간 보존",
            })
        candidate = str(item.get("action") or "keep")
        requires_review = bool(item.get("requires_user_review"))
        apply_candidate = candidate in {"cut_candidate", "shorten_candidate"} and (
            apply_review_candidates or not requires_review
        )
        keep_end = end
        if apply_candidate:
            keep_seconds = max(0.0, min(end - start, float(item.get("suggested_keep_seconds") or 0)))
            keep_end = start + keep_seconds
        if keep_end - start >= 0.08:
            timeline.append({
                "source_start": round(start, 3), "source_end": round(keep_end, 3),
                "action": (
                    "candidate_excerpt" if apply_candidate
                    else "visual_highlight" if candidate == "highlight"
                    else "protected_user_review" if requires_review else "keep"
                ),
                "reason": str(item.get("reason") or "")[:1000],
                "candidate_action": candidate,
                "requires_user_review": requires_review,
                "source_edl_index": index,
            })
        cursor = max(cursor, end)
    if cursor < duration - 0.08:
        timeline.append({
            "source_start": round(cursor, 3), "source_end": round(duration, 3),
            "action": "keep_unreviewed_gap", "reason": "EDL 이후 원본 결론 구간 보존",
        })
    return {
        "recommended_direction": review.get("overall_diagnosis") or "사업 신뢰 중심 미드폼",
        "target_length_seconds": 0,
        "render_timeline": timeline,
        "estimated_output_duration": round(sum(
            float(item["source_end"]) - float(item["source_start"]) for item in timeline
        ), 3),
        "apply_review_candidates": apply_review_candidates,
        "contains_unapproved_review_simulation": apply_review_candidates,
        "human_review_priorities": list(review.get("human_review_priorities") or []),
    }


async def run(args: argparse.Namespace) -> int:
    source = args.video.resolve()
    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    ingest = MediaIngestService()
    media = await asyncio.to_thread(ingest.probe, source)
    started = time.perf_counter()
    if args.reuse_artifact:
        artifact_path = args.reuse_artifact.resolve()
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact.setdefault("source", {})["path"] = str(source)
        artifact["source"]["analysis_reused_from_identical_bytes"] = True
        if args.no_preview:
            temporary_artifact = artifact_path.with_name(f".{artifact_path.name}.part")
            temporary_artifact.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary_artifact.replace(artifact_path)
            print(json.dumps({
                "source": str(source), "artifact": str(artifact_path),
                "preview": None, "analysis_reused_from_identical_bytes": True,
            }, ensure_ascii=False, indent=2))
            return 0
        review = artifact["business_review"]
        plan = _preview_plan(
            review, float(media["duration"]),
            apply_review_candidates=bool(args.apply_review_candidates),
        )
        preview_kind = "proposal" if args.apply_review_candidates else "safe"
        preview_path = output_root / f"{_slug(source.stem)}-business-preview-{preview_kind}-{args.profile}.mp4"
        preview_meta = await asyncio.to_thread(
            EditRenderService().render_timeline,
            source=source, output=preview_path,
            timeline=plan["render_timeline"], duration=float(media["duration"]),
            has_audio=bool(media.get("has_audio")), profile=args.profile,
        )
        preview_meta["path"] = str(preview_path)
        artifact.setdefault("preview_plans", {})[preview_kind] = plan
        artifact.setdefault("previews", {})[preview_kind] = preview_meta
        temporary_artifact = artifact_path.with_name(f".{artifact_path.name}.part")
        temporary_artifact.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_artifact.replace(artifact_path)
        print(json.dumps({
            "preview_kind": preview_kind,
            "preview_duration_seconds": plan["estimated_output_duration"],
            "preview": str(preview_path),
            "artifact": str(artifact_path),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }, ensure_ascii=False, indent=2))
        return 0
    with tempfile.TemporaryDirectory(prefix="business-edit-review-") as raw:
        work = Path(raw)
        analysis_source = source
        proxy_used = False
        if int(media.get("width") or 0) > 1920 or int(media.get("height") or 0) > 1080:
            analysis_source = work / "analysis-proxy-1080p.mp4"
            await asyncio.to_thread(
                ingest.create_analysis_proxy, source, analysis_source,
                float(media.get("duration") or 0),
            )
            proxy_used = True

        transcript, silences, scenes = await ingest.inspect_and_transcribe(
            analysis_source, media, work_dir=work,
        )
        manifest = await asyncio.to_thread(
            TimecodedFrameExtractor().extract,
            source=analysis_source, output_dir=work / "frames",
            duration=float(media.get("duration") or 0), scene_times=scenes,
            interval_seconds=float(args.interval),
        )
        analysis = EditAnalysisService()
        try:
            visual = await analysis.analyze_visual_frames(
                manifest=manifest, transcript=transcript, media=media,
            )
        except Exception as exc:
            visual = {
                "schema_version": 1,
                "status": "failed",
                "fallback_used": True,
                "message": VISUAL_FALLBACK_MESSAGE,
                "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                "frame_count": manifest.get("frame_count", 0),
                "frame_results": [],
                "segments": [],
            }

        evidence, trace, _strategy = await analysis.collect_evidence(
            topic="현장형 상업주방 설계 동선 시공 장비 Before After",
            purpose="브랜드신뢰형 외식창업자 상담유도형",
            strategy_id=None,
        )
        review = await analysis.review_rough_cut_business(
            transcript=transcript,
            media=media,
            visual_analysis=visual,
            evidence=evidence,
            target_min_seconds=float(args.target_min),
            target_max_seconds=float(args.target_max),
        )
        plan = _preview_plan(
            review, float(media["duration"]),
            apply_review_candidates=bool(args.apply_review_candidates),
        )
        preview_path: Path | None = None
        preview_meta: dict[str, Any] | None = None
        expected = float(plan.get("estimated_output_duration") or 0)
        if not args.no_preview and expected >= 90:
            preview_kind = "proposal" if args.apply_review_candidates else "safe"
            preview_path = output_root / f"{_slug(source.stem)}-business-preview-{preview_kind}-{args.profile}.mp4"
            preview_meta = await asyncio.to_thread(
                EditRenderService().render_timeline,
                source=analysis_source,
                output=preview_path,
                timeline=plan.get("render_timeline") or [],
                duration=float(media["duration"]),
                has_audio=bool(media.get("has_audio")),
                profile=args.profile,
            )
            preview_meta["path"] = str(preview_path)

        artifact = {
            "schema_version": 1,
            "source": {
                "path": str(source), "read_only": True, "media": media,
                "analysis_proxy_used": proxy_used,
            },
            "settings": {
                "video_type": "rough_cut", "target_min_seconds": args.target_min,
                "target_max_seconds": args.target_max, "frame_interval_seconds": args.interval,
                "preview_profile": args.profile,
                "apply_review_candidates": bool(args.apply_review_candidates),
            },
            "signals": {"silences": silences, "scene_changes": scenes},
            "transcript": transcript,
            "visual_analysis": visual,
            "retrieval_trace": trace,
            "business_review": review,
            "preview_plan": plan,
            "preview": preview_meta,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        artifact_path = output_root / f"{_slug(source.stem)}-business-review.json"
        temporary_artifact = artifact_path.with_name(f".{artifact_path.name}.part")
        temporary_artifact.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_artifact.replace(artifact_path)

        print(json.dumps({
            "source": str(source),
            "media": media,
            "visual_status": visual.get("status"),
            "analyzed_frames": visual.get("analyzed_frame_count", 0),
            "business_completeness_score": review.get("business_completeness_score"),
            "improvement_potential_score": review.get("improvement_potential_score"),
            "estimated_final_length_seconds": review.get("estimated_final_length_seconds"),
            "safe_preview_duration_seconds": expected,
            "artifact": str(artifact_path),
            "preview": str(preview_path) if preview_path else None,
            "elapsed_seconds": artifact["elapsed_seconds"],
        }, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/business-edit-review"))
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--target-min", type=float, default=180.0)
    parser.add_argument("--target-max", type=float, default=240.0)
    parser.add_argument("--profile", choices=["preview_720p", "preview_1080p"], default="preview_1080p")
    parser.add_argument(
        "--apply-review-candidates", action="store_true",
        help="simulate user-review-gated candidates in a reversible preview; never a final render",
    )
    parser.add_argument(
        "--reuse-artifact", type=Path,
        help="render a safe/proposal preview from an existing review without rerunning AI analysis",
    )
    parser.add_argument("--no-preview", action="store_true")
    args = parser.parse_args()
    if not args.video.is_file():
        parser.error("video file does not exist")
    if args.target_min < 90 or args.target_max < args.target_min:
        parser.error("target range is invalid")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
