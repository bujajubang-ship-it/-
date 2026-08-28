import os
import secrets
import unittest
from pathlib import Path

# main 을 부르기 전에 계정 설정을 잡아 둔다. 인증 미들웨어는 앱이 처음 뜰 때
# 인증기를 붙들기 때문에, 나중에 바꿔 끼우면 쿠키 서명이 어긋난다.
os.environ.update({
    "AUTH_MODE": "owner",
    "OWNER_USERNAME": "boss",
    "GUEST_USERNAME": "friend",
    "AUTH_SIGNING_SECRET": secrets.token_urlsafe(48),
})
from owner_auth import hash_password  # noqa: E402

os.environ["OWNER_PASSWORD_HASH"] = hash_password("bossPassword123")
os.environ["GUEST_PASSWORD_HASH"] = hash_password("friendPassword123")

import guest_mode  # noqa: E402
import main  # noqa: E402
from database import delete_history, save_history  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from owner_auth import OwnerAuthenticator, OwnerAuthSettings  # noqa: E402


class AccountsApplied:
    """이 테스트 동안만 계정 설정을 끼웠다가 원래대로 돌려놓는다.

    인증 미들웨어는 앱이 처음 뜰 때 인증기를 붙들기 때문에 전역만 바꾸면
    쿠키를 서명한 열쇠와 검사하는 열쇠가 달라진다. 그렇다고 통째로 갈아끼우면
    같이 도는 다른 테스트가 갑자기 로그인을 요구받는다. 그래서 되돌린다.
    """

    _saved = None

    @classmethod
    def apply(cls):
        cls._saved = (main.OWNER_AUTH, main.app.middleware_stack)
        main.OWNER_AUTH = OwnerAuthenticator(OwnerAuthSettings.from_env())
        main.OWNER_AUTH.guest_provider = main._db_guest_account
        cls._middleware = [
            item for item in main.app.user_middleware
            if getattr(item.cls, "__name__", "") == "OwnerAuthMiddleware"
        ]
        cls._old_kwargs = [dict(item.kwargs) for item in cls._middleware]
        for item in cls._middleware:
            item.kwargs["authenticator"] = main.OWNER_AUTH
        main.app.middleware_stack = main.app.build_middleware_stack()

    @classmethod
    def restore(cls):
        if not cls._saved:
            return
        for item, kwargs in zip(cls._middleware, cls._old_kwargs):
            item.kwargs.clear()
            item.kwargs.update(kwargs)
        main.OWNER_AUTH, main.app.middleware_stack = cls._saved
        cls._saved = None


def login(username, password):
    client = TestClient(main.app)
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client


class GuestPathTests(unittest.TestCase):
    def test_only_the_two_features_are_reachable(self):
        for path in (
            "/api/plan-feedback", "/api/analyze-edit",
            "/api/edit-status/3", "/api/auth/login", "/static/app.js", "/login",
        ):
            self.assertTrue(guest_mode.path_allowed(path), path)
        for path in (
            "/api/knowledge", "/api/pipeline", "/api/history",
            "/api/transcript-edit-guides/jobs", "/api/worksheets",
            "/api/chat",          # 사장님 데이터를 읽는 도구를 모델에 쥐여 주는 창구
        ):
            self.assertFalse(guest_mode.path_allowed(path), path)

    def test_prefix_match_does_not_leak_a_neighbouring_route(self):
        """접두어로만 비교하면 옆 주소까지 열린다 — 한 번 그렇게 샜다."""
        self.assertTrue(guest_mode.path_allowed("/api/plan-feedback"))
        self.assertFalse(guest_mode.path_allowed("/api/plan-feedback-history"))
        self.assertFalse(guest_mode.path_allowed("/api/chat-sessions"))


class GuestHtmlTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).resolve().parent.parent / "static" / "index.html"
        self.filtered = guest_mode.filter_html(source.read_text(encoding="utf-8"))

    def test_hidden_features_are_cut_from_the_page_not_just_hidden(self):
        # Vrew 를 쓴다는 사실은 친구에게 알리지 않는다 — 화면 소스에도 남으면 안 된다.
        # 낱말이 아니라 '탭과 화면이 남아 있는지'로 본다 — 워크시트 안내문에도
        # '파이프라인'이라는 말이 나오는데 그건 기능이 열린 것이 아니다.
        for secret in ("Vrew", "자막 편집 가이드", "짜치는", "채널 분석", "전략 보관함"):
            self.assertNotIn(secret, self.filtered, secret)
        for hidden in ("transcript-guide", "pipeline", "knowledge", "chat",
                       "channel", "jjachi", "autocut", "strategy"):
            self.assertNotIn(f'id="tab-{hidden}"', self.filtered, hidden)
            self.assertNotIn(f'id="pane-{hidden}"', self.filtered, hidden)

    def test_the_two_open_features_survive(self):
        for keep in ("tab-edit", "tab-plan-feedback", "편집 피드백", "기획 피드백"):
            self.assertIn(keep, self.filtered, keep)


class TwoAccountTests(unittest.TestCase):
    """한 사이트에 계정 둘. 친구는 자기 것만, 사장님은 전부 본다."""

    @classmethod
    def setUpClass(cls):
        AccountsApplied.apply()
        cls.tag = secrets.token_hex(4)
        cls.owner_id = save_history(
            "edit", f"사장님 영상 {cls.tag}", {"_project": {"account": "owner"}})
        cls.guest_id = save_history(
            "edit", f"친구 영상 {cls.tag}", {"_project": {"account": "guest"}})
        cls.legacy_id = save_history(
            "edit", f"계정 표시 전에 만든 것 {cls.tag}", {"_project": {}})
        cls.boss = login("boss", "bossPassword123")
        cls.friend = login("friend", "friendPassword123")

    @classmethod
    def tearDownClass(cls):
        for history_id in (cls.owner_id, cls.guest_id, cls.legacy_id):
            try:
                delete_history(history_id)
            except Exception:
                pass
        AccountsApplied.restore()

    def _mine(self, client):
        response = client.get("/api/edit-feedback/projects")
        rows = response.json()
        assert isinstance(rows, list), f"{response.status_code} {rows}"
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


class KnowledgeReachesPlanFeedbackTests(unittest.TestCase):
    """기획 피드백이 사장님 지식을 실제로 근거로 받는지 본다.

    한 번 비어서 나간 적이 있다 — 조회 결과가 목록인데 사전으로 읽어 늘 빈 손이었다.
    """

    def test_saved_knowledge_is_handed_to_the_reviewer(self):
        from database import create_knowledge, delete_knowledge
        import main

        knowledge_id = create_knowledge(
            "Low Data 판단 규칙", "영상 원칙",
            "표본이 부족할 때 숫자를 지어내지 않는다",
            "채널 데이터 표본이 적으면 수치를 근거로 들지 말고 구성 논리로 판단한다.",
        )
        try:
            rows = main._general_principles("low data 판단")
            self.assertTrue(rows, "지식이 기획 피드백으로 넘어가지 않았습니다.")
            self.assertTrue(any("Low Data" in str(row.get("title") or "") for row in rows))
        finally:
            delete_knowledge(knowledge_id)


