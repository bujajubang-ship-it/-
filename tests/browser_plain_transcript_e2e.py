"""Live browser smoke for the independent transcript editing guide tab.

Run against a local server with a disposable DB:
    python tests/browser_plain_transcript_e2e.py http://127.0.0.1:8765
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


SCRIPT = """안녕하세요. 오늘은 초음파세척기를 3년 사용한 결과를 말씀드리겠습니다.
처음에는 정말 설거지가 줄어드는지 저도 의심했습니다.
하지만 바쁜 점심 영업 뒤에도 그릇에 남은 기름이 확실히 줄었습니다.
먼저 설치 전 주방 동선에서 세척기 위치를 확인해야 합니다.
잔반은 반드시 제거한 다음 초음파세척기에 넣어야 합니다.
잔반을 제거하지 않으면 세척 효과가 떨어집니다.
물을 채우고 제조사가 권장한 온도까지 올립니다.
정확한 온도와 세제 양은 제품 설명서와 현장 화면을 직접 확인해야 합니다.
세척 중에는 제품 작동 화면을 보여주면 이해가 쉽습니다.
세척 후에는 도어타입 세척기로 헹굼을 마무리합니다.
이 설명은 앞에서 말씀드린 잔반 제거와 같은 내용입니다.
모든 식당에 초음파세척기가 필요한 것은 아닙니다.
설거지 양이 적은 매장은 투자비를 먼저 계산해야 합니다.
구매 전에는 핵심 부품의 제조사와 교체 가능 여부를 확인하십시오.
A/S 접수 방식과 방문 가능 지역도 계약 전에 확인해야 합니다.
실제 사용자는 가장 큰 장점으로 저녁 마감 시간을 꼽았습니다.
설치 전과 비교하면 마감 시간이 짧아졌다고 말했습니다.
제품이 맞는지 궁금하다면 현재 주방 동선과 설거지 양을 함께 알려주세요."""


def run(base_url: str) -> dict[str, object]:
    started = time.monotonic()
    download_names: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.goto(f"{base_url}/?tab=transcript-guide", wait_until="networkidle")
        page.locator("#pane-transcript-guide:not(.hidden)").wait_for()
        existing = page.locator("#tg-projects-list .edit-project-card", has_text="[SMOKE] 전체 대본 편집 흐름")
        if existing.count():
            existing.first.click()
            page.locator("#tg-report-section:not(.hidden)").wait_for(timeout=30_000)
        else:
            page.locator("#tg-title-input").fill("[SMOKE] 전체 대본 편집 흐름")
            page.locator("#tg-topic-input").fill("초음파세척기 장기 사용과 구매 기준")
            page.locator("#tg-target-input").fill("5")
            page.locator("#tg-purpose-input").fill("구매 상담")
            page.locator("#tg-script-input").fill(SCRIPT)
            page.locator("#tg-request-input").fill("실사용 증거를 앞에 두고 A/S는 마지막에 배치")
            page.locator("#tg-analyze-btn").click()
            page.locator("#tg-report-section:not(.hidden)").wait_for(timeout=600_000)
        assert page.locator("#tg-overall-list li").count() > 0
        assert page.locator("#tg-table-body tr").count() > 0
        assert "S001" in (page.locator("#tg-sentence-list").text_content() or "")
        assert "[상세 편집 순서]" in (page.locator("#tg-employee-guide-text").text_content() or "")
        history_id = page.evaluate("currentTranscriptGuideProjectId")

        if page.locator("#tg-version-select option").count() < 2:
            page.locator("#tg-revision-input").fill("실사용 후기를 더 앞에 두고 A/S는 마지막으로 보내.")
            page.locator("#tg-revision-btn").click()
            page.locator("#tg-version-select option[value='2']").wait_for(
                state="attached", timeout=600_000,
            )
        page.locator("#tg-version-select").select_option("2")
        assert "v2" in page.locator("#tg-report-title").inner_text()

        page.get_by_role("button", name="📋 직원용 편집 가이드 전체 복사").click()
        page.locator("#tg-revision-status").filter(has_text="복사했습니다").wait_for()
        assert "[상세 편집 순서]" in page.evaluate("navigator.clipboard.readText()")
        for label in ("Markdown 다운로드", "TXT 다운로드"):
            with page.expect_download() as download_info:
                page.get_by_role("button", name=label, exact=True).click()
            download_names.append(download_info.value.suggested_filename)

        page.reload(wait_until="networkidle")
        page.locator(f".edit-project-card[onclick='openTranscriptGuideProject({history_id})']").click()
        page.locator("#tg-report-section:not(.hidden)").wait_for(timeout=30_000)
        assert "v2" in page.locator("#tg-report-title").inner_text()
        assert page.locator("#tg-version-select option").count() == 2
        screenshot = Path("/tmp/transcript-guide-e2e.png")
        page.screenshot(path=str(screenshot), full_page=True)
        browser.close()
    return {
        "history_id": history_id,
        "versions": 2,
        "downloads": download_names,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "screenshot": str(screenshot),
    }


if __name__ == "__main__":
    print(json.dumps(run(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"), ensure_ascii=False))
