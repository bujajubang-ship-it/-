import httpx
import os
from datetime import date
from typing import Dict, List


TOKEN_URL = "https://oauth2.googleapis.com/token"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"


class AnalyticsService:
    def __init__(self):
        self.client_id = os.getenv("OAUTH_CLIENT_ID", "")
        self.client_secret = os.getenv("OAUTH_CLIENT_SECRET", "")
        self.refresh_token = os.getenv("OAUTH_REFRESH_TOKEN", "")
        self._access_token = ""
        self.http = httpx.AsyncClient(timeout=30.0)

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    async def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        resp = await self.http.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
        })
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]
        return self._access_token

    async def get_video_analytics(self, start_date: str = "2020-01-01") -> List[Dict]:
        """영상별 CTR, 시청 시간, 평균 시청 유지율 가져오기"""
        token = await self._get_access_token()
        end_date = date.today().isoformat()

        resp = await self.http.get(ANALYTICS_URL, params={
            "ids": "channel==MINE",
            "startDate": start_date,
            "endDate": end_date,
            "metrics": "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,impressions,impressionClickThroughRate,likes,comments,subscribersGained",
            "dimensions": "video",
            "sort": "-views",
            "maxResults": 200,
        }, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()

        data = resp.json()
        headers = [h["name"] for h in data.get("columnHeaders", [])]
        rows = data.get("rows", [])

        results = []
        for row in rows:
            item = dict(zip(headers, row))
            results.append({
                "video_id": item.get("video", ""),
                "views": int(item.get("views", 0)),
                "watch_minutes": round(float(item.get("estimatedMinutesWatched", 0))),
                "avg_view_duration_sec": round(float(item.get("averageViewDuration", 0))),
                "avg_view_percentage": round(float(item.get("averageViewPercentage", 0)), 1),
                "impressions": int(item.get("impressions", 0)),
                "ctr": round(float(item.get("impressionClickThroughRate", 0)) * 100, 2),
                "likes": int(item.get("likes", 0)),
                "comments": int(item.get("comments", 0)),
                "subscribers_gained": int(item.get("subscribersGained", 0)),
            })
        return results

    async def get_channel_overview(self) -> Dict:
        """채널 전체 최근 30일 요약"""
        token = await self._get_access_token()
        end_date = date.today().isoformat()

        resp = await self.http.get(ANALYTICS_URL, params={
            "ids": "channel==MINE",
            "startDate": "2025-01-01",
            "endDate": end_date,
            "metrics": "views,estimatedMinutesWatched,averageViewDuration,impressionClickThroughRate,subscribersGained,subscribersLost",
            "dimensions": "month",
            "sort": "month",
        }, headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json()

    async def close(self):
        await self.http.aclose()
