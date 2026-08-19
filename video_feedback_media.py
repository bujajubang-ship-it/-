"""Low-cost media signals for feedback only; never renders or edits video."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter, ImageStat


BUSINESS_VISUAL_KEYWORDS = (
    "도면", "동선", "배수", "그리스", "후드", "덕트", "가스", "전기",
    "주방", "세척", "싱크", "렌지", "냉장", "선반", "시공", "공사",
    "철거", "바닥", "설치", "납품", "완성", "before", "after",
)


def _timestamp(seconds: float) -> str:
    value = max(0, int(seconds))
    return f"{value // 60:02d}:{value % 60:02d}"


def probe_video(path: str | Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate",
            "-of", "json", str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("영상 파일 정보를 확인하지 못했습니다.")
    payload = json.loads(result.stdout or "{}")
    duration = float((payload.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise RuntimeError("영상 길이를 확인하지 못했습니다.")
    streams = payload.get("streams") or []
    video = next((row for row in streams if row.get("codec_type") == "video"), {})
    return {
        "duration_seconds": round(duration, 3),
        "size_bytes": int((payload.get("format") or {}).get("size") or 0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "video_codec": video.get("codec_name"),
        "fps": video.get("r_frame_rate"),
        "has_audio": any(row.get("codec_type") == "audio" for row in streams),
    }


def extract_audio(video_path: str | Path, audio_path: str | Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k",
            str(audio_path), "-y",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0 or not Path(audio_path).is_file():
        raise RuntimeError("영상 음성을 준비하지 못했습니다.")


def _nearby_text(seconds: float, segments: list[dict[str, Any]]) -> str:
    rows = []
    for segment in segments:
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start + 5)
        if start - 4 <= seconds <= end + 4:
            text = str(segment.get("text") or "").strip()
            if text:
                rows.append(text)
    return " ".join(rows)[:360]


def _average_hash(image: Image.Image) -> int:
    reduced = image.convert("L").resize((8, 8))
    pixels = list(reduced.getdata())
    average = sum(pixels) / max(1, len(pixels))
    value = 0
    for pixel in pixels:
        value = (value << 1) | int(pixel >= average)
    return value


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _frame_metrics(
    path: Path, *, previous: Image.Image | None, seconds: float,
    segments: list[dict[str, Any]],
) -> tuple[dict[str, Any], Image.Image]:
    with Image.open(path) as opened:
        image = opened.convert("RGB").resize((320, 180))
    gray = image.convert("L")
    brightness = float(ImageStat.Stat(gray).mean[0])
    edge_variance = float(ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).var[0])
    difference = 0.0
    if previous is not None:
        difference = float(ImageStat.Stat(ImageChops.difference(image, previous)).mean[0])
    stability = max(0.0, min(1.0, 1.0 - difference / 70.0))
    scene_change = max(0.0, min(1.0, difference / 55.0))
    brightness_score = max(0.0, 1.0 - abs(brightness - 128.0) / 128.0)
    focus_score = max(0.0, min(1.0, edge_variance / 1800.0))
    nearby = _nearby_text(seconds, segments)
    tags = [keyword for keyword in BUSINESS_VISUAL_KEYWORDS if keyword.lower() in nearby.lower()]
    business_score = min(1.0, len(tags) / 3.0)
    score = (
        focus_score * 0.25
        + brightness_score * 0.15
        + stability * 0.22
        + scene_change * 0.13
        + business_score * 0.25
    )
    return (
        {
            "time_seconds": round(seconds, 3),
            "timecode": _timestamp(seconds),
            "brightness_score": round(brightness_score, 3),
            "focus_score": round(focus_score, 3),
            "stability_score": round(stability, 3),
            "scene_change_score": round(scene_change, 3),
            "business_tags": tags,
            "nearby_transcript": nearby,
            "selection_score": round(score, 4),
            "hash": _average_hash(image),
        },
        image,
    )


def select_representative_frames(
    video_path: str | Path,
    work_dir: str | Path,
    *,
    duration_seconds: float,
    transcript_segments: list[dict[str, Any]],
    max_selected: int = 30,
    absolute_max: int = 40,
) -> dict[str, Any]:
    """Extract bounded thumbnails and return summaries, never image payloads."""

    selected_limit = max(1, min(int(max_selected), int(absolute_max), 40))
    candidate_limit = 90
    interval = max(2.0, duration_seconds / candidate_limit)
    frames_dir = Path(work_dir) / "frame_candidates"
    frames_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = frames_dir / "frame_%04d.jpg"
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video_path),
            "-vf", f"fps=1/{interval:.6f},scale=320:-2",
            "-frames:v", str(candidate_limit), "-q:v", "5", str(output_pattern), "-y",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        shutil.rmtree(frames_dir, ignore_errors=True)
        return {
            "status": "failed", "selected_frames_count": 0,
            "reason": "frame_extraction_failed", "frames": [],
        }

    candidates: list[dict[str, Any]] = []
    previous = None
    for index, path in enumerate(sorted(frames_dir.glob("frame_*.jpg"))):
        seconds = min(duration_seconds, index * interval)
        try:
            metrics, previous = _frame_metrics(
                path, previous=previous, seconds=seconds,
                segments=transcript_segments,
            )
            candidates.append(metrics)
        except Exception:
            continue

    # Cover the full timeline first, then fill remaining slots by quality and
    # channel-specific evidence value. Similar hashes are never selected twice.
    selected: list[dict[str, Any]] = []
    bucket_seconds = max(8.0, duration_seconds / selected_limit)
    bucket_count = max(1, math.ceil(duration_seconds / bucket_seconds))
    for bucket in range(bucket_count):
        start, end = bucket * bucket_seconds, (bucket + 1) * bucket_seconds
        rows = [row for row in candidates if start <= row["time_seconds"] < end]
        if rows:
            selected.append(max(rows, key=lambda row: row["selection_score"]))
    for row in sorted(candidates, key=lambda item: item["selection_score"], reverse=True):
        if len(selected) >= selected_limit:
            break
        if row in selected:
            continue
        if any(
            abs(row["time_seconds"] - existing["time_seconds"]) < 3
            or _hamming(row["hash"], existing["hash"]) <= 4
            for existing in selected
        ):
            continue
        selected.append(row)
    selected = sorted(selected, key=lambda row: row["time_seconds"])[:selected_limit]
    for row in selected:
        row.pop("hash", None)
        row["summary"] = (
            f"{row['timecode']} | focus={row['focus_score']:.2f}, "
            f"brightness={row['brightness_score']:.2f}, stability={row['stability_score']:.2f}, "
            f"scene_change={row['scene_change_score']:.2f}; "
            f"business_tags={','.join(row['business_tags']) or 'none'}; "
            f"speech={row['nearby_transcript'][:180] or 'none'}"
        )
    shutil.rmtree(frames_dir, ignore_errors=True)
    return {
        "status": "available" if selected else "no_data",
        "candidate_frames_count": len(candidates),
        "selected_frames_count": len(selected),
        "interval_seconds": round(interval, 3),
        "absolute_limit": 40,
        "selection_method": (
            "timeline_coverage+scene_change+focus+brightness+stability+"
            "duplicate_hash+transcript_business_tags"
        ),
        "images_sent_to_gpt": 0,
        "frames": selected,
    }


def compress_transcript_segments(
    segments: list[dict[str, Any]], *, max_chars: int = 12000
) -> dict[str, Any]:
    """Build extractive 30-second summaries and key lines without another model."""

    normalized = [
        {
            "start": float(row.get("start") or 0),
            "end": float(row.get("end") or row.get("start") or 0),
            "text": str(row.get("text") or "").strip(),
        }
        for row in segments
        if str(row.get("text") or "").strip()
    ]
    windows: dict[int, list[dict[str, Any]]] = {}
    for row in normalized:
        windows.setdefault(int(row["start"] // 30), []).append(row)
    summaries = []
    for bucket, rows in sorted(windows.items()):
        combined = " ".join(row["text"] for row in rows)
        tags = [key for key in BUSINESS_VISUAL_KEYWORDS if key.lower() in combined.lower()]
        summaries.append(
            {
                "range": f"{_timestamp(bucket * 30)}-{_timestamp((bucket + 1) * 30)}",
                "summary": combined[:420],
                "business_tags": tags,
            }
        )
    key_sentences = []
    for row in normalized:
        tags = [key for key in BUSINESS_VISUAL_KEYWORDS if key.lower() in row["text"].lower()]
        if tags or len(key_sentences) < 5:
            key_sentences.append(
                {
                    "timecode": _timestamp(row["start"]),
                    "text": row["text"][:260],
                    "business_tags": tags,
                }
            )
    payload = {
        "segment_count": len(normalized),
        "window_summaries": summaries,
        "key_sentences": key_sentences[:40],
    }
    serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > max_chars:
        payload["window_summaries"] = summaries[: max(1, len(summaries) // 2)]
        payload["key_sentences"] = key_sentences[:20]
        payload["truncated"] = True
    return payload
