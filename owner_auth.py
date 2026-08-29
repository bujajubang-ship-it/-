"""Stateless owner authentication for the private YouTube researcher site.

The password itself is never configured.  Only a versioned scrypt hash is
accepted.  Browser sessions are signed with a full HMAC-SHA256 digest and are
kept in an HttpOnly cookie.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse


PASSWORD_HASH_VERSION = "scrypt_v1"
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
MIN_PASSWORD_LENGTH = 12
SESSION_TOKEN_VERSION = 1

PUBLIC_PATHS = frozenset(
    {
        "/healthz",
        "/login",
        "/api/auth/login",
        "/api/auth/logout",
    }
)
MACHINE_PATHS = frozenset({("POST", "/api/pipeline-remind")})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AuthConfigurationError(RuntimeError):
    """Raised when production authentication is missing or malformed."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _constant_time_text_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _parse_password_hash(encoded: str) -> tuple[bytes, bytes]:
    try:
        version, n_raw, r_raw, p_raw, salt_raw, digest_raw = encoded.split("$", 5)
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        salt = _b64decode(salt_raw)
        digest = _b64decode(digest_raw)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AuthConfigurationError("OWNER_PASSWORD_HASH has an invalid format.") from exc

    if version != PASSWORD_HASH_VERSION or (n, r, p) != (
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
    ):
        raise AuthConfigurationError("OWNER_PASSWORD_HASH uses unsupported parameters.")
    if len(salt) < 16 or len(digest) != SCRYPT_DKLEN:
        raise AuthConfigurationError("OWNER_PASSWORD_HASH has invalid salt or digest data.")
    return salt, digest


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Create the only password-hash format accepted by this application."""

    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must contain at least {MIN_PASSWORD_LENGTH} characters.")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=64 * 1024 * 1024,
    )
    return "$".join(
        (
            PASSWORD_HASH_VERSION,
            str(SCRYPT_N),
            str(SCRYPT_R),
            str(SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt, expected = _parse_password_hash(encoded)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=len(expected),
            maxmem=64 * 1024 * 1024,
        )
        return hmac.compare_digest(actual, expected)
    except (AuthConfigurationError, ValueError, UnicodeError):
        return False


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise AuthConfigurationError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise AuthConfigurationError(f"{name} is outside the allowed range.")
    return value


def _is_render() -> bool:
    value = os.getenv("RENDER", "").strip().lower()
    return value not in {"", "0", "false", "no", "off"}


@dataclass(frozen=True)
class OwnerAuthSettings:
    mode: str
    production: bool
    username: str
    password_hash: str
    signing_secret: str
    # 친구용 계정(선택). 넣으면 같은 사이트에 두 번째 로그인이 생기고,
    # 그 계정으로 들어오면 열어 준 기능만 보인다.
    guest_username: str = ""
    guest_password_hash: str = ""
    cookie_name: str = "yt_owner_session"
    session_ttl_seconds: int = 7 * 24 * 60 * 60
    max_failures: int = 5
    failure_window_seconds: int = 15 * 60
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "OwnerAuthSettings":
        production = _is_render() or os.getenv("APP_ENV", "").strip().lower() == "production"
        raw_mode = os.getenv("AUTH_MODE", "").strip().lower()
        mode = raw_mode or ("owner" if production else "disabled")
        allowed_origins = tuple(
            item.strip().rstrip("/")
            for item in os.getenv("AUTH_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        )
        settings = cls(
            mode=mode,
            production=production,
            username=os.getenv("OWNER_USERNAME", "").strip(),
            password_hash=os.getenv("OWNER_PASSWORD_HASH", "").strip(),
            guest_username=os.getenv("GUEST_USERNAME", "").strip(),
            guest_password_hash=os.getenv("GUEST_PASSWORD_HASH", "").strip(),
            signing_secret=os.getenv("AUTH_SIGNING_SECRET", ""),
            cookie_name=os.getenv("AUTH_COOKIE_NAME", "yt_owner_session").strip(),
            session_ttl_seconds=_env_int(
                "AUTH_SESSION_TTL_SECONDS",
                7 * 24 * 60 * 60,
                minimum=300,
                maximum=90 * 24 * 60 * 60,
            ),
            max_failures=_env_int(
                "AUTH_LOGIN_MAX_FAILURES", 5, minimum=3, maximum=20
            ),
            failure_window_seconds=_env_int(
                "AUTH_LOGIN_WINDOW_SECONDS", 15 * 60, minimum=60, maximum=24 * 60 * 60
            ),
            allowed_origins=allowed_origins,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.mode not in {"disabled", "owner"}:
            raise AuthConfigurationError("AUTH_MODE must be 'disabled' or 'owner'.")
        if self.production and self.mode != "owner":
            raise AuthConfigurationError("AUTH_MODE=owner is required in production.")
        if self.mode == "disabled":
            return
        if not self.username:
            raise AuthConfigurationError("OWNER_USERNAME is required when owner auth is enabled.")
        if not self.password_hash:
            raise AuthConfigurationError(
                "OWNER_PASSWORD_HASH is required when owner auth is enabled."
            )
        _parse_password_hash(self.password_hash)
        if len(self.signing_secret.encode("utf-8")) < 32:
            raise AuthConfigurationError(
                "AUTH_SIGNING_SECRET must contain at least 32 bytes."
            )
        if not self.cookie_name or any(ch in self.cookie_name for ch in " ;,\t\r\n"):
            raise AuthConfigurationError("AUTH_COOKIE_NAME is invalid.")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise AuthConfigurationError("AUTH_ALLOWED_ORIGINS contains an invalid origin.")

    @property
    def enabled(self) -> bool:
        return self.mode == "owner"

    @property
    def secure_cookie(self) -> bool:
        return self.production


class LoginRateLimiter:
    def __init__(self, max_failures: int, window_seconds: int):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        attempts = self._attempts[key]
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()
        if not attempts:
            self._attempts.pop(key, None)
            return deque()
        return attempts

    def retry_after(self, key: str, *, now: float | None = None) -> int:
        current = time.time() if now is None else now
        with self._lock:
            attempts = self._prune(key, current)
            if len(attempts) < self.max_failures:
                return 0
            return max(1, int(attempts[0] + self.window_seconds - current) + 1)

    def record_failure(self, key: str, *, now: float | None = None) -> None:
        current = time.time() if now is None else now
        with self._lock:
            attempts = self._prune(key, current)
            if not attempts:
                attempts = self._attempts[key]
            attempts.append(current)

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


class OwnerAuthenticator:
    def __init__(self, settings: OwnerAuthSettings):
        settings.validate()
        self.settings = settings
        # 친구 계정 목록을 어디서 읽을지. [(아이디, 해시)] 를 준다.
        # 기본은 환경변수 한 벌, 앱이 DB에서 읽도록 바꿔 끼운다.
        self.guest_provider = lambda: (
            [(settings.guest_username, settings.guest_password_hash)]
            if settings.guest_username and settings.guest_password_hash else [])
        self.rate_limiter = LoginRateLimiter(
            settings.max_failures, settings.failure_window_seconds
        )

    def verify_credentials(self, username: str, password: str) -> bool:
        return self.role_for(username, password) is not None

    def role_for(self, username: str, password: str) -> str | None:
        """맞으면 'owner' 또는 'guest'를, 틀리면 None."""
        found = self.identify(username, password)
        return found[0] if found else None

    def identify(self, username: str, password: str) -> tuple[str, str] | None:
        """(역할, 아이디) 를 준다. 친구가 여럿이라 누구인지까지 알아야 한다.

        아이디가 있는지 없는지를 응답 속도로 알아채지 못하게, 사장님 검사는
        언제나 끝까지 돌리고 친구 목록도 도중에 끊지 않는다.
        """
        owner_password = verify_password(password, self.settings.password_hash)
        owner_name = _constant_time_text_equal(username, self.settings.username)
        matched_guest = None
        for guest_username, guest_hash in self._guest_accounts():
            if not (guest_username and guest_hash):
                continue
            ok_password = verify_password(password, guest_hash)
            ok_name = _constant_time_text_equal(username, guest_username)
            if ok_password and ok_name and matched_guest is None:
                matched_guest = guest_username
        if owner_password and owner_name:
            return ("owner", self.settings.username)
        if matched_guest:
            return ("guest", matched_guest)
        return None

    def guest_usernames(self) -> list[str]:
        return [name for name, h in self._guest_accounts() if name and h]

    def _guest_accounts(self) -> list[tuple[str, str]]:
        try:
            rows = self.guest_provider() or []
        except Exception:
            return []
        out = []
        for row in rows:
            try:
                name, password_hash = row
            except (TypeError, ValueError):
                continue
            out.append((str(name or "").strip(), str(password_hash or "").strip()))
        return out

    def issue_session(self, *, now: int | None = None, role: str = "owner",
                      username: str | None = None) -> str:
        issued_at = int(time.time()) if now is None else int(now)
        if role == "guest":
            username = username or (self.guest_usernames() or [""])[0]
        else:
            username = self.settings.username
        payload = {
            "exp": issued_at + self.settings.session_ttl_seconds,
            "iat": issued_at,
            "sub": username,
            "role": role,
            "v": SESSION_TOKEN_VERSION,
        }
        payload_raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        encoded_payload = _b64encode(payload_raw)
        signature = hmac.new(
            self.settings.signing_secret.encode("utf-8"),
            encoded_payload.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{encoded_payload}.{_b64encode(signature)}"

    def verify_session(self, token: str, *, now: int | None = None) -> dict[str, Any] | None:
        try:
            encoded_payload, encoded_signature = token.split(".", 1)
            supplied_signature = _b64decode(encoded_signature)
            expected_signature = hmac.new(
                self.settings.signing_secret.encode("utf-8"),
                encoded_payload.encode("ascii"),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                return None
            payload = json.loads(_b64decode(encoded_payload))
            current = int(time.time()) if now is None else int(now)
            if not isinstance(payload, dict):
                return None
            if payload.get("v") != SESSION_TOKEN_VERSION:
                return None
            # 계정이 둘이므로 사장님·친구 중 어느 쪽 이름인지 본다.
            # 친구 계정을 나중에 껐다면 그 세션은 더 이상 통하지 않아야 한다.
            subject = payload.get("sub")
            role = payload.get("role") or "owner"
            if role == "guest":
                # 계정을 지우면 그 사람의 로그인도 그 자리에서 끊긴다
                if subject not in self.guest_usernames():
                    return None
            elif subject != self.settings.username:
                return None
            issued_at = int(payload.get("iat", 0))
            expires_at = int(payload.get("exp", 0))
            if issued_at > current + 60 or expires_at <= current or expires_at <= issued_at:
                return None
            return payload
        except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            return None

    def request_session(self, request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(self.settings.cookie_name, "")
        return self.verify_session(token) if token else None

    def origin_is_allowed(self, request: Request) -> bool:
        origin = request.headers.get("origin", "").rstrip("/")
        if not origin:
            return True
        host = request.headers.get("host", "")
        allowed = set(self.settings.allowed_origins)
        if host:
            allowed.update({f"https://{host}", f"http://{host}"})
        return origin in allowed


class OwnerAuthMiddleware:
    """Pure ASGI middleware so request bodies and streaming responses stay untouched."""

    def __init__(self, app: Any, authenticator: OwnerAuthenticator):
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not self.authenticator.settings.enabled:
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if path in PUBLIC_PATHS or (method, path) in MACHINE_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        session = self.authenticator.request_session(request)
        if session is None:
            if path.startswith("/api/"):
                response = JSONResponse(
                    {"error": "로그인이 필요합니다."},
                    status_code=401,
                    headers={"Cache-Control": "no-store"},
                )
            else:
                target = path
                query = scope.get("query_string", b"").decode("latin-1")
                if query:
                    target = f"{target}?{query}"
                response = RedirectResponse(
                    f"/login?next={quote(target, safe='')}",
                    status_code=303,
                    headers={"Cache-Control": "no-store"},
                )
            await response(scope, receive, send)
            return

        if method in UNSAFE_METHODS and not self.authenticator.origin_is_allowed(request):
            response = JSONResponse(
                {"error": "허용되지 않은 요청 출처입니다."}, status_code=403
            )
            await response(scope, receive, send)
            return

        scope.setdefault("state", {})["owner"] = session.get("sub")
        await self.app(scope, receive, send)


def _password_hash_cli() -> int:
    password = getpass.getpass("New owner password: ")
    confirmation = getpass.getpass("Confirm owner password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    print(hash_password(password))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Owner authentication utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "hash-password", help="Read a password without echo and print its scrypt hash"
    )
    args = parser.parse_args()
    if args.command == "hash-password":
        return _password_hash_cli()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
