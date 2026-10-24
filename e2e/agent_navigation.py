#!/usr/bin/env python3
"""Workspace navigation checks against the built frontend and neutral fixtures.

No real API, agent, terminal or credential is accessed. Reuses the existing
terminal fixture so checks include actual React navigation and input frames.
"""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect
from terminal_workspace import Fixture, _session, PENDING, ORIGIN, BRAVE, ARGS, wait_open


class NavigationFixture(Fixture):
    def _http(self, route):
        path = urlsplit(route.request.url).path
        meta = {"path": "notes.txt", "name": "notes.txt", "size": 14, "mtime": 100,
                "revision": "fixture-1", "writable": True, "hard_links": 1, "kind": "text",
                "lang": None, "mode": "edit", "mime": "text/plain", "active": False,
                "limits": {"edit": 10000000, "paged": 50000000}}
        body = None
        if path.endswith("/projects"):
            body = {"projects": []}
        elif path.startswith("/api/files/"):
            if path.endswith("/list"):
                body = {"path": "", "root": "/home/node/projects", "entries": [{"name": "notes.txt", "path": "notes.txt", "type": "file", "kind": "text", "link": False, "size": 14, "mtime": 100}], "truncated": False}
            elif path.endswith("/meta"):
                body = meta
            elif path.endswith("/text"):
                body = dict(meta, text="Fixture notes\n", invalid_utf8=False, line_ending="lf", editable=True, edit_blocked=None)
        elif path == "/api/activity" and route.request.method == "GET":
            body = {"items": [{"id": 1, "kind": "finished", "title": "Ended session review", "body": None,
                              "session_id": "work1-ended", "env": "work1", "attention": False,
                              "read": True, "created_at": 100}], "unread": 0}
        elif path == "/api/sessions" and route.request.method == "POST":
            req = route.request.post_data_json
            body = _session("created-from-directory", req.get("name", "new-agent"), req["kind"], "idle", env=req["env"])
            self.state["sessions"].append(body)
            self.posts.append((path, route.request.post_data))
        if body is not None:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        else:
            super()._http(route)


def fixture(dist):
    fx = NavigationFixture(dist)
    rows = []
    for env, count in [("work1", 6), ("work2", 3)]:
        for n in range(1, count + 1):
            pending = dict(PENDING, id=f"request-{env}") if n == 2 else None
            row = _session(f"{env}-{n}", f"agent{n}", "claude", "needs_input" if pending else "working", env=env, pending=pending)
            row.update(project=f"personal-projects/{env}-project-{n}", project_dir=f"/home/node/projects/{env}-project-{n}", last_message=f"Review the {env} project {n} changes.")
            rows.append(row)
    # Discovered laptop sessions can have no known project or working directory.
    rows[-1].update(project=None, project_dir=None)
    old = _session("work1-ended", "ended-agent", "claude", "exited", live=False, env="work1")
    fx.state = dict(fx.state, sessions=rows + [old], counts={"sessions": 9, "working": 7, "needs_input": 2, "finished_unread": 0, "by_env": {"work1": 6, "work2": 3}})
    return fx


