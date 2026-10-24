"""Browser smoke checks and configured test-command runs for previews.

A smoke check drives Playwright (sync API in a worker thread) against the
preview's verified phone URL through its trusted gateway when a fresh route
proof exists, else the reachable local URL, at a phone and a desktop viewport,
records the page result, and writes a screenshot per viewport. A test-command
run executes the preview's configured test command inside the container and
keeps the tail of its output.

Artifacts live under ``settings.artifacts_dir/<run_id>/``. Only the last 20
runs per preview are kept.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from . import activity, previews
from .errors import ApiError
from .db import Database, new_id, now
from .runtime import Runtime

RETENTION = 20
TEST_TIMEOUT = 900.0
OUTPUT_MAX_LINES = 400
OUTPUT_MAX_BYTES = 200 * 1024
FAILED_REQUESTS_MAX = 50
NAV_TIMEOUT_MS = 30000

VIEWPORTS = ("phone", "desktop")
DESKTOP_SIZE = {"width": 1440, "height": 900}
PHONE_DEVICE = "Pixel 7"

BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


# -- views -------------------------------------------------------------------

def _artifacts_for(rt: Runtime, run_id: str) -> list[dict[str, str]]:
    folder = rt.settings.artifacts_dir / run_id
    if not folder.is_dir():
        return []
    items: list[dict[str, str]] = []
    for entry in sorted(folder.iterdir()):
        if entry.is_file():
            items.append({"name": entry.name, "url": f"/api/checks/{run_id}/artifacts/{entry.name}"})
    return items


def check_run_view(rt: Runtime, row: sqlite3.Row) -> dict[str, Any]:
    from .db import loads

    return {
        "id": row["id"],
        "preview_id": row["preview_id"],
        "kind": row["kind"],
        "status": row["status"],
        "target_url": row["target_url"],
        "summary": row["summary"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "result": loads(row["result_json"]),
        "artifacts": _artifacts_for(rt, row["id"]),
    }


def get_run(db: Database, run_id: str) -> sqlite3.Row:
    row = db.one("SELECT * FROM check_runs WHERE id = ?", (run_id,))
    if row is None:
        raise ApiError(404, "not_found", "The check run does not exist.")
    return row


def list_runs(db: Database, *, preview_id: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    limit = max(1, min(int(limit), 100))
    if preview_id is not None:
        return db.query(
            "SELECT * FROM check_runs WHERE preview_id = ? ORDER BY started_at DESC LIMIT ?",
            (preview_id, limit),
        )
    return db.query("SELECT * FROM check_runs ORDER BY started_at DESC LIMIT ?", (limit,))


def latest_run(db: Database, preview_id: str) -> sqlite3.Row | None:
    return db.one(
        "SELECT * FROM check_runs WHERE preview_id = ? ORDER BY started_at DESC LIMIT 1",
        (preview_id,),
    )


# -- run lifecycle -----------------------------------------------------------

async def _target(rt: Runtime, preview_row: sqlite3.Row) -> tuple[str | None, bool]:
    """Return (target URL, through the gateway).

    The verified phone URL of the preview's gateway when a route proof is
    fresh (relay proves it once more when needed), else the reachable local
    URL. Never an unverified tailnet URL. An unpublished Work 1 port has no
    target.
    """
    from . import preview_gateway

    _, host_port = await previews.row_host_port(rt, preview_row)
    if host_port is None:
        return None, False
    gw = preview_gateway.gateway_for(rt.settings, preview_row["env"], host_port)
    mgr = preview_gateway.manager(rt)
    if gw is not None and mgr is not None and gw.public_origin:
        proof = mgr.proofs.get(gw.listen_port)
        if not preview_gateway.fresh(proof):
            proof = await mgr.verify(gw, force=True)
        if preview_gateway.fresh(proof):
            return proof["url"], True
    return previews.build_urls(rt, host_port)["local"], False


def _finish_run(rt: Runtime, run_id: str, status: str, *, summary: str, result: dict[str, Any] | None) -> None:
    from .db import dumps

    rt.db.update(
        "check_runs",
        run_id,
        {
            "status": status,
            "summary": summary[:500] if summary else summary,
            "result_json": dumps(result),
            "finished_at": now(),
        },
    )
    row = rt.db.one("SELECT * FROM check_runs WHERE id = ?", (run_id,))
    if row is not None:
        _publish_check(rt, row, status)


def _publish_check(rt: Runtime, run_row: sqlite3.Row, status: str) -> None:
    rt.bus.publish("checks")
    if status in ("running",):
        return
    preview_row = None
    if run_row["preview_id"]:
        preview_row = rt.db.one("SELECT * FROM previews WHERE id = ?", (run_row["preview_id"],))
    name = preview_row["name"] if preview_row else "preview"
    env = preview_row["env"] if preview_row else None
    attention = status == "failed"
    kind_word = "test" if run_row["kind"] == "test_command" else "check"
    activity.record(
        rt.db,
        kind="check",
        title=f"{kind_word} {name}: {status}",
        body=run_row["summary"],
        session_id=None,
        env=env,
        attention=attention,
        dedupe_key=None,
        bus=rt.bus,
    )


async def start_smoke_check(rt: Runtime, preview_id: str, viewports: list[str] | None = None) -> sqlite3.Row:
    preview_row = previews.get_preview(rt.db, preview_id)
    chosen = _clean_viewports(viewports)
    target, via_gateway = await _target(rt, preview_row)
    run_id = _create_run(rt, preview_id, "smoke", target)
    previews.spawn(rt, _run_smoke(rt, run_id, preview_id, target, chosen, via_gateway=via_gateway))
    return get_run(rt.db, run_id)


async def start_test_command(rt: Runtime, preview_id: str) -> sqlite3.Row:
    preview_row = previews.get_preview(rt.db, preview_id)
    if not preview_row["test_command"]:
        raise ApiError(409, "no_test_command", "This preview has no test command.")
    run_id = _create_run(rt, preview_id, "test_command", None)
    previews.spawn(rt, _run_test(rt, run_id, preview_id))
    return get_run(rt.db, run_id)


def _clean_viewports(viewports: list[str] | None) -> list[str]:
    if not viewports:
        return list(VIEWPORTS)
    chosen = [v for v in VIEWPORTS if v in viewports]
    return chosen or list(VIEWPORTS)


def _create_run(rt: Runtime, preview_id: str, kind: str, target: str | None) -> str:
    run_id = new_id()
    rt.db.insert(
        "check_runs",
        {
            "id": run_id,
            "preview_id": preview_id,
            "kind": kind,
            "status": "running",
            "target_url": target,
            "summary": None,
            "result_json": None,
            "started_at": now(),
            "finished_at": None,
        },
    )
    rt.bus.publish("checks")
    (rt.settings.artifacts_dir / run_id).mkdir(parents=True, exist_ok=True)
    return run_id


# -- smoke check -------------------------------------------------------------

async def _run_smoke(
    rt: Runtime, run_id: str, preview_id: str, target: str | None, viewports: list[str], *, via_gateway: bool = False
) -> None:
    folder = rt.settings.artifacts_dir / run_id
    if rt.settings.browser_executable is None:
        _write_result_json(folder, {"error": "no_browser"})
        _finish_run(rt, run_id, "error", summary="No browser is configured for checks.", result={"error": "no_browser"})
        _retain(rt, preview_id)
        return
    if not target:
        _finish_run(rt, run_id, "error", summary="No target URL is available.", result={"error": "no_target"})
        _retain(rt, preview_id)
        return
    # The gateway needs a relay session. The check browser gets its own short
    # session, which relay deletes when the run ends. A direct local URL never
    # gets a relay cookie. _playwright_smoke installs the origin guard before
    # it adds the cookie.
    cookie_value = start_check_session(rt) if via_gateway and rt.auth is not None else None
    credential = None
    if cookie_value is not None:
        from .auth import COOKIE_NAME, SECURE_COOKIE_NAME

        credential = {"name": SECURE_COOKIE_NAME if target.startswith("https://") else COOKIE_NAME, "value": cookie_value}
    try:
        result = await asyncio.to_thread(
            _playwright_smoke, rt.settings.browser_executable, target, viewports, folder, credential
        )
    except Exception as exc:  # pragma: no cover - defensive
        _finish_run(rt, run_id, "error", summary=f"The check failed to run: {exc}", result={"error": str(exc)})
        _retain(rt, preview_id)
        return
    finally:
        if cookie_value is not None:
            end_check_session(rt, cookie_value)
    status = "failed" if result.get("failed") else "passed"
    _write_result_json(folder, result)
    _finish_run(rt, run_id, status, summary=result.get("summary", ""), result=result)
    _retain(rt, preview_id)


def _origin_key(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) with the default port filled in, else None."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.scheme not in ("http", "https", "ws", "wss"):
        return None
    default = 443 if parts.scheme in ("https", "wss") else 80
    return parts.scheme, parts.hostname.lower(), port or default


class OriginGuard:
    """Keep a credentialed check on one exact origin.

    A cookie has no port scope, so a request to another port of the same host
    would carry the relay session to a direct project app. The guard is
    installed before the cookie exists and sees every request of the context:

    - HTTP(S): only the exact gateway origin continues; any other request is
      aborted before it leaves the browser.
    - WebSocket: only ws/wss to the same host and port connects, so HMR keeps
      working. Any other socket is never connected: no handshake is sent.
    - Redirects: Playwright does not route the next hop of a redirect, so the
      gateway refuses a cross-origin redirect for a check session
      (``check_session``). The guard still records any redirected request to
      another origin, and the final page must stay on the gateway origin.

    Service workers are blocked in the context, so none can bypass the routes.

    Every other origin is blocked. Only an attempt on the gateway's own host
    (another port or scheme) could carry the host-only cookie, so only that
    fails the check (``violations``). A blocked third-party host (a font CDN)
    is reported as a warning (``third_party``).
    """

    def __init__(self, target: str):
        key = _origin_key(target)
        if key is None or key[0] not in ("http", "https"):
            raise ValueError("the guard needs an http(s) target")
        self.http = key
        self.ws = ("wss" if key[0] == "https" else "ws", key[1], key[2])
        self.violations: list[str] = []
        self.third_party: list[str] = []

    def same_host(self, url: str) -> bool:
        key = _origin_key(url)
        return key is None or key[1] == self.http[1]

    def _note(self, what: str, url: str) -> None:
        key = _origin_key(url)
        where = f"{key[0]}://{key[1]}:{key[2]}" if key else "an invalid URL"
        bucket = self.violations if self.same_host(url) else self.third_party
        if len(bucket) < 20 and f"{what} to {where}" not in bucket:
            bucket.append(f"{what} to {where}")

    def allowed(self, url: str, *, websocket: bool = False) -> bool:
        return _origin_key(url) == (self.ws if websocket else self.http)

    def install(self, context: Any) -> None:
        context.route(lambda url: True, self._http)
        context.route_web_socket(lambda url: True, self._websocket)
        context.on("request", self._request)

    def _http(self, route: Any) -> None:
        url = route.request.url
        if self.allowed(url):
            route.continue_()
            return
        self._note("a request", url)
        route.abort("blockedbyclient")

    def _request(self, request: Any) -> None:
        if request.redirected_from is not None and not self.allowed(request.url):
            self._note("a redirect", request.url)

    def _websocket(self, ws: Any) -> None:
        # Runs on Playwright's dispatcher: only non-blocking calls here.
        if self.allowed(ws.url, websocket=True):
            ws.connect_to_server()
            return
        # Not connected: the socket stays inside the page and no handshake is sent.
        self._note("a WebSocket", ws.url)


def start_check_session(rt: Runtime) -> str:
    """A short relay session for one gateway check. The gateway applies the
    stricter check rules to it (no redirect to another origin)."""
    from .auth import _sha256_hex

    value = rt.auth.create_session("relay-check")
    rt.cache.setdefault("check_sessions", set()).add(_sha256_hex(value))
    return value


def end_check_session(rt: Runtime, value: str) -> None:
    from .auth import _sha256_hex

    rt.auth.delete_session(value)
    rt.cache.setdefault("check_sessions", set()).discard(_sha256_hex(value))


def _playwright_smoke(
    browser_executable: str,
    target: str,
    viewports: list[str],
    folder: Path,
    credential: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Blocking Playwright run. Executed in a worker thread.

    ``credential`` (a relay session cookie for a gateway check) is added only
    after the ``OriginGuard`` is in place, in a context without service workers.
    """
    from playwright.sync_api import sync_playwright

    per_viewport: dict[str, Any] = {}
    failed = False
    reasons: list[str] = []
    warnings: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=browser_executable,
            headless=True,
            args=BROWSER_ARGS,
        )
        try:
            devices = pw.devices
            for viewport in viewports:
                options: dict[str, Any] = {"ignore_https_errors": True}
                if credential:
                    options["service_workers"] = "block"
                if viewport == "phone" and PHONE_DEVICE in devices:
                    context = browser.new_context(**devices[PHONE_DEVICE], **options)
                else:
                    context = browser.new_context(viewport=DESKTOP_SIZE, **options)
                guard: OriginGuard | None = None
                if credential:
                    guard = OriginGuard(target)
                    guard.install(context)
                    context.add_cookies(
                        [
                            {
                                "name": credential["name"],
                                "value": credential["value"],
                                "url": target,
                                "secure": target.startswith("https://"),
                                "httpOnly": True,
                                "sameSite": "Strict",
                            }
                        ]
                    )
                console_errors: list[str] = []
                page_errors: list[str] = []
                failed_requests: list[dict[str, Any]] = []
                page = context.new_page()

                def _on_console(msg: Any) -> None:
                    if msg.type == "error":
                        console_errors.append(msg.text[:500])

                def _on_pageerror(err: Any) -> None:
                    page_errors.append(str(err)[:500])

                def _on_requestfailed(request: Any) -> None:
                    if guard is not None and not guard.allowed(request.url) and not guard.same_host(request.url):
                        return  # a third-party host that the guard blocked: a warning, below
                    if len(failed_requests) < FAILED_REQUESTS_MAX:
                        failure = request.failure
                        failed_requests.append(
                            {"url": request.url[:500], "kind": "network", "detail": str(failure)[:200]}
                        )

                def _on_response(response: Any) -> None:
                    if response.status >= 400 and len(failed_requests) < FAILED_REQUESTS_MAX:
                        failed_requests.append(
                            {"url": response.url[:500], "kind": "status", "status": response.status}
                        )

                page.on("console", _on_console)
                page.on("pageerror", _on_pageerror)
                page.on("requestfailed", _on_requestfailed)
                page.on("response", _on_response)

                entry: dict[str, Any] = {
                    "status": None,
                    "final_url": None,
                    "title": None,
                    "console_errors": console_errors,
                    "page_errors": page_errors,
                    "failed_requests": failed_requests,
                    "screenshot": f"{viewport}.png",
                }
                try:
                    response = page.goto(target, wait_until="load", timeout=NAV_TIMEOUT_MS)
                    status = response.status if response else None
                    entry["status"] = status
                    entry["final_url"] = page.url
                    entry["title"] = page.title()
                    if status is not None and status >= 400:
                        failed = True
                        reasons.append(f"{viewport}: main response {status}")
                except Exception as exc:
                    failed = True
                    entry["nav_error"] = str(exc)[:300]
                    reasons.append(f"{viewport}: navigation failed")
                try:
                    page.screenshot(path=str(folder / f"{viewport}.png"), full_page=False)
                except Exception:
                    pass

                if guard is not None:
                    if not guard.allowed(page.url):
                        guard._note("the final page", page.url)
                    entry["blocked"] = list(guard.violations)
                    entry["blocked_third_party"] = list(guard.third_party)
                    for item in guard.third_party:
                        warnings.append(f"{viewport}: blocked {item} (only the gateway origin is allowed)")
                    if guard.violations:
                        failed = True
                        reasons.append(f"{viewport}: the app tried to leave the gateway origin ({guard.violations[0]})")
                if page_errors:
                    failed = True
                    reasons.append(f"{viewport}: {len(page_errors)} page error(s)")
                for fr in failed_requests:
                    if fr.get("kind") == "network":
                        failed = True
                        reasons.append(f"{viewport}: a request failed at the network level")
                    elif fr.get("kind") == "status" and int(fr.get("status", 0)) >= 500:
                        failed = True
                        reasons.append(f"{viewport}: a subresource returned {fr['status']}")
                    elif fr.get("kind") == "status":
                        warnings.append(f"{viewport}: subresource {fr.get('status')} {fr.get('url', '')[:80]}")

                per_viewport[viewport] = entry
                context.close()
        finally:
            browser.close()

    summary = "All viewports loaded without errors." if not failed else "; ".join(dict.fromkeys(reasons))[:500]
    if warnings and not failed:
        summary = "Loaded with warnings: " + "; ".join(dict.fromkeys(warnings))[:400]
    return {
        "target": target,
        "failed": failed,
        "summary": summary,
        "warnings": warnings,
        "viewports": per_viewport,
    }


