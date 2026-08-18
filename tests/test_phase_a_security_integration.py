import glob
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from owner_auth import MACHINE_PATHS, PUBLIC_PATHS, hash_password


class PhaseASecurityIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.password = "phase-a-test-password"
        cls.machine_secret = "phase-a-machine-secret"
        cls.environment = patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                # APP_ENV keeps auth production-grade while DB_PATH stays an
                # isolated development SQLite file rather than /data.
                "RENDER": "false",
                "AUTH_MODE": "owner",
                "OWNER_USERNAME": "phase-a-owner",
                "OWNER_PASSWORD_HASH": hash_password(
                    cls.password, salt=b"phase-a-testsalt"
                ),
                "AUTH_SIGNING_SECRET": "phase-a-test-signing-secret-with-at-least-32-bytes",
                "AUTH_ALLOWED_ORIGINS": "https://testserver",
                "PIPELINE_REMIND_SECRET": cls.machine_secret,
                "PIPELINE_REMIND_PHONE": "",
                "CNMAKER_BASE": "",
                "CNMAKER_SECRET": "",
                "ANTHROPIC_API_KEY": "",
                "OPENAI_API_KEY": "",
                "DB_BACKEND": "sqlite",
                "DB_PATH": str(Path(cls.temp_dir.name) / "phase-a.db"),
            },
            clear=False,
        )
        cls.environment.start()
        sys.modules.pop("main", None)
        sys.modules.pop("database", None)
        cls.main = importlib.import_module("main")

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("main", None)
        sys.modules.pop("database", None)
        cls.environment.stop()
        cls.temp_dir.cleanup()

    def client(self):
        return TestClient(self.main.app, base_url="https://testserver")

    def authenticated_client(self):
        client = self.client()
        response = client.post(
            "/api/auth/login",
            json={"username": "phase-a-owner", "password": self.password},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return client, response

    def test_public_health_is_minimal_and_cors_wildcard_is_absent(self):
        response = self.client().get(
            "/healthz", headers={"Origin": "https://untrusted.example"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_pages_and_private_apis_require_login(self):
        client = self.client()
        page = client.get("/", follow_redirects=False)
        self.assertEqual(page.status_code, 303)
        self.assertTrue(page.headers["location"].startswith("/login?next="))

        protected = [
            ("GET", "/api/health"),
            ("GET", "/api/history"),
            ("GET", "/api/knowledge"),
            ("GET", "/api/chat-sessions"),
            ("GET", "/api/pipeline"),
            ("GET", "/api/worksheet"),
            ("POST", "/api/chat"),
            ("POST", "/api/midform"),
            ("POST", "/api/shortform"),
            ("POST", "/api/channel-analyze"),
            ("POST", "/api/video-feedback"),
        ]
        for method, path in protected:
            response = client.request(method, path)
            self.assertEqual(response.status_code, 401, f"{method} {path}")

    def test_every_registered_api_route_is_default_private_or_explicitly_exempt(self):
        for route in self.main.app.routes:
            path = getattr(route, "path", "")
            methods = getattr(route, "methods", set()) or set()
            if not path.startswith("/api/"):
                continue
            if path in PUBLIC_PATHS:
                continue
            if any((method, path) in MACHINE_PATHS for method in methods):
                continue
            self.assertNotIn(path, PUBLIC_PATHS)

    def test_all_eighteen_existing_claude_feature_routes_are_preserved(self):
        expected = {
            "/api/analyze",
            "/api/edit-feedback",
            "/api/planning",
            "/api/intro",
            "/api/script",
            "/api/shortform",
            "/api/midform",
            "/api/detail-page",
            "/api/topic-suggest",
            "/api/yt-search",
            "/api/channel-analyze",
            "/api/video-decision",
            "/api/sns-convert",
            "/api/blog",
            "/api/video-feedback",
            "/api/chat",
            "/api/jjachi",
            "/api/worksheet/autofill",
        }
        post_routes = {
            route.path
            for route in self.main.app.routes
            if "POST" in (getattr(route, "methods", set()) or set())
        }
        self.assertTrue(expected.issubset(post_routes))
        self.assertEqual(len(expected), 18)

    def test_login_cookie_security_and_authenticated_root(self):
        client, login = self.authenticated_client()
        cookie = login.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertIn("path=/", cookie)
        self.assertEqual(client.get("/api/auth/me").json()["username"], "phase-a-owner")
        self.assertEqual(client.get("/").status_code, 200)

    def test_login_failure_limit_returns_429(self):
        client = self.client()
        try:
            for _ in range(self.main.OWNER_AUTH.settings.max_failures):
                response = client.post(
                    "/api/auth/login",
                    json={"username": "phase-a-owner", "password": "wrong-password"},
                )
                self.assertEqual(response.status_code, 401)
            blocked = client.post(
                "/api/auth/login",
                json={"username": "phase-a-owner", "password": "wrong-password"},
            )
            self.assertEqual(blocked.status_code, 429)
            self.assertIn("retry-after", blocked.headers)
        finally:
            self.main.OWNER_AUTH.rate_limiter.clear("testclient")

    def test_crud_routes_still_work_after_authentication(self):
        client, _ = self.authenticated_client()

        knowledge = client.post(
            "/api/knowledge",
            json={
                "title": "보안 회귀 테스트",
                "category": "BusinessPT",
                "summary": "요약",
                "content": "내용",
            },
        )
        self.assertEqual(knowledge.status_code, 200)
        knowledge_id = knowledge.json()["id"]
        self.assertTrue(any(row["id"] == knowledge_id for row in client.get("/api/knowledge").json()))
        self.assertEqual(client.delete(f"/api/knowledge/{knowledge_id}").status_code, 200)

        chat = client.post(
            "/api/chat-sessions", json={"title": "테스트", "messages": []}
        )
        chat_id = chat.json()["id"]
        self.assertEqual(client.get(f"/api/chat-sessions/{chat_id}").status_code, 200)
        self.assertEqual(client.delete(f"/api/chat-sessions/{chat_id}").status_code, 200)

        pipeline = client.post(
            "/api/pipeline",
            json={"title": "테스트 콘텐츠", "stage": "planning"},
        )
        pipeline_id = pipeline.json()["id"]
        self.assertEqual(client.put(f"/api/pipeline/{pipeline_id}", json={"stage": "filming"}).status_code, 200)
        self.assertEqual(client.delete(f"/api/pipeline/{pipeline_id}").status_code, 200)

        worksheet = client.post("/api/worksheet", json={"data": {"title": "테스트"}})
        worksheet_id = worksheet.json()["id"]
        self.assertEqual(client.put(f"/api/worksheet/{worksheet_id}", json={"data": {"title": "수정"}}).status_code, 200)
        self.assertEqual(client.delete(f"/api/worksheet/{worksheet_id}").status_code, 200)

        self.main.save_history("planning", "인증 테스트", {"ok": True})
        history_id = client.get("/api/history").json()[0]["id"]
        self.assertEqual(client.get(f"/api/history/{history_id}").status_code, 200)
        self.assertEqual(client.delete(f"/api/history/{history_id}").status_code, 200)

    def test_existing_claude_chat_sse_contract_reaches_route(self):
        client, _ = self.authenticated_client()
        async def fallback_stream(_service, message, history, attachments):
            yield "anthropic", "fallback-ok"

        with patch.object(self.main.StrategyChatService, "stream", fallback_stream):
            response = client.post(
                "/api/chat", json={"message": "테스트", "history": [], "attachments": []}
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertIn('"provider": "anthropic"', response.text)
        self.assertIn('"token": "fallback-ok"', response.text)

    def test_authenticated_multipart_video_upload_reaches_stream_and_cleans_temp_file(self):
        client, _ = self.authenticated_client()
        before = set(glob.glob("/tmp/vf_*.mp4"))
        response = client.post(
            "/api/video-feedback",
            data={"topic": "인증 테스트"},
            files={"file": ("sample.mp4", b"test-video-body", "video/mp4")},
        )
        after = set(glob.glob("/tmp/vf_*.mp4"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("ANTHROPIC_API_KEY", response.text)
        self.assertEqual(after, before)

    def test_pipeline_remind_keeps_cookie_free_machine_auth(self):
        client = self.client()
        self.assertEqual(client.post("/api/pipeline-remind").status_code, 401)
        with patch.object(self.main, "list_pipeline", return_value=[]):
            response = client.post(
                "/api/pipeline-remind",
                headers={"x-secret": self.machine_secret},
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["sent"])

    def test_transcript_debug_is_disabled_in_production(self):
        client, _ = self.authenticated_client()
        self.assertEqual(client.get("/api/transcript-debug").status_code, 404)

    def test_video_feedback_kv_restore_is_not_an_application_startup_hook(self):
        self.assertFalse(hasattr(self.main, "_vf_restore_if_empty"))


if __name__ == "__main__":
    unittest.main()
