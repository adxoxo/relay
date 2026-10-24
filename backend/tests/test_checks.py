"""Tests for relay.checks and the artifact route.

The artifact-route tests use FastAPI's TestClient with require_user overridden.
The retention and startup tests use the check_runs table directly. One live
test runs the real Playwright smoke check against a local HTTP server; it is
skipped when the host browser is missing.
"""

from __future__ import annotations

import asyncio
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from fakes import FakePodman, ok
from relay import checks
from relay.errors import ApiError
from relay.runtime import Runtime, Principal
from typing import Any

BROWSER = "/opt/brave.com/brave/brave"


def _insert_preview_row(rt: Runtime, *, env: str = "work2", name: str = "app", test_command=None) -> str:
    from relay.db import now, new_id

    pid = new_id()
    ts = now()
    rt.db.insert(
        "previews",
        {
            "id": pid,
            "env": env,
            "name": name,
            "project_dir": "/home/node/projects",
            "command": "npm run dev",
            "port": 8100,
            "health_path": "/",
            "test_command": test_command,
            "created_at": ts,
            "updated_at": ts,
        },
    )
    return pid


def _insert_run(rt: Runtime, preview_id: str, *, status: str = "passed", started_at: float) -> str:
    from relay.db import new_id

    rid = new_id()
    rt.db.insert(
        "check_runs",
        {
            "id": rid,
            "preview_id": preview_id,
            "kind": "smoke",
            "status": status,
            "target_url": "http://127.0.0.1:8100",
            "summary": "s",
            "result_json": None,
            "started_at": started_at,
            "finished_at": started_at,
        },
    )
    return rid


# -- retention ---------------------------------------------------------------

def test_retention_keeps_last_20(rt: Runtime):
    preview_id = _insert_preview_row(rt)
    ids = []
    for i in range(25):
        rid = _insert_run(rt, preview_id, started_at=1000 + i)
        (rt.settings.artifacts_dir / rid).mkdir(parents=True, exist_ok=True)
        (rt.settings.artifacts_dir / rid / "result.json").write_text("{}")
        ids.append(rid)
    checks._retain(rt, preview_id)
    remaining = checks.list_runs(rt.db, preview_id=preview_id, limit=100)
    assert len(remaining) == 20
    # The five oldest folders are gone.
    for rid in ids[:5]:
        assert not (rt.settings.artifacts_dir / rid).exists()
    for rid in ids[5:]:
        assert (rt.settings.artifacts_dir / rid).exists()


# -- startup reconciliation --------------------------------------------------

def test_startup_marks_running_as_error(rt: Runtime):
    preview_id = _insert_preview_row(rt)
    rid = _insert_run(rt, preview_id, status="running", started_at=1000)
    checks.reconcile_startup(rt)
    row = checks.get_run(rt.db, rid)
    assert row["status"] == "error"
    assert row["summary"] == "Interrupted by a relay restart."
    assert row["finished_at"] is not None


# -- test-command run --------------------------------------------------------

async def test_test_command_passes_on_exit_zero(rt: Runtime, fake_podman: FakePodman):
    fake_podman.handler = lambda args, inp: ok("all tests passed\n")
    preview_id = _insert_preview_row(rt, test_command="pytest -q")
    row = await checks.start_test_command(rt, preview_id)
    for _ in range(30):
        await asyncio.sleep(0.05)
        run = checks.get_run(rt.db, row["id"])
        if run["status"] != "running":
            break
    assert run["status"] == "passed"
    output = (rt.settings.artifacts_dir / row["id"] / "output.txt").read_text()
    assert "all tests passed" in output
    for t in list(rt.background):
        t.cancel()


async def test_test_command_fails_on_nonzero(rt: Runtime, fake_podman: FakePodman):
    from relay.podman import ProcResult

    fake_podman.handler = lambda args, inp: ProcResult(1, b"boom\n", b"")
    preview_id = _insert_preview_row(rt, test_command="pytest -q")
    row = await checks.start_test_command(rt, preview_id)
    for _ in range(30):
        await asyncio.sleep(0.05)
        run = checks.get_run(rt.db, row["id"])
        if run["status"] != "running":
            break
    assert run["status"] == "failed"
    for t in list(rt.background):
        t.cancel()


async def test_test_command_requires_command(rt: Runtime):
    preview_id = _insert_preview_row(rt, test_command=None)
    with pytest.raises(ApiError) as exc:
        await checks.start_test_command(rt, preview_id)
    assert exc.value.code == "no_test_command"


# -- smoke check with no browser --------------------------------------------

async def test_smoke_check_no_browser_is_error(rt: Runtime):
    assert rt.settings.browser_executable is None
    preview_id = _insert_preview_row(rt)
    row = await checks.start_smoke_check(rt, preview_id)
    for _ in range(30):
        await asyncio.sleep(0.05)
        run = checks.get_run(rt.db, row["id"])
        if run["status"] != "running":
            break
    assert run["status"] == "error"
    assert "browser" in run["summary"].lower()
    for t in list(rt.background):
        t.cancel()


