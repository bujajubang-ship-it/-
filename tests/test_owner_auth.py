import base64
import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from owner_auth import (
    AuthConfigurationError,
    LoginRateLimiter,
    OwnerAuthenticator,
    OwnerAuthMiddleware,
    OwnerAuthSettings,
    hash_password,
    verify_password,
)


TEST_PASSWORD = "test-password-only"
TEST_SIGNING_SECRET = "test-only-signing-secret-with-more-than-32-bytes"


def test_settings(**overrides):
    values = {
        "mode": "owner",
        "production": True,
        "username": "test-owner",
        "password_hash": hash_password(TEST_PASSWORD, salt=b"0123456789abcdef"),
        "signing_secret": TEST_SIGNING_SECRET,
        "session_ttl_seconds": 3600,
        "max_failures": 5,
        "failure_window_seconds": 900,
        "allowed_origins": ("https://testserver",),
    }
    values.update(overrides)
    return OwnerAuthSettings(**values)


class PasswordHashTests(unittest.TestCase):
    def test_scrypt_hash_round_trip_and_wrong_password(self):
        encoded = hash_password(TEST_PASSWORD, salt=b"0123456789abcdef")
        self.assertTrue(encoded.startswith("scrypt_v1$"))
        self.assertNotIn(TEST_PASSWORD, encoded)
        self.assertTrue(verify_password(TEST_PASSWORD, encoded))
        self.assertFalse(verify_password("different-password", encoded))

    def test_short_password_is_rejected_by_hash_generator(self):
        with self.assertRaises(ValueError):
            hash_password("short")

    def test_malformed_hash_fails_closed(self):
        self.assertFalse(verify_password(TEST_PASSWORD, "not-a-valid-hash"))
        with self.assertRaises(AuthConfigurationError):
            test_settings(password_hash="not-a-valid-hash").validate()


class ProductionConfigurationTests(unittest.TestCase):
    def test_render_without_auth_settings_fails_closed(self):
        with patch.dict(os.environ, {"RENDER": "true"}, clear=True):
            with self.assertRaises(AuthConfigurationError):
                OwnerAuthSettings.from_env()

    def test_production_cannot_disable_auth(self):
        with self.assertRaises(AuthConfigurationError):
            test_settings(mode="disabled").validate()

    def test_local_development_keeps_disabled_default(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = OwnerAuthSettings.from_env()
        self.assertFalse(settings.production)
        self.assertFalse(settings.enabled)


class SignedSessionTests(unittest.TestCase):
    def setUp(self):
        self.auth = OwnerAuthenticator(test_settings())

    def test_full_hmac_sha256_signature_and_expiry(self):
        token = self.auth.issue_session(now=1_000)
        _, signature = token.split(".", 1)
        signature += "=" * (-len(signature) % 4)
        self.assertEqual(len(base64.urlsafe_b64decode(signature)), 32)
        self.assertIsNotNone(self.auth.verify_session(token, now=1_100))
        self.assertIsNone(self.auth.verify_session(token, now=4_600))

    def test_tampering_is_rejected(self):
        token = self.auth.issue_session(now=1_000)
        payload, signature = token.split(".", 1)
        replacement = "A" if signature[-1] != "A" else "B"
        self.assertIsNone(
            self.auth.verify_session(f"{payload}.{signature[:-1]}{replacement}", now=1_100)
        )

    def test_credentials_are_checked(self):
        self.assertTrue(self.auth.verify_credentials("test-owner", TEST_PASSWORD))
        self.assertFalse(self.auth.verify_credentials("other", TEST_PASSWORD))
        self.assertFalse(self.auth.verify_credentials("test-owner", "wrong-password"))


class LoginRateLimiterTests(unittest.TestCase):
    def test_failures_are_limited_and_expire(self):
        limiter = LoginRateLimiter(max_failures=3, window_seconds=60)
        for offset in range(3):
            limiter.record_failure("client", now=100 + offset)
        self.assertGreater(limiter.retry_after("client", now=103), 0)
        self.assertEqual(limiter.retry_after("client", now=163), 0)


class MiddlewareTransportTests(unittest.TestCase):
    def setUp(self):
        self.auth = OwnerAuthenticator(test_settings())
        app = FastAPI()
        app.add_middleware(OwnerAuthMiddleware, authenticator=self.auth)

        @app.get("/healthz")
        async def healthz():
            return {"ok": True}

        @app.get("/api/private")
        async def private():
            return {"private": True}

        @app.get("/api/stream")
        async def stream():
            async def events():
                yield "data: first\n\n"
                yield "data: second\n\n"

            return StreamingResponse(events(), media_type="text/event-stream")

        @app.post("/api/upload")
        async def upload(file: UploadFile = File(...)):
            return {"content": (await file.read()).decode("utf-8")}

        self.client = TestClient(app, base_url="https://testserver")

    def _authenticate(self):
        self.client.cookies.set(
            self.auth.settings.cookie_name,
            self.auth.issue_session(),
        )

    def test_default_deny_and_public_health(self):
        self.assertEqual(self.client.get("/healthz").json(), {"ok": True})
        self.assertEqual(self.client.get("/api/private").status_code, 401)

    def test_streaming_response_is_not_buffered_or_rewritten(self):
        self._authenticate()
        response = self.client.get("/api/stream")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertEqual(response.text, "data: first\n\ndata: second\n\n")

    def test_multipart_body_reaches_endpoint_unchanged(self):
        self._authenticate()
        response = self.client.post(
            "/api/upload", files={"file": ("sample.txt", b"body-preserved", "text/plain")}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "body-preserved")

    def test_cross_origin_mutation_is_rejected(self):
        self._authenticate()
        response = self.client.post(
            "/api/upload",
            headers={"Origin": "https://untrusted.example"},
            files={"file": ("sample.txt", b"blocked", "text/plain")},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