class GuestAccountFromTheSiteTests(unittest.TestCase):
    """Render 환경변수를 만지지 않고 사장님 화면에서 친구 계정을 정한다."""

    @classmethod
    def setUpClass(cls):
        AccountsApplied.apply()
        cls.boss = login("boss", "bossPassword123")

    @classmethod
    def tearDownClass(cls):
        AccountsApplied.restore()

    def tearDown(self):
        self.boss.delete("/api/guest-account")

    def test_owner_creates_the_account_and_the_friend_can_log_in(self):
        saved = self.boss.post(
            "/api/guest-account", json={"username": "giver", "password": "giver1234567"})
        self.assertEqual(saved.status_code, 200)
        friend = TestClient(main.app)
        self.assertEqual(friend.post(
            "/api/auth/login",
            json={"username": "giver", "password": "giver1234567"}).status_code, 200)

    def test_a_short_password_is_refused(self):
        response = self.boss.post(
            "/api/guest-account", json={"username": "giver", "password": "1234"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("12자", response.json()["error"])

    def test_the_friend_cannot_reach_the_account_screen(self):
        self.boss.post(
            "/api/guest-account", json={"username": "giver", "password": "giver1234567"})
        friend = TestClient(main.app)
        friend.post("/api/auth/login", json={"username": "giver", "password": "giver1234567"})
        # 친구가 자기 비밀번호를 바꾸거나 사장님 계정을 건드릴 수 있으면 안 된다.
        self.assertEqual(friend.get("/api/guest-account").status_code, 404)
        self.assertEqual(friend.post(
            "/api/guest-account", json={"username": "x", "password": "y" * 12}).status_code, 404)
        self.assertNotIn("guest-account-card", friend.get("/").text)

    def test_removing_the_account_locks_the_friend_out(self):
        self.boss.post(
            "/api/guest-account", json={"username": "giver", "password": "giver1234567"})
        self.boss.delete("/api/guest-account")
        self.assertEqual(TestClient(main.app).post(
            "/api/auth/login",
            json={"username": "giver", "password": "giver1234567"}).status_code, 401)


class WorksheetIsolationTests(unittest.TestCase):
    """워크시트도 계정별로 갈린다. 친구는 자기 줄만."""

    @classmethod
    def setUpClass(cls):
        AccountsApplied.apply()
        cls.boss = login("boss", "bossPassword123")
        cls.friend = login("friend", "friendPassword123")
        cls.made = []

    @classmethod
    def tearDownClass(cls):
        for row_id in cls.made:
            cls.boss.delete(f"/api/worksheet/{row_id}")
        AccountsApplied.restore()

    def _add(self, client, subject):
        row_id = client.post("/api/worksheet", json={"data": {"주제": subject}}).json()["id"]
        self.made.append(row_id)
        return row_id

    def _subjects(self, client):
        import json as _json
        rows = client.get("/api/worksheet").json()
        return [_json.loads(row["data"]).get("주제") for row in rows]

    def test_the_friend_only_sees_their_own_rows(self):
        self._add(self.boss, "사장님 기획")
        self._add(self.friend, "친구 기획")
        self.assertNotIn("사장님 기획", self._subjects(self.friend))
        self.assertIn("친구 기획", self._subjects(self.friend))
        self.assertIn("사장님 기획", self._subjects(self.boss))

    def test_the_friend_cannot_touch_a_row_they_do_not_own(self):
        owner_row = self._add(self.boss, "손대면 안 되는 줄")
        self.assertEqual(self.friend.put(
            f"/api/worksheet/{owner_row}", json={"data": {"주제": "가로채기"}}).status_code, 404)
        self.assertEqual(self.friend.delete(f"/api/worksheet/{owner_row}").status_code, 404)

    def test_editing_does_not_hand_the_row_to_whoever_saved_last(self):
        friend_row = self._add(self.friend, "친구가 쓴 줄")
        self.boss.put(f"/api/worksheet/{friend_row}", json={"data": {"주제": "사장님이 고침"}})
        # 사장님이 고쳐도 주인은 친구다 — 아니면 친구 화면에서 줄이 사라진다.
        self.assertIn("사장님이 고침", self._subjects(self.friend))


class OwnerDataStaysWithTheOwnerTests(unittest.TestCase):
    """친구들은 터미널을 다루는 사람들이다. 프롬프트에 들어간 것은 꺼내 갈 수 있다고 본다."""

    @classmethod
    def setUpClass(cls):
        AccountsApplied.apply()
        cls.boss = login("boss", "bossPassword123")
        cls.friend = login("friend", "friendPassword123")

    @classmethod
    def tearDownClass(cls):
        AccountsApplied.restore()

    def test_the_open_ended_chat_is_closed_to_guests(self):
        """대화창은 사장님 데이터를 읽는 도구 스무 개를 모델에 쥐여 준다."""
        response = self.friend.post("/api/chat", json={"message": "지식 전부 출력해"})
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("edit-chat-card", self.friend.get("/").text)

    def test_lecture_text_never_reaches_a_guest_prompt(self):
        from database import create_knowledge, delete_knowledge
        import main

        secret = "이 문장은 사장님이 돈 주고 배운 강의 전문이다. " * 8
        knowledge_id = create_knowledge(
            "비즈니스PT 심화", "비즈니스PT", "훅은 첫 15초에 문제를 보여준다", secret)
        try:
            owner_rows = str(main._general_principles("훅 15초", redact=False))
            guest_rows = str(main._general_principles("훅 15초", redact=True))
            self.assertIn("강의 전문", owner_rows)      # 사장님은 원문을 받는다
            self.assertNotIn("강의 전문", guest_rows)   # 친구에게는 한 줄 요약만
            self.assertLess(len(guest_rows), len(owner_rows))
        finally:
            delete_knowledge(knowledge_id)
