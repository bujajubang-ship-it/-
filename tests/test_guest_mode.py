import unittest
from pathlib import Path

import guest_mode


class GuestPathTests(unittest.TestCase):
    def test_only_the_two_features_are_reachable(self):
        for path in (
            "/api/chat", "/api/plan-feedback", "/api/analyze-edit",
            "/api/edit-status/3", "/api/auth/login", "/static/app.js", "/login",
        ):
            self.assertTrue(guest_mode.path_allowed(path), path)
        for path in (
            "/api/knowledge", "/api/pipeline", "/api/history",
            "/api/transcript-edit-guides/jobs", "/api/worksheets",
        ):
            self.assertFalse(guest_mode.path_allowed(path), path)

    def test_prefix_match_does_not_leak_a_neighbouring_route(self):
        """`/api/chat` 를 열면서 `/api/chat-sessions` 까지 열어 버린 적이 있다."""
        self.assertTrue(guest_mode.path_allowed("/api/chat"))
        self.assertFalse(guest_mode.path_allowed("/api/chat-sessions"))
        self.assertFalse(guest_mode.path_allowed("/api/plan-feedback-history"))


class GuestHtmlTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parent.parent / "static" / "index.html"
        self.filtered = guest_mode.filter_html(source.read_text(encoding="utf-8"))

    def test_hidden_features_are_cut_from_the_page_not_just_hidden(self):
        # Vrew 를 쓴다는 사실은 친구에게 알리지 않는다 — 화면 소스에도 남으면 안 된다.
        for secret in ("Vrew", "자막 편집 가이드", "tab-transcript-guide",
                       "짜치는", "채널 분석", "파이프라인", "전략 보관함"):
            self.assertNotIn(secret, self.filtered, secret)

    def test_the_two_open_features_survive(self):
        for keep in ("tab-edit", "tab-plan-feedback", "편집 피드백", "기획 피드백"):
            self.assertIn(keep, self.filtered, keep)


if __name__ == "__main__":
    unittest.main()
