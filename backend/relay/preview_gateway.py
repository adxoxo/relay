"""Trusted preview gateways: one loopback listener per approved app.

A gateway runs inside the relay process on ``127.0.0.1:<listen_port>``. Each
listener is a separate browser origin, so a project app keeps its root URL,
its assets, its redirects, its cookies, and its HMR WebSocket. Tailscale
Serve can map an external HTTPS port to the listener; relay never creates
that route itself.

For every request a gateway:

1. accepts only its own Host values (loopback and its configured public host);
2. answers the route proof (``PROOF_PATH``) without forwarding it;
3. requires a valid relay session cookie and an allowed Tailscale identity;
4. requires its own Origin on a state-changing request or a WebSocket;
5. forwards to exactly one endpoint, ``127.0.0.1:<target_port>``;
6. removes the relay cookies, ``Authorization``, and forwarded identity
   headers from the request;
7. drops any ``Set-Cookie`` for a relay cookie name from the response, and
   maps an absolute redirect to the upstream back to its own origin.

The gateway origin is never one of relay's allowed origins, so a project page
cannot make a state-changing call or open a WebSocket to the relay API.

Route proof: relay requests ``<public_origin>/.well-known/relay-gateway-proof``
with a random nonce. Only this process knows the HMAC key, so a correct answer
proves that the public route ends at this gateway now. A phone link appears
only for a proof younger than ``PROOF_TTL`` seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .auth import RESERVED_COOKIE_NAMES, is_secure
from .config import GatewayConfig, Settings
from .errors import ApiError
from .runtime import Runtime

log = logging.getLogger("relay.gateway")

PROOF_PATH = "/.well-known/relay-gateway-proof"
PROOF_TTL = 60.0          # seconds a route proof stays fresh
PROOF_TIMEOUT = 3.0
NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WS_MAX_MESSAGE = 16 * 1024 * 1024

HOP_BY_HOP = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "trailers",
        "transfer-encoding", "upgrade", "proxy-connection",
    }
)
# Request headers that the app never receives.
STRIPPED_REQUEST = HOP_BY_HOP | frozenset(
    {
        "host", "cookie", "authorization", "origin", "referer", "forwarded", "x-forwarded-for",
        "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port", "x-forwarded-prefix", "x-real-ip",
    }
)
STRIPPED_REQUEST_PREFIXES = ("tailscale-", "x-forwarded-", "sec-websocket-")
RESERVED_LOWER = frozenset(n.lower() for n in RESERVED_COOKIE_NAMES)


# -- header helpers ----------------------------------------------------------

def filter_cookie_header(value: str) -> str | None:
    """Remove every relay cookie from a Cookie header. Keep the app's cookies."""
    kept = []
    for part in value.split(";"):
        item = part.strip()
        if not item:
            continue
        name = item.split("=", 1)[0].strip()
        if name.lower() in RESERVED_LOWER:
            continue
        kept.append(item)
    return "; ".join(kept) or None


def is_reserved_set_cookie(value: str) -> bool:
    name = value.split("=", 1)[0].strip().lower()
    return name in RESERVED_LOWER


def gateway_hosts(settings: Settings, gw: GatewayConfig) -> set[str]:
    port = gw.listen_port
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if gw.public_origin:
        netloc = urlsplit(gw.public_origin).netloc.lower()
        hosts.add(netloc)
        if ":" not in netloc:
            hosts.add(f"{netloc}:443")
    return hosts


def gateway_origins(gw: GatewayConfig) -> set[str]:
    port = gw.listen_port
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}
    if gw.public_origin:
        origins.add(gw.public_origin.lower())
    return origins


def proof_value(secret: bytes, listen_port: int, nonce: str) -> str:
    return hmac.new(secret, f"relay-gateway:{listen_port}:{nonce}".encode(), hashlib.sha256).hexdigest()


def origin_key(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) with the default port filled in, else None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.scheme not in ("http", "https"):
        return None
    return parts.scheme, parts.hostname.lower(), port or (443 if parts.scheme == "https" else 80)


def gateway_for(settings: Settings, env: str, host_port: int | None) -> GatewayConfig | None:
    if host_port is None:
        return None
    for gw in settings.preview_gateways:
        if gw.env == env and gw.target_port == host_port:
            return gw
    return None


# -- the ASGI application ----------------------------------------------------

