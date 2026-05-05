import re
import httpx
from typing import List, Dict


class NaverService:
    def __init__(self, client_id: str, client_secret: str):
        self.headers = {
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        }
        self.client = httpx.AsyncClient(timeout=20.0)

    def _clean(self, text: str) -> str:
        return re.sub(r"<[^>]+>", "", text).strip()

    async def search_cafe(self, keyword: str, display: int = 100) -> List[Dict]:
        try:
            resp = await self.client.get(
                "https://openapi.naver.com/v1/search/cafearticle.json",
                params={"query": keyword, "display": display, "sort": "sim"},
                headers=self.headers,
            )
            resp.raise_for_status()
        except Exception:
            return []

        results = []
        for item in resp.json().get("items", []):
            title = self._clean(item.get("title", ""))
            desc = self._clean(item.get("description", ""))
            if title:
                results.append({
                    "title": title,
                    "description": desc[:300],
                    "cafe_name": item.get("cafename", ""),
                    "link": item.get("link", ""),
                })
        return results

    async def close(self):
        await self.client.aclose()
