"""Tests for the phone feedback fixes (2026-10-01).

- Heartbeat: the change stream and the terminal answer a browser ``ping``.
- History scroll: swipe requests become tmux copy-mode calls, coalesced, and a
  keystroke leaves copy mode before it is written.
- Failed start: the agent wrapper keeps the tmux session, and the startup
  watch reports the error line as an activity item.
- Conversation data: any Claude hook event records the transcript path.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from relay import activity, screen, sessions, terminal
from relay.adapters import claude
from relay.app import create_app
from relay.auth import Auth
from relay.errors import ApiError
from relay.tmux import scroll_args

BASE = "http://127.0.0.1:8765"


# -- heartbeat ---------------------------------------------------------------

def test_stream_answers_ping(rt):
    app = create_app(runtime=rt, features=False)
    token = Auth(rt.settings, rt.db).ensure_token()
    with TestClient(app, base_url=BASE) as client:
        client.post("/api/auth/login", json={"token": token})
        cookie = client.cookies.get("relay_session")
        with client.websocket_connect("/api/stream", headers={"Cookie": f"relay_session={cookie}"}) as ws:
            assert ws.receive_json()["type"] == "hello"
            ws.send_json({"type": "ping"})
            assert ws.receive_json() == {"type": "pong"}
            # The stream still forwards bus topics after a ping.
            client.app.state.rt.bus.publish("sessions")
            assert ws.receive_json()["type"] == "changed"


class FakeSocket:
    def __init__(self):
        self.frames: list[dict] = []

    async def send_json(self, obj):
        self.frames.append(obj)

    async def send_bytes(self, data):
        self.frames.append({"bytes": data})


class ScrollTmux:
    """Records scroll calls; ``lines`` > 0 stays in copy mode until 0 or a large negative."""

    def __init__(self, delay: float = 0.0):
        self.calls: list[int] = []
        self.delay = delay

    async def scroll(self, socket, session_id, lines):
        self.calls.append(lines)
        if self.delay:
            await asyncio.sleep(self.delay)
        return lines > 0


def _bridge(rt, tmux):
    rt.tmux["work1"] = tmux
    bridge = terminal.TerminalBridge(rt, rt.settings.env("work1"), "/tmp/tmux-1000/default", "$3", "sess")
    bridge._ws = FakeSocket()
    writes: list[bytes] = []
    bridge._write = lambda data: writes.append(data)  # type: ignore[method-assign]
    return bridge, writes


async def test_terminal_ping_gets_pong(rt):
    bridge, _ = _bridge(rt, ScrollTmux())
    await bridge._handle_json({"type": "ping"})
    assert bridge._ws.frames == [{"type": "pong"}]


async def test_terminal_ignores_non_object_json(rt):
    bridge, writes = _bridge(rt, ScrollTmux())
    await bridge._handle_json(["input", "x"])  # used to raise AttributeError and drop the socket
    assert writes == []


async def test_scroll_requests_coalesce_while_one_runs(rt):
    tmux = ScrollTmux(delay=0.05)
    bridge, _ = _bridge(rt, tmux)
    # Requests that arrive before the worker runs merge into one call.
    for _ in range(5):
        await bridge._handle_json({"type": "scroll", "lines": 3})
    await bridge._scroll_task
    assert tmux.calls == [15]
    # Requests that arrive while a call runs merge into the next call.
    await bridge._handle_json({"type": "scroll", "lines": 3})
    await asyncio.sleep(0.01)
    for _ in range(4):
        await bridge._handle_json({"type": "scroll", "lines": 3})
    await bridge._scroll_task
    assert tmux.calls == [15, 3, 12]
    assert bridge._in_scroll is True
    assert bridge._ws.frames[-1] == {"type": "scroll", "active": True}


async def test_input_leaves_copy_mode_before_it_is_written(rt):
    tmux = ScrollTmux()
    bridge, writes = _bridge(rt, tmux)
    await bridge._handle_json({"type": "scroll", "lines": 10})
    await bridge._scroll_task
    order: list[str] = []
    real_scroll = tmux.scroll

    async def scroll(socket, session_id, lines):
        order.append(f"scroll {lines}")
        return await real_scroll(socket, session_id, lines)

    tmux.scroll = scroll  # type: ignore[method-assign]
    bridge._write = lambda data: order.append(f"write {data!r}")  # type: ignore[method-assign]
    await bridge._handle_json({"type": "input", "data": "y"})
    assert order == ["scroll 0", "write b'y'"]
    assert bridge._in_scroll is False
    assert bridge._ws.frames[-1] == {"type": "scroll", "active": False}


async def test_input_without_scroll_makes_no_tmux_call(rt):
    tmux = ScrollTmux()
    bridge, writes = _bridge(rt, tmux)
    await bridge._handle_json({"type": "input", "data": "ls\r"})
    assert tmux.calls == []
    assert writes == [b"ls\r"]


def test_scroll_args_shapes():
    up = scroll_args("$3", 5)
    assert up[:4] == ["copy-mode", "-e", "-t", "$3"]
    assert "scroll-up" in up and up[-1] == "#{pane_in_mode}"
    down = scroll_args("$3", -4)
    assert down[0] == "if-shell" and "send-keys -t '$3' -X -N 4 scroll-down-and-cancel" in down
    cancel = scroll_args("$3", 0)
    assert "send-keys -t '$3' -X cancel" in cancel
    assert "-N" in scroll_args("$3", 10_000) and "500" in scroll_args("$3", 10_000)


def test_scroll_args_rejects_bad_session_id():
    with pytest.raises(Exception):
        scroll_args("$3; kill-server", 1)


# -- failed start ------------------------------------------------------------

def test_agent_wrapper_keeps_the_session_on_failure_and_shell_uses_exec():
    agent = sessions._launch_wrapper(["claude", "--name", "a"])
    assert agent[:2] == ["bash", "-lc"] and "set -m" in agent[2] and "exec bash -l" in agent[2]
    assert agent[3:] == ["relay-launch", "claude", "--name", "a"]
    shell = sessions._launch_wrapper(["bash", "-l"])
    assert shell[2] == 'umask 077; exec "$@"'


BWRAP_SCREEN = """\
error: bubblewrap is required for subprocess env scrubbing and isolation. Install with: sudo apt-get install -y bubblewrap, or set CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0 to disable (loses subprocess isolation).
      at WWr (/$bunfs/root/chunk-yrpa7y9e.js:11:20188)
      at async <anonymous> (/$bunfs/root/chunk-pf9xnj2q.js:24:6752)
