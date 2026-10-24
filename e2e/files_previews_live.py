#!/usr/bin/env python3
"""Live check for Files and Previews on a temporary relay stack.

It never touches the running relay service, its state, or its frontend build.

What it starts (and stops at the end, by exact PID or socket):

- a temporary relay app on 127.0.0.1:18765 with its own state folder, a
  disposable token, the isolated frontend build, and ``background=False``: no
  hook provisioning, no inventory reconcile, no inbox poll, no terminal client
  cleanup. Each env gets a disposable projects root
  ``/home/node/.relay-fixture-<id>`` and an isolated previews tmux socket;
- one trusted gateway (127.0.0.1:18621 -> 127.0.0.1:18611) and a local TCP
  forwarder on 127.0.0.1:18622 that stands in for a Tailscale Serve route;
- disposable web apps in the containers on an isolated tmux socket:
  work2 127.0.0.1:18611 (host network), work1 0.0.0.0:8787 (published as
  18787) and 0.0.0.0:18612 (not published).

Then it drives Brave (Playwright) at 320, 390, 768, and 1280 px and prints
PASS or FAIL per step. Screenshots mask every preview card that is not a
fixture, so no other project shows in the evidence.

Usage:
    .venv/bin/python e2e/files_previews_live.py --out DIR --dist BUILD_DIR
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

BRAVE = "/opt/brave.com/brave/brave"
PORT = 18765
GW_PORT = 18621
ROUTE_PORT = 18622
W2_APP = 18611
W2_MANAGED = 18613
W1_PUBLISHED = 8787
W1_HOST = 18787
W1_UNPUBLISHED = 18612
TMUX_SOCKET = "/tmp/tmux-1000/relay-fp-test"
PREVIEWS_SOCKET = "/tmp/tmux-1000/relay-fp-previews"
CONTAINERS = {"work1": "job-work1", "work2": "job-work2"}
HOST_HOMES = {"work1": Path.home() / "Work" / "work1", "work2": Path.home() / "Work" / "work2"}
ASSETS = Path("/home/adyu/Work/work2/projects/outputs/2026-10-01/relay-files-previews-mockup-2026-10-01/assets")
PAGED_BYTES = 46_000_000

WEB_APP = r'''
import base64, hashlib, json, struct, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PORT = int(sys.argv[1]); BIND = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
INDEX = b"""<!doctype html><html><head><meta charset="utf-8"><title>Fixture web app</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/assets/app.css"></head>
<body><h1>Fixture web app</h1><p id="asset">asset: waiting</p><p id="ws">ws: waiting</p><pre id="echo"></pre>
<script src="/assets/app.js"></script></body></html>"""
JS = b"""document.getElementById('asset').textContent = 'asset: loaded';
fetch('/echo').then(r => r.json()).then(j => { document.getElementById('echo').textContent = JSON.stringify(j); window.__echo = j; });
const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/hmr?token=fixture', 'vite-hmr');
ws.onmessage = (e) => { const m = JSON.parse(e.data);
  if (m.type === 'connected') { window.__wsHeaders = m.headers; ws.send('ping'); }
  else if (m.type === 'echo') { document.getElementById('ws').textContent = 'ws: ' + ws.protocol + ' ' + m.data; } };
"""
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def out(self, code, body, ctype, extra=()):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
        for k, v in extra: self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(body)
    def do_HEAD(self): self.do_GET()
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/hmr" and self.headers.get("Upgrade", "").lower() == "websocket": return self.ws()
        if p == "/": return self.out(200, INDEX, "text/html; charset=utf-8", [("Set-Cookie", "app_session=fixture; Path=/; HttpOnly")])
        if p == "/assets/app.js": return self.out(200, JS, "text/javascript")
        if p == "/assets/app.css": return self.out(200, b"body{font-family:sans-serif;padding:16px}", "text/css")
        if p == "/echo": return self.out(200, json.dumps(dict(self.headers.items())).encode(), "application/json")
        if p == "/redirect": return self.out(302, b"", "text/plain", [("Location", "http://127.0.0.1:%d/landing" % PORT)])
        if p == "/setcookie": return self.out(200, b"ok", "text/plain", [("Set-Cookie", "relay_session=evil; Path=/"), ("Set-Cookie", "__Host-relay_session=evil; Path=/; Secure"), ("Set-Cookie", "app_pref=1; Path=/")])
        if p == "/landing": return self.out(200, b"landing", "text/plain")
        return self.out(404, b"not found", "text/plain")
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n)
        self.out(200, json.dumps({"body": body.decode(), "origin": self.headers.get("Origin")}).encode(), "application/json")
    def ws(self):
        accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] + GUID).encode()).digest()).decode()
        protos = [p.strip() for p in (self.headers.get("Sec-WebSocket-Protocol") or "").split(",") if p.strip()]
        self.send_response(101); self.send_header("Upgrade", "websocket"); self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        if "vite-hmr" in protos: self.send_header("Sec-WebSocket-Protocol", "vite-hmr")
        self.end_headers(); self.wfile.flush()
        sock = self.connection
        def send(text):
            data = text.encode(); hdr = bytes([0x81])
            hdr += bytes([len(data)]) if len(data) < 126 else (bytes([126]) + struct.pack(">H", len(data)) if len(data) < 65536 else bytes([127]) + struct.pack(">Q", len(data)))
            sock.sendall(hdr + data)
        send(json.dumps({"type": "connected", "headers": dict(self.headers.items())}))
        f = sock.makefile("rb")
        while True:
            h = f.read(2)
            if len(h) < 2: break
            op, ln, masked = h[0] & 15, h[1] & 127, h[1] & 128
            if ln == 126: ln = struct.unpack(">H", f.read(2))[0]
            elif ln == 127: ln = struct.unpack(">Q", f.read(8))[0]
            mask = f.read(4) if masked else b"\0\0\0\0"
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(f.read(ln)))
            if op == 8:
                try: sock.sendall(b"\x88\x00")
                except OSError: pass
                break
            if op == 9: sock.sendall(bytes([0x8A, len(data)]) + data); continue
            if op == 1: send(json.dumps({"type": "echo", "data": data.decode()}))
        self.close_connection = True
ThreadingHTTPServer.daemon_threads = True
ThreadingHTTPServer((BIND, PORT), H).serve_forever()
'''

README = """# Fixture project

This folder is a **disposable fixture** for the relay Files check.

| Bed | Crop | Sow by |
| --- | --- | --- |
| A | Herbs | April |
| B | Tomatoes | May |

