import asyncio
import httpx
from typing import List, Dict

BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeService:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=30.0)

    async def search_videos(self, keyword: str, max_results: int = 20) -> List[Dict]:
        params = {
            "key": self.api_key,
            "q": keyword,
            "part": "snippet",
            "type": "video",
            "order": "viewCount",
            "maxResults": max_results,
            "relevanceLanguage": "ko",
            "regionCode": "KR",
        }
        resp = await self.client.get(f"{BASE}/search", params=params)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            return []

        video_ids = [item["id"]["videoId"] for item in items]
        resp = await self.client.get(f"{BASE}/videos", params={
            "key": self.api_key,
            "id": ",".join(video_ids),
            "part": "snippet,statistics",
        })
        resp.raise_for_status()

        videos = []
        for item in resp.json().get("items", []):
            s = item.get("statistics", {})
            sn = item.get("snippet", {})
            thumbnails = sn.get("thumbnails", {})
            thumb = (thumbnails.get("maxres") or thumbnails.get("high") or thumbnails.get("medium") or {}).get("url", "")
            videos.append({
                "id": item["id"],
                "title": sn.get("title", ""),
                "description": sn.get("description", "")[:500],
                "channel": sn.get("channelTitle", ""),
                "published_at": sn.get("publishedAt", "")[:10],
                "thumbnail_url": thumb,
                "view_count": int(s.get("viewCount", 0)),
                "like_count": int(s.get("likeCount", 0)),
                "comment_count": int(s.get("commentCount", 0)),
                "url": f"https://www.youtube.com/watch?v={item['id']}",
            })

        videos.sort(key=lambda x: x["view_count"], reverse=True)
        return videos

    async def get_comments(self, video_id: str, max_comments: int = 50) -> List[Dict]:
        try:
            resp = await self.client.get(f"{BASE}/commentThreads", params={
                "key": self.api_key,
                "videoId": video_id,
                "part": "snippet",
                "order": "relevance",
                "maxResults": 100,
            })
            resp.raise_for_status()
        except Exception:
            return []

        comments = []
        for item in resp.json().get("items", []):
            c = item["snippet"]["topLevelComment"]["snippet"]
            text = c.get("textDisplay", "").strip()
            if len(text) > 10:
                comments.append({
                    "text": text[:300],
                    "like_count": c.get("likeCount", 0),
                })

        comments.sort(key=lambda x: x["like_count"], reverse=True)
        return comments[:max_comments]

    async def get_comments_for_videos(self, videos: List[Dict]) -> List[Dict]:
        results = await asyncio.gather(
            *[self.get_comments(v["id"]) for v in videos],
            return_exceptions=True,
        )
        for video, comments in zip(videos, results):
            video["comments"] = comments if not isinstance(comments, Exception) else []
        return videos

    async def close(self):
        await self.client.aclose()
