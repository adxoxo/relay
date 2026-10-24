#!/usr/bin/env python3
"""Live end-to-end check for a running relay service.

This script drives a real browser (Brave, Chromium-compatible) against a
running relay service. It logs in through the UI, creates Shell sessions in
both environments, uses the terminal, and ends the sessions. It prints a
PASS or FAIL line per step and exits non-zero on any failure.

Do not run this against a service that is not up. The service and the frontend
build must both be present. This script only uses Shell sessions. It never
starts an agent (claude or codex) and always ends the sessions it created.

Usage:
    python e2e/live_e2e.py [--base-url URL] [--token TOKEN] [--out DIR]

Defaults:
    --base-url  http://127.0.0.1:8765
    --token     read from the relay token file (settings.token_path)
    --out       e2e/artifacts
"""

from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable

# The CLI package lives under backend/. Add it to the path so we can read the
# token location from settings without duplicating the logic.
REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

BROWSER_EXECUTABLE = "/opt/brave.com/brave/brave"
BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
DEFAULT_BASE_URL = "http://127.0.0.1:8765"


class StepError(AssertionError):
    """A named step failed."""


class Reporter:
    """Prints PASS or FAIL per step and tracks the overall result."""

    def __init__(self) -> None:
        self.failed = 0
        self.passed = 0

    def run(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            value = fn()
        except Exception as exc:  # noqa: BLE001 - report and continue where possible
            self.failed += 1
            print(f"FAIL  {name}: {exc}")
            raise StepError(name) from exc
        self.passed += 1
        print(f"PASS  {name}")
        return value

    def summary(self) -> int:
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} steps passed, {self.failed} failed")
        return 0 if self.failed == 0 else 1


# -- token discovery ---------------------------------------------------------

def read_default_token() -> str:
    from relay.config import load_settings

    settings = load_settings()
    path = settings.token_path
    if not path.exists():
        raise SystemExit(f"No token at {path}. Pass --token or start the service once.")
    token = path.read_text().strip()
    if not token:
        raise SystemExit(f"Token file {path} is empty. Run 'relay token --rotate'.")
    return token


# -- API helper (cleanup and assertions) -------------------------------------

class Api:
    """Bearer-token REST helper for setup checks and finally-block cleanup."""

    def __init__(self, base_url: str, token: str):
        import httpx

        self._client = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )

    def state(self) -> dict[str, Any]:
        resp = self._client.get("/api/state")
        resp.raise_for_status()
        return resp.json()

    def live_sessions(self, env: str, name: str) -> list[dict[str, Any]]:
        data = self.state()
        return [
            s for s in data.get("sessions", [])
            if s.get("env") == env and s.get("name") == name and s.get("status") == "live"
        ]

    def end_session(self, session: dict[str, Any]) -> None:
        tmux = session.get("tmux") or {}
        body = {
            "tmux_session_id": tmux.get("session_id"),
            "tmux_created": tmux.get("created"),
            "pane_count": tmux.get("pane_count"),
        }
        try:
            self._client.post(f"/api/sessions/{session.get('id')}/end", json=body)
        except Exception:  # noqa: BLE001 - best effort cleanup
            pass

    def end_all(self, env: str, name: str) -> None:
        for session in self.live_sessions(env, name):
            self.end_session(session)

    def close(self) -> None:
        self._client.close()


# -- UI helpers --------------------------------------------------------------

# Selectors follow the built frontend (frontend/src):
# - a session card is a button named "<Env label>, <name>, <status>"
# - workspace tabs and the Conversation/Terminal switch are toggle buttons
# - dialogs have role="dialog"; the New session form uses #new-work, #new-kind, #new-name

ENV_LABELS = {"work1": "Work 1", "work2": "Work 2"}


def login_via_hash(page: Any, base_url: str, token: str) -> None:
    """Load the app with the token in the fragment. The Login screen auto-submits."""
    page.goto(f"{base_url}/#token={token}")
    page.wait_for_selector(".agent-list", timeout=20000)
    if "token=" in page.url:
        raise StepError("the token fragment is still in the URL after login")


def choose_workspace(page: Any, label: str) -> None:
    page.locator(".workspace-tabs button", has_text=label).first.click()


def session_card(page: Any, env_label: str, name: str) -> Any:
    import re

    pattern = re.compile(rf"^{re.escape(env_label)}, {re.escape(name)}, ")
    return page.get_by_role("button", name=pattern)


def assert_env_visible(page: Any, label: str) -> None:
    # The tabs render from /api/state, which can arrive after the list shell.
    tab = page.locator(".workspace-tabs button", has_text=label).first
    try:
        tab.wait_for(timeout=10000)
    except Exception as exc:  # noqa: BLE001 - report as a step failure
        raise StepError(f"no workspace tab for {label}") from exc