class GatewayApp:
    """ASGI app for one gateway listener."""

    def __init__(self, rt: Runtime, gw: GatewayConfig, secret: bytes):
        self.rt = rt
        self.gw = gw
        self.secret = secret
        self.hosts = gateway_hosts(rt.settings, gw)
        self.origins = gateway_origins(gw)
        self.upstream_origin = f"http://127.0.0.1:{gw.target_port}"
        self.upstream_aliases = (
            f"http://127.0.0.1:{gw.target_port}",
            f"http://localhost:{gw.target_port}",
        )
        self.client = httpx.AsyncClient(
            base_url=self.upstream_origin,
            timeout=httpx.Timeout(connect=5.0, read=None, write=60.0, pool=10.0),
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    async def __call__(self, scope: dict, receive, send) -> None:
        kind = scope["type"]
        if kind == "http":
            await self._http(scope, receive, send)
        elif kind == "websocket":
            await self._websocket(scope, receive, send)

    # -- shared checks -----------------------------------------------------
    def _headers(self, scope: dict) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for k, v in scope.get("headers", []):
            out.setdefault(k.decode("latin-1").lower(), []).append(v.decode("latin-1"))
        return out

    def _host_ok(self, headers: dict[str, list[str]]) -> bool:
        host = (headers.get("host") or [""])[0].strip().lower()
        return host in self.hosts

    def _authenticate(self, scope: dict) -> tuple[str, str | None]:
        """Return ('ok' | 'unauthorized' | 'forbidden', auth session id)."""
        from starlette.requests import HTTPConnection

        conn = HTTPConnection(scope)
        auth = self.rt.auth
        if auth is None:
            return "unauthorized", None
        try:
            auth._check_identity(conn.headers)
        except ApiError:
            return "forbidden", None
        session_id = auth.session_from_cookie(conn)
        return ("ok" if session_id else "unauthorized"), session_id

    def _own_origin(self, scope: dict, headers: dict[str, list[str]]) -> str:
        from starlette.requests import HTTPConnection

        host = (headers.get("host") or [""])[0].strip()
        scheme = "https" if is_secure(HTTPConnection(scope)) else "http"
        return f"{scheme}://{host}"

    def _upstream_headers(self, headers: dict[str, list[str]], *, websocket: bool = False) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for name, values in headers.items():
            if name in STRIPPED_REQUEST or name.startswith(STRIPPED_REQUEST_PREFIXES):
                continue
            for value in values:
                out.append((name, value))
        cookie = filter_cookie_header("; ".join(headers.get("cookie", [])))
        if cookie:
            out.append(("cookie", cookie))
        referer = (headers.get("referer") or [None])[0]
        if referer:
            for origin in self.origins:
                if referer.lower().startswith(origin + "/") or referer.lower() == origin:
                    out.append(("referer", self.upstream_origin + referer[len(origin):]))
                    break
        if not websocket:
            out.append(("host", f"127.0.0.1:{self.gw.target_port}"))
            origin = (headers.get("origin") or [None])[0]
            if origin is not None:
                out.append(("origin", self.upstream_origin))
        return out

    def _rewrite_location(self, value: str, own_origin: str) -> str:
        for alias in self.upstream_aliases:
            if value == alias or value.startswith(alias + "/") or value.startswith(alias + "?"):
                return own_origin + value[len(alias):]
        return value

    # -- HTTP --------------------------------------------------------------
    async def _http(self, scope: dict, receive, send) -> None:
        headers = self._headers(scope)
        method = scope["method"].upper()
        if not self._host_ok(headers):
            await _plain(send, 421, "Unknown host for this relay preview gateway.")
            return
        if not (scope.get("raw_path") or scope["path"].encode()).startswith(b"/"):
            await _plain(send, 400, "Bad request target.")
            return
        if scope["path"] == PROOF_PATH:
            await self._proof(scope, send)
            return
        verdict, session_id = self._authenticate(scope)
        if verdict != "ok":
            await self._login_page(send, 403 if verdict == "forbidden" else 401)
            return
        # A browser check (checks.start_check_session) must never follow a
        # redirect to another origin: the browser would send the cookie there.
        check_session = session_id in self.rt.cache.get("check_sessions", ())
        origin = (headers.get("origin") or [None])[0]
        if method not in SAFE_METHODS and origin is not None and origin.lower() not in self.origins:
            await _plain(send, 403, "This request came from another origin. The gateway refused it.")
            return

        async def body():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                if chunk:
                    yield chunk
                if not message.get("more_body", False):
                    return

        raw_path = scope.get("raw_path") or scope["path"].encode()
        target = raw_path.decode("latin-1")
        if scope.get("query_string"):
            target += "?" + scope["query_string"].decode("latin-1")
        has_body = method not in ("GET", "HEAD") or any(
            n in headers for n in ("content-length", "transfer-encoding")
        )
        # An explicit URL: the upstream stays 127.0.0.1:<target_port> and the raw
        # path ("//x" included) reaches the app unchanged.
        url = httpx.URL(self.upstream_origin).copy_with(raw_path=target.encode("latin-1"))
        request = self.client.build_request(
            method,
            url,
            headers=self._upstream_headers(headers),
            content=body() if has_body else None,
        )
        try:
            response = await self.client.send(request, stream=True)
        except httpx.HTTPError:
            await _plain(
                send,
                502,
                f"The app does not answer on 127.0.0.1:{self.gw.target_port}. Start it, then reload this page.",
            )
            return
        try:
            own_origin = self._own_origin(scope, headers)
            out_headers: list[tuple[bytes, bytes]] = []
            for name, value in response.headers.multi_items():
                lname = name.lower()
                if lname in HOP_BY_HOP:
                    continue
                if lname == "set-cookie" and is_reserved_set_cookie(value):
                    continue
                if lname == "location":
                    value = self._rewrite_location(value, own_origin)
                    if (
                        check_session
                        and 300 <= response.status_code < 400
                        and origin_key(urljoin(own_origin + target, value)) != origin_key(own_origin)
                    ):
                        await _plain(send, 502, "relay check: the app redirected to another origin. The gateway stopped it.")
                        return
                out_headers.append((lname.encode("latin-1"), value.encode("latin-1")))
            await send({"type": "http.response.start", "status": response.status_code, "headers": out_headers})
            async for chunk in response.aiter_raw():
                if chunk:
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            await response.aclose()

    async def _proof(self, scope: dict, send) -> None:
        from urllib.parse import parse_qs

        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        nonce = (query.get("nonce") or [""])[0]
        if scope["method"].upper() != "GET" or not NONCE_RE.match(nonce):
            await _plain(send, 400, "Bad proof request.")
            return
        body = (
            '{"gateway":"relay","listen_port":%d,"proof":"%s"}'
            % (self.gw.listen_port, proof_value(self.secret, self.gw.listen_port, nonce))
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _login_page(self, send, status: int) -> None:
        relay = (self.rt.settings.public_origins or self.rt.settings.local_origins)[0]
        text = (
            "Log in to relay first, then open this preview again."
            if status == 401
            else "This Tailscale identity is not allowed."
        )
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>relay preview gateway</title></head>"
            "<body style='font-family:sans-serif;padding:2rem;max-width:36rem;margin:auto'>"
            f"<h1 style='font-size:1.3rem'>relay preview gateway</h1><p>{html.escape(text)}</p>"
            f"<p><a href='{html.escape(relay)}/'>Open relay</a></p></body></html>"
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(page)).encode()),
                    (b"content-security-policy", b"default-src 'none'; style-src 'unsafe-inline'"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": page})

    # -- WebSocket ---------------------------------------------------------
    async def _websocket(self, scope: dict, receive, send) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import ConnectionClosed, InvalidHandshake

        headers = self._headers(scope)
        message = await receive()
        if message["type"] != "websocket.connect":
            return
        if not self._host_ok(headers) or not (scope.get("raw_path") or scope["path"].encode()).startswith(b"/"):
            await send({"type": "websocket.close", "code": 4400})
            return
        verdict, _ = self._authenticate(scope)
        if verdict != "ok":
            await send({"type": "websocket.close", "code": 4403 if verdict == "forbidden" else 4401})
            return
        origin = (headers.get("origin") or [None])[0]
        if origin is not None and origin.lower() not in self.origins:
            await send({"type": "websocket.close", "code": 4403})
            return
        path = (scope.get("raw_path") or scope["path"].encode()).decode("latin-1")
        if scope.get("query_string"):
            path += "?" + scope["query_string"].decode("latin-1")
        url = f"ws://127.0.0.1:{self.gw.target_port}{path}"
        subprotocols = scope.get("subprotocols") or None
        try:
            upstream = await connect(
                url,
                additional_headers=self._upstream_headers(headers, websocket=True),
                origin=self.upstream_origin if origin is not None else None,
                subprotocols=subprotocols,
                open_timeout=5,
                max_size=WS_MAX_MESSAGE,
                ping_interval=None,
                user_agent_header=None,
            )
        except (OSError, InvalidHandshake, asyncio.TimeoutError, TimeoutError):
            await send({"type": "websocket.close", "code": 1011})
            return
        await send({"type": "websocket.accept", "subprotocol": upstream.subprotocol})

        async def client_to_upstream() -> None:
            while True:
                msg = await receive()
                if msg["type"] == "websocket.disconnect":
                    with contextlib.suppress(Exception):
                        await upstream.close()
                    return
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])

        async def upstream_to_client() -> None:
            try:
                async for data in upstream:
                    if isinstance(data, str):
                        await send({"type": "websocket.send", "text": data})
                    else:
                        await send({"type": "websocket.send", "bytes": data})
            except ConnectionClosed:
                pass
            code = upstream.close_code or 1000
            if code in (1005, 1006, 1015):
                code = 1000
            with contextlib.suppress(Exception):
                await send({"type": "websocket.close", "code": code})

        tasks = {asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await upstream.close()


async def _plain(send, status: int, text: str) -> None:
    body = text.encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# -- listeners and route proofs ----------------------------------------------

def _quiet(base: type) -> type:
    """A uvicorn protocol class that logs through ``relay.gateway`` and skips access lines."""

    class Quiet(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.logger = log
            self.access_log = False
            conn = getattr(self, "conn", None)
            if conn is not None and hasattr(conn, "logger"):
                conn.logger = log

    Quiet.__name__ = f"Quiet{base.__name__}"
    return Quiet

@dataclass
class _Listener:
    gw: GatewayConfig
    app: GatewayApp
    server: Any
    task: asyncio.Task


@dataclass
class GatewayManager:
    """Owns the gateway listeners and the route-proof cache. Lives on ``rt.cache``."""

    rt: Runtime
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    listeners: dict[int, _Listener] = field(default_factory=dict)
    errors: dict[int, str] = field(default_factory=dict)
    proofs: dict[int, dict[str, Any]] = field(default_factory=dict)

    async def start(self) -> None:
        import uvicorn

        for gw in self.rt.settings.preview_gateways:
            app = GatewayApp(self.rt, gw, self.secret)
            # No log_config, log_level, or access_log here: each of them changes the
            # process-wide "uvicorn.*" loggers, so relay's own access log would stop.
            config = uvicorn.Config(
                app,
                host="127.0.0.1",
                port=gw.listen_port,
                lifespan="off",
                log_config=None,
                proxy_headers=False,
                server_header=False,
                ws_ping_interval=20.0,
                ws_ping_timeout=30.0,
                ws_max_size=WS_MAX_MESSAGE,
                timeout_graceful_shutdown=3,
            )
            config.load()
            # App paths and query strings can carry the app's own tokens: the gateway
            # logs errors only, through its own logger.
            config.http_protocol_class = _quiet(config.http_protocol_class)
            if config.ws_protocol_class is not None:
                config.ws_protocol_class = _quiet(config.ws_protocol_class)
            server = uvicorn.Server(config)
            server.lifespan = config.lifespan_class(config)
            try:
                await server.startup()
            except SystemExit:
                await app.aclose()
                self.errors[gw.listen_port] = f"The gateway could not listen on 127.0.0.1:{gw.listen_port}."
                log.error("preview gateway %d could not start", gw.listen_port)
                continue
            task = asyncio.create_task(server.main_loop())
            self.listeners[gw.listen_port] = _Listener(gw, app, server, task)
            log.info("preview gateway 127.0.0.1:%d -> 127.0.0.1:%d", gw.listen_port, gw.target_port)

    async def stop(self) -> None:
        for listener in list(self.listeners.values()):
            listener.server.should_exit = True
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await listener.task
            with contextlib.suppress(Exception):
                await listener.server.shutdown()
            with contextlib.suppress(Exception):
                await listener.app.aclose()
        self.listeners.clear()

    def running(self, listen_port: int) -> bool:
        return listen_port in self.listeners

    async def verify(self, gw: GatewayConfig, *, force: bool = False) -> dict[str, Any]:
        """Prove that ``gw.public_origin`` reaches this gateway. Cached for PROOF_TTL."""
        cached = self.proofs.get(gw.listen_port)
        if cached and not force and time.time() - cached["checked_at"] < PROOF_TTL / 2:
            return cached
        if not gw.public_origin:
            result = {"status": "not_set_up", "checked_at": time.time(), "message": None}
        elif not self.running(gw.listen_port):
            result = {
                "status": "failed",
                "checked_at": time.time(),
                "message": self.errors.get(gw.listen_port, "The gateway listener is not running."),
            }
        else:
            nonce = secrets.token_hex(16)
            url = f"{gw.public_origin}{PROOF_PATH}?nonce={nonce}"
            expected = proof_value(self.secret, gw.listen_port, nonce)
            try:
                async with httpx.AsyncClient(timeout=PROOF_TIMEOUT, follow_redirects=False, trust_env=False) as client:
                    resp = await client.get(url)
                data = resp.json() if resp.status_code == 200 else {}
                ok = isinstance(data, dict) and hmac.compare_digest(str(data.get("proof", "")), expected)
                message = None if ok else "The public route answers, but it does not end at this relay gateway."
            except (httpx.HTTPError, ValueError) as exc:
                ok = False
                message = f"relay cannot reach {urlsplit(gw.public_origin).netloc} ({type(exc).__name__})."
            if ok and gw.public_origin.startswith("https://"):
                # The proof shows that one path reaches this gateway. The Serve
                # status must also show that every path of the public port does.
                ok, message = serve_route_matches(await tailscale_serve_status(), gw)
            result = {"status": "verified" if ok else "failed", "checked_at": time.time(), "message": message}
        result["url"] = gw.public_origin + "/" if gw.public_origin else None
        result["gateway_port"] = gw.listen_port
        self.proofs[gw.listen_port] = result
        return result


SERVE_STATUS_TIMEOUT = 3.0


async def tailscale_serve_status() -> dict[str, Any] | None:
    """``tailscale serve status --json`` (read only), or None when it cannot be read."""
    import json
    import shutil

    binary = shutil.which("tailscale")
    if binary is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "serve", "status", "--json",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), SERVE_STATUS_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(out or b"{}")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def serve_route_matches(status: dict[str, Any] | None, gw: GatewayConfig) -> tuple[bool, str | None]:
    """True when Tailscale Serve sends every path of the public port to this gateway only."""
    if status is None:
        return False, "relay cannot read the Tailscale Serve status, so it cannot confirm the route."
    parts = urlsplit(gw.public_origin or "")
    public_port = parts.port or 443
    key = f"{(parts.hostname or '').lower()}:{public_port}"
    # Serve keeps the listener (TCP) and the handlers (Web) in separate maps.
    # The public port must be an HTTPS listener: exactly True, not only truthy.
    tcp = status.get("TCP")
    listener = {str(k): v for k, v in tcp.items()}.get(str(public_port)) if isinstance(tcp, dict) else None
    if not isinstance(listener, dict) or listener.get("HTTPS") is not True:
        return False, f"Tailscale Serve has no HTTPS listener on port {public_port}."
    web = {str(k).lower(): v for k, v in (status.get("Web") or {}).items()}
    handlers = (web.get(key) or {}).get("Handlers") if isinstance(web.get(key), dict) else None
    if not isinstance(handlers, dict) or not handlers:
        return False, "Tailscale Serve has no route for this port."
    # Exact IPv4 loopback only: the gateway binds 127.0.0.1, and "localhost" can
    # resolve to ::1, where another process could listen.
    target = f"http://127.0.0.1:{gw.listen_port}"
    proxy = (handlers.get("/") or {}).get("Proxy") if isinstance(handlers.get("/"), dict) else None
    if set(handlers) != {"/"} or proxy != target:
        return False, "The Tailscale Serve route for this port does not send every path to this gateway."
    funnel = {str(k).lower(): v for k, v in (status.get("AllowFunnel") or {}).items()}
    if funnel.get(key):
        return False, "Tailscale Funnel is on for this port. relay needs a tailnet-only route."
    return True, None


def manager(rt: Runtime) -> GatewayManager | None:
    return rt.cache.get("gateways")


async def start(rt: Runtime) -> None:
    if not rt.settings.preview_gateways:
        return
    mgr = GatewayManager(rt)
    rt.cache["gateways"] = mgr
    await mgr.start()


async def stop(rt: Runtime) -> None:
    mgr = rt.cache.pop("gateways", None)
    if mgr is not None:
        await mgr.stop()


def fresh(proof: dict[str, Any] | None) -> bool:
    return bool(proof and proof.get("status") == "verified" and time.time() - proof["checked_at"] < PROOF_TTL)
