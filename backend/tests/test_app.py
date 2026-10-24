"""Tests for the app factory: security headers, host check, SPA, and /api/stream."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.auth import Auth

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def app(rt):
    return create_app(runtime=rt, features=False)


@pytest.fixture
def token(rt) -> str:
    return Auth(rt.settings, rt.db).ensure_token()


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE) as c:
        yield c


def test_health_is_public(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "version": "0.1.0"}


def test_security_headers_present(client):
    resp = client.get("/api/health")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["Cache-Control"] == "no-store"


def test_bad_host_rejected(client):
    resp = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_host"


def test_empty_host_rejected(client):
    # A blank Host header must not bypass the DNS-rebinding check when
    # allowed_hosts is configured. Browsers always send Host; only a
    # non-browser client on loopback could send an empty one.
    resp = client.get("/api/health", headers={"Host": ""})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_host"


def test_unknown_api_path_json_404(client):
    resp = client.get("/api/does-not-exist")
    assert resp.status_code == 404
    assert resp.json() == {"error": "not_found", "message": "Unknown API path."}


def test_spa_fallback_when_dist_missing(client):
    resp = client.get("/some/app/route")
    assert resp.status_code == 200
    assert "frontend is not built" in resp.text
    assert resp.headers["Cache-Control"] == "no-store"


def test_spa_serves_index_when_built(rt, tmp_path):
    dist = rt.settings.frontend_dist
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>relay-app</title>")
    (dist / "assets" / "main.js").write_text("console.log('relay')")
    app = create_app(runtime=rt, features=False)
    with TestClient(app, base_url=BASE) as client:
        resp = client.get("/dashboard")
        assert resp.status_code == 200
        assert "relay-app" in resp.text
        assert resp.headers["Cache-Control"] == "no-store"
        asset = client.get("/assets/main.js")
        assert asset.status_code == 200
        assert "immutable" in asset.headers.get("Cache-Control", "")


@pytest.fixture
def asset_client(rt):
    dist = rt.settings.frontend_dist
    (dist / "assets").mkdir(parents=True)
    # An app shell over the gzip size floor: it must stay uncompressed (no-store, not an asset).
    (dist / "index.html").write_text("<!doctype html><title>relay-app</title>" + "<!-- shell -->" * 200)
    (dist / "assets" / "index-abc.js").write_text("export const relay = 'terminal';\n" * 2000)
    (dist / "assets" / "tiny.js").write_text("console.log(1)")
    (dist / "assets" / "logo.png").write_bytes(b"\x89PNG" + b"\x00" * 4000)
    app = create_app(runtime=rt, features=False)
    with TestClient(app, base_url=BASE) as client:
        yield client, (dist / "assets" / "index-abc.js").read_bytes()


def test_assets_gzip_keeps_mime_cache_and_csp(asset_client):
    client, js = asset_client
    resp = client.get("/assets/index-abc.js", headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers["Content-Encoding"] == "gzip"
    assert "accept-encoding" in resp.headers["Vary"].lower()
    assert resp.headers["Content-Type"].startswith(("text/javascript", "application/javascript"))
    assert resp.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["ETag"].startswith('W/"')
    assert resp.content == js  # the client decodes gzip; the bytes are the file
    assert int(resp.headers.get("Content-Length", 0)) < len(js) or "Content-Length" not in resp.headers


def test_assets_identity_and_conditional_and_range(asset_client):
    client, js = asset_client
    plain = client.get("/assets/index-abc.js", headers={"Accept-Encoding": "identity"})
    assert "Content-Encoding" not in plain.headers
    assert plain.headers["Content-Length"] == str(len(js))
    strong = plain.headers["ETag"]
    assert not strong.startswith("W/")
    weak = client.get("/assets/index-abc.js", headers={"Accept-Encoding": "gzip"}).headers["ETag"]
    assert weak == "W/" + strong
    for tag in (weak, strong):
        cached = client.get("/assets/index-abc.js", headers={"Accept-Encoding": "gzip", "If-None-Match": tag})
        assert cached.status_code == 304
        assert cached.content == b""
    part = client.get("/assets/index-abc.js", headers={"Accept-Encoding": "gzip", "Range": "bytes=0-9"})
    assert part.status_code == 206
    assert "Content-Encoding" not in part.headers
    assert part.headers["Content-Range"] == f"bytes 0-9/{len(js)}"
    assert part.content == js[:10]


@pytest.mark.parametrize(
    ("accept", "gzipped"),
    [
        ("gzip;q=0", False),
        ("identity, gzip;q=0", False),
        ("br, gzip; q=0.0", False),
        ("gzip;q=0.5", True),
        ("deflate, gzip", True),
        ("br", False),
    ],
)
def test_assets_gzip_follows_quality_values(asset_client, accept, gzipped):
    client, js = asset_client
    resp = client.get("/assets/index-abc.js", headers={"Accept-Encoding": accept})
    assert resp.status_code == 200
    assert (resp.headers.get("Content-Encoding") == "gzip") is gzipped
    assert "accept-encoding" in resp.headers["Vary"].lower()
    assert resp.content == js


def test_304_for_cached_gzip_keeps_weak_tag_and_vary(asset_client):
    client, _ = asset_client
    weak = client.get("/assets/index-abc.js", headers={"Accept-Encoding": "gzip"}).headers["ETag"]
    cached = client.get("/assets/index-abc.js", headers={"Accept-Encoding": "gzip", "If-None-Match": weak})
    assert cached.status_code == 304
    assert cached.headers["ETag"] == weak
    assert "accept-encoding" in cached.headers["Vary"].lower()
    assert cached.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    strong = weak.removeprefix("W/")
    plain = client.get("/assets/index-abc.js", headers={"Accept-Encoding": "identity", "If-None-Match": strong})
    assert plain.status_code == 304
    assert plain.headers["ETag"] == strong
    head = client.head("/assets/index-abc.js", headers={"Accept-Encoding": "gzip"})
    assert head.status_code == 200
    assert "Content-Encoding" not in head.headers


def test_gzip_only_for_large_text_assets(asset_client):
    client, _ = asset_client
    for path in ("/assets/tiny.js", "/assets/logo.png", "/", "/api/health"):
        resp = client.get(path, headers={"Accept-Encoding": "gzip"})
        assert resp.status_code == 200, path
        assert "Content-Encoding" not in resp.headers, path


def test_stream_rejects_without_cookie(client):
    import websockets.exceptions
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises((WebSocketDisconnect, Exception)) as exc_info:
        with client.websocket_connect("/api/stream"):
            pass
    # Close code 4401.
    code = getattr(exc_info.value, "code", None)
    assert code == 4401 or "4401" in str(exc_info.value)


def test_stream_delivers_changed(client, token):
    client.post("/api/auth/login", json={"token": token})
    cookie = client.cookies.get("relay_session")
    with client.websocket_connect("/api/stream", headers={"Cookie": f"relay_session={cookie}"}) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["version"] == "0.1.0"
        # Publish on the bus; expect a coalesced 'changed' message.
        rt = client.app.state.rt
        rt.bus.publish("sessions", "activity")
        msg = ws.receive_json()
        assert msg["type"] == "changed"
        assert "sessions" in msg["topics"]
        assert "activity" in msg["topics"]
