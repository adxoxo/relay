#!/usr/bin/env python3
"""Live check for the phone Activity page on a temporary relay stack.

It never touches the running relay service, its state, its tmux servers, or
its frontend build.

What it starts (and stops at the end, by exact PID):

- a temporary relay app on 127.0.0.1:18766 with its own state folder, a
  disposable token, the given frontend build, and ``background=False``: no
  hook provisioning, no inventory reconcile, no inbox poll. Each env gets a
  disposable projects root ``/home/node/.relay-fixture-act-<id>`` and its own
  tmux socket paths;
- neutral fixture rows in the temporary database: four shell sessions and
  fourteen Activity items with fixed ages;
- a harness control folder: the app process turns a request file into one
  ``activity.record`` call (the real bus and stream), an empty feed, or a
  fixture session row change;
- for the terminal check only: one tmux server in job-work1 on the isolated
  socket ``/tmp/tmux-1000/relay-act-test`` with one idle ``sh`` session. The
  helper stops it with ``kill-server`` and waits for its PID to exit.

Then it drives Brave (Playwright). Desktop (768 and 1280 px) runs first with a
fixed clock and reduced motion, so the screenshots compare pixel for pixel
with an earlier run (``--compare``). The phone flow (390 and 320 px) covers
the Activity page entries, history, the Files guard, resize, live update,
Mark all read, and the empty state.

Usage:
    .venv/bin/python e2e/activity_page_live.py --dist BUILD_DIR --out DIR [--desktop-only] [--compare DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import secrets
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import traceback
import zlib
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

BRAVE = "/opt/brave.com/brave/brave"
PORT = 18766
CONTAINERS = {"work1": "job-work1", "work2": "job-work2"}
HOST_HOMES = {"work1": Path.home() / "Work" / "work1", "work2": Path.home() / "Work" / "work2"}
TMUX_SOCKET = "/tmp/tmux-1000/relay-act-test"  # only the helper's own tmux server listens here
PREVIEWS_SOCKET = "/tmp/tmux-1000/relay-act-previews"
NOTES = "# Fixture notes\n\nThis file is a disposable fixture for the Activity check.\n"
LONG_NOTES = "# Long fixture notes\n\n" + "".join(f"- Fixture line {n} for the Files scroll check.\n" for n in range(1, 121))

SESSIONS = [  # id, env, name, age of last activity (s)
    ("fixture-a", "work1", "fixture-alpha", 120),
    ("fixture-b", "work2", "fixture-beta", 600),
    ("fixture-c", "work1", "fixture-gamma", 1800),
    ("fixture-d", "work2", "fixture-delta", 5400),
]
LONG = (
    "The fixture turn finished. It updated the sample notes and ran the sample checks. "
    "All checks passed. The summary lists each changed file. Next, review the summary "
    "and send the next prompt when you are ready. This line is here to test the clamp."
)
ITEMS = [  # kind, title, body, session, age (s), read
    ("needs_input", "Needs approval: fixture command", "Run the fixture check in fixture-alpha.", "fixture-a", 240, False),
    ("turn_complete", "Turn complete", LONG, "fixture-b", 720, False),
    ("tool", "Edited fixture/notes.md", None, "fixture-a", 1500, False),
    ("turn_complete", "Turn complete", "The sample layout is ready for review.", "fixture-c", 3600, True),
    ("check", "Fixture check passed", "All 12 sample checks passed.", None, 4200, True),
    ("session_created", "Started fixture-delta", None, "fixture-d", 5000, True),
    ("needs_input", "Needs your answer", "Waiting for your answer about the sample layout.", "fixture-d", 6000, False),
    ("tool", "Ran the sample tests", "npm test (fixture)", "fixture-b", 7200, True),
    ("preview", "Preview fixture-web is up", None, None, 9000, True),
    ("turn_complete", "Turn complete", "Done. The sample page is tidy.", "fixture-a", 10800, True),
    ("session_created", "Started fixture-gamma", None, "fixture-c", 14400, True),
    ("tool", "Edited fixture/layout.md", "One table row changed.", "fixture-c", 18000, False),
    ("error", "Fixture error", "A sample command failed once and then passed.", "fixture-b", 21600, True),
    ("session_created", "Started fixture-alpha", None, "fixture-a", 25200, True),
]
LIVE_EVENT = {
    "kind": "turn_complete",
    "title": "Fixture turn complete",
    "body": "Neutral fixture event for the Activity page check.",
    "session_id": "fixture-b",
    "env": "work2",
}


# -- small helpers -------------------------------------------------------------

def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_port(port: int, seconds: float = 30) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


class Report:
    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.secrets: list[str] = []  # disposable token and cookie: never written out

    def redact(self, text: str) -> str:
        for value in self.secrets:
            if value:
                text = text.replace(value, "<redacted>")
        return text

    def step(self, name: str, fn: Callable[[], Any]) -> Any:
        started = time.time()
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - record and continue
            detail = self.redact(f"{type(exc).__name__}: {exc}")[:800]
            self.steps.append({"step": name, "result": "FAIL", "detail": detail, "seconds": round(time.time() - started, 2)})
            print(f"FAIL  {name}: {detail}", flush=True)
            return None
        self.steps.append({"step": name, "result": "PASS", "detail": value, "seconds": round(time.time() - started, 2)})
        print(f"PASS  {name}", flush=True)
        return value

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s["result"] == "FAIL")


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def wait_js(pg: Any, js: str, timeout: float = 10) -> None:
    """Poll a page expression. The app CSP blocks wait_for_function; page.evaluate is not affected."""
    deadline = time.time() + timeout
    while not pg.evaluate(js):
        check(time.time() < deadline, f"timed out: {js}")
        time.sleep(0.1)


def png_rows(path: Path) -> tuple[int, int, list[bytes]]:
    """Decode an 8-bit, non-interlaced RGB or RGBA PNG (what Chromium writes)."""
    data = path.read_bytes()
    pos, idat, width, height, bpp = 8, b"", 0, 0, 4
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
            check(depth == 8 and interlace == 0 and ctype in (2, 6), f"unsupported PNG {path.name}")
            bpp = 4 if ctype == 6 else 3
        elif kind == b"IDAT":
            idat += body
    raw = zlib.decompress(idat)
    stride = width * bpp
    rows, prev, i = [], bytearray(stride), 0
    for _ in range(height):
        f, line = raw[i], bytearray(raw[i + 1 : i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(bytes(line))
        prev = line
    return width, height, rows


def pixel_diff(a: Path, b: Path) -> dict[str, Any]:
    """Count changed pixels, the largest channel change, and their bounding box."""
    if a.read_bytes() == b.read_bytes():
        return {"identical_bytes": True, "different_pixels": 0, "max_channel_delta": 0}
    wa, ha, ra = png_rows(a)
    wb, hb, rb = png_rows(b)
    if (wa, ha) != (wb, hb):
        return {"identical_bytes": False, "size": [[wa, ha], [wb, hb]], "different_pixels": None, "max_channel_delta": 255}
    bpp = len(ra[0]) // wa
    count, delta, xs, ys = 0, 0, [], []
    for y in range(ha):
        if ra[y] == rb[y]:
            continue
        for x in range(wa):
            pa, pb = ra[y][x * bpp : x * bpp + bpp], rb[y][x * bpp : x * bpp + bpp]
            if pa != pb:
                count += 1
                delta = max(delta, max(abs(i - j) for i, j in zip(pa, pb)))
                xs.append(x)
                ys.append(y)
    return {"identical_bytes": False, "different_pixels": count, "max_channel_delta": delta,
            "bbox": [min(xs), min(ys), max(xs), max(ys)]}


# -- fixtures and the temporary relay ---------------------------------------------

def make_fixtures(fixture: str) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for env_id, home in HOST_HOMES.items():
        root = home / fixture
        if root.exists():
            raise RuntimeError(f"fixture folder exists already: {root}")
        root.mkdir(mode=0o755)
        roots[env_id] = root
    (roots["work1"] / "notes.md").write_text(NOTES)
    (roots["work1"] / "docs").mkdir()
    (roots["work1"] / "docs" / "long-notes.md").write_text(LONG_NOTES)
    (roots["work2"] / "README.md").write_text("# Work 2 fixture\n")
    return roots


def seed(db: Any, now: float) -> None:
    for sid, env_id, name, age in SESSIONS:
        db.insert_session({
            "id": sid, "env": env_id, "kind": "shell", "display_name": name, "tmux_name": name,
            "origin": "dashboard", "status": "live", "state": "idle", "socket_path": TMUX_SOCKET,
            "project_dir": "/home/node/fixture-project", "activity_at": now - age,  # display only: fixed for pixel checks
            "created_at": now - 30000, "updated_at": now - age,
        })
    env_of = {sid: env_id for sid, env_id, _, _ in SESSIONS}
    for n, (kind, title, body, sid, age, read) in enumerate(reversed(ITEMS)):
        attention = kind in ("needs_input", "turn_complete", "session_interrupted", "error")
        db.execute(
            "INSERT INTO activity (session_id, env, kind, title, body, attention, read_at, dedupe_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sid, env_of.get(sid), kind, title, body, int(attention), (now - age + 30) if read else None, f"fixture-{n}", now - age),
        )


def serve(cfg_path: str) -> None:
    import uvicorn

    from relay import activity
    from relay.app import create_app
    from relay.config import EnvConfig, Settings
    from relay.runtime import build_runtime

    cfg = json.loads(Path(cfg_path).read_text())
    envs = {
        env_id: EnvConfig(
            id=env_id, label=label, container=CONTAINERS[env_id], host_home=HOST_HOMES[env_id],
            projects_root=f"/home/node/{cfg['fixture']}", tmux_sockets=(TMUX_SOCKET,), previews_socket=PREVIEWS_SOCKET,
        )
        for env_id, label in (("work1", "Work 1"), ("work2", "Work 2"))
    }
    settings = Settings(state_dir=Path(cfg["state"]), envs=envs, port=PORT, frontend_dist=Path(cfg["dist"]), docker_socket=None)
    rt = build_runtime(settings)
    for env_id, env in envs.items():
        rt.env_status[env_id].container = asyncio.run(rt.podman.inspect(env))  # read only
    seed(rt.db, cfg["now"])
    app = create_app(runtime=rt, features=True, background=False)
    control = Path(cfg["control"])

    async def control_loop() -> None:
        # A request file becomes one real activity.record call (bus + stream) or an empty feed.
        while True:
            for req in sorted(control.glob("*.json")):
                body = json.loads(req.read_text())
                if body["op"] == "event":
                    ev = body["event"]
                    new_id = activity.record(rt.db, kind=ev["kind"], title=ev["title"], body=ev["body"],
                                             session_id=ev["session_id"], env=ev["env"], bus=rt.bus)
                    result = {"id": new_id}
                elif body["op"] == "session":
                    rt.db.update_session(body["event"]["id"], **body["event"]["fields"])
                    rt.bus.publish("sessions")
                    result = {"updated": body["event"]["id"]}
                else:
                    rt.db.execute("DELETE FROM activity")
                    rt.bus.publish("activity")
                    result = {"cleared": True}
                req.unlink()
                req.with_suffix(".done").write_text(json.dumps(result))
            await asyncio.sleep(0.2)

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):
            self._control = asyncio.get_running_loop().create_task(control_loop())
            await super().startup(sockets)

    Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="info")).run()


def harness(control: Path, op: str, event: dict | None = None) -> dict:
    name = f"req-{time.time_ns()}"
    (control / f"{name}.tmp").write_text(json.dumps({"op": op, "event": event}))
    (control / f"{name}.tmp").rename(control / f"{name}.json")
    done = control / f"{name}.done"
    deadline = time.time() + 10
    while not done.exists():
        check(time.time() < deadline, f"harness did not answer {op}")
        time.sleep(0.1)
    return json.loads(done.read_text())


# -- the isolated tmux server (terminal check only) ------------------------------------------

def podman(*args: str, check_rc: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if "XDG_DATA_HOME_VSCODE_SNAP_ORIG" in env:
        env["XDG_DATA_HOME"] = env["XDG_DATA_HOME_VSCODE_SNAP_ORIG"]
    return subprocess.run(["podman", "--events-backend=none", *args], capture_output=True, timeout=30, check=check_rc, env=env)


def tmux(*args: str, check_rc: bool = True) -> str:
    res = podman("exec", "--user", "1000:1000", CONTAINERS["work1"], "tmux", "-S", TMUX_SOCKET, *args, check_rc=check_rc)
    return res.stdout.decode().strip()


def tmux_socket_free() -> bool:
    return podman("exec", "--user", "1000:1000", CONTAINERS["work1"], "test", "-e", TMUX_SOCKET, check_rc=False).returncode != 0


def start_tmux(fixture: str) -> dict:
    tmux("new-session", "-d", "-s", "act-term", "-x", "80", "-y", "24", "-c", f"/home/node/{fixture}", "sh")
    sid, pane, pid = tmux("display-message", "-p", "-t", "act-term", "#{session_id} #{pane_id} #{pid}").split()
    return {"session_id": sid, "pane_id": pane, "server_pid": int(pid)}


def stop_tmux(info: dict) -> dict:
    if "server_pid" not in info:  # started, but the PID read failed
        info["server_pid"] = int(tmux("display-message", "-p", "#{pid}", check_rc=False) or 0)
    tmux("kill-server", check_rc=False)
    deadline = time.time() + 10
    alive = info["server_pid"] > 0
    while alive and time.time() < deadline:
        alive = podman("exec", CONTAINERS["work1"], "kill", "-0", str(info["server_pid"]), check_rc=False).returncode == 0
        if not alive:
            break
        time.sleep(0.2)
    podman("exec", "--user", "1000:1000", CONTAINERS["work1"], "rm", "-f", TMUX_SOCKET, check_rc=False)
    gone = podman("exec", CONTAINERS["work1"], "test", "-e", TMUX_SOCKET, check_rc=False).returncode != 0
    return {"server_pid": info["server_pid"], "server_exited": not alive, "socket_removed": gone}


def tmux_size() -> dict:
    clients = tmux("list-clients", "-F", "#{client_width}x#{client_height}", check_rc=False).split()
    window = tmux("display-message", "-p", "-t", "act-term", "#{window_width}x#{window_height}", check_rc=False)
    return {"clients": clients, "window": window}


# -- browser steps ------------------------------------------------------------------------

DESKTOP_WIDTHS = ((768, 1024), (1280, 900))


def desktop_shots(browser: Any, base: str, cookie: str, now: float, folder: Path) -> dict:
    """Controlled desktop screenshots: fixed clock, reduced motion, fonts ready, same state."""
    folder.mkdir(parents=True, exist_ok=True)
    result = {}
    for width, height in DESKTOP_WIDTHS:
        ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1, reduced_motion="reduce")
        ctx.add_cookies([{"name": "relay_session", "value": cookie, "url": base}])
        ctx.clock.set_fixed_time(dt.datetime.fromtimestamp(now, dt.timezone.utc))
        pg = ctx.new_page()
        pg.goto(base + "/")
        wait_js(pg, "document.querySelectorAll('.agent-card').length === 4")
        pg.wait_for_selector(".conn-indicator:not(.reconnecting)")
        pg.evaluate("document.fonts.ready.then(() => true)")
        pg.mouse.move(2, height - 2)
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(folder / f"w{width}-agents.png"))
        pg.click(".bell")
        wait_js(pg, f"document.querySelectorAll('aside.notification-panel .notice').length === {len(ITEMS)}")
        pg.mouse.move(2, height - 2)
        pg.wait_for_timeout(300)
        pg.screenshot(path=str(folder / f"w{width}-popup.png"))
        m = pg.evaluate("() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,"
                        " expanded: document.querySelector('.bell').getAttribute('aria-expanded')})")
        check(m["sw"] <= m["cw"], f"{width}px popup: page overflow {m}")
        check(m["expanded"] == "true", f"{width}px: the desktop bell keeps aria-expanded: {m}")
        result[width] = m
        ctx.close()
    return result


def run_checks(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    shots = out / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    if not port_free(PORT):
        print(f"port {PORT} is in use; stop", file=sys.stderr)
        return 2
    fixture = f".relay-fixture-act-{secrets.token_hex(4)}"
    work = Path(args.work) / fixture
    work.mkdir(parents=True)
    state, control = work / "state", work / "control"
    state.mkdir()
    control.mkdir()
    now = float(int(time.time()))
    rep = Report()
    info: dict[str, Any] = {"fixture_root": f"/home/node/{fixture}", "relay_port": PORT, "dist": args.dist}
    server: subprocess.Popen | None = None
    roots: dict[str, Path] = {}
    base = f"http://127.0.0.1:{PORT}"
    db_path = state / "relay.db"

    def db_one(sql: str, params: tuple = ()) -> Any:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            return con.execute(sql, params).fetchone()
        finally:
            con.close()

    try:
        roots = make_fixtures(fixture)
        cfg = work / "harness.json"
        cfg.write_text(json.dumps({"fixture": fixture, "state": str(state), "dist": args.dist, "now": now, "control": str(control)}))
        log = open(out / "harness-relay.log", "w")
        server = subprocess.Popen([sys.executable, __file__, "--serve", str(cfg)], stdout=log, stderr=subprocess.STDOUT, cwd=str(REPO / "backend"))
        info["server_pid"] = server.pid
        check(wait_port(PORT), "harness did not start")
        token = (state / "token").read_text().strip()
        rep.secrets.append(token)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=BRAVE, headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
            page = phone.new_page()
            js_errors: list[str] = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))

            def login() -> str:
                page.goto(f"{base}/#token={token}")
                page.wait_for_selector(".mobile-nav")
                wait_js(page, "document.querySelectorAll('.agent-card').length === 4")
                cookie = [c for c in phone.cookies() if c["name"] == "relay_session"]
                check(len(cookie) == 1, "login sets relay_session")
                rep.secrets.append(cookie[0]["value"])
                return "disposable token login on the temporary stack"

            rep.step("login", login)
            cookie = next(c["value"] for c in phone.cookies() if c["name"] == "relay_session")

            folder = out / "desktop"
            rep.step("desktop 768/1280: controlled screenshots, popup open, no overflow",
                     lambda: desktop_shots(browser, base, cookie, now, folder))
            if args.compare:
                def compare() -> dict:
                    res = {}
                    for p in sorted(Path(args.compare).glob("*.png")):
                        res[p.name] = pixel_diff(p, folder / p.name)
                    check(len(res) == 2 * len(DESKTOP_WIDTHS), f"baseline images: {sorted(res)}")
                    # A change of 1 in one channel is rasterizer noise at anti-aliased edges; the
                    # same build can give it on one run and not on the next. Anything larger fails.
                    changed = {k: v for k, v in res.items() if v["max_channel_delta"] > 1}
                    check(not changed, f"desktop pixels changed: {changed}")
                    return res

                rep.step("desktop 768/1280: pixels equal the pre-edit build", compare)
            if args.desktop_only:
                browser.close()
                return 1 if rep.failed else 0

            # -- phone flow -----------------------------------------------------------
            tab = ".mobile-nav button:has-text('Activity')"
            activity_page = ".view-activity:not([hidden])"
            item_back = ".activity-session:not([hidden]) .mobile-back"  # the session opened from Activity
            dash_back = ".dashboard .conversation .mobile-back"  # the ordinary session panel
            responses: list[Any] = []
            page.on("response", lambda r: responses.append(r) if r.url.startswith(base + "/api/activity?") else None)
            terminal_ws: list[str] = []
            page.on("websocket", lambda w: terminal_ws.append(w.url.split("/api/", 1)[-1]) if "/terminal?" in w.url else None)

            def ev(js: str) -> Any:
                return page.evaluate(js)

            def wait_badge(n: int) -> None:
                js = f"document.querySelector('.mobile-nav .nav-badge')?.textContent === '{n}'" if n else "!document.querySelector('.mobile-nav .nav-badge')"
                wait_js(page, js, 5)

            def wait_read(item_id: int) -> None:
                deadline = time.time() + 5
                while db_one("SELECT read_at FROM activity WHERE id = ?", (item_id,))[0] is None:
                    check(time.time() < deadline, f"item {item_id} is still unread in the database")
                    time.sleep(0.1)

            def api_items() -> dict:
                return ev("fetch('/api/activity?limit=50').then(r => r.json())")

            def sort_items(items: list[dict]) -> list[dict]:
                return sorted(items, key=lambda i: (-int(i["attention"]), -i["created_at"]))

            def shown_titles() -> list[str]:
                return page.locator(".as-page .notice strong").all_inner_texts()

            def wait_scroll(y: float) -> None:
                try:
                    wait_js(page, f"Math.abs(window.scrollY - {y}) <= 2", 5)
                except AssertionError:
                    raise AssertionError(f"scroll {ev('window.scrollY')} != {y}") from None

            state_: dict[str, Any] = {}

            def open_from_agents() -> dict:
                ev("window.scrollTo(0, 200)")
                state_["agents_y"] = ev("window.scrollY")
                check(state_["agents_y"] > 50, f"Agents must scroll: {state_['agents_y']}")
                h0 = ev("history.length")
                page.click(tab)
                page.wait_for_selector(activity_page)
                check(page.locator("aside.notification-panel").count() == 0, "no popup on a phone")
                heads = ev("[...document.querySelectorAll('.view-activity h1, .view-activity h2')].map(e => e.tagName + ':' + e.textContent)")
                check(heads[:2] == ["H1:Activity", "H2:While you were away"], f"headings: {heads}")
                check(page.get_attribute(tab, "aria-current") == "page", "tab aria-current")
                check(ev("history.length") == h0 + 1, "one history entry")
                check(ev("window.scrollY") == 0, "page opens at the top")
                page.screenshot(path=str(shots / "phone-390-activity.png"))
                state_["h_open"] = h0 + 1
                return {"agents_scroll": state_["agents_y"], "headings": heads}

            rep.step("phone: Activity tab opens the page (no popup, one entry, aria-current)", open_from_agents)

            def retap() -> str:
                page.click(tab)
                page.click(".bell")
                page.click(tab)
                page.wait_for_timeout(300)
                check(page.locator(activity_page).count() == 1, "page stays open")
                check(ev("history.length") == state_["h_open"], "re-taps add no entry")
                check(page.get_attribute(".bell", "aria-expanded") is None, "the phone bell opens a page, no aria-expanded")
                return "tab, bell, tab: one page, no new entry"

            rep.step("phone: re-tap of the tab and the bell keeps one page and one entry", retap)

            def order_and_marks() -> dict:
                data = api_items()
                want = [i["title"] for i in sort_items(data["items"])]
                check(shown_titles() == want, f"order {shown_titles()} != {want}")
                unread = sum(1 for i in data["items"] if not i["read"])
                check(page.locator(".as-page .notice.unread").count() == unread, "unread marks")
                check(page.inner_text(".mobile-nav .nav-badge") == str(data["unread"]), "tab badge")
                check(page.inner_text(".bell .bell-badge") == str(data["unread"]), "bell badge")
                clamp = ev("getComputedStyle(document.querySelector('.as-page .notice-body')).webkitLineClamp")
                check(clamp == "4", f"body clamp {clamp}")
                return {"items": len(want), "unread_marks": unread, "badge": data["unread"]}

            rep.step("phone: list order, unread marks, badges, 4-line excerpt", order_and_marks)

            def sizes_and_scroll() -> dict:
                m = ev("""() => {
                  const fs = (s) => getComputedStyle(document.querySelector(s)).fontSize;
                  const box = (e) => { const r = e.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; };
                  const panel = document.querySelector('.as-page');
                  let safe = false;
                  const walk = (rules) => { for (const r of rules) {
                    if (r.selectorText && r.selectorText.includes('.view-activity') && r.cssText.includes('safe-area-inset-bottom')) safe = true;
                    if (r.cssRules) walk(r.cssRules); } };
                  for (const sh of document.styleSheets) walk(sh.cssRules);
                  return {title: fs('.as-page .notice strong'), body: fs('.as-page .notice-body'), time: fs('.as-page .notice small'),
                    tab: box([...document.querySelectorAll('.mobile-nav button')].find(b => b.textContent.includes('Activity'))),
                    bell: box(document.querySelector('.bell')), markAll: box(document.querySelector('.as-page .mark-all')),
                    items: [...document.querySelectorAll('.as-page .notice')].map(box),
                    panelScrolls: panel.scrollHeight > panel.clientHeight + 1, docScrolls: document.documentElement.scrollHeight > innerHeight,
                    safe};
                }""")
                check(m["title"] == "16px" and m["body"] == "16px" and m["time"] == "14px", f"text sizes {m}")
                small = {k: m[k] for k in ("tab", "bell", "markAll") if min(m[k]) < 44}
                small_items = [b for b in m["items"] if min(b) < 44]
                check(not small and not small_items, f"targets under 44 px: {small} {small_items[:3]}")
                check(m["docScrolls"] and not m["panelScrolls"], "the page scrolls, not the panel")
                check(m["safe"], "safe-area rule on .view-activity")
                ev("window.scrollTo(0, document.documentElement.scrollHeight)")
                page.wait_for_timeout(200)
                gap = ev("document.querySelector('.mobile-nav').getBoundingClientRect().top"
                         " - [...document.querySelectorAll('.as-page .notice')].pop().getBoundingClientRect().bottom")
                check(gap >= 0, f"last item under the nav by {-gap}px")
                m["last_item_gap_px"] = gap
                del m["items"]
                return m

            rep.step("phone: 16/14 px text, 44 px targets, page scroll, room for nav and safe area", sizes_and_scroll)

            absent: set[str] = {"fixture-gone"}  # session ids that are not in the state

            def visible_item(unread_only: bool, not_session: str | None = None, want: str | None = None) -> tuple[int, dict]:
                data = sort_items(api_items()["items"])
                wait_js(page, f"document.querySelectorAll('.as-page .notice').length === {len(data)}")
                boxes = ev("[...document.querySelectorAll('.as-page .notice')].map(e => { const r = e.getBoundingClientRect(); return [r.top, r.bottom]; })")
                nav_top = ev("document.querySelector('.mobile-nav').getBoundingClientRect().top")
                for i, (top, bottom) in enumerate(boxes):
                    item = data[i]
                    sid = item["session_id"]
                    if (top >= 70 and bottom <= nav_top and sid and sid not in absent and sid != not_session
                            and (want is None or sid == want) and (item["read"] is False or not unread_only)):
                        return i, item
                raise AssertionError(f"no visible item with a session (want {want})")

            def scroll_list_to(want: str) -> float:
                """Scroll the list so the first item of a session sits in the upper half."""
                data = sort_items(api_items()["items"])
                wait_js(page, f"document.querySelectorAll('.as-page .notice').length === {len(data)}")
                i = next(n for n, it in enumerate(data) if it["session_id"] == want)
                ev(f"window.scrollTo(0, Math.max(0, document.querySelectorAll('.as-page .notice')[{i}].getBoundingClientRect().top + scrollY - 260))")
                return ev("window.scrollY")

            def item_and_back_arrow() -> dict:
                ev("window.scrollTo(0, 500)")
                y = ev("window.scrollY")
                check(y > 100, f"Activity must scroll: {y}")
                i, item = visible_item(unread_only=True)
                before = int(page.inner_text(".mobile-nav .nav-badge"))
                url = ev("location.href")
                page.locator(".as-page .notice").nth(i).click()
                back = page.locator(item_back)
                back.wait_for(state="visible")
                check(back.get_attribute("aria-label") == "Back to Activity", "back label")
                check(page.locator(activity_page).count() == 0, "Activity page hidden")
                name = page.inner_text(".activity-session:not([hidden]) .chat-name h2")
                want = next(n for sid, _, n, _ in SESSIONS if sid == item["session_id"])
                check(name.startswith(want), f"item session {name} != {want}")
                check(ev("location.href") == url, "the item entry keeps the URL of the entry under the visit")
                check(page.get_attribute(".mobile-nav button:has-text('Session')", "aria-current") == "page", "Session current")
                after = before - 1 if item["attention"] else before
                wait_badge(after)
                wait_read(item["id"])
                box = back.bounding_box()
                check(box["width"] >= 44 and box["height"] >= 44, f"back arrow {box}")
                page.screenshot(path=str(shots / "phone-390-session-from-activity.png"))
                back.click()
                page.wait_for_selector(activity_page)
                wait_scroll(y)
                check(page.get_attribute(tab, "aria-current") == "page", "Activity tab current again")
                return {"item_id": item["id"], "session": item["session_id"], "list_scroll": y, "badge": [before, after]}

            rep.step("phone: item opens its session, marks it read; Back arrow returns to the same list position", item_and_back_arrow)

            def item_and_browser_back() -> dict:
                ev("window.scrollTo(0, 300)")
                y = ev("window.scrollY")
                i, item = visible_item(unread_only=False)
                page.locator(".as-page .notice").nth(i).click()
                page.wait_for_selector(item_back, state="visible")
                ev("history.back()")
                page.wait_for_selector(activity_page)
                wait_scroll(y)
                check(page.locator(".activity-session:not([hidden])").count() == 0, "session hidden")
                return {"session": item["session_id"], "list_scroll": y}

            rep.step("phone: browser Back from that session returns to the same list position", item_and_browser_back)

            def nested() -> dict:
                ev("window.scrollTo(0, 400)")
                y = ev("window.scrollY")
                i, item = visible_item(unread_only=False)
                page.locator(".as-page .notice").nth(i).click()
                back = page.locator(item_back)
                back.wait_for(state="visible")
                page.click(tab)  # Activity again, from the session that Activity opened
                page.wait_for_selector(activity_page)
                check(ev("window.scrollY") == 0, "the new visit opens at the top")
                ev("history.back()")
                back.wait_for(state="visible")  # the session, still with its Back to Activity
                back.click()
                page.wait_for_selector(activity_page)
                wait_scroll(y)
                return {"session": item["session_id"], "list_scroll": y}

            rep.step("phone: Activity from a session opened by Activity; Back twice returns to the list", nested)

            def back_and_forward() -> dict:
                list_y = ev("window.scrollY")
                ev("history.back()")
                page.wait_for_selector(".intro", state="visible")
                check(page.locator(activity_page).count() == 0, "Activity page closed")
                wait_scroll(state_["agents_y"])
                check(page.get_attribute(".mobile-nav button:has-text('Agents')", "aria-current") == "page", "Agents current")
                ev("history.forward()")  # native Forward into the Activity entry
                page.wait_for_selector(activity_page)
                wait_scroll(list_y)
                check(page.get_attribute(tab, "aria-current") == "page", "Activity current after Forward")
                ev("history.forward()")  # and into its item session
                back = page.locator(item_back)
                back.wait_for(state="visible")
                name = page.inner_text(".activity-session:not([hidden]) .chat-name h2")
                ev("history.back()")
                page.wait_for_selector(activity_page)
                wait_scroll(list_y)
                ev("history.back()")
                page.wait_for_selector(".intro", state="visible")
                wait_scroll(state_["agents_y"])
                return {"agents_scroll": state_["agents_y"], "list_scroll": list_y, "forward_session": name}

            rep.step("phone: Back to Agents at its scroll; Forward reopens Activity at its position and its item session", back_and_forward)

            def from_session() -> str:
                ev("window.scrollTo(0, 0)")
                page.click(".agent-card:has-text('fixture-alpha')")
                back = page.locator(dash_back)
                back.wait_for(state="visible")
                check(back.get_attribute("aria-label") == "Back to agents", "ordinary label")
                page.click(".bell")
                page.wait_for_selector(activity_page)
                check(page.locator(".dashboard .conversation").is_hidden(), "session hidden under Activity")
                ev("history.back()")
                back.wait_for(state="visible")
                check(back.get_attribute("aria-label") == "Back to agents", "ordinary label after restore")
                check(ev("location.hash") == "#/s/fixture-a", "hash kept")
                check(page.get_attribute(".mobile-nav button:has-text('Session')", "aria-current") == "page", "Session current")
                back.click()  # the ordinary Back path is unchanged
                page.wait_for_selector(".intro", state="visible")
                check(ev("location.hash") == "", "ordinary Back clears the hash")
                return "bell from an ordinary session, Back restores it; its Back arrow still goes to Agents"

            rep.step("phone: bell from a session; Back restores the session; ordinary Back unchanged", from_session)

            def from_previews() -> str:
                page.click(".mobile-nav button:has-text('Previews')")
                page.wait_for_selector(".view-previews", state="visible")
                page.click(tab)
                page.wait_for_selector(activity_page)
                check(page.locator(".view-previews").is_hidden(), "Previews hidden under Activity")
                ev("history.back()")
                page.wait_for_selector(".view-previews", state="visible")
                check(page.locator(activity_page).count() == 0, "Activity closed")
                check(page.get_attribute(".mobile-nav button:has-text('Previews')", "aria-current") == "page", "Previews current")
                ev("document.querySelector('.view-previews').__actMark = 1")
                page.click(tab)
                page.wait_for_selector(activity_page)
                i, _ = visible_item(unread_only=False)
                page.locator(".as-page .notice").nth(i).click()
                page.locator(item_back).wait_for(state="visible")
                page.locator(item_back).click()
                page.wait_for_selector(activity_page)
                ev("history.back()")
                page.wait_for_selector(".view-previews", state="visible")
                check(ev("document.querySelector('.view-previews').__actMark === 1"), "the same Previews element after the item round trip")
                return "Previews restored after a direct Back and after an item round trip (same element)"

            rep.step("phone: Back from Activity restores Previews, also after an item session", from_previews)

            notes_host = roots["work1"] / "notes.md"

            def files_guard() -> dict:
                page.click(".mobile-nav button:has-text('Files')")
                page.click(".ws-switch button:has-text('Work 1')")
                page.click(".entries .entry:has-text('notes.md')")
                page.click(".fp-bar button:has-text('Edit')")
                page.fill("#file-editor", "draft that must not be lost\n")
                page.wait_for_selector(".st.dirty")
                h = ev("history.length")
                for control in (tab, ".bell"):
                    page.click(control)
                    page.wait_for_selector(".modal #files-ask-title")
                    page.click(".modal button:has-text('Keep editing')")
                    page.wait_for_selector(".modal #files-ask-title", state="detached")
                    check(page.input_value("#file-editor") == "draft that must not be lost\n", "draft kept")
                    check(page.locator(activity_page).count() == 0, "stays on Files")
                    check(ev("history.length") == h, "no entry after Keep editing")
                # Re-entry: both entry controls in one task while the guard is open.
                ev("() => { const t = [...document.querySelectorAll('.mobile-nav button')].find(b => b.textContent.includes('Activity'));"
                   " t.click(); document.querySelector('.bell').click(); }")
                page.wait_for_selector(".modal #files-ask-title")
                check(page.locator(".modal #files-ask-title").count() == 1, "one dialog")
                page.screenshot(path=str(shots / "phone-390-files-guard.png"))
                page.click(".modal button:has-text('Discard changes')")
                page.wait_for_selector(activity_page)
                page.wait_for_timeout(300)
                check(ev("history.length") == h + 1, f"one entry after Discard: {ev('history.length')} vs {h}")
                check(notes_host.read_text() == NOTES, "the file on disk is unchanged")
                ev("history.back()")
                page.wait_for_selector(".view-files", state="visible")
                check(page.locator(".file-pane").is_visible(), "the open file is restored")
                check(page.locator("#file-editor").count() == 0, "the discarded edit is gone")
                ev("history.back()")  # Files' own Back closes the file
                page.wait_for_selector(".file-pane", state="hidden")
                ev("history.forward()")  # the Files entry: Files keeps the file closed
                page.wait_for_timeout(400)
                check(page.locator(".file-pane").is_hidden(), "Forward to the Files entry does not reopen the file")
                ev("history.forward()")  # the Activity entry: Activity again
                page.wait_for_selector(activity_page)
                check(ev("document.querySelector('.modal #files-ask-title')") is None, "no stray dialog")
                ev("history.back()")
                page.wait_for_selector(".view-files", state="visible")
                check(page.locator(".file-pane").is_hidden(), "Files as it was under Activity")
                return {"history_before": h, "file_unchanged": True}

            rep.step("phone: Files guard (Keep editing, Discard, re-entry) and Files history with Back/Forward", files_guard)

            long_host = roots["work1"] / "docs" / "long-notes.md"

            def files_view() -> dict:
                return ev("""() => { const v = document.querySelector('.view-files'); const pane = document.querySelector('.file-pane');
                  return {same_element: v?.__actMark === 1, visible: !!v && v.offsetParent !== null,
                    file: pane && pane.offsetParent !== null ? document.querySelector('.fp-title strong')?.textContent ?? null : null,
                    view_tab: document.querySelector('.fp-bar .seg button[aria-pressed=true]')?.textContent ?? null,
                    editing: !!document.querySelector('#file-editor'),
                    search: document.querySelector('#file-search')?.value ?? null,
                    entry: JSON.stringify(history.state), y: Math.round(scrollY)}; }""")

            def files_state() -> dict:
                page.click(".mobile-nav button:has-text('Files')")
                page.click(".ws-switch button:has-text('Work 1')")
                page.fill("#file-search", "long-notes")
                page.click(".results .entry:has-text('long-notes.md')")
                page.click(".fp-bar .seg button:has-text('Text')")
                wait_js(page, "document.documentElement.scrollHeight > innerHeight + 700")
                ev("window.scrollTo(0, 600)")
                ev("document.querySelector('.view-files').__actMark = 1")
                before = files_view()
                check(before["file"] == "long-notes.md" and before["view_tab"] == "Text" and before["search"] == "long-notes"
                      and before["entry"] == '{"relayFile":true}' and before["y"] == 600, f"Files setup {before}")
                # Files, Activity, item session, Activity, Files.
                page.click(tab)
                page.wait_for_selector(activity_page)
                i, item = visible_item(unread_only=False)
                page.locator(".as-page .notice").nth(i).click()
                page.locator(item_back).wait_for(state="visible")
                page.locator(item_back).click()
                page.wait_for_selector(activity_page)
                ev("history.back()")
                page.wait_for_selector(".view-files", state="visible")
                wait_scroll(before["y"])
                after_item = files_view()
                check(after_item == before, f"Files after the item round trip {after_item} != {before}")
                # Native Forward into Activity and Back: Files keeps its open file.
                ev("history.forward()")
                page.wait_for_selector(activity_page)
                check(page.locator(".view-files").is_hidden(), "Files hidden under Activity")
                ev("history.back()")
                page.wait_for_selector(".view-files", state="visible")
                wait_scroll(before["y"])
                after_forward = files_view()
                check(after_forward == before, f"Files after Forward and Back {after_forward} != {before}")
                # Forward from a Files edit with unsaved changes asks first.
                page.click(".fp-bar button:has-text('Edit')")
                page.fill("#file-editor", "unsaved draft before Forward\n")
                page.wait_for_selector(".st.dirty")
                ev("history.forward()")
                page.wait_for_selector(".modal #files-ask-title")
                check(page.locator(activity_page).count() == 0, "Files stays while the guard asks")
                page.click(".modal button:has-text('Keep editing')")
                page.wait_for_selector(".modal #files-ask-title", state="detached")
                wait_js(page, "JSON.stringify(history.state) === '{\"relayFile\":true}'", 5)
                check(page.input_value("#file-editor") == "unsaved draft before Forward\n", "draft kept after Keep editing")
                check(page.locator(activity_page).count() == 0, "no Activity after Keep editing")
                ev("history.forward()")
                page.wait_for_selector(".modal #files-ask-title")
                page.click(".modal button:has-text('Discard changes')")
                page.wait_for_selector(activity_page)
                check(long_host.read_text() == LONG_NOTES, "the file on disk is unchanged")
                ev("history.back()")
                page.wait_for_selector(".view-files", state="visible")
                after_discard = files_view()
                check(after_discard["file"] == "long-notes.md" and not after_discard["editing"] and after_discard["same_element"],
                      f"Files after Discard and Back {after_discard}")
                # Files' own Back afterward closes the file; the search stays.
                ev("history.back()")
                page.wait_for_selector(".file-pane", state="hidden")
                closed = files_view()
                check(closed["same_element"] and closed["search"] == "long-notes", f"Files after its own Back {closed}")
                check(page.locator(".results .entry:has-text('long-notes.md')").is_visible(), "search results shown")
                return {"before": before, "after_item_round_trip": after_item, "after_forward_back": after_forward,
                        "after_discard_back": after_discard, "after_files_back": closed}

            rep.step("phone: Files keeps folder, search, open file, view tab, scroll through an item session and Forward; Forward asks about unsaved edits", files_state)

            def term_view() -> dict:
                return ev("""() => { const r = document.querySelector('.dashboard .xterm-rows'); const sc = document.querySelector('.dashboard .xterm-screen');
                  return {rows: r ? r.children.length : null, screen: sc ? [sc.style.width, sc.style.height] : null,
                    visible: !!sc && sc.offsetParent !== null,
                    tab: document.querySelector('.dashboard .chat-tabs button[aria-pressed=true]')?.textContent?.trim() ?? null}; }""")

            def session_state() -> dict:
                check(tmux_socket_free(), f"{TMUX_SOCKET} exists already in job-work1; stop")
                info["tmux"] = {}  # from here on, cleanup stops this run's tmux server
                term = start_tmux(fixture)
                info["tmux"] = term
                harness(control, "session", {"id": "fixture-a", "fields": {
                    "kind": "claude", "foreground": "claude", "tmux_session_id": term["session_id"], "pane_id": term["pane_id"]}})
                page.click(".mobile-nav button:has-text('Agents')")
                wait_js(page, "[...document.querySelectorAll('.agent-card')].some(c => c.textContent.includes('fixture-alpha') && c.textContent.includes('Claude'))")
                ev("window.scrollTo(0, 0)")
                page.click(".agent-card:has-text('fixture-alpha')")
                composer = ".dashboard .composer textarea"
                page.wait_for_selector(composer, state="visible")
                draft = "unsent draft for fixture-alpha"
                page.fill(composer, draft)
                page.click(".bell")
                page.wait_for_selector(activity_page)
                ev("history.back()")  # direct Back
                page.wait_for_selector(composer, state="visible")
                check(page.input_value(composer) == draft, "draft after a direct Back")
                page.click(".dashboard .chat-tabs button:has-text('Terminal')")
                page.wait_for_selector(".dashboard .xterm-screen", state="visible")
                deadline = time.time() + 10
                while len(tmux_size()["clients"]) != 1:
                    check(time.time() < deadline, "the terminal did not attach to the isolated tmux session")
                    time.sleep(0.2)
                page.wait_for_timeout(1500)  # the first fit and its resize settle
                before = {**term_view(), **tmux_size(), "terminal_sockets": len(terminal_ws)}
                check(before["visible"] and before["tab"] == "Terminal", f"terminal setup {before}")
                page.click(tab)
                page.wait_for_selector(activity_page)
                i, item = visible_item(unread_only=False, not_session="fixture-a")
                page.locator(".as-page .notice").nth(i).click()
                page.locator(item_back).wait_for(state="visible")
                page.wait_for_timeout(1500)
                hidden = {**term_view(), **tmux_size(), "terminal_sockets": len(terminal_ws)}
                page.locator(item_back).click()
                page.wait_for_selector(activity_page)
                ev("history.back()")
                page.wait_for_selector(".dashboard .xterm-screen", state="visible")
                page.wait_for_timeout(1500)
                after = {**term_view(), **tmux_size(), "terminal_sockets": len(terminal_ws)}
                check(not hidden["visible"], "terminal hidden during the item session")
                for k in ("rows", "screen", "tab", "clients", "window", "terminal_sockets"):
                    check(before[k] == hidden[k] == after[k], f"terminal {k}: {before[k]} / {hidden[k]} / {after[k]}")
                page.click(".dashboard .chat-tabs button:has-text('Conversation')")
                page.wait_for_selector(composer, state="visible")
                check(page.input_value(composer) == draft, "draft after the item round trip")
                return {"item_session": item["session_id"], "before": before, "hidden_during_item": hidden, "after": after,
                        "draft_kept": True, "terminal_urls": terminal_ws}

            rep.step("phone: session draft, Terminal tab, and tmux size survive a direct Back and an item round trip", session_state)

            x_draft = "alpha-only draft"
            x_composer = ".dashboard .composer textarea[aria-label='Message fixture-alpha']"

            def composers() -> list:
                return ev("[...document.querySelectorAll('.composer textarea')].map(t => [t.getAttribute('aria-label'), t.value,"
                          " t.offsetParent !== null])")

            def widen_item_draft() -> dict:
                harness(control, "session", {"id": "fixture-b", "fields": {"kind": "claude", "foreground": "claude"}})
                page.wait_for_selector(x_composer, state="visible")  # session X = fixture-alpha, still open
                page.fill(x_composer, x_draft)
                page.click(".bell")
                page.wait_for_selector(activity_page)
                wait_js(page, "!!document.querySelector('.agent-card') && [...document.querySelectorAll('.agent-card')]"
                              ".some(c => c.textContent.includes('fixture-beta') && c.textContent.includes('Claude'))")
                i, _ = visible_item(unread_only=False, want="fixture-b")
                page.locator(".as-page .notice").nth(i).click()
                y_composer = ".activity-session:not([hidden]) .composer textarea"
                page.wait_for_selector(y_composer, state="visible")
                check(page.get_attribute(y_composer, "aria-label") == "Message fixture-beta", "item session Y")
                check(page.input_value(y_composer) == "", "Y starts without a draft")
                page.set_viewport_size({"width": 1024, "height": 844})
                page.wait_for_selector(x_composer, state="visible")
                wide = composers()
                check(page.locator(".view-activity").count() == 0, "the visit ends on widen")
                check(page.input_value(x_composer) == x_draft, f"X keeps its draft: {wide}")
                check(not any(label != "Message fixture-alpha" and value == x_draft for label, value, _ in wide),
                      f"no other composer holds X's draft: {wide}")
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_selector(x_composer, state="visible")
                check(page.input_value(x_composer) == x_draft, "X keeps its draft after narrow")
                return {"after_widen": wide, "hash": ev("location.hash")}

            rep.step("phone: widen from item session Y restores session X with its draft; Y never gets X's draft", widen_item_draft)

            def end_after_navigation() -> dict:
                page.click(".bell")
                page.wait_for_selector(activity_page)
                i, _ = visible_item(unread_only=False, want="fixture-b")
                page.locator(".as-page .notice").nth(i).click()
                page.locator(item_back).wait_for(state="visible")
                held: list = []
                pattern = "**/api/sessions/fixture-b/end"
                page.route(pattern, lambda route: held.append(route))  # held, then answered here: no real End
                page.click(".activity-session:not([hidden]) button[aria-label='End session']")
                page.click(".modal button.destructive")
                for _ in range(100):
                    if held:
                        break
                    page.wait_for_timeout(50)
                check(len(held) == 1, "the End request is held")
                ev("history.back()")  # navigate while End is pending
                page.wait_for_selector(activity_page)
                held[0].fulfill(status=200, content_type="application/json", body='{"ended": true}')
                page.wait_for_selector(".modal", state="detached")
                page.wait_for_timeout(500)
                page.unroute(pattern)
                check(page.locator(activity_page).count() == 1, "the list stays after the late End answer")
                check(ev("JSON.stringify(history.state)").startswith('{"relayActivity"'), "still on the list entry")
                ev("history.back()")
                page.wait_for_selector(x_composer, state="visible")
                check(ev("location.hash") == "#/s/fixture-a", "the selection under the visit stays")
                check(page.input_value(x_composer) == x_draft, "X keeps its draft")
                return {"hash": ev("location.hash"), "end_request": "held, answered 200 by the helper after Back"}

            rep.step("phone: a late End answer for an item session does not clear the session under the visit", end_after_navigation)

            def missing_session() -> dict:
                gone = harness(control, "event", {"kind": "turn_complete", "title": "Fixture gone session",
                                                  "body": "Its session is not in the state.", "session_id": "fixture-gone", "env": "work1"})["id"]
                page.click(".bell")
                page.wait_for_selector(activity_page)
                data = sort_items(api_items()["items"])
                wait_js(page, f"document.querySelectorAll('.as-page .notice').length === {len(data)}")
                i = next(n for n, it in enumerate(data) if it["id"] == gone)
                h = ev("history.length")
                page.locator(".as-page .notice").nth(i).click()
                page.wait_for_selector(".toast-stack :text('This session is no longer available.')")
                check(page.locator(activity_page).count() == 1 and page.locator(".activity-session:not([hidden])").count() == 0,
                      "the list stays; no item view opens")
                check(ev("history.length") == h, "no entry for an absent session")
                wait_read(gone)
                # A shown item session leaves the state.
                y = scroll_list_to("fixture-c")
                i, _ = visible_item(unread_only=False, want="fixture-c")
                page.locator(".as-page .notice").nth(i).click()
                page.locator(item_back).wait_for(state="visible")
                harness(control, "session", {"id": "fixture-c", "fields": {"status": "ended", "ended_at": now - 200000}})
                absent.add("fixture-c")
                page.wait_for_selector(activity_page)
                wait_js(page, "JSON.stringify(history.state).startsWith('{\"relayActivity\"')", 5)
                wait_scroll(y)
                check(page.locator(".activity-session:not([hidden])").count() == 0, "no item view")
                check(not ev("document.body.classList.contains('chat-open')"), "the list scrolls as a page")
                # Forward into the now-stale item entry.
                ev("history.forward()")
                page.wait_for_timeout(300)
                wait_js(page, "JSON.stringify(history.state).startsWith('{\"relayActivity\"')", 5)
                page.wait_for_selector(activity_page)
                wait_scroll(y)
                check(page.locator(".activity-session:not([hidden])").count() == 0, "no blank item view after Forward")
                ev("history.back()")
                page.wait_for_selector(x_composer, state="visible")
                check(page.input_value(x_composer) == x_draft, "the view under the visit keeps X and its draft")
                return {"absent_item": gone, "list_scroll": y}

            rep.step("phone: an absent item session does not open; a vanished one and its stale entry return to the list", missing_session)

            def rapid_forward() -> dict:
                page.click(".bell")
                page.wait_for_selector(activity_page)
                y = scroll_list_to("fixture-d")
                i, item = visible_item(unread_only=False, want="fixture-d")
                page.locator(".as-page .notice").nth(i).click()
                page.locator(item_back).wait_for(state="visible")
                page.locator(item_back).click()
                page.wait_for_selector(activity_page)
                wait_scroll(y)
                ev("history.back()")
                page.wait_for_selector(x_composer, state="visible")
                pattern = "**/api/activity?*"
                results = {}
                for case in ("release while the item shows", "release after Back to the list"):
                    held: list = []
                    page.route(pattern, lambda route: held.append(route) if route.request.method == "GET" else route.continue_())
                    try:
                        results[case] = rapid_case(case, held, y)
                    finally:
                        for route in held:
                            try:
                                route.continue_()
                            except Exception:  # noqa: BLE001 - already continued
                                pass
                        page.unroute(pattern)
                return {"saved_list_scroll": y, "cases": results}

            def rapid_case(case: str, held: list, y: float) -> dict:
                # Forward to the list, Forward to its item, Back to the list, with the list GET held.
                ev("history.forward()")  # the list mounts; its GET waits
                page.wait_for_selector(activity_page)
                for _ in range(100):
                    if held:
                        break
                    page.wait_for_timeout(50)
                check(bool(held) and page.locator(".as-page .notice").count() == 0, f"{case}: the list load is held")
                ev("history.forward()")  # the item session, before the list loads
                page.locator(item_back).wait_for(state="visible")
                if case.startswith("release while"):
                    for route in held:
                        route.continue_()
                    held.clear()
                    wait_js(page, "document.querySelectorAll('.as-page .notice').length > 0")
                ev("history.back()")  # the list
                page.wait_for_selector(activity_page)
                if case.startswith("release after"):
                    for route in held:
                        route.continue_()
                    held.clear()
                wait_scroll(y)
                result = {"held_requests": "released", "list_scroll": ev("Math.round(scrollY)")}
                ev("history.back()")
                page.wait_for_selector(x_composer, state="visible")
                return result

            rep.step("phone: rapid Forward during a held list load keeps the list position", rapid_forward)

            def resize() -> dict:
                page.click(".mobile-nav button:has-text('Agents')")
                page.wait_for_selector(".intro", state="visible")
                page.click(tab)
                page.wait_for_selector(activity_page)
                page.set_viewport_size({"width": 1024, "height": 844})
                page.wait_for_selector(".intro", state="visible")
                check(page.locator(".view-activity").count() == 0, "Activity page closed on widen")
                check(page.locator("aside.notification-panel").count() == 0, "no popup after widen")
                check(page.get_attribute(".bell", "aria-expanded") == "false", "desktop bell state")
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(300)
                check(page.locator(".view-activity").count() == 0, "no Activity after narrow")
                check(page.locator(".intro").is_visible(), "Agents after narrow")
                return "widen past 760 px restores Agents"

            rep.step("phone: widening past 760 px on Activity restores the prior page", resize)

            def live_event() -> dict:
                page.click(tab)
                page.wait_for_selector(activity_page)
                count = len(api_items()["items"])
                wait_js(page, f"document.querySelectorAll('.as-page .notice').length === {count}")
                page.wait_for_timeout(1500)  # let the opening load and any throttled reload settle
                badge = int(ev("document.querySelector('.mobile-nav .nav-badge')?.textContent ?? '0'"))  # no badge at 0
                seen = len(responses)
                new_id = harness(control, "event", LIVE_EVENT)["id"]
                wait_js(page, f"document.querySelectorAll('.as-page .notice').length === {count + 1}", 10)
                body = next((b for b in (r.json() for r in reversed(responses[seen:])) if any(i["id"] == new_id for i in b["items"])), None)
                check(body is not None, f"no stream reload returned id {new_id}")
                got = next(i for i in body["items"] if i["id"] == new_id)
                for k, v in LIVE_EVENT.items():
                    check(got[k] == v, f"field {k}: {got[k]!r}")
                check(got["attention"] is True and got["read"] is False, "attention, unread")
                want = sort_items(body["items"])
                pos = [i["id"] for i in want].index(new_id)
                check(shown_titles() == [i["title"] for i in want], "order after the live update")
                check(page.locator(".as-page .notice").nth(pos).locator("strong").inner_text() == LIVE_EVENT["title"], "position")
                wait_badge(badge + 1)
                return {"id": new_id, "position": pos, "reloads_after_event": len(responses) - seen, "badge": [badge, badge + 1]}

            rep.step("phone: a neutral event through the real stream appears in place (id and fields match)", live_event)

            def overflow() -> dict:
                res = {}
                for width, height in ((390, 844), (320, 720)):
                    page.set_viewport_size({"width": width, "height": height})
                    ev("window.scrollTo(0, 0)")
                    page.wait_for_timeout(200)
                    m = ev("({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,"
                           " mark: (() => { const r = document.querySelector('.as-page .mark-all').getBoundingClientRect(); return [r.width, r.height]; })()})")
                    check(m["sw"] <= m["cw"], f"{width}px overflow {m}")
                    check(min(m["mark"]) >= 44, f"{width}px Mark all read {m['mark']}")
                    page.screenshot(path=str(shots / f"phone-{width}-activity-top.png"))
                    res[width] = m
                page.set_viewport_size({"width": 390, "height": 844})
                return res

            rep.step("phone 390/320: no horizontal scroll, Mark all read 44 px", overflow)

            def mark_all() -> dict:
                page.click(".as-page .mark-all")
                wait_js(page, "document.querySelectorAll('.as-page .notice.unread').length === 0")
                page.wait_for_selector(".mobile-nav .nav-badge", state="detached")
                page.wait_for_selector(".bell .bell-badge", state="detached")
                unread = db_one("SELECT COUNT(*) FROM activity WHERE read_at IS NULL")[0]
                check(unread == 0, f"database unread {unread}")
                check(page.locator(".as-page .mark-all").count() == 0, "Mark all read hides")
                return {"database_unread": unread}

            rep.step("phone: Mark all read clears marks, both badges, and the database read state", mark_all)

            def empty() -> str:
                harness(control, "clear")
                page.wait_for_selector(".as-page .notice-empty")
                check(page.locator(".as-page .notice").count() == 0, "no items")
                check(page.locator(".as-page .mark-all").count() == 0 and page.locator(".nav-badge").count() == 0, "no button, no badge")
                page.screenshot(path=str(shots / "phone-390-activity-empty.png"))
                return page.inner_text(".as-page .notice-empty")

            rep.step("phone: empty state", empty)
            info["page_errors"] = js_errors
            rep.step("no page errors", lambda: check(not js_errors, f"page errors: {js_errors}") or "none")
            browser.close()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rep.steps.append({"step": "harness", "result": "FAIL", "detail": rep.redact(traceback.format_exc()[-800:])})
    finally:
        cleanup: dict[str, Any] = {}
        if server is not None:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            cleanup["server_pid"] = server.pid
            cleanup["server_exit"] = server.returncode
        if "tmux" in info:
            try:
                cleanup["tmux"] = stop_tmux(info["tmux"])
            except Exception as exc:  # noqa: BLE001
                cleanup["tmux_error"] = str(exc)
        for env_id, root in roots.items():
            if root.name.startswith(".relay-fixture-act-") and root.parent == HOST_HOMES[env_id]:
                shutil.rmtree(root)
                cleanup[f"removed_{env_id}"] = str(root)
        shutil.rmtree(work, ignore_errors=True)
        cleanup["work_removed"] = not work.exists()
        log_path = out / "harness-relay.log"
        if log_path.exists():
            log_path.write_text(rep.redact(log_path.read_text()))
        cleanup["port_free_after"] = port_free(PORT)
        info["cleanup"] = cleanup
        name = "desktop-results.json" if args.desktop_only else "live-results.json"
        (out / name).write_text(rep.redact(json.dumps({"info": info, "steps": rep.steps}, indent=2, default=str)))
        print(f"{len(rep.steps) - rep.failed} passed, {rep.failed} failed")
    return 1 if rep.failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve")
    ap.add_argument("--out")
    ap.add_argument("--dist")
    ap.add_argument("--work", default="/tmp")
    ap.add_argument("--desktop-only", action="store_true", help="only the controlled desktop screenshots")
    ap.add_argument("--compare", help="folder of earlier desktop screenshots to compare pixel for pixel")
    args = ap.parse_args()
    if args.serve:
        serve(args.serve)
        return 0
    if not args.out or not args.dist:
        ap.error("--out and --dist are required")
    return run_checks(args)


if __name__ == "__main__":
    sys.exit(main())