def run(dist, out):
    passed = []
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=BRAVE, args=ARGS)
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, screen={"width": 390, "height": 844}, is_mobile=True, has_touch=True, device_scale_factor=2)
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        fx = fixture(dist)
        fx.install(page, screen=(390, 844))
        page.goto(ORIGIN)
        expect(page.locator(".workspace-session-group")).to_have_count(2)
        page.locator(".agent-card", has_text="Work 1").filter(has_text="agent1").tap()
        wait_open(page)
        expect(page.locator(".session-switch .switch-chip:not(.switch-new)")).to_have_count(6)
        expect(page.locator(".switch-chip.current .switch-project")).to_have_text("work1-project-1")
        expect(page.locator(".session-workspaces .workspace-pill[aria-pressed='true']")).to_contain_text("Work 1")
        expect(page.locator(".session-workspaces .workspace-pill", has_text="Work 2").locator(".attention-count")).to_have_text("1")
        page.locator(".session-workspaces .directory-button").tap()
        directory = page.get_by_role("dialog", name="All agents")
        directory.get_by_label("Search agents, projects or workspaces").fill("work2 agent3")
        expect(directory.locator(".directory-project")).to_have_text("Claude · projects")
        page.keyboard.press("Escape")
        passed.append("Phone overview groups both workspaces; terminal tabs show only the selected workspace and project subtitles, with other-workspace requests visible.")

        page.locator(".term-compose-field").fill("draft one, work1 agent1")
        page.locator(".session-workspaces .workspace-pill", has_text="Work 2").tap()
        wait_open(page)
        expect(page.locator(".chat-title h2")).to_contain_text("Work 2")
        expect(page.locator(".term-compose-field")).to_have_value("")
        page.locator(".term-compose-field").fill("draft two, work2 agent1")
        page.locator(".switch-chip", has_text="agent3").tap()
        wait_open(page)
        page.locator(".session-workspaces .workspace-pill", has_text="Work 1").tap()
        wait_open(page)
        expect(page.locator(".term-compose-field")).to_have_value("draft one, work1 agent1")
        page.locator(".session-workspaces .workspace-pill", has_text="Work 2").tap()
        wait_open(page)
        expect(page.locator(".switch-chip.current")).to_contain_text("agent3")
        passed.append("Workspace switches restore the last exact session; duplicate agent names retain independent drafts.")

        directory_button = page.locator(".session-workspaces .directory-button")
        directory_button.tap()
        dialog = page.get_by_role("dialog", name="All agents")
        expect(dialog).to_be_visible()
        expect(dialog.locator(".directory-group")).to_have_count(2)
        assert page.evaluate("document.activeElement.tagName") == "BUTTON"
        expect(dialog.locator(".directory-session")).to_have_count(9)
        dialog.get_by_label("Search agents, projects or workspaces").fill("work1 agent1")
        expect(dialog.locator(".directory-session")).to_have_count(1)
        dialog.locator(".directory-session").tap()
        wait_open(page)
        expect(dialog).not_to_be_visible()
        expect(page.locator(".term-compose-field")).to_have_value("draft one, work1 agent1")
        expect(page.locator(".session-workspaces .workspace-pill[aria-pressed='true']")).to_contain_text("Work 1")
        assert not any(fx.frames(s["id"]) for s in fx.state["sessions"])
        passed.append("Directory opens without a keyboard; workspace-aware search selects the right session and draft, with no terminal input from navigation.")

        directory_button.tap()
        dialog.get_by_role("button", name="Needs you").tap()
        expect(dialog.locator(".directory-session")).to_have_count(2)
        expect(dialog.locator(".directory-group")).to_have_count(2)
        dialog.get_by_label("Search agents, projects or workspaces").fill("no match")
        expect(dialog.get_by_role("status")).to_have_text("No agents match your search.")
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(directory_button).to_be_focused()
        passed.append("Needs you spans both workspaces; empty search, Escape and focus return work.")

        # Verify stale remembered identities fall back to a live session.
        page.evaluate("localStorage.setItem('relay.lastSessionByWorkspace', JSON.stringify({work2:'missing-session'}))")
        page.locator(".session-workspaces .workspace-pill", has_text="Work 2").tap()
        wait_open(page)
        expect(page.locator(".switch-chip.current")).to_contain_text("agent1")
        expect(page.locator(".term-compose-field")).to_have_value("draft two, work2 agent1")
        page.reload()
        wait_open(page)
        expect(page.locator(".term-compose-field")).to_have_value("draft two, work2 agent1")
        passed.append("A missing remembered session falls back safely; session drafts also survive reload.")

        # A direct link to an ended session remains identifiable and read-only.
        page.goto(ORIGIN + "/#/s/work1-ended")
        expect(page.locator(".saved-transcript")).to_be_visible()
        expect(page.locator(".switch-chip.current")).to_contain_text("ended-agent")
        expect(page.locator(".session-workspaces .workspace-pill[aria-pressed='true']")).to_contain_text("Work 1")
        passed.append("An ended session opened by link stays in the right workspace and shows its saved transcript.")

        # Empty and unavailable target workspaces never show another workspace's terminal.
        fx.state["sessions"] = [s for s in fx.state["sessions"] if s["env"] == "work1"]
        fx.state["envs"] = [dict(e, available=False, status="stopped") if e["id"] == "work2" else e for e in fx.state["envs"]]
        page.reload()
        expect(page.locator(".saved-transcript")).to_be_visible()
        page.locator(".session-workspaces .workspace-pill", has_text="Work 2").tap()
        expect(page.locator(".env-unavailable")).to_contain_text("Work 2 is unavailable")
        expect(page.locator(".session-pane")).to_have_count(0)
        passed.append("An empty unavailable workspace opens its explicit overview state instead of leaving an unrelated terminal underneath.")
        ctx.close()

        # Activity must use its displayed ended item, rather than the selection beneath it.
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, screen={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        page = ctx.new_page()
        activity_fx = fixture(dist)
        activity_fx.install(page, screen=(390, 844))
        page.goto(ORIGIN + "/#/s/work2-1")
        wait_open(page)
        page.get_by_label("Back to agents", exact=True).tap()
        page.locator(".mobile-nav button", has_text="Activity").tap()
        page.locator(".view-activity .notice").tap()
        expect(page.locator(".activity-session:not([hidden]) .saved-transcript")).to_be_visible()
        expect(page.locator(".activity-session:not([hidden]) .switch-chip.current")).to_contain_text("ended-agent")
        page.locator(".activity-session:not([hidden]) .directory-button").tap()
        expect(page.locator(".directory-session.selected")).to_contain_text("ended-agent")
        page.keyboard.press("Escape")
        page.get_by_label("Back to Activity", exact=True).tap()
        expect(page.locator(".view-activity:not([hidden])")).to_be_visible()
        passed.append("Activity's ended session keeps its own workspace, switcher and directory selection; Back returns to Activity.")
        ctx.close()

        # New session from the directory must honor dirty Files before leaving.
        ctx = browser.new_context(viewport={"width": 1440, "height": 960})
        page = ctx.new_page()
        files_fx = fixture(dist)
        files_fx.install(page)
        page.goto(ORIGIN)
        page.get_by_role("button", name="Files", exact=True).first.click()
        page.locator(".node", has_text="notes.txt").click()
        page.get_by_role("button", name="Edit", exact=True).click()
        page.get_by_label("File text of notes.txt").fill("Unsaved fixture changes")
        page.locator(".top-right .directory-button").click()
        page.locator(".agent-directory .directory-footer button").click()
        expect(page.get_by_role("dialog", name="Discard unsaved changes?")).to_be_visible()
        page.get_by_role("dialog", name="Discard unsaved changes?").locator("button.btn", has_text="Keep editing").click()
        expect(page.get_by_label("File text of notes.txt")).to_have_value("Unsaved fixture changes")
        expect(page.get_by_role("dialog", name="A fresh session.")).not_to_be_visible()
        assert not any(path == "/api/sessions" for path, _ in files_fx.posts)
        page.locator(".top-right .directory-button").click()
        page.locator(".agent-directory .directory-footer button").click()
        page.get_by_role("button", name="Discard changes", exact=True).click()
        expect(page.get_by_role("dialog", name="A fresh session.")).to_be_visible()
        page.locator("#new-kind").select_option("shell")
        page.locator("#new-name").fill("directory-created")
        page.get_by_role("button", name="Start session", exact=True).click()
        expect(page.locator(".session-pane .chat-title h2")).to_contain_text("directory-created")
        expect(page.locator(".terminal-host .xterm")).to_be_visible()
        passed.append("Directory New session respects Keep editing and Discard in Files, then opens the created session on Agents.")
        ctx.close()

        # Screenshots and geometry at phone, tablet and desktop widths.
        for width, height, touch in [(320, 740, True), (390, 844, True), (412, 915, True), (800, 1280, True), (1440, 960, False)]:
            ctx = browser.new_context(viewport={"width": width, "height": height}, screen={"width": width, "height": height}, is_mobile=touch, has_touch=touch)
            page = ctx.new_page()
            current = fixture(dist)
            current.install(page, screen=(width, height))
            page.goto(ORIGIN + "/#/s/work1-1")
            page.wait_for_selector(".terminal-host .xterm")
            page.wait_for_function("document.fonts.status === 'loaded'")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow at {width}"
            page.screenshot(path=str(out / f"terminal-{width}.png"))
            opener = page.locator(".session-workspaces .directory-button") if width <= 760 else page.locator(".top-right .directory-button")
            opener.click()
            expect(page.get_by_role("dialog", name="All agents")).to_be_visible()
            page.wait_for_function("getComputedStyle(document.querySelector('.agent-directory')).opacity === '1'")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            box = page.locator(".agent-directory").bounding_box()
            assert box["y"] >= 0 and box["y"] + box["height"] <= height + 1
            page.screenshot(path=str(out / f"directory-{width}.png"))
            page.keyboard.press("Escape")
            if width == 1440:
                page.locator(".terminal-host").click()
                before = {s["id"]: list(current.frames(s["id"])) for s in current.state["sessions"]}
                page.keyboard.press("Control+k")
                expect(page.get_by_role("dialog", name="All agents")).to_be_visible()
                assert "pointer-open" not in page.locator(".agent-directory").get_attribute("class")
                assert all(current.frames(sid) == frames for sid, frames in before.items()), "Directory shortcut sent terminal input"
                page.keyboard.press("Escape")
            ctx.close()
        passed.append("Terminal and directory fit 320, 390, 412, 800 and 1440 pixel viewports; keyboard directory shortcut opens without animation.")
        assert not errors, errors
        browser.close()
    result = {"scope": "Built frontend, neutral HTTP/WebSocket fixtures", "passed": passed, "errors": errors}
    (out / "navigation-results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    run(args.dist.resolve(), args.out.resolve())