def _write_result_json(folder: Path, result: dict[str, Any]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True))


# -- test command ------------------------------------------------------------

async def _run_test(rt: Runtime, run_id: str, preview_id: str) -> None:
    folder = rt.settings.artifacts_dir / run_id
    preview_row = previews.get_preview(rt.db, preview_id)
    env = rt.settings.env(preview_row["env"])
    command = preview_row["test_command"]
    try:
        res = await rt.podman.exec(
            env,
            ["bash", "-lc", command],
            workdir=preview_row["project_dir"],
            timeout=TEST_TIMEOUT,
        )
        code = res.code
        output = res.out + (("\n" + res.err) if res.err else "")
        timed_out = False
    except (asyncio.TimeoutError, TimeoutError):
        code = -1
        output = "The test command timed out after 900 s."
        timed_out = True
    except Exception as exc:  # pragma: no cover - defensive
        code = -1
        output = f"The test command failed to run: {exc}"
        timed_out = False
    tail = _tail(output, OUTPUT_MAX_LINES, OUTPUT_MAX_BYTES)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "output.txt").write_text(tail)
    passed = code == 0 and not timed_out
    result = {"exit_code": code, "timed_out": timed_out}
    _write_result_json(folder, result)
    status = "passed" if passed else "failed"
    summary = "The test command passed." if passed else (
        "The test command timed out." if timed_out else f"The test command failed (exit {code})."
    )
    _finish_run(rt, run_id, status, summary=summary, result=result)
    _retain(rt, preview_id)


def _tail(text: str, max_lines: int, max_bytes: int) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    out = "\n".join(lines)
    raw = out.encode("utf-8", "replace")
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
        out = raw.decode("utf-8", "replace")
    return out


# -- retention and startup ---------------------------------------------------

def _retain(rt: Runtime, preview_id: str) -> None:
    """Keep only the last RETENTION runs per preview. Delete older rows and folders."""
    rows = rt.db.query(
        "SELECT id FROM check_runs WHERE preview_id = ? ORDER BY started_at DESC",
        (preview_id,),
    )
    stale = rows[RETENTION:]
    for row in stale:
        run_id = row["id"]
        rt.db.execute("DELETE FROM check_runs WHERE id = ?", (run_id,))
        folder = rt.settings.artifacts_dir / run_id
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
    if stale:
        rt.bus.publish("checks")


def reconcile_startup(rt: Runtime) -> None:
    """Runs left 'running' after a restart become 'error'."""
    rows = rt.db.query("SELECT * FROM check_runs WHERE status = 'running'")
    for row in rows:
        rt.db.update(
            "check_runs",
            row["id"],
            {"status": "error", "summary": "Interrupted by a relay restart.", "finished_at": now()},
        )
    if rows:
        rt.bus.publish("checks")
