import asyncio
import json
import os
import re
import uuid
import subprocess

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from analyzer import Analyzer
from naver_service import NaverService
from youtube_service import YouTubeService
from analytics_service import AnalyticsService
from viewtrap_service import ViewTrapService
from heatmap_service import fetch_heatmap, summarize_for_prompt
from database import (init_db, save_history, list_history, get_history, delete_history,
                       init_pipeline, list_pipeline, create_pipeline_item,
                       update_pipeline_item, delete_pipeline_item)

init_db()
init_pipeline()

app = FastAPI(title="YouTube Content Researcher")


async def fetch_product_info(url: str) -> str:
    """스마트스토어 상품 페이지에서 제품 정보 추출"""
    if not url or not url.startswith("http"):
        return ""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9",
        }
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
        html = resp.text

        # og 메타태그 추출
        def meta(prop):
            m = re.search(rf'<meta[^>]+(?:property|name)=["\']og:{prop}["\'][^>]+content=["\']([^"\']+)["\']', html)
            if not m:
                m = re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:{prop}["\']', html)
            return m.group(1).strip() if m else ""

        title       = meta("title")
        description = meta("description")

        # JSON-LD 구조화 데이터 추출 (가격·브랜드 등)
        jld_match = re.search(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S)
        jld_text = ""
        if jld_match:
            try:
                jld = json.loads(jld_match.group(1))
                if isinstance(jld, dict):
                    name   = jld.get("name", "")
                    price  = jld.get("offers", {}).get("price", "") if isinstance(jld.get("offers"), dict) else ""
                    brand  = jld.get("brand", {}).get("name", "") if isinstance(jld.get("brand"), dict) else ""
                    desc   = jld.get("description", "")
                    jld_text = "\n".join(filter(None, [
                        f"제품명: {name}" if name else "",
                        f"브랜드: {brand}" if brand else "",
                        f"가격: {price}원" if price else "",
                        f"설명: {desc[:300]}" if desc else "",
                    ]))
            except Exception:
                pass

        parts = []
        if title:       parts.append(f"제품명: {title}")
        if description: parts.append(f"소개: {description[:200]}")
        if jld_text:    parts.append(jld_text)

        return "\n".join(parts) if parts else ""
    except Exception:
        return ""
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class AnalyzeRequest(BaseModel):
    keyword: str


class EditFeedbackRequest(BaseModel):
    keyword: str
    script: str
    product_url: str = ""


class PlanningRequest(BaseModel):
    keyword: str
    product_desc: str
    market_insights: str = ""


class IntroRequest(BaseModel):
    keyword: str
    product_desc: str
    problem_definition: str
    viewer_desire: str


class ScriptRequest(BaseModel):
    keyword: str
    product_desc: str
    reference_script: str
    context: str = ""


class ShortformRequest(BaseModel):
    keyword: str
    product_desc: str = ""
    duration: str = "30"


class MidformRequest(BaseModel):
    keyword: str
    product_desc: str = ""
    product_url: str = ""


class ChannelAnalyzeRequest(BaseModel):
    channel_id: str


class VideoDecisionRequest(BaseModel):
    videos: list


class SnsConvertRequest(BaseModel):
    keyword: str
    script: str


class AttachmentItem(BaseModel):
    media_type: str   # image/jpeg, image/png, image/webp, application/pdf
    data: str         # base64


class ChatRequest(BaseModel):
    message: str
    history: list = []
    attachments: list = []  # List[AttachmentItem]


class DetailPageRequest(BaseModel):
    keyword: str
    product_desc: str = ""
    price: str = ""
    target_customer: str = ""


class BlogRequest(BaseModel):
    keyword: str
    memo: str = ""
    region: str = ""
    link: str = ""
    photos: list = []  # [{"media_type": "image/jpeg", "data": "base64..."}]


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/health")
async def health():
    return {
        "youtube": bool(os.getenv("YOUTUBE_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "naver": bool(os.getenv("NAVER_CLIENT_ID")),
        "analytics": bool(os.getenv("OAUTH_REFRESH_TOKEN")),
        "viewtrap": bool(os.getenv("VIEWTRAP_TOKEN")),
        "my_channel_id": os.getenv("MY_CHANNEL_ID", ""),
    }


@app.get("/api/viewtrap-ref")
async def viewtrap_ref():
    token = os.getenv("VIEWTRAP_TOKEN", "").strip()
    if not token:
        return {"top_videos": [], "hot_videos": [], "error": "VIEWTRAP_TOKEN 없음"}
    svc = ViewTrapService(token)
    top, hot = await asyncio.gather(
        svc.get_top_videos(),
        svc.get_hot_videos(),
    )
    return {"top_videos": top[:20], "hot_videos": hot[:20]}


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    async def stream():
        if not youtube_key:
            yield sse({"step": "error", "message": ".env 파일에 YOUTUBE_API_KEY를 설정해주세요."})
            return
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return

        yt = YouTubeService(youtube_key)
        try:
            yield sse({"step": "searching", "message": f'"{req.keyword}" 유튜브 영상 검색 중...'})
            videos = await yt.search_videos(req.keyword, max_results=20)

            if not videos:
                yield sse({"step": "error", "message": "검색 결과가 없습니다. 키워드를 확인해주세요."})
                return

            yield sse({"step": "found", "message": f"상위 {len(videos)}개 영상 발견! 댓글 수집 중..."})
            videos_with_comments = await yt.get_comments_for_videos(videos[:10])
            total = sum(len(v.get("comments", [])) for v in videos_with_comments)
            yield sse({"step": "comments_done", "message": f"댓글 {total}개 수집 완료!"})

            # Most Replayed 히트맵 수집 (상위 5개 영상, 병렬)
            yield sse({"step": "heatmap", "message": "Most Replayed 시청 패턴 수집 중..."})
            heatmap_tasks = [fetch_heatmap(v["id"]) for v in videos_with_comments[:5]]
            heatmap_results = await asyncio.gather(*heatmap_tasks, return_exceptions=True)
            heatmap_count = 0
            for v, result in zip(videos_with_comments[:5], heatmap_results):
                if isinstance(result, dict) and result.get("heatmap"):
                    dur = result.get("duration") or v.get("duration_sec", 0)
                    summary = summarize_for_prompt(result["heatmap"], dur)
                    if summary:
                        v["heatmap_summary"] = summary
                        heatmap_count += 1
            yield sse({"step": "heatmap_done", "message": f"시청 패턴 수집 완료 ({heatmap_count}개 영상)"})

            naver_results = []
            if naver_id and naver_secret:
                yield sse({"step": "naver", "message": "네이버 카페 반응 수집 중..."})
                naver = NaverService(naver_id, naver_secret)
                naver_results = await naver.search_cafe(req.keyword)
                await naver.close()
                yield sse({"step": "naver_done", "message": f"네이버 카페 {len(naver_results)}개 게시글 수집 완료!"})

            yield sse({"step": "analyzing", "message": "AI 분석 중... (보통 30~60초 소요)"})
            analyzer = Analyzer()
            report = await analyzer.analyze(req.keyword, videos_with_comments, naver_results)

            # Attach top video metadata for UI display
            report["top_videos"] = [
                {
                    "title": v["title"],
                    "views": v["view_count"],
                    "url": v["url"],
                    "thumbnail": v["thumbnail_url"],
                    "channel": v["channel"],
                    "success_reason": next(
                        (tv.get("success_reason", "") for tv in report.get("top_videos", []) if tv.get("url") == v["url"]),
                        "",
                    ),
                }
                for v in videos[:8]
            ]

            save_history("research", req.keyword, report)
            yield sse({"step": "done", "report": report, "keyword": req.keyword})

        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            await yt.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/edit-feedback")
async def edit_feedback(req: EditFeedbackRequest):
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    async def stream():
        if not youtube_key:
            yield sse({"step": "error", "message": ".env 파일에 YOUTUBE_API_KEY를 설정해주세요."})
            return
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return
        if not req.script.strip():
            yield sse({"step": "error", "message": "대본을 입력해주세요."})
            return

        yt = YouTubeService(youtube_key)
        try:
            yield sse({"step": "searching", "message": f'"{req.keyword}" 시장 데이터 수집 중...'})
            videos = await yt.search_videos(req.keyword, max_results=20)

            if not videos:
                yield sse({"step": "error", "message": "검색 결과가 없습니다. 키워드를 확인해주세요."})
                return

            yield sse({"step": "found", "message": f"상위 {len(videos)}개 영상 발견! 댓글 수집 중..."})
            videos_with_comments = await yt.get_comments_for_videos(videos[:10])
            total = sum(len(v.get("comments", [])) for v in videos_with_comments)
            yield sse({"step": "comments_done", "message": f"댓글 {total}개 수집 완료!"})

            # Most Replayed 히트맵 수집 (상위 5개 영상, 병렬)
            yield sse({"step": "heatmap", "message": "Most Replayed 시청 패턴 수집 중..."})
            heatmap_tasks = [fetch_heatmap(v["id"]) for v in videos_with_comments[:5]]
            heatmap_results = await asyncio.gather(*heatmap_tasks, return_exceptions=True)
            heatmap_count = 0
            for v, result in zip(videos_with_comments[:5], heatmap_results):
                if isinstance(result, dict) and result.get("heatmap"):
                    dur = result.get("duration") or v.get("duration_sec", 0)
                    summary = summarize_for_prompt(result["heatmap"], dur)
                    if summary:
                        v["heatmap_summary"] = summary
                        heatmap_count += 1
            yield sse({"step": "heatmap_done", "message": f"시청 패턴 수집 완료 ({heatmap_count}개 영상)"})

            naver_results = []
            if naver_id and naver_secret:
                yield sse({"step": "naver", "message": "네이버 카페 반응 수집 중..."})
                naver = NaverService(naver_id, naver_secret)
                naver_results = await naver.search_cafe(req.keyword)
                await naver.close()
                yield sse({"step": "naver_done", "message": f"네이버 카페 {len(naver_results)}개 게시글 수집 완료!"})

            # 스마트스토어 URL 크롤링
            product_page_info = ""
            if req.product_url.strip():
                yield sse({"step": "crawling", "message": "스마트스토어 상품 페이지 분석 중..."})
                product_page_info = await fetch_product_info(req.product_url)

            script_with_product = req.script
            if product_page_info:
                script_with_product = f"[스마트스토어 상품 정보]\n{product_page_info}\n\n[영상 대본]\n{req.script}"

            # ViewTrap 레퍼런스 수집
            viewtrap_refs = None
            vt_token = os.getenv("VIEWTRAP_TOKEN", "").strip()
            if vt_token:
                yield sse({"step": "viewtrap", "message": "ViewTrap 성과 레퍼런스 수집 중..."})
                vt_svc = ViewTrapService(vt_token)
                vt_top, vt_hot = await asyncio.gather(
                    vt_svc.get_top_videos(),
                    vt_svc.get_hot_videos(),
                )
                viewtrap_refs = {"top_videos": vt_top, "hot_videos": vt_hot}
                total_refs = len(vt_top) + len(vt_hot)
                if total_refs:
                    yield sse({"step": "viewtrap_done", "message": f"ViewTrap 레퍼런스 {total_refs}개 수집 완료!"})

            yield sse({"step": "analyzing", "message": "AI가 대본 분석 중... (보통 20~40초 소요)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.analyze_edit_feedback(req.keyword, script_with_product, videos_with_comments, naver_results, viewtrap_refs))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()

            if viewtrap_refs:
                report["viewtrap_top"] = viewtrap_refs.get("top_videos", [])[:10]
                report["viewtrap_hot"] = viewtrap_refs.get("hot_videos", [])[:10]

            save_history("edit", req.keyword, report)
            yield sse({"step": "done", "report": report, "keyword": req.keyword})

        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            await yt.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/planning")
async def planning(req: PlanningRequest):
    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return
        if not req.product_desc.strip():
            yield sse({"step": "error", "message": "내 제품/서비스 설명을 입력해주세요."})
            return
        try:
            yield sse({"step": "analyzing", "message": "AI가 문제 정의 + 제목 + 썸네일 기획 중... (30초 내외)"})
            analyzer = Analyzer()
            report = await analyzer.analyze_planning(req.keyword, req.product_desc, req.market_insights)
            save_history("planning", req.keyword, report)
            yield sse({"step": "done", "report": report, "keyword": req.keyword})
        except Exception as e:
            yield sse({"step": "error", "message": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/intro")
async def intro(req: IntroRequest):
    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return
        if not req.problem_definition.strip():
            yield sse({"step": "error", "message": "문제 정의를 입력해주세요."})
            return
        if not req.viewer_desire.strip():
            yield sse({"step": "error", "message": "시청자가 원하는 것을 입력해주세요."})
            return
        try:
            yield sse({"step": "analyzing", "message": "AI가 도입부 대본 작성 중... (30초 내외)"})
            analyzer = Analyzer()
            report = await analyzer.write_intro(req.keyword, req.product_desc, req.problem_definition, req.viewer_desire)
            save_history("intro", req.keyword, report)
            yield sse({"step": "done", "report": report, "keyword": req.keyword})
        except Exception as e:
            yield sse({"step": "error", "message": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/script")
async def script(req: ScriptRequest):
    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return
        if not req.reference_script.strip():
            yield sse({"step": "error", "message": "레퍼런스 대본을 입력해주세요."})
            return
        try:
            yield sse({"step": "analyzing", "message": "AI가 대본 분석 및 변형 중... (30~60초 소요)"})
            analyzer = Analyzer()
            report = await analyzer.write_script(req.keyword, req.product_desc, req.reference_script, req.context)
            save_history("script", req.keyword, report)
            yield sse({"step": "done", "report": report, "keyword": req.keyword})
        except Exception as e:
            yield sse({"step": "error", "message": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/shortform")
async def shortform(req: ShortformRequest):
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return
        if not req.keyword.strip():
            yield sse({"step": "error", "message": "주제/키워드를 입력해주세요."})
            return

        videos_with_comments = []
        naver_results = []
        yt = YouTubeService(youtube_key) if youtube_key else None
        try:
            if yt:
                yield sse({"step": "searching", "message": f'"{req.keyword}" 시장 데이터 수집 중...'})
                videos = await yt.search_videos(req.keyword, max_results=10)
                if videos:
                    yield sse({"step": "found", "message": f"상위 {len(videos)}개 영상 발견! 댓글 수집 중..."})
                    videos_with_comments = await yt.get_comments_for_videos(videos[:5])
                    total = sum(len(v.get("comments", [])) for v in videos_with_comments)
                    yield sse({"step": "comments_done", "message": f"댓글 {total}개 수집 완료!"})

                    yield sse({"step": "heatmap", "message": "Most Replayed 시청 패턴 수집 중..."})
                    heatmap_tasks = [fetch_heatmap(v["id"]) for v in videos_with_comments[:5]]
                    heatmap_results = await asyncio.gather(*heatmap_tasks, return_exceptions=True)
                    heatmap_count = 0
                    for v, result in zip(videos_with_comments[:5], heatmap_results):
                        if isinstance(result, dict) and result.get("heatmap"):
                            dur = result.get("duration") or v.get("duration_sec", 0)
                            summary = summarize_for_prompt(result["heatmap"], dur)
                            if summary:
                                v["heatmap_summary"] = summary
                                heatmap_count += 1
                    yield sse({"step": "heatmap_done", "message": f"시청 패턴 수집 완료 ({heatmap_count}개 영상)"})

            if naver_id and naver_secret:
                yield sse({"step": "naver", "message": "네이버 카페 반응 수집 중..."})
                naver = NaverService(naver_id, naver_secret)
                naver_results = await naver.search_cafe(req.keyword)
                await naver.close()
                yield sse({"step": "naver_done", "message": f"네이버 카페 {len(naver_results)}개 게시글 수집 완료!"})

            yield sse({"step": "analyzing", "message": f"AI가 {req.duration}초 릴스 기획 중... (30~60초 소요)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.analyze_shortform(
                req.keyword, req.product_desc, req.duration,
                videos_with_comments or None, naver_results or None
            ))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
            save_history("shortform", req.keyword, report)
            yield sse({"step": "done", "report": report, "keyword": req.keyword})
        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            if yt:
                await yt.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/midform")
async def midform(req: MidformRequest):
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    async def stream():
        if not youtube_key:
            yield sse({"step": "error", "message": ".env 파일에 YOUTUBE_API_KEY를 설정해주세요."})
            return
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return

        yt = YouTubeService(youtube_key)
        try:
            yield sse({"step": "searching", "message": f'"{req.keyword}" 유튜브 영상 검색 중...'})
            videos = await yt.search_videos(req.keyword, max_results=20)
            if not videos:
                yield sse({"step": "error", "message": "검색 결과가 없습니다. 키워드를 확인해주세요."})
                return

            yield sse({"step": "found", "message": f"상위 {len(videos)}개 영상 발견! 댓글 수집 중..."})
            videos_with_comments = await yt.get_comments_for_videos(videos[:10])
            total = sum(len(v.get("comments", [])) for v in videos_with_comments)
            yield sse({"step": "comments_done", "message": f"댓글 {total}개 수집 완료!"})

            # Most Replayed 히트맵 수집 (상위 5개 영상, 병렬)
            yield sse({"step": "heatmap", "message": "Most Replayed 시청 패턴 수집 중..."})
            heatmap_tasks = [fetch_heatmap(v["id"]) for v in videos_with_comments[:5]]
            heatmap_results = await asyncio.gather(*heatmap_tasks, return_exceptions=True)
            heatmap_count = 0
            for v, result in zip(videos_with_comments[:5], heatmap_results):
                if isinstance(result, dict) and result.get("heatmap"):
                    dur = result.get("duration") or v.get("duration_sec", 0)
                    summary = summarize_for_prompt(result["heatmap"], dur)
                    if summary:
                        v["heatmap_summary"] = summary
                        heatmap_count += 1
            yield sse({"step": "heatmap_done", "message": f"시청 패턴 수집 완료 ({heatmap_count}개 영상)"})

            naver_results = []
            if naver_id and naver_secret:
                yield sse({"step": "naver", "message": "네이버 카페 반응 수집 중..."})
                naver = NaverService(naver_id, naver_secret)
                naver_results = await naver.search_cafe(req.keyword)
                await naver.close()
                yield sse({"step": "naver_done", "message": f"네이버 카페 {len(naver_results)}개 게시글 수집 완료!"})

            # 스마트스토어 URL 크롤링
            product_page_info = ""
            if req.product_url.strip():
                yield sse({"step": "crawling", "message": "스마트스토어 상품 페이지 분석 중..."})
                product_page_info = await fetch_product_info(req.product_url)

            combined_desc = req.product_desc.strip()
            if product_page_info:
                combined_desc = f"[스마트스토어 상품 정보]\n{product_page_info}\n\n[추가 메모]\n{combined_desc}" if combined_desc else f"[스마트스토어 상품 정보]\n{product_page_info}"

            viewtrap_refs = None
            vt_token = os.getenv("VIEWTRAP_TOKEN", "").strip()
            if vt_token:
                yield sse({"step": "viewtrap", "message": "ViewTrap 성과 레퍼런스 수집 중..."})
                vt_svc = ViewTrapService(vt_token)
                vt_top, vt_hot = await asyncio.gather(
                    vt_svc.get_top_videos(),
                    vt_svc.get_hot_videos(),
                )
                viewtrap_refs = {"top_videos": vt_top, "hot_videos": vt_hot}
                total_refs = len(vt_top) + len(vt_hot)
                if total_refs:
                    yield sse({"step": "viewtrap_done", "message": f"ViewTrap 레퍼런스 {total_refs}개 수집 완료!"})

            yield sse({"step": "analyzing", "message": "AI가 전체 영상 기획 작성 중... (1~2분 소요)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.analyze_midform(req.keyword, combined_desc, videos_with_comments, naver_results, viewtrap_refs))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
            if viewtrap_refs:
                report["viewtrap_top"] = viewtrap_refs.get("top_videos", [])[:10]
                report["viewtrap_hot"] = viewtrap_refs.get("hot_videos", [])[:10]
            save_history("midform", req.keyword, report)
            yield sse({"step": "done", "report": report, "keyword": req.keyword})

        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            await yt.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/detail-page")
async def detail_page(req: DetailPageRequest):
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    async def stream():
        if not youtube_key:
            yield sse({"step": "error", "message": ".env 파일에 YOUTUBE_API_KEY를 설정해주세요."})
            return

        yt = YouTubeService(youtube_key)
        try:
            yield sse({"step": "searching", "message": f'"{req.keyword}" 유사 제품 리뷰 검색 중...'})
            videos = await yt.search_videos(req.keyword + " 리뷰", max_results=20)
            if not videos:
                videos = await yt.search_videos(req.keyword, max_results=20)

            yield sse({"step": "found", "message": f"유사 제품 영상 {len(videos)}개 발견! 반응 수집 중..."})
            videos_with_comments = await yt.get_comments_for_videos(videos[:10])
            total = sum(len(v.get("comments", [])) for v in videos_with_comments)
            yield sse({"step": "comments_done", "message": f"고객 반응 {total}개 수집 완료!"})

            naver_results = []
            if naver_id and naver_secret:
                yield sse({"step": "naver", "message": "네이버 후기·커뮤니티 반응 수집 중..."})
                naver = NaverService(naver_id, naver_secret)
                naver_results = await naver.search_cafe(req.keyword)
                await naver.close()
                yield sse({"step": "naver_done", "message": f"네이버 {len(naver_results)}개 수집 완료!"})

            yield sse({"step": "analyzing", "message": "AI가 상세페이지 기획안 작성 중... (1~2분 소요)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(
                analyzer.analyze_detail_page(req.keyword, req.product_desc, req.price, req.target_customer, videos_with_comments, naver_results)
            )
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
            save_history("detail_page", req.keyword, report)
            yield sse({"step": "done", "report": report, "keyword": req.keyword})

        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            await yt.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/topic-suggest")
async def topic_suggest():
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    async def stream():
        if not youtube_key:
            yield sse({"step": "error", "message": ".env 파일에 YOUTUBE_API_KEY를 설정해주세요."})
            return
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return

        yt = YouTubeService(youtube_key)
        try:
            all_videos = []
            yt_keywords = [
                "업소용 주방용품 추천",
                "민쿡tv 주방",
                "식당 주방 아이템",
                "음식점 주방용품",
            ]
            yield sse({"step": "youtube", "message": "유튜브 트렌드 영상 수집 중..."})
            for kw in yt_keywords:
                try:
                    results = await yt.search_videos(kw, max_results=8)
                    all_videos.extend(results)
                except Exception:
                    pass
            # deduplicate by url
            seen = set()
            unique_videos = []
            for v in all_videos:
                if v["url"] not in seen:
                    seen.add(v["url"])
                    unique_videos.append(v)
            yield sse({"step": "youtube_done", "message": f"유튜브 영상 {len(unique_videos)}개 수집 완료!"})

            all_naver = []
            if naver_id and naver_secret:
                naver_keywords = [
                    "주방용품 추천 식당",
                    "업소용 냉장고",
                    "아프니까사장이다 주방",
                    "식당 가스레인지 추천",
                    "고창모 주방",
                ]
                yield sse({"step": "naver", "message": "네이버 카페 트렌드 수집 중..."})
                naver_svc = NaverService(naver_id, naver_secret)
                for kw in naver_keywords:
                    try:
                        results = await naver_svc.search_cafe(kw)
                        all_naver.extend(results)
                    except Exception:
                        pass
                await naver_svc.close()
                yield sse({"step": "naver_done", "message": f"네이버 카페 게시글 {len(all_naver)}개 수집 완료!"})

            yield sse({"step": "analyzing", "message": "AI가 트렌드 분석 + 주제 추천 중... (30~60초 소요)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.analyze_topic_trends(unique_videos, all_naver))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
            save_history("topic", "트렌드 주제 추천", report)
            yield sse({"step": "done", "report": report})

        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            await yt.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/channel-analyze")
async def channel_analyze(req: ChannelAnalyzeRequest):
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()

    async def stream():
        if not youtube_key:
            yield sse({"step": "error", "message": ".env 파일에 YOUTUBE_API_KEY를 설정해주세요."})
            return
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return
        if not req.channel_id.strip():
            yield sse({"step": "error", "message": "채널 ID를 입력해주세요."})
            return

        yt = YouTubeService(youtube_key)
        analytics = AnalyticsService()
        try:
            yield sse({"step": "channel_info", "message": "채널 정보 불러오는 중..."})
            channel_info, videos = await yt.get_channel_videos(req.channel_id.strip(), max_videos=100)
            yield sse({"step": "videos_loaded", "message": f"영상 {len(videos)}개 데이터 수집 완료!"})

            analytics_data = []
            if analytics.is_configured():
                yield sse({"step": "analytics", "message": "Analytics 데이터 수집 중 (CTR, 시청 유지율)..."})
                try:
                    analytics_data = await analytics.get_video_analytics()
                    # video_id 기준으로 videos에 병합
                    analytics_map = {a["video_id"]: a for a in analytics_data}
                    for v in videos:
                        a = analytics_map.get(v["id"], {})
                        v["ctr"] = a.get("ctr", None)
                        v["avg_view_percentage"] = a.get("avg_view_percentage", None)
                        v["watch_minutes"] = a.get("watch_minutes", None)
                        v["subscribers_gained"] = a.get("subscribers_gained", None)
                    yield sse({"step": "analytics_done", "message": f"Analytics {len(analytics_data)}개 영상 데이터 수집 완료!"})
                except Exception as ae:
                    yield sse({"step": "analytics_warn", "message": f"Analytics 데이터 수집 실패 (공개 데이터로 진행): {ae}"})

            yield sse({"step": "analyzing", "message": "AI 분석 중... (30~60초 소요)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.analyze_channel(channel_info, videos))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(3)
            report = _task.result()
            report["channel_info"] = channel_info
            report["total_analyzed"] = len(videos)
            report["has_analytics"] = bool(analytics_data)
            save_history("channel", channel_info.get("title", req.channel_id), report)
            yield sse({"step": "done", "report": report})

        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            await yt.close()
            await analytics.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/video-decision")
async def video_decision(req: VideoDecisionRequest):
    from datetime import date

    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return
        if not req.videos:
            yield sse({"step": "error", "message": "영상 정보를 하나 이상 입력해주세요."})
            return
        try:
            yield sse({"step": "analyzing", "message": f"영상 {len(req.videos)}개 분석 중... (30~60초 소요)"})
            analyzer = Analyzer()
            current_date = date.today().strftime("%Y년 %m월 %d일")
            _task = asyncio.create_task(analyzer.analyze_video_decision(req.videos, current_date))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(3)
            report = _task.result()
            save_history("decision", f"업로드 결정 ({len(req.videos)}개 영상)", report)
            yield sse({"step": "done", "report": report})
        except Exception as e:
            yield sse({"step": "error", "message": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sns-convert")
async def sns_convert(req: SnsConvertRequest):
    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": "ANTHROPIC_API_KEY가 설정되지 않았습니다."})
            return
        if not req.keyword.strip() or not req.script.strip():
            yield sse({"step": "error", "message": "키워드와 원본 내용을 입력해주세요."})
            return
        try:
            yield sse({"step": "converting", "message": "블로그·스레드·숏폼 스크립트 생성 중... (30초~1분 소요)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.analyze_sns_convert(req.keyword, req.script))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
            save_history("sns", req.keyword, report)
            yield sse({"step": "done", "report": report, "keyword": req.keyword})
        except Exception as e:
            yield sse({"step": "error", "message": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/blog")
async def blog(req: BlogRequest):
    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return
        if not req.keyword.strip():
            yield sse({"step": "error", "message": "키워드/제목을 입력해주세요."})
            return

        try:
            photo_count = len(req.photos)
            if photo_count:
                yield sse({"step": "analyzing_photos", "message": f"사진 {photo_count}장 분석 중..."})
            yield sse({"step": "writing", "message": "블로그 원고 작성 중... (1~2분 소요)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(
                analyzer.analyze_blog(req.keyword, req.memo, req.photos, req.region, req.link)
            )
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
            save_history("blog", req.keyword, report)
            yield sse({"step": "done", "result": report, "keyword": req.keyword})

        except Exception as e:
            yield sse({"step": "error", "message": str(e)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/video-feedback")
async def video_feedback(file: UploadFile = File(...)):
    # 파일을 청크 단위로 임시 파일에 저장 (대용량 파일 메모리 문제 방지)
    uid = uuid.uuid4().hex
    video_path = f"/tmp/vf_{uid}.mp4"
    audio_path = f"/tmp/vf_{uid}.mp3"
    filename = file.filename or "영상 피드백"

    with open(video_path, "wb") as f_out:
        while True:
            chunk = await file.read(1024 * 1024)  # 1MB씩 읽기
            if not chunk:
                break
            f_out.write(chunk)

    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env 파일에 ANTHROPIC_API_KEY를 설정해주세요."})
            return

        try:
            # 1. 파일 저장 완료 알림
            yield sse({"step": "uploading", "message": "영상 파일 저장 완료, 파일 검사 중..."})
            await asyncio.sleep(0)

            # 1b. 파일 유효성 빠른 검사 (ffprobe ~1초)
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                    capture_output=True, text=True, timeout=15
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError("파일 검사 시간이 초과되었습니다. 파일이 손상되었을 수 있습니다.")
            if probe.returncode != 0:
                raise RuntimeError(
                    "유효하지 않은 파일입니다. mp4, mov, avi 등 동영상 파일을 업로드해주세요.\n"
                    f"상세: {probe.stderr.strip()[-200:]}"
                )

            loop = asyncio.get_event_loop()

            # 2. 오디오 추출 (executor로 비동기 처리 — 긴 영상도 ping 유지)
            yield sse({"step": "extracting", "message": "오디오 추출 중..."})

            def _run_ffmpeg():
                return subprocess.run(
                    ["ffmpeg", "-i", video_path, "-vn", "-acodec", "mp3", "-q:a", "2", audio_path, "-y"],
                    capture_output=True, text=True, timeout=300
                )

            ffmpeg_future = loop.run_in_executor(None, _run_ffmpeg)
            while not ffmpeg_future.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(5)
            try:
                result = await ffmpeg_future
            except subprocess.TimeoutExpired:
                raise RuntimeError("오디오 추출 시간이 초과되었습니다 (5분). 파일이 너무 크거나 손상되었을 수 있습니다.")
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg 오류: {result.stderr[-500:]}")

            # 3. Whisper로 자막 추출
            yield sse({"step": "transcribing", "message": "자막 추출 중... (영상 길이에 따라 2~5분 소요)"})

            def run_whisper():
                import whisper
                model = whisper.load_model("small")
                result_w = model.transcribe(audio_path, language="ko")
                return result_w["text"]

            try:
                transcript = await asyncio.wait_for(
                    loop.run_in_executor(None, run_whisper),
                    timeout=600  # 10분
                )
            except asyncio.TimeoutError:
                raise RuntimeError("자막 추출 시간이 초과되었습니다 (10분). 영상이 너무 길거나 손상되었을 수 있습니다.")

            # 4. Claude AI 분석
            yield sse({"step": "analyzing", "message": "AI 피드백 분석 중..."})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.analyze_video_feedback(transcript))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            feedback = _task.result()

            save_history("video_feedback", filename, {"transcript": transcript, "feedback": feedback})
            yield sse({"step": "done", "transcript": transcript, "feedback": feedback})

        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            for path in [video_path, audio_path]:
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except Exception:
                    pass

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat")
async def chat(req: ChatRequest):
    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"error": "ANTHROPIC_API_KEY가 설정되지 않았습니다."})
            return
        if not req.message.strip():
            yield sse({"error": "메시지를 입력해주세요."})
            return
        try:
            analyzer = Analyzer()
            async for token in analyzer.chat_stream(req.message, req.history, req.attachments):
                yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/history")
async def history_list(type: str = ""):
    return list_history(type)


@app.get("/api/history/{id}")
async def history_get(id: int):
    item = get_history(id)
    if not item:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="없음")
    return item


@app.delete("/api/history/{id}")
async def history_delete(id: int):
    delete_history(id)
    return {"ok": True}


# ── 콘텐츠 파이프라인 API ──────────────────────────────────────────────

class PipelineItem(BaseModel):
    title: str
    stage: str = "filming"
    content_type: str = "미드폼"
    editor: str = ""
    planned_date: str = ""
    notes: str = ""


@app.get("/api/pipeline")
async def pipeline_list():
    return list_pipeline()


@app.post("/api/pipeline")
async def pipeline_create(item: PipelineItem):
    id_ = create_pipeline_item(
        item.title, item.stage, item.content_type,
        item.editor, item.planned_date, item.notes
    )
    return {"id": id_}


@app.put("/api/pipeline/{id}")
async def pipeline_update(id: int, item: dict):
    update_pipeline_item(id, item)
    return {"ok": True}


@app.delete("/api/pipeline/{id}")
async def pipeline_delete(id: int):
    delete_pipeline_item(id)
    return {"ok": True}


# ── 기존 영상 최적화 체크리스트 ──────────────────────────────────────
from database import list_optimize, create_optimize, update_optimize, delete_optimize

@app.get("/api/optimize")
async def optimize_list():
    return list_optimize()

@app.post("/api/optimize")
async def optimize_create(request: Request):
    data = await request.json()
    id_ = create_optimize(data.get("title", ""), data.get("notes", ""))
    return {"id": id_}

@app.put("/api/optimize/{id}")
async def optimize_update(id: int, request: Request):
    data = await request.json()
    update_optimize(id, data)
    return {"ok": True}

@app.delete("/api/optimize/{id}")
async def optimize_delete(id: int):
    delete_optimize(id)
    return {"ok": True}


# ── 기획 워크시트 (스프레드시트형 작업공간) ──────────────────────────
from database import (list_worksheet, create_worksheet_row,
                      update_worksheet_row, delete_worksheet_row)

@app.get("/api/worksheet")
async def worksheet_list():
    return list_worksheet()

@app.post("/api/worksheet")
async def worksheet_create(request: Request):
    data = await request.json()
    id_ = create_worksheet_row(json.dumps(data.get("data", {}), ensure_ascii=False))
    return {"id": id_}

@app.put("/api/worksheet/{id}")
async def worksheet_update(id: int, request: Request):
    data = await request.json()
    update_worksheet_row(id, json.dumps(data.get("data", {}), ensure_ascii=False))
    return {"ok": True}

@app.delete("/api/worksheet/{id}")
async def worksheet_delete(id: int):
    delete_worksheet_row(id)
    return {"ok": True}


@app.get("/api/transcript-debug")
async def transcript_debug(request: Request):
    """쿠키/스크립트 수집 진단. ?url=<영상url> 주면 그 영상으로 실제 수집 시도."""
    import transcript_service as ts
    resolved = ts._cookiefile()
    info = {
        "code_version": "cookie-v6",
        "YT_COOKIES_FILE_env": os.getenv("YT_COOKIES_FILE", ""),
        "YT_COOKIES_B64_set": bool(os.getenv("YT_COOKIES_B64", "").strip()),
        "cookiefile_resolved": resolved,
        "cookiefile_exists": bool(resolved and os.path.exists(resolved)),
    }
    try:
        if resolved and os.path.exists(resolved):
            with open(resolved) as f:
                lines = f.readlines()
            info["cookiefile_lines"] = len(lines)
            info["has_login_cookies"] = any("__Secure-1PSID" in l or "LOGIN_INFO" in l for l in lines)
    except Exception as e:
        info["read_error"] = str(e)

    url = request.query_params.get("url", "").strip()
    if url:
        import yt_dlp
        clients_param = request.query_params.get("clients", "").strip()
        diag = {"clients": clients_param or "default"}
        try:
            if clients_param:
                opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
                        "skip_download": True, "ignore_no_formats_error": True,
                        "extractor_args": {"youtube": {"player_client": clients_param.split(",")}}}
                cf = ts._cookiefile()
                if cf:
                    opts["cookiefile"] = cf
            else:
                opts = ts._base_opts(skip_download=True)
            with yt_dlp.YoutubeDL(opts) as ydl:
                meta = ydl.extract_info(url, download=False)
            diag["extract_ok"] = True
            diag["title"] = (meta.get("title") or "")[:50]
            diag["fmt_count"] = len(meta.get("formats") or [])
            ac = sorted(list((meta.get("automatic_captions") or {}).keys()))
            diag["auto_caption_total"] = len(ac)
            diag["ko_caption"] = [l for l in ac if l.startswith("ko")]
            ko = (meta.get("automatic_captions") or {}).get("ko") or (meta.get("subtitles") or {}).get("ko") or []
            diag["ko_track_exts"] = [t.get("ext") for t in ko]
        except Exception as e:
            diag["extract_ok"] = False
            diag["extract_error"] = str(e)[:200]
        if not clients_param:
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, ts.fetch_transcript, url, False)
            diag["fetch_method"] = res.get("method")
            diag["fetch_error"] = res.get("error")
            diag["fetch_len"] = len(res.get("text", ""))
            diag["text_head"] = res.get("text", "")[:150]
        info["url_test"] = diag
    return info


@app.post("/api/worksheet/autofill")
async def worksheet_autofill(request: Request):
    """키워드 → 경쟁영상·댓글·카페·스크립트 수집 후 Opus 4.8가 워크시트 카드 자동 작성."""
    from transcript_service import fetch_transcript
    body = await request.json()
    keyword = (body.get("keyword") or "").strip()
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env에 ANTHROPIC_API_KEY를 설정해주세요."})
            return
        if not keyword:
            yield sse({"step": "error", "message": "키워드를 입력해주세요."})
            return

        videos_with_comments = []
        naver_results = []
        transcripts = []
        yt = YouTubeService(youtube_key) if youtube_key else None
        try:
            if yt:
                yield sse({"step": "searching", "message": f'"{keyword}" 경쟁영상(조회수순) 수집 중...'})
                videos = await yt.search_videos(keyword, max_results=12)
                if videos:
                    yield sse({"step": "found", "message": f"상위 {len(videos)}개 영상 발견! 인기댓글 수집 중..."})
                    videos_with_comments = await yt.get_comments_for_videos(videos[:6])

            if naver_id and naver_secret:
                yield sse({"step": "naver", "message": "네이버 카페 반응 수집 중..."})
                naver = NaverService(naver_id, naver_secret)
                naver_results = await naver.search_cafe(keyword)
                await naver.close()

            viewtrap_refs = None
            vt_token = os.getenv("VIEWTRAP_TOKEN", "").strip()
            if vt_token:
                yield sse({"step": "viewtrap", "message": "ViewTrap 성과영상·핫비디오 레퍼런스 수집 중..."})
                try:
                    svc = ViewTrapService(vt_token)
                    vt_top, vt_hot = await asyncio.gather(svc.get_top_videos(), svc.get_hot_videos())
                    viewtrap_refs = {"top_videos": vt_top[:15], "hot_videos": vt_hot[:15]}
                    yield sse({"step": "viewtrap_done",
                               "message": f"ViewTrap 성과 {len(vt_top)}개·핫비디오 {len(vt_hot)}개 수집 완료!"})
                except Exception:
                    yield sse({"step": "warn", "message": "ViewTrap 수집 실패(토큰 만료 가능) — 나머지 데이터로 진행."})

            # 상위 2개 영상 스크립트 (자막 우선 → 없으면 Whisper)
            top = (videos_with_comments or [])[:2]
            loop = asyncio.get_event_loop()
            for i, v in enumerate(top, 1):
                yield sse({"step": "transcribing",
                           "message": f"경쟁영상 {i}/{len(top)} 스크립트 추출 중... (자막 우선, 없으면 받아쓰기 2~5분)"})
                fut = loop.run_in_executor(None, fetch_transcript, v["url"])
                while not fut.done():
                    yield sse({"step": "ping"})
                    await asyncio.sleep(5)
                res = fut.result()
                res["title"] = v["title"]
                transcripts.append(res)
                if res.get("error") == "youtube_gate":
                    yield sse({"step": "warn",
                               "message": "⚠️ 유튜브 봇 차단으로 일부 스크립트 수집 실패(서버 IP 제한). 자막 있는 영상은 정상."})
            ok = sum(1 for t in transcripts if t.get("text"))
            yield sse({"step": "transcribed", "message": f"스크립트 {ok}/{len(top)}개 확보"})

            yield sse({"step": "writing", "message": "Opus 4.8가 워크시트 작성 중... (30~60초)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.autofill_worksheet(
                keyword, videos_with_comments or None, naver_results or None,
                transcripts or None, viewtrap_refs))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            data = _task.result()
            if "keyword" not in data:
                data["keyword"] = keyword
            row_id = create_worksheet_row(json.dumps(data, ensure_ascii=False))
            yield sse({"step": "done", "id": row_id, "data": data, "keyword": keyword})
        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            if yt:
                await yt.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/")
async def root():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate"})

app.mount("/", StaticFiles(directory="static", html=True), name="static")
