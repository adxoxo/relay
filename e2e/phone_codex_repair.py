#!/usr/bin/env python3
"""Neutral-fixture phone check for the Codex idle, terminal history and mobile
usability repair (plans/relay-codex-phone-repair-2026-10-11.md).

Loads the built frontend (``--dist``) in a real browser with every network
request intercepted: no loopback server, no real relay, no real agent pane.
``/api/*`` calls and the two WebSockets (``/api/stream``,
``/api/sessions/<id>/terminal``) are answered with fixed fake data, so the
page is the real App/SessionPanel/TerminalPane code driven by fake state.

This checks source-to-view behavior only: the History key-bar control sends
the existing ``{type:'scroll', lines, epoch}`` message (never an arrow key),
and the keyboard-open layout keeps the back control and the Latest turn /
Terminal tabs reachable at 320, 375, 390, 768 and 1280 px. It does not touch a
real relay service, a real agent, or any live session; it proves nothing
about the live production outcome.

Usage:
    python e2e/phone_codex_repair.py --dist DIR [--browser PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

BROWSER_EXECUTABLE = "/opt/brave.com/brave/brave"
BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
FAKE_ORIGIN = "http://relay.phone-repair-fixture.invalid"

SESSIONS = [
    {
        "id": "s-agent3", "env": "work2", "env_label": "Work 2", "name": "agent3",
        "tmux_name": "agent3", "kind": "codex", "origin": "discovered", "status": "live",
        "state": "working", "state_source": "terminal", "state_at": 100.0,
        "project_dir": "/home/node/projects", "project": "projects",
        "last_message": None, "activity_at": 100.0, "foreground": None, "pending": None,
        "agent_session_id": None, "created_at": 90.0, "ended_at": None, "end_reason": None,
        "tmux": {"session_id": "$1", "created": 90.0, "pane_id": "%1", "pane_count": 1,
                 "socket": "/tmp/tmux-1000/default", "server_pid": 9},
        "capabilities": {"terminal": True, "prompt": False, "approve": False, "interrupt": True,
                          "conversation": True, "resume": False, "end": True},
        "notes": [],
    },
    {
        "id": "s-agent2", "env": "work2", "env_label": "Work 2", "name": "agent2",
        "tmux_name": "agent2", "kind": "codex", "origin": "discovered", "status": "live",
        "state": "working", "state_source": "terminal", "state_at": 100.0,
        "project_dir": "/home/node/projects", "project": "projects",
        "last_message": None, "activity_at": 100.0, "foreground": None, "pending": None,
        "agent_session_id": None, "created_at": 90.0, "ended_at": None, "end_reason": None,
        "tmux": {"session_id": "$2", "created": 90.0, "pane_id": "%1", "pane_count": 1,
                 "socket": "/tmp/tmux-1000/default", "server_pid": 9},
        "capabilities": {"terminal": True, "prompt": False, "approve": False, "interrupt": True,
                          "conversation": True, "resume": False, "end": True},
        "notes": [],
    },
]

STATE = {
    "generated_at": 100.0, "version": "fixture", "host": {"name": "fixture-workspace"},
    "tailscale_hostname": None,
    "envs": [
        {"id": "work1", "label": "Work 1", "container": "job-work1", "available": True,
         "status": "running", "network": None, "projects_root": "/home/node/projects",
         "error": None, "snapshot_at": 100.0, "agents": {"claude": True, "codex": False},
         "provisioned": True},
        {"id": "work2", "label": "Work 2", "container": "job-work2", "available": True,
         "status": "running", "network": None, "projects_root": "/home/node/projects",
         "error": None, "snapshot_at": 100.0, "agents": {"claude": False, "codex": True},
         "provisioned": True},
    ],
    "sessions": SESSIONS,
    "counts": {"sessions": 2, "working": 2, "needs_input": 0, "finished_unread": 0,
               "by_env": {"work1": 0, "work2": 2}},
    "activity_unread": 0,
}

CONVERSATION = {"supported": True, "reason": None, "items": [
    {"id": "1", "role": "user", "text": "Check the task and run its checks.", "ts": 90.0, "tool": None},
    {"id": "2", "role": "assistant", "text": "I will check the task and its result.", "ts": 91.0, "tool": None},
]}


class StepError(AssertionError):
    pass


class Reporter:
    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0

    def run(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - report and keep going
            self.failed += 1
            print(f"FAIL  {name}: {exc}")
            return None
        self.passed += 1
        print(f"PASS  {name}")
        return value

    def summary(self) -> int:
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} steps passed, {self.failed} failed")
        return 0 if self.failed == 0 else 1


def _json_route(route: Any, body: Any) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def install_fixtures(page: Any, dist: Path) -> None:
    """Serve the build from disk and answer every API call and socket with fake data."""

    def handle_static(route: Any) -> None:
        url = route.request.url
        path = url[len(FAKE_ORIGIN):].split("?", 1)[0] or "/"
        if path == "/":
            path = "/index.html"
        local = dist / path.lstrip("/")
        if local.is_file():
            route.fulfill(path=str(local))
        else:
            route.fulfill(path=str(dist / "index.html"))

    def handle_api(route: Any) -> None:
        url = route.request.url
        if "/api/auth/me" in url:
            _json_route(route, {"authenticated": True})
        elif "/api/state" in url:
            _json_route(route, STATE)
        elif "/conversation" in url:
            _json_route(route, CONVERSATION)
        elif "/api/activity" in url:
            _json_route(route, {"items": [], "unread": 0})
        else:
            _json_route(route, {})

    def handle_stream_ws(ws: Any) -> None:
        ws.connect_to_server()

    def handle_terminal_ws(ws: Any) -> None:
        # No server connection: every reply is fabricated here, so nothing
        # ever reaches a real agent pane.
        def on_message(message: Any) -> None:
            if not isinstance(message, str):
                return
            try:
                frame = json.loads(message)
            except ValueError:
                return
            if frame.get("type") == "resize":
                return
            if frame.get("type") == "input":
                # A key during a history view ends it; terminal_input_seen()
                # checks this never carries an arrow sequence (never sent here).
                INPUT_SEEN.append(frame.get("data"))
                return
            if frame.get("type") == "scroll":
                epoch = frame.get("epoch")
                SCROLL_SEEN.append(frame)
                ws.send(json.dumps({"type": "history", "epoch": epoch, "status": "loading"}))
                ws.send(json.dumps({
                    "type": "history", "epoch": epoch, "status": "page", "first": True,
                    "items": [
                        {"role": "user", "text": "Here is the earlier request."},
                        {"role": "assistant", "text": "Here is the earlier result."},
                    ],
                    "before": 0, "more": False, "note": "Start of the saved conversation.",
                }))

        ws.on_message(on_message)

    page.route(f"{FAKE_ORIGIN}/**", lambda route: (
        handle_api(route) if "/api/" in route.request.url else handle_static(route)
    ))
    page.route_web_socket(f"{FAKE_ORIGIN.replace('http://', 'ws://')}/api/stream", handle_stream_ws)
    page.route_web_socket(
        lambda ws_url: "/terminal" in ws_url, handle_terminal_ws
    )


SCROLL_SEEN: list[dict[str, Any]] = []
INPUT_SEEN: list[Any] = []


def run(dist: Path, browser_path: str | None) -> int:
    from playwright.sync_api import sync_playwright

    reporter = Reporter()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=browser_path or BROWSER_EXECUTABLE, args=BROWSER_ARGS
        )
        try:
            for width, height, label in [(320, 690, "320px"), (375, 667, "375px"), (390, 844, "390px"),
                                          (768, 1024, "768px"), (1280, 800, "1280px")]:
                is_phone = width <= 760
                # A real phone viewport is also a touch, mobile-emulated context.
                # Desktop/tablet widths stay a plain mouse context: the key bar
                # is phone-only by design (body.touch), so 768/1280 must not
                # force it on just to make a fixture pass; they exercise the
                # desktop wheel-history gesture instead.
                context = browser.new_context(
                    viewport={"width": width, "height": height},
                    has_touch=is_phone, is_mobile=is_phone,
                )
                page = context.new_page()
                install_fixtures(page, dist)
                page.goto(f"{FAKE_ORIGIN}/")

                reporter.run(f"{label}: app loads with no horizontal overflow", lambda: (
                    page.wait_for_selector(".app-shell", timeout=10000),
                    _assert_no_overflow(page),
                ))

                if is_phone:
                    reporter.run(f"{label}: compact phone overview replaces the hero", lambda: (
                        _assert_visible(page, ".phone-head h1", "Your agents"),
                        _assert_hidden(page, ".intro"),
                        _assert_hidden(page, ".metrics"),
                    ))

                reporter.run(f"{label}: open a session and reach the terminal", lambda: (
                    page.click(".agent-card >> nth=0"),
                    page.click("button:has-text('Terminal')"),
                    page.wait_for_selector(".terminal-wrap", timeout=5000),
                ))

                if is_phone:
                    reporter.run(f"{label}: status badge shows beside the session name", lambda: (
                        _assert_visible(page, ".chat-head .chat-state-mobile", "Working"),
                    ))
                    reporter.run(f"{label}: Interrupt/End sit in their own mobile row", lambda: (
                        _assert_hidden(page, ".chat-head .chat-actions"),
                        _assert_visible_display(page, ".chat-actions-mobile"),
                    ))
                    reporter.run(f"{label}: touch key-bar targets are >= 44 px, bar can wrap", lambda: (
                        _assert_key_bar_sizes(page, require_single_row=width >= 375),
                    ))

                    SCROLL_SEEN.clear()
                    INPUT_SEEN.clear()
                    reporter.run(
                        f"{label}: History control sends the existing scroll message, no arrow keys",
                        lambda: (
                            page.click(".key-bar >> text=History"),
                            page.wait_for_selector(".term-history", timeout=5000),
                            _assert_history_request_shape(),
                        ),
                    )
                    reporter.run(
                        f"{label}: Back to live does not cover the key bar",
                        lambda: _assert_pill_clears_key_bar(page),
                    )

                    reporter.run(
                        f"{label}: keyboard-open keeps Back and the view tabs reachable",
                        lambda: _assert_kb_open_layout(page),
                    )
                else:
                    SCROLL_SEEN.clear()
                    INPUT_SEEN.clear()
                    reporter.run(
                        f"{label}: desktop mouse wheel opens history (no key bar required)",
                        lambda: (
                            _assert_hidden(page, ".key-bar"),
                            page.hover(".terminal-host"),
                            page.mouse.wheel(0, -120),
                            page.wait_for_selector(".term-history", timeout=5000),
                            _assert_history_request_shape(),
                        ),
                    )

                context.close()
        finally:
            browser.close()
    return reporter.summary()


def _assert_no_overflow(page: Any) -> None:
    overflow = page.evaluate(
        "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
    )
    if overflow:
        raise StepError("page overflows horizontally")


def _assert_visible(page: Any, selector: str, text_contains: str) -> None:
    el = page.query_selector(selector)
    if el is None or not el.is_visible():
        raise StepError(f"{selector} is not visible")
    if text_contains not in (el.inner_text() or ""):
        raise StepError(f"{selector} did not contain {text_contains!r}")


def _assert_visible_display(page: Any, selector: str) -> None:
    el = page.query_selector(selector)
    if el is None:
        raise StepError(f"{selector} not found")
    display = el.evaluate("el => getComputedStyle(el).display")
    if display == "none":
        raise StepError(f"{selector} is display:none")


def _assert_hidden(page: Any, selector: str) -> None:
    el = page.query_selector(selector)
    if el is None:
        return
    display = el.evaluate("el => getComputedStyle(el).display")
    if display != "none":
        raise StepError(f"{selector} is visible (display:{display})")


def _assert_key_bar_sizes(page: Any, *, require_single_row: bool) -> None:
    sizes = page.eval_on_selector_all(
        ".key-bar button",
        "els => els.map(e => ({w: e.getBoundingClientRect().width, h: e.getBoundingClientRect().height, "
        "top: e.getBoundingClientRect().top, label: e.textContent.trim(), "
        "fits: e.scrollWidth <= e.clientWidth}))",
    )
    if not sizes:
        raise StepError("no key-bar buttons found")
    for s in sizes:
        if s["h"] < 44:
            raise StepError(f"key-bar button height {s['h']} < 44px")
    bar_overflow = page.eval_on_selector(
        ".key-bar", "el => el.scrollWidth > el.parentElement.clientWidth + 1"
    )
    if bar_overflow:
        raise StepError("key-bar overflows its container instead of wrapping")
    rows = len({round(s["top"]) for s in sizes})
    if require_single_row:
        if rows != 1:
            raise StepError(f"the approved preview has one key-bar row; this viewport wrapped to {rows}")
        for s in sizes:
            if s["w"] < 44:
                raise StepError(f"key-bar button width {s['w']} < 44px in a single row")
            if not s["fits"]:
                raise StepError(f"key-bar label {s['label']!r} overflows its {s['w']:.1f}px button")


def _assert_pill_clears_key_bar(page: Any) -> None:
    rects = page.evaluate(
        """() => {
          const r = (sel) => { const el = document.querySelector(sel); return el && el.getBoundingClientRect().toJSON(); };
          return {pill: r('.term-live-pill'), bar: r('.key-bar')};
        }"""
    )
    pill, bar = rects["pill"], rects["bar"]
    if pill is None or bar is None:
        raise StepError(f"missing element: {rects}")
    overlap = not (pill["bottom"] <= bar["top"] or pill["top"] >= bar["bottom"]
                   or pill["right"] <= bar["left"] or pill["left"] >= bar["right"])
    if overlap:
        raise StepError(f"Back to live {pill} overlaps the key bar {bar}")


def _assert_history_request_shape() -> None:
    if not SCROLL_SEEN:
        raise StepError("the History control sent no scroll request")
    frame = SCROLL_SEEN[-1]
    if "lines" not in frame or "epoch" not in frame:
        raise StepError(f"scroll request missing lines/epoch: {frame}")
    if INPUT_SEEN:
        raise StepError(f"the History control also sent input to the agent: {INPUT_SEEN}")


def _assert_kb_open_layout(page: Any) -> None:
    # One atomic evaluate: Root.tsx's own visualViewport listener reacts to a
    # real resize between round-trips and can flip body.kb-open back off
    # before a second call reads it, which is a race in this harness, not in
    # the app (a real keyboard does not toggle on and off between frames).
    result = page.evaluate(
        """() => {
          document.documentElement.style.setProperty('--vvh', '420px');
          document.body.classList.add('kb-open');
          const disp = (sel) => {
            const el = document.querySelector(sel);
            return el ? getComputedStyle(el).display : null;
          };
          const conv = document.querySelector('.conversation');
          return {
            chatHead: disp('.chat-head'),
            chatTabs: disp('.chat-tabs'),
            actionsMobile: disp('.chat-actions-mobile'),
            mobileNav: disp('.mobile-nav'),
            convHeight: conv ? conv.getBoundingClientRect().height : null,
          };
        }"""
    )
    if result["chatHead"] == "none":
        raise StepError("chat-head hid with the keyboard open")
    if result["chatTabs"] == "none":
        raise StepError("chat-tabs (Latest turn / Terminal) hid with the keyboard open")
    if result["actionsMobile"] != "none":
        raise StepError("chat-actions-mobile stayed visible with the keyboard open")
    if result["mobileNav"] != "none":
        raise StepError("mobile-nav stayed visible with the keyboard open")
    if result["convHeight"] is None or abs(result["convHeight"] - 420) > 2:
        raise StepError(f".conversation height {result['convHeight']} does not track --vvh (420px)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path, help="Built frontend directory (index.html + assets/)")
    parser.add_argument("--browser", default=None, help="Browser executable path")
    args = parser.parse_args()
    if not (args.dist / "index.html").is_file():
        print(f"No index.html under {args.dist}", file=sys.stderr)
        return 2
    return run(args.dist, args.browser)


if __name__ == "__main__":
    sys.exit(main())
