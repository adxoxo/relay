#!/usr/bin/env python3
"""Private HTTPS proof for workspace navigation using only task-owned shells.

Reads the existing token privately. Never sends input to an existing agent.
Creates one uniquely named shell per available workspace and ends only those
exact new IDs with their tmux identity. No service or container restart.
"""
import argparse
import json
import re
import secrets
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect
from live_e2e import read_default_token, BROWSER_EXECUTABLE, BROWSER_ARGS


def main(base, out):
    out.mkdir(parents=True, exist_ok=True)
    token = read_default_token()
    client = httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    created, checks, cleanup = [], [], []
    name = "relay-ui-proof-" + secrets.token_hex(4)
    report = {"url": base, "scope": "Real private HTTPS UI and task-owned shells", "checks": checks, "cleanup": cleanup}
    try:
        state = client.get("/api/state").raise_for_status().json()
        envs = [env for env in state["envs"] if env["available"]]
        assert {env["id"] for env in envs} >= {"work1", "work2"}, "Both workspaces must be available for this proof"
        for env in envs:
            response = client.post("/api/sessions", json={"env": env["id"], "kind": "shell", "name": name})
            response.raise_for_status()
            created.append(response.json())
        by_env = {s["env"]: s for s in created}
        ids = {s["id"] for s in created}
        remembered = {env: row["id"] for env, row in by_env.items()}
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=BROWSER_EXECUTABLE, args=BROWSER_ARGS)
            context = browser.new_context(viewport={"width": 390, "height": 844}, screen={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            context.add_init_script("localStorage.setItem('relay.lastSessionByWorkspace', " + json.dumps(json.dumps(remembered)) + ")")
            response = context.request.post(base + "/api/auth/login", data={"token": token}, headers={"Origin": base})
            assert response.ok, f"Login failed with HTTP {response.status}"
            page = context.new_page()
            output = {sid: [] for sid in ids}

            def socket_open(ws):
                match = re.search(r"/api/sessions/([^/]+)/terminal", ws.url)
                if not match or match.group(1) not in ids:
                    return
                sid = match.group(1)
                ws.on("framereceived", lambda data: output[sid].append(data.decode("utf-8", "replace") if isinstance(data, bytes) else data))

            page.on("websocket", socket_open)
            page.goto(base + "/#/s/" + by_env["work1"]["id"])
            expect(page.locator(".session-workspaces .workspace-pill[aria-pressed='true']")).to_contain_text("Work 1")
            expect(page.locator(".switch-chip.current")).to_contain_text(name)
            checks.append("Private HTTPS login opens the exact task-owned Work 1 shell, with workspace and scoped tab identity.")

            def send_marker(env):
                sid = by_env[env]["id"]
                assert page.url.endswith("#/s/" + sid), "Refuse to send input to an unowned session"
                marker = "RELAY_UI_" + env.upper() + "_" + secrets.token_hex(4)
                page.locator(".term-compose-field").fill("printf '%s\\n' '" + marker + "'")
                expect(page.locator(".term-compose-send")).to_be_enabled(timeout=20000)
                page.locator(".term-compose-send").tap()
                expect(page.locator(".term-compose-field")).to_have_value("", timeout=10000)
                # A standalone output line, rather than just the echoed command.
                deadline = __import__("time").monotonic() + 15
                while __import__("time").monotonic() < deadline:
                    raw = "".join(output[sid])
                    # tmux inserts ANSI cursor and bracketed-paste controls
                    # around Bash output; compare logical text lines.
                    clean = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", raw)
                    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", clean).replace("\r", "")
                    if marker in [line.strip() for line in clean.split("\n")]:
                        checks.append(f"Send reaches real {env} shell output on this browser's terminal WebSocket.")
                        return
                    page.wait_for_timeout(100)
                report["output_diagnostics"] = {"env": env, "frames": len(output[sid]), "bytes": len(raw),
                    "marker_lines": [line[-160:] for line in clean.split("\n") if marker in line]}
                page.screenshot(path=str(out / "failed-marker.png"), mask=[page.locator(".switch-chip:not(.current)")])
                raise AssertionError(f"No execution output marker returned from {env}")

            send_marker("work1")
            page.locator(".term-compose-field").fill("Unsent Work 1 proof draft")
            page.locator(".session-workspaces .workspace-pill", has_text="Work 2").tap()
            expect(page.locator(".chat-title h2")).to_contain_text("Work 2")
            assert page.url.endswith("#/s/" + by_env["work2"]["id"])
            expect(page.locator(".term-compose-field")).to_have_value("")
            send_marker("work2")
            page.locator(".term-compose-field").fill("Unsent Work 2 proof draft")
            page.locator(".session-workspaces .directory-button").tap()
            directory = page.get_by_role("dialog", name="All agents")
            expect(directory).to_be_visible()
            directory.get_by_label("Search agents, projects or workspaces").fill(name)
            expect(directory.locator(".directory-session")).to_have_count(2)
            expect(directory.locator(".directory-group")).to_have_count(2)
            directory.get_by_role("button", name=re.compile(r"^Open Work 1,")).click()
            expect(directory).not_to_be_visible()
            assert page.url.endswith("#/s/" + by_env["work1"]["id"])
            expect(page.locator(".term-compose-field")).to_have_value("Unsent Work 1 proof draft")
            checks.append("Grouped directory distinguishes identical names across real workspaces and restores the correct unsent draft.")
            page.locator(".session-workspaces .workspace-pill", has_text="Work 2").tap()
            expect(page.locator(".term-compose-field")).to_have_value("Unsent Work 2 proof draft")
            page.reload()
            expect(page.locator(".term-compose-field")).to_have_value("Unsent Work 2 proof draft")
            assert page.url.endswith("#/s/" + by_env["work2"]["id"])
            checks.append("Workspace memory and per-session drafts survive real navigation and a reload.")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(out / "private-phone.png"), mask=[page.locator(".switch-chip:not(.current)")])
            browser.close()
        report["passed"] = True
    finally:
        for session in created:
            tmux = session["tmux"]
            response = client.post(f"/api/sessions/{session['id']}/end", json={"tmux_session_id": tmux["session_id"], "tmux_created": tmux["created"], "pane_count": tmux["pane_count"]})
            cleanup.append({"session_id": session["id"], "env": session["env"], "end_http_status": response.status_code})
        remaining = client.get("/api/state").raise_for_status().json()["sessions"]
        live_owned = [s["id"] for s in remaining if s["id"] in {x["id"] for x in created} and s["status"] == "live"]
        report["owned_live_remaining"] = live_owned
        (out / "live-results.json").write_text(json.dumps(report, indent=2) + "\n")
        client.close()
        assert not live_owned, "Task-owned shells remain live"
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    main(args.base_url.rstrip("/"), args.out)
