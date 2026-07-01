"""gpt-image로 유튜브 섬네일 예시 이미지 생성.
cnmaker와 동일 설정: 모델 gpt-image-2, raw HTTP(urllib), 응답 b64_json → data URL.
"""
import os
import io
import base64
import json
import urllib.request
import urllib.error

OPENAI_URL = "https://api.openai.com/v1/images/generations"
MODEL = "gpt-image-2"  # gpt-image-1도 동작
# gpt-image 네이티브 가로 최대 = 1536x1024(3:2). 유튜브 썸네일(16:9)로 크롭·리사이즈.
GEN_SIZE = "1536x1024"
YT_W, YT_H = 1280, 720  # 유튜브 썸네일 규격 16:9


def _to_youtube_16x9(b64_png: str) -> str:
    """생성된 3:2 이미지를 16:9(1280x720)로 중앙 크롭·리사이즈. 실패 시 원본 반환."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(base64.b64decode(b64_png))).convert("RGB")
        w, h = im.size
        target = YT_W / YT_H
        if w / h > target:            # 너무 넓으면 좌우 크롭
            nw = int(h * target); x = (w - nw) // 2
            im = im.crop((x, 0, x + nw, h))
        else:                          # 너무 높으면 상하 크롭
            nh = int(w / target); y = (h - nh) // 2
            im = im.crop((0, y, w, y + nh))
        im = im.resize((YT_W, YT_H), Image.LANCZOS)
        buf = io.BytesIO(); im.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return b64_png


def _build_prompt(design: str, copy: str) -> str:
    parts = [
        "Create a bold, high-contrast YouTube thumbnail for a Korean YouTube channel (landscape 16:9 style).",
        "Eye-catching, one clear focal subject, punchy colors, professional quality.",
    ]
    if design.strip():
        parts.append(f"Design details: {design.strip()}")
    if copy.strip():
        lines = [l.strip() for l in copy.strip().splitlines() if l.strip()]
        if lines:
            parts.append("Overlay the following Korean text large and bold on the thumbnail, "
                         "spelled EXACTLY and clearly legible: " + " / ".join(lines[:3]))
    parts.append("All Korean text must be rendered accurately (correct spelling) and highly legible.")
    return " ".join(parts)


def generate_thumbnail(design: str, copy: str = "") -> dict:
    """{'b64': 유튜브16:9 base64png, 'error': ''} — 실패 시 b64 빈 문자열."""
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return {"b64": "", "error": "OPENAI_API_KEY 미설정"}
    body = json.dumps({
        "model": MODEL,
        "prompt": _build_prompt(design, copy),
        "size": GEN_SIZE,
        "quality": "high",
        "n": 1,
    }).encode()
    req = urllib.request.Request(
        OPENAI_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            data = json.load(r)
        b64 = _to_youtube_16x9(data["data"][0]["b64_json"])
        return {"b64": b64, "error": ""}
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", {}).get("message", "")[:200]
        except Exception:
            msg = f"HTTP {e.code}"
        return {"b64": "", "error": msg}
    except Exception as e:
        return {"b64": "", "error": str(e)[:200]}
