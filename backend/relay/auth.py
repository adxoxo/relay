"""Token authentication and browser sessions.

relay is a single-user private dashboard. Access is granted by one secret
token, kept on the host at ``settings.token_path`` with mode 0600. A browser
exchanges the token once at ``/api/auth/login`` for a session cookie; scripts
can send the token directly as a bearer credential.

The cookie value is a random string. The database stores only its SHA-256
hex digest as the ``auth_sessions`` row id, so a database leak never yields a
usable cookie. Cookie auth on a state-changing request also needs an allowed
``Origin`` (CSRF defence); a bearer request does not, because a browser never
attaches the ``Authorization`` header on its own.

Cookie names: an https request uses ``__Host-relay_session`` (Secure, Path=/,
no Domain). The prefix stops any page on the same host, for example a project
app on another port, from setting a narrower copy, and HttpOnly stops a page
from overwriting it. A plain http request (the laptop at 127.0.0.1) uses
``relay_session``, because browsers refuse a ``__Host-`` cookie without
Secure. Each request reads only the name for its own scheme.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import Request, WebSocket

from . import paths
from .config import Settings
from .db import Database, now
from .errors import ApiError
from .runtime import Principal

COOKIE_NAME = "relay_session"            # plain http (laptop loopback)
SECURE_COOKIE_NAME = "__Host-relay_session"  # https (Tailscale Serve)
RESERVED_COOKIE_NAMES = frozenset({COOKIE_NAME, SECURE_COOKIE_NAME, "__Secure-relay_session"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SLIDING_REFRESH = 86400.0  # refresh a session's expiry when last seen over a day ago
RATE_LIMIT_MAX = 10        # failed logins per window per client address
RATE_LIMIT_WINDOW = 600.0  # seconds


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_token() -> str:
    """A 256-bit URL-safe access token that never starts with ``-`` or ``_``.

    ``token_urlsafe`` can start with ``-``, and a shell command such as
    ``live_e2e.py --token -abc...`` then reads the value as an option.
    Regenerating keeps the full 32 random bytes and the uniform distribution
    of the accepted outputs.
    """
    while True:
        token = secrets.token_urlsafe(32)
        if token[0].isalnum():
            return token


class Auth:
    """Authenticate HTTP requests and WebSocket handshakes. Installed as ``rt.auth``."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._token: str | None = None
        self._token_stamp: tuple[int, int] | None = None  # (inode, mtime_ns) of the token file
        # client address -> failed login timestamps within the window
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    # -- token ---------------------------------------------------------------
    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            st = self.settings.token_path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns)

    def ensure_token(self) -> str:
        """Return the access token, creating it once with mode 0600 when missing."""
        path = self.settings.token_path
        if path.exists():
            self._token = path.read_text().strip()
            if self._token:
                self._token_stamp = self._file_stamp()
                return self._token
        token = new_token()
        paths.write_private_file(path, token)
        self._token = token
        self._token_stamp = self._file_stamp()
        return token

    def _current_token(self) -> str:
        # ``relay token --rotate`` runs in another process and replaces the file.
        # Re-read it when it changes, so rotation revokes the old token at once
        # instead of at the next service restart.
        if self._token is None or self._file_stamp() != self._token_stamp:
            return self.ensure_token()
        return self._token

    def rotate_token(self) -> str:
        """Create a new token and drop every existing browser session."""
        token = new_token()
        paths.write_private_file(self.settings.token_path, token)
        self._token = token
        self._token_stamp = self._file_stamp()
        self.db.execute("DELETE FROM auth_sessions")
        return token

    def verify_token(self, value: str) -> bool:
        """Constant-time compare of a candidate token to the stored token."""
        if not value:
            return False
        return hmac.compare_digest(value, self._current_token())

    # -- browser sessions ----------------------------------------------------
    def create_session(self, user_agent: str | None) -> str:
        """Create a session row and return the opaque cookie value."""
        cookie_value = secrets.token_urlsafe(32)
        ts = now()
        self.db.insert(
            "auth_sessions",
            {
                "id": _sha256_hex(cookie_value),
                "created_at": ts,
                "expires_at": ts + self.settings.cookie_max_age,
                "last_seen": ts,
                "user_agent": (user_agent or "")[:400] or None,
            },
        )
        return cookie_value

    def _lookup_session(self, cookie_value: str) -> str | None:
        """Return the session id for a valid, unexpired cookie, else ``None``.

        Applies sliding refresh: when the session was last seen over a day ago,
        push its expiry forward and update ``last_seen``.
        """
        session_id = _sha256_hex(cookie_value)
        row = self.db.one("SELECT * FROM auth_sessions WHERE id = ?", (session_id,))
        if row is None:
            return None
        ts = now()
        if row["expires_at"] <= ts:
            self.db.execute("DELETE FROM auth_sessions WHERE id = ?", (session_id,))
            return None
        if ts - row["last_seen"] > SLIDING_REFRESH:
            self.db.update(
                "auth_sessions",
                session_id,
                {"last_seen": ts, "expires_at": ts + self.settings.cookie_max_age},
            )
        return session_id

    def session_from_cookie(self, conn: Request | WebSocket) -> str | None:
        """The auth session id for the scheme's cookie, without any Origin rule.

        The preview gateway uses this: it runs on another origin, so it does its
        own Host and Origin checks.
        """
        cookie_value = self.session_cookie(conn)
        if not cookie_value:
            return None
        return self._lookup_session(cookie_value)

    def delete_session(self, cookie_value: str) -> None:
        self.db.execute("DELETE FROM auth_sessions WHERE id = ?", (_sha256_hex(cookie_value),))

    # -- rate limiting -------------------------------------------------------
    def _client_key(self, request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def check_rate_limit(self, request: Request) -> None:
        """Raise 429 when a client made too many failed logins recently."""
        key = self._client_key(request)
        cutoff = time.monotonic() - RATE_LIMIT_WINDOW
        bucket = self._failures[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_MAX:
            raise ApiError(429, "rate_limited", "Too many login attempts. Try again later.")

    def record_failure(self, request: Request) -> None:
        self._failures[self._client_key(request)].append(time.monotonic())

    def clear_failures(self, request: Request) -> None:
        self._failures.pop(self._client_key(request), None)

    # -- cookie flags --------------------------------------------------------
    def _is_secure(self, request: Request | WebSocket) -> bool:
        return is_secure(request)

    def cookie_name(self, request: Request | WebSocket) -> str:
        return SECURE_COOKIE_NAME if self._is_secure(request) else COOKIE_NAME

    def session_cookie(self, request: Request | WebSocket) -> str | None:
        """The session cookie value for this request's scheme, else ``None``."""
        return request.cookies.get(self.cookie_name(request)) or None

    def cookie_params(self, request: Request) -> dict:
        """Keyword arguments for ``Response.set_cookie`` for a new session cookie.

        Never set ``domain``: a ``__Host-`` cookie must be host-only.
        """
        secure = self._is_secure(request)
        return {
            "key": SECURE_COOKIE_NAME if secure else COOKIE_NAME,
            "httponly": True,
            "samesite": "strict",
            "path": "/",
            "max_age": self.settings.cookie_max_age,
            "secure": secure,
        }

    def clear_cookie_params(self, request: Request) -> dict:
        secure = self._is_secure(request)
        return {
            "key": SECURE_COOKIE_NAME if secure else COOKIE_NAME,
            "path": "/",
            "httponly": True,
            "samesite": "strict",
            "secure": secure,
        }

    # -- origin and identity -------------------------------------------------
    def _origin_allowed(self, origin: str | None) -> bool:
        if not origin:
            return False
        # Exact match: a browser sends the canonical origin ("scheme://host[:port]").
        # Configured origins are normalised once in Settings.allowed_origins.
        return origin in self.settings.allowed_origins

    def _check_identity(self, headers) -> str | None:
        """Return the Tailscale login when present and allowed, else raise 403.

        When ``tailscale_logins`` is not configured, the header is not enforced.
        """
        login = headers.get("tailscale-user-login")
        if not self.settings.tailscale_logins:
            return login or None
        if login and login not in self.settings.tailscale_logins:
            raise ApiError(403, "forbidden_identity", "This identity is not allowed.")
        return login or None

    # -- request authentication ----------------------------------------------
    async def authenticate_request(self, request: Request) -> Principal:
        bearer = self._bearer_token(request.headers.get("authorization"))
        if bearer is not None:
            if not self.verify_token(bearer):
                raise ApiError(401, "unauthorized", "Invalid or missing credentials.")
            login = self._check_identity(request.headers)
            return Principal(via="bearer", tailscale_login=login)

        cookie_value = self.session_cookie(request)
        if cookie_value:
            session_id = self._lookup_session(cookie_value)
            if session_id is None:
                raise ApiError(401, "unauthorized", "Invalid or missing credentials.")
            if request.method not in SAFE_METHODS and not self._origin_allowed(request.headers.get("origin")):
                raise ApiError(403, "bad_origin", "Origin not allowed.")
            # A page on another origin of the same site (a preview app on
            # another port of the tailnet host) gets the SameSite=Strict cookie
            # too. Browsers label such a request ``same-site``; refuse it.
            if request.headers.get("sec-fetch-site", "").lower() in ("same-site", "cross-site"):
                raise ApiError(403, "bad_origin", "Origin not allowed.")
            login = self._check_identity(request.headers)
            return Principal(via="cookie", auth_session_id=session_id, tailscale_login=login)

        raise ApiError(401, "unauthorized", "Invalid or missing credentials.")

    async def authenticate_websocket(self, websocket: WebSocket) -> Principal | None:
        """Return a Principal for a valid handshake, else ``None`` (bad origin) ...

        Raises ``ApiError`` only for a forbidden identity. The caller maps a
        ``None`` result to close code 4401, and a bad origin to 4403 by
        inspecting the Origin separately.
        """
        origin = websocket.headers.get("origin")
        if origin is not None and not self._origin_allowed(origin):
            raise ApiError(403, "bad_origin", "Origin not allowed.")

        bearer = self._bearer_token(websocket.headers.get("authorization"))
        if bearer is not None:
            if not self.verify_token(bearer):
                return None
            login = self._check_identity(websocket.headers)
            return Principal(via="bearer", tailscale_login=login)

        cookie_value = self.session_cookie(websocket)
        if cookie_value:
            session_id = self._lookup_session(cookie_value)
            if session_id is None:
                return None
            login = self._check_identity(websocket.headers)
            return Principal(via="cookie", auth_session_id=session_id, tailscale_login=login)

        return None

    @staticmethod
    def _bearer_token(header: str | None) -> str | None:
        if not header:
            return None
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return None
        return value.strip() or None


def is_secure(request: Request | WebSocket) -> bool:
    """True for https: Tailscale Serve sets ``X-Forwarded-Proto: https``."""
    if request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https":
        return True
    return request.url.scheme in ("https", "wss")


def origin_host(origin: str) -> str | None:
    """Return the host[:port] of an origin string, or ``None`` when unparsable."""
    try:
        parts = urlsplit(origin)
    except ValueError:
        return None
    return parts.netloc or None
