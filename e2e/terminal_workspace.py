#!/usr/bin/env python3
"""Neutral-fixture check for the terminal workspace repair (2026-10-13).

Loads a built frontend (``--dist``) in Brave with every request intercepted:
no loopback server, no real relay, no real agent or shell. ``/api/*`` calls
and both WebSockets are answered here with fixed fake data, and every terminal
input frame is recorded per session id, so the checks read exactly what the
page would send.

Contracts checked: a session opens on its terminal (no Latest turn tab), the
pending request shows beside it, the touch text field (composition, local
edits, Insert, Send with a bracketed paste and one Enter), key-bar keys without
focus, history modes and Back to live, draft isolation per session (switch
strip and two panes), disabled controls while disconnected and no input
replay after reconnect, keyboard geometry, phone landscape, tablet and desktop.

``--before DIR`` also captures the installed build at the keyboard geometry
for the audit (evidence only, no assertions).

It proves nothing about a real relay, a real phone keyboard or network.

Usage:
    python e2e/terminal_workspace.py --dist DIR [--shots DIR] [--before DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

BRAVE = "/opt/brave.com/brave/brave"
ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
ORIGIN = "http://relay.workspace-fixture.invalid"


def _session(sid: str, name: str, kind: str, state: str, *, live: bool = True,
             pending: dict | None = None, env: str = "work2") -> dict:
    return {
        "id": sid, "env": env, "env_label": "Work 2" if env == "work2" else "Work 1", "name": name,
        "tmux_name": name, "kind": kind, "origin": "relay", "status": "live" if live else "exited",
        "state": state, "state_source": "hook", "state_at": 100.0,
        "project_dir": "/home/node/projects/demo", "project": "demo",
        "last_message": None, "activity_at": 100.0, "foreground": None, "pending": pending,
        "agent_session_id": None, "created_at": 90.0, "ended_at": None if live else 95.0,
        "end_reason": None,
        "tmux": {"session_id": "$1", "created": 90.0, "pane_id": "%1", "pane_count": 1,
                 "socket": "/tmp/fixture", "server_pid": 9} if live else None,
        "capabilities": {"terminal": live, "prompt": live, "approve": pending is not None,
                         "interrupt": live, "conversation": True, "resume": not live, "end": live},
        "notes": [],
    }


PENDING = {"id": "req-1", "source": "hook", "tool": "Bash", "summary": "npm test",
           "detail": "Run the test suite", "match": [], "created_at": 99.0, "answerable": True}
SESSIONS = [
    _session("s-alpha", "alpha", "claude", "needs_input", pending=PENDING),
    _session("s-beta", "beta", "codex", "working"),
    _session("s-shell", "shell1", "shell", "idle"),
    _session("s-old", "oldjob", "claude", "exited", live=False),
]
STATE = {
    "generated_at": 100.0, "version": "fixture", "host": {"name": "fixture"}, "tailscale_hostname": None,
    "envs": [
        {"id": "work1", "label": "Work 1", "container": "c1", "available": True, "status": "running",
         "network": None, "projects_root": "/home/node/projects", "error": None, "snapshot_at": 100.0,
         "agents": {"claude": True, "codex": True}, "provisioned": True},
        {"id": "work2", "label": "Work 2", "container": "c2", "available": True, "status": "running",
         "network": None, "projects_root": "/home/node/projects", "error": None, "snapshot_at": 100.0,
         "agents": {"claude": True, "codex": True}, "provisioned": True},
    ],
    "sessions": SESSIONS,
    "counts": {"sessions": 3, "working": 1, "needs_input": 1, "finished_unread": 0,
               "by_env": {"work1": 0, "work2": 3}},
    "activity_unread": 1,
}
ACTIVITY = {"items": [{"id": 1, "kind": "needs_input", "title": "beta needs input", "body": None,
                       "session_id": "s-beta", "env": "work2", "attention": True, "read": False,
                       "created_at": 99.0}], "unread": 1}
CONVERSATION = {"supported": True, "reason": None, "items": [
    {"id": "1", "role": "user", "text": "Old saved request.", "ts": 90.0, "tool": None},
    {"id": "2", "role": "assistant", "text": "Old saved answer.", "ts": 91.0, "tool": None},
]}


class Fixture:
    """Fake API and sockets. Records input frames and API posts per session."""

    def __init__(self, dist: Path, extra: int = 0) -> None:
        self.dist = dist
        # Extra idle shells make a long session list (its own scroll region).
        self.state = dict(STATE, sessions=SESSIONS + [
            _session(f"s-x{i}", f"extra{i}", "shell", "idle") for i in range(extra)])
        self.inputs: dict[str, list[str]] = {}
        self.scrolls: dict[str, list[dict]] = {}
        self.posts: list[tuple[str, Any]] = []
        self.sockets: dict[str, list[Any]] = {}
        self.connects: dict[str, int] = {}
        self.mute_pings = False  # a socket that looks open but never answers (a frozen background tab)
        # The real bridge sends {"type":"ready"} once its tmux client runs. hold_ready
        # keeps new sockets open but not ready until release_ready().
        self.hold_ready = False
        self.held: dict[str, Any] = {}
        self.pings: dict[str, list[dict]] = {}

    def install(self, page: Any, screen: tuple[int, int] | None = None) -> None:
        if screen:
            pin_screen(page, *screen)
        page.route(f"{ORIGIN}/**", self._http)
        page.route_web_socket(re.compile(r".*/api/stream.*"), lambda ws: None)  # open, silent
        page.route_web_socket(re.compile(r".*/terminal.*"), self._terminal)

    def _http(self, route: Any) -> None:
        req = route.request
        url = req.url
        path = url[len(ORIGIN):].split("?", 1)[0] or "/"
        if "/api/" not in url:
            local = self.dist / (path.lstrip("/") or "index.html")
            route.fulfill(path=str(local if local.is_file() else self.dist / "index.html"))
            return
        if req.method != "GET":
            self.posts.append((path, req.post_data))
        body: Any = {}
        if path == "/api/auth/me":
            body = {"authenticated": True}
        elif path == "/api/state":
            body = self.state
        elif path.endswith("/conversation"):
            body = CONVERSATION
        elif path.startswith("/api/activity"):
            body = ACTIVITY
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def _terminal(self, ws: Any) -> None:
        sid = re.search(r"/api/sessions/([^/]+)/terminal", ws.url).group(1)
        self.sockets.setdefault(sid, []).append(ws)
        self.connects[sid] = self.connects.get(sid, 0) + 1
        agent = sid != "s-shell"
        started = [False]

        def start() -> None:
            ws.send(json.dumps({"type": "ready"}))
            # Agent TUIs ask for bracketed paste; the fake shell does not.
            ws.send((b"\x1b[?2004h" if agent else b"") + f"fixture {sid}\r\n$ ".encode())

        def on_message(message: Any) -> None:
            if not isinstance(message, str):
                return
            frame = json.loads(message)
            kind = frame.get("type")
            if kind == "resize" and not started[0]:
                started[0] = True
                if self.hold_ready:
                    self.held[sid] = start
                else:
                    start()
            elif kind == "ping":
                self.pings.setdefault(sid, []).append(frame)
                if not self.mute_pings:
                    # The bridge contract: echo a valid nonce; a plain ping gets a plain pong.
                    pong = {"type": "pong"}
                    if isinstance(frame.get("nonce"), str):
                        pong["nonce"] = frame["nonce"]
                    ws.send(json.dumps(pong))
            elif kind == "input":
                self.inputs.setdefault(sid, []).append(frame.get("data"))
            elif kind == "scroll":
                self.scrolls.setdefault(sid, []).append(frame)
                epoch = frame.get("epoch")
                if frame.get("to") == "bottom":
                    ws.send(json.dumps({"type": "scroll", "active": False}))
                elif agent:
                    ws.send(json.dumps({"type": "history", "epoch": epoch, "status": "loading"}))
                    ws.send(json.dumps({"type": "history", "epoch": epoch, "status": "page", "first": True,
                                        "items": [{"role": "user", "text": "Earlier request."},
                                                  {"role": "assistant", "text": "Earlier answer."}],
                                        "before": 0, "more": False, "note": None}))
                else:
                    ws.send(json.dumps({"type": "scroll", "active": True, "position": 3, "history": 40}))

        ws.on_message(on_message)

    def frames(self, sid: str) -> list[str]:
        return list(self.inputs.get(sid, []))

    def release_ready(self, sid: str) -> None:
        self.held.pop(sid)()

    def send_raw(self, sid: str, frame: dict | bytes) -> None:
        """Send one frame on the newest socket of a session (stale replies, output)."""
        self.sockets[sid][-1].send(frame if isinstance(frame, bytes) else json.dumps(frame))


class StepError(AssertionError):
    pass


class Reporter:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

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


def expect(cond: bool, msg: str) -> None:
    if not cond:
        raise StepError(msg)


def no_overflow(page: Any) -> None:
    w = page.evaluate("() => [document.documentElement.scrollWidth, innerWidth]")
    expect(w[0] <= w[1], f"horizontal overflow {w}")


def active_tag(page: Any) -> str:
    return page.evaluate("() => { const e = document.activeElement; return e ? e.tagName + '.' + e.className : '' }")


def pin_screen(page: Any, width: int, height: int) -> None:
    """Stand in for the physical screen size.

    Brave headless reports its default 1280 x 800 screen in a route-fulfilled
    fixture document, whatever the context's screen option says. A real device
    reports its physical size, which the on-screen keyboard does not change.
    """
    page.add_init_script(
        f"Object.defineProperty(Screen.prototype, 'width', {{get: () => {width}}});"
        f"Object.defineProperty(Screen.prototype, 'height', {{get: () => {height}}});")


def wait_open(page: Any, pane: str = ".session-pane") -> None:
    page.wait_for_selector(f"{pane} .terminal-host .xterm", timeout=8000)
    page.wait_for_function(
        "(sel) => { const b = document.querySelector(sel + ' .key-bar button, ' + sel + ' .terminal-host');"
        " return !!b && !document.querySelector(sel + ' .key-bar button:disabled') }", arg=pane, timeout=8000)


def shot(page: Any, shots: Path | None, name: str) -> None:
    if shots:
        page.screenshot(path=str(shots / f"{name}.png"))


def phone_checks(r: Reporter, browser: Any, dist: Path, shots: Path | None) -> None:
    fx = Fixture(dist)
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, screen={"width": 390, "height": 844},
                              is_mobile=True, has_touch=True, device_scale_factor=2)
    page = ctx.new_page()
    fx.install(page, screen=(390, 844))
    page.goto(f"{ORIGIN}/")
    page.wait_for_selector(".agent-card", timeout=10000)
    shot(page, shots, "after-phone-overview")

    def open_alpha() -> None:
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="alpha").first.tap()
        wait_open(page)
        expect(page.locator("text=Latest turn").count() == 0, "Latest turn tab still shows")
        expect(page.locator(".chat-tabs").count() == 0, "view tabs still show")
        expect("TEXTAREA" not in active_tag(page), f"selection focused a text field: {active_tag(page)}")
        no_overflow(page)
    r.run("phone: overview opens the terminal directly, no keyboard", open_alpha)

    def pending() -> None:
        bar = page.locator(".pending-bar")
        expect(bar.is_visible(), "pending request not visible")
        expect("npm test" in bar.inner_text(), "pending summary missing")
        bar.locator("button", has_text="Allow once").tap()
        page.wait_for_timeout(200)
        expect(any(p == "/api/sessions/s-alpha/approval" and "allow" in (d or "") for p, d in fx.posts),
               f"approval not posted: {fx.posts}")
    r.run("phone: pending request beside the terminal; Allow once posts the approval", pending)

    def strip() -> None:
        chips = page.locator(".session-switch .switch-chip:not(.switch-new)")
        expect(chips.count() == 3, f"switch strip shows {chips.count()} sessions, expected 3 live")
        expect(page.locator(".switch-chip[aria-current='true']").inner_text().strip().startswith("alpha"),
               "current chip is not alpha")
        expect(page.locator(".switch-chip", has_text="alpha").locator(".switch-flag").count() == 1,
               "pending flag missing on the chip")
    r.run("phone: compact switch strip lists live sessions with status", strip)
    shot(page, shots, "after-phone-terminal")

    field = page.locator(".term-compose-field")

    def compose_local() -> None:
        before = len(fx.frames("s-alpha"))
        field.tap()
        page.keyboard.type("line one")
        page.keyboard.press("Backspace")
        page.keyboard.type("e")
        page.keyboard.press("Enter")  # a new line in the field, not a send
        page.keyboard.insert_text("lïne twö")
        # An IME still composing: Ctrl+Enter must not send.
        field.evaluate("""el => {
          el.dispatchEvent(new CompositionEvent('compositionstart', {data: ''}));
          el.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', ctrlKey: true, isComposing: true, bubbles: true}));
          el.dispatchEvent(new CompositionEvent('compositionend', {data: ''}));
        }""")
        page.wait_for_timeout(400)
        expect(field.input_value() == "line one\nlïne twö", f"field value {field.input_value()!r}")
        expect(len(fx.frames("s-alpha")) == before, f"local edits leaked: {fx.frames('s-alpha')[before:]}")
    r.run("phone: text field keeps local edits, Enter adds a line, composition never sends", compose_local)

    def insert() -> None:
        before = len(fx.frames("s-alpha"))
        page.locator(".term-compose-insert").tap()
        page.wait_for_timeout(500)
        new = fx.frames("s-alpha")[before:]
        expect(new == ["\x1b[200~line one\rlïne twö\x1b[201~"], f"Insert frames {new!r}")
        expect(field.input_value() == "", "draft not cleared after Insert")
    r.run("phone: Insert sends one bracketed paste and no Enter", insert)

    def send_once() -> None:
        field.fill("run checks")
        before = len(fx.frames("s-alpha"))
        send = page.locator(".term-compose-send")
        send.tap()
        send.tap(force=True)  # a double tap
        page.wait_for_timeout(700)
        new = fx.frames("s-alpha")[before:]
        expect(new == ["\x1b[200~run checks\x1b[201~", "\r"], f"Send frames {new!r}")
    r.run("phone: Send pastes once, then one Enter after a pause; double tap sends nothing more", send_once)

    def keys() -> None:
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        before = len(fx.frames("s-alpha"))
        for label in ["Esc", "Tab", "Ctrl+C", "Enter"]:
            page.locator(".key-bar button", has_text=label).tap()
        page.locator(".key-bar button[aria-label='Arrow up']").tap()
        page.locator(".key-bar button[aria-label='Arrow down']").tap()
        page.wait_for_timeout(200)
        new = fx.frames("s-alpha")[before:]
        expect(new == ["\x1b", "\t", "\x03", "\r", "\x1b[A", "\x1b[B"], f"key frames {new!r}")
        tag = active_tag(page)
        expect("TEXTAREA" not in tag, f"a key tap focused a text field (keyboard): {tag}")
    r.run("phone: key-bar keys send their bytes and do not focus a text field", keys)

    def history_modes() -> None:
        before = len(fx.frames("s-alpha"))
        page.locator(".key-bar button", has_text="History").tap()
        page.wait_for_selector(".term-history-label", timeout=3000)
        pill = page.locator(".term-live-pill")
        expect("Saved transcript" in pill.inner_text(), f"pill {pill.inner_text()!r}")
        shot(page, shots, "after-phone-saved-transcript")
        pill.tap()
        page.wait_for_timeout(300)
        expect(page.locator(".term-history").count() == 0, "saved transcript still shows after Back to live")
        expect(fx.scrolls["s-alpha"][-1].get("to") == "bottom", "Back to live did not ask for the bottom")
        expect(len(fx.frames("s-alpha")) == before, "History sent terminal input")
        expect("TEXTAREA" not in active_tag(page), "History focused a text field")
    r.run("phone: History shows a labeled saved transcript; Back to live returns", history_modes)

    def drafts() -> None:
        field.fill("draft for alpha")
        page.locator(".switch-chip", has_text="beta").tap()
        wait_open(page)
        expect(page.locator(".chat-title h2").inner_text().startswith("beta"), "switch did not open beta")
        expect(field.input_value() == "", f"alpha draft leaked into beta: {field.input_value()!r}")
        field.fill("draft for beta")
        page.locator(".switch-chip", has_text="alpha").tap()
        wait_open(page)
        expect(field.input_value() == "draft for alpha", f"alpha draft lost: {field.input_value()!r}")
        page.reload()
        wait_open(page)
        expect(field.input_value() == "draft for alpha", "alpha draft lost on reload")
        page.locator(".switch-chip", has_text="beta").tap()
        wait_open(page)
        expect(field.input_value() == "draft for beta", "beta draft lost")
        expect(all("draft for" not in f for f in fx.frames("s-beta") + fx.frames("s-alpha")),
               "a draft was sent without Send")
    r.run("phone: drafts stay per session across switch and reload, never sent", drafts)

    def disconnect() -> None:
        # beta is open with its draft. Drop its socket.
        before = len(fx.frames("s-beta"))
        connects = fx.connects["s-beta"]
        fx.sockets["s-beta"][-1].close(code=1011, reason="fixture drop")
        page.wait_for_timeout(150)
        expect(page.locator(".key-bar button:disabled").count() == 7, "keys not disabled while disconnected")
        expect(page.locator(".term-compose-send").is_disabled(), "Send not disabled while disconnected")
        field.fill("draft for beta, edited offline")
        page.wait_for_function(f"() => true", timeout=100)
        page.wait_for_timeout(1300)
        shot(page, shots, "after-phone-reconnecting")
        page.wait_for_function("() => !document.querySelector('.key-bar button:disabled')", timeout=8000)
        expect(fx.connects["s-beta"] == connects + 1, "no reconnect")
        page.wait_for_timeout(400)
        expect(len(fx.frames("s-beta")) == before, f"input replayed after reconnect: {fx.frames('s-beta')[before:]}")
        expect(field.input_value() == "draft for beta, edited offline", "offline draft lost")
    r.run("phone: disconnect disables send controls, keeps the draft, no replay after reconnect", disconnect)

    def activity_entry() -> None:
        page.locator(".mobile-back").tap()
        page.wait_for_selector(".agent-card")
        page.locator(".mobile-nav button", has_text="Activity").tap()
        page.locator(".notice", has_text="beta needs input").tap()
        page.wait_for_selector(".activity-session:not([hidden]) .terminal-host .xterm", timeout=8000)
        expect(page.locator("text=Latest turn").count() == 0, "Latest turn shows from Activity")
        expect(page.locator(".activity-session:not([hidden]) .term-compose-field").input_value()
               == "draft for beta, edited offline", "draft not kept on Activity entry")
        shot(page, shots, "after-phone-from-activity")
        page.locator(".activity-session:not([hidden]) .mobile-back").tap()
        page.wait_for_selector(".view-activity:not([hidden])", timeout=3000)
    r.run("phone: Activity opens the terminal directly, Back returns to Activity", activity_entry)

    def menu() -> None:
        page.locator(".mobile-nav button", has_text="Agents").tap()
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="alpha").first.tap()
        wait_open(page)
        page.locator(".pane-menu-button").tap()
        items = page.locator(".pane-menu [role=menuitem]")
        expect(items.count() == 2, "menu should hold Interrupt and End")
        shot(page, shots, "after-phone-actions-menu")
        page.keyboard.press("Escape")
        expect(page.locator(".pane-menu").count() == 0, "menu did not close on Escape")
    r.run("phone: Interrupt and End sit in the actions menu", menu)
    ctx.close()


def keyboard_geometry(r: Reporter, browser: Any, dist: Path, shots: Path | None, label: str,
                      assert_fix: bool) -> None:
    """A 384 x 854 phone whose keyboard leaves a 384 x 360 viewport (shorter than wide)."""
    fx = Fixture(dist)
    ctx = browser.new_context(viewport={"width": 384, "height": 854}, screen={"width": 384, "height": 854},
                              is_mobile=True, has_touch=True, device_scale_factor=2)
    page = ctx.new_page()
    fx.install(page, screen=(384, 854))
    page.goto(f"{ORIGIN}/")
    page.locator(".agent-card:visible, .sidebar-session:visible", has_text="beta").first.tap()
    page.wait_for_selector(".chat-head", timeout=10000)
    if page.locator(".chat-tabs button", has_text="Terminal").count():
        page.locator(".chat-tabs button", has_text="Terminal").tap()  # installed build
    page.wait_for_selector(".terminal-host .xterm", timeout=8000)
    page.wait_for_timeout(500)
    target = ".term-compose-field" if page.locator(".term-compose-field").count() else ".xterm-helper-textarea"
    page.focus(target)
    # A keyboard shrinks the layout viewport but does not rotate the screen.
    # Playwright's set_viewport_size also flips screen.orientation, so set the
    # metrics directly with the screen kept portrait.
    cdp = ctx.new_cdp_session(page)

    def metrics(height: int) -> None:
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": 384, "height": height, "deviceScaleFactor": 2, "mobile": True,
            "screenWidth": 384, "screenHeight": 854,
            "screenOrientation": {"type": "portraitPrimary", "angle": 0}})

    metrics(360)
    page.wait_for_function("() => innerHeight === 360", timeout=5000)
    page.wait_for_timeout(400)
    state = page.evaluate("""() => {
      const vis = (s) => { const e = document.querySelector(s); if (!e) return null;
        const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
        return cs.display !== 'none' && cs.visibility !== 'hidden' && r.height > 0 && r.bottom <= innerHeight + 1 && r.top >= -1; };
      const host = document.querySelector('.terminal-host');
      return {kb: document.body.classList.contains('kb-open'), back: vis('.mobile-back'), name: vis('.chat-name h2'),
        keys: vis('.key-bar'), field: vis('.term-compose-field'), topbar: vis('.topbar'), nav: vis('.mobile-nav'),
        hostH: host ? host.getBoundingClientRect().height : 0, overflowX: document.documentElement.scrollWidth > innerWidth,
        orientation: screen.orientation && screen.orientation.type, inner: [innerWidth, innerHeight],
        vv: window.visualViewport && visualViewport.height};
    }""")
    print(f"INFO  {label} keyboard geometry: {state}")
    if shots:
        # page.screenshot() would restore Playwright's own viewport; capture through CDP.
        import base64
        data = cdp.send("Page.captureScreenshot", {"format": "png"})["data"]
        (shots / f"{label}-keyboard-384x360.png").write_bytes(base64.b64decode(data))
    if not assert_fix:
        ctx.close()
        return

    def check() -> None:
        expect(state["kb"], f"keyboard shrink not seen as keyboard: {state}")
        expect(state["back"] and state["name"], f"back or session name not reachable: {state}")
        expect(state["keys"] and state["field"], f"keys or text field not reachable: {state}")
        expect(not state["topbar"] and not state["nav"], f"chrome not hidden: {state}")
        expect(state["hostH"] >= 80, f"terminal too short: {state['hostH']}")
        expect(not state["overflowX"], "horizontal overflow")
    r.run("keyboard 384x360: seen as keyboard, not rotation; back, name, keys, field reachable", check)

    def close_kb() -> None:
        page.evaluate("() => document.activeElement.blur()")
        metrics(854)
        page.wait_for_timeout(300)
        expect(not page.evaluate("() => document.body.classList.contains('kb-open')"), "kb-open stuck")
        # Approved redesign keeps an open terminal full-screen. Its workspace
        # strip replaces the global bottom nav and must return after typing.
        expect(page.locator(".session-switch").is_visible(), "workspace switcher did not return")
        expect(page.locator(".mobile-back").is_visible(), "Back to agents is not reachable")
    r.run("keyboard closes: layout returns", close_kb)
    ctx.close()


def landscape_checks(r: Reporter, browser: Any, dist: Path, shots: Path | None) -> None:
    fx = Fixture(dist)
    ctx = browser.new_context(viewport={"width": 844, "height": 390}, screen={"width": 844, "height": 390},
                              is_mobile=True, has_touch=True, device_scale_factor=2)
    page = ctx.new_page()
    fx.install(page, screen=(844, 390))
    page.goto(f"{ORIGIN}/#/s/s-beta")
    wait_open(page)

    def check() -> None:
        box = page.evaluate("""() => { const r = (s) => document.querySelector(s).getBoundingClientRect();
          return {host: r('.terminal-host').height, input: r('.term-input').height, conv: r('.session-pane').height,
                  strip: getComputedStyle(document.querySelector('.session-switch')).display,
                  body: document.body.className, screen: [screen.width, screen.height]}; }""")
        print(f"INFO  landscape boxes: {box}")
        expect(box["strip"] != "none", "switch strip hidden in landscape")
        expect(box["host"] >= 140, f"terminal too short in landscape: {box}")
        expect(box["input"] <= 64, f"keys and text field use more than one row: {box}")
        expect(page.locator(".dashboard.split").count() == 0, "two panes in phone landscape")
        no_overflow(page)
        page.locator(".switch-chip", has_text="shell1").tap()
        wait_open(page)
        expect(page.locator(".chat-title h2").inner_text().startswith("shell1"), "landscape switch failed")
    r.run("phone landscape 844x390: one full-screen pane, one input row, switch strip works", check)
    shot(page, shots, "after-phone-landscape")
    ctx.close()


def tablet_checks(r: Reporter, browser: Any, dist: Path, shots: Path | None) -> None:
    fx = Fixture(dist)
    ctx = browser.new_context(viewport={"width": 800, "height": 1280}, screen={"width": 800, "height": 1280},
                              is_mobile=True, has_touch=True)
    page = ctx.new_page()
    fx.install(page, screen=(800, 1280))
    page.goto(f"{ORIGIN}/")
    page.wait_for_selector(".agent-card", timeout=10000)

    def portrait() -> None:
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="beta").first.tap()
        wait_open(page)
        expect(page.locator(".agent-list").is_visible(), "overview hidden beside the terminal")
        expect(page.locator(".pane-split").count() == 0, "split offered at 800px")
        expect(page.locator(".term-compose-field").is_visible(), "touch text field missing on tablet")
        no_overflow(page)
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="shell1").first.tap()
        wait_open(page)
        expect(page.locator(".chat-title h2").inner_text().startswith("shell1"), "list pick did not switch")
    r.run("tablet portrait 800x1280: overview beside one terminal; list pick switches", portrait)
    shot(page, shots, "after-tablet-portrait")

    def landscape_split() -> None:
        page.set_viewport_size({"width": 1280, "height": 800})
        page.wait_for_timeout(300)
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="alpha").first.tap()
        wait_open(page)
        page.locator(".pane-split").tap()
        expect(page.locator(".dashboard.split").count() == 1, "split did not open")
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="shell1").first.tap()
        wait_open(page, ".session-pane ~ .session-pane")
        panes = page.locator(".session-pane")
        expect(panes.count() == 2, f"{panes.count()} panes")
        expect(panes.nth(0).locator(".chat-title h2").inner_text().startswith("alpha"), "pane 1 changed")
        expect(panes.nth(1).locator(".chat-title h2").inner_text().startswith("shell1"), "pane 2 not shell1")
        # Picking pane 1's session while pane 2 is active activates pane 1, no duplicate.
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="alpha").first.tap()
        expect("active-pane" in (panes.nth(0).get_attribute("class") or ""), "pane 1 not active")
        expect(page.locator(".session-pane").count() == 2, "duplicate pane")
        f1 = panes.nth(0).locator(".term-compose-field")
        f2 = panes.nth(1).locator(".term-compose-field")
        f1.fill("left text")
        f2.fill("right text")
        expect(f1.input_value() == "left text" and f2.input_value() == "right text", "pane drafts mixed")
        a0, s0 = len(fx.frames("s-alpha")), len(fx.frames("s-shell"))
        panes.nth(1).locator(".key-bar button", has_text="Esc").tap()
        panes.nth(1).locator(".term-compose-send").tap()
        page.wait_for_timeout(500)
        expect(fx.frames("s-alpha")[a0:] == [], f"pane 2 input reached pane 1: {fx.frames('s-alpha')[a0:]}")
        expect(fx.frames("s-shell")[s0:] == ["\x1b", "right text\r"], f"pane 2 frames {fx.frames('s-shell')[s0:]!r}")
        expect(f1.input_value() == "left text", "pane 1 draft lost")
        expect(page.locator(".agent-card:visible .pane-tag, .sidebar-session:visible .pane-tag").count() == 2, "pane tags missing in the list")
        no_overflow(page)
    r.run("tablet landscape 1280x800: optional second pane; input stays in its pane", landscape_split)
    shot(page, shots, "after-tablet-landscape-split")
    ctx.close()


def same_session_guard_checks(r: Reporter, browser: Any, dist: Path) -> None:
    """Split on, pane 2 = beta; then beta comes to pane 1 by browser Back and by the
    bell Activity popup (paths that skip the list guard). beta must mount once."""
    fx = Fixture(dist)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    fx.install(page, screen=(1280, 800))
    # Every WebSocket the page creates, to count the terminal sockets it still holds open.
    page.add_init_script("""(() => { const Native = window.WebSocket; window.__sockets = [];
      window.WebSocket = class extends Native { constructor(u, p) { super(u, p); window.__sockets.push(this); } }; })()""")
    open_beta = ("() => window.__sockets.filter((s) => s.url.includes('/s-beta/terminal')"
                 " && s.readyState <= WebSocket.OPEN).length")
    page.goto(f"{ORIGIN}/")
    page.wait_for_selector(".agent-card", timeout=10000)
    beta_panes = ".session-pane:has(.chat-title h2:text-matches('^beta'))"

    def pane2_beta() -> None:
        page.locator(".pane-split").tap()
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="beta").first.tap()
        wait_open(page, ".session-pane ~ .session-pane")
        expect(page.locator(".session-pane").nth(1).locator(".chat-title h2").inner_text().startswith("beta"),
               "pane 2 is not beta")

    def one_beta(label: str, before: int) -> None:
        wait_open(page)
        page.wait_for_timeout(400)
        expect(page.locator(beta_panes).count() == 1, f"{label}: beta shows in {page.locator(beta_panes).count()} panes")
        expect(page.locator(".terminal-host").count() == 1, f"{label}: {page.locator('.terminal-host').count()} terminals")
        expect(page.locator(".pane-empty").count() == 1, f"{label}: pane 2 is not empty")
        expect(page.evaluate(open_beta) == 1, f"{label}: {page.evaluate(open_beta)} open beta sockets")
        field = page.locator(beta_panes).locator(".term-compose-field")
        expect(field.input_value() == "beta draft", f"{label}: draft {field.input_value()!r}")
        expect(fx.frames("s-beta")[before:] == [], f"{label}: input without a tap")
        page.locator(beta_panes).locator(".term-compose-send").tap()
        page.wait_for_timeout(600)
        expect(fx.frames("s-beta")[before:] == ["\x1b[200~beta draft\x1b[201~", "\r"],
               f"{label}: frames {fx.frames('s-beta')[before:]!r}")
        expect(all(f.input_value() != "beta draft" for f in page.locator(".term-compose-field").all()),
               f"{label}: a second copy of the draft is left to send again")

    def via_back() -> None:
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="beta").first.tap()
        wait_open(page)
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="alpha").first.tap()
        wait_open(page)
        pane2_beta()
        page.locator(".session-pane").nth(1).locator(".term-compose-field").fill("beta draft")
        before = len(fx.frames("s-beta"))
        page.go_back()  # hash -> #/s/s-beta: pane 1 becomes beta
        page.wait_for_function("() => location.hash === '#/s/s-beta'", timeout=3000)
        one_beta("browser Back", before)
    r.run("split, pane 2 = beta, Back selects beta: one terminal, one socket, pane 2 empty, one send", via_back)

    def via_activity_popup() -> None:
        page.locator(".pane-empty").tap(position={"x": 20, "y": 20})  # pane 2 active (not its Close button)
        expect(page.locator(".dashboard.split").count() == 1, "split closed")
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="shell1").first.tap()  # pane 2 = shell1
        wait_open(page, ".session-pane ~ .session-pane")
        page.locator(".session-pane").first.locator(".chat-head").tap()  # pane 1 active
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="alpha").first.tap()  # pane 1 = alpha
        wait_open(page)
        page.locator(".session-pane").nth(1).locator(".chat-head").tap()  # pane 2 active
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="beta").first.tap()  # pane 2 = beta
        wait_open(page, ".session-pane ~ .session-pane")
        page.locator(".session-pane").nth(1).locator(".term-compose-field").fill("beta draft")
        before = len(fx.frames("s-beta"))
        page.locator(".bell").tap()
        page.locator(".notification-panel .notice", has_text="beta needs input").tap()
        one_beta("bell Activity", before)
    r.run("split, pane 2 = beta, bell Activity selects beta: one terminal, one socket, pane 2 empty, one send",
          via_activity_popup)
    ctx.close()


def desktop_checks(r: Reporter, browser: Any, dist: Path, shots: Path | None) -> None:
    fx = Fixture(dist)
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    fx.install(page)
    page.goto(f"{ORIGIN}/")
    page.wait_for_selector(".agent-card", timeout=10000)

    def direct() -> None:
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="shell1").first.click()
        page.wait_for_selector(".terminal-host .xterm", timeout=8000)
        page.wait_for_timeout(400)
        expect(page.locator(".key-bar").count() == 0, "touch key bar on a mouse desktop")
        page.locator(".terminal-host").click()
        before = len(fx.frames("s-shell"))
        page.keyboard.type("pwd")
        page.keyboard.press("Enter")
        page.wait_for_timeout(300)
        expect("".join(fx.frames("s-shell")[before:]) == "pwd\r", f"direct typing frames {fx.frames('s-shell')[before:]!r}")
        page.mouse.move(700, 400)
        page.mouse.wheel(0, -400)
        page.wait_for_selector(".term-live-pill", timeout=3000)
        expect("Terminal scrollback" in page.locator(".term-live-pill").inner_text(), "scrollback mode not labeled")
        page.locator(".term-live-pill").click()
        page.wait_for_timeout(300)
        expect(page.locator(".term-live-pill").count() == 0, "Back to live did not clear the scrollback label")
    r.run("desktop 1440: direct terminal typing; wheel scrollback labeled; Back to live", direct)

    def split_and_end() -> None:
        page.locator(".pane-split").click()
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="beta").first.click()
        page.wait_for_selector(".session-pane >> nth=1", timeout=5000)
        shot(page, shots, "after-desktop-split")
        page.locator(".session-pane >> nth=1").locator(".pane-menu-button").click()
        page.locator(".pane-menu [role=menuitem]", has_text="End session").click()
        page.wait_for_selector(".modal", timeout=3000)
        expect(not any("/end" in p for p, _ in fx.posts), "End posted without confirmation")
        page.keyboard.press("Escape")
        page.locator(".session-pane >> nth=1").locator(".pane-close").click()
        expect(page.locator(".session-pane").count() == 1, "pane did not close")
        page.get_by_role("button", name="Agents", exact=True).first.click()
        page.locator(".ended-toggle").click()
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="oldjob").first.click()
        page.wait_for_selector(".saved-transcript .term-history-item", timeout=5000)
        expect("not a live screen" in page.locator(".saved-transcript").inner_text(), "ended view not labeled")
        no_overflow(page)
    r.run("desktop: split, End stays behind its dialog, close pane, ended session shows a labeled transcript", split_and_end)
    shot(page, shots, "after-desktop-ended")
    ctx.close()


PANE_STATE_JS = """() => {
  const panes = [...document.querySelectorAll('.session-pane')];
  const el = document.activeElement;
  const r = (e) => e ? e.getBoundingClientRect() : null;
  return {
    inner: [innerWidth, innerHeight], kb: document.body.classList.contains('kb-open'),
    phone: document.body.classList.contains('phone-screen'),
    panes: panes.map((p) => {
      const f = p.querySelector('.term-compose-field'), send = p.querySelector('.term-compose-send');
      return {name: p.querySelector('.chat-title h2').textContent.trim(),
              value: f ? f.value : null, mark: f ? f.dataset.mark || null : null,
              fieldBottom: f ? r(f).bottom : null, sendBottom: send ? r(send).bottom : null,
              hostH: r(p.querySelector('.terminal-host')).height,
              fixed: getComputedStyle(p).position === 'fixed'};
    }),
    focused: el && el.classList.contains('term-compose-field') ? el.getAttribute('aria-label') : (el ? el.tagName : null),
    listVisible: [...document.querySelectorAll('.agent-list, .sidebar-sessions')].some(e => e.getBoundingClientRect().height > 0),
    topbar: getComputedStyle(document.querySelector('.topbar')).display,
    docH: document.documentElement.scrollHeight,
  };
}"""


def tablet_keyboard_checks(r: Reporter, browser: Any, dist: Path, shots: Path | None) -> None:
    """1280 x 800 touch tablet: the keyboard leaves 1280 x 400 with the screen still landscape."""
    fx = Fixture(dist)
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, screen={"width": 1280, "height": 800},
                              is_mobile=True, has_touch=True)
    page = ctx.new_page()
    fx.install(page, screen=(1280, 800))
    page.goto(f"{ORIGIN}/")
    page.wait_for_selector(".agent-card", timeout=10000)
    page.locator(".agent-card:visible, .sidebar-session:visible", has_text="alpha").first.tap()
    wait_open(page)
    page.locator(".pane-split").tap()
    page.locator(".agent-card:visible, .sidebar-session:visible", has_text="shell1").first.tap()
    wait_open(page, ".session-pane ~ .session-pane")
    pane2 = page.locator(".session-pane").nth(1)
    field2 = pane2.locator(".term-compose-field")
    field2.fill("draft in pane two")
    field2.evaluate("el => { el.dataset.mark = 'same-node'; el.focus(); }")
    connects = dict(fx.connects)
    cdp = ctx.new_cdp_session(page)

    def metrics(height: int) -> None:
        cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": 1280, "height": height, "deviceScaleFactor": 1, "mobile": True,
            "screenWidth": 1280, "screenHeight": 800,
            "screenOrientation": {"type": "landscapePrimary", "angle": 0}})

    metrics(400)
    page.wait_for_function("() => innerHeight === 400", timeout=5000)
    page.wait_for_timeout(500)
    open_state = page.evaluate(PANE_STATE_JS)
    print(f"INFO  tablet keyboard open: {open_state}")
    if shots:
        import base64
        data = cdp.send("Page.captureScreenshot", {"format": "png"})["data"]
        (shots / "after-tablet-keyboard-1280x400.png").write_bytes(base64.b64decode(data))

    def keyboard_open() -> None:
        s = open_state
        expect(not s["phone"], "tablet read as a phone")
        expect(s["kb"], "keyboard not detected")
        expect([p["name"].split(" /")[0] for p in s["panes"]] == ["alpha", "shell1"], f"panes {s['panes']}")
        expect(s["panes"][1]["value"] == "draft in pane two" and s["panes"][1]["mark"] == "same-node",
               "pane 2 field was replaced or lost its draft")
        expect(s["focused"] == "Text for shell1", f"focus moved to {s['focused']}")
        expect(not any(p["fixed"] for p in s["panes"]), "phone landscape layout applied to a tablet")
        expect(all(p["sendBottom"] is not None and p["sendBottom"] <= 400 + 1 for p in s["panes"]),
               f"pane controls below the keyboard: {s['panes']}")
        expect(all(p["hostH"] >= 80 for p in s["panes"]), f"terminal too short: {s['panes']}")
        expect(s["listVisible"], "session overview hidden")
        expect(fx.connects == connects, f"a terminal reconnected: {connects} -> {fx.connects}")
    r.run("tablet keyboard 1280x400: both panes stay, pane 2 keeps session, draft, node and focus", keyboard_open)

    def keyboard_close() -> None:
        metrics(800)
        page.wait_for_function("() => innerHeight === 800", timeout=5000)
        page.wait_for_timeout(400)
        s = page.evaluate(PANE_STATE_JS)
        expect(not s["kb"], "kb-open stuck after the keyboard closed")
        expect(len(s["panes"]) == 2 and s["panes"][1]["mark"] == "same-node", f"panes after close {s['panes']}")
        expect(s["focused"] == "Text for shell1", f"focus after close: {s['focused']}")
        expect(s["topbar"] != "none", "top bar did not return")
        a0, s0 = len(fx.frames("s-alpha")), len(fx.frames("s-shell"))
        page.keyboard.type(" ok")
        pane2.locator(".term-compose-send").tap()
        page.wait_for_timeout(400)
        expect(fx.frames("s-shell")[s0:] == ["draft in pane two ok\r"], f"pane 2 frames {fx.frames('s-shell')[s0:]!r}")
        expect(fx.frames("s-alpha")[a0:] == [], "pane 2 text reached pane 1")
        expect(fx.connects == connects, "a terminal reconnected")
    r.run("tablet keyboard closes: both panes return; typing and Send stay with pane 2", keyboard_close)
    ctx.close()


def wide_visibility_checks(r: Reporter, browser: Any, dist: Path, shots: Path | None) -> None:
    """Terminal controls inside the first viewport; a long list scrolls in its own region."""
    for width, height, touch, label in [(1440, 1000, True, "touch-1440x1000"), (1440, 900, False, "desktop-1440x900"),
                                        (1280, 800, False, "desktop-1280x800"), (800, 1280, True, "tablet-800x1280")]:
        fx = Fixture(dist, extra=14)
        ctx = browser.new_context(viewport={"width": width, "height": height},
                                  screen={"width": width, "height": height}, has_touch=touch, is_mobile=touch)
        page = ctx.new_page()
        fx.install(page, screen=(width, height))
        page.goto(f"{ORIGIN}/")
        page.wait_for_selector(".agent-card", timeout=10000)

        def check() -> None:
            card = page.locator(".agent-card:visible, .sidebar-session:visible", has_text="alpha").first
            card.tap() if touch else card.click()
            wait_open(page)
            if width >= 1180:
                split = page.locator(".pane-split")
                split.tap() if touch else split.click()
                other = page.locator(".agent-card:visible, .sidebar-session:visible", has_text="shell1").first
                other.tap() if touch else other.click()
                wait_open(page, ".session-pane ~ .session-pane")
            page.wait_for_timeout(300)
            s = page.evaluate("""() => {
              const vis = (e) => { const b = e.getBoundingClientRect(); return b.top >= -1 && b.bottom <= innerHeight + 1 && b.height > 0; };
              const list = [...document.querySelectorAll('.agent-list, .sidebar-sessions')].find(e => e.getBoundingClientRect().height > 0);
              const panes = [...document.querySelectorAll('.session-pane')];
              const ctrls = panes.flatMap((p) => [...p.querySelectorAll('.term-compose-send, .key-bar button, .chat-head, .pending-bar')]);
              return {docH: document.documentElement.scrollHeight, inner: innerHeight, panes: panes.length,
                ctrlsVisible: ctrls.every(vis), ctrlCount: ctrls.length,
                hosts: panes.map((p) => p.querySelector('.terminal-host').getBoundingClientRect().height),
                hostBottom: Math.max(...panes.map((p) => p.querySelector('.terminal-host').getBoundingClientRect().bottom)),
                listScroll: [list.scrollHeight, list.clientHeight, getComputedStyle(list).overflowY],
                intro: getComputedStyle(document.querySelector('.intro')).display,
                counts: getComputedStyle(document.querySelector('.phone-head')).display,
                pendingCard: !!document.querySelector('.agent-card .agent-pending')};
            }""")
            print(f"INFO  {label}: {s}")
            expect(s["docH"] <= s["inner"] + 1, f"document {s['docH']} px taller than the {s['inner']} px viewport")
            expect(s["ctrlsVisible"] and s["ctrlCount"] > 0, "a pane control is outside the viewport")
            expect(s["hostBottom"] <= s["inner"] + 1, "terminal extends below the viewport")
            expect(min(s["hosts"]) >= 240, f"terminal too short: {s['hosts']}")
            expect(s["listScroll"][2] == "auto" and s["listScroll"][0] > s["listScroll"][1],
                   f"the long list does not scroll in its own region: {s['listScroll']}")
            expect(s["intro"] == "none" and s["counts"] != "none", "hero still shown or compact counts missing")
            expect(s["pendingCard"], "pending request missing beside the terminal")
            no_overflow(page)
        r.run(f"{label}: workspace fits the viewport, controls visible, list scrolls alone", check)
        shot(page, shots, f"after-wide-{label}")
        ctx.close()


def wake_checks(r: Reporter, browser: Any, dist: Path) -> None:
    """Readiness: input waits for the bridge 'ready' frame and, after a wake, for the
    answer to that exact probe. Text stays editable; nothing is lost or replayed."""
    fx = Fixture(dist)
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page = ctx.new_page()
    fx.install(page, screen=(390, 844))
    page.goto(f"{ORIGIN}/")
    page.wait_for_selector(".agent-card", timeout=10000)
    field = page.locator(".term-compose-field")
    send = page.locator(".term-compose-send")
    wake = "() => document.dispatchEvent(new Event('visibilitychange'))"
    keys_off = "() => document.querySelectorAll('.key-bar button:disabled').length === 7"
    keys_on = "() => !document.querySelector('.key-bar button:disabled')"

    def blocked(sid: str, before: int, why: str) -> None:
        expect(page.evaluate(keys_off), f"keys enabled {why}")
        expect(send.is_disabled(), f"Send enabled {why}")
        expect(field.is_editable(), f"text field not editable {why}")
        send.tap(force=True)
        page.locator(".terminal-host").tap()
        page.keyboard.type("zz")  # direct terminal typing
        page.wait_for_timeout(200)
        expect(fx.frames(sid)[before:] == [], f"input sent {why}: {fx.frames(sid)[before:]!r}")
        page.evaluate("() => document.activeElement && document.activeElement.blur()")

    def delayed_ready() -> None:
        fx.hold_ready = True
        page.locator(".agent-card:visible, .sidebar-session:visible", has_text="shell1").first.tap()
        page.wait_for_selector(".terminal-host .xterm", timeout=8000)
        page.wait_for_timeout(300)  # socket open, resize sent, no ready frame yet
        expect("s-shell" in fx.held, "the socket did not open")
        field.fill("typed before ready")
        blocked("s-shell", 0, "before the bridge is ready")
        fx.hold_ready = False
        fx.release_ready("s-shell")
        page.wait_for_function(keys_on, timeout=3000)
        page.wait_for_timeout(200)
        expect(fx.frames("s-shell") == [], f"input replayed after ready: {fx.frames('s-shell')!r}")
        expect(field.input_value() == "typed before ready", "draft lost before ready")
    r.run("initial ready delayed: open socket alone keeps input off; text kept; nothing replayed", delayed_ready)

    def answered() -> None:
        before = len(fx.frames("s-shell"))
        page.evaluate(wake)
        page.wait_for_function(keys_on, timeout=3000)
        last = fx.pings["s-shell"][-1]
        expect(isinstance(last.get("nonce"), str), f"wake probe has no nonce: {last}")
        field.fill("echo after wake")
        send.tap()
        page.wait_for_timeout(300)
        expect(fx.frames("s-shell")[before:] == ["echo after wake\r"], f"frames {fx.frames('s-shell')[before:]!r}")
        expect(page.locator(".term-compose-note").count() == 0, "a note flashed for an answered probe")
    r.run("wake, matching reply: the probe answers, Send works once", answered)

    def stale_then_matching() -> None:
        fx.mute_pings = True
        before = len(fx.frames("s-shell"))
        field.fill("held for the probe")
        page.evaluate(wake)
        page.wait_for_timeout(100)
        nonce = fx.pings["s-shell"][-1]["nonce"]
        older = fx.pings["s-shell"][-2].get("nonce")
        # Old replies and output that were buffered before the pause must not confirm it.
        fx.send_raw("s-shell", {"type": "pong"})
        fx.send_raw("s-shell", {"type": "pong", "nonce": older or "old"})
        fx.send_raw("s-shell", b"queued output\r\n")
        page.wait_for_timeout(300)
        blocked("s-shell", before, "after a stale reply")
        expect(field.input_value() == "held for the probe", "draft changed during the probe")
        fx.send_raw("s-shell", {"type": "pong", "nonce": nonce})
        page.wait_for_function(keys_on, timeout=2000)
        fx.mute_pings = False
        expect(fx.frames("s-shell")[before:] == [], "input replayed after the matching reply")
    r.run("wake, stale replies rejected; only the matching nonce ends the probe", stale_then_matching)

    def unanswered() -> None:
        fx.mute_pings = True
        before, connects = len(fx.frames("s-shell")), fx.connects["s-shell"]
        field.fill("draft kept across a dead socket")
        page.evaluate(wake)
        page.wait_for_timeout(100)
        blocked("s-shell", before, "during an unanswered probe")
        page.wait_for_timeout(1400)
        expect("Checking the connection" in page.locator(".term-compose-note").inner_text(), "no note for a slow probe")
        fx.mute_pings = False
        page.wait_for_function(keys_on, timeout=10000)  # 4 s timeout -> new socket -> ready
        expect(fx.connects["s-shell"] == connects + 1, "the dead socket was not replaced")
        page.wait_for_timeout(300)
        expect(fx.frames("s-shell")[before:] == [], f"input sent or replayed: {fx.frames('s-shell')[before:]!r}")
        expect(field.input_value() == "draft kept across a dead socket", "draft lost")
        field.fill("")
    r.run("wake, no reply: input waits, draft kept, 4 s timeout reconnects, nothing replayed", unanswered)

    def enter_held_then_sent() -> None:
        page.locator(".switch-chip", has_text="alpha").tap()
        wait_open(page)
        before = len(fx.frames("s-alpha"))
        fx.mute_pings = True
        field.fill("held enter")
        send.tap()
        page.evaluate(wake)  # within the 300 ms paste pause
        page.wait_for_timeout(500)
        expect(fx.frames("s-alpha")[before:] == ["\x1b[200~held enter\x1b[201~"], f"frames {fx.frames('s-alpha')[before:]!r}")
        fx.send_raw("s-alpha", {"type": "pong", "nonce": fx.pings["s-alpha"][-1]["nonce"]})
        page.wait_for_timeout(300)
        fx.mute_pings = False
        expect(fx.frames("s-alpha")[before:] == ["\x1b[200~held enter\x1b[201~", "\r"],
               f"Enter not sent once after the matching reply: {fx.frames('s-alpha')[before:]!r}")
    r.run("delayed Enter during a probe: held, then sent once on its own socket", enter_held_then_sent)

    def enter_dropped_on_timeout() -> None:
        before, connects = len(fx.frames("s-alpha")), fx.connects["s-alpha"]
        fx.mute_pings = True
        field.fill("dropped enter")
        send.tap()
        page.evaluate(wake)
        page.wait_for_timeout(4600)  # probe timeout -> replaced socket
        fx.mute_pings = False
        page.wait_for_function(keys_on, timeout=8000)
        expect(fx.connects["s-alpha"] == connects + 1, "socket not replaced")
        page.wait_for_timeout(300)
        expect(fx.frames("s-alpha")[before:] == ["\x1b[200~dropped enter\x1b[201~"],
               f"Enter moved to the new socket or text replayed: {fx.frames('s-alpha')[before:]!r}")
        expect("Enter" in page.locator(".term-compose-note").inner_text(), "no note that Enter was not sent")
    r.run("delayed Enter, probe times out: Enter dropped with a note, never sent on the new socket",
          enter_dropped_on_timeout)
    ctx.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--shots", type=Path, default=None)
    parser.add_argument("--before", type=Path, default=None, help="Installed build, captured for the audit")
    parser.add_argument("--only-geometry", action="store_true",
                        help="Only the tablet keyboard, wide visibility, phone keyboard and landscape checks")
    args = parser.parse_args()
    if not (args.dist / "index.html").is_file():
        print(f"No index.html under {args.dist}", file=sys.stderr)
        return 2
    if args.shots:
        args.shots.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    r = Reporter()
    started = time.time()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=BRAVE, headless=True, args=ARGS)
        try:
            if args.before:
                try:
                    keyboard_geometry(r, browser, args.before, args.shots, "before", assert_fix=False)
                except Exception as exc:  # noqa: BLE001 - evidence capture only
                    print(f"INFO  installed-build capture failed: {exc}")
            tablet_keyboard_checks(r, browser, args.dist, args.shots)
            wide_visibility_checks(r, browser, args.dist, args.shots)
            if not args.only_geometry:
                wake_checks(r, browser, args.dist)
                same_session_guard_checks(r, browser, args.dist)
                phone_checks(r, browser, args.dist, args.shots)
            keyboard_geometry(r, browser, args.dist, args.shots, "after", assert_fix=True)
            landscape_checks(r, browser, args.dist, args.shots)
            if not args.only_geometry:
                tablet_checks(r, browser, args.dist, args.shots)
                desktop_checks(r, browser, args.dist, args.shots)
        finally:
            browser.close()
    total = r.passed + r.failed
    print(f"\n{r.passed}/{total} steps passed, {r.failed} failed ({time.time() - started:.0f} s)")
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
