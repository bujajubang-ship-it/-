import asyncio
import json
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

from analyzer import Analyzer
from naver_service import NaverService
from youtube_service import YouTubeService
from analytics_service import AnalyticsService
from database import init_db, save_history, list_history, get_history, delete_history

init_db()

app = FastAPI(title="YouTube Content Researcher")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class AnalyzeRequest(BaseModel):
    keyword: str


class EditFeedbackRequest(BaseModel):
    keyword: str
    script: str


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


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/api/health")
async def health():
    return {
        "youtube": bool(os.getenv("YOUTUBE_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "naver": bool(os.getenv("NAVER_CLIENT_ID")),
        "analytics": bool(os.getenv("OAUTH_REFRESH_TOKEN")),
        "my_channel_id": os.getenv("MY_CHANNEL_ID", ""),
    }


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

            naver_results = []
            if naver_id and naver_secret:
                yield sse({"step": "naver", "message": "네이버 카페 반응 수집 중..."})
                naver = NaverService(naver_id, naver_secret)
                naver_results = await naver.search_cafe(req.keyword)
                await naver.close()
                yield sse({"step": "naver_done", "message": f"네이버 카페 {len(naver_results)}개 게시글 수집 완료!"})

            yield sse({"step": "analyzing", "message": "AI가 대본 분석 중... (보통 20~40초 소요)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.analyze_edit_feedback(req.keyword, req.script, videos_with_comments, naver_results))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()

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

            naver_results = []
            if naver_id and naver_secret:
                yield sse({"step": "naver", "message": "네이버 카페 반응 수집 중..."})
                naver = NaverService(naver_id, naver_secret)
                naver_results = await naver.search_cafe(req.keyword)
                await naver.close()
                yield sse({"step": "naver_done", "message": f"네이버 카페 {len(naver_results)}개 게시글 수집 완료!"})

            yield sse({"step": "analyzing", "message": "AI가 전체 영상 기획 작성 중... (1~2분 소요)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.analyze_midform(req.keyword, req.product_desc, videos_with_comments, naver_results))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
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


app.mount("/", StaticFiles(directory="static", html=True), name="static")
