"""Trusted preview gateway: real loopback listeners, a header echo app upstream.

The upstream app records every request it receives, so each test can prove
what the app saw: never a relay cookie, never Authorization, never a forwarded
identity header.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from dataclasses import replace

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus

from relay import preview_gateway
from relay.app import create_app
from relay.auth import Auth
from relay.config import GatewayConfig

BASE = "http://127.0.0.1:8765"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Echo:
    """A small upstream app: echoes headers, redirects, sets cookies, speaks WebSocket."""

    def __init__(self, port: int):
        self.port = port
        self.seen: list[dict] = []

    async def __call__(self, scope, receive, send):
        headers = [(k.decode(), v.decode()) for k, v in scope["headers"]]
        if scope["type"] == "websocket":
            await receive()
            self.seen.append({"ws": scope["path"], "headers": headers, "subprotocols": scope.get("subprotocols")})
            sub = "vite-hmr" if "vite-hmr" in (scope.get("subprotocols") or []) else None
            await send({"type": "websocket.accept", "subprotocol": sub})
            await send({"type": "websocket.send", "text": json.dumps({"type": "connected"})})
            while True:
                msg = await receive()
                if msg["type"] == "websocket.disconnect":
                    return
                await send({"type": "websocket.send", "text": "echo:" + (msg.get("text") or "")})
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        path = scope["path"]
        self.seen.append({"path": path, "query": scope["query_string"].decode(), "headers": headers, "body": body.decode()})
        extra: list[tuple[bytes, bytes]] = []
        status = 200
        out = json.dumps({"path": path}).encode()
        ctype = b"application/json"
        if path == "/redirect":
            status, extra = 302, [(b"location", f"http://127.0.0.1:{self.port}/landing?x=1".encode())]
        elif path == "/relative":
            status, extra = 302, [(b"location", b"/landing")]
        elif path == "/elsewhere":
            status, extra = 302, [(b"location", b"http://127.0.0.1:9/landing")]
        elif path == "/setcookie":
            extra = [
                (b"set-cookie", b"relay_session=evil; Path=/"),
                (b"set-cookie", b"__Host-relay_session=evil; Path=/; Secure"),
                (b"set-cookie", b"app_session=keep; Path=/; HttpOnly"),
            ]
        elif path.startswith("/assets/"):
            out, ctype = b"console.log('asset')", b"text/javascript"
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", ctype), (b"content-length", str(len(out)).encode()), *extra],
            }
        )
        await send({"type": "http.response.body", "body": out})


async def serve(app, port: int):
    config = uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_config=None)
    config.load()
    # The upstream app stays quiet, so every uvicorn log line in a test comes from relay.
    config.http_protocol_class = preview_gateway._quiet(config.http_protocol_class)
    config.ws_protocol_class = preview_gateway._quiet(config.ws_protocol_class)
    server = uvicorn.Server(config)
    server.lifespan = config.lifespan_class(config)
    await server.startup()
    task = asyncio.create_task(server.main_loop())
    return server, task


@contextlib.asynccontextmanager
async def stack(rt, *, public: str | None = None):
    up_port, gw_port = free_port(), free_port()
    echo = Echo(up_port)
    server, task = await serve(echo, up_port)
    gw = GatewayConfig(listen_port=gw_port, env="work2", target_port=up_port, public_origin=public)
    object.__setattr__(rt.settings, "preview_gateways", (gw,))
    rt.auth = Auth(rt.settings, rt.db)
    rt.auth.ensure_token()
    await preview_gateway.start(rt)
    try:
        yield echo, gw, rt.auth.create_session("test")
    finally:
        await preview_gateway.stop(rt)
        server.should_exit = True
        await task
        await server.shutdown()


def header(seen: dict, name: str) -> list[str]:
    return [v for k, v in seen["headers"] if k == name]


async def test_gateway_needs_a_relay_session_and_its_own_host(rt):
    async with stack(rt) as (echo, gw, cookie):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gw.listen_port}") as c:
            assert (await c.get("/")).status_code == 401
            assert (await c.get("/", headers={"Cookie": "relay_session=not-a-session"})).status_code == 401
            bad_host = await c.get("/", headers={"Host": f"evil.example:{gw.listen_port}", "Cookie": f"relay_session={cookie}"})
            assert bad_host.status_code == 421
        assert echo.seen == []


async def test_app_never_receives_relay_credentials(rt):
    async with stack(rt) as (echo, gw, cookie):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gw.listen_port}") as c:
            resp = await c.get(
                "/echo?q=1",
                headers={
                    "Cookie": f"relay_session={cookie}; app_session=abc; __Host-relay_session=other",
                    "Authorization": "Bearer secret-token-value",
                    "Tailscale-User-Login": "adam@example.com",
                    "Tailscale-User-Name": "Adam",
                    "X-Forwarded-For": "100.64.0.9",
                    "X-Forwarded-Proto": "http",
                },
            )
        assert resp.status_code == 200
        seen = echo.seen[-1]
        assert seen["query"] == "q=1"
        assert header(seen, "cookie") == ["app_session=abc"]
        assert header(seen, "authorization") == []
        assert not [k for k, _ in seen["headers"] if k.startswith(("tailscale-", "x-forwarded-"))]
        assert header(seen, "host") == [f"127.0.0.1:{gw.target_port}"]
        assert cookie not in json.dumps(seen)


async def test_https_uses_only_the_host_prefixed_cookie(rt):
    async with stack(rt) as (echo, gw, cookie):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gw.listen_port}") as c:
            secure = {"X-Forwarded-Proto": "https"}
            legacy = await c.get("/", headers={**secure, "Cookie": f"relay_session={cookie}"})
            assert legacy.status_code == 401
            ok = await c.get("/", headers={**secure, "Cookie": f"__Host-relay_session={cookie}"})
            assert ok.status_code == 200
        assert header(echo.seen[-1], "cookie") == []


async def test_app_cannot_set_the_relay_cookie(rt):
    async with stack(rt) as (echo, gw, cookie):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gw.listen_port}") as c:
            resp = await c.get("/setcookie", headers={"Cookie": f"relay_session={cookie}"})
        set_cookies = resp.headers.get_list("set-cookie")
        assert set_cookies == ["app_session=keep; Path=/; HttpOnly"]


async def test_redirects_assets_and_posts(rt):
    async with stack(rt) as (echo, gw, cookie):
        own = f"http://127.0.0.1:{gw.listen_port}"
        async with httpx.AsyncClient(base_url=own, cookies={"relay_session": cookie}) as c:
            redirect = await c.get("/redirect")
            assert redirect.status_code == 302
            assert redirect.headers["location"] == f"{own}/landing?x=1"
            assert (await c.get("/relative")).headers["location"] == "/landing"
            asset = await c.get("/assets/app.js?v=3")
            assert asset.status_code == 200 and asset.text == "console.log('asset')"
            assert echo.seen[-1]["query"] == "v=3"
            foreign = await c.post("/submit", content=b"a=1", headers={"Origin": "http://127.0.0.1:8765"})
            assert foreign.status_code == 403
            posted = await c.post("/submit", content=b"a=1", headers={"Origin": own})
            assert posted.status_code == 200
        seen = echo.seen[-1]
        assert seen["body"] == "a=1"
        assert header(seen, "origin") == [f"http://127.0.0.1:{gw.target_port}"]


async def test_hmr_websocket_passes_without_relay_cookie(rt):
    async with stack(rt) as (echo, gw, cookie):
        own = f"http://127.0.0.1:{gw.listen_port}"
        url = f"ws://127.0.0.1:{gw.listen_port}/?token=hmr"
        async with ws_connect(
            url,
            subprotocols=["vite-hmr"],
            origin=own,
            additional_headers={"Cookie": f"relay_session={cookie}; app_session=abc"},
        ) as ws:
            assert ws.subprotocol == "vite-hmr"
            assert json.loads(await ws.recv()) == {"type": "connected"}
            await ws.send("ping")
            assert await ws.recv() == "echo:ping"
        seen = next(s for s in echo.seen if "ws" in s)
        assert header(seen, "cookie") == ["app_session=abc"]
        assert seen["subprotocols"] == ["vite-hmr"]
        for kwargs in ({"origin": own}, {"origin": "http://127.0.0.1:8765", "additional_headers": {"Cookie": f"relay_session={cookie}"}}):
            with pytest.raises(InvalidStatus):
                async with ws_connect(url, **kwargs):
                    pass


async def test_route_proof_reaches_only_this_gateway(rt):
    async with stack(rt) as (echo, gw, cookie):
        mgr = preview_gateway.manager(rt)
        # Stand-ins for a Tailscale route: one ends at the gateway, one at another server.
        to_gateway = replace(gw, public_origin=f"http://127.0.0.1:{gw.listen_port}")
        to_app = replace(gw, public_origin=f"http://127.0.0.1:{gw.target_port}")
        assert (await mgr.verify(to_gateway, force=True))["status"] == "verified"
        # The gateway answers the proof itself and never forwards it to the app.
        assert not [s for s in echo.seen if s.get("path") == preview_gateway.PROOF_PATH]
        wrong = await mgr.verify(to_app, force=True)
        assert wrong["status"] == "failed"
        assert not preview_gateway.fresh(wrong)


def test_relay_api_refuses_the_gateway_origin(rt):
    app = create_app(runtime=rt, features=False)
    with TestClient(app, base_url=BASE) as c:
        token = Auth(rt.settings, rt.db).ensure_token()
        assert c.post("/api/auth/login", json={"token": token}).status_code == 204
        resp = c.post("/api/auth/logout", headers={"Origin": "http://127.0.0.1:18501"})
        assert resp.status_code == 403 and resp.json()["error"] == "bad_origin"
        same_site = c.get("/api/auth/me", headers={"Sec-Fetch-Site": "same-site"})
        assert same_site.status_code == 403
        assert c.get("/api/auth/me", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 200


async def test_gateway_keeps_relay_access_log_and_logs_no_app_paths(rt, caplog):
    import logging

    access = logging.getLogger("uvicorn.access")
    marker = logging.NullHandler()
    access.addHandler(marker)
    try:
        async with stack(rt) as (echo, gw, cookie):
            assert marker in access.handlers  # relay's own access log still works
            with caplog.at_level(logging.INFO):
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gw.listen_port}") as c:
                    await c.get("/echo?app_token=abc", headers={"Cookie": f"relay_session={cookie}"})
            relay_records = [r for r in caplog.records if r.name.startswith(("uvicorn", "relay"))]
            assert not [r for r in relay_records if "app_token" in r.getMessage()]
    finally:
        access.removeHandler(marker)


async def test_check_session_never_gets_a_redirect_to_another_origin(rt):
    from relay import checks

    async with stack(rt) as (echo, gw, cookie):
        check_value = checks.start_check_session(rt)
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gw.listen_port}") as c:
                person = await c.get("/elsewhere", headers={"Cookie": f"relay_session={cookie}"})
                assert person.status_code == 302  # a person may follow an app's redirect
                check = await c.get("/elsewhere", headers={"Cookie": f"relay_session={check_value}"})
                assert check.status_code == 502 and "location" not in check.headers
                same = await c.get("/redirect", headers={"Cookie": f"relay_session={check_value}"})
                assert same.status_code == 302  # a same-origin redirect still works for a check
        finally:
            checks.end_check_session(rt, check_value)


async def test_request_target_keeps_the_single_upstream(rt):
    async with stack(rt) as (echo, gw, cookie):
        reader, writer = await asyncio.open_connection("127.0.0.1", gw.listen_port)
        writer.write(
            f"GET //other.example/x?y=1 HTTP/1.1\r\nHost: 127.0.0.1:{gw.listen_port}\r\n"
            f"Cookie: relay_session={cookie}\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        status = (await reader.readline()).decode()
        writer.close()
        assert " 200 " in status
        assert echo.seen[-1]["path"] == "//other.example/x" and echo.seen[-1]["query"] == "y=1"


def test_serve_status_must_send_every_path_to_the_gateway():
    gw = GatewayConfig(listen_port=18501, env="work1", target_port=15173, public_origin="https://laptop.example.ts.net:18501")
    key = "laptop.example.ts.net:18501"

    def status(handlers, funnel=False):
        out = {"TCP": {"18501": {"HTTPS": True}}, "Web": {key: {"Handlers": handlers}}}
        if funnel:
            out["AllowFunnel"] = {key: True}
        return out

    ok = status({"/": {"Proxy": "http://127.0.0.1:18501"}})
    assert preview_gateway.serve_route_matches(ok, gw) == (True, None)
    bad = [
        None,  # the status cannot be read
        {"Web": {}},  # no route
        status({"/": {"Proxy": "http://127.0.0.1:15173"}}),  # a direct route to the app
        status({"/": {"Proxy": "http://localhost:18501"}}),  # localhost can resolve to ::1: exact 127.0.0.1 only
        status({"/": {"Proxy": "http://127.0.0.1:15173"}, "/.well-known/": {"Proxy": "http://127.0.0.1:18501"}}),  # split paths
        status({"/": {"Proxy": "http://127.0.0.1:18501"}}, funnel=True),  # public internet
    ]
    # The HTTPS listener (TCP map) must exist on the public port with HTTPS exactly True.
    for tcp in (None, {}, {"18501": {"HTTPS": False}}, {"18501": {"HTTPS": "true"}}, {"18501": {}}, {"443": {"HTTPS": True}}):
        item = status({"/": {"Proxy": "http://127.0.0.1:18501"}})
        if tcp is None:
            del item["TCP"]  # missing TCP map
        else:
            item["TCP"] = tcp  # missing port, false, not exactly True, no HTTPS key, another port
        bad.append(item)
    for item in bad:
        assert preview_gateway.serve_route_matches(item, gw)[0] is False

    # Without an explicit port, the public port is 443.
    gw443 = GatewayConfig(listen_port=18501, env="work1", target_port=15173, public_origin="https://laptop.example.ts.net")
    web443 = {"laptop.example.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:18501"}}}}
    assert preview_gateway.serve_route_matches({"TCP": {"443": {"HTTPS": True}}, "Web": web443}, gw443) == (True, None)
    assert preview_gateway.serve_route_matches({"TCP": {"18501": {"HTTPS": True}}, "Web": web443}, gw443)[0] is False


async def test_https_proof_also_needs_the_serve_route(rt, monkeypatch):
    async with stack(rt) as (echo, gw, cookie):
        mgr = preview_gateway.manager(rt)
        # An https origin whose HMAC proof passes (the stand-in answers like the gateway).
        target = replace(gw, public_origin="https://laptop.example.ts.net:18501")

        class Resp:
            status_code = 200

            def __init__(self, url):
                self.nonce = url.rsplit("=", 1)[1]

            def json(self):
                return {"proof": preview_gateway.proof_value(mgr.secret, gw.listen_port, self.nonce)}

        class Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url):
                return Resp(url)

        monkeypatch.setattr(preview_gateway.httpx, "AsyncClient", Client)

        async def status_split():
            return {"Web": {"laptop.example.ts.net:18501": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{gw.target_port}"}}}}}

        monkeypatch.setattr(preview_gateway, "tailscale_serve_status", status_split)
        assert (await mgr.verify(target, force=True))["status"] == "failed"

        async def status_ok():
            return {
                "TCP": {"18501": {"HTTPS": True}},
                "Web": {"laptop.example.ts.net:18501": {"Handlers": {"/": {"Proxy": f"http://127.0.0.1:{gw.listen_port}"}}}},
            }

        monkeypatch.setattr(preview_gateway, "tailscale_serve_status", status_ok)
        target = replace(gw, public_origin="https://laptop.example.ts.net:18501")
        result = await mgr.verify(target, force=True)
        assert result["status"] == "verified"

