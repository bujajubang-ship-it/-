"""친구용 사이트 모드.

같은 코드로 Render 서비스를 하나 더 띄우되, 환경변수 `SITE_MODE=guest` 를 주면
친구에게 열어 줄 기능만 남는다. DB도 따로 쓰므로 사장님 데이터는 아예 들어오지 않는다.

**화면에서 탭을 숨기는 것만으로는 막은 것이 아니다.** 주소를 직접 치면 그대로 열린다.
그래서 서버에서 허용된 길만 통과시킨다.

열어 주는 기능
  · 편집 피드백  — 촬영한 대본을 넣으면 점수와 고칠 점
  · 기획 피드백  — 촬영 전 기획안을 넣으면 점수와 고칠 점

닫는 것 중 특히 중요한 것
  · 자막 편집 가이드 — Vrew 에이전트를 쓰는 방식은 알리지 않는다(사장님 지시)
  · 채널 분석·지식·상담·파이프라인 — 사장님 자산과 개인 기록
"""

from __future__ import annotations

import os
import re

GUEST_TABS = ("edit", "plan-feedback")

# 친구 사이트에서 열어 두는 API.
# 접두어로만 비교하면 `/api/chat` 이 `/api/chat-sessions` 까지 열어 버린다.
# 그래서 정확히 같은 주소와, 뒤에 하위 경로가 붙는 주소를 나눠 둔다.
_ALLOWED_EXACT = frozenset({
    "/api/site-mode",
    "/api/health",
    "/api/chat",                  # 편집 피드백 안에 붙어 있는 후속 질문 창
    "/api/plan-feedback",         # 기획 피드백
    "/api/analyze-edit",          # 편집 피드백
    "/api/edit-feedback",         # 편집 피드백 실행
    "/api/edit-feedback/projects",
})

_ALLOWED_PREFIXES = (
    "/api/auth/",
    "/api/edit-",                 # 편집 피드백 진행 상태·결과
    "/api/history/",              # 자기가 만든 편집 피드백 다시 열기 (주인 확인은 각 화면에서)
    "/api/analyze-edit/",
)

# 로그인 화면과 정적 파일은 언제나 열려 있어야 한다.
_ALWAYS_OPEN = ("/", "/login", "/favicon.ico", "/static", "/assets")


def is_guest() -> bool:
    return os.getenv("SITE_MODE", "").strip().lower() == "guest"


def path_allowed(path: str) -> bool:
    """친구 사이트에서 이 주소를 열어도 되는가."""
    if not path.startswith("/api/"):
        return True                      # 화면·정적 파일은 통과, 내용은 앞단에서 가린다
    return path in _ALLOWED_EXACT or path.startswith(_ALLOWED_PREFIXES)


def hidden_notice() -> str:
    return "이 사이트에서는 제공하지 않는 기능입니다."


_TAB_BUTTON = re.compile(r'<button[^>]*id="tab-([a-z0-9-]+)"[^>]*>.*?</button>\s*', re.S)
_HTML_COMMENT = re.compile(r"<!--(?!\[if).*?-->\s*", re.S)
_PANE_START = re.compile(r'<div id="pane-([a-z0-9-]+)"')

# 친구 화면에 남길 pane. 탭에 안 걸린 pane(기획 단계별 화면 등)은 편집 피드백이 쓰므로 남긴다.
_KEEP_PANES = set(GUEST_TABS) | {
    "decision", "research", "planning", "intro", "script",
}


def _cut_block(html: str, start: int) -> int:
    """`<div ...>` 하나가 닫히는 자리를 찾는다. 여는 div를 세어 짝을 맞춘다."""
    depth = 0
    index = start
    while index < len(html):
        opening = html.find("<div", index)
        closing = html.find("</div>", index)
        if closing < 0:
            return len(html)
        if 0 <= opening < closing:
            depth += 1
            index = opening + 4
            continue
        depth -= 1
        index = closing + 6
        if depth == 0:
            return index
    return len(html)


def filter_html(html: str) -> str:
    """친구에게 보여 줄 화면만 남긴다.

    탭을 자바스크립트로 감추기만 하면 **화면 소스에 내용이 그대로 남는다.**
    자막 편집 가이드 화면에는 Vrew 를 쓴다는 것이 적혀 있어 소스만 열어도 보인다.
    그래서 서버가 내려보내기 전에 잘라 낸다.
    """
    html = _TAB_BUTTON.sub(
        lambda m: m.group(0) if m.group(1) in GUEST_TABS else "", html)
    # 화면을 잘라내도 `<!-- 자막 편집 가이드 탭 -->` 같은 주석이 남아 무슨 기능이 있는지 드러난다.
    html = _HTML_COMMENT.sub("", html)
    while True:
        cut = None
        for match in _PANE_START.finditer(html):
            if match.group(1) not in _KEEP_PANES:
                cut = match
                break
        if cut is None:
            return html
        html = html[:cut.start()] + html[_cut_block(html, cut.start()):]
