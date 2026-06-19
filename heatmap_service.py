"""
YouTube Most Replayed 히트맵 데이터 수집.
yt-dlp를 사용해 시청자 집중 구간(heatmap)을 추출.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, List, Dict

_executor = ThreadPoolExecutor(max_workers=3)


def _fetch_heatmap_sync(video_id: str) -> Optional[Dict]:
    """yt-dlp로 히트맵 + duration 동기 추출 (executor에서 실행)."""
    import warnings
    warnings.filterwarnings("ignore")
    try:
        import yt_dlp
    except ImportError:
        return None

    ydl_opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}",
                download=False,
            )
        return {
            "heatmap": info.get("heatmap") or [],
            "duration": info.get("duration") or 0,
        }
    except Exception:
        return None


async def fetch_heatmap(video_id: str) -> Optional[Dict]:
    """비동기 래퍼: 히트맵 + duration 반환."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _fetch_heatmap_sync, video_id)


def _fmt_time(sec: float) -> str:
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _get_top_hotspots(heatmap: List[Dict], top_n: int = 5) -> List[Dict]:
    """강도 기준 상위 N개 구간 추출 (인접 구간 병합)."""
    if not heatmap:
        return []

    threshold = 0.65
    high = [m for m in heatmap if m.get("value", 0) >= threshold]

    # 인접 구간 병합 (30초 이내)
    merged = []
    for seg in sorted(high, key=lambda x: x["start_time"]):
        if merged and seg["start_time"] - merged[-1]["end_time"] < 30:
            merged[-1]["end_time"] = max(merged[-1]["end_time"], seg["end_time"])
            merged[-1]["value"] = max(merged[-1]["value"], seg.get("value", 0))
        else:
            merged.append({"start_time": seg["start_time"], "end_time": seg["end_time"], "value": seg.get("value", 0)})

    merged.sort(key=lambda x: x["value"], reverse=True)
    return merged[:top_n]


def summarize_for_prompt(heatmap: List[Dict], duration_sec: float = 0) -> str:
    """프롬프트에 포함할 텍스트 요약."""
    if not heatmap:
        return ""

    hotspots = _get_top_hotspots(heatmap)
    if not hotspots:
        return ""

    lines = ["Most Replayed 핫스팟 (시청자가 가장 많이 되감은 구간):"]
    for i, seg in enumerate(hotspots, 1):
        pct = f" (영상의 {int(seg['start_time'] / duration_sec * 100)}% 지점)" if duration_sec > 0 else ""
        lines.append(
            f"  {i}위 {_fmt_time(seg['start_time'])}~{_fmt_time(seg['end_time'])}"
            f" 강도{int(seg['value'] * 100)}%{pct}"
        )

    # 패턴 분류
    dur = heatmap[-1]["end_time"] if heatmap else 1
    if dur <= 0:
        dur = 1
    avg_first = _avg(heatmap, 0, dur / 3)
    avg_mid = _avg(heatmap, dur / 3, dur * 2 / 3)
    avg_last = _avg(heatmap, dur * 2 / 3, dur)

    if avg_first >= avg_mid and avg_first >= avg_last:
        pattern = "초반 집중형 — 오프닝/훅 구간에서 가장 높은 재생 집중도"
    elif avg_mid >= avg_first and avg_mid >= avg_last:
        pattern = "중반 클라이맥스형 — 핵심 정보/비교 구간에서 최고 집중도"
    elif avg_last > avg_mid:
        pattern = "후반 강화형 — 결론/가격공개/CTA 구간 집중"
    else:
        pattern = "전체 고른 참여형"

    lines.append(f"참여 패턴: {pattern}")
    return "\n".join(lines)


def _avg(heatmap: List[Dict], from_sec: float, to_sec: float) -> float:
    segs = [m for m in heatmap if from_sec <= m["start_time"] < to_sec]
    if not segs:
        return 0.0
    return sum(m.get("value", 0) for m in segs) / len(segs)
