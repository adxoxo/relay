"""Tests for relay.auth via the running app (TestClient)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from relay.app import create_app
from relay.auth import Auth
from relay.config import Settings

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def app(rt):
    return create_app(runtime=rt, features=False)


@pytest.fixture
def token(rt) -> str:
    auth = Auth(rt.settings, rt.db)
    return auth.ensure_token()


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE) as c:
        yield c


def _login(client: TestClient, token: str, **kwargs):
    return client.post("/api/auth/login", json={"token": token}, **kwargs)


def test_login_success_sets_cookie(client, token):
    resp = _login(client, token)
    assert resp.status_code == 204
    set_cookie = resp.headers.get("set-cookie", "")
    assert "relay_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=strict" in set_cookie.lower()
    assert "Path=/" in set_cookie
    assert "Max-Age=" in set_cookie
    # http request: no Secure flag.
    assert "Secure" not in set_cookie


def test_login_failure(client, token):
    resp = _login(client, "wrong-token")
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_token"


def test_rate_limit_after_ten_failures(client, token):
    for _ in range(10):
        assert _login(client, "bad").status_code == 401
    resp = _login(client, "bad")
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    # A correct token is still rate limited within the window.
    assert _login(client, token).status_code == 429


def test_cookie_secure_flag_over_https(client, token):
    resp = _login(client, token, headers={"X-Forwarded-Proto": "https"})
    assert resp.status_code == 204
    assert "Secure" in resp.headers.get("set-cookie", "")


def test_https_cookie_is_host_prefixed_and_http_login_still_works(client, token):
    https = {"X-Forwarded-Proto": "https"}
    resp = _login(client, token, headers=https)
    cookies = resp.headers.get_list("set-cookie")
    assert any(c.startswith("relay_session=") and "Max-Age=0" in c for c in cookies)  # the old plain cookie expires
    set_cookie = next(c for c in cookies if c.startswith("__Host-relay_session="))
    assert set_cookie.startswith("__Host-relay_session=")
    assert "Secure" in set_cookie and "Path=/" in set_cookie and "Domain" not in set_cookie
    value = set_cookie.split(";", 1)[0].split("=", 1)[1]
    client.cookies.clear()
    # An https request reads only the prefixed cookie. A plain copy (for
    # example one that a page on another port planted) is ignored.
    assert client.get("/api/auth/me", headers={**https, "Cookie": f"relay_session={value}"}).status_code == 401
    assert client.get("/api/auth/me", headers={**https, "Cookie": f"__Host-relay_session={value}"}).status_code == 200
    # Local http login (the laptop at 127.0.0.1) keeps the plain name without Secure.
    local = _login(client, token)
    assert local.headers["set-cookie"].startswith("relay_session=") and "Secure" not in local.headers["set-cookie"]
    assert client.get("/api/auth/me").status_code == 200


def test_logout_invalidates_session(client, token):
    _login(client, token)
    # Authenticated /me works.
    assert client.get("/api/auth/me").status_code == 200
    # Logout needs an allowed Origin (cookie auth, POST).
    resp = client.post("/api/auth/logout", headers={"Origin": BASE})
    assert resp.status_code == 204
    # Cookie is cleared client-side; the stored session is gone server-side too.
    assert client.get("/api/auth/me").status_code == 401


def test_bearer_token_works(client, token):
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["via"] == "bearer"


def test_me_requires_credentials(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_cookie_post_without_allowed_origin_is_403(client, token):
    _login(client, token)
    resp = client.post("/api/auth/logout", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    assert resp.json()["error"] == "bad_origin"


def test_origin_must_match_exactly(client, token):
    _login(client, token)
    for bad in (BASE + "/", BASE + "//", BASE + " "):
        resp = client.post("/api/auth/logout", headers={"Origin": bad})
        assert resp.status_code == 403, bad
    assert client.post("/api/auth/logout", headers={"Origin": BASE}).status_code == 204


def test_cookie_post_without_origin_is_403(client, token):
    _login(client, token)
    resp = client.post("/api/auth/logout")
    assert resp.status_code == 403
    assert resp.json()["error"] == "bad_origin"


def test_bearer_post_needs_no_origin(client, token):
    resp = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 204


def test_rotate_token_drops_sessions(rt, token):
    auth = Auth(rt.settings, rt.db)
    auth.ensure_token()
    cookie = auth.create_session("agent")
    assert auth._lookup_session(cookie) is not None
    new_token = auth.rotate_token()
    assert new_token != token
    assert auth._lookup_session(cookie) is None


def test_forbidden_identity(tmp_path):
    from relay.config import EnvConfig
    from relay.db import Database
    from relay.runtime import build_runtime

    envs = {"work2": EnvConfig(id="work2", label="W2", container="job-work2", host_home=tmp_path / "w2")}
    envs["work2"].host_home.mkdir(parents=True)
    state = tmp_path / "state"
    state.mkdir()
    settings = Settings(
        state_dir=state,
        envs=envs,
        frontend_dist=tmp_path / "dist",
        browser_executable=None,
        tailscale_logins=("adam@example.com",),
    )
    rt = build_runtime(settings, db=Database(settings.db_path))
    app = create_app(runtime=rt, features=False)
    with TestClient(app, base_url=BASE) as client:
        auth: Auth = rt.auth
        tok = auth.ensure_token()
        # Allowed identity passes.
        ok = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {tok}", "Tailscale-User-Login": "adam@example.com"},
        )
        assert ok.status_code == 200
        assert ok.json()["tailscale_login"] == "adam@example.com"
        # Unlisted identity is forbidden.
        bad = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {tok}", "Tailscale-User-Login": "mallory@example.com"},
        )
        assert bad.status_code == 403
        assert bad.json()["error"] == "forbidden_identity"
    rt.db.close()


def test_new_token_never_starts_with_an_option_character(monkeypatch):
    # A token such as "-abc" breaks "--token <value>" command lines.
    from relay import auth as auth_mod

    samples = iter(["-starts-with-dash" + "x" * 26, "_underscore" + "y" * 32, "Good" + "z" * 39])
    monkeypatch.setattr(auth_mod.secrets, "token_urlsafe", lambda n: next(samples))
    assert auth_mod.new_token().startswith("Good")


def test_rotation_in_another_process_revokes_the_old_token(settings):
    # ``relay token --rotate`` uses its own Auth instance (the CLI process).
    # The running service must stop accepting the old token without a restart.
    from relay.auth import Auth
    from relay.db import Database

    db = Database(settings.db_path)
    service = Auth(settings, db)
    old = service.ensure_token()
    assert service.verify_token(old)

    new = Auth(settings, db).rotate_token()
    assert not service.verify_token(old)
    assert service.verify_token(new)
    db.close()
