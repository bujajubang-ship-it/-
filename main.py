import asyncio
import datetime
import json
import os
import re
import secrets
import time
import uuid
import subprocess
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

load_dotenv()

from analyzer import Analyzer
from naver_service import NaverService
from youtube_service import YouTubeService
from analytics_service import AnalyticsService
from analytics_repository import AnalyticsRepository, init_analytics_schema
from analytics_sync import AnalyticsSyncCoordinator
from channel_analysis import analyze_channel_with_fallback, fetch_retention_sample
from collection_service import YouTubeCollectionScheduler, YouTubeCollectionService
from strategy_brain.chat_service import StrategyChatService
from strategy_repository import (
    StrategyRepository,
    capture_legacy_strategy,
    init_strategy_schema,
)
from strategy_context import generate_strategy_context
from strategy_memory import init_strategy_memory_schema, remember_interaction
from edit_project_store import EditProjectStore, public_project, transition_project, utc_now
from media_ingest import MediaIngestService, MediaValidationError, StorageCapacityError
from edit_analysis_service import EditAnalysisService
from edit_plan_service import prepare_plan, plan_diff
from conservative_rough_cut import (
    ROUGH_CUT_MODE, apply_script_choices, initialize_script_editor,
)
from edit_render_service import EditRenderService
from edit_storage import EditStorageService, object_storage_configured, object_storage_from_env
from edit_job_queue import EditJobQueue, EditJobWorker
from edit_pipeline import EditPipeline
from multisource_roughcut import (
    apply_story_reasoning, bounded_story_candidates, ensure_multisource, find_source, new_source,
)
from edit_render_contract import build_final_render_payload, requires_external_final, validate_final_payload
from edit_learning_service import (
    EditFeedbackService,
    record_approved_edit_memory,
)
from viewtrap_service import ViewTrapService
from worksheet_ai_service import WorksheetAIService
from video_feedback_jobs import VideoFeedbackJobManager
from plain_transcript_edit import render_csv as render_transcript_edit_csv
from plain_transcript_edit import render_markdown as render_transcript_edit_markdown
from plain_transcript_edit import render_vrew_prompt as render_transcript_edit_vrew
from plain_transcript_edit_jobs import HISTORY_TYPE as TRANSCRIPT_EDIT_GUIDE_HISTORY_TYPE
from plain_transcript_edit_jobs import PROJECT_MODE as TRANSCRIPT_EDIT_GUIDE_MODE
from plain_transcript_edit_jobs import PlainTranscriptEditJobManager
from heatmap_service import fetch_heatmap, summarize_for_prompt
from owner_auth import OwnerAuthenticator, OwnerAuthMiddleware, OwnerAuthSettings
import guest_mode
from plan_feedback import PlanFeedbackService, validate_feedback
from database import (init_db, save_history, list_history, get_history, delete_history,
                       init_pipeline, list_pipeline, create_pipeline_item,
                       update_pipeline_item, delete_pipeline_item)

init_db()
init_pipeline()
init_analytics_schema()
init_strategy_schema()
init_strategy_memory_schema()

# ===== 영상 피드백 legacy 보조 사본 =====
# 운영 source of truth는 Render Starter의 /data Persistent Disk에 있는
# /data/history.db다. Lightsail KV에는 신규 피드백의 보조 사본만 남기며,
# 의도적으로 삭제한 기록이 되살아나지 않도록 앱 시작 시 자동복원하지 않는다.
_KV_BASE = os.environ.get("CNMAKER_BASE", "").rstrip("/")
_KV_SECRET = os.environ.get("CNMAKER_SECRET", "")
_VF_KEY = "yt_video_feedback"


def _vf_rows():
    """저장된 영상 피드백 기록 전부 (백업용)."""
    out = []
    for row in list_history("video_feedback", limit=500):
        full = get_history(row["id"])
        if full:
            out.append({"keyword": full.get("keyword", ""), "report": full.get("report"),
                        "created_at": str(full.get("created_at", ""))})
    return out


def _vf_backup():
    if not _KV_BASE or not _KV_SECRET:
        return
    try:
        httpx.post(f"{_KV_BASE}/kv/{_VF_KEY}", json={"rows": _vf_rows()},
                   headers={"x-secret": _KV_SECRET}, timeout=8)
    except Exception:
        pass


COLLECTION_SCHEDULER = YouTubeCollectionScheduler()
EDIT_RENDERING: set[int] = set()
EDIT_RENDERING_LOCK = threading.Lock()
EDIT_RENDER_TASKS: dict[int, asyncio.Task] = {}
EDIT_JOB_QUEUE = EditJobQueue()
EDIT_PIPELINE = EditPipeline()
VIDEO_FEEDBACK_JOBS = VideoFeedbackJobManager()
PLAIN_TRANSCRIPT_EDIT_JOBS = PlainTranscriptEditJobManager()
EDIT_JOB_WORKER = EditJobWorker(
    EDIT_JOB_QUEUE,
    {
        "analysis": EDIT_PIPELINE.analysis,
        "rendering": EDIT_PIPELINE.rendering,
        "preview_rendering": EDIT_PIPELINE.preview_rendering,
        "performance_sync": EDIT_PIPELINE.performance_sync,
        "source_analysis": EDIT_PIPELINE.source_analysis,
        "story_planning": EDIT_PIPELINE.story_planning,
        "rough_cut_rendering": EDIT_PIPELINE.rough_cut_rendering,
    },
    # final_rendering is intentionally absent: the API service must never
    # claim a source-resolution long-running final encode.
    allowed_types={
        "analysis", "rendering", "preview_rendering", "performance_sync",
        "source_analysis", "story_planning", "rough_cut_rendering",
    },
)


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    COLLECTION_SCHEDULER.start()
    EDIT_JOB_WORKER.start()
    VIDEO_FEEDBACK_JOBS.start()
    PLAIN_TRANSCRIPT_EDIT_JOBS.start()
    cleanup_task = asyncio.create_task(_edit_storage_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        render_tasks = list(EDIT_RENDER_TASKS.values())
        for task in render_tasks:
            task.cancel()
        if render_tasks:
            await asyncio.gather(*render_tasks, return_exceptions=True)
        await EDIT_JOB_WORKER.stop()
        await VIDEO_FEEDBACK_JOBS.stop()
        await PLAIN_TRANSCRIPT_EDIT_JOBS.stop()
        await COLLECTION_SCHEDULER.stop()


app = FastAPI(title="YouTube Content Researcher", lifespan=app_lifespan)
OWNER_AUTH = OwnerAuthenticator(OwnerAuthSettings.from_env())
app.add_middleware(OwnerAuthMiddleware, authenticator=OWNER_AUTH)


def current_role(request: Request) -> str:
    """지금 요청을 보낸 사람이 사장님인지 친구인지."""
    if guest_mode.is_guest():
        return "guest"                       # 사이트 자체가 친구용으로 떠 있는 경우
    session = OWNER_AUTH.request_session(request)
    return str((session or {}).get("role") or "owner")


@app.middleware("http")
async def block_hidden_features(request: Request, call_next):
    """친구 사이트에서는 열어 준 기능 말고는 서버가 막는다.

    화면에서 탭을 숨기는 것만으로는 주소를 직접 치면 그대로 열린다.
    """
    if current_role(request) == "guest" and not guest_mode.path_allowed(request.url.path):
        return JSONResponse({"error": guest_mode.hidden_notice()}, status_code=404)
    return await call_next(request)


@app.get("/api/site-mode")
async def site_mode(request: Request):
    return {"guest": current_role(request) == "guest", "tabs": list(guest_mode.GUEST_TABS)}


PLAN_FEEDBACK = PlanFeedbackService()


class PlanFeedbackRequest(BaseModel):
    keyword: str = ""
    plan: str


@app.post("/api/plan-feedback")
async def plan_feedback(req: PlanFeedbackRequest):
    """촬영 전 기획안을 검사한다. 사장님 채널 수치는 쓰지 않는다."""
    plan = (req.plan or "").strip()
    if len(plan) < 20:
        return JSONResponse({"error": "기획 내용을 조금 더 자세히 적어 주세요."}, status_code=400)
    try:
        principles = _general_principles(req.keyword or plan[:40])
    except Exception:
        principles = []
    try:
        result = await PLAN_FEEDBACK.review(
            keyword=req.keyword or "", plan_text=plan, principles=principles,
        )
        return validate_feedback(result)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=422)
    except Exception as exc:
        return JSONResponse({"error": f"기획 피드백을 만들지 못했습니다: {exc}"}, status_code=502)


def _general_principles(query: str) -> list[dict[str, Any]]:
    """기획을 판단할 때 근거로 쓸 지식을 가져온다.

    사장님이 쌓아 둔 지식(Low Data 판단 규칙, 영상 구성 원칙 등)을 그대로 쓴다.
    친구는 지식 탭 자체를 못 열지만, 조언의 근거로는 이 지식이 쓰인다.
    """
    from strategy_brain.retrieval import StrategyRetrieval
    envelope = StrategyRetrieval().search_knowledge({"query": query, "limit": 8})
    rows = getattr(envelope, "data", None)
    if isinstance(rows, dict):          # 담는 모양이 바뀌어도 견디게 둘 다 받는다
        rows = rows.get("items")
    return list(rows or [])