# -- artifact route ----------------------------------------------------------

@pytest.fixture
def client(rt: Runtime):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from relay import routes_previews
    from relay.errors import ApiError as _ApiError
    from relay.runtime import require_user

    app = FastAPI()
    app.state.rt = rt
    app.include_router(routes_previews.router)

    async def _fake_user():
        return Principal(via="bearer")

    app.dependency_overrides[require_user] = _fake_user

    @app.exception_handler(_ApiError)
    async def _handle(request, exc: _ApiError):
        return JSONResponse(exc.body(), status_code=exc.status)

    return TestClient(app)


def test_artifact_rejects_bad_name(client, rt: Runtime):
    preview_id = _insert_preview_row(rt)
    rid = _insert_run(rt, preview_id, started_at=1000)
    resp = client.get(f"/api/checks/{rid}/artifacts/..%2f..%2fetc%2fpasswd")
    assert resp.status_code in (400, 404)


def test_artifact_rejects_traversal_name(client, rt: Runtime):
    preview_id = _insert_preview_row(rt)
    rid = _insert_run(rt, preview_id, started_at=1000)
    resp = client.get(f"/api/checks/{rid}/artifacts/evil.exe")
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


def test_artifact_serves_png_no_store(client, rt: Runtime):
    preview_id = _insert_preview_row(rt)
    rid = _insert_run(rt, preview_id, started_at=1000)
    folder = rt.settings.artifacts_dir / rid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "phone.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    resp = client.get(f"/api/checks/{rid}/artifacts/phone.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "no-store"


def test_artifact_missing_file_404(client, rt: Runtime):
    preview_id = _insert_preview_row(rt)
    rid = _insert_run(rt, preview_id, started_at=1000)
    (rt.settings.artifacts_dir / rid).mkdir(parents=True, exist_ok=True)
    resp = client.get(f"/api/checks/{rid}/artifacts/desktop.png")
    assert resp.status_code == 404


# -- live Playwright smoke check ---------------------------------------------

