import os
import httpx
from typing import List, Dict, Optional

VIEWTRAP_BASE = "https://api.viewtrap.com"
LATEST_ROUND = 92


class ViewTrapService:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Cookie": f"token={token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://app.viewtrap.com",
            "Referer": "https://app.viewtrap.com/",
        }

    async def _fetch_videos(self, round_no: int) -> List[Dict]:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{VIEWTRAP_BASE}/api/v2/contents/videos",
                    params={"round_no": round_no},
                    headers=self.headers,
                )
            if resp.status_code != 200:
                return []
            return resp.json().get("data", {}).get("videos", [])
        except Exception:
            return []

    def _normalize(self, item: Dict) -> Dict:
        return {
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "thumbnail": item.get("thumbnail", ""),
            "channel": item.get("channelTitle", ""),
            "views": item.get("viewCount", 0),
            "likes": item.get("likeCount", 0),
            "comments": item.get("commentCount", 0),
            "performance_rate": item.get("performanceRate", 0),
            "performance_rate_str": item.get("performanceRateStr", ""),
            "shorts": item.get("shorts", False),
            "new_video": item.get("newVideo", False),
            "published_at": item.get("publishedAt", ""),
            "duration_sec": item.get("durationSec", 0),
        }

    async def get_top_videos(self, round_no: int = LATEST_ROUND, limit: int = 20) -> List[Dict]:
        """성과율 높은 영상 (일반 영상만, shorts 제외)"""
        raw = await self._fetch_videos(round_no)
        filtered = [v for v in raw if not v.get("shorts") and v.get("display_yn") == "Y"]
        filtered.sort(key=lambda v: v.get("performanceRate") or 0, reverse=True)
        return [self._normalize(v) for v in filtered[:limit]]

    async def get_hot_videos(self, round_no: int = LATEST_ROUND, limit: int = 20) -> List[Dict]:
        """최근 신규 + 성과율 높은 영상 (핫비디오)"""
        raw = await self._fetch_videos(round_no)
        filtered = [v for v in raw if v.get("newVideo") and v.get("display_yn") == "Y"]
        if not filtered:
            # 신규 없으면 전체에서 성과율 상위
            filtered = [v for v in raw if v.get("display_yn") == "Y"]
        filtered.sort(key=lambda v: v.get("performanceRate") or 0, reverse=True)
        return [self._normalize(v) for v in filtered[:limit]]
