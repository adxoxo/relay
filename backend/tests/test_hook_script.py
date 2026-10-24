"""Tests for the standalone hook script, run as a subprocess with a tmp HOME."""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = str(Path(__file__).resolve().parents[1] / "relay" / "adapters" / "hook_script.py")


def _run(args, *, home, stdin=b"", env_extra=None):
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, HOOK, *args],
        input=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def _inbox_files(home):
    return sorted(glob.glob(str(Path(home) / ".local" / "state" / "relay" / "inbox" / "*.json")))


def _read_only_event(home):
    files = _inbox_files(home)
    assert len(files) == 1
    return json.loads(Path(files[0]).read_text())


def test_claude_stdin_mode(tmp_path):
    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": "abc",
        "transcript_path": "/home/node/.claude/projects/p/t.jsonl",
    }
    proc = _run(
        ["claude"],
        home=tmp_path,
        stdin=json.dumps(payload).encode(),
        env_extra={"RELAY_SESSION_ID": "relay1", "RELAY_ENV": "work2", "TMUX": "/s,9,0", "TMUX_PANE": "%3"},
    )
    assert proc.returncode == 0
    assert proc.stdout == b""
    event = _read_only_event(tmp_path)
    assert event["v"] == 1
    assert event["source"] == "claude"
    assert event["relay_session_id"] == "relay1"
    assert event["relay_env"] == "work2"
    assert event["tmux"] == "/s,9,0"
    assert event["tmux_pane"] == "%3"
    assert event["payload"]["hook_event_name"] == "SessionStart"
    assert isinstance(event["ts"], (int, float))


def test_codex_argv_mode(tmp_path):
    payload = {"type": "agent-turn-complete", "thread-id": "t1", "turn-id": "u1"}
    proc = _run(["codex", json.dumps(payload)], home=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout == b""
    event = _read_only_event(tmp_path)
    assert event["source"] == "codex"
    assert event["payload"]["type"] == "agent-turn-complete"
    assert event["payload"]["thread-id"] == "t1"


def test_file_mode_and_name(tmp_path):
    payload = {"hook_event_name": "Stop"}
    _run(["claude"], home=tmp_path, stdin=json.dumps(payload).encode())
    files = _inbox_files(tmp_path)
    assert len(files) == 1
    name = os.path.basename(files[0])
    # name is <time_ns>-<8 hex>.json
    import re

    assert re.match(r"^\d+-[0-9a-f]{8}\.json$", name)
    inbox = Path(tmp_path) / ".local" / "state" / "relay" / "inbox"
    assert (os.stat(inbox).st_mode & 0o777) == 0o700
    assert (os.stat(files[0]).st_mode & 0o777) == 0o600
    # no leftover tmp files
    assert not list(inbox.glob(".*"))


def test_trimming(tmp_path):
    big = "x" * 5000
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {
            "command": "npm test",
            "description": "run tests",
            "content": "c" * 500,
            "new_string": "n" * 500,
            "secret_field": "s" * 5000,
        },
        "tool_response": {"stdout": "y" * 100000},
        "last_assistant_message": "m" * 6000,
        "other": big,
    }
    _run(["claude"], home=tmp_path, stdin=json.dumps(payload).encode())
    event = _read_only_event(tmp_path)
    p = event["payload"]
    assert "tool_response" not in p
    ti = p["tool_input"]
    assert ti["command"] == "npm test"
    assert ti["description"] == "run tests"
    assert len(ti["content"]) == 200
    assert len(ti["new_string"]) == 200
    assert "secret_field" not in ti  # dropped: not in keep or preview list
    assert len(p["last_assistant_message"]) == 4000
    assert len(p["other"]) == 2000


def test_empty_stdout_on_garbage(tmp_path):
    proc = _run(["claude"], home=tmp_path, stdin=b"not json at all {{{")
    assert proc.returncode == 0
    assert proc.stdout == b""
    assert _inbox_files(tmp_path) == []


def test_unknown_source_exits_zero(tmp_path):
    proc = _run(["mystery"], home=tmp_path, stdin=b"{}")
    assert proc.returncode == 0
    assert proc.stdout == b""
    assert _inbox_files(tmp_path) == []


def test_no_args_exits_zero(tmp_path):
    proc = _run([], home=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout == b""