class _QuietHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<!doctype html><html><head><title>Relay Test</title></head><body><h1>Hi</h1></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.mark.skipif(not os.path.exists(BROWSER), reason="host browser missing")
def test_live_smoke_check_passes(tmp_path):
    """Run the real Playwright smoke check against a local page."""
    server = HTTPServer(("127.0.0.1", 0), _QuietHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        folder = tmp_path / "run"
        folder.mkdir()
        result = checks._playwright_smoke(
            BROWSER, f"http://127.0.0.1:{port}/", ["phone", "desktop"], folder
        )
        assert result["failed"] is False, result["summary"]
        assert result["viewports"]["desktop"]["status"] == 200
        assert result["viewports"]["desktop"]["title"] == "Relay Test"
        assert (folder / "phone.png").is_file()
        assert (folder / "desktop.png").is_file()
    finally:
        server.shutdown()
        thread.join(timeout=5)



# -- credential boundary of a gateway check (two disposable origins) ----------------
#
# The check browser holds a relay session for the gateway origin. A second,
# direct app on another loopback port must never receive a request, a cookie,
# or a WebSocket handshake from it.

class _Recorder:
    """A loopback app that records every request and WebSocket handshake it gets."""

    def __init__(self, routes: dict[str, Any] | None = None):
        import base64
        import hashlib
        from http.server import ThreadingHTTPServer

        outer = self
        self.seen: list[dict[str, Any]] = []
        self.routes = routes or {}

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                upgrade = self.headers.get("Upgrade", "").lower() == "websocket"
                outer.seen.append({"path": path, "ws": upgrade, "cookie": self.headers.get("Cookie", ""),
                                   "protocol": self.headers.get("Sec-WebSocket-Protocol")})
                if upgrade:
                    key = self.headers["Sec-WebSocket-Key"]
                    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                    self.send_response(101)
                    self.send_header("Upgrade", "websocket")
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Sec-WebSocket-Accept", accept)
                    if "vite-hmr" in (self.headers.get("Sec-WebSocket-Protocol") or ""):
                        self.send_header("Sec-WebSocket-Protocol", "vite-hmr")
                    self.end_headers()
                    self.close_connection = True
                    return
                status, headers, body, delay = outer.routes.get(path, (404, {}, b"", 0))
                if delay:
                    import time as _t

                    _t.sleep(delay)
                body = body() if callable(body) else body
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        ThreadingHTTPServer.daemon_threads = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture
def gated(rt: Runtime):
    """A real preview gateway (own event loop thread) in front of a disposable app."""
    import socket

    from relay import preview_gateway
    from relay.auth import Auth
    from relay.config import GatewayConfig

    other = _Recorder({"/landing": (200, {"Content-Type": "text/plain"}, b"direct app", 0)})
    app = _Recorder()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        gw_port = sock.getsockname()[1]
    gw_url = f"http://127.0.0.1:{gw_port}"
    page = (
        "<!doctype html><title>gw</title><script>"
        f"new WebSocket('ws://127.0.0.1:{gw_port}/hmr', 'vite-hmr');"
        f"new WebSocket('ws://127.0.0.1:{other.port}/hmr', 'vite-hmr');"
        f"fetch('http://127.0.0.1:{other.port}/api', {{credentials: 'include'}}).catch(() => 0);"
        "</script>"
        f"<img src='http://127.0.0.1:{other.port}/img'><img src='/slow'>"
    ).encode()
    away = {"Location": f"http://127.0.0.1:{other.port}/landing"}
    app.routes.update(
        {
            "/": (200, {"Content-Type": "text/html"}, b"<!doctype html><title>ok</title><link rel='stylesheet' href='/app.css'>", 0),
            "/app.css": (200, {"Content-Type": "text/css"}, b"body{}", 0),
            "/away": (302, away, b"", 0),
            "/chain": (302, {"Location": "/step"}, b"", 0),
            "/step": (302, away, b"", 0),
            "/sockets": (200, {"Content-Type": "text/html"}, page, 0),
            "/slow": (404, {}, b"", 1.0),
            "/thirdparty": (200, {"Content-Type": "text/html"},
                            f"<!doctype html><title>cdn</title><img src='http://localhost:{other.port}/font'>".encode(), 0),
        }
    )
    object.__setattr__(
        rt.settings, "preview_gateways", (GatewayConfig(listen_port=gw_port, env="work2", target_port=app.port),)
    )
    rt.auth = Auth(rt.settings, rt.db)
    rt.auth.ensure_token()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    asyncio.run_coroutine_threadsafe(preview_gateway.start(rt), loop).result(timeout=15)
    try:
        yield gw_url, app, other
    finally:
        asyncio.run_coroutine_threadsafe(preview_gateway.stop(rt), loop).result(timeout=15)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        app.close()
        other.close()


def _gateway_check(rt: Runtime, url: str, tmp_path, name: str) -> dict:
    folder = tmp_path / name
    folder.mkdir()
    value = checks.start_check_session(rt)
    try:
        return checks._playwright_smoke(BROWSER, url, ["desktop"], folder, {"name": "relay_session", "value": value})
    finally:
        checks.end_check_session(rt, value)


@pytest.mark.skipif(not os.path.exists(BROWSER), reason="host browser missing")
def test_gateway_check_never_follows_a_redirect_to_another_port(rt: Runtime, gated, tmp_path):
    gw_url, app, other = gated
    ok = _gateway_check(rt, f"{gw_url}/", tmp_path, "ok")
    assert ok["failed"] is False, ok["summary"]
    assert [s["path"] for s in app.seen] == ["/", "/app.css"]  # the session reached the gateway; assets pass
    for path in ("/away", "/chain"):
        result = _gateway_check(rt, f"{gw_url}{path}", tmp_path, path.strip("/"))
        assert result["failed"] is True
        assert "502" in result["summary"]
    assert other.seen == []  # no request, so no cookie, reached the other port


@pytest.mark.skipif(not os.path.exists(BROWSER), reason="host browser missing")
def test_gateway_check_keeps_hmr_and_blocks_other_origins(rt: Runtime, gated, tmp_path):
    gw_url, app, other = gated
    result = _gateway_check(rt, f"{gw_url}/sockets", tmp_path, "sockets")
    hmr = [s for s in app.seen if s["ws"]]
    assert hmr and hmr[0]["protocol"] == "vite-hmr"  # same-origin HMR passes the gateway (it needs the session)
    assert all("relay_session" not in s["cookie"] for s in app.seen)  # the gateway strips the cookie
    assert other.seen == []  # no WebSocket handshake, fetch, or image reached the other port
    blocked = result["viewports"]["desktop"]["blocked"]
    assert any(v.startswith("a WebSocket") for v in blocked) and any(v.startswith("a request") for v in blocked)
    assert result["failed"] is True


@pytest.mark.skipif(not os.path.exists(BROWSER), reason="host browser missing")
def test_direct_local_check_gets_no_relay_cookie(tmp_path):
    app = _Recorder({"/": (200, {"Content-Type": "text/html"}, b"<!doctype html><title>x</title>", 0)})
    try:
        folder = tmp_path / "local"
        folder.mkdir()
        checks._playwright_smoke(BROWSER, f"http://127.0.0.1:{app.port}/", ["desktop"], folder)
        assert app.seen and not any(s["cookie"] for s in app.seen)
    finally:
        app.close()


@pytest.mark.skipif(not os.path.exists(BROWSER), reason="host browser missing")
def test_gateway_check_blocks_a_third_party_host_as_a_warning(rt: Runtime, gated, tmp_path):
    gw_url, app, other = gated
    result = _gateway_check(rt, f"{gw_url}/thirdparty", tmp_path, "thirdparty")
    assert other.seen == []  # blocked: only the gateway origin may load
    assert result["failed"] is False, result["summary"]  # another host never gets the host-only cookie
    assert result["viewports"]["desktop"]["blocked_third_party"] == ["a request to http://localhost:%d" % other.port]

