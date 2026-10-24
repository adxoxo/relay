"""Tests for the inbox polling loop."""

from __future__ import annotations

import json
import os

import pytest

from relay import inbox


def _inbox_dir(rt, env_id="work1"):
    env = rt.settings.env(env_id)
    d = env.host_inbox_dir
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_event(directory, name, event):
    path = directory / name
    path.write_text(json.dumps(event))
    return path


def _live_claude_session(rt, env="work1"):
    return rt.db.insert_session(
        dict(
            env=env,
            kind="claude",
            display_name="a",
            tmux_name="a",
            origin="dashboard",
            status="live",
            socket_path="/tmp/tmux-1000/default",
            pane_id="%1",
        )
    )


@pytest.mark.asyncio
async def test_valid_event_processed_and_deleted(rt):
    sid = _live_claude_session(rt)
    d = _inbox_dir(rt)
    event = {
        "v": 1,
        "source": "claude",
        "ts": 100.0,
        "relay_session_id": sid,
        "relay_env": "work1",
        "payload": {"hook_event_name": "SessionStart", "session_id": "agent-x"},
    }
    _write_event(d, "1000-aabbccdd.json", event)
    handled = await inbox.process_once(rt, "work1")
    assert handled == 1
    assert list(d.iterdir()) == []
    assert rt.db.get_session(sid)["agent_session_id"] == "agent-x"


@pytest.mark.asyncio
async def test_bad_names_skipped(rt):
    d = _inbox_dir(rt)
    _write_event(d, "not-an-event.json", {"v": 1})
    _write_event(d, "1000-XYZ.json", {"v": 1})
    _write_event(d, "1000-aabbccdd.txt", {"v": 1})
    handled = await inbox.process_once(rt, "work1")
    assert handled == 0
    # invalid names are left untouched (they never matched)
    assert len(list(d.iterdir())) == 3


@pytest.mark.asyncio
async def test_symlink_refused_and_deleted(rt):
    d = _inbox_dir(rt)
    target = d.parent / "target.json"
    target.write_text(json.dumps({"v": 1, "source": "claude", "payload": {}}))
    link = d / "1000-deadbeef.json"
    os.symlink(target, link)
    handled = await inbox.process_once(rt, "work1")
    # a symlink is deleted without counting as handled
    assert not link.exists()
    assert target.exists()
    assert handled == 0


@pytest.mark.asyncio
async def test_oversize_deleted(rt):
    d = _inbox_dir(rt)
    path = d / "1000-cafebabe.json"
    path.write_bytes(b"x" * (256 * 1024 + 10))
    handled = await inbox.process_once(rt, "work1")
    assert not path.exists()
    assert handled == 1


@pytest.mark.asyncio
async def test_invalid_json_deleted(rt):
    d = _inbox_dir(rt)
    path = d / "1001-aabbccdd.json"
    path.write_text("{ not json")
    handled = await inbox.process_once(rt, "work1")
    assert not path.exists()
    assert handled == 1


@pytest.mark.asyncio
async def test_name_order(rt):
    sid = _live_claude_session(rt)
    d = _inbox_dir(rt)
    # working then idle: after processing in order the final state is idle
    _write_event(d, "1000-00000001.json", {
        "v": 1, "source": "claude", "ts": 10.0, "relay_session_id": sid,
        "payload": {"hook_event_name": "UserPromptSubmit"},
    })
    _write_event(d, "2000-00000002.json", {
        "v": 1, "source": "claude", "ts": 20.0, "relay_session_id": sid,
        "payload": {"hook_event_name": "Stop", "last_assistant_message": "done"},
    })
    handled = await inbox.process_once(rt, "work1")
    assert handled == 2
    row = rt.db.get_session(sid)
    assert row["state"] == "idle"
    assert row["last_message"] == "done"


@pytest.mark.asyncio
async def test_unresolved_event_still_deleted(rt):
    d = _inbox_dir(rt)
    _write_event(d, "1000-11223344.json", {
        "v": 1, "source": "claude", "ts": 10.0, "relay_session_id": "missing",
        "payload": {"hook_event_name": "Stop"},
    })
    handled = await inbox.process_once(rt, "work1")
    assert handled == 1
    assert list(d.iterdir()) == []
    # a hook_events row is still stored with a null session
    rows = rt.db.query("SELECT * FROM hook_events")
    assert len(rows) == 1
    assert rows[0]["session_id"] is None
