"""경쟁 영상 스크립트(자막) 수집.

전략:
  1) yt-dlp로 한국어 자동자막(automatic_captions) 우선 시도 — 빠르고 서버 부담 적음
  2) 자막이 없으면 오디오 다운로드 후 Whisper(small)로 받아쓰기 — 느리지만 정확
모든 단계는 blocking이라 호출부에서 executor로 돌린다. 실패 시 빈 문자열 반환(봇차단 등).
"""
import os
import re
import base64
import tempfile
import httpx

# 워크시트 자동작성에 쓸 도입부 분석은 앞부분이 핵심 → 길이 상한
MAX_CHARS = 6000

# web 클라이언트는 데이터센터 IP에서 자막·포맷을 제한당함 → android 클라이언트가 우회됨
_YT_CLIENTS = {"youtube": {"player_client": ["android", "web"]}}

# 데이터센터 IP 봇차단의 확실한 우회 = 로그인 쿠키.
# Render 환경변수 YT_COOKIES_B64(cookies.txt를 base64 인코딩)에 넣으면 자동 사용.
_COOKIE_PATH = None


def _cookie_source():
    """원본 쿠키 바이트 (시크릿파일 우선, 없으면 B64). 없으면 None."""
    path = os.getenv("YT_COOKIES_FILE", "").strip()
    if path and os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            pass
    b64 = os.getenv("YT_COOKIES_B64", "").strip()
    if b64:
        try:
            return base64.b64decode(b64)
        except Exception:
            pass
    return None


def _cookiefile():
    """yt-dlp에 넘길 cookies.txt 경로. 없으면 None.
    yt-dlp가 사용 후 쿠키를 되쓰며 깎아먹으므로(차단 재발), 매 호출마다 원본을
    /tmp 사본에 새로 써서 항상 '풀쿠키'로 시작하게 한다."""
    global _COOKIE_PATH
    raw = _cookie_source()
    if raw is None:
        return None
    try:
        if not _COOKIE_PATH:
            fd, _COOKIE_PATH = tempfile.mkstemp(prefix="ytcookies_", suffix=".txt")
            os.close(fd)
        with open(_COOKIE_PATH, "wb") as f:  # 매번 원본으로 리셋
            f.write(raw)
        return _COOKIE_PATH
    except Exception:
        return None


def _base_opts(**extra):
    """공통 yt-dlp 옵션.
    쿠키가 있으면 web 클라이언트(쿠키 인증을 제대로 씀)를, 없으면 android
    클라이언트(무인증 자막 우회)를 쓴다 — android는 쿠키 인증을 무시하기 때문."""
    # 자막만 필요 → 재생 포맷이 없어도 정보(자막)는 받도록 ignore_no_formats_error
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "ignore_no_formats_error": True}
    cf = _cookiefile()
    if cf:
        opts["cookiefile"] = cf
        opts["extractor_args"] = {"youtube": {"player_client": ["web", "android"]}}
    else:
        opts["extractor_args"] = _YT_CLIENTS  # android 우선 (무인증 자막)
    opts.update(extra)
    return opts


def _parse_vtt(text: str) -> str:
    """WebVTT/자막 텍스트에서 타임스탬프·태그·중복을 제거하고 순수 문장만 추출."""
    lines = []
    seen = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "WEBVTT" or "-->" in line or line.isdigit():
            continue
        if line.startswith(("Kind:", "Language:", "NOTE")):
            continue
        line = re.sub(r"<[^>]+>", "", line)          # <c> 등 인라인 태그 제거
        line = re.sub(r"\s+", " ", line).strip()
        if not line or line == seen:                  # 자동자막 흔한 중복 라인 제거
            continue
        lines.append(line)
        seen = line
    return " ".join(lines).strip()


def _fetch_auto_caption(info: dict) -> str:
    """extract_info 결과에서 한국어 자막 트랙을 받아 텍스트로 변환."""
    for store in ("subtitles", "automatic_captions"):
        tracks = (info.get(store) or {})
        for lang in ("ko", "ko-KR", "ko-orig"):
            for track in tracks.get(lang, []):
                if track.get("ext") not in ("vtt", "srt", "ttml", "json3"):
                    continue
                try:
                    r = httpx.get(track["url"], timeout=20.0)
                    r.raise_for_status()
                    if track["ext"] == "json3":
                        import json as _json
                        data = _json.loads(r.text)
                        segs = []
                        for ev in data.get("events", []):
                            for seg in ev.get("segs", []):
                                segs.append(seg.get("utf8", ""))
                        txt = re.sub(r"\s+", " ", "".join(segs)).strip()
                    else:
                        txt = _parse_vtt(r.text)
                    if len(txt) > 50:
                        return txt[:MAX_CHARS]
                except Exception:
                    continue
    return ""


def _whisper_from_audio(video_url: str) -> str:
    """오디오 다운로드 후 Whisper(small)로 받아쓰기. 느림(2~5분). 실패 시 빈 문자열."""
    import yt_dlp
    tmpdir = tempfile.mkdtemp(prefix="ws_audio_")
    out = os.path.join(tmpdir, "a.%(ext)s")
    ydl_opts = _base_opts(
        format="bestaudio/best",
        outtmpl=out,
        postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
    )
    audio_path = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([video_url])
        for f in os.listdir(tmpdir):
            if f.endswith((".mp3", ".m4a", ".webm")):
                audio_path = os.path.join(tmpdir, f)
                break
        if not audio_path:
            return ""
        import whisper
        model = whisper.load_model("small")
        result = model.transcribe(audio_path, language="ko")
        return (result.get("text") or "").strip()[:MAX_CHARS]
    except Exception:
        return ""
    finally:
        try:
            if audio_path and os.path.exists(audio_path):
                os.remove(audio_path)
            os.rmdir(tmpdir)
        except Exception:
            pass


def fetch_transcript(video_url: str, allow_whisper: bool = True) -> dict:
    """경쟁 영상 스크립트 1개 수집. {'text':..., 'method':'caption'|'whisper'|'', 'error':...}"""
    import yt_dlp
    info = None
    try:
        with yt_dlp.YoutubeDL(_base_opts(skip_download=True, writesubtitles=False)) as ydl:
            info = ydl.extract_info(video_url, download=False)
    except Exception as e:
        msg = str(e)
        if "Sign in" in msg or "bot" in msg.lower():
            return {"text": "", "method": "", "error": "youtube_gate"}
        # 메타 추출 실패해도 whisper는 별도 시도 가능
    if info:
        cap = _fetch_auto_caption(info)
        if cap:
            return {"text": cap, "method": "caption", "error": ""}
    if allow_whisper:
        txt = _whisper_from_audio(video_url)
        if txt:
            return {"text": txt, "method": "whisper", "error": ""}
        return {"text": "", "method": "", "error": "no_transcript"}
    return {"text": "", "method": "", "error": "no_caption"}
