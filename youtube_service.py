import asyncio
import re
import httpx
from datetime import datetime, timedelta, timezone
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
            # 길고 구체적인 키워드는 조회수순(viewCount)으로 0건이 나올 수 있음 → 관련성순으로 폴백
            params_rel = dict(params); params_rel["order"] = "relevance"
            resp = await self.client.get(f"{BASE}/search", params=params_rel)
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

    # ---------------------------------------------------------------- 검색·트렌드

    # 유튜브가 쓰는 카테고리 번호. 부자주방과 관련 있는 것만 골라 뒀다.
    CATEGORIES = {
        "26": "노하우/스타일(주방·살림·리뷰)",
        "22": "인물/블로그",
        "24": "엔터테인먼트",
        "23": "코미디",
        "28": "과학기술",
        "20": "게임",
        "27": "교육",
        "17": "스포츠",
        "10": "음악",
    }

    async def search_advanced(self, keyword: str, *, days: int = 0, order: str = "viewCount",
                              duration: str = "any", max_results: int = 25,
                              region: str = "KR") -> List[Dict]:
        """조건을 걸어 유튜브를 검색한다.

        days     : 최근 며칠 안의 영상만 (0 = 제한 없음). 트렌드를 볼 땐 30~90일이 유용.
        order    : viewCount(조회수) / date(최신) / relevance(관련도) / rating(평점)
        duration : any / short(4분 미만) / medium(4~20분) / long(20분 초과)

        조회수만으로는 '오래돼서 쌓인 영상'과 '지금 터진 영상'이 구분되지 않으므로,
        하루평균 조회수와 채널 구독자 대비 배수(떡상 지표)를 같이 계산해 돌려준다.
        """
        params = {
            "key": self.api_key, "q": keyword, "part": "snippet", "type": "video",
            "order": order, "maxResults": min(max_results, 50),
            "relevanceLanguage": "ko", "regionCode": region,
        }
        if duration in ("short", "medium", "long"):
            params["videoDuration"] = duration
        if days and days > 0:
            after = datetime.now(timezone.utc) - timedelta(days=days)
            params["publishedAfter"] = after.strftime("%Y-%m-%dT%H:%M:%SZ")

        resp = await self.client.get(f"{BASE}/search", params=params)
        resp.raise_for_status()
        items = resp.json().get("items", [])
        if not items and order == "viewCount":
            # 조건이 좁으면 조회수순은 0건이 나올 수 있다 → 관련도순으로 한 번 더
            params["order"] = "relevance"
            resp = await self.client.get(f"{BASE}/search", params=params)
            resp.raise_for_status()
            items = resp.json().get("items", [])
        ids = [i["id"]["videoId"] for i in items if i.get("id", {}).get("videoId")]
        return await self._videos_with_channel(ids)

    async def get_trending(self, *, category: str = "", region: str = "KR",
                           max_results: int = 25) -> List[Dict]:
        """지금 인기 급상승 영상. 카테고리를 주면 그 분야만."""
        params = {
            "key": self.api_key, "part": "snippet,statistics,contentDetails",
            "chart": "mostPopular", "regionCode": region,
            "maxResults": min(max_results, 50),
        }
        if category:
            params["videoCategoryId"] = category
        resp = await self.client.get(f"{BASE}/videos", params=params)
        resp.raise_for_status()
        vids = [self._video_row(it) for it in resp.json().get("items", [])]
        return await self._attach_channel_stats(vids)

    async def _videos_with_channel(self, ids: List[str]) -> List[Dict]:
        if not ids:
            return []
        resp = await self.client.get(f"{BASE}/videos", params={
            "key": self.api_key, "id": ",".join(ids),
            "part": "snippet,statistics,contentDetails",
        })
        resp.raise_for_status()
        vids = [self._video_row(it) for it in resp.json().get("items", [])]
        return await self._attach_channel_stats(vids)

    async def _attach_channel_stats(self, vids: List[Dict]) -> List[Dict]:
        """채널 구독자수를 붙이고 '떡상 지표'를 계산한다.

        구독자 대비 조회수 배수(view_per_sub)가 크면 구독자 밖으로 퍼진 영상이다.
        작은 채널이 크게 터진 주제 = 우리가 따라 만들 만한 주제.
        """
        ch_ids = list({v["channel_id"] for v in vids if v.get("channel_id")})
        subs = {}
        for i in range(0, len(ch_ids), 50):
            resp = await self.client.get(f"{BASE}/channels", params={
                "key": self.api_key, "id": ",".join(ch_ids[i:i + 50]), "part": "statistics",
            })
            if resp.status_code != 200:
                continue
            for c in resp.json().get("items", []):
                subs[c["id"]] = int(c.get("statistics", {}).get("subscriberCount", 0) or 0)

        now = datetime.now(timezone.utc)
        for v in vids:
            v["subscriber_count"] = subs.get(v.get("channel_id"), 0)
            try:
                pub = datetime.fromisoformat(v["published_full"].replace("Z", "+00:00"))
                age_days = max((now - pub).days, 1)
            except Exception:
                age_days = 1
            v["age_days"] = age_days
            v["views_per_day"] = round(v["view_count"] / age_days)
            v["view_per_sub"] = round(v["view_count"] / v["subscriber_count"], 1) if v["subscriber_count"] else 0
            v["engage_rate"] = round((v["like_count"] + v["comment_count"]) / v["view_count"] * 100, 2) if v["view_count"] else 0
        vids.sort(key=lambda x: x["views_per_day"], reverse=True)
        return vids

    @staticmethod
    def _video_row(item: Dict) -> Dict:
        s = item.get("statistics", {})
        sn = item.get("snippet", {})
        cd = item.get("contentDetails", {})
        th = sn.get("thumbnails", {})
        thumb = (th.get("maxres") or th.get("high") or th.get("medium") or {}).get("url", "")
        dur = cd.get("duration", "PT0S")
        h = re.search(r"(\d+)H", dur)
        m_ = re.search(r"(\d+)M", dur)
        s2 = re.search(r"(\d+)S", dur)
        return {
            "id": item["id"],
            "title": sn.get("title", ""),
            "description": (sn.get("description") or "")[:400],
            "channel": sn.get("channelTitle", ""),
            "channel_id": sn.get("channelId", ""),
            "published_at": (sn.get("publishedAt") or "")[:10],
            "published_full": sn.get("publishedAt") or "",
            "thumbnail_url": thumb,
            "view_count": int(s.get("viewCount", 0) or 0),
            "like_count": int(s.get("likeCount", 0) or 0),
            "comment_count": int(s.get("commentCount", 0) or 0),
            "duration_sec": (int(h.group(1)) * 3600 if h else 0) + (int(m_.group(1)) * 60 if m_ else 0) + (int(s2.group(1)) if s2 else 0),
            "url": f"https://www.youtube.com/watch?v={item['id']}",
        }

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

    async def resolve_channel(self, query: str) -> str:
        """채널 주소·@핸들·이름 아무거나 받아서 채널 ID(UC...)로 바꾼다.

        사장님이 채팅에 채널 링크를 그냥 붙여넣을 수 있어야 하므로,
        받을 수 있는 형태를 최대한 넓게 잡는다.
        """
        q = (query or "").strip()
        m = re.search(r"(UC[A-Za-z0-9_-]{22})", q)          # 주소 안의 채널 ID
        if m:
            return m.group(1)

        handle = None
        m = re.search(r"youtube\.com/@([A-Za-z0-9_.-]+)", q)   # .../@핸들
        if m:
            handle = m.group(1)
        elif q.startswith("@"):
            handle = q[1:]
        if handle:
            r = await self.client.get(f"{BASE}/channels", params={
                "key": self.api_key, "forHandle": "@" + handle, "part": "id"})
            items = r.json().get("items", []) if r.status_code == 200 else []
            if items:
                return items[0]["id"]

        m = re.search(r"youtube\.com/(?:c|user)/([A-Za-z0-9_.-]+)", q)   # 옛날 주소
        if m:
            r = await self.client.get(f"{BASE}/channels", params={
                "key": self.api_key, "forUsername": m.group(1), "part": "id"})
            items = r.json().get("items", []) if r.status_code == 200 else []
            if items:
                return items[0]["id"]

        # 영상 주소를 줬으면 그 영상의 채널을 찾는다
        m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", q)
        if m:
            r = await self.client.get(f"{BASE}/videos", params={
                "key": self.api_key, "id": m.group(1), "part": "snippet"})
            items = r.json().get("items", []) if r.status_code == 200 else []
            if items:
                return items[0]["snippet"]["channelId"]

        # 마지막 수단: 이름으로 검색
        name = re.sub(r"https?://\S+", "", q).strip() or q
        r = await self.client.get(f"{BASE}/search", params={
            "key": self.api_key, "q": name, "type": "channel", "part": "snippet", "maxResults": 1})
        items = r.json().get("items", []) if r.status_code == 200 else []
        if items:
            return items[0]["snippet"]["channelId"]
        raise ValueError(f"채널을 찾지 못했습니다: {query}")

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