- [x] Measure the beds
- [ ] Draw the path layout ([layout notes](https://example.com/layout))

> Tip: mulch keeps the soil damp for longer.

```
npm run dev
```
"""

APP_TS = """// Watering schedule for each bed.
import { beds, type Bed } from "./beds";

export interface Slot {
  bed: string;
  minutes: number; // per day
}

export function weeklySlots(list: Bed[] = beds): Slot[] {
  return list.map((bed) => ({ bed: bed.id, minutes: 12 }));
}
"""


# -- small helpers -------------------------------------------------------------

def podman(*args: str, check: bool = True, input: bytes | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if "XDG_DATA_HOME_VSCODE_SNAP_ORIG" in env:
        env["XDG_DATA_HOME"] = env["XDG_DATA_HOME_VSCODE_SNAP_ORIG"]
    return subprocess.run(
        ["podman", "--events-backend=none", *args], input=input, capture_output=True, timeout=timeout, check=check, env=env
    )


def cexec(env_id: str, *cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    return podman("exec", "--user", "1000:1000", CONTAINERS[env_id], *cmd, check=check)


def port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn does; TIME_WAIT is not "in use"
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_port(port: int, seconds: float = 20) -> bool:
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
        self.secrets: list[str] = []  # disposable test token and cookie: never written out

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
            detail = self.redact(f"{type(exc).__name__}: {exc}")[:600]
            self.steps.append({"step": name, "result": "FAIL", "detail": detail, "seconds": round(time.time() - started, 2)})
            print(f"FAIL  {name}: {detail}", flush=True)
            return None
        self.steps.append(
            {"step": name, "result": "PASS", "detail": value if isinstance(value, (str, int, float, dict, list)) else None,
             "seconds": round(time.time() - started, 2)}
        )
        print(f"PASS  {name}", flush=True)
        return value

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s["result"] == "FAIL")


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


# -- fixtures ---------------------------------------------------------------------

def make_fixtures(fixture: str) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for env_id, home in HOST_HOMES.items():
        root = home / fixture
        if root.exists():
            raise RuntimeError(f"fixture folder exists already: {root}")
        root.mkdir(mode=0o755)
        roots[env_id] = root
    w1, w2 = roots["work1"], roots["work2"]
    (w1 / "garden" / "docs").mkdir(parents=True)
    (w1 / "garden" / "src").mkdir()
    (w1 / "garden" / "logs").mkdir()
    (w1 / "garden" / "node_modules" / "pkg").mkdir(parents=True)
    (w1 / "garden" / "README.md").write_text(README)
    (w1 / "garden" / "docs" / "planting-guide.md").write_text("# Planting guide\n\n- Water in the early morning.\n- Check the soil.\n")
    os.chmod(w1 / "garden" / "docs" / "planting-guide.md", 0o640)
    (w1 / "garden" / "src" / "schedule.ts").write_text(APP_TS)
    (w1 / "garden" / "docs" / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>\n')
    (w1 / "garden" / "docs" / "photo.heic").write_bytes(b"\x00\x00\x00\x18ftypheic" + os.urandom(2000))
    (w1 / "garden" / "docs" / "archive.bin").write_bytes(b"\x00\x01binary" * 300)
    shutil.copyfile(ASSETS / "bed-layout.png", w1 / "garden" / "docs" / "bed-layout.png")
    shutil.copyfile(ASSETS / "walkthrough.webm", w1 / "garden" / "docs" / "walkthrough.webm")
    (w1 / "garden" / "node_modules" / "pkg" / "README.md").write_text("skipped by search\n")
    (w1 / "garden" / ".readme-hidden.md").write_text("hidden\n")
    line = b"2026-10-01 07:00:00 INFO  [vite] hmr update /src/schedule.ts (fixture line)\n"
    with open(w1 / "garden" / "logs" / "dev-server.log", "wb") as fh:
        n = PAGED_BYTES // len(line)
        fh.write(line * n + b"x" * (PAGED_BYTES - n * len(line) - 1) + b"\n")
    # Links resolved in the container path space.
    os.symlink("docs/planting-guide.md", w1 / "garden" / "guide-link.md")
    os.symlink(f"/home/node/{fixture}/garden/docs", w1 / "garden" / "docs-abs-link")
    os.symlink(f"../../work2/{fixture}/work2-only.txt", w1 / "garden" / "escape-relative")
    os.symlink(str(w2 / "work2-only.txt"), w1 / "garden" / "escape-host-path")
    os.symlink("/home/node/projects", w1 / "garden" / "escape-projects")
    os.mkfifo(w1 / "garden" / "pipe")
    # Work 2
    (w2 / "web" / "assets").mkdir(parents=True)
    (w2 / "web" / "server.py").write_text(WEB_APP)
    (w2 / "README.md").write_text("# Work 2 fixture\n\nThis file exists only in Work 2.\n")
    (w2 / "work2-only.txt").write_text("work2 secret fixture\n")
    # Equal relative paths in both workspaces, for the late-response checks.
    for env_id, root in (("work1", w1), ("work2", w2)):
        tag = "Work 1" if env_id == "work1" else "Work 2"
        (root / "same").mkdir()
        (root / "same" / "notes.md").write_text(f"# {tag} copy\n\nThis file exists in both workspaces.\n")
        line = f"{tag} paged line\n".encode()
        (root / "same" / "big.log").write_bytes(line * (11_000_000 // len(line) + 1))
    (w1 / "web1").mkdir()
    (w1 / "web1" / "server.py").write_text(WEB_APP)
    return roots


def start_apps(fixture: str) -> None:
    def tmux(env_id: str, name: str, cwd: str, *argv: str) -> None:
        cexec(env_id, "tmux", "-S", TMUX_SOCKET, "new-session", "-d", "-s", name, "-c", cwd, *argv)

    tmux("work2", "fp-web2", f"/home/node/{fixture}/web", "python3", "server.py", str(W2_APP), "127.0.0.1")
    tmux("work1", "fp-web1", f"/home/node/{fixture}/web1", "python3", "server.py", str(W1_PUBLISHED), "0.0.0.0")
    tmux("work1", "fp-unpub", f"/home/node/{fixture}/web1", "python3", "server.py", str(W1_UNPUBLISHED), "0.0.0.0")


def stop_apps() -> list[str]:
    done = []
    for env_id in CONTAINERS:
        for sock in (TMUX_SOCKET, PREVIEWS_SOCKET):
            res = cexec(env_id, "tmux", "-S", sock, "kill-server", check=False)
            cexec(env_id, "rm", "-f", sock, check=False)
            done.append(f"{env_id}:{sock}:{res.returncode}")
    return done


# -- the temporary relay (subprocess) ----------------------------------------------------

def serve(cfg_path: str) -> None:
    import uvicorn

    from relay.app import create_app
    from relay.config import EnvConfig, GatewayConfig, Settings
    from relay.runtime import build_runtime

    cfg = json.loads(Path(cfg_path).read_text())
    envs = {
        env_id: EnvConfig(
            id=env_id,
            label=label,
            container=CONTAINERS[env_id],
            host_home=HOST_HOMES[env_id],
            projects_root=f"/home/node/{cfg['fixture']}",
            previews_socket=PREVIEWS_SOCKET,
        )
        for env_id, label in (("work1", "Work 1"), ("work2", "Work 2"))
    }
    settings = Settings(
        state_dir=Path(cfg["state"]),
        envs=envs,
        port=PORT,
        frontend_dist=Path(cfg["dist"]),
        browser_executable=BRAVE,
        # The stand-in route is plain http on loopback. A real deployment uses
        # an https public_origin (load_settings enforces that).
        preview_gateways=(GatewayConfig(listen_port=GW_PORT, env="work2", target_port=W2_APP, public_origin=f"http://127.0.0.1:{ROUTE_PORT}"),),
    )
    rt = build_runtime(settings)
    for env_id, env in envs.items():
        rt.env_status[env_id].container = asyncio.run(rt.podman.inspect(env))
    app = create_app(runtime=rt, features=True, background=False)

    async def forward(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_r, up_w = await asyncio.open_connection("127.0.0.1", GW_PORT)
        except OSError:
            writer.close()
            return

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                dst.close()

        await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))

    class Server(uvicorn.Server):
        async def startup(self, sockets=None):  # the stand-in route starts with the app
            self._route = await asyncio.start_server(forward, "127.0.0.1", ROUTE_PORT)
            await super().startup(sockets)

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="info", proxy_headers=True)
    Server(config).run()


# -- browser steps ------------------------------------------------------------------------

def run_checks(args: argparse.Namespace) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    shots = out / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    for p in (PORT, GW_PORT, ROUTE_PORT, W2_APP, W2_MANAGED, W1_UNPUBLISHED):
        if not port_free(p):
            print(f"port {p} is in use; stop", file=sys.stderr)
            return 2
    fixture = f".relay-fixture-{secrets.token_hex(4)}"
    work = Path(args.work) / fixture
    work.mkdir(parents=True)
    state = work / "state"
    state.mkdir()
    rep = Report()
    info: dict[str, Any] = {"fixture_root": f"/home/node/{fixture}", "relay_port": PORT, "gateway": GW_PORT, "route_stand_in": ROUTE_PORT}
    server: subprocess.Popen | None = None
    roots: dict[str, Path] = {}
    base = f"http://127.0.0.1:{PORT}"

    try:
        roots = make_fixtures(fixture)
        start_apps(fixture)
        check(wait_port(W2_APP) and wait_port(W1_HOST), "fixture apps did not start")
        cfg = work / "harness.json"
        cfg.write_text(json.dumps({"fixture": fixture, "state": str(state), "dist": args.dist}))
        log = open(out / "harness-relay.log", "w")
        server = subprocess.Popen(
            [sys.executable, __file__, "--serve", str(cfg)], stdout=log, stderr=subprocess.STDOUT, cwd=str(REPO / "backend")
        )
        info["harness_pid"] = server.pid
        check(wait_port(PORT, 30) and wait_port(GW_PORT, 10) and wait_port(ROUTE_PORT, 10), "harness did not start")
        token = (state / "token").read_text().strip()
        rep.secrets.append(token)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=BRAVE, headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            phone = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
            page = phone.new_page()
            raw_responses: list[dict[str, Any]] = []
            page.on(
                "response",
                lambda r: raw_responses.append({"url": r.url.split("?")[0], "status": r.status, "range": r.headers.get("content-range")})
                if "/api/files/" in r.url and "/raw" in r.url
                else None,
            )

            def shot(name: str, target=None, mask=None) -> str:
                path = shots / f"{name}.png"
                (target or page).screenshot(path=str(path), full_page=False, mask=mask or [])
                return path.name

            def login() -> str:
                page.goto(f"{base}/#token={token}")
                page.wait_for_selector(".mobile-nav", timeout=15000)
                cookie = [c for c in phone.cookies() if c["name"] == "relay_session"]
                check(len(cookie) == 1 and not cookie[0]["secure"], "local http login must set relay_session without Secure")
                rep.secrets.append(cookie[0]["value"])
                return "local http login sets relay_session (HttpOnly, not Secure)"

            rep.step("login over local http", login)

            def files_list() -> str:
                page.click(".mobile-nav button:has-text('Files')")
                page.click(".ws-switch button:has-text('Work 1')")
                page.wait_for_selector(".entries .entry:has-text('garden')")
                page.click(".entries .entry:has-text('garden')")
                page.wait_for_selector(".entries .entry:has-text('README.md')")
                texts = page.locator(".entries .entry strong").all_inner_texts()
                check("node_modules" in texts and ".readme-hidden.md" not in texts, f"hidden names must stay hidden: {texts}")
                return shot("phone-390-files-list")

            rep.step("phone: folder list with path and hidden names off", files_list)

            def specials() -> str:
                outside = page.locator(".entries .entry:has-text('escape-relative') small").inner_text()
                host = page.locator(".entries .entry:has-text('escape-host-path') small").inner_text()
                projects = page.locator(".entries .entry:has-text('escape-projects') small").inner_text()
                fifo = page.locator(".entries .entry:has-text('pipe')")
                check("outside" in outside.lower() and "outside" in projects.lower(), f"links outside: {outside} / {projects}")
                check("broken" in host.lower() or "outside" in host.lower(), f"host-path link: {host}")
                check(fifo.is_disabled(), "a FIFO must not open")
                # The container agrees: realpath of the inside links stays under the fixture root.
                inside = cexec("work1", "realpath", "-e", "--", f"/home/node/{fixture}/garden/docs-abs-link").stdout.decode().strip()
                check(inside == f"/home/node/{fixture}/garden/docs", f"container realpath: {inside}")
                return {"escape-relative": outside, "escape-host-path": host, "escape-projects": projects}

            rep.step("phone: links outside the root and a FIFO stay closed", specials)

            def markdown() -> str:
                page.click(".entries .entry:has-text('README.md')")
                page.wait_for_selector(".md h2:has-text('Fixture project')")
                check(page.locator(".md table").count() == 1, "markdown table")
                link = page.locator(".md a.md-link")
                check(link.get_attribute("rel") == "noopener noreferrer" and link.get_attribute("target") == "_blank", "web link attrs")
                return shot("phone-markdown-preview")

            rep.step("phone: Markdown preview", markdown)

            def phone_back_clean() -> str:
                page.go_back()
                page.wait_for_selector(".entries .entry:has-text('README.md')")
                check(page.locator(".file-pane").is_hidden(), "file pane must close on Back")
                return "browser Back closes the file"

            rep.step("phone Back closes a clean file", phone_back_clean)

            guide_host = roots["work1"] / "garden" / "docs" / "planting-guide.md"

            def edit_and_save() -> str:
                page.click(".entries .entry:has-text('docs')")
                page.click(".entries .entry:has-text('planting-guide.md')")
                page.click(".fp-bar button:has-text('Edit')")
                page.fill("#file-editor", "# Planting guide\n\n- Water in the evening.\n- Check the soil.\n")
                page.wait_for_selector(".st.dirty:has-text('Unsaved changes')")
                shot("phone-editing-unsaved")
                page.click(".fp-bar button:has-text('Save')")
                page.wait_for_selector(".st.saved")
                st = guide_host.stat()
                check(guide_host.read_text().startswith("# Planting guide\n\n- Water in the evening."), "saved text on disk")
                check(oct(st.st_mode & 0o777) == "0o640" and st.st_uid == 1000, f"mode/uid kept: {oct(st.st_mode)} {st.st_uid}")
                return {"mode": oct(st.st_mode & 0o777), "uid": st.st_uid}

            rep.step("phone: explicit edit and atomic save keep mode and uid 1000", edit_and_save)

            def guard_and_back() -> str:
                page.click(".fp-bar button:has-text('Edit')")
                page.fill("#file-editor", "draft that must not be lost\n")
                page.click(".fp-head .back")
                page.wait_for_selector(".modal h2:has-text('Discard unsaved changes?')")
                shot("phone-discard-guard")
                page.click(".modal button:has-text('Keep editing')")
                check(page.input_value("#file-editor") == "draft that must not be lost\n", "draft kept")
                page.go_back()  # the phone Back gesture
                page.wait_for_selector(".modal h2:has-text('Discard unsaved changes?')")
                page.click(".modal button:has-text('Keep editing')")
                check(page.locator("#file-editor").is_visible(), "still editing after phone Back")
                page.click(".mobile-nav button:has-text('Agents')")
                page.wait_for_selector(".modal h2:has-text('Discard unsaved changes?')")
                page.click(".modal button:has-text('Keep editing')")
                check(page.locator("#file-editor").is_visible(), "still on Files after the nav guard")
                return "in-app Back, phone Back, and the bottom nav ask first"

            rep.step("unsaved guard: in-app Back, phone Back, app navigation", guard_and_back)

            def stale_save() -> str:
                before = guide_host.read_text()
                # An agent in the container writes the file while the user edits.
                cexec("work1", "sh", "-c", 'printf "# Planting guide\\n\\n- Water at noon (agent).\\n- Check the soil.\\n" > "$1"', "sh",
                      f"/home/node/{fixture}/garden/docs/planting-guide.md")
                agent_text = guide_host.read_text()
                check(agent_text != before, "agent write")
                try:  # Save now, or the 5 s change poll finds the conflict first
                    page.click(".fp-bar button:has-text('Save')", timeout=2500)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_selector(".banner.conflict:has-text('This file changed while you edited it.')", timeout=15000)
                check(guide_host.read_text() == agent_text, "a stale save must leave the file unchanged")
                page.click(".banner.conflict button:has-text('Compare')")
                page.wait_for_selector(".diff .del")
                shot("phone-conflict-compare")
                page.click(".banner.conflict button:has-text('Overwrite with mine')")
                page.wait_for_selector(".modal h2:has-text('Overwrite the newer version?')")
                page.click(".modal button:has-text('Overwrite')")
                page.wait_for_selector(".st.saved")
                check(guide_host.read_text() == "draft that must not be lost\n", "explicit overwrite saved the draft")
                return "409 kept the agent's text; explicit overwrite then saved"

            rep.step("stale save: 409, compare, explicit overwrite", stale_save)

            def paged() -> str:
                page.go_back()
                page.wait_for_selector(".entries")
                page.click(".crumbs button:has-text('garden')")
                page.click(".entries .entry:has-text('logs')")
                page.click(".entries .entry:has-text('dev-server.log')")
                page.wait_for_selector(".pager .range:has-text('Lines 1–200')", timeout=20000)
                check(page.locator(".fp-bar button:has-text('Edit')").is_disabled(), "edit off over 10 MB")
                note = page.locator(".edit-note").inner_text()
                check("10.0 MB" in note and "46.0 MB" in note, note)
                shot("phone-paged-46mb")
                page.locator(".pager:not(.bottom) button:has-text('Next')").click()
                page.wait_for_selector(".pager .range:has-text('Lines 201–400')")
                cexec("work1", "sh", "-c", 'printf "appended by an agent\\n" >> "$1"', "sh", f"/home/node/{fixture}/garden/logs/dev-server.log")
                try:  # Next now (409 from the page route), or the change poll stops paging first
                    page.locator(".pager:not(.bottom) button:has-text('Next')").click(timeout=2500)
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_selector(".banner.note:has-text('This file changed while you read it.')", timeout=15000)
                check(page.locator(".pager:not(.bottom) button:has-text('Next')").is_disabled(), "paging stops")
                shot("phone-paged-changed")
                page.click(".banner.note button:has-text('Reload this page')")
                page.wait_for_selector(".banner.note:has-text('changed while you read')", state="detached")
                page.wait_for_selector(".pager .range:has-text('Lines 201–400')")
                check(page.locator(".pager:not(.bottom) button:has-text('Next')").is_enabled(), "paging works again")
                return page.locator(".pager .range").first.inner_text()

            rep.step("paged view: 46 MB log, Next, revision change, reload", paged)

            def image() -> str:
                page.go_back()
                page.click(".crumbs button:has-text('garden')")
                page.click(".entries .entry:has-text('docs')")
                page.click(".entries .entry:has-text('bed-layout.png')")
                page.wait_for_function("() => document.querySelector('.img-stage img')?.naturalWidth > 0")
                page.click(".media-bar button:has-text('100%')")
                page.click(".media-bar button[aria-label='Zoom in']")
                label = page.locator(".zoom-label").inner_text()
                check(label == "125%", label)
                return shot("phone-image-zoom")

            rep.step("image: Fit, 100%, zoom in", image)

            def video() -> dict:
                page.go_back()
                page.click(".entries .entry:has-text('walkthrough.webm')")
                page.wait_for_function("() => document.querySelector('video')?.readyState >= 1", timeout=15000)
                preload = page.get_attribute("video", "preload")
                page.evaluate("async () => { const v = document.querySelector('video'); v.muted = true; await v.play(); }")
                page.wait_for_timeout(600)
                page.evaluate("() => { const v = document.querySelector('video'); v.currentTime = 2.0; }")
                page.wait_for_function("() => document.querySelector('video').currentTime >= 2.0", timeout=10000)
                t = page.evaluate("() => document.querySelector('video').currentTime")
                ranged = [r for r in raw_responses if r["status"] == 206 and r["url"].endswith("/raw")]
                check(preload == "metadata", f"preload={preload}")
                check(ranged, "the player must use Range (206)")
                src = page.get_attribute("video", "src")
                head_full = page.request.head(base + src)
                head_part = page.request.head(base + src, headers={"Range": "bytes=0-99"})
                check(head_full.status == 200 and head_full.body() == b"" and head_full.headers["accept-ranges"] == "bytes", "HEAD full")
                check(head_part.status == 206 and head_part.headers["content-range"].startswith("bytes 0-99/"), "HEAD range")
                shot("phone-video")
                return {"preload": preload, "current_time": round(t, 2), "range_responses": len(ranged), "example": ranged[-1]["range"]}

            rep.step("video: native player, play, seek, Range 206", video)

            def svg_and_heic() -> str:
                page.go_back()
                page.click(".entries .entry:has-text('logo.svg')")
                page.wait_for_selector(".banner.note:has-text('An SVG file can contain scripts')")
                check(page.locator(".code").count() == 1, "svg shown as text")
                shot("phone-svg-as-text")
                page.go_back()
                page.click(".entries .entry:has-text('photo.heic')")
                page.wait_for_selector(".fallback:has-text('This browser cannot show HEIC images.')", timeout=10000)
                shot("phone-unsupported-heic")
                page.go_back()
                page.click(".entries .entry:has-text('archive.bin')")
                page.wait_for_selector(".fallback:has-text('No preview for this file type.')")
                page.go_back()
                return "SVG as text, HEIC fallback, binary download only"

            rep.step("active and unsupported content", svg_and_heic)

            def search() -> dict:
                page.fill("#file-search", "read")
                page.wait_for_selector(".results .entry")
                names = page.locator(".results .entry strong").all_inner_texts()
                check(names == ["README.md"], f"names-only search skips node_modules and hidden names: {names}")
                shot("phone-files-search")
                page.click(".fb-head button:has-text('Show hidden')")
                page.wait_for_function("() => document.querySelectorAll('.results .entry').length === 2")
                hidden = page.locator(".results .entry strong").all_inner_texts()
                page.click(".fb-head button:has-text('Show hidden')")
                page.fill("#file-search", "zzz-nothing")
                page.wait_for_selector(".search-empty")
                shot("phone-files-search-empty")
                page.fill("#file-search", "")
                return {"default": names, "with_hidden": hidden}

            rep.step("search: names only, hidden toggle, empty state", search)

            def isolation() -> str:
                page.click(".ws-switch button:has-text('Work 2')")
                page.wait_for_selector(".entries .entry:has-text('work2-only.txt')")
                page.click(".entries .entry:has-text('README.md')")
                page.wait_for_selector(".md h2:has-text('Work 2 fixture')")
                shot("phone-work2-file")
                page.go_back()
                page.click(".ws-switch button:has-text('Work 1')")
                page.fill("#file-search", "work2-only")
                page.wait_for_selector(".search-empty")
                page.fill("#file-search", "")
                r = page.request.get(f"{base}/api/files/work1/text", params={"path": "garden/escape-relative"})
                check(r.status == 403 and r.json()["error"] == "outside_root", f"work1 cannot read work2: {r.status}")
                return "Work 2 files open in Work 2 only; a Work 1 link to Work 2 is refused (403 outside_root)"

            rep.step("workspace isolation", isolation)

            def hold(pattern: str) -> list:
                """Hold GET requests (reads) that match; let a save (PUT) pass."""
                held: list = []
                page.route(pattern, lambda route: held.append(route) if route.request.method == "GET" else route.continue_())
                return held

            finished: list = []
            failed_reqs: list = []
            page.on("requestfinished", lambda r: finished.append(r))
            page.on("requestfailed", lambda r: failed_reqs.append(r))

            def release(pattern: str, held: list) -> str:
                """Let the held requests go on. Return what reached the page: a late
                response ("late response delivered") or a cancelled request ("aborted")."""
                for route in held:
                    try:
                        route.continue_()
                    except Exception:  # noqa: BLE001 - already settled
                        pass
                page.unroute(pattern)
                page.wait_for_timeout(1500)
                outcome = []
                for route in held:
                    req = route.request
                    if any(r is req for r in finished):
                        outcome.append("late response delivered")
                    elif any(r is req for r in failed_reqs):
                        outcome.append("aborted by the page")
                    else:
                        outcome.append("unknown")
                return ", ".join(outcome)

            def wait_held(held: list) -> None:
                for _ in range(100):
                    if held:
                        return
                    page.wait_for_timeout(50)
                raise AssertionError("the request was not held")

            def open_same(ws: str, name: str) -> None:
                page.click(f".ws-switch button:has-text('{ws}')")
                page.click(".crumbs button:has-text('projects')")
                page.click(".entries .entry:has-text('same')")
                page.click(f".entries .entry:has-text('{name}')")

            def late_text() -> dict:
                pattern = "**/api/files/work1/text?**"
                held = hold(pattern)
                open_same("Work 1", "notes.md")
                wait_held(held)
                page.go_back()  # leave the file while its text is still in flight
                open_same("Work 2", "notes.md")
                page.wait_for_selector(".md h2:has-text('Work 2 copy')")
                released = release(pattern, held)
                heading = page.locator(".md h2").inner_text()
                where = page.locator(".fp-title small").inner_text()
                check(heading == "Work 2 copy" and where.startswith("Work 2"), f"late Work 1 text replaced the view: {heading} / {where}")
                shot("phone-late-text-equal-path")
                page.go_back()
                return {"held": len(held), "late_request": released, "shown": heading, "where": where}

            rep.step("late text response cannot cross a workspace switch (equal paths)", late_text)

            def late_page_equal_path() -> dict:
                open_same("Work 1", "big.log")
                page.wait_for_selector(".pager .range:has-text('Lines 1–200')")
                pattern = "**/api/files/work1/page?**"
                held = hold(pattern)
                page.locator(".pager:not(.bottom) button:has-text('Next')").click()
                wait_held(held)
                page.go_back()
                open_same("Work 2", "big.log")
                page.wait_for_selector(".code:has-text('Work 2 paged line')")
                released = release(pattern, held)
                code = page.locator(".code").first.inner_text()
                rng = page.locator(".pager .range").first.inner_text()
                check("Work 1" not in code and "Lines 1–200" in rng, f"late Work 1 page replaced the view: {rng}")
                check(page.locator(".pager:not(.bottom) button:has-text('Next')").is_enabled(), "paging not stuck")
                shot("phone-late-page-equal-path")
                page.go_back()
                return {"held": len(held), "late_request": released, "range": rng.split("\n")[0]}

            rep.step("late page response cannot cross a workspace switch (equal paths)", late_page_equal_path)

            def late_page_other_file() -> dict:
                open_same("Work 1", "big.log")
                page.wait_for_selector(".pager .range:has-text('Lines 1–200')")
                pattern = "**/api/files/work1/page?**"
                held = hold(pattern)
                page.locator(".pager:not(.bottom) button:has-text('Next')").click()
                wait_held(held)
                page.go_back()
                page.click(".entries .entry:has-text('notes.md')")
                page.wait_for_selector(".md h2:has-text('Work 1 copy')")
                released = release(pattern, held)
                check(page.locator(".pager").count() == 0 and page.locator(".md h2").inner_text() == "Work 1 copy",
                      "a late page replaced another file")
                page.go_back()
                page.click(".crumbs button:has-text('projects')")
                return {"held": len(held), "late_request": released}

            rep.step("late page response cannot replace another file", late_page_other_file)

            def late_conflict_after_discard() -> dict:
                page.click(".ws-switch button:has-text('Work 1')")
                page.click(".crumbs button:has-text('projects')")
                page.click(".entries .entry:has-text('garden')")
                page.click(".entries .entry:has-text('docs')")
                page.click(".entries .entry:has-text('planting-guide.md')")
                page.click(".fp-bar button:has-text('Edit')")
                page.fill("#file-editor", "a draft that the user discards\n")
                cexec("work1", "sh", "-c", 'printf "# Planting guide\\n\\n- Agent text, second write.\\n" > "$1"', "sh",
                      f"/home/node/{fixture}/garden/docs/planting-guide.md")
                agent_text = guide_host.read_text()
                pattern = "**/api/files/work1/text?**"
                held = hold(pattern)
                page.click(".fp-bar button:has-text('Save')")  # 409, then the conflict read is held
                wait_held(held)
                page.click(".fp-bar button:has-text('Cancel')")
                page.click(".modal button:has-text('Discard changes')")
                released = release(pattern, held)
                check(page.locator(".banner.conflict").count() == 0, "a late conflict read armed a finished edit")
                check(page.locator("button:has-text('Overwrite with mine')").count() == 0, "Overwrite offered without an edit")
                check(guide_host.read_text() == agent_text, "the agent's text must stay")
                shot("phone-late-conflict-after-discard")
                page.go_back()
                page.click(".crumbs button:has-text('projects')")
                return {"held": len(held), "late_request": released, "disk_unchanged": True}

            rep.step("late conflict read after Discard cannot arm Overwrite", late_conflict_after_discard)

            def error_state() -> str:
                victim = roots["work1"] / "garden" / "src" / "schedule.ts"
                page.fill("#file-search", "schedule")
                page.wait_for_selector(".results .entry:has-text('schedule.ts')")
                victim.rename(victim.with_name("schedule.moved"))
                page.click(".results .entry:has-text('schedule.ts')")
                page.wait_for_selector(".banner.conflict:has-text('relay cannot open this file.')")
                shot("phone-open-error")
                victim.with_name("schedule.moved").rename(victim)
                page.click(".banner.conflict button:has-text('Try again')")
                page.wait_for_selector(".code .tok-k")
                shot("phone-code-view")
                page.go_back()
                page.fill("#file-search", "")
                return "a missing file shows an error with Try again; the code view highlights"

            rep.step("error state and code view", error_state)

            # -- Previews -----------------------------------------------------------------
            def previews_detected() -> dict:
                page.click(".mobile-nav button:has-text('Previews')")
                page.wait_for_selector(".pv-card", timeout=30000)
                data = page.request.get(f"{base}/api/previews").json()
                fixture_cards = [d for d in data["detected"] if (d.get("project") or "").startswith(("web", "web1"))]
                keys = {d["key"]: d for d in fixture_cards}
                check("work2:18611" in keys and keys["work2:18611"]["project"] == "web", f"work2 app: {list(keys)}")
                check("work1:18787" in keys and keys["work1:18787"]["reachable"], "work1 published app")
                check("work1:c18612" in keys and keys["work1:c18612"]["phone"]["status"] == "blocked", "work1 unpublished")
                check(keys["work2:18611"]["phone"]["status"] == "verified", f"stand-in route proof: {keys['work2:18611']['phone']}")
                all_keys = [d["key"] for d in data["detected"]]
                check(f"work2:{PORT}" not in all_keys and "work2:8765" not in all_keys, "relay itself must not show")
                card = page.locator(".pv-card", has_text="Not registered").filter(has_text="18611")
                check(card.locator("button:has-text('Stop')").count() == 0 and card.locator("button:has-text('Remove')").count() == 0,
                      "an unregistered process has no Stop and no Remove")
                others = page.locator(".pv-card").filter(has_not_text=f"{W2_APP}").filter(has_not_text=f"{W1_PUBLISHED}").filter(has_not_text=f"{W1_UNPUBLISHED}")
                shot("phone-previews-head", mask=[others])
                for name, port in (("work2-app", W2_APP), ("work1-published", W1_PUBLISHED), ("work1-unpublished", W1_UNPUBLISHED)):
                    page.locator(".pv-card").filter(has_text=str(port)).first.screenshot(path=str(shots / f"phone-card-{name}.png"))
                info["scan"] = {
                    "envs": data["scan"]["envs"],
                    "docker": data["scan"]["docker"],
                    "fixture_endpoints": sorted(keys),
                    "other_endpoints_count": len(all_keys) - len(keys),
                }
                return {"fixture": sorted(keys), "others_masked": len(all_keys) - len(keys)}

            rep.step("previews: real container discovery lists the fixture apps under the right workspace", previews_detected)

            def blocked_card() -> str:
                card = page.locator(".pv-card").filter(has_text=str(W1_UNPUBLISHED))
                card.scroll_into_view_if_needed()
                check(card.locator(".access.blocked:has-text('Not reachable from the laptop')").count() == 1, "blocked state")
                others = page.locator(".pv-card").filter(has_not_text=str(W1_UNPUBLISHED))
                return shot("phone-previews-work1-unpublished", mask=[others])

            rep.step("previews: Work 1 unpublished port is not reachable", blocked_card)

            def gateway_flow() -> dict:
                route = f"http://127.0.0.1:{ROUTE_PORT}"
                card = page.locator(".pv-card").filter(has_text=str(W2_APP)).filter(has_text="Not registered")
                link = card.locator("a:has-text('Open on phone')")
                check(link.get_attribute("href") == f"{route}/", f"open link: {link.get_attribute('href')}")
                app = phone.new_page()
                app.goto(f"{route}/")
                app.wait_for_selector("#asset:has-text('asset: loaded')")
                app.wait_for_selector("#ws:has-text('ws: vite-hmr ping')", timeout=10000)
                app.wait_for_function("() => window.__echo && window.__wsHeaders", timeout=10000)
                echo = app.evaluate("() => window.__echo")
                ws_headers = app.evaluate("() => window.__wsHeaders")
                for hdrs, where in ((echo, "http"), (ws_headers, "websocket")):
                    low = {k.lower(): v for k, v in hdrs.items()}
                    check("relay_session" not in low.get("cookie", ""), f"{where}: relay cookie reached the app")
                    check("authorization" not in low, f"{where}: Authorization reached the app")
                    check(not [k for k in low if k.startswith("tailscale-") or k.startswith("x-forwarded-")], f"{where}: identity headers")
                check("app_session=fixture" in {k.lower(): v for k, v in echo.items()}.get("cookie", ""), "the app's own cookie passes")
                app.screenshot(path=str(shots / "gateway-app-page.png"))
                redirect = app.request.get(f"{route}/redirect", max_redirects=0)
                check(redirect.headers["location"] == f"{route}/landing", f"redirect: {redirect.headers.get('location')}")
                set_cookies = [h["value"] for h in app.request.get(f"{route}/setcookie").headers_array if h["name"].lower() == "set-cookie"]
                check(set_cookies == ["app_pref=1; Path=/"], f"reserved cookies dropped: {set_cookies}")
                # The gateway origin cannot change relay state.
                app.evaluate(f"() => fetch('{base}/api/auth/logout', {{method: 'POST', credentials: 'include'}}).catch(() => null)")
                app.wait_for_timeout(500)
                check(page.request.get(f"{base}/api/auth/me").status == 200, "the relay session survived the cross-origin logout")
                app.close()
                anon = browser.new_context()
                r = anon.request.get(f"{route}/")
                check(r.status == 401, f"no relay session: {r.status}")
                anon.close()
                return {"echo_cookie": {k.lower(): v for k, v in echo.items()}.get("cookie"), "ws_protocol": "vite-hmr", "redirect": redirect.headers["location"]}

            rep.step("gateway: assets, redirect, cookies, HMR WebSocket, no relay credentials", gateway_flow)

            def register_and_check() -> dict:
                card = page.locator(".pv-card").filter(has_text=str(W2_APP)).filter(has_text="Not registered")
                card.locator("button:has-text('Register preview')").click()
                page.wait_for_selector(".modal h2:has-text('Register a preview')")
                prefilled = (page.input_value("#pv-port"), page.input_value("#pv-project"), page.input_value("#pv-env"))
                check(prefilled == (str(W2_APP), "web", "work2"), f"prefilled: {prefilled}")
                page.fill("#pv-name", "fixture web")
                page.fill("#pv-command", f"python3 server.py {W2_APP}")
                page.fill("#pv-test_command", "python3 -c \"print('fixture tests ok')\"")
                page.click(".modal button:has-text('Register preview')")
                page.wait_for_selector(".pv-card:has-text('Registered as “fixture web”')")
                reg = page.locator(".pv-card").filter(has_text="Registered as “fixture web”")
                check(reg.locator("button:has-text('Stop')").count() == 0, "relay did not start it: no Stop")
                pid = next(p for p in page.request.get(f"{base}/api/previews").json()["previews"] if p["name"] == "fixture web")["id"]
                stop = page.request.post(f"{base}/api/previews/{pid}/stop", headers={"Origin": base})
                check(stop.status == 409 and stop.json()["error"] == "not_managed", f"stop: {stop.status}")
                reg.locator("button:has-text('Run check')").click()
                deadline = time.time() + 90
                run = None
                while time.time() < deadline:
                    runs = page.request.get(f"{base}/api/checks", params={"preview_id": pid}).json()["runs"]
                    run = runs[0] if runs else None
                    if run and run["status"] != "running":
                        break
                    time.sleep(1)
                check(run is not None and run["status"] == "passed", f"check: {run and run['status']} {run and run['summary']}")
                check(run["target_url"] == f"http://127.0.0.1:{ROUTE_PORT}/", f"check target: {run['target_url']}")
                reg.locator("button:has-text('Run tests')").click()
                deadline = time.time() + 60
                while time.time() < deadline:
                    runs = page.request.get(f"{base}/api/checks", params={"preview_id": pid}).json()["runs"]
                    if runs and runs[0]["kind"] == "test_command" and runs[0]["status"] != "running":
                        break
                    time.sleep(1)
                check(runs[0]["status"] == "passed", f"tests: {runs[0]['status']}")
                page.wait_for_timeout(1500)
                others = page.locator(".pv-card").filter(has_not_text="fixture web")
                shot("phone-previews-registered-check", mask=[others])
                return {"check_target": run["target_url"], "check": run["status"], "tests": runs[0]["status"], "stop": stop.json()["error"]}

            rep.step("register a detected app; Run check uses the verified gateway URL; Run tests; Stop keeps 409", register_and_check)

            def managed() -> dict:
                body = {"env": "work2", "name": "fixture managed", "project": "web", "command": f"python3 -m http.server {W2_MANAGED} --bind 127.0.0.1", "port": W2_MANAGED}
                created = page.request.post(f"{base}/api/previews", data=json.dumps(body), headers={"Origin": base, "Content-Type": "application/json"})
                check(created.status == 201, f"create: {created.status}")
                page.reload()
                page.wait_for_selector(".mobile-nav")
                page.click(".mobile-nav button:has-text('Previews')")
                page.wait_for_selector(".pv-card:has-text('fixture managed')", timeout=30000)
                card = page.locator(".pv-card").filter(has_text="fixture managed")
                card.locator("button:has-text('Start')").click()
                page.wait_for_selector(".pv-card:has-text('fixture managed') .src:has-text('Managed')", timeout=40000)
                card = page.locator(".pv-card").filter(has_text="fixture managed")
                check(card.locator("button:has-text('Remove')").is_disabled(), "Remove waits for Stop")
                card.locator("button:has-text('Stop')").click()
                page.wait_for_selector(".pv-card:has-text('fixture managed') button:has-text('Start')", timeout=20000)
                return "Start and Stop work for a managed preview on the isolated previews socket"

            rep.step("managed preview: Start and Stop stay", managed)

            # -- layout checks ----------------------------------------------------------------
            def overflow_and_targets() -> dict:
                results = {}
                for width, height, mobile in ((320, 720, True), (390, 844, True), (768, 1024, True), (1280, 900, False)):
                    ctx = browser.new_context(viewport={"width": width, "height": height}, is_mobile=mobile, has_touch=mobile)
                    ctx.add_cookies([{"name": "relay_session", "value": next(c["value"] for c in phone.cookies() if c["name"] == "relay_session"), "url": base}])
                    pg = ctx.new_page()
                    pg.goto(base + "/")
                    nav = ".mobile-nav button:has-text('Files')" if width < 760 else ".rail-button[aria-label='Files']"
                    pg.click(nav)
                    pg.click(".ws-switch button:has-text('Work 1')")
                    if width < 760:
                        pg.click(".entries .entry:has-text('garden')")
                        pg.click(".entries .entry:has-text('README.md')")
                    else:
                        pg.click(".tree .node:has-text('garden')")
                        pg.click(".tree .node:has-text('README.md')")
                    pg.wait_for_selector(".md h2")
                    pg.screenshot(path=str(shots / f"w{width}-files-markdown.png"))
                    measures = {}
                    for view in ("files", "previews"):
                        if view == "previews":
                            pg.click(".mobile-nav button:has-text('Previews')" if width < 760 else ".rail-button[aria-label='Previews']")
                            pg.wait_for_selector(".pv-card", timeout=30000)
                        m = pg.evaluate("() => ({sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth})")
                        check(m["sw"] <= m["cw"], f"{width}px {view}: page overflow {m}")
                        measures[view] = m
                        if width < 760:
                            small = pg.evaluate(
                                """() => [...document.querySelectorAll('.view-files .btn, .view-files .entry, .view-files .seg button, .fp-head .back,'
                                  + ' .view-previews .btn, .view-previews .seg button, .access summary, .mobile-nav button')]
                                  .filter(e => e.offsetParent !== null)
                                  .map(e => ({t: (e.innerText || e.getAttribute('aria-label') || '').trim().slice(0, 30), h: e.getBoundingClientRect().height}))
                                  .filter(x => x.h < 44)"""
                            )
                            check(not small, f"{width}px {view}: targets under 44 px: {small[:5]}")
                    fixture_card = pg.locator(".pv-card").filter(has_text="fixture web").first
                    fixture_card.scroll_into_view_if_needed()
                    others = pg.locator(".pv-card").filter(has_not_text="fixture")
                    pg.screenshot(path=str(shots / f"w{width}-previews.png"), mask=[others])
                    results[width] = measures
                    ctx.close()
                return results

            rep.step("no page overflow at 320/390/768/1280; phone targets at least 44 px", overflow_and_targets)

            def keyboard() -> str:
                ctx = browser.new_context(viewport={"width": 1280, "height": 900})
                ctx.add_cookies([{"name": "relay_session", "value": next(c["value"] for c in phone.cookies() if c["name"] == "relay_session"), "url": base}])
                pg = ctx.new_page()
                pg.goto(base + "/")
                pg.click(".rail-button[aria-label='Files']")
                pg.wait_for_selector(".tree .node")
                pg.focus("#file-search")
                for _ in range(8):
                    pg.keyboard.press("Tab")
                    if pg.evaluate("() => document.activeElement?.classList.contains('node')"):
                        break
                check(pg.evaluate("() => document.activeElement?.classList.contains('node')"), "Tab reaches the tree")
                outline = pg.evaluate("() => getComputedStyle(document.activeElement).outlineStyle")
                pg.keyboard.press("Enter")
                pg.wait_for_selector(".tree .node[aria-expanded='true']")
                ctx.close()
                return f"Tab reaches the tree, focus outline {outline}, Enter expands a folder"

            rep.step("keyboard focus on the desktop tree", keyboard)
            browser.close()
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rep.steps.append({"step": "harness", "result": "FAIL", "detail": traceback.format_exc()[-800:]})
    finally:
        cleanup = {}
        if server is not None:
            server.send_signal(signal.SIGTERM)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
            cleanup["harness_exit"] = server.returncode
        try:
            cleanup["tmux"] = stop_apps()
        except Exception as exc:  # noqa: BLE001
            cleanup["tmux_error"] = str(exc)
        for env_id, root in roots.items():
            if root.name.startswith(".relay-fixture-") and root.parent == HOST_HOMES[env_id]:
                shutil.rmtree(root)
                cleanup[f"removed_{env_id}"] = str(root)
        if (state / "artifacts").is_dir():
            shutil.copytree(state / "artifacts", out / "check-artifacts", dirs_exist_ok=True)
        shutil.rmtree(work, ignore_errors=True)
        cleanup["ports_free_after"] = {p: port_free(p) for p in (PORT, GW_PORT, ROUTE_PORT, W2_APP, W2_MANAGED)}
        info["cleanup"] = cleanup
        (out / "live-results.json").write_text(rep.redact(json.dumps({"info": info, "steps": rep.steps}, indent=2, default=str)))
        print(f"{len(rep.steps) - rep.failed} passed, {rep.failed} failed")
    return 1 if rep.failed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve")
    ap.add_argument("--out", default=str(REPO / "e2e" / "artifacts" / "files-previews"))
    ap.add_argument("--dist", default=str(REPO / "frontend" / "dist"))
    ap.add_argument("--work", default="/tmp")
    args = ap.parse_args()
    if args.serve:
        serve(args.serve)
        return 0
    return run_checks(args)


if __name__ == "__main__":
    sys.exit(main())
