import asyncio
import re
import httpx
from datetime import datetime, timezone
from typing import List, Dict, Tuple

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
            "part": "snippet,statistics,contentDetails",
        })
        resp.raise_for_status()

        videos = []
        for item in resp.json().get("items", []):
            s = item.get("statistics", {})
            sn = item.get("snippet", {})
            cd = item.get("contentDetails", {})
            thumbnails = sn.get("thumbnails", {})
            thumb = (thumbnails.get("maxres") or thumbnails.get("high") or thumbnails.get("medium") or {}).get("url", "")
            dur = cd.get("duration", "PT0S")
            h = re.search(r"(\d+)H", dur)
            m_ = re.search(r"(\d+)M", dur)
            s2 = re.search(r"(\d+)S", dur)
            duration_sec = (int(h.group(1)) * 3600 if h else 0) + \
                           (int(m_.group(1)) * 60 if m_ else 0) + \
                           (int(s2.group(1)) if s2 else 0)
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
                "duration_sec": duration_sec,
                "url": f"https://www.youtube.com/watch?v={item['id']}",
            })

        videos.sort(key=lambda x: x["view_count"], reverse=True)
        return videos

    async def get_videos_by_ids(self, video_ids: List[str]) -> List[Dict]:
        """영상 ID 목록 → 제목·썸네일·통계 (search_videos와 같은 dict 형태)."""
        if not video_ids:
            return []
        resp = await self.client.get(f"{BASE}/videos", params={
            "key": self.api_key,
            "id": ",".join(video_ids),
            "part": "snippet,statistics,contentDetails",
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

    async def get_channel_info(self, channel_id: str) -> Dict:
        resp = await self.client.get(f"{BASE}/channels", params={
            "key": self.api_key,
            "id": channel_id,
            "part": "snippet,statistics,contentDetails",
        })
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items:
            raise ValueError(f"채널을 찾을 수 없습니다: {channel_id}")
        item = items[0]
        return {
            "id": item["id"],
            "title": item["snippet"]["title"],
            "subscriber_count": int(item["statistics"].get("subscriberCount", 0)),
            "video_count": int(item["statistics"].get("videoCount", 0)),
            "view_count": int(item["statistics"].get("viewCount", 0)),
            "uploads_playlist_id": item["contentDetails"]["relatedPlaylists"]["uploads"],
        }

    async def get_channel_videos(self, channel_id: str, max_videos: int = 200) -> Tuple[Dict, List[Dict]]:
        channel_info = await self.get_channel_info(channel_id)
        playlist_id = channel_info["uploads_playlist_id"]

        video_ids: List[str] = []
        page_token = None
        while len(video_ids) < max_videos:
            params: Dict = {
                "key": self.api_key,
                "playlistId": playlist_id,
                "part": "snippet",
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            resp = await self.client.get(f"{BASE}/playlistItems", params=params)
            resp.raise_for_status()
            data = resp.json()
            for it in data.get("items", []):
                vid_id = it["snippet"]["resourceId"].get("videoId", "")
                if vid_id:
                    video_ids.append(vid_id)
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        video_ids = video_ids[:max_videos]

        videos: List[Dict] = []
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            resp = await self.client.get(f"{BASE}/videos", params={
                "key": self.api_key,
                "id": ",".join(batch),
                "part": "snippet,statistics,contentDetails",
            })
            resp.raise_for_status()
            for item in resp.json().get("items", []):
                s = item.get("statistics", {})
                sn = item.get("snippet", {})
                cd = item.get("contentDetails", {})

                dur = cd.get("duration", "PT0S")
                h = re.search(r"(\d+)H", dur)
                m = re.search(r"(\d+)M", dur)
                s2 = re.search(r"(\d+)S", dur)
                duration_sec = (int(h.group(1)) * 3600 if h else 0) + \
                               (int(m.group(1)) * 60 if m else 0) + \
                               (int(s2.group(1)) if s2 else 0)

                pub = sn.get("publishedAt", "")
                try:
                    dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                    publish_day_en = dt.strftime("%A")
                    DAY_KR = {"Monday": "월", "Tuesday": "화", "Wednesday": "수",
                              "Thursday": "목", "Friday": "금", "Saturday": "토", "Sunday": "일"}
                    publish_day = DAY_KR.get(publish_day_en, publish_day_en)
                    publish_hour = dt.hour
                    publish_date = dt.strftime("%Y-%m-%d")
                except Exception:
                    publish_day = ""
                    publish_hour = 0
                    publish_date = pub[:10]

                view_count = int(s.get("viewCount", 0))
                like_count = int(s.get("likeCount", 0))
                comment_count = int(s.get("commentCount", 0))

                videos.append({
                    "id": item["id"],
                    "title": sn.get("title", ""),
                    "description": sn.get("description", "")[:200],
                    "tags": sn.get("tags", [])[:8],
                    "published_at": publish_date,
                    "publish_day": publish_day,
                    "publish_hour": publish_hour,
                    "duration_sec": duration_sec,
                    "view_count": view_count,
                    "like_count": like_count,
                    "comment_count": comment_count,
                    "url": f"https://www.youtube.com/watch?v={item['id']}",
                    "engagement_rate": round((like_count + comment_count) / max(view_count, 1) * 100, 2),
                })

        videos.sort(key=lambda x: x["published_at"], reverse=True)
        return channel_info, videos

    async def close(self):
        await self.client.aclose()