def assert_session_listed(page: Any, env_label: str, name: str) -> None:
    choose_workspace(page, "All agents")
    session_card(page, env_label, name).first.wait_for(timeout=10000)


def open_new_session_dialog(page: Any) -> Any:
    page.get_by_role("button", name="New session").first.click()
    dialog = page.get_by_role("dialog")
    dialog.wait_for(timeout=5000)
    return dialog


def create_shell_session(page: Any, env_id: str, name: str) -> None:
    """Create a Shell session through the dialog and wait until it is selected."""
    dialog = open_new_session_dialog(page)
    page.locator("#new-work").select_option(env_id)
    page.locator("#new-kind").select_option("shell")
    page.locator("#new-name").fill(name)
    dialog.get_by_role("button", name="Start session").click()
    dialog.wait_for(state="detached", timeout=20000)
    heading = page.locator(".conversation h2").first
    deadline = time.time() + 10
    while time.time() < deadline:
        text = heading.inner_text()
        if name in text and ENV_LABELS[env_id] in text:
            return
        time.sleep(0.25)
    raise StepError(f"the new session {name} in {ENV_LABELS[env_id]} was not selected")


def select_session(page: Any, env_id: str, name: str) -> None:
    choose_workspace(page, "All agents")
    session_card(page, ENV_LABELS[env_id], name).first.click()


def open_terminal_tab(page: Any, env_id: str, name: str) -> None:
    select_session(page, env_id, name)
    page.locator(".chat-tabs button", has_text="Terminal").click()
    page.wait_for_selector(".xterm-rows", timeout=15000)


def type_in_terminal(page: Any, text: str) -> None:
    """Type into the xterm and press Enter."""
    page.locator(".xterm").first.click()
    page.keyboard.type(text, delay=15)
    page.keyboard.press("Enter")


def terminal_text(page: Any) -> str:
    """Return the current xterm DOM text (the DOM renderer fills .xterm-rows)."""
    return page.locator(".xterm-rows").first.inner_text()


def wait_for_terminal_text(page: Any, needle: str, timeout_ms: int = 12000) -> bool:
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        if needle in terminal_text(page):
            return True
        time.sleep(0.25)
    return False


def end_session_via_ui(page: Any, env_id: str, name: str) -> None:
    """Select the session, click End session, confirm in the dialog."""
    select_session(page, env_id, name)
    page.locator(".chat-actions").get_by_role("button", name="End session").click()
    dialog = page.get_by_role("dialog")
    dialog.wait_for(timeout=5000)
    dialog.get_by_role("button", name="End session", exact=True).click()
    dialog.wait_for(state="detached", timeout=20000)


# -- the run -----------------------------------------------------------------

