from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import hmac
import ipaddress
import json
import secrets
import time
from http.cookies import CookieError, SimpleCookie

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .database import SessionLocal
from .models import AuthSession


ADMIN_SESSION_COOKIE = "__Host-alg_admin_session"
CLIENT_SESSION_COOKIE = "__Host-alg_client_session"
DEV_ADMIN_SESSION_COOKIE = "alg_admin_session"
DEV_CLIENT_SESSION_COOKIE = "alg_client_session"
SESSION_DEFAULT_MAX_AGE = 12 * 60 * 60
REMEMBER_CLIENT_MAX_AGE = 30 * 24 * 60 * 60
AUTH_EXPIRES_SESSION_KEY = "auth_expires_at"
REMEMBER_SESSION_KEY = "remember_client"
SESSION_ACTIVITY_WRITE_INTERVAL = timedelta(minutes=1)


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def require_csrf(request: Request) -> None:
    expected = request.session.get("csrf_token")
    supplied = request.headers.get("x-csrf-token", "")
    if not isinstance(expected, str) or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Token CSRF tidak valid atau kedaluwarsa")


class SessionExpiryMiddleware:
    """Enforce the authenticated session expiry loaded from server storage."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        session = scope.get("session")
        if session is not None:
            is_authenticated = "admin_username" in session or "client_user_id" in session
            expires_at = session.get(AUTH_EXPIRES_SESSION_KEY)
            if is_authenticated and (
                not isinstance(expires_at, (int, float)) or time.time() >= expires_at
            ):
                session.clear()
        await self.app(scope, receive, send)


class DatabaseSessionMiddleware:
    """Keep session data server-side and send only a random opaque token."""

    def __init__(self, app: ASGIApp, *, secret_key: str, https_only: bool) -> None:
        self.app = app
        self.secret_key = secret_key.encode("utf-8")
        self.https_only = https_only
        prefix = "__Host-" if https_only else ""
        self.cookie_names = {
            "admin": f"{prefix}alg_admin_session" if https_only else DEV_ADMIN_SESSION_COOKIE,
            "client": f"{prefix}alg_client_session" if https_only else DEV_CLIENT_SESSION_COOKIE,
        }

    def _session_hash(self, token: str) -> str:
        return hmac.new(self.secret_key, token.encode("ascii"), "sha256").hexdigest()

    @staticmethod
    def _cookie_header(scope: Scope, name: str) -> str | None:
        cookie = SimpleCookie()
        for header_name, value in scope.get("headers", []):
            if header_name.lower() == b"cookie":
                try:
                    cookie.load(value.decode("latin-1"))
                except CookieError:
                    continue
        morsel = cookie.get(name)
        return morsel.value if morsel else None

    def _cookie_value(self, name: str, token: str, max_age: int) -> bytes:
        attrs = [f"{name}={token}", "Path=/", f"Max-Age={max_age}", "HttpOnly", "SameSite=Lax"]
        if self.https_only:
            attrs.append("Secure")
        return "; ".join(attrs).encode("latin-1")

    def _expired_cookie(self, name: str) -> bytes:
        attrs = [f"{name}=", "Path=/", "Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT", "HttpOnly", "SameSite=Lax"]
        if self.https_only:
            attrs.append("Secure")
        return "; ".join(attrs).encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        session: dict[str, object] = {}
        loaded_record: AuthSession | None = None
        loaded_cookie: str | None = None
        expired_cookies: list[str] = []
        for user_type in ("admin", "client"):
            cookie_name = self.cookie_names[user_type]
            token = self._cookie_header(scope, cookie_name)
            if not token:
                continue
            session_hash = self._session_hash(token)
            with SessionLocal() as db:
                record = db.get(AuthSession, session_hash)
                now = datetime.now(timezone.utc)
                expires_at = record.expires_at if record is not None else None
                if expires_at is not None and expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if (
                    record is None
                    or record.revoked_at is not None
                    or expires_at <= now
                    or record.user_type != user_type
                ):
                    expired_cookies.append(cookie_name)
                    continue
                try:
                    decoded = json.loads(record.session_data)
                except (TypeError, json.JSONDecodeError):
                    decoded = None
                if not isinstance(decoded, dict):
                    expired_cookies.append(cookie_name)
                    continue
                session = decoded
                loaded_record = record
                loaded_cookie = cookie_name
                last_seen_at = record.last_seen_at
                if last_seen_at.tzinfo is None:
                    last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
                if now - last_seen_at >= SESSION_ACTIVITY_WRITE_INTERVAL:
                    record.last_seen_at = now
                    db.commit()
            break

        scope["session"] = session
        scope.setdefault("state", {})["session_hash"] = loaded_record.session_hash if loaded_record else None
        original_session = dict(session)

        async def send_wrapper(message: Message) -> None:
            if scope["type"] == "http" and message["type"] == "http.response.start":
                changed = session != original_session
                response_headers = list(message.get("headers", []))
                for name in expired_cookies:
                    response_headers.append((b"set-cookie", self._expired_cookie(name)))
                if changed:
                    if loaded_record is not None:
                        with SessionLocal() as db:
                            record = db.get(AuthSession, loaded_record.session_hash)
                            if record is not None and record.revoked_at is None:
                                record.revoked_at = datetime.now(timezone.utc)
                                db.commit()
                    user_type = "admin" if isinstance(session.get("admin_username"), str) else "client" if isinstance(session.get("client_user_id"), int) else None
                    if not session or user_type is None:
                        if loaded_cookie:
                            response_headers.append((b"set-cookie", self._expired_cookie(loaded_cookie)))
                    else:
                        raw_token = secrets.token_urlsafe(32)
                        session_hash = self._session_hash(raw_token)
                        now = datetime.now(timezone.utc)
                        max_age = REMEMBER_CLIENT_MAX_AGE if session.get(REMEMBER_SESSION_KEY) is True else SESSION_DEFAULT_MAX_AGE
                        expires_at = datetime.fromtimestamp(int(session.get(AUTH_EXPIRES_SESSION_KEY, now.timestamp() + max_age)), timezone.utc)
                        user_id = session["admin_username"] if user_type == "admin" else str(session["client_user_id"])
                        with SessionLocal() as db:
                            db.add(AuthSession(
                                session_hash=session_hash,
                                user_type=user_type,
                                user_id=str(user_id),
                                session_data=json.dumps(session, separators=(",", ":")),
                                created_at=now,
                                last_seen_at=now,
                                expires_at=expires_at,
                            ))
                            db.commit()
                        new_cookie = self.cookie_names[user_type]
                        if loaded_cookie and loaded_cookie != new_cookie:
                            response_headers.append((b"set-cookie", self._expired_cookie(loaded_cookie)))
                        response_headers.append((b"set-cookie", self._cookie_value(new_cookie, raw_token, max_age)))
                message["headers"] = response_headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RateLimitMiddleware:
    """Small in-process request limiter for the single-instance local backend."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.requests: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self.last_cleanup = time.monotonic()

    @staticmethod
    def _limit(path: str) -> tuple[int, int]:
        if path in {"/api/auth/login", "/api/auth/client-login"}:
            return 8, 60
        if path in {
            "/api/auth/register",
            "/api/auth/resend-code",
            "/api/auth/password-reset/request",
        }:
            return 4, 60
        if path in {"/api/auth/verify-email", "/api/auth/verify-otp", "/api/auth/password-reset/confirm"}:
            return 10, 60
        if path in {"/api/ea/master/snapshot", "/api/mt5/follower/positions", "/api/mt5/follower/report"}:
            return 720, 60
        if path == "/api/mt5/prepayment-checks":
            return 4, 60
        if path == "/api/mt5/servers":
            return 60, 60
        if path.startswith("/api/tg/"):
            return 12, 60
        if path == "/api/parser/test":
            return 20, 60
        if path == "/api/cs/chat/message":
            return 12, 60
        return 120, 60

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in {"/health", "/favicon.ico"}:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        identity = str(client[0]) if client else "unknown"
        # cloudflared connects from loopback. Only trust its client-IP header on
        # that local hop; requests from other peers cannot spoof rate-limit keys.
        if client and identity in {"127.0.0.1", "::1"}:
            forwarded_ip = dict(scope.get("headers", [])).get(b"cf-connecting-ip", b"").decode("ascii", "ignore").strip()
            try:
                identity = str(ipaddress.ip_address(forwarded_ip))
            except ValueError:
                pass
        key = (identity, path)
        limit, window = self._limit(path)
        now = time.monotonic()
        hits = self.requests[key]
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            body = json.dumps({"detail": "Terlalu banyak permintaan; coba lagi sebentar."}).encode()
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [(b"content-type", b"application/json"), (b"retry-after", str(window).encode())],
            })
            await send({"type": "http.response.body", "body": body})
            return
        hits.append(now)

        if now - self.last_cleanup > 300 or len(self.requests) > 4096:
            cutoff = now - 60
            for old_key in list(self.requests):
                queue = self.requests[old_key]
                while queue and queue[0] <= cutoff:
                    queue.popleft()
                if not queue:
                    self.requests.pop(old_key, None)
            self.last_cleanup = now

        await self.app(scope, receive, send)