@app.exception_handler(StarletteHTTPException)
async def api_http_error(request: Request, exc: StarletteHTTPException):
    """Keep every application-generated API error machine-readable."""

    if request.url.path.startswith("/api/"):
        detail = exc.detail if isinstance(exc.detail, str) else "요청을 처리하지 못했습니다."
        return JSONResponse(
            {"error": detail},
            status_code=exc.status_code,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def api_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": "요청 형식이 올바르지 않습니다."},
            status_code=422,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    return JSONResponse({"detail": exc.errors()}, status_code=422)


@app.exception_handler(Exception)
async def api_unhandled_error(request: Request, exc: Exception):
    if request.url.path.startswith("/api/"):
        print(
            f"[api-error] path={request.url.path} type={type(exc).__name__}",
            flush=True,
        )
        return JSONResponse(
            {"error": "서버에서 요청을 처리하지 못했습니다. 잠시 후 다시 시도해주세요."},
            status_code=500,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
    return PlainTextResponse("Internal Server Error", status_code=500)

if OWNER_AUTH.settings.production and not os.getenv("PIPELINE_REMIND_SECRET", "").strip():
    raise RuntimeError("PIPELINE_REMIND_SECRET is required in production.")

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


class AnalyzeRequest(BaseModel):
    keyword: str


class EditFeedbackRequest(BaseModel):
    keyword: str
    script: str
    product_url: str = ""


def edit_feedback_project_report(
    report: dict[str, Any], request: EditFeedbackRequest, *, account: str = "owner",
) -> dict[str, Any]:
    """Add reopenable project inputs without changing the legacy report shape."""

    payload = dict(report or {})
    payload["_project"] = {
        "schema_version": 1,
        "name": request.keyword.strip(),
        "script": request.script,
        "product_url": request.product_url.strip(),
        # 누가 만든 것인지 남긴다. 친구는 자기 것만 보고, 사장님은 다 본다.
        "account": account,
    }
    return payload


class PlainTranscriptEditRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    topic: str = Field(min_length=1, max_length=300)
    script: str = Field(min_length=10, max_length=300_000)
    target_duration_seconds: float = Field(default=0, ge=0, le=21600)
    purpose: str = Field(default="", max_length=500)
    additional_request: str = Field(default="", max_length=4000)


class PlainTranscriptEditRevisionRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


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
    history: list = Field(default_factory=list)
    attachments: list = Field(default_factory=list)  # List[AttachmentItem]
    session_id: int | None = None


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


class OwnerLoginRequest(BaseModel):
    username: str
    password: str


class StrategyGenerateRequest(BaseModel):
    prompt: str
    content_type: str = "미드폼"
    existing_strategy_id: int | None = None


class StrategyCreateRequest(BaseModel):
    topic: str
    content_type: str = "미드폼"
    strategy: dict
    evidence: list = Field(default_factory=list)
    status: str = "draft"
    source_history_id: int | None = None
    pipeline_id: int | None = None
    worksheet_id: int | None = None


class StrategyVideoLinkRequest(BaseModel):
    video_id: str
    title_at_upload: str = ""
    thumbnail_text: str = ""


class EditPlanRevisionRequest(BaseModel):
    message: str


class EditPlanApprovalRequest(BaseModel):
    version: int | None = None


class EditScriptToggleRequest(BaseModel):
    segment_id: str = Field(min_length=1, max_length=160)
    deleted: bool


class EditProjectUploadLinkRequest(BaseModel):
    video_id: str


def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.get("/healthz")
async def healthz():
    """Public liveness check without configuration or channel details."""
    return JSONResponse(
        {"ok": True},
        headers={"X-App-Revision": os.getenv("RENDER_GIT_COMMIT", "local")[:12]},
    )


@app.get("/login")
async def owner_login_page(request: Request):
    if OWNER_AUTH.settings.enabled and OWNER_AUTH.request_session(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse("static/login.html", headers={"Cache-Control": "no-store"})


@app.post("/api/auth/login")
async def owner_login(request: Request, credentials: OwnerLoginRequest):
    if not OWNER_AUTH.settings.enabled:
        return JSONResponse(
            {"error": "Owner authentication is disabled."}, status_code=404
        )

    client_key = request.client.host if request.client else "unknown"
    retry_after = OWNER_AUTH.rate_limiter.retry_after(client_key)
    if retry_after:
        return JSONResponse(
            {"error": "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해주세요."},
            status_code=429,
            headers={"Retry-After": str(retry_after), "Cache-Control": "no-store"},
        )

    role = OWNER_AUTH.role_for(credentials.username, credentials.password)
    if role is None:
        OWNER_AUTH.rate_limiter.record_failure(client_key)
        retry_after = OWNER_AUTH.rate_limiter.retry_after(client_key)
        headers = {"Cache-Control": "no-store"}
        if retry_after:
            headers["Retry-After"] = str(retry_after)
        return JSONResponse(
            {"error": "아이디 또는 비밀번호가 맞지 않습니다."},
            status_code=401,
            headers=headers,
        )

    OWNER_AUTH.rate_limiter.clear(client_key)
    response = JSONResponse(
        {"ok": True}, headers={"Cache-Control": "no-store"}
    )
    response.set_cookie(
        OWNER_AUTH.settings.cookie_name,
        OWNER_AUTH.issue_session(role=role),
        max_age=OWNER_AUTH.settings.session_ttl_seconds,
        httponly=True,
        secure=OWNER_AUTH.settings.secure_cookie,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def owner_logout():
    response = JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
    response.delete_cookie(
        OWNER_AUTH.settings.cookie_name,
        path="/",
        secure=OWNER_AUTH.settings.secure_cookie,
        httponly=True,
        samesite="strict",
    )
    return response


@app.get("/api/auth/me")
async def owner_me(request: Request):
    return {"username": request.state.owner}


@app.get("/api/health")
async def health():
    return {
        "youtube": bool(os.getenv("YOUTUBE_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "strategy_provider": os.getenv("STRATEGY_BRAIN_PROVIDER", "openai"),
        "naver": bool(os.getenv("NAVER_CLIENT_ID")),
        "analytics": bool(os.getenv("OAUTH_REFRESH_TOKEN")),
        "viewtrap": bool(os.getenv("VIEWTRAP_TOKEN")),
        "my_channel_id": os.getenv("MY_CHANNEL_ID", ""),
        "collection": AnalyticsRepository().get_collection_status(),
        "revision": os.getenv("RENDER_GIT_COMMIT", "local")[:12],
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


@app.get("/api/analytics/status")
async def analytics_collection_status():
    status = AnalyticsRepository().get_collection_status()
    data_through = status.get("data_through")
    lag_days = None
    if data_through:
        try:
            lag_days = (datetime.date.today() - datetime.date.fromisoformat(data_through[:10])).days
        except ValueError:
            pass
    status["data_lag_days"] = lag_days
    status["is_stale"] = lag_days is None or lag_days > 7
    status["scheduler_enabled"] = COLLECTION_SCHEDULER.enabled
    return status


@app.get("/api/analytics/reporting/status")
async def analytics_reporting_status():
    return AnalyticsRepository().get_reporting_status()


@app.post("/api/analytics/refresh")
async def analytics_manual_refresh():
    async def stream():
        yield sse({"step": "starting", "message": "YouTube 전체 성과 snapshot 수집을 시작합니다."})
        try:
            result = await YouTubeCollectionService().run_once(trigger="manual")
            yield sse({"step": "done", **result.public_dict()})
        except Exception as exc:
            yield sse({"step": "error", "message": str(exc)[:300]})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/strategies/generate")
async def strategy_generate(req: StrategyGenerateRequest):
    if not req.prompt.strip():
        return JSONResponse({"error": "기획 요청을 입력해주세요."}, status_code=400)
    repository = StrategyRepository()
    existing = None
    if req.existing_strategy_id is not None:
        existing_row = repository.get(req.existing_strategy_id)
        if not existing_row:
            return JSONResponse({"error": "기존 전략을 찾지 못했습니다."}, status_code=404)
        existing = existing_row.get("strategy")
    try:
        strategy = await generate_strategy_context(
            req.prompt, content_type=req.content_type, existing=existing
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:300]}, status_code=503)
    evidence = strategy.get("evidence") or []
    if req.existing_strategy_id is not None:
        repository.update(
            req.existing_strategy_id,
            {"topic": strategy.get("topic") or req.prompt, "strategy": strategy, "evidence": evidence},
        )
        strategy_id = req.existing_strategy_id
    else:
        strategy_id = repository.create(
            topic=strategy.get("topic") or req.prompt,
            content_type=req.content_type,
            strategy=strategy,
            evidence=evidence,
        )
    return {"id": strategy_id, "strategy": strategy}


@app.get("/api/strategies")
async def strategies_list(q: str = "", limit: int = 50):
    return StrategyRepository().list(limit=max(1, min(limit, 100)), query=q)


@app.post("/api/strategies")
async def strategies_create(req: StrategyCreateRequest):
    strategy_id = StrategyRepository().create(
        topic=req.topic,
        content_type=req.content_type,
        strategy=req.strategy,
        evidence=req.evidence,
        status=req.status,
        source_history_id=req.source_history_id,
        pipeline_id=req.pipeline_id,
        worksheet_id=req.worksheet_id,
    )
    return {"id": strategy_id}


@app.get("/api/strategies/{strategy_id}")
async def strategies_get(strategy_id: int):
    item = StrategyRepository().get(strategy_id)
    if not item:
        return JSONResponse({"error": "전략을 찾지 못했습니다."}, status_code=404)
    return item


@app.put("/api/strategies/{strategy_id}")
async def strategies_update(strategy_id: int, request: Request):
    fields = await request.json()
    if not StrategyRepository().update(strategy_id, fields):
        return JSONResponse({"error": "수정할 전략을 찾지 못했거나 필드가 없습니다."}, status_code=404)
    return {"ok": True}


@app.post("/api/strategies/{strategy_id}/link-video")
async def strategy_link_video(strategy_id: int, req: StrategyVideoLinkRequest):
    repository = StrategyRepository()
    if not repository.get(strategy_id):
        return JSONResponse({"error": "전략을 찾지 못했습니다."}, status_code=404)
    if not req.video_id.strip():
        return JSONResponse({"error": "YouTube video ID를 입력해주세요."}, status_code=400)
    repository.link_video(
        strategy_id,
        req.video_id.strip(),
        title_at_upload=req.title_at_upload,
        thumbnail_text=req.thumbnail_text,
    )
    repository.refresh_performance_checkpoints()
    return {"ok": True}


@app.post("/api/strategies/{strategy_id}/activate")
async def strategy_activate(strategy_id: int):
    try:
        links = StrategyRepository().activate(strategy_id)
    except KeyError:
        return JSONResponse({"error": "전략을 찾지 못했습니다."}, status_code=404)
    return {"ok": True, **links}


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    account = current_role(request)

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
async def edit_feedback(req: EditFeedbackRequest, request: Request):
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

            # Keep the original project inputs with the feedback so reopening a
            # project restores both the result and the source context. Legacy
            # edit history rows remain readable because the report shape stays
            # top-level and this metadata is purely additive.
            report = edit_feedback_project_report(report, req, account=account)
            history_id = save_history("edit", req.keyword, report)
            yield sse({
                "step": "done", "report": report, "keyword": req.keyword,
                "history_id": history_id,
            })

        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            await yt.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/transcript-edit-guides/jobs")
async def create_plain_transcript_edit_job(req: PlainTranscriptEditRequest):
    job = PLAIN_TRANSCRIPT_EDIT_JOBS.enqueue_initial(req.model_dump())
    if PLAIN_TRANSCRIPT_EDIT_JOBS._worker_task is None:
        asyncio.create_task(PLAIN_TRANSCRIPT_EDIT_JOBS.process_once())
    return JSONResponse({
        "ok": True, "job_id": job["job_id"],
        "status_url": f"/api/transcript-edit-guides/jobs/{job['job_id']}",
    }, status_code=202)


@app.get("/api/transcript-edit-guides/jobs/{job_id}")
async def get_plain_transcript_edit_job(job_id: str):
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return JSONResponse({"error": "올바르지 않은 작업 ID입니다."}, status_code=400)
    job = PLAIN_TRANSCRIPT_EDIT_JOBS.store.get(job_id)
    if not job:
        return JSONResponse({"error": "분석 작업을 찾지 못했습니다."}, status_code=404)
    return job


@app.get("/api/edit-feedback/projects")
async def list_edit_feedback_projects(request: Request):
    role = current_role(request)
    projects = []
    for summary in list_history("edit", limit=200):
        row = get_history(int(summary["id"]))
        if not row:
            continue
        report = row.get("report") or {}
        metadata = report.get("_project") or {}
        if metadata.get("mode") in {"plain_transcript_flow", TRANSCRIPT_EDIT_GUIDE_MODE}:
            continue
        # 친구는 자기가 만든 것만 본다. 사장님은 친구 것까지 다 본다.
        # 계정이 생기기 전에 만들어진 것은 전부 사장님 것이다.
        if role == "guest" and metadata.get("account") != "guest":
            continue
        projects.append({
            "account": metadata.get("account") or "owner",
            "id": int(row["id"]), "type": "edit", "keyword": row.get("keyword") or "",
            "created_at": str(row.get("created_at") or ""),
            "mode": "legacy_edit_feedback",
            "title": metadata.get("title") or metadata.get("name") or row.get("keyword") or "",
            "topic": metadata.get("topic") or row.get("keyword") or "",
            "current_version": 0,
            "expected_duration_seconds": None,
            "last_revision_summary": metadata.get("last_revision_summary") or "",
        })
    return projects


@app.get("/api/transcript-edit-guides/projects")
async def list_transcript_edit_guide_projects():
    projects = []
    for summary in list_history(TRANSCRIPT_EDIT_GUIDE_HISTORY_TYPE, limit=200):
        row = get_history(int(summary["id"]))
        if not row:
            continue
        report = row.get("report") or {}
        metadata = report.get("_project") or {}
        current = report.get("current_result") or {}
        projects.append({
            "id": int(row["id"]), "type": TRANSCRIPT_EDIT_GUIDE_HISTORY_TYPE,
            "keyword": row.get("keyword") or "", "created_at": str(row.get("created_at") or ""),
            "title": metadata.get("title") or row.get("keyword") or "",
            "topic": metadata.get("topic") or "",
            "current_version": int(metadata.get("current_version") or 1),
            "expected_duration_seconds": current.get("recommended_duration_seconds"),
            "last_revision_summary": metadata.get("last_revision_summary") or "",
        })
    return projects


@app.post("/api/transcript-edit-guides/projects/{history_id}/revisions")
async def revise_plain_transcript_edit_project(
    history_id: int, req: PlainTranscriptEditRevisionRequest,
):
    row = get_history(history_id)
    if not row:
        return JSONResponse({"error": "편집 프로젝트를 찾지 못했습니다."}, status_code=404)
    if row.get("type") != TRANSCRIPT_EDIT_GUIDE_HISTORY_TYPE or ((row.get("report") or {}).get("_project") or {}).get("mode") != TRANSCRIPT_EDIT_GUIDE_MODE:
        return JSONResponse({"error": "자막 편집 가이드 프로젝트가 아닙니다."}, status_code=409)
    job = PLAIN_TRANSCRIPT_EDIT_JOBS.enqueue_revision(history_id, req.message.strip())
    if PLAIN_TRANSCRIPT_EDIT_JOBS._worker_task is None:
        asyncio.create_task(PLAIN_TRANSCRIPT_EDIT_JOBS.process_once())
    return JSONResponse({
        "ok": True, "job_id": job["job_id"],
        "status_url": f"/api/transcript-edit-guides/jobs/{job['job_id']}",
    }, status_code=202)


def _transcript_edit_guide_version(history_id: int, version: int | None = None):
    row = get_history(history_id)
    if not row:
        raise KeyError("자막 편집 가이드 프로젝트를 찾지 못했습니다.")
    project = row.get("report") or {}
    if row.get("type") != TRANSCRIPT_EDIT_GUIDE_HISTORY_TYPE or (project.get("_project") or {}).get("mode") != TRANSCRIPT_EDIT_GUIDE_MODE:
        raise ValueError("자막 편집 가이드 프로젝트가 아닙니다.")
    versions = project.get("versions") or []
    selected = next(
        (item for item in versions if int(item.get("version") or 0) == int(version or 0)),
        versions[-1] if versions and version is None else None,
    )
    if not selected:
        raise LookupError("편집안 버전을 찾지 못했습니다.")
    return project, selected


@app.get("/api/transcript-edit-guides/projects/{history_id}/download/{kind}")
async def download_plain_transcript_edit_project(
    history_id: int, kind: str, version: int | None = None,
):
    try:
        project, selected = _transcript_edit_guide_version(history_id, version)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except (ValueError, LookupError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    version_number = int(selected.get("version") or 0)
    if kind == "markdown":
        content, media_type, suffix = render_transcript_edit_markdown(project.get("_project") or {}, selected), "text/markdown; charset=utf-8", "md"
    elif kind in ("txt", "vrew"):
        # 편집자에게 넘기는 문서는 Vrew 에이전트 가이드 하나로 통일했다.
        content, media_type, suffix = render_transcript_edit_vrew(project, selected), "text/plain; charset=utf-8", "txt"
    elif kind == "csv":
        content, media_type, suffix = render_transcript_edit_csv(selected.get("result") or {}), "text/csv; charset=utf-8", "csv"
    else:
        return JSONResponse({"error": "markdown, txt, vrew 또는 csv만 다운로드할 수 있습니다."}, status_code=400)
    # Vrew 지시문은 사람이 읽는 가이드와 파일 이름이 겹치면 안 된다.
    filename_stem = f"transcript-edit-guide-v{version_number}"
    if kind == "vrew":
        filename_stem += "-vrew"
    return Response(
        content=content, media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename_stem}.{suffix}"'},
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
            history_id = save_history("planning", req.keyword, report)
            strategy_id = capture_legacy_strategy(
                "기획", req.keyword, report, source_history_id=history_id
            )
            yield sse({"step": "done", "report": report, "keyword": req.keyword, "strategy_id": strategy_id})
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
            kb = [k for k in list_knowledge(active_only=True) if k.get("category") != "바이럴"] or None
            _task = asyncio.create_task(analyzer.analyze_shortform(
                req.keyword, req.product_desc, req.duration,
                videos_with_comments or None, naver_results or None, kb
            ))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
            history_id = save_history("shortform", req.keyword, report)
            strategy_id = capture_legacy_strategy(
                "숏폼", req.keyword, report, source_history_id=history_id
            )
            yield sse({"step": "done", "report": report, "keyword": req.keyword, "strategy_id": strategy_id})
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

            yield sse({"step": "analyzing", "message": "AI가 전체 영상 기획 작성 중... (2~3분 소요 — 완성본이 길어서 조금 걸려요, 멈춘 거 아니에요!)"})
            analyzer = Analyzer()
            kb = [k for k in list_knowledge(active_only=True) if k.get("category") != "바이럴"] or None
            _task = asyncio.create_task(analyzer.analyze_midform(req.keyword, combined_desc, videos_with_comments, naver_results, viewtrap_refs, kb))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
            if viewtrap_refs:
                report["viewtrap_top"] = viewtrap_refs.get("top_videos", [])[:10]
                report["viewtrap_hot"] = viewtrap_refs.get("hot_videos", [])[:10]
            history_id = save_history("midform", req.keyword, report)
            strategy_id = capture_legacy_strategy(
                "미드폼", req.keyword, report, source_history_id=history_id
            )
            yield sse({"step": "done", "report": report, "keyword": req.keyword, "strategy_id": strategy_id})

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


@app.post("/api/yt-search")
async def yt_search(request: Request):
    """유튜브 검색 / 인기 급상승 → 부자주방 관점 AI 분석.

    mode=search  : 키워드로 검색 (기간·정렬·길이 조건)
    mode=trending: 지금 인기 급상승 (카테고리별)
    """
    d = await request.json()
    mode = (d.get("mode") or "search").strip()
    query = (d.get("query") or "").strip()
    days = int(d.get("days") or 0)
    order = (d.get("order") or "viewCount").strip()
    duration = (d.get("duration") or "any").strip()
    category = (d.get("category") or "").strip()
    want_ai = bool(d.get("ai", True))
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()

    async def stream():
        if not youtube_key:
            yield sse({"step": "error", "message": ".env 파일에 YOUTUBE_API_KEY를 설정해주세요."})
            return
        if mode == "search" and not query:
            yield sse({"step": "error", "message": "검색어를 입력해주세요."})
            return

        yt = YouTubeService(youtube_key)
        try:
            if mode == "trending":
                label = YouTubeService.CATEGORIES.get(category, "전체")
                yield sse({"step": "youtube", "message": f"인기 급상승({label}) 불러오는 중..."})
                videos = await yt.get_trending(category=category, max_results=30)
            else:
                span = f"최근 {days}일" if days else "전체 기간"
                yield sse({"step": "youtube", "message": f"'{query}' 검색 중 ({span})..."})
                videos = await yt.search_advanced(query, days=days, order=order,
                                                  duration=duration, max_results=30)

            if not videos:
                yield sse({"step": "error", "message": "결과가 없습니다. 조건을 넓혀보세요."})
                return
            yield sse({"step": "videos", "videos": videos,
                       "message": f"영상 {len(videos)}개 수집 완료"})

            if not want_ai:
                yield sse({"step": "done", "report": None, "videos": videos})
                return
            if not os.getenv("ANTHROPIC_API_KEY", "").strip():
                yield sse({"step": "done", "report": None, "videos": videos})
                return

            yield sse({"step": "analyzing", "message": "AI가 뭘 만들지 읽는 중... (30~60초)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.analyze_search(mode, query or "인기 급상승", videos))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
            title = f"검색: {query}" if mode == "search" else f"인기급상승: {YouTubeService.CATEGORIES.get(category, '전체')}"
            save_history("yt-search", title, {"videos": videos, "report": report})
            yield sse({"step": "done", "report": report, "videos": videos})

        except Exception as e:
            yield sse({"step": "error", "message": str(e)})
        finally:
            await yt.close()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/yt-categories")
async def yt_categories():
    """인기 급상승 카테고리 목록 (프론트 드롭다운용)."""
    return [{"id": "", "name": "전체"}] + [
        {"id": k, "name": v} for k, v in YouTubeService.CATEGORIES.items()
    ]


@app.post("/api/channel-analyze")
async def channel_analyze(req: ChannelAnalyzeRequest):
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()

    async def stream():
        request_id = uuid.uuid4().hex[:10]
        started_at = time.monotonic()
        stage = "validation"

        def elapsed() -> float:
            return round(time.monotonic() - started_at, 2)

        def log_stage(name: str, **fields):
            details = " ".join(f"{key}={value}" for key, value in fields.items())
            print(
                f"[channel-analyze:{request_id}] stage={name} elapsed={elapsed()}s {details}".rstrip(),
                flush=True,
            )

        if not youtube_key:
            yield sse({"step": "error", "message": "YouTube Data API 설정을 확인해주세요."})
            return
        if not (
            os.getenv("OPENAI_API_KEY", "").strip()
            or os.getenv("ANTHROPIC_API_KEY", "").strip()
        ):
            yield sse({"step": "error", "message": "AI 분석 API 설정을 확인해주세요."})
            return
        if not req.channel_id.strip():
            yield sse({"step": "error", "message": "채널 ID를 입력해주세요."})
            return

        yt = YouTubeService(youtube_key)
        analytics = AnalyticsService()
        analytics_repository = AnalyticsRepository()
        try:
            log_stage("started")
            stage = "youtube_data"
            yield sse({"step": "channel_info", "message": "채널 정보 불러오는 중..."})
            channel_info, videos = await asyncio.wait_for(
                yt.get_channel_videos(req.channel_id.strip(), max_videos=100),
                timeout=45,
            )
            log_stage("youtube_data_done", videos=len(videos))
            yield sse({"step": "videos_loaded", "message": f"영상 {len(videos)}개 데이터 수집 완료!"})

            analytics_data = []
            retention_data = []
            retention_failed = 0
            if analytics.is_configured():
                stage = "youtube_analytics"
                yield sse({
                    "step": "analytics",
                    "message": "Analytics와 retention 데이터를 병렬 수집 중...",
                })
                try:
                    analytics_channel_id = await asyncio.wait_for(
                        analytics.get_authenticated_channel_id(), timeout=30
                    )
                    if analytics_channel_id != channel_info.get("id"):
                        yield sse({
                            "step": "analytics_warn",
                            "message": "내 채널이 아닌 분석 대상에는 OAuth Analytics를 결합하지 않습니다.",
                        })
                    else:
                        coordinator = AnalyticsSyncCoordinator(analytics, analytics_repository)
                        period_end = datetime.date.today().isoformat()
                        metrics_task = asyncio.create_task(
                            coordinator.sync_video_snapshots(
                                videos,
                                period_start="2020-01-01",
                                period_end=period_end,
                            )
                        )
                        retention_task = asyncio.create_task(
                            fetch_retention_sample(
                                analytics,
                                videos,
                                period_start="2020-01-01",
                                period_end=period_end,
                            )
                        )
                        analytics_data, retention_result = await asyncio.wait_for(
                            asyncio.gather(metrics_task, retention_task), timeout=55
                        )
                        retention_data, retention_failed = retention_result
                    # video_id 기준으로 videos에 병합
                    analytics_map = {a["video_id"]: a for a in analytics_data}
                    for v in videos:
                        a = analytics_map.get(v["id"], {})
                        v["analytics_metric_statuses"] = {
                            name: metric.get("status")
                            for name, metric in (a.get("metrics") or {}).items()
                        }
                        v["avg_view_percentage"] = a.get("avg_view_percentage", None)
                        v["avg_view_percentage_status"] = a.get(
                            "avg_view_percentage_status", "unavailable"
                        )
                        v["watch_minutes"] = a.get("watch_minutes", None)
                        v["watch_minutes_status"] = a.get(
                            "watch_minutes_status", "unavailable"
                        )
                        v["shares"] = a.get("shares", None)
                        v["shares_status"] = a.get("shares_status", "unavailable")
                        v["subscribers_gained"] = a.get("subscribers_gained", None)
                        v["subscribers_gained_status"] = a.get(
                            "subscribers_gained_status", "unavailable"
                        )
                        v["subscribers_lost"] = a.get("subscribers_lost", None)
                        v["subscribers_lost_status"] = a.get(
                            "subscribers_lost_status", "unavailable"
                        )
                        v["analytics_data_through"] = a.get("data_through")
                        v["analytics_sample_size"] = a.get("sample_size", 0)
                    log_stage(
                        "youtube_analytics_done",
                        metrics=len(analytics_data),
                        retention=len(retention_data),
                        retention_failed=retention_failed,
                    )
                    yield sse({
                        "step": "analytics_done",
                        "message": (
                            f"Analytics {len(analytics_data)}개 · retention "
                            f"{len(retention_data)}개 수집 완료!"
                        ),
                    })
                except Exception as ae:
                    log_stage("youtube_analytics_partial", error=type(ae).__name__)
                    yield sse({
                        "step": "analytics_warn",
                        "message": "Analytics 일부를 가져오지 못해 공개 데이터로 계속 진행합니다.",
                    })

            # Thumbnail reach is only loaded from previously imported official
            # channel_reach_basic_a1 reports. A user click never creates or waits
            # for a Reporting API job; that belongs to scheduled collection.
            stage = "reporting_cache"
            try:
                reach_map = analytics_repository.get_reach_for_videos(
                    [v["id"] for v in videos]
                )
            except Exception as reach_error:
                reach_map = {}
                log_stage("reporting_cache_skipped", error=type(reach_error).__name__)
                yield sse({
                    "step": "reporting_warn",
                    "message": "공식 Reach 캐시는 건너뛰고 Analytics 데이터로 계속 진행합니다.",
                })
            for v in videos:
                reach = reach_map.get(v["id"])
                if reach:
                    ctr = reach["thumbnail_ctr"]
                    v["impressions"] = reach.get("thumbnail_impressions")
                    v["impressions_status"] = reach.get(
                        "thumbnail_impressions_status", "not_reported"
                    )
                    v["ctr"] = ctr.get("value")
                    v["ctr_status"] = ctr.get("status")
                    v["ctr_source"] = reach.get("source")
                    v["ctr_period_start"] = reach.get("period_start")
                    v["ctr_period_end"] = reach.get("period_end")
                    v["ctr_source_as_of"] = reach.get("source_as_of")
                    v["ctr_report_generated_at"] = reach.get("report_generated_at")
                    v["ctr_collected_at"] = reach.get("collected_at")
                    v["ctr_sample_size"] = ctr.get("sample_size", 0)
                else:
                    v["impressions"] = None
                    v["impressions_status"] = "unavailable"
                    v["ctr"] = None
                    v["ctr_status"] = "unavailable"
                    v["ctr_source"] = "youtube_reporting_api:channel_reach_basic_a1"
                    v["ctr_sample_size"] = 0

            log_stage("reporting_cache_done", reach_rows=len(reach_map))
            stage = "ai_analysis"
            yield sse({
                "step": "analyzing",
                "message": "GPT-5.6 Sol이 채널 전략을 생성 중...",
            })
            _task = asyncio.create_task(
                analyze_channel_with_fallback(channel_info, videos, retention_data)
            )
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(3)
            report, ai_provider, openai_error = _task.result()
            log_stage(
                "ai_analysis_done",
                provider=ai_provider,
                openai_fallback_reason=openai_error or "none",
            )
            report["channel_info"] = channel_info
            report["total_analyzed"] = len(videos)
            report["has_analytics"] = bool(analytics_data)
            data_through_values = [
                item.get("data_through") for item in analytics_data if item.get("data_through")
            ]
            report["analytics_metadata"] = {
                "source": "youtube_analytics_api_v2",
                "data_through": max(data_through_values) if data_through_values else None,
                "sample_size": sum(item.get("sample_size", 0) for item in analytics_data),
                "thumbnail_reach_source": "youtube_reporting_api:channel_reach_basic_a1",
                "thumbnail_reach_sample_size": sum(
                    video.get("ctr_sample_size", 0) for video in videos
                ),
                "reporting_mode": "cached_async_collection_only",
                "retention_source": "youtube_analytics_api_v2",
                "retention_requested": len(retention_data) + retention_failed,
                "retention_sample_size": len(retention_data),
                "retention_failed": retention_failed,
                "ai_provider": ai_provider,
                "openai_fallback_reason": openai_error,
            }
            stage = "history_save"
            history_id = await asyncio.wait_for(
                asyncio.to_thread(
                    save_history,
                    "channel",
                    channel_info.get("title", req.channel_id),
                    report,
                ),
                timeout=35,
            )
            report["analysis_metadata"] = {
                "request_id": request_id,
                "elapsed_seconds": elapsed(),
                "history_id": history_id,
            }
            log_stage("done", history_id=history_id)
            yield sse({
                "step": "done",
                "report": report,
                "elapsed_seconds": elapsed(),
                "history_id": history_id,
            })

        except Exception as e:
            log_stage("failed", failed_stage=stage, error=type(e).__name__)
            messages = {
                "youtube_data": "YouTube 채널 데이터를 가져오지 못했습니다.",
                "youtube_analytics": "YouTube Analytics 조회가 완료되지 않았습니다.",
                "ai_analysis": "AI 분석이 제한 시간 안에 완료되지 않았습니다.",
                "history_save": "분석 결과 저장이 완료되지 않았습니다.",
            }
            yield sse({
                "step": "error",
                "message": messages.get(stage, "채널 분석을 완료하지 못했습니다."),
                "failed_stage": stage,
                "request_id": request_id,
            })
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
async def video_feedback(file: UploadFile = File(...), topic: str = Form("")):
    filename = file.filename or "영상 피드백"
    job, source_path = VIDEO_FEEDBACK_JOBS.create_upload(
        filename=filename, topic=(topic or "").strip()
    )
    job_id = str(job["job_id"])
    max_bytes = int(os.getenv("VIDEO_FEEDBACK_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
    written = 0
    try:
        with source_path.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("upload_too_large")
                output.write(chunk)
        if written <= 0:
            raise ValueError("empty_upload")
        VIDEO_FEEDBACK_JOBS.finish_upload(job_id)
    except Exception:
        VIDEO_FEEDBACK_JOBS.fail_upload(job_id)
        try:
            source_path.unlink(missing_ok=True)
            source_path.parent.rmdir()
        except OSError:
            pass
        return JSONResponse(
            {"error": "영상 업로드를 완료하지 못했습니다. 파일 크기와 형식을 확인해 주세요."},
            status_code=400,
        )
    finally:
        await file.close()
    return JSONResponse(
        {
            "job_id": job_id,
            "status": "queued",
            "status_url": f"/api/video-feedback/jobs/{job_id}",
            "message": "업로드를 완료했습니다. 백그라운드에서 글 피드백을 생성합니다.",
        },
        status_code=202,
    )


@app.get("/api/video-feedback/jobs/{job_id}")
async def video_feedback_job_status(job_id: str):
    job = VIDEO_FEEDBACK_JOBS.store.get(job_id)
    if not job:
        return JSONResponse({"error": "영상 피드백 작업을 찾지 못했습니다."}, status_code=404)
    return JSONResponse(job, headers={"Cache-Control": "no-store"})


# ===== AI 편집 디렉터 / 협업형 영상 편집 =====

EDIT_VIDEO_TYPES = {"raw_footage", "rough_cut"}
EDIT_TARGET_FORMATS = {"short_reel", "mid_form", "long_form", "custom"}
EDIT_PURPOSES = {"조회수형", "상담유도형", "제품판매형", "현장기록형", "브랜드신뢰형", ""}


async def _edit_storage_cleanup_loop():
    interval = max(300, int(os.getenv("EDIT_CLEANUP_INTERVAL_SECONDS", "3600")))
    while True:
        try:
            with EDIT_RENDERING_LOCK:
                active = set(EDIT_RENDERING)
            active.update(
                int(job.get("project_id") or 0)
                for job in (EDIT_JOB_QUEUE.snapshot().get("active") or [])
            )
            result = await asyncio.to_thread(
                EditStorageService().cleanup, in_memory_active=active
            )
            if result.get("deleted_bytes"):
                print(
                    f"[edit-storage] cleanup bytes={result['deleted_bytes']} files={result['deleted_files']}",
                    flush=True,
                )
        except Exception as exc:
            print(f"[edit-storage] cleanup failed type={type(exc).__name__}", flush=True)
        await asyncio.sleep(interval)


class EditStoragePreflightRequest(BaseModel):
    file_size: int = Field(ge=0, le=100 * 1024 * 1024 * 1024)
    target_format: str = "mid_form"


class EditStorageCleanupRequest(BaseModel):
    dry_run: bool = False


class EditMediaPurgeRequest(BaseModel):
    confirmed: bool = False


class EditMultipartStartRequest(BaseModel):
    client_upload_id: str = Field(min_length=8, max_length=160)
    force_new: bool = False
    create_new_project: bool = False
    reuse_existing: bool = True
    original_filename: str | None = Field(default=None, max_length=240)
    source_hash: str | None = Field(default=None, max_length=160)
    file_hash: str | None = Field(default=None, max_length=160)
    filename: str = Field(min_length=1, max_length=240)
    file_size: int = Field(gt=0, le=100 * 1024 * 1024 * 1024)
    content_type: str = Field(default="video/mp4", max_length=120)
    video_type: str = "raw_footage"
    target_format: str = "mid_form"
    target_length_seconds: float = Field(default=0, ge=0, le=21600)
    purpose: str = ""
    topic: str = Field(default="", max_length=300)
    strategy_id: int | None = None


class EditMultipartPartRequest(BaseModel):
    part_number: int = Field(ge=1, le=10000)


class EditMultipartCompleteRequest(BaseModel):
    parts: list[dict[str, Any]] = Field(min_length=1, max_length=10000)


class EditMultiSourceProjectRequest(BaseModel):
    topic: str = Field(default="", max_length=300)
    purpose: str = ""
    target_length_seconds: float = Field(default=0, ge=0, le=21600)
    strategy_id: int | None = None


class EditSourceMultipartStartRequest(BaseModel):
    client_upload_id: str = Field(min_length=8, max_length=160)
    filename: str = Field(min_length=1, max_length=240)
    file_size: int = Field(gt=0, le=100 * 1024 * 1024 * 1024)
    content_type: str = Field(default="video/mp4", max_length=120)
    speaker: str = Field(default="", max_length=160)
    recorded_at: str | None = Field(default=None, max_length=80)


def _new_edit_project(project_uuid: str, *, settings: dict, source: dict, status: str) -> dict:
    return transition_project(
        {
            "schema_version": 2, "project_uuid": project_uuid, "status": status,
            "source": source, "settings": settings, "transcript": {},
            "analysis_signals": {"silences": [], "scene_changes": []},
            "diagnosis": {}, "evidence_trace": [], "evidence_snapshot": {},
            "visual_analysis": {"status": "pending", "fallback_used": False},
            "strategy_snapshot": None, "plan_versions": [], "conversation": [],
            "approved_version": None, "approved_at": None, "outputs": {},
            "render_runs": [], "applied_edit_log": [], "advisory_edit_log": [],
            "timings": {}, "approval_memory_id": None,
            "upload_feedback": {"video_id": None, "linked_at": None, "checkpoints": []},
            "error": None,
        },
        status,
        reason="project created",
    )


@app.get("/api/edit-storage")
async def edit_storage_status():
    storage, queue = await asyncio.gather(
        asyncio.to_thread(EditStorageService().snapshot),
        asyncio.to_thread(EDIT_JOB_QUEUE.snapshot),
    )
    storage["queue"] = queue
    return storage


@app.get("/api/edit-jobs")
async def edit_jobs_status(project_id: int | None = None):
    if project_id is None:
        return await asyncio.to_thread(EDIT_JOB_QUEUE.snapshot)
    jobs = await asyncio.to_thread(EDIT_JOB_QUEUE.list, project_id=project_id, limit=100)
    row = await asyncio.to_thread(EditProjectStore().get, project_id)
    project = (row or {}).get("report") or {}
    stale_job = _stale_preview_job(project, jobs)
    if stale_job:
        jobs = [
            ({**job, "status": "stale_rendering"} if int(job["job_id"]) == int(stale_job["job_id"]) else job)
            for job in jobs
        ]
    return {
        "project_id": project_id,
        "jobs": jobs,
        "queue": await asyncio.to_thread(EDIT_JOB_QUEUE.snapshot),
        "stale_rendering": bool(stale_job),
        "message": (
            "기존 렌더가 멈췄습니다. 720p 프록시로 다시 시작할 수 있습니다."
            if stale_job else None
        ),
    }


@app.post("/api/edit-jobs/{job_id}/retry")
async def edit_jobs_retry(job_id: int):
    try:
        job = await asyncio.to_thread(EDIT_JOB_QUEUE.retry, job_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    row = EditProjectStore().get(int(job["project_id"]))
    if row:
        project = row["report"]
        if job.get("type") == "source_analysis":
            try:
                source = find_source(project, str((job.get("payload") or {}).get("source_id") or ""))
                source["status"] = "ANALYZING"
                source["error"] = None
            except KeyError:
                pass
        elif job.get("type") == "story_planning":
            project["story_plan_state"] = "queued"
        elif job.get("type") == "preview_rendering":
            project["preview_state"] = "queued"
            project["preview_error"] = None
            source = project.get("source") or {}
            if int(source.get("size_bytes") or 0) >= EditPipeline._large_source_threshold():
                project["processing_message"] = "원본 용량이 커서 720p 작업용 프록시로 변환 후 분석합니다."
        next_status = "queued" if job.get("type") in {"rendering", "rough_cut_rendering"} else project.get("status") or "uploaded"
        project = transition_project(project, next_status, lifecycle="QUEUED", reason="owner retry", job_id=job_id)
        project["error"] = None
        project["retry_needed"] = False
        EditProjectStore().save(int(job["project_id"]), project)
    return {"ok": True, "job": job}


@app.post("/api/edit-storage/preflight")
async def edit_storage_preflight(req: EditStoragePreflightRequest):
    if req.target_format not in EDIT_TARGET_FORMATS:
        return JSONResponse({"error": "목표 결과 형식이 올바르지 않습니다."}, status_code=400)
    if object_storage_configured():
        local = await asyncio.to_thread(EditStorageService().snapshot)
        if not local.get("direct_upload_enabled"):
            return await asyncio.to_thread(
                EditStorageService().estimate_upload,
                req.file_size,
                target_format=req.target_format,
            )
        reserve = int((local.get("policy") or {}).get("reserve_bytes") or 0)
        return {
            "enough": int(local.get("free_bytes") or 0) >= reserve,
            "direct_upload": True, "file_bytes": req.file_size,
            "required_bytes": reserve, "reserve_bytes": reserve,
            "free_bytes": local.get("free_bytes"), "shortfall_bytes": max(0, reserve - int(local.get("free_bytes") or 0)),
        }
    return await asyncio.to_thread(
        EditStorageService().estimate_upload,
        req.file_size,
        target_format=req.target_format,
    )


@app.post("/api/edit-projects/multisource")
async def edit_multisource_create(req: EditMultiSourceProjectRequest):
    """Create an empty interview bundle; media is added with resumable uploads."""

    if req.purpose not in EDIT_PURPOSES:
        return JSONResponse({"error": "영상 목적이 올바르지 않습니다."}, status_code=400)
    if req.strategy_id is not None and not StrategyRepository().get(req.strategy_id):
        return JSONResponse({"error": "연결할 콘텐츠 전략을 찾지 못했습니다."}, status_code=404)
    project_uuid = uuid.uuid4().hex
    project = _new_edit_project(
        project_uuid,
        settings={
            "video_type": "raw_footage", "target_format": "long_form",
            "target_length_seconds": round(float(req.target_length_seconds), 3),
            "rough_cut_mode": ROUGH_CUT_MODE,
            "purpose": req.purpose, "topic": req.topic.strip()[:300],
            "content_strategy_id": req.strategy_id,
        },
        source={}, status="uploading",
    )
    project.update({
        "schema_version": 3, "project_mode": "multisource_roughcut",
        "sources": [], "uploads_finalized": False,
        "story_plan_state": "collecting_sources", "source": {}, "upload": {},
    })
    store = EditProjectStore()
    project_id = store.create(keyword=req.topic.strip() or "멀티소스 러프컷", project=project)
    return {"ok": True, "project": public_project(store.get(project_id))}


@app.post("/api/edit-projects/{project_id}/sources/multipart/start")
async def edit_source_multipart_start(project_id: int, req: EditSourceMultipartStartRequest):
    if not object_storage_configured():
        return JSONResponse({"error": "Object Storage 직접 업로드가 설정되지 않았습니다."}, status_code=503)
    suffix = Path(req.filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
        return JSONResponse({"error": "지원하지 않는 영상 형식입니다."}, status_code=400)
    try:
        row = _edit_row(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    project = row["report"]
    ensure_multisource(project)
    if project.get("project_mode") != "multisource_roughcut":
        return JSONResponse({"error": "멀티소스 러프컷 프로젝트가 아닙니다."}, status_code=409)
    if project.get("uploads_finalized"):
        return JSONResponse({"error": "원본 묶음이 확정되어 새 영상을 추가할 수 없습니다."}, status_code=409)
    backend = object_storage_from_env()
    for existing in project.get("sources") or []:
        upload = existing.get("upload") or {}
        if upload.get("client_upload_id") != req.client_upload_id:
            continue
        parts = []
        if upload.get("multipart_upload_id"):
            try:
                parts = await asyncio.to_thread(
                    backend.list_parts, existing["storage_key"], upload["multipart_upload_id"]
                )
            except Exception:
                parts = []
        return {
            "project_id": project_id, "source_id": existing["source_id"],
            "part_size": upload.get("part_size"), "uploaded_parts": parts,
            "status": existing.get("status"),
        }
    item = new_source(
        filename=req.filename, size_bytes=req.file_size,
        speaker=req.speaker, recorded_at=req.recorded_at,
    )
    object_key = backend.key(str(project["project_uuid"]), f"source-{item['source_id']}{suffix}")
    part_size = max(8 * 1024 * 1024, int(os.getenv("EDIT_MULTIPART_PART_MB", "64")) * 1024 * 1024)
    item.update({
        "storage_key": object_key, "storage_backend": "object",
        "upload": {
            "client_upload_id": req.client_upload_id, "part_size": part_size,
            "file_size": req.file_size, "started_at": utc_now(),
            "multipart_upload_id": None,
        },
    })
    project["sources"].append(item)
    store = EditProjectStore()
    store.save(project_id, project)
    try:
        upload_id = await asyncio.to_thread(
            backend.initiate_multipart, object_key, content_type=req.content_type,
        )
        item["upload"]["multipart_upload_id"] = upload_id
        store.save(project_id, project)
    except Exception as exc:
        item["status"] = "FAILED_UPLOAD"
        item["error"] = f"{type(exc).__name__}: 업로드 시작 실패"
        store.save(project_id, project)
        return JSONResponse({"error": "원본 업로드를 시작하지 못했습니다.", "source_id": item["source_id"]}, status_code=503)
    return {
        "project_id": project_id, "source_id": item["source_id"],
        "part_size": part_size, "uploaded_parts": [], "status": item["status"],
    }


def _multipart_source(project_id: int, source_id: str) -> tuple[EditProjectStore, dict, dict, Any, dict]:
    store = EditProjectStore()
    row = store.get(project_id)
    if not row:
        raise KeyError("편집 프로젝트를 찾지 못했습니다.")
    project = row["report"]
    item = find_source(project, source_id)
    upload = item.get("upload") or {}
    if not upload.get("multipart_upload_id") or not item.get("storage_key"):
        raise RuntimeError("진행 중인 원본 업로드가 없습니다.")
    backend = object_storage_from_env()
    if backend is None:
        raise RuntimeError("Object Storage가 설정되지 않았습니다.")
    return store, project, item, backend, upload


@app.post("/api/edit-projects/{project_id}/sources/{source_id}/parts/sign")
async def edit_source_part_sign(project_id: int, source_id: str, req: EditMultipartPartRequest):
    try:
        _store, _project, item, backend, upload = _multipart_source(project_id, source_id)
        url = await asyncio.to_thread(
            backend.presigned_part, item["storage_key"], upload["multipart_upload_id"], req.part_number,
        )
        return {"url": url, "part_number": req.part_number, "expires_seconds": 3600}
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception:
        return JSONResponse({"error": "업로드 URL을 만들지 못했습니다."}, status_code=503)


@app.get("/api/edit-projects/{project_id}/sources/{source_id}/upload-status")
async def edit_source_upload_status(project_id: int, source_id: str):
    try:
        _store, _project, item, backend, upload = _multipart_source(project_id, source_id)
        parts = await asyncio.to_thread(
            backend.list_parts, item["storage_key"], upload["multipart_upload_id"],
        )
        return {"project_id": project_id, "source_id": source_id, "status": item.get("status"), "uploaded_parts": parts}
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception:
        return JSONResponse({"error": "업로드 상태를 확인하지 못했습니다."}, status_code=503)


@app.post("/api/edit-projects/{project_id}/sources/{source_id}/complete")
async def edit_source_multipart_complete(
    project_id: int, source_id: str, req: EditMultipartCompleteRequest,
):
    try:
        store, project, item, backend, upload = _multipart_source(project_id, source_id)
        metadata = await asyncio.to_thread(
            backend.complete_multipart, item["storage_key"], upload["multipart_upload_id"], req.parts,
        )
        if int(metadata.get("size_bytes") or 0) != int(upload.get("file_size") or 0):
            raise RuntimeError("완료된 원본 크기가 선택한 파일과 일치하지 않습니다.")
        item["size_bytes"] = int(metadata["size_bytes"])
        item["etag"] = metadata.get("etag")
        item["status"] = "UPLOAD_COMPLETE"
        item["upload"]["completed_at"] = utc_now()
        item["upload"].pop("multipart_upload_id", None)
        store.save(project_id, project)
        job = EDIT_JOB_QUEUE.enqueue(
            project_id, "source_analysis", payload={"source_id": source_id},
            idempotency_key=f"source-analysis:{project_id}:{source_id}:{metadata.get('etag') or metadata['size_bytes']}",
            max_attempts=5, priority=48,
            defer_seconds=1 if EDIT_JOB_WORKER._task is not None else 0,
        )
        project["jobs"] = sorted(set((project.get("jobs") or []) + [int(job["job_id"])]))
        store.save(project_id, project)
        if EDIT_JOB_WORKER._task is None:
            asyncio.create_task(_run_edit_job_without_lifespan(job, store))
        return {"ok": True, "source_id": source_id, "job": job, "project": public_project(store.get(project_id))}
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc), "source_preserved": True}, status_code=409)
    except Exception:
        return JSONResponse({"error": "업로드 완료 처리에 실패했습니다. 원본 object는 보존됩니다."}, status_code=503)


@app.post("/api/edit-projects/{project_id}/sources/finalize")
async def edit_sources_finalize(project_id: int):
    try:
        row = _edit_row(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    project = row["report"]
    ensure_multisource(project)
    if not project.get("sources"):
        return JSONResponse({"error": "먼저 원본 영상을 하나 이상 업로드해주세요."}, status_code=409)
    if any(source.get("status") in {"UPLOADING", "FAILED_UPLOAD"} for source in project["sources"]):
        return JSONResponse({"error": "완료되지 않은 원본 업로드가 있습니다."}, status_code=409)
    project["uploads_finalized"] = True
    project["story_plan_state"] = "waiting_for_sources"
    EditProjectStore().save(project_id, project)
    await EDIT_PIPELINE._queue_story_if_ready(project_id, project)
    return {"ok": True, "project": public_project(EditProjectStore().get(project_id))}


@app.post("/api/edit-uploads/multipart/start")
async def edit_multipart_start(req: EditMultipartStartRequest):
    if not object_storage_configured():
        return JSONResponse({"error": "Object Storage 직접 업로드가 아직 설정되지 않았습니다."}, status_code=503)
    if req.video_type not in EDIT_VIDEO_TYPES or req.target_format not in EDIT_TARGET_FORMATS or req.purpose not in EDIT_PURPOSES:
        return JSONResponse({"error": "편집 프로젝트 설정이 올바르지 않습니다."}, status_code=400)
    if req.strategy_id is not None and not StrategyRepository().get(req.strategy_id):
        return JSONResponse({"error": "연결할 콘텐츠 전략을 찾지 못했습니다."}, status_code=404)
    suffix = Path(req.filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}:
        return JSONResponse({"error": "지원하지 않는 영상 형식입니다."}, status_code=400)
    store = EditProjectStore()
    # A deliberate new production run never looks up an earlier project.
    # Existing lookup remains available only for explicit upload continuation.
    create_new = bool(req.force_new or req.create_new_project or not req.reuse_existing)
    existing = None if create_new else store.find_by_client_upload_id(req.client_upload_id)
    backend = object_storage_from_env()
    if existing:
        project = existing["report"]
        upload = project.get("upload") or {}
        parts = []
        if upload.get("multipart_upload_id"):
            try:
                parts = await asyncio.to_thread(
                    backend.list_parts, upload["object_key"], upload["multipart_upload_id"],
                )
            except Exception:
                parts = []
            return {
                "project_id": existing["id"], "upload_id": upload.get("upload_id"),
                "part_size": upload.get("part_size"), "uploaded_parts": parts,
                "status": project.get("status"),
            }
        if project.get("status") != "upload_failed":
            return {
                "project_id": existing["id"], "upload_id": upload.get("upload_id"),
                "part_size": upload.get("part_size"), "uploaded_parts": parts,
                "status": project.get("status"),
            }
        try:
            multipart_upload_id = await asyncio.to_thread(
                backend.initiate_multipart, upload["object_key"], content_type=req.content_type,
            )
            upload["multipart_upload_id"] = multipart_upload_id
            upload["restarted_at"] = utc_now()
            project["upload"] = upload
            project = transition_project(
                project, "uploading", lifecycle="UPLOADING", reason="multipart upload resumed",
            )
            store.save(int(existing["id"]), project)
            return {
                "project_id": existing["id"], "upload_id": upload.get("upload_id"),
                "part_size": upload.get("part_size"), "uploaded_parts": [],
                "status": "uploading",
            }
        except Exception:
            return JSONResponse({"error": "중단된 업로드를 재개하지 못했습니다."}, status_code=503)
    project_uuid = uuid.uuid4().hex
    settings = {
        "video_type": req.video_type, "target_format": req.target_format,
        "target_length_seconds": round(float(req.target_length_seconds), 3),
        "rough_cut_mode": ROUGH_CUT_MODE,
        "purpose": req.purpose, "topic": req.topic.strip()[:300],
        "content_strategy_id": req.strategy_id,
    }
    object_key = backend.key(project_uuid, f"source{suffix}")
    upload_session_id = uuid.uuid4().hex
    part_size = max(8 * 1024 * 1024, int(os.getenv("EDIT_MULTIPART_PART_MB", "64")) * 1024 * 1024)
    project = _new_edit_project(
        project_uuid, settings=settings, status="uploading",
        source={"filename": req.filename, "size_bytes": req.file_size, "media": {}, "storage_backend": "object", "object_key": object_key},
    )
    project["upload"] = {
        "upload_id": upload_session_id, "client_upload_id": req.client_upload_id,
        "force_new": create_new, "create_new_project": create_new,
        "reuse_existing": not create_new,
        "original_filename": req.original_filename or req.filename,
        "source_hash": req.source_hash, "file_hash": req.file_hash,
        "object_key": object_key,
        "part_size": part_size, "file_size": req.file_size, "started_at": utc_now(),
        "multipart_upload_id": None,
    }
    project_id = store.create(keyword=req.topic.strip() or req.filename, project=project)
    try:
        multipart_upload_id = await asyncio.to_thread(
            backend.initiate_multipart, object_key, content_type=req.content_type,
        )
        project["upload"]["multipart_upload_id"] = multipart_upload_id
        store.save(project_id, project)
    except Exception as exc:
        project = transition_project(project, "upload_failed", lifecycle="FAILED_UPLOAD", reason=type(exc).__name__)
        project["error"] = "Object Storage 업로드를 시작하지 못했습니다."
        store.save(project_id, project)
        return JSONResponse({"error": project["error"], "project_id": project_id}, status_code=503)
    return {
        "project_id": project_id, "upload_id": upload_session_id,
        "part_size": part_size, "uploaded_parts": [], "status": "uploading",
    }


def _multipart_project(project_id: int) -> tuple[EditProjectStore, dict, Any, dict]:
    store = EditProjectStore()
    row = store.get(project_id)
    if not row:
        raise KeyError("편집 프로젝트를 찾지 못했습니다.")
    project = row["report"]
    upload = project.get("upload") or {}
    if not upload.get("multipart_upload_id") or not upload.get("object_key"):
        raise RuntimeError("진행 중인 multipart upload가 없습니다.")
    backend = object_storage_from_env()
    if backend is None:
        raise RuntimeError("Object Storage가 설정되지 않았습니다.")
    return store, project, backend, upload


@app.post("/api/edit-uploads/{project_id}/parts/sign")
async def edit_multipart_sign(project_id: int, req: EditMultipartPartRequest):
    try:
        _store, project, backend, upload = _multipart_project(project_id)
        if project.get("status") != "uploading":
            return JSONResponse({"error": "현재 프로젝트는 업로드 중이 아닙니다."}, status_code=409)
        url = await asyncio.to_thread(backend.presigned_part, upload["object_key"], upload["multipart_upload_id"], req.part_number)
        return {"url": url, "part_number": req.part_number, "expires_seconds": 3600}
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception:
        return JSONResponse({"error": "업로드 URL을 만들지 못했습니다."}, status_code=503)


@app.get("/api/edit-uploads/{project_id}/status")
async def edit_multipart_status(project_id: int):
    try:
        _store, project, backend, upload = _multipart_project(project_id)
        parts = await asyncio.to_thread(backend.list_parts, upload["object_key"], upload["multipart_upload_id"])
        return {
            "project_id": project_id, "status": project.get("status"),
            "file_size": upload.get("file_size"), "part_size": upload["part_size"],
            "uploaded_bytes": sum(int(part.get("size_bytes") or 0) for part in parts),
            "uploaded_part_count": len(parts),
            "uploaded_parts": parts,
        }
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception:
        return JSONResponse({"error": "업로드 상태를 확인하지 못했습니다."}, status_code=503)


@app.post("/api/edit-uploads/{project_id}/complete")
async def edit_multipart_complete(project_id: int, req: EditMultipartCompleteRequest):
    try:
        print(f"[edit-upload-complete] project_id={project_id} parts={len(req.parts)} status=started", flush=True)
        store, project, backend, upload = _multipart_project(project_id)
        metadata = await asyncio.to_thread(
            backend.complete_multipart, upload["object_key"], upload["multipart_upload_id"], req.parts
        )
        if int(metadata.get("size_bytes") or 0) != int(upload.get("file_size") or 0):
            raise RuntimeError("완료된 object 크기가 선택한 영상과 일치하지 않습니다.")
        project["source"]["size_bytes"] = metadata["size_bytes"]
        project["source"]["etag"] = metadata.get("etag")
        project["upload"]["completed_at"] = utc_now()
        project["upload"].pop("multipart_upload_id", None)
        project = transition_project(project, "uploaded", lifecycle="UPLOADED", reason="multipart upload completed")
        store.save(project_id, project)
        job = EDIT_JOB_QUEUE.enqueue(
            project_id, "analysis",
            idempotency_key=f"analysis:{project_id}:{upload.get('upload_id') or metadata.get('etag') or metadata['size_bytes']}",
            payload={"source_size": metadata["size_bytes"], "upload_id": upload.get("upload_id")},
            max_attempts=3, priority=50,
            defer_seconds=1 if EDIT_JOB_WORKER._task is not None else 0,
        )
        project["jobs"] = sorted(set((project.get("jobs") or []) + [int(job["job_id"])]))
        store.save(project_id, project)
        print(f"[edit-upload-complete] project_id={project_id} job_id={job['job_id']} status=completed", flush=True)
        return {"ok": True, "project": public_project(store.get(project_id)), "job": job}
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    except Exception:
        return JSONResponse({"error": "업로드 완료 처리에 실패했습니다. 업로드 데이터는 보존됩니다."}, status_code=503)


@app.delete("/api/edit-uploads/{project_id}")
async def edit_multipart_abort(project_id: int):
    try:
        store, project, backend, upload = _multipart_project(project_id)
        await asyncio.to_thread(backend.abort_multipart, upload["object_key"], upload["multipart_upload_id"])
        project = transition_project(project, "upload_failed", lifecycle="FAILED_UPLOAD", reason="owner aborted upload")
        project["upload"]["aborted_at"] = utc_now()
        project["upload"].pop("multipart_upload_id", None)
        store.save(project_id, project)
        return {"ok": True, "project": public_project(store.get(project_id))}
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.post("/api/edit-storage/cleanup")
async def edit_storage_cleanup(req: EditStorageCleanupRequest):
    with EDIT_RENDERING_LOCK:
        active = set(EDIT_RENDERING)
    active.update(
        int(job.get("project_id") or 0)
        for job in (EDIT_JOB_QUEUE.snapshot().get("active") or [])
    )
    result = await asyncio.to_thread(
        EditStorageService().cleanup,
        dry_run=req.dry_run,
        in_memory_active=active,
    )
    result["storage"] = await asyncio.to_thread(EditStorageService().snapshot)
    return result


def _edit_row(project_id: int) -> dict:
    row = EditProjectStore().get(project_id)
    if not row:
        raise KeyError("편집 프로젝트를 찾지 못했습니다.")
    return row


def _job_time(value: Any) -> datetime.datetime | None:
    try:
        parsed = datetime.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


def _stale_preview_job(project: dict, jobs: list[dict], *, stale_seconds: int = 600) -> dict | None:
    """Find a preview with no output and no worker heartbeat for ten minutes."""
    if (project.get("outputs") or {}).get("preview"):
        return None
    rendering_states = {"rendering", "preview_rendering", "running", "stale_rendering"}
    if (
        str(project.get("preview_state") or "") not in rendering_states
        and str(project.get("status") or "") not in rendering_states
    ):
        return None
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=max(600, int(stale_seconds))
    )
    for job in jobs:
        if job.get("type") != "preview_rendering" or job.get("status") != "running":
            continue
        heartbeat = _job_time(job.get("heartbeat_at") or job.get("started_at"))
        if heartbeat is None or heartbeat < cutoff:
            return job
    return None


def _preview_restart_candidate(project: dict, jobs: list[dict]) -> dict | None:
    stale = _stale_preview_job(project, jobs)
    if stale:
        return stale
    if (project.get("outputs") or {}).get("preview"):
        return None
    return next((
        job for job in jobs
        if job.get("type") == "preview_rendering"
        and (
            job.get("status") == "stale_rendering"
            or (job.get("status") == "failed" and job.get("retry_needed"))
        )
    ), None)


def _seconds_since(value: Any) -> int | None:
    parsed = _job_time(value)
    if parsed is None:
        return None
    return max(0, int((datetime.datetime.now(datetime.timezone.utc) - parsed).total_seconds()))


async def _edit_status_payload(project_id: int) -> dict:
    row = _edit_row(project_id)
    project = row["report"]
    jobs = await asyncio.to_thread(EDIT_JOB_QUEUE.list, project_id=project_id, limit=100)
    stale_preview = _stale_preview_job(project, jobs)
    active = next((job for job in jobs if job.get("status") in {"running", "queued"}), None)
    job = stale_preview or active or (jobs[0] if jobs else None)
    job_status = "stale_rendering" if stale_preview else str((job or {}).get("status") or "not_started")
    heartbeat_at = (job or {}).get("heartbeat_at") or (job or {}).get("started_at")
    heartbeat_age = _seconds_since(heartbeat_at)

    proxy = project.get("proxy") or {}
    analysis_progress = project.get("analysis_progress") or {}
    processing_progress = project.get("processing_progress") or {}
    processing_output = project.get("processing_output") or {}
    outputs = project.get("outputs") or {}
    job_type = str((job or {}).get("type") or "")
    source_size = int((project.get("source") or {}).get("size_bytes") or 0)
    large_source = source_size >= EditPipeline._large_source_threshold()

    if job_type == "preview_rendering" and large_source and proxy.get("status") != "ready":
        stage, label = "proxy_generation", "720p 프록시 생성"
    elif job_type == "preview_rendering":
        stage, label = "preview_rendering", "검토용 720p 프리뷰 생성"
    elif job_type in {"analysis", "source_analysis"}:
        stage = str(analysis_progress.get("stage") or "proxy_analysis")
        label = str(analysis_progress.get("label") or "프록시 기반 분석")
    elif job_type == "story_planning":
        stage, label = "rough_cut_planning", "러프컷 구성"
    elif job_type == "rough_cut_rendering":
        stage, label = "rough_cut_rendering", "러프컷 생성"
    elif project.get("preview_state") == "succeeded" or outputs.get("preview"):
        stage, label = "completed", "검토용 720p 프리뷰 완료"
    else:
        stage = str(processing_progress.get("current_stage") or project.get("status") or "waiting")
        label = str(processing_progress.get("stage_label") or project.get("processing_message") or "작업 대기")

    if processing_progress.get("current_stage") == stage:
        stage_percent = int(processing_progress.get("stage_percent") or 0)
        overall_percent = int(processing_progress.get("overall_percent") or 0)
    elif stage in {"proxy_analysis", "transcribing", "visual_analysis", "diagnosing", "rough_cut_planning"}:
        stage_percent = int(analysis_progress.get("percent") or 0)
        bases = {"proxy_analysis": 35, "transcribing": 40, "visual_analysis": 58, "diagnosing": 65, "rough_cut_planning": 75}
        overall_percent = bases.get(stage, 35) + round(stage_percent * 0.15)
    elif stage == "completed":
        stage_percent = overall_percent = 100
    elif stage == "preview_rendering":
        stage_percent, overall_percent = (0, 80) if job_status == "queued" else (5, 81)
    elif stage == "proxy_generation":
        stage_percent, overall_percent = (0, 10)
    else:
        stage_percent = int(processing_progress.get("stage_percent") or 0)
        overall_percent = int(processing_progress.get("overall_percent") or 0)

    output_meta: dict[str, Any] = {}
    if job_status in {"running", "queued", "stale_rendering"} and processing_output:
        output_meta = processing_output
    elif outputs.get("preview"):
        output_meta = outputs["preview"]
    elif proxy.get("object_key"):
        output_meta = proxy
    output_path = str(
        output_meta.get("object_key") or output_meta.get("path")
        or output_meta.get("filename") or ""
    )
    output_exists = bool(output_meta.get("exists") or output_meta.get("size_bytes"))
    output_size = max(0, int(output_meta.get("size_bytes") or 0))
    if output_meta.get("object_key"):
        try:
            backend = object_storage_from_env()
            metadata = await asyncio.to_thread(backend.head, output_meta["object_key"]) if backend else None
            if metadata:
                output_exists = True
                output_size = max(0, int(metadata.get("size_bytes") or output_size))
        except Exception:
            output_exists = False
    size_updated_at = (
        output_meta.get("size_updated_at") or output_meta.get("checked_at")
        or output_meta.get("created_at")
    )
    growth_bytes = max(0, int(output_meta.get("size_growth_bytes") or 0))
    growth_state = (
        "increasing" if growth_bytes > 0 else
        ("completed" if stage == "completed" or output_meta.get("completed") else ("unchanged" if output_exists else "none"))
    )
    stage_started_at = output_meta.get("stage_started_at") or analysis_progress.get("updated_at")
    stage_age = _seconds_since(stage_started_at)
    size_age = _seconds_since(output_meta.get("size_updated_at"))
    error_text = " ".join(str(value or "") for value in (
        (job or {}).get("error"), project.get("error"), project.get("preview_error")
    )).lower()
    capacity_error = any(token in error_text for token in (
        "temporary storage", "no space left", "enospc", "localworkingspaceinsufficient",
        "size of temporary storage volume", "proxy size limit exceeded",
    ))
    stale_reasons: list[str] = []
    project_running_state = (
        project.get("preview_state") in {"rendering", "preview_rendering", "running"}
        or project.get("status") in {"preview_rendering", "running"}
    )
    if stale_preview or job_status == "stale_rendering":
        stale_reasons.append("preview heartbeat가 10분 이상 없습니다")
    elif job_status == "running" and heartbeat_age is not None and heartbeat_age >= 600:
        stale_reasons.append("worker heartbeat가 10분 이상 없습니다")
    if job_status == "running" and output_exists and growth_bytes <= 0 and size_age is not None and size_age >= 300:
        stale_reasons.append("output 파일 크기가 5분 이상 증가하지 않았습니다")
    if job_status == "running" and stage_age is not None and stage_age >= 900 and growth_bytes <= 0:
        stale_reasons.append("같은 단계에서 15분 이상 변화가 없습니다")
    if capacity_error:
        stale_reasons.append("/tmp 저장공간 한도를 초과했습니다")
    if project_running_state and not active:
        orphan_age = _seconds_since(
            processing_progress.get("updated_at")
            or processing_output.get("checked_at")
            or (project.get("storage_state") or {}).get("render_heartbeat_at")
        )
        if orphan_age is None or orphan_age >= 600:
            stale_reasons.append("project는 실행 중이지만 active job이 없습니다")
    is_stale = bool(stale_reasons)
    project_retry_state = project.get("status") in {
        "uploading", "direct_uploading", "failed_retry_needed", "analysis_failed", "render_failed",
    }
    can_retry = is_stale or project_retry_state or job_status in {"failed", "failed_retry_needed", "stale_rendering", "cancelled"}
    if is_stale:
        health_status = "stale"
        status_message = "작업이 멈춘 것으로 보입니다. 720p 프록시로 다시 시작할 수 있습니다."
    elif job_status == "running" and (heartbeat_age or 0) >= 180:
        health_status = "slow"
        status_message = "작업이 오래 걸리고 있지만 worker가 계속 처리 중입니다."
    elif job_status == "running":
        health_status = "normal"
        status_message = f"정상 진행 중입니다. 마지막 처리 {heartbeat_age or 0}초 전"
    elif job_status == "completed":
        health_status, status_message = "completed", "작업이 완료됐습니다."
    else:
        health_status, status_message = "waiting", "안전 작업 큐에서 대기 중입니다."
    if not is_stale and stage == "proxy_generation":
        status_message = "원본 용량이 커서 720p 작업용 프록시로 변환 후 분석합니다. " + status_message
    elif not is_stale and stage == "preview_rendering":
        status_message = "검토용 720p 프리뷰를 생성 중입니다. 최종 원본 화질 렌더는 승인 후 요청하세요. " + status_message

    transcript = project.get("transcript") or {}
    processing_checked_age = _seconds_since(processing_output.get("checked_at"))
    return {
        "project_id": project_id, "job_id": int((job or {}).get("job_id") or 0) or None,
        "active_job_id": int((active or {}).get("job_id") or 0) or None,
        "project_status": project.get("status"), "job_status": job_status,
        "current_stage": stage, "stage_label": label,
        "stage_percent": max(0, min(100, stage_percent)),
        "overall_percent": max(0, min(100, overall_percent)),
        "last_heartbeat_at": heartbeat_at,
        "seconds_since_last_heartbeat": heartbeat_age,
        "last_processed_at": processing_progress.get("updated_at") or size_updated_at or heartbeat_at,
        "seconds_since_last_processing": _seconds_since(processing_progress.get("updated_at") or size_updated_at or heartbeat_at),
        "proxy_status": proxy.get("status") or ("not_required" if not large_source else "pending"),
        "transcript_status": "completed" if transcript.get("segments") else ("processing" if job_type in {"analysis", "source_analysis"} else "pending"),
        "rough_cut_status": (
            "completed" if outputs.get("rough_cut") else project.get("story_plan_state")
            or ("ready" if project.get("plan_versions") else "pending")
        ),
        "preview_status": "stale_rendering" if stale_preview else project.get("preview_state") or "not_requested",
        "render_status": project.get("final_render_state") or "not_requested",
        "output_file_path": output_path or None,
        "output_file_exists": output_exists,
        "output_file_size_mb": round(output_size / 1024 / 1024, 2),
        "output_file_size_updated_at": size_updated_at,
        "output_file_size_growth": growth_state,
        "output_file_size_growth_mb": round(growth_bytes / 1024 / 1024, 2),
        "is_stale": is_stale, "stale_reason": "; ".join(stale_reasons) or None,
        "can_retry": can_retry,
        "retry_reason": ("; ".join(stale_reasons) if can_retry else None),
        "recommended_action": (
            "retry_upload_complete" if project.get("status") in {"uploading", "direct_uploading"} and not (project.get("source") or {}).get("object_key") else
            ("restart_with_720p_proxy" if can_retry and large_source else "wait")
        ),
        "worker_active": job_status == "running" and heartbeat_age is not None and heartbeat_age < 180,
        "ffmpeg_active": (
            job_status == "running"
            and stage in {"proxy_generation", "preview_rendering", "rough_cut_rendering"}
            and processing_checked_age is not None and processing_checked_age < 30
        ),
        "health_status": health_status, "status_message": status_message,
    }


async def _run_edit_job_without_lifespan(job: dict, store: EditProjectStore) -> None:
    """Exercise the same durable handler in CLI/tests that omit ASGI lifespan.

    Production always has the singleton worker. This fallback prevents an API
    request from waiting forever in embedded/TestClient environments while
    keeping the persisted job state and idempotent handler contract identical.
    """
    pipeline = EditPipeline(store)
    handler = {
        "analysis": pipeline.analysis,
        "rendering": pipeline.rendering,
        "preview_rendering": pipeline.preview_rendering,
        "performance_sync": pipeline.performance_sync,
        "source_analysis": pipeline.source_analysis,
        "story_planning": pipeline.story_planning,
        "rough_cut_rendering": pipeline.rough_cut_rendering,
    }.get(str(job.get("type") or ""))
    if not handler:
        return
    claimed = EDIT_JOB_QUEUE.claim("embedded-worker", allowed_types={str(job["type"])})
    if not claimed:
        return
    try:
        timings = await handler(claimed) or {}
        EDIT_JOB_QUEUE.finish(int(claimed["job_id"]), timings=timings)
    except Exception as exc:
        EDIT_JOB_QUEUE.fail(
            int(claimed["job_id"]), exc,
            retryable=getattr(exc, "retryable", True),
        )


@app.get("/api/edit-projects")
async def edit_projects_list(limit: int = 30):
    return EditProjectStore().list(limit=limit)


@app.get("/api/edit-projects/{project_id}")
async def edit_projects_get(project_id: int):
    try:
        project = public_project(_edit_row(project_id))
        with EDIT_RENDERING_LOCK:
            project["runtime_rendering"] = project_id in EDIT_RENDERING
        jobs = EDIT_JOB_QUEUE.list(project_id=project_id, limit=100)
        stale_job = _stale_preview_job(project, jobs)
        current_job = next((
            job for job in jobs
            if job.get("status") in {"running", "queued"}
            and (not stale_job or int(job["job_id"]) != int(stale_job["job_id"]))
        ), None)
        project["last_job"] = jobs[0] if jobs else None
        project["failed_job"] = next((job for job in jobs if job.get("status") == "failed"), None)
        if stale_job:
            project["stale_rendering"] = True
            project["stale_rendering_job"] = {**stale_job, "status": "stale_rendering"}
            project["preview_state"] = "stale_rendering"
            project["runtime_rendering"] = False
            project["status_message"] = "기존 렌더가 멈췄습니다. 720p 프록시로 다시 시작할 수 있습니다."
        if current_job:
            project["current_job"] = current_job
            project["runtime_rendering"] = bool(
                current_job.get("type") in {"rendering", "preview_rendering", "rough_cut_rendering"}
                and current_job.get("status") == "running"
            )
            if current_job.get("status") == "queued":
                active = EDIT_JOB_QUEUE.snapshot().get("active") or []
                queued = [job for job in active if job.get("status") == "queued"]
                project["queue_position"] = next((index for index, job in enumerate(queued, 1) if int(job["job_id"]) == int(current_job["job_id"])), None)
        return project
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.get("/api/edit-projects/{project_id}/status")
async def edit_projects_progress_status(project_id: int):
    try:
        return await _edit_status_payload(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/api/edit-projects/analyze")
async def edit_projects_analyze(
    file: UploadFile = File(...),
    video_type: str = Form("raw_footage"),
    target_format: str = Form("mid_form"),
    target_length_seconds: float = Form(0),
    purpose: str = Form(""),
    topic: str = Form(""),
    strategy_id: int | None = Form(None),
):
    if video_type not in EDIT_VIDEO_TYPES:
        return JSONResponse({"error": "영상 타입이 올바르지 않습니다."}, status_code=400)
    if target_format not in EDIT_TARGET_FORMATS:
        return JSONResponse({"error": "목표 결과 형식이 올바르지 않습니다."}, status_code=400)
    if purpose not in EDIT_PURPOSES:
        return JSONResponse({"error": "영상 목적이 올바르지 않습니다."}, status_code=400)
    if target_length_seconds < 0 or target_length_seconds > 21600:
        return JSONResponse({"error": "목표 길이는 0~21600초로 입력해주세요."}, status_code=400)
    if strategy_id is not None and not StrategyRepository().get(strategy_id):
        return JSONResponse({"error": "연결할 콘텐츠 전략을 찾지 못했습니다."}, status_code=404)

    store = EditProjectStore()
    ingest = MediaIngestService(store)
    project_uuid = uuid.uuid4().hex
    try:
        source_path, size_bytes, original_filename = await ingest.persist_upload(
            file, project_uuid, target_format=target_format
        )
    except StorageCapacityError as exc:
        return JSONResponse({"error": str(exc)}, status_code=507)
    except MediaValidationError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "영상을 안전하게 저장하지 못했습니다."}, status_code=503)

    settings = {
        "video_type": video_type,
        "target_format": target_format,
        "target_length_seconds": round(float(target_length_seconds), 3),
        "rough_cut_mode": ROUGH_CUT_MODE,
        "purpose": purpose,
        "topic": topic.strip()[:300],
        "content_strategy_id": strategy_id,
    }
    project = {
        "schema_version": 1,
        "project_uuid": project_uuid,
        "status": "uploaded",
        "source": {
            "filename": original_filename,
            "storage_name": source_path.name,
            "size_bytes": size_bytes,
            "media": {},
        },
        "settings": settings,
        "transcript": {},
        "analysis_signals": {"silences": [], "scene_changes": []},
        "diagnosis": {},
        "visual_analysis": {"status": "pending", "fallback_used": False},
        "evidence_trace": [],
        "evidence_snapshot": {},
        "strategy_snapshot": None,
        "plan_versions": [],
        "conversation": [],
        "approved_version": None,
        "approved_at": None,
        "outputs": {},
        "render_runs": [],
        "applied_edit_log": [],
        "advisory_edit_log": [],
        "timings": {},
        "approval_memory_id": None,
        "upload_feedback": {"video_id": None, "linked_at": None, "checkpoints": []},
        "error": None,
    }
    project_id = store.create(
        keyword=topic.strip() or original_filename,
        project=project,
    )

    async def stream():
        nonlocal project
        analysis_started = time.perf_counter()
        try:
            yield sse({"step": "validating", "message": "업로드를 마쳤습니다. 영상 메타데이터를 확인합니다.", "project_id": project_id})
            media = await asyncio.to_thread(ingest.probe, source_path)
            project["source"]["media"] = media
            project["status"] = "transcribing"
            store.save(project_id, project)

            yield sse({"step": "signals", "message": "대사·정적·장면 전환을 타임코드로 분석합니다.", "project_id": project_id})
            signals_started = time.perf_counter()
            inspect_task = asyncio.create_task(ingest.inspect_and_transcribe(source_path, media))
            waited = 0
            while not inspect_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(inspect_task), timeout=3)
                except TimeoutError:
                    waited += 3
                    yield sse({
                        "step": "transcribing",
                        "message": f"음성과 편집 힌트를 분석하고 있습니다. ({waited}초)",
                        "project_id": project_id,
                    })
            transcript, silences, scenes = inspect_task.result()
            project["transcript"] = transcript
            project["analysis_signals"] = {"silences": silences, "scene_changes": scenes}
            project["timings"]["media_and_transcript_seconds"] = round(
                time.perf_counter() - signals_started, 3
            )
            project["status"] = "retrieving_context"
            analysis = EditAnalysisService()
            yield sse({"step": "visual", "message": "현장 화면·주방기구·공사 장면을 타임코드별로 분석합니다.", "project_id": project_id})
            visual_analysis = await EditPipeline(store)._run_visual_analysis(
                source=source_path, transcript=transcript, media=media,
                scenes=scenes, analysis=analysis,
            )
            project["visual_analysis"] = visual_analysis
            project["timings"]["visual_analysis_seconds"] = visual_analysis.get("analysis_seconds")
            store.save(project_id, project)

            yield sse({"step": "retrieving", "message": "유사 영상 retention·과거 피드백·비즈니스PT 지식을 연결합니다.", "project_id": project_id})
            retrieval_started = time.perf_counter()
            evidence_task = asyncio.create_task(
                analysis.collect_evidence(
                    topic=settings["topic"], purpose=settings["purpose"], strategy_id=strategy_id
                )
            )
            while not evidence_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(evidence_task), timeout=2)
                except TimeoutError:
                    yield sse({"step": "retrieving", "message": "채널 근거를 병렬로 비교하고 있습니다.", "project_id": project_id})
            evidence, trace, strategy = evidence_task.result()
            trace.append({
                "tool": "visual_frame_analysis", "source": "proxy_timecoded_frames",
                "sample_size": int((visual_analysis or {}).get("analyzed_frame_count") or 0),
                "freshness": None, "unavailable": (visual_analysis or {}).get("status") != "succeeded",
            })
            project["timings"]["retrieval_seconds"] = round(
                time.perf_counter() - retrieval_started, 3
            )
            project["evidence_snapshot"] = evidence
            project["evidence_trace"] = trace
            project["strategy_snapshot"] = strategy
            project["status"] = "diagnosing"
            store.save(project_id, project)

            yield sse({"step": "diagnosing", "message": "AI가 타임코드 기반 최초 편집안을 작성합니다. 아직 편집은 실행하지 않습니다.", "project_id": project_id})
            diagnosis_started = time.perf_counter()
            diagnosis_task = asyncio.create_task(
                analysis.diagnose(
                    transcript=transcript,
                    media=media,
                    silences=silences,
                    scenes=scenes,
                    settings=settings,
                    evidence=evidence,
                    strategy=strategy,
                    visual_analysis=visual_analysis,
                )
            )
            waited = 0
            while not diagnosis_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(diagnosis_task), timeout=4)
                except TimeoutError:
                    waited += 4
                    yield sse({
                        "step": "diagnosing",
                        "message": f"편집 제안과 채널 근거를 대조하고 있습니다. ({waited}초)",
                        "project_id": project_id,
                    })
            diagnosis = diagnosis_task.result()
            project["timings"]["gpt_diagnosis_seconds"] = round(
                time.perf_counter() - diagnosis_started, 3
            )
            plan = prepare_plan(
                diagnosis.get("plan") or {},
                float(media["duration"]),
                target_format=settings.get("target_format"),
                transcript=transcript,
                rough_cut_mode=ROUGH_CUT_MODE,
                source_filename=str((project.get("source") or {}).get("filename") or "원본 영상"),
            )
            project["diagnosis"] = {key: value for key, value in diagnosis.items() if key != "plan"}
            project["plan_versions"] = [
                {
                    "version": 1,
                    "status": "proposed",
                    "created_at": utc_now(),
                    "source": "ai_diagnosis",
                    "user_request": "",
                    "revision_summary": "AI 최초 분석 제안",
                    "diff": [],
                    "plan": plan,
                }
            ]
            project["rough_cut_script_editor"] = initialize_script_editor(project)
            project["status"] = "proposed"
            project["error"] = None
            project["timings"]["analysis_total_seconds"] = round(
                time.perf_counter() - analysis_started, 3
            )
            store.save(project_id, project)
            yield sse({"step": "done", "message": "분석 제안이 준비됐습니다. 대화로 수정한 뒤 승인해주세요.", "project": public_project(store.get(project_id))})
        except Exception as exc:
            project["status"] = "analysis_failed"
            project["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            store.save(project_id, project)
            yield sse({
                "step": "error",
                "message": str(exc)[:300] or "편집 분석에 실패했습니다.",
                "project_id": project_id,
                "source_preserved": True,
            })

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/edit-projects/{project_id}/revise")
async def edit_projects_revise(project_id: int, req: EditPlanRevisionRequest):
    message = req.message.strip()
    if not message:
        return JSONResponse({"error": "수정 요청을 입력해주세요."}, status_code=400)
    try:
        row = _edit_row(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    project = row["report"]
    versions = project.get("plan_versions") or []
    if not versions:
        return JSONResponse({"error": "먼저 영상 분석을 완료해주세요."}, status_code=409)
    if project.get("status") in {"queued", "rendering"}:
        return JSONResponse({"error": "렌더링 중에는 편집안을 바꿀 수 없습니다."}, status_code=409)

    if project.get("project_mode") == "multisource_roughcut":
        async def multisource_stream():
            nonlocal project
            try:
                yield sse({"step": "revising", "message": "기존 분석 cache를 유지하고 구성 순서만 수정합니다."})
                ready = [source for source in project.get("sources") or [] if source.get("status") == "SOURCE_ANALYZED"]
                candidates = bounded_story_candidates(ready)
                current = versions[-1]["plan"]
                reasoning = await EditAnalysisService().plan_multisource_story(
                    candidates=candidates, evidence=project.get("evidence_snapshot") or {},
                    strategy=project.get("strategy_snapshot"), settings=project.get("settings") or {},
                    user_request=message, current_plan=current,
                )
                plan = apply_story_reasoning(
                    project, reasoning,
                    target_length_seconds=float((project.get("settings") or {}).get("target_length_seconds") or 0),
                )
                version = int(versions[-1]["version"]) + 1
                versions.append({
                    "version": version, "status": "revised", "created_at": utc_now(),
                    "source": "multisource_user_revision", "user_request": message[:4000],
                    "revision_summary": str(reasoning.get("recommended_direction") or "구성 수정 반영")[:1000],
                    "diff": plan_diff(current, plan), "plan": plan,
                })
                project["plan_versions"] = versions
                project["rough_cut_script_editor"] = initialize_script_editor(project)
                project["conversation"] = (project.get("conversation") or []) + [
                    {"role": "user", "content": message[:4000], "created_at": utc_now(), "version": version},
                    {"role": "assistant", "content": versions[-1]["revision_summary"], "created_at": utc_now(), "version": version},
                ]
                project["approved_version"], project["approved_at"] = None, None
                project = transition_project(project, "revised", lifecycle="AWAITING_REVIEW", reason="multi-source story revision")
                EditProjectStore().save(project_id, project)
                yield sse({"step": "done", "message": "러프컷 구성안을 수정했습니다.", "project": public_project(EditProjectStore().get(project_id))})
            except Exception as exc:
                yield sse({"step": "error", "message": str(exc)[:300] or "구성안 수정에 실패했습니다."})

        return StreamingResponse(
            multisource_stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def stream():
        nonlocal project
        try:
            yield sse({"step": "revising", "message": "요청을 현재 편집안과 대조합니다."})
            current = versions[-1]["plan"]
            analysis = EditAnalysisService()
            revision_task = asyncio.create_task(
                analysis.revise(
                    current_plan=current,
                    user_request=message,
                    transcript=project.get("transcript") or {},
                    media=(project.get("source") or {}).get("media") or {},
                    settings=project.get("settings") or {},
                    evidence=project.get("evidence_snapshot") or {},
                    strategy=project.get("strategy_snapshot"),
                    visual_analysis=project.get("visual_analysis") or {},
                )
            )
            waited = 0
            while not revision_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(revision_task), timeout=3)
                except TimeoutError:
                    waited += 3
                    yield sse({"step": "revising", "message": f"합의안을 갱신하고 있습니다. ({waited}초)"})
            revised = revision_task.result()
            media_duration = float(((project.get("source") or {}).get("media") or {}).get("duration") or 0)
            plan = prepare_plan(
                revised.get("plan") or {},
                media_duration,
                target_format=(project.get("settings") or {}).get("target_format"),
                transcript=project.get("transcript") or {},
                rough_cut_mode=ROUGH_CUT_MODE,
                source_filename=str((project.get("source") or {}).get("filename") or "원본 영상"),
            )
            version = int(versions[-1]["version"]) + 1
            diff = plan_diff(current, plan)
            versions.append(
                {
                    "version": version,
                    "status": "revised",
                    "created_at": utc_now(),
                    "source": "user_revision",
                    "user_request": message[:4000],
                    "revision_summary": str(revised.get("revision_summary") or "수정 요청 반영")[:1000],
                    "diff": diff,
                    "plan": plan,
                }
            )
            project["plan_versions"] = versions
            project["rough_cut_script_editor"] = initialize_script_editor(project)
            project["conversation"] = (project.get("conversation") or []) + [
                {"role": "user", "content": message[:4000], "created_at": utc_now(), "version": version},
                {"role": "assistant", "content": str(revised.get("revision_summary") or "수정 요청 반영")[:2000], "created_at": utc_now(), "version": version},
            ]
            project = transition_project(project, "revised", lifecycle="AWAITING_REVIEW", reason="owner revision")
            project["approved_version"] = None
            project["approved_at"] = None
            project["error"] = None
            EditProjectStore().save(project_id, project)
            yield sse({"step": "done", "message": "수정안을 반영했습니다. 변경 내용을 확인해주세요.", "project": public_project(EditProjectStore().get(project_id))})
        except Exception as exc:
            yield sse({"step": "error", "message": str(exc)[:300] or "편집안 수정에 실패했습니다."})

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/edit-projects/{project_id}/edit-script/initialize")
async def edit_projects_script_initialize(project_id: int):
    try:
        row = _edit_row(project_id)
        project = row["report"]
        state = initialize_script_editor(project)
    except (KeyError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404 if isinstance(exc, KeyError) else 409)
    project["rough_cut_script_editor"] = state
    EditProjectStore().save(project_id, project)
    return {"ok": True, "script_editor": state, "project": public_project(EditProjectStore().get(project_id))}


@app.post("/api/edit-projects/{project_id}/edit-script/toggle")
async def edit_projects_script_toggle(project_id: int, req: EditScriptToggleRequest):
    try:
        row = _edit_row(project_id)
        project = row["report"]
        state = initialize_script_editor(project)
    except (KeyError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404 if isinstance(exc, KeyError) else 409)
    active_preview = next((
        job for job in EDIT_JOB_QUEUE.list(project_id=project_id, limit=100)
        if job.get("type") in {"preview_rendering", "rough_cut_rendering"}
        and job.get("status") == "running"
    ), None)
    if active_preview:
        return JSONResponse({"error": "현재 러프컷을 만드는 중입니다. 완료 후 스크립트를 수정해주세요."}, status_code=409)
    row_by_id = {
        str(item.get("segment_id") or ""): item
        for item in state.get("transcript_segments") or []
    }
    selected = row_by_id.get(req.segment_id)
    if not selected:
        return JSONResponse({"error": "편집 스크립트 구간을 찾지 못했습니다."}, status_code=404)
    deleted = set(str(value) for value in state.get("deleted_segment_ids") or [])
    restored = set(str(value) for value in state.get("restored_segment_ids") or [])
    if req.deleted:
        deleted.add(req.segment_id)
        restored.discard(req.segment_id)
    else:
        deleted.discard(req.segment_id)
        restored.add(req.segment_id)
    state = apply_script_choices(state, deleted_ids=deleted, restored_ids=restored)
    warning = None
    if req.deleted and (
        selected.get("viewer_confusion_risk")
        or selected.get("context_continuity")
        or not selected.get("topic_complete", True)
    ):
        warning = "이 구간을 삭제하면 앞뒤 문맥이 어색할 수 있습니다."
    state["last_warning"] = warning
    state["updated_at"] = utc_now()
    project["rough_cut_script_editor"] = state
    if (project.get("outputs") or {}).get("preview"):
        project["preview_state"] = "stale"
    EditProjectStore().save(project_id, project)
    return {
        "ok": True, "warning": warning, "script_editor": state,
        "project": public_project(EditProjectStore().get(project_id)),
    }


@app.post("/api/edit-projects/{project_id}/edit-script/save")
async def edit_projects_script_save(project_id: int):
    try:
        row = _edit_row(project_id)
        project = row["report"]
        state = initialize_script_editor(project)
    except (KeyError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=404 if isinstance(exc, KeyError) else 409)
    if not state.get("dirty") and state.get("saved_version"):
        return {"ok": True, "reused": True, "project": public_project(row)}
    versions = project.get("plan_versions") or []
    next_version = max((int(item.get("version") or 0) for item in versions), default=0) + 1
    plan = json.loads(json.dumps(state.get("user_modified_edit_plan") or {}, ensure_ascii=False))
    versions.append({
        "version": next_version, "status": "approved", "created_at": utc_now(),
        "source": "user_script_edit", "user_request": "편집 스크립트 직접 수정",
        "revision_summary": f"스크립트 구간 {len(state.get('deleted_segment_ids') or [])}개 삭제 반영",
        "diff": plan_diff(versions[-1].get("plan") or {}, plan) if versions else [],
        "plan": plan,
    })
    for item in versions[:-1]:
        if item.get("status") == "approved":
            item["status"] = "superseded"
    project["plan_versions"] = versions
    project["approved_version"] = next_version
    project["approved_at"] = utc_now()
    project["approved_plan_snapshot"] = json.loads(json.dumps(versions[-1], ensure_ascii=False))
    state["base_version"] = next_version
    state["saved_version"] = next_version
    state["dirty"] = False
    state["saved_at"] = utc_now()
    project["rough_cut_script_editor"] = state
    project["conversation"] = (project.get("conversation") or []) + [{
        "role": "user", "content": f"편집 스크립트 수정본 v{next_version} 저장",
        "created_at": utc_now(), "version": next_version,
    }]
    if project.get("status") not in {"final_queued", "final_rendering"}:
        project = transition_project(
            project, "approved", lifecycle="APPROVED",
            reason=f"owner saved script edit v{next_version}",
        )
    EditProjectStore().save(project_id, project)
    return {"ok": True, "version": next_version, "project": public_project(EditProjectStore().get(project_id))}


@app.post("/api/edit-projects/{project_id}/approve")
async def edit_projects_approve(project_id: int, req: EditPlanApprovalRequest):
    try:
        row = _edit_row(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    project = row["report"]
    versions = project.get("plan_versions") or []
    if not versions:
        return JSONResponse({"error": "승인할 편집안이 없습니다."}, status_code=409)
    latest = int(versions[-1]["version"])
    requested = req.version if req.version is not None else latest
    if requested != latest:
        return JSONResponse({"error": "과거 버전은 승인할 수 없습니다. 최신 수정안을 확인해주세요."}, status_code=409)
    if project.get("status") in {"queued", "rendering"}:
        return JSONResponse({"error": "이미 렌더링 중입니다."}, status_code=409)
    project["approved_version"] = latest
    project["approved_at"] = utc_now()
    project = transition_project(project, "approved", lifecycle="APPROVED", reason=f"owner approved v{latest}")
    versions[-1]["status"] = "approved"
    project["approved_plan_snapshot"] = json.loads(json.dumps(versions[-1], ensure_ascii=False))
    project["conversation"] = (project.get("conversation") or []) + [
        {"role": "user", "content": f"편집안 v{latest} 승인", "created_at": utc_now(), "version": latest}
    ]
    try:
        project["approval_memory_id"] = record_approved_edit_memory(
            project_id, project
        )
    except Exception:
        project["approval_memory_id"] = None
    EditProjectStore().save(project_id, project)
    return {"ok": True, "project": public_project(EditProjectStore().get(project_id))}


async def _run_edit_render_job(
    project_id: int, project: dict, version_row: dict, approved_version: int
) -> None:
    """Keep ffmpeg alive even when the browser's SSE connection closes."""

    started = time.perf_counter()
    store = EditProjectStore()
    renderer = EditRenderService()
    try:
        source = store.resolve_media_path(project, "source")
        directory = store.project_dir(str(project["project_uuid"]))
        render_future = asyncio.create_task(
            asyncio.to_thread(
                renderer.render_project,
                source=source,
                directory=directory,
                plan=version_row["plan"],
                media=(project.get("source") or {}).get("media") or {},
                version=int(approved_version),
            )
        )
        while not render_future.done():
            try:
                await asyncio.wait_for(asyncio.shield(render_future), timeout=15)
            except TimeoutError:
                project.setdefault("storage_state", {})["render_heartbeat_at"] = utc_now()
                store.save(project_id, project)
        outputs, edit_log = render_future.result()
        project["outputs"] = outputs
        project["applied_edit_log"] = edit_log
        project["advisory_edit_log"] = renderer.advisory_log(version_row["plan"])
        project["render_runs"] = (project.get("render_runs") or []) + [
            {
                "version": int(approved_version), "created_at": utc_now(),
                "outputs": outputs, "edit_log": edit_log,
            }
        ]
        project["status"] = "completed"
        project["error"] = None
        project.setdefault("timings", {})["render_seconds"] = round(
            time.perf_counter() - started, 3
        )
        project.setdefault("storage_state", {})["render_completed_at"] = utc_now()
        project["storage_state"].pop("render_heartbeat_at", None)
        store.save(project_id, project)
    except asyncio.CancelledError:
        project["status"] = "render_failed"
        project["error"] = "RenderInterrupted: 서비스 재시작으로 렌더링이 중단됐습니다. 원본은 보존되었습니다."
        store.save(project_id, project)
        raise
    except Exception as exc:
        project["status"] = "render_failed"
        project["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        store.save(project_id, project)
    finally:
        with EDIT_RENDERING_LOCK:
            EDIT_RENDERING.discard(project_id)
        EDIT_RENDER_TASKS.pop(project_id, None)


@app.post("/api/edit-projects/{project_id}/render/preview/restart-proxy")
async def edit_projects_preview_restart_proxy(project_id: int, force: bool = False):
    try:
        row = _edit_row(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    project = row["report"]
    approved_version = int(project.get("approved_version") or 0)
    version_row = next((
        row for row in project.get("plan_versions") or []
        if int(row.get("version") or 0) == approved_version
    ), None)
    restart_job_type = "preview_rendering" if approved_version and version_row else "analysis"
    if (project.get("outputs") or {}).get("preview") and not force and restart_job_type == "preview_rendering":
        return JSONResponse({"error": "이미 preview 파일이 생성되었습니다."}, status_code=409)
    source = project.get("source") or {}
    if source.get("storage_backend") != "object" or not source.get("object_key"):
        return JSONResponse({"error": "Object Storage 원본을 찾지 못했습니다."}, status_code=410)
    try:
        backend = object_storage_from_env()
        if backend is None:
            raise RuntimeError("Object Storage not configured")
        await asyncio.to_thread(backend.head, source["object_key"])
    except Exception:
        return JSONResponse({"error": "Object Storage 원본을 확인하지 못했습니다."}, status_code=503)

    jobs = await asyncio.to_thread(EDIT_JOB_QUEUE.list, project_id=project_id, limit=100)
    candidate = next((
        job for job in jobs
        if job.get("status") in {"running", "queued"}
        and (force or (_seconds_since(job.get("heartbeat_at") or job.get("started_at")) or 0) >= 600)
    ), None)
    candidate = candidate or _preview_restart_candidate(project, jobs) or next((
        job for job in jobs if job.get("status") in {"failed", "stale_rendering"} and job.get("retry_needed")
    ), None)
    recoverable_project = project.get("status") in {
        "preview_rendering", "running", "uploading", "direct_uploading",
        "failed_retry_needed", "analysis_failed", "render_failed", "uploaded",
    }
    if not candidate and not recoverable_project:
        return JSONResponse({"error": "새 job으로 복구할 멈춘 작업이 없습니다."}, status_code=409)
    fresh_active = next((job for job in jobs if job.get("status") in {"running", "queued"}), None)
    if fresh_active and not candidate and not force:
        return JSONResponse({"error": "worker heartbeat가 살아 있어 기존 job을 계속 처리 중입니다."}, status_code=409)
    active_new = next((
        job for job in jobs
        if job.get("type") == "preview_rendering"
        and job.get("status") in {"queued", "running"}
        and candidate and int(job["job_id"]) != int(candidate["job_id"])
    ), None)
    if active_new and not force:
        response_project = public_project(EditProjectStore().get(project_id))
        response_project["current_job"] = active_new
        return {"ok": True, "job": active_new, "project": response_project, "reused": True}

    old_job = None
    if candidate:
        try:
            marker = (
                EDIT_JOB_QUEUE.mark_stale_rendering
                if not force and candidate.get("type") == "preview_rendering" and candidate.get("status") in {"running", "failed", "stale_rendering"}
                else EDIT_JOB_QUEUE.mark_replaced
            )
            old_job = await asyncio.to_thread(marker, int(candidate["job_id"]))
        except (KeyError, RuntimeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)
    replaced_id = int((old_job or {}).get("job_id") or 0)
    job = await asyncio.to_thread(
        EDIT_JOB_QUEUE.enqueue,
        project_id, restart_job_type,
        idempotency_key=(
            f"proxy-restart:{project_id}:{replaced_id}:{time.time_ns()}"
        ),
        payload={
            "approved_version": approved_version or None, "profile": "preview_720p",
            "proxy_restart": True, "replaces_job_id": replaced_id or None,
        },
        max_attempts=2, priority=50,
        defer_seconds=1 if EDIT_JOB_WORKER._task is not None else 0,
    )
    project["jobs"] = sorted(set((project.get("jobs") or []) + [int(job["job_id"])]))
    project["preview_state"] = "queued" if restart_job_type == "preview_rendering" else project.get("preview_state") or "not_requested"
    project["preview_error"] = None
    project["retry_needed"] = False
    project["processing_message"] = "720p 프록시 생성 중"
    project["proxy_workflow_stage"] = "proxy_queued"
    project["processing_output"] = {}
    project["processing_progress"] = {
        "current_stage": "proxy_generation", "stage_label": "720p 프록시 생성",
        "stage_percent": 0, "overall_percent": 10, "updated_at": utc_now(),
    }
    project["replaced_preview_job_id"] = replaced_id or None
    proxy = project.get("proxy") or {}
    if proxy.get("status") != "ready":
        project["proxy"] = {
            "profile": "working_720p", "status": "pending",
            "source_size_bytes": int(source.get("size_bytes") or 0),
        }
    EditProjectStore().save(project_id, project)
    if EDIT_JOB_WORKER._task is None:
        asyncio.create_task(_run_edit_job_without_lifespan(job, EditProjectStore()))
    response_project = public_project(EditProjectStore().get(project_id))
    response_project["current_job"] = job
    return {"ok": True, "old_job": old_job, "job": job, "project": response_project}


@app.post("/api/edit-projects/{project_id}/render/preview")
async def edit_projects_preview_render(project_id: int):
    try:
        row = _edit_row(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    project = row["report"]
    approved_version = int(project.get("approved_version") or 0)
    version_row = next(
        (item for item in project.get("plan_versions") or [] if int(item.get("version") or 0) == approved_version),
        None,
    )
    if not approved_version or not version_row:
        return JSONResponse({"error": "편집안을 먼저 승인해야 합니다."}, status_code=409)
    source = project.get("source") or {}
    if source.get("storage_backend") == "object" and source.get("object_key"):
        try:
            await asyncio.to_thread(object_storage_from_env().head, source["object_key"])
        except Exception:
            return JSONResponse({"error": "Object Storage 원본을 확인하지 못했습니다."}, status_code=503)
    else:
        try:
            EditProjectStore().resolve_media_path(project, "source")
        except FileNotFoundError:
            return JSONResponse({"error": "원본 파일이 없습니다."}, status_code=410)
    preview_jobs = EDIT_JOB_QUEUE.list(project_id=project_id, limit=100)
    if _stale_preview_job(project, preview_jobs):
        return JSONResponse({
            "error": "기존 렌더가 멈췄습니다. 720p 프록시로 다시 시작할 수 있습니다."
        }, status_code=409)
    active = next(
        (
            job for job in preview_jobs
            if job.get("type") == "preview_rendering" and job.get("status") in {"queued", "running"}
        ),
        None,
    )
    if active:
        job = active
    else:
        preview_run = max(
            int(project.get("preview_run_count") or 0) + 1,
            sum(1 for row in preview_jobs if row.get("type") == "preview_rendering") + 1,
        )
        job = EDIT_JOB_QUEUE.enqueue(
            project_id, "preview_rendering",
            idempotency_key=f"preview:{project_id}:v{approved_version}:run{preview_run}",
            payload={"approved_version": approved_version, "profile": "preview_720p"},
            max_attempts=2, priority=55,
            defer_seconds=1 if EDIT_JOB_WORKER._task is not None else 0,
        )
        project["jobs"] = sorted(set((project.get("jobs") or []) + [int(job["job_id"])]))
        project["preview_run_count"] = preview_run
        project["preview_state"] = "queued"
        project["preview_error"] = None
        project["processing_output"] = {}
        project["processing_progress"] = {
            "current_stage": "preview_rendering", "stage_label": "검토용 720p 프리뷰 생성",
            "stage_percent": 0, "overall_percent": 80, "updated_at": utc_now(),
        }
        EditProjectStore().save(project_id, project)
        if EDIT_JOB_WORKER._task is None:
            asyncio.create_task(_run_edit_job_without_lifespan(job, EditProjectStore()))

    async def stream():
        started = time.perf_counter()
        large_proxy_needed = (
            int(source.get("size_bytes") or 0) >= EditPipeline._large_source_threshold()
            and not (project.get("proxy") or {}).get("object_key")
        )
        yield sse({
            "step": "queued", "percent": 3,
            "message": "720p 프록시 생성 중" if large_proxy_needed else "검토용 720p 프리뷰 작업을 준비합니다.",
        })
        while True:
            await asyncio.sleep(1)
            current = EDIT_JOB_QUEUE.get(int(job["job_id"])) or {}
            if current.get("status") in {"completed", "failed", "cancelled", "stale_rendering"}:
                break
            elapsed = time.perf_counter() - started
            current_row = EditProjectStore().get(project_id)
            current_report = (current_row or {}).get("report") or {}
            current_proxy = current_report.get("proxy") or {}
            proxy_ready = bool(current_proxy.get("object_key"))
            yield sse({
                "step": "queued" if current.get("status") == "queued" else "rendering",
                "percent": min(92, 8 + int(elapsed / 3)),
                "message": (
                    "720p 프록시 생성 중" if large_proxy_needed and not proxy_ready else
                    ("검토용 720p 프리뷰 대기 중입니다." if current.get("status") == "queued" else "검토용 720p 프리뷰를 생성 중입니다. 최종 원본 화질 렌더는 승인 후 요청하세요.")
                ),
            })
        latest = EditProjectStore().get(project_id)
        current = EDIT_JOB_QUEUE.get(int(job["job_id"])) or {}
        if current.get("status") == "completed":
            yield sse({"step": "done", "percent": 100, "message": "검토용 720p 프리뷰를 만들었습니다.", "project": public_project(latest)})
        else:
            report = (latest or {}).get("report") or {}
            yield sse({"step": "error", "percent": 100, "message": str(report.get("preview_error") or current.get("error") or "preview 렌더링에 실패했습니다.")[:300]})

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/edit-projects/{project_id}/render")
async def edit_projects_render(project_id: int):
    try:
        row = _edit_row(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    project = row["report"]
    approved_version = project.get("approved_version")
    versions = project.get("plan_versions") or []
    version_row = next(
        (item for item in versions if int(item.get("version") or 0) == int(approved_version or 0)),
        None,
    )
    if not approved_version or not version_row:
        return JSONResponse({"error": "편집안을 먼저 승인해야 합니다."}, status_code=409)
    if project.get("project_mode") == "multisource_roughcut":
        snapshot = project.get("approved_plan_snapshot") or {}
        if int(snapshot.get("version") or 0) != int(approved_version):
            return JSONResponse({"error": "승인된 immutable 구성안이 없습니다."}, status_code=409)
        active = next((
            job for job in EDIT_JOB_QUEUE.list(project_id=project_id, limit=100)
            if job.get("type") == "rough_cut_rendering" and job.get("status") in {"queued", "running"}
        ), None)
        if active:
            return JSONResponse({
                "ok": True, "job": active, "message": "러프컷 작업이 이미 진행 중입니다.",
                "project": public_project(EditProjectStore().get(project_id)),
            }, status_code=202)
        existing = (project.get("outputs") or {}).get("rough_cut") or {}
        if existing:
            return {"ok": True, "reused": True, "project": public_project(EditProjectStore().get(project_id))}
        job = EDIT_JOB_QUEUE.enqueue(
            project_id, "rough_cut_rendering",
            idempotency_key=f"rough-cut:{project_id}:v{approved_version}",
            payload={"approved_version": int(approved_version), "profile": "preview_720p"},
            max_attempts=3, priority=58,
            defer_seconds=1 if EDIT_JOB_WORKER._task is not None else 0,
        )
        project["jobs"] = sorted(set((project.get("jobs") or []) + [int(job["job_id"])]))
        project = transition_project(
            project, "queued", lifecycle="ROUGH_CUT_QUEUED",
            reason="approved multi-source rough cut queued", job_id=int(job["job_id"]),
        )
        EditProjectStore().save(project_id, project)
        if EDIT_JOB_WORKER._task is None:
            asyncio.create_task(_run_edit_job_without_lifespan(job, EditProjectStore()))
        return JSONResponse({
            "ok": True, "job": job,
            "message": "승인된 구성안을 러프컷 안전 작업 큐에 등록했습니다.",
            "project": public_project(EditProjectStore().get(project_id)),
        }, status_code=202)
    external_final = requires_external_final(project)
    selected_job_type = "final_rendering" if external_final else "rendering"
    active_job = next((job for job in EDIT_JOB_QUEUE.list(project_id=project_id, limit=100) if job.get("type") == selected_job_type and job.get("status") in {"queued", "running"}), None)
    if project.get("status") not in {"approved", "render_failed", "completed", "queued", "rendering", "final_queued", "final_rendering"} and not active_job:
        return JSONResponse({"error": "현재 상태에서는 렌더링할 수 없습니다."}, status_code=409)
    store = EditProjectStore()
    source = project.get("source") or {}
    if source.get("storage_backend") == "object" and source.get("object_key"):
        try:
            backend = object_storage_from_env()
            await asyncio.to_thread(backend.head, source["object_key"])
        except Exception:
            return JSONResponse({"error": "Object Storage 원본을 확인하지 못했습니다."}, status_code=503)
    else:
        try:
            store.resolve_media_path(project, "source")
        except FileNotFoundError:
            return JSONResponse(
                {"error": "원본 보존 기간이 끝났거나 원본 파일이 없습니다. 다시 업로드해주세요."},
                status_code=410,
            )
    required_kinds = {"full", "decision"}
    if version_row["plan"].get("create_short_highlight"):
        required_kinds.add("short")
    existing_complete = True
    for kind in required_kinds:
        value = (project.get("outputs") or {}).get(kind) or {}
        if value.get("storage_backend") == "object" and value.get("object_key"):
            try:
                await asyncio.to_thread(object_storage_from_env().head, value["object_key"])
            except Exception:
                existing_complete = False
                break
        else:
            try:
                store.resolve_media_path(project, kind)
            except FileNotFoundError:
                existing_complete = False
                break
    if not existing_complete and source.get("storage_backend") != "object":
        capacity = await asyncio.to_thread(
            EditStorageService(store).estimate_render, project, version_row["plan"]
        )
        if not capacity["enough"]:
            needed_mb = max(1, (capacity["required_bytes"] + 1024 * 1024 - 1) // (1024 * 1024))
            free_mb = capacity["free_bytes"] // (1024 * 1024)
            return JSONResponse(
                {
                    "error": f"렌더링에는 약 {needed_mb}MB가 필요하지만 현재 {free_mb}MB만 남았습니다. 오래된 결과를 먼저 정리해주세요.",
                    "storage": capacity,
                },
                status_code=507,
            )
    if external_final and source.get("storage_backend") != "object":
        return JSONResponse(
            {"error": "대용량 원본 final render는 Object Storage 원본이 필요합니다."},
            status_code=409,
        )
    if external_final and active_job:
        project["final_render_state"] = "rendering" if active_job.get("status") == "running" else "queued"
        store.save(project_id, project)
        return JSONResponse(
            {
                "ok": True, "deferred": True, "job": active_job,
                "message": "4K 원본 final render가 전용 worker 대기열에 있습니다.",
                "project": public_project(store.get(project_id)),
            },
            status_code=202,
        )
    if active_job:
        job = active_job
    elif existing_complete:
        return {"ok": True, "project": public_project(store.get(project_id)), "reused": True}
    elif external_final:
        backend = object_storage_from_env()
        if backend is None:
            return JSONResponse({"error": "Object Storage 연결이 필요합니다."}, status_code=503)
        payload = build_final_render_payload(
            project, approved_version=int(approved_version),
            plan=version_row["plan"], backend=backend,
        )
        validate_final_payload(payload)
        job = EDIT_JOB_QUEUE.enqueue(
            project_id, "final_rendering",
            idempotency_key=f"final:{project_id}:v{approved_version}:run{len(project.get('render_runs') or []) + 1}",
            payload=payload, max_attempts=3, priority=70,
        )
        project["jobs"] = sorted(set((project.get("jobs") or []) + [int(job["job_id"])]))
        project["final_render_state"] = "queued"
        project["final_render_job_id"] = int(job["job_id"])
        project = transition_project(
            project, "final_queued", lifecycle="QUEUED",
            reason="original-resolution render deferred to external worker",
            job_id=int(job["job_id"]),
        )
        project["error"] = None
        store.save(project_id, project)
        return JSONResponse(
            {
                "ok": True, "deferred": True, "job": job,
                "message": "4K 원본은 보존했습니다. final render는 전용 worker 대기열에 등록했습니다.",
                "project": public_project(store.get(project_id)),
            },
            status_code=202,
        )
    else:
        job = EDIT_JOB_QUEUE.enqueue(
            project_id, "rendering",
            idempotency_key=f"render:{project_id}:v{approved_version}:run{len(project.get('render_runs') or []) + 1}",
            payload={"approved_version": int(approved_version)}, max_attempts=2, priority=60,
            defer_seconds=1 if EDIT_JOB_WORKER._task is not None else 0,
        )
        project["jobs"] = sorted(set((project.get("jobs") or []) + [int(job["job_id"])]))
        project = transition_project(project, "queued", lifecycle="QUEUED", reason="approved render queued", job_id=int(job["job_id"]))
        project["error"] = None
        store.save(project_id, project)
        if EDIT_JOB_WORKER._task is None:
            asyncio.create_task(_run_edit_job_without_lifespan(job, store))

    async def stream():
        started = time.perf_counter()
        yield sse({"step": "rendering", "percent": 3, "message": "승인된 타임라인을 검증합니다."})
        while True:
            await asyncio.sleep(2)
            elapsed = time.perf_counter() - started
            current = EDIT_JOB_QUEUE.get(int(job["job_id"])) or {}
            if current.get("status") in {"completed", "failed", "cancelled"}:
                break
            queue_position = None
            if current.get("status") == "queued":
                queue_position = next((item.get("queue_position") for item in EDIT_JOB_QUEUE.snapshot().get("active") or [] if int(item["job_id"]) == int(job["job_id"])), None)
            yield sse({
                "step": "queued" if current.get("status") == "queued" else "rendering",
                "percent": min(92, 8 + int(elapsed / 8)), "queue_position": queue_position,
                "message": (f"안전한 작업 큐 {queue_position or '-'}번째에서 기다리고 있습니다." if current.get("status") == "queued" else f"ffmpeg가 승인된 컷을 렌더링하고 있습니다. ({int(elapsed)}초)"),
            })
        row = store.get(project_id)
        final_project = (row or {}).get("report") or {}
        current = EDIT_JOB_QUEUE.get(int(job["job_id"])) or {}
        if final_project.get("status") == "completed" and current.get("status") == "completed":
            yield sse({
                "step": "done", "percent": 100,
                "message": "승인된 편집본을 만들었습니다.",
                "project": public_project(row),
            })
        else:
            yield sse({
                "step": "error", "percent": 100,
                "message": str(final_project.get("error") or "렌더링에 실패했습니다.")[:300],
            })

    return StreamingResponse(
        stream(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.delete("/api/edit-projects/{project_id}/files")
async def edit_projects_delete_files(project_id: int, scope: str = "all"):
    with EDIT_RENDERING_LOCK:
        active = set(EDIT_RENDERING)
    active.update(
        int(job.get("project_id") or 0)
        for job in (EDIT_JOB_QUEUE.snapshot().get("active") or [])
    )
    try:
        result = await asyncio.to_thread(
            EditStorageService().delete_project_files,
            project_id,
            scope=scope,
            in_memory_active=active,
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError:
        return JSONResponse({"error": "정리 범위가 올바르지 않습니다."}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    result["project"] = public_project(_edit_row(project_id))
    return result


@app.post("/api/edit-projects/{project_id}/media-purge")
async def edit_projects_media_purge(project_id: int, req: EditMediaPurgeRequest):
    if not req.confirmed:
        return JSONResponse(
            {"error": "다운로드/업로드 완료 확인이 필요합니다. 편집 기록과 EDL은 계속 보존됩니다."},
            status_code=409,
        )
    try:
        row = _edit_row(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    project = row["report"]
    if project.get("status") not in {"completed", "published_or_downloaded", "media_purged"}:
        return JSONResponse({"error": "완료된 편집 프로젝트만 미디어를 정리할 수 있습니다."}, status_code=409)
    active = {
        int(job.get("project_id") or 0)
        for job in (EDIT_JOB_QUEUE.snapshot().get("active") or [])
    }
    try:
        result = await asyncio.to_thread(
            EditStorageService().delete_project_files,
            project_id,
            scope="media",
            in_memory_active=active,
        )
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    saved = EditProjectStore().get(project_id)
    latest = saved["report"]
    latest = transition_project(latest, "media_purged", lifecycle="MEDIA_PURGED", reason="owner confirmed media purge")
    latest.setdefault("storage_state", {})["owner_confirmed_purge_at"] = utc_now()
    EditProjectStore().save(project_id, latest)
    result["project"] = public_project(EditProjectStore().get(project_id))
    return result


@app.get("/api/edit-projects/{project_id}/outputs/{kind}")
async def edit_projects_output(project_id: int, kind: str, download: bool = False):
    if kind not in {"preview", "full", "short", "decision", "rough_cut"}:
        return JSONResponse({"error": "출력 파일을 찾지 못했습니다."}, status_code=404)
    try:
        row = _edit_row(project_id)
    except KeyError:
        return JSONResponse({"error": "출력 파일을 찾지 못했습니다."}, status_code=404)
    output = (row["report"].get("outputs") or {}).get(kind) or {}
    if output.get("storage_backend") == "object" and output.get("object_key"):
        backend = object_storage_from_env()
        if backend is None:
            return JSONResponse({"error": "Object Storage 연결을 확인해주세요."}, status_code=503)
        try:
            download_options = {"expires_seconds": 3600}
            if download:
                download_options["download_filename"] = str(
                    output.get("filename") or f"{kind}.mp4"
                )
            url = await asyncio.to_thread(
                backend.presigned_download, output["object_key"], **download_options
            )
        except Exception:
            return JSONResponse({"error": "다운로드 링크를 만들지 못했습니다."}, status_code=503)
        return RedirectResponse(url, status_code=302)
    try:
        path = EditProjectStore().resolve_media_path(row["report"], kind)
    except FileNotFoundError:
        return JSONResponse({"error": "출력 파일을 찾지 못했습니다."}, status_code=404)
    media_type = "application/json" if kind == "decision" else "video/mp4"
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.post("/api/edit-projects/{project_id}/link-upload")
async def edit_projects_link_upload(project_id: int, req: EditProjectUploadLinkRequest):
    video_id = req.video_id.strip()
    if not video_id or len(video_id) > 64:
        return JSONResponse({"error": "YouTube video ID를 입력해주세요."}, status_code=400)
    try:
        row = _edit_row(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    project = row["report"]
    project["upload_feedback"] = {
        "video_id": video_id,
        "linked_at": utc_now(),
        "checkpoints": (project.get("upload_feedback") or {}).get("checkpoints") or [],
        "comparisons": (project.get("upload_feedback") or {}).get("comparisons") or [],
    }
    project = transition_project(
        project, "published_or_downloaded", lifecycle="PUBLISHED_OR_DOWNLOADED",
        reason=f"YouTube video linked: {video_id}",
    )
    strategy_id = (project.get("settings") or {}).get("content_strategy_id")
    if strategy_id:
        try:
            StrategyRepository().link_video(int(strategy_id), video_id)
        except Exception:
            pass
    try:
        comparison = EditFeedbackService().evaluate(project_id, project)
    except Exception:
        comparison = {
            "status": "pending",
            "video_id": video_id,
            "checked_at": utc_now(),
            "message": "성과 데이터가 아직 준비되지 않았습니다. 연결은 저장됐으며 다음 자동 수집에서 다시 확인합니다.",
        }
    project["upload_feedback"]["latest_comparison"] = comparison
    if comparison.get("status") == "measured":
        project["upload_feedback"]["comparisons"] = (
            project["upload_feedback"].get("comparisons") or []
        ) + [comparison]
    EditProjectStore().save(project_id, project)
    job = EDIT_JOB_QUEUE.enqueue(
        project_id, "performance_sync",
        idempotency_key=f"performance:{project_id}:{video_id}:{datetime.date.today().isoformat()}",
        payload={"video_id": video_id}, max_attempts=5, priority=150,
        defer_seconds=1 if EDIT_JOB_WORKER._task is not None else 0,
    )
    project["jobs"] = sorted(set((project.get("jobs") or []) + [int(job["job_id"])]))
    EditProjectStore().save(project_id, project)
    return {"ok": True, "project": public_project(EditProjectStore().get(project_id))}


@app.post("/api/edit-projects/{project_id}/feedback/refresh")
async def edit_projects_refresh_feedback(project_id: int):
    try:
        row = _edit_row(project_id)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    project = row["report"]
    if not str((project.get("upload_feedback") or {}).get("video_id") or "").strip():
        return JSONResponse({"error": "먼저 업로드한 YouTube video ID를 연결해주세요."}, status_code=409)
    evaluation_failed = False
    try:
        result = EditFeedbackService().evaluate(project_id, project)
    except Exception:
        evaluation_failed = True
        result = {
            "status": "pending",
            "video_id": (project.get("upload_feedback") or {}).get("video_id"),
            "checked_at": utc_now(),
            "message": "성과 데이터를 읽지 못했습니다. 이전 정상 비교 결과는 유지됩니다.",
        }
    feedback = project.get("upload_feedback") or {}
    if evaluation_failed:
        feedback["last_refresh_status"] = result
        project["upload_feedback"] = feedback
        EditProjectStore().save(project_id, project)
        return {"ok": True, "project": public_project(EditProjectStore().get(project_id))}
    comparisons = feedback.get("comparisons") or []
    fingerprint = (result.get("source_as_of"), result.get("status"))
    if not comparisons or (
        comparisons[-1].get("source_as_of"), comparisons[-1].get("status")
    ) != fingerprint:
        comparisons.append(result)
    feedback["latest_comparison"] = result
    feedback["comparisons"] = comparisons[-40:]
    project["upload_feedback"] = feedback
    EditProjectStore().save(project_id, project)
    return {"ok": True, "project": public_project(EditProjectStore().get(project_id))}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    async def stream():
        if not req.message.strip():
            yield sse({"error": "메시지를 입력해주세요."})
            return
        try:
            service = StrategyChatService()
            answer_parts: list[str] = []
            trace: list[dict] = []
            started = time.perf_counter()
            event_stream = service.stream_events(
                req.message, req.history, req.attachments
            ).__aiter__()
            next_event = asyncio.create_task(anext(event_stream))
            while True:
                try:
                    event = await asyncio.wait_for(
                        asyncio.shield(next_event), timeout=5
                    )
                except TimeoutError:
                    yield sse(
                        {
                            "ping": True,
                            "progress": "GPT가 확보한 근거를 비교해 답변을 구성하고 있습니다.",
                        }
                    )
                    continue
                except StopAsyncIteration:
                    break
                event_type = event.get("type")
                if event_type == "provider":
                    yield sse({"provider": event.get("provider")})
                elif event_type == "progress":
                    yield sse({"progress": event.get("message")})
                elif event_type == "token":
                    token = str(event.get("token") or "")
                    answer_parts.append(token)
                    yield sse({"token": token})
                elif event_type == "trace":
                    trace = list(event.get("sources") or [])
                    yield sse(
                        {
                            "trace": trace,
                            "intent": event.get("intent"),
                            "duration_ms": event.get("duration_ms"),
                        }
                    )
                next_event = asyncio.create_task(anext(event_stream))
            await asyncio.to_thread(
                remember_interaction,
                req.message,
                "".join(answer_parts),
                trace=trace,
                source_session_id=req.session_id,
            )
            print(
                "[strategy-chat] done "
                f"elapsed={time.perf_counter()-started:.2f}s sources={len(trace)}"
            )
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            message = str(e)[:300] or "AI 전략가 요청에 실패했습니다."
            yield f"data: {json.dumps({'error': message}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/history")
async def history_list(type: str = ""):
    return list_history(type)


@app.get("/api/history/{id}")
async def history_get(id: int, request: Request):
    item = get_history(id)
    if not item:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="없음")
    # 목록에서 걸러도 번호를 직접 넣으면 열린다. 여기서도 주인을 확인한다.
    if current_role(request) == "guest":
        account = ((item.get("report") or {}).get("_project") or {}).get("account")
        if account != "guest":
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


# ── 업로드 예정인데 기획이 안 끝난 건 문자로 독촉 ──────────────────────
# 촬영 전 기획 트랙 7단계. 여기 머물러 있으면 아직 찍을 준비가 안 된 것이다.
PLAN_TRACK = {
    "pick": "영상 고르기", "analyze": "영상 뜯어보기", "collect": "비슷한 영상 모으기",
    "copy": "제목·카피", "thumb": "썸네일", "intro": "도입부 대본", "body": "본문 대본",
    "planning": "기획",
}
SMS_PROXY = "https://bujajubang-analyzer.onrender.com/api/sms/send"
REMIND_PHONE = os.getenv("PIPELINE_REMIND_PHONE", "").strip()
REMIND_SECRET = os.getenv("PIPELINE_REMIND_SECRET", "").strip()


@app.post("/api/pipeline-remind")
async def pipeline_remind(request: Request, days: int = 7, test: int = 0):
    """업로드 예정일이 코앞인데 아직 기획 단계에 머물러 있는 건을 찾아 문자로 알린다.

    라이트세일 cron이 매일 아침 부른다. 해당 건이 없으면 아무것도 보내지 않는다.
    ?test=1 이면 조건과 상관없이 지금 상태를 문자로 한 번 보내본다(설치 확인용).
    """
    supplied_secret = request.headers.get("x-secret", "")
    if not REMIND_SECRET or not secrets.compare_digest(supplied_secret, REMIND_SECRET):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    today = datetime.date.today()
    limit = today + datetime.timedelta(days=days)
    late = []
    for it in list_pipeline():
        d = (it.get("planned_date") or "").strip()
        if not d or it.get("stage") not in PLAN_TRACK:
            continue
        try:
            pd = datetime.date.fromisoformat(d[:10])
        except ValueError:
            continue
        if pd <= limit:                       # 지난 것도 포함 — 이미 늦었으니 더 급하다
            late.append((pd, it))
    late.sort(key=lambda x: x[0])

    if not late and not test:
        return {"ok": True, "sent": False, "reason": "독촉할 건 없음"}

    lines = []
    for pd, it in late[:8]:
        dday = (pd - today).days
        when = "오늘" if dday == 0 else ("D%+d" % dday if dday > 0 else "%d일 지남" % -dday)
        lines.append("· %s(%s) %s — %s 단계"
                     % (pd.strftime("%m/%d"), when, (it.get("title") or "")[:22],
                        PLAN_TRACK.get(it.get("stage"), it.get("stage"))))

    if late:
        msg = ("[부자주방 콘텐츠]\n업로드 예정인데 기획이 안 끝났습니다.\n\n"
               + "\n".join(lines)
               + ("\n외 %d건" % (len(late) - 8) if len(late) > 8 else "")
               + "\n\n기획 마저 진행해 주세요.\nhttps://youtube-researcher.onrender.com/")
    else:
        msg = ("[부자주방 콘텐츠]\n설치 확인용 문자입니다.\n"
               "업로드 예정인데 기획이 안 끝난 건은 지금 없습니다.\n"
               "https://youtube-researcher.onrender.com/")

    if not REMIND_PHONE:
        return JSONResponse(
            {"ok": False, "sent": False, "error": "PIPELINE_REMIND_PHONE 미설정"},
            status_code=503,
        )

    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(SMS_PROXY, json={"phone": REMIND_PHONE, "msg": msg})
            res = r.json()
    except Exception as e:
        return {"ok": False, "sent": False, "msg": msg, "error": str(e)[:200]}

    return {"ok": True, "sent": str(res.get("result_code")) == "1",
            "count": len(late), "msg": msg, "res": res}


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


# ── AI 상담 대화 세션 (저장·목록·이어가기) ──────────────────────────
from database import (list_chat_sessions, get_chat_session, create_chat_session,
                      update_chat_session, delete_chat_session)

@app.get("/api/chat-sessions")
async def chat_sessions_list():
    return list_chat_sessions()

@app.get("/api/chat-sessions/{id}")
async def chat_session_get(id: int):
    s = get_chat_session(id)
    if not s:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="없음")
    return s

@app.post("/api/chat-sessions")
async def chat_session_create(request: Request):
    d = await request.json()
    id_ = create_chat_session((d.get("title", "") or "새 대화")[:80],
                              json.dumps(d.get("messages", []), ensure_ascii=False))
    return {"id": id_}

@app.put("/api/chat-sessions/{id}")
async def chat_session_update(id: int, request: Request):
    d = await request.json()
    title = d.get("title")
    msgs = d.get("messages")
    update_chat_session(id,
                        title=(title[:80] if isinstance(title, str) else None),
                        messages=(json.dumps(msgs, ensure_ascii=False) if msgs is not None else None))
    return {"ok": True}

@app.delete("/api/chat-sessions/{id}")
async def chat_session_delete(id: int):
    delete_chat_session(id)
    return {"ok": True}


# ── 키 컨텐츠 지식 저장소 ─────────────────────────────────────────
from database import (list_knowledge, create_knowledge, update_knowledge, delete_knowledge)

@app.get("/api/knowledge")
async def knowledge_list():
    return list_knowledge()

@app.post("/api/knowledge")
async def knowledge_create(request: Request):
    d = await request.json()
    id_ = create_knowledge(d.get("title", ""), d.get("category", "키컨텐츠"),
                           d.get("summary", ""), d.get("content", ""))
    return {"id": id_}

@app.put("/api/knowledge/{id}")
async def knowledge_update(id: int, request: Request):
    d = await request.json()
    fields = {k: d[k] for k in ("title", "category", "summary", "content", "active") if k in d}
    update_knowledge(id, fields)
    return {"ok": True}

@app.delete("/api/knowledge/{id}")
async def knowledge_delete(id: int):
    delete_knowledge(id)
    return {"ok": True}


@app.post("/api/worksheet/thumbnail")
async def worksheet_thumbnail(request: Request):
    """섬네일 디자인 묘사(+문구)로 gpt-image 예시 썸네일 생성 → 유튜브 16:9 이미지."""
    from image_gen import generate_thumbnail
    body = await request.json()
    design = (body.get("design") or "").strip()
    copy = (body.get("copy") or "").strip()

    async def stream():
        if not os.getenv("OPENAI_API_KEY", "").strip():
            yield sse({"step": "error", "message": "OPENAI_API_KEY 미설정 — Render 환경변수에 추가해주세요."})
            return
        if not design and not copy:
            yield sse({"step": "error", "message": "먼저 섬네일 디자인 또는 문구를 채워주세요."})
            return
        yield sse({"step": "generating", "message": "gpt-image로 섬네일 생성 중... (1~2분 소요)"})
        loop = asyncio.get_event_loop()
        fut = loop.run_in_executor(None, generate_thumbnail, design, copy)
        while not fut.done():
            yield sse({"step": "ping"})
            await asyncio.sleep(4)
        res = fut.result()
        if res.get("b64"):
            yield sse({"step": "done", "image": "data:image/png;base64," + res["b64"]})
        else:
            yield sse({"step": "error", "message": "생성 실패: " + (res.get("error") or "")})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/jjachi")
async def jjachi(request: Request):
    """짜치는 기획 — 사장님이 워크시트에 답한 진짜 현실을 조합해 '짜치지만 공감가는' 기획안."""
    body = await request.json()
    topic = (body.get("topic") or "").strip()
    answers = body.get("answers") or {}
    if not isinstance(answers, dict):
        answers = {}
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    async def stream():
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            yield sse({"step": "error", "message": ".env에 ANTHROPIC_API_KEY를 설정해주세요."})
            return
        if not topic:
            yield sse({"step": "error", "message": "영상 주제를 입력해주세요."})
            return
        videos_with_comments = []
        naver_results = []
        yt = YouTubeService(youtube_key) if youtube_key else None
        try:
            if yt and topic:
                yield sse({"step": "searching", "message": f'"{topic}" 시청자 진짜 반응 수집 중...'})
                videos = await yt.search_videos(topic, max_results=10)
                if videos:
                    videos_with_comments = await yt.get_comments_for_videos(videos[:5])
            if naver_id and naver_secret and topic:
                yield sse({"step": "naver", "message": "네이버 카페 반응 수집 중..."})
                naver = NaverService(naver_id, naver_secret)
                naver_results = await naver.search_cafe(topic)
                await naver.close()
            knowledge = [k for k in list_knowledge(active_only=True)] or None
            yield sse({"step": "writing", "message": "Opus 4.8가 '마음을 얻는 기획' 작성 중... (30~60초)"})
            analyzer = Analyzer()
            _task = asyncio.create_task(analyzer.plan_jjachi(
                topic, answers,
                videos_with_comments or None, naver_results or None, knowledge))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            report = _task.result()
            save_history("jjachi", topic or "감명영상 기획", report)
            yield sse({"step": "done", "report": report, "topic": topic})
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



@app.get("/api/transcript-debug")
async def transcript_debug(request: Request):
    """쿠키/스크립트 수집 진단. ?url=<영상url> 주면 그 영상으로 실제 수집 시도."""
    debug_enabled = os.getenv("ENABLE_TRANSCRIPT_DEBUG", "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if OWNER_AUTH.settings.production or not debug_enabled:
        return JSONResponse({"error": "not found"}, status_code=404)
    import transcript_service as ts
    resolved = ts._cookiefile()
    info = {
        "code_version": "cookie-v8",
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
            allow_whisper = request.query_params.get("whisper", "") == "1"
            loop = asyncio.get_event_loop()
            res = await loop.run_in_executor(None, ts.fetch_transcript, url, allow_whisper)
            diag["whisper_allowed"] = allow_whisper
            diag["fetch_method"] = res.get("method")
            diag["fetch_error"] = res.get("error")
            diag["fetch_len"] = len(res.get("text", ""))
            diag["text_head"] = res.get("text", "")[:200]
        info["url_test"] = diag
    return info


def _yt_video_id(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else ""


@app.post("/api/worksheet/autofill")
async def worksheet_autofill(request: Request):
    """레퍼런스 영상(링크+사용자가 붙여넣은 스크립트) + 댓글·썸네일(비전)·카페·ViewTrap →
    GPT Strategy Brain이 워크시트 카드를 자동 작성한다. (사용자가 스크립트 제공)"""
    body = await request.json()
    keyword = (body.get("keyword") or "").strip()
    brief = (body.get("brief") or "").strip()  # 이번 영상 핵심 내용
    refs_in = body.get("ref_videos") or []  # [{url, script}]
    youtube_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    naver_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    naver_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()

    async def stream():
        if not (
            os.getenv("OPENAI_API_KEY", "").strip()
            or os.getenv("ANTHROPIC_API_KEY", "").strip()
        ):
            yield sse({"step": "error", "message": "AI provider 연결이 설정되어 있지 않습니다."})
            return

        ref_videos = []
        naver_results = []
        yt = YouTubeService(youtube_key) if youtube_key else None
        try:
            # 1) 레퍼런스 영상: 링크 → 제목·썸네일·통계·댓글 (Data API, 차단 없음)
            id_to_script = {}
            ids = []
            for r in refs_in:
                vid = _yt_video_id(r.get("url", ""))
                if vid:
                    ids.append(vid)
                    id_to_script[vid] = (r.get("script") or "").strip()
            if yt and ids:
                yield sse({"step": "fetching", "message": f"레퍼런스 영상 {len(ids)}개 정보(제목·썸네일·댓글) 수집 중..."})
                vids = await yt.get_videos_by_ids(ids)
                vids = await yt.get_comments_for_videos(vids)
                for v in vids:
                    v["script"] = id_to_script.get(v["id"], "")
                ref_videos = vids
                yield sse({"step": "fetched", "message": f"레퍼런스 {len(ref_videos)}개 수집 완료 (스크립트 {sum(1 for v in ref_videos if v.get('script'))}개 포함)"})
            # Data API가 설정되지 않았거나 메타데이터 조회가 비어도 사용자가
            # 직접 제공한 레퍼런스 스크립트는 GPT 기획 근거에서 잃지 않는다.
            fetched_ids = {str(video.get("id") or "") for video in ref_videos}
            for source in refs_in:
                vid = _yt_video_id(source.get("url", ""))
                script = (source.get("script") or "").strip()
                if vid and vid not in fetched_ids and script:
                    ref_videos.append({
                        "id": vid, "title": "(사용자 제공 레퍼런스)",
                        "url": source.get("url", ""), "script": script,
                        "comments": [],
                    })

            # 키워드 미지정 시 첫 레퍼런스 영상 제목에서 보완
            kw = keyword or (ref_videos[0]["title"] if ref_videos else "")

            # 2) 네이버 카페
            if naver_id and naver_secret and kw:
                yield sse({"step": "naver", "message": "네이버 카페 반응 수집 중..."})
                naver = NaverService(naver_id, naver_secret)
                naver_results = await naver.search_cafe(kw)
                await naver.close()

            # 3) ViewTrap
            viewtrap_refs = None
            vt_token = os.getenv("VIEWTRAP_TOKEN", "").strip()
            if vt_token:
                yield sse({"step": "viewtrap", "message": "ViewTrap 레퍼런스 수집 중..."})
                try:
                    svc = ViewTrapService(vt_token)
                    vt_top, vt_hot = await asyncio.gather(svc.get_top_videos(), svc.get_hot_videos())
                    if vt_top or vt_hot:
                        viewtrap_refs = {"top_videos": vt_top[:15], "hot_videos": vt_hot[:15]}
                except Exception:
                    pass

            # 4) OpenAI Strategy Brain 작성. CNMAKER와 완전히 분리된 경로다.
            knowledge = list_knowledge(active_only=True)
            yield sse({"step": "writing", "message": "GPT가 채널 데이터·비즈니스PT·레퍼런스를 연결해 워크시트를 작성 중..."})
            service = WorksheetAIService(
                legacy_factory=(lambda: Analyzer())
                if os.getenv("ANTHROPIC_API_KEY", "").strip() else None
            )
            _task = asyncio.create_task(service.generate(
                kw, ref_videos or None, naver_results or None, viewtrap_refs,
                knowledge or None, brief,
            ))
            while not _task.done():
                yield sse({"step": "ping"})
                await asyncio.sleep(8)
            generation = _task.result()
            data = generation.data
            if "keyword" not in data:
                data["keyword"] = kw
            row_id = create_worksheet_row(json.dumps(data, ensure_ascii=False))
            yield sse({
                "step": "done", "id": row_id, "data": data, "keyword": kw,
                "provider": generation.provider,
                "retrieval_trace": generation.retrieval_trace,
                "retrieval_summary": generation.retrieval_summary,
            })
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
async def root(request: Request):
    headers = {"Cache-Control": "no-store, no-cache, must-revalidate"}
    if current_role(request) == "guest":
        # 친구 화면은 열어 준 기능만 남겨 내려보낸다. 감추기만 하면 소스에 그대로 남는다.
        html = guest_mode.filter_html(Path("static/index.html").read_text(encoding="utf-8"))
        return HTMLResponse(html, headers=headers)
    return FileResponse("static/index.html", headers=headers)

app.mount("/", StaticFiles(directory="static", html=True), name="static")