def run(base_url: str, token: str, out_dir: Path, *, restart_cmd: str | None = None) -> int:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    reporter = Reporter()
    name = f"e2e-{secrets.token_hex(2)}"
    api = Api(base_url, token)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=BROWSER_EXECUTABLE, args=BROWSER_ARGS)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        try:
            reporter.run("login with the token link", lambda: login_via_hash(page, base_url, token))

            def envs_present() -> None:
                assert_env_visible(page, "Work 1")
                assert_env_visible(page, "Work 2")
            reporter.run("both environments visible", envs_present)

            def agents_listed() -> None:
                assert_session_listed(page, "Work 1", "agent1")
                assert_session_listed(page, "Work 2", "agent1")
            reporter.run("agent1 listed in Work 1 and Work 2", agents_listed)

            reporter.run(f"create Shell {name} in Work 1", lambda: create_shell_session(page, "work1", name))
            reporter.run(f"create Shell {name} in Work 2", lambda: create_shell_session(page, "work2", name))

            def same_name_distinct() -> None:
                w1 = api.live_sessions("work1", name)
                w2 = api.live_sessions("work2", name)
                if len(w1) != 1 or len(w2) != 1 or w1[0]["id"] == w2[0]["id"]:
                    raise StepError(f"expected one live {name} per env with distinct ids, got {len(w1)} and {len(w2)}")
            reporter.run("same name in both envs has distinct ids", same_name_distinct)

            def check_env(env_id: str) -> None:
                open_terminal_tab(page, env_id, name)
                type_in_terminal(page, 'echo "env=$WORK_ENV-$((6*7))"')
                if not wait_for_terminal_text(page, f"env={env_id}-42"):
                    raise StepError(f"terminal did not show env={env_id}-42")
            reporter.run("Work 1 terminal routes to work1", lambda: check_env("work1"))
            reporter.run("Work 2 terminal routes to work2", lambda: check_env("work2"))
            page.screenshot(path=str(out_dir / "terminal-desktop.png"))

            def survives_reload() -> None:
                page.reload()
                page.wait_for_selector(".agent-list", timeout=20000)
                open_terminal_tab(page, "work1", name)
                if not wait_for_terminal_text(page, "env=work1-42"):
                    raise StepError("earlier terminal output is gone after a browser reload")
            reporter.run("session survives a browser reload", survives_reload)

            if restart_cmd:
                def survives_service_restart() -> None:
                    import subprocess

                    subprocess.run(restart_cmd, shell=True, check=True)  # noqa: S602 - operator-provided
                    deadline = time.time() + 40
                    while time.time() < deadline:
                        try:
                            if api.live_sessions("work2", name):
                                break
                        except Exception:  # noqa: BLE001 - service still restarting
                            pass
                        time.sleep(1)
                    else:
                        raise StepError("the session is not live after the service restart")
                    page.wait_for_selector(".xterm-rows", timeout=20000)
                    open_terminal_tab(page, "work2", name)
                    type_in_terminal(page, 'echo "after-restart-$((7*6))"')
                    if not wait_for_terminal_text(page, "after-restart-42", timeout_ms=20000):
                        raise StepError("the terminal did not reconnect after the service restart")
                reporter.run("session and terminal survive a relay restart", survives_service_restart)

            def duplicate_rejected() -> None:
                dialog = open_new_session_dialog(page)
                page.locator("#new-work").select_option("work1")
                page.locator("#new-kind").select_option("shell")
                page.locator("#new-name").fill(name.upper())
                dialog.get_by_role("button", name="Start session").click()
                page.locator("#new-name-error").wait_for(timeout=8000)
                if len(api.live_sessions("work1", name)) != 1:
                    raise StepError("a duplicate session was created")
                dialog.get_by_role("button", name="Cancel").click()
                dialog.wait_for(state="detached", timeout=5000)
            reporter.run("duplicate name (other case) shows an inline error", duplicate_rejected)

            def end_both() -> None:
                end_session_via_ui(page, "work1", name)
                end_session_via_ui(page, "work2", name)
                deadline = time.time() + 10
                while time.time() < deadline:
                    if not api.live_sessions("work1", name) and not api.live_sessions("work2", name):
                        return
                    time.sleep(0.5)
                raise StepError("a session is still live after End")
            reporter.run("end both sessions through the dialog", end_both)

            def phone_layout() -> None:
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(600)
                scroll_w = page.evaluate("document.documentElement.scrollWidth")
                if scroll_w > 391:
                    raise StepError(f"horizontal overflow at 390 px: scrollWidth={scroll_w}")
                page.screenshot(path=str(out_dir / "phone-agents.png"), full_page=True)
                session_card(page, "Work 1", "agent1").first.click()
                page.get_by_role("button", name="Back to agents").wait_for(timeout=5000)
                if page.evaluate("document.documentElement.scrollWidth") > 391:
                    raise StepError("horizontal overflow in the phone session view")
                page.screenshot(path=str(out_dir / "phone-session.png"), full_page=True)
                page.get_by_role("button", name="Back to agents").click()
            reporter.run("phone layout has no horizontal overflow", phone_layout)

            def no_page_errors() -> None:
                if page_errors:
                    raise StepError(f"JavaScript errors: {page_errors[:3]}")
            reporter.run("no JavaScript errors", no_page_errors)

            page.set_viewport_size({"width": 1440, "height": 1000})
            page.screenshot(path=str(out_dir / "final-desktop.png"), full_page=True)
        except StepError:
            try:
                page.screenshot(path=str(out_dir / "failure.png"), full_page=True)
            except Exception:  # noqa: BLE001
                pass
        finally:
            # Always end the created sessions through the API.
            for env_id in ("work1", "work2"):
                try:
                    api.end_all(env_id, name)
                except Exception:  # noqa: BLE001 - best effort cleanup
                    pass
            context.close()
            browser.close()

    api.close()
    return reporter.summary()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live end-to-end check for a running relay service.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Service base URL.")
    parser.add_argument("--token", default=None, help="Access token (default: read from the token file).")
    parser.add_argument(
        "--restart-cmd",
        default=None,
        help="Optional shell command that restarts the service (tests reconnect after a restart).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "e2e" / "artifacts",
        help="Screenshot output directory.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    token = args.token if args.token else read_default_token()
    return run(args.base_url, token, args.out, restart_cmd=args.restart_cmd)


if __name__ == "__main__":
    raise SystemExit(main())