Bun v1.4.3 (Linux x64)

[relay] claude exited with code 1. The terminal stays open.
[work1] ~/projects $
"""


def test_launch_failure_finds_the_error_line():
    program, code, reason = screen.launch_failure(BWRAP_SCREEN)
    assert (program, code) == ("claude", 1)
    assert reason.startswith("error: bubblewrap is required")


def test_launch_failure_none_without_marker():
    assert screen.launch_failure("Welcome to Claude Code\n❯ ") is None


class CaptureTmux:
    def __init__(self, text):
        self.text = text

    async def capture(self, socket, pane_id, *, lines=80):
        return self.text


async def test_startup_watch_reports_a_failed_start(rt):
    rt.tmux["work1"] = CaptureTmux(BWRAP_SCREEN)
    sid = rt.db.insert_session(
        dict(env="work1", kind="claude", display_name="agent4", tmux_name="agent4", origin="dashboard",
             status="live", state="unknown", socket_path="/tmp/tmux-1000/default", pane_id="%9")
    )
    await sessions.watch_startup(rt, sid, duration=1.0, interval=0.01)
    row = rt.db.get_session(sid)
    assert row["status"] == "live"  # the tmux session stays for the user to read
    assert row["state"] == "exited"
    items = activity.list_items(rt.db)
    failed = [i for i in items if i["title"] == "agent4 could not start"]
    assert failed and failed[0]["body"].startswith("error: bubblewrap is required")


# -- conversation data -------------------------------------------------------

def _claude_row(rt):
    sid = rt.db.insert_session(
        dict(env="work1", kind="claude", display_name="agent1", tmux_name="agent1", origin="discovered",
             status="live", state="unknown", socket_path="/tmp/tmux-1000/default", pane_id="%0")
    )
    return sid


def test_any_hook_event_records_transcript_path(rt):
    sid = _claude_row(rt)
    event = {"source": "claude", "ts": 200.0, "payload": {
        "hook_event_name": "PreToolUse", "session_id": "abc-123", "tool_name": "Bash",
        "transcript_path": "/home/node/.claude/projects/-home-node-projects/abc-123.jsonl",
    }}
    claude.apply_event(rt, rt.db.get_session(sid), event)
    row = rt.db.get_session(sid)
    assert row["transcript_path"].endswith("abc-123.jsonl")
    assert row["agent_session_id"] == "abc-123"
    assert row["state"] == "working"


def test_stale_event_does_not_record_transcript_path(rt):
    sid = _claude_row(rt)
    rt.db.update_session(sid, state_at=500.0)
    event = {"source": "claude", "ts": 100.0, "payload": {
        "hook_event_name": "Stop", "session_id": "old", "transcript_path": "/home/node/.claude/projects/x/old.jsonl",
    }}
    claude.apply_event(rt, rt.db.get_session(sid), event)
    assert rt.db.get_session(sid)["transcript_path"] is None
