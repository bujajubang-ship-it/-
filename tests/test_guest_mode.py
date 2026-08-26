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


class TwoAccountTests(unittest.TestCase):
    """한 사이트에 계정 둘. 친구는 자기 것만, 사장님은 전부 본다."""

    @classmethod
    def setUpClass(cls):
        import os
        import secrets

        os.environ.update({
            "AUTH_MODE": "owner",
            "OWNER_USERNAME": "boss", "GUEST_USERNAME": "friend",
            "AUTH_SIGNING_SECRET": secrets.token_urlsafe(48),
        })
        from owner_auth import OwnerAuthenticator, OwnerAuthSettings, hash_password
        os.environ["OWNER_PASSWORD_HASH"] = hash_password("bossPassword123")
        os.environ["GUEST_PASSWORD_HASH"] = hash_password("friendPassword123")

        import main
        from database import delete_history, save_history
        from fastapi.testclient import TestClient

        # 다른 테스트가 main 을 먼저 불러오면 그때의 설정이 굳는다. 여기서 다시 읽는다.
        main.OWNER_AUTH = OwnerAuthenticator(OwnerAuthSettings.from_env())
        cls._delete_history = staticmethod(delete_history)

        # 같은 DB를 여러 테스트가 함께 쓰므로 이번 판에서 만든 것만 알아볼 수 있게 표를 붙인다.
        cls.tag = secrets.token_hex(4)
        cls.owner_id = save_history(
            "edit", f"사장님 영상 {cls.tag}", {"_project": {"account": "owner"}})
        cls.guest_id = save_history(
            "edit", f"친구 영상 {cls.tag}", {"_project": {"account": "guest"}})
        cls.legacy_id = save_history(
            "edit", f"계정 표시 전에 만든 것 {cls.tag}", {"_project": {}})

        def login(username, password):
            client = TestClient(main.app)
            response = client.post(
                "/api/auth/login", json={"username": username, "password": password})
            assert response.status_code == 200, response.text
            return client

        cls.boss = login("boss", "bossPassword123")
        cls.friend = login("friend", "friendPassword123")

    @classmethod
    def tearDownClass(cls):
        for history_id in (cls.owner_id, cls.guest_id, cls.legacy_id):
            try:
                cls._delete_history(history_id)
            except Exception:
                pass

    def _mine(self, client):
        rows = client.get("/api/edit-feedback/projects").json()
        return [row["keyword"] for row in rows if self.tag in (row.get("keyword") or "")]

    def test_the_friend_sees_only_their_own_work(self):
        self.assertEqual(self._mine(self.friend), [f"친구 영상 {self.tag}"])

    def test_the_owner_sees_everything_including_older_rows(self):
        # 계정 표시가 없던 시절 기록은 사장님 것으로 본다.
        self.assertCountEqual(self._mine(self.boss), [
            f"사장님 영상 {self.tag}",
            f"친구 영상 {self.tag}",
            f"계정 표시 전에 만든 것 {self.tag}",
        ])

    def test_guessing_an_id_does_not_open_someone_elses_work(self):
        """목록에서 걸러도 번호를 직접 넣으면 열리던 길을 막았다."""
        self.assertEqual(self.friend.get(f"/api/history/{self.owner_id}").status_code, 404)
        self.assertEqual(self.friend.get(f"/api/history/{self.guest_id}").status_code, 200)
        self.assertEqual(self.boss.get(f"/api/history/{self.guest_id}").status_code, 200)

    def test_the_page_and_the_api_agree_on_what_is_hidden(self):
        friend_page = self.friend.get("/").text
        self.assertNotIn("Vrew", friend_page)
        self.assertNotIn("tab-knowledge", friend_page)
        self.assertIn("tab-plan-feedback", friend_page)
        self.assertEqual(
            self.friend.get("/api/transcript-edit-guides/projects").status_code, 404)
        owner_page = self.boss.get("/").text
        self.assertIn("Vrew", owner_page)
        self.assertEqual(
            self.boss.get("/api/transcript-edit-guides/projects").status_code, 200)


if __name__ == "__main__":
    unittest.main()
