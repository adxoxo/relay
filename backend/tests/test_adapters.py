"""Tests for the agent adapters, event dispatch, approvals, and hooks_install."""

from __future__ import annotations

import json

import pytest

from relay import hooks_install
from relay.adapters import claude, codex, handle_event
from relay.db import loads
from relay.errors import ApiError


# -- helpers -----------------------------------------------------------------

class FakeTmux:
    """A tmux double that returns a scripted capture and records send_keys."""

    def __init__(self, screen: str = ""):
        self.screen = screen
        self.sent: list[tuple] = []

    async def capture(self, socket, pane_id, *, lines=80):
        return self.screen

    async def send_keys(self, socket, pane_id, *keys):
        self.sent.append((socket, pane_id, keys))


def _install_tmux(rt, env_id, tmux):
    rt.tmux[env_id] = tmux


def _session(rt, *, env="work1", kind="claude", state="idle", socket="/tmp/tmux-1000/default",
             pane_id="%1", server_pid=9, pending=None, state_at=None):
    fields = dict(
        env=env, kind=kind, display_name="a", tmux_name="a", origin="dashboard",
        status="live", socket_path=socket, pane_id=pane_id, server_pid=server_pid,
        state=state,
    )
    if pending is not None:
        fields["pending_json"] = json.dumps(pending)
    if state_at is not None:
        fields["state_at"] = state_at
    return rt.db.insert_session(fields)


def _claude_event(name, *, ts=100.0, session_id=None, tmux=None, tmux_pane=None, **payload):
    payload["hook_event_name"] = name
    return {
        "v": 1, "source": "claude", "ts": ts,
        "relay_session_id": session_id, "tmux": tmux, "tmux_pane": tmux_pane,
        "payload": payload,
    }


def _row(rt, sid):
    return rt.db.get_session(sid)


# -- state machine -----------------------------------------------------------

def test_session_start_sets_idle_and_metadata(rt):
    sid = _session(rt, state="unknown")
    event = _claude_event("SessionStart", transcript_path="/home/node/.claude/projects/p/t.jsonl")
    event["payload"]["session_id"] = "agent-1"
    claude.apply_event(rt, _row(rt, sid), event)
    row = _row(rt, sid)
    assert row["state"] == "idle"
    assert row["state_source"] == "hook"
    assert row["agent_session_id"] == "agent-1"
    assert row["transcript_path"] == "/home/node/.claude/projects/p/t.jsonl"


def test_session_start_clears_a_folder_trust_request(rt):
    sid = _session(rt, state="needs_input")
    rt.db.update_session(sid, state_source="screen", pending_json='{"id":"t1","source":"screen","answerable":false}')
    claude.apply_event(rt, _row(rt, sid), _claude_event("SessionStart"))
    row = _row(rt, sid)
    assert row["pending_json"] is None
    assert row["state"] == "idle"


def test_user_prompt_submit_sets_working_and_clears_pending(rt):
    sid = _session(rt, state="needs_input", pending={"id": "x", "match": ["a"]})
    claude.apply_event(rt, _row(rt, sid), _claude_event("UserPromptSubmit", ts=200.0))
    row = _row(rt, sid)
    assert row["state"] == "working"
    assert row["pending_json"] is None


def test_pretooluse_working_unless_needs_input(rt):
    sid = _session(rt, state="idle")
    claude.apply_event(rt, _row(rt, sid), _claude_event("PreToolUse", tool_name="Bash"))
    assert _row(rt, sid)["state"] == "working"

    sid2 = _session(rt, state="needs_input", pending={"id": "y", "match": ["a"]})
    claude.apply_event(rt, _row(rt, sid2), _claude_event("PreToolUse", ts=200.0, tool_name="Bash"))
    assert _row(rt, sid2)["state"] == "needs_input"


def test_permission_request_sets_pending_and_attention(rt):
    sid = _session(rt, state="working")
    claude.apply_event(rt, _row(rt, sid), _claude_event(
        "PermissionRequest", ts=150.0, tool_name="Bash",
        tool_input={"command": "npm run test", "description": "run tests"}))
    row = _row(rt, sid)
    assert row["state"] == "needs_input"
    pending = loads(row["pending_json"])
    assert pending["tool"] == "Bash"
    assert pending["summary"] == "npm run test"
    assert pending["detail"] == "run tests"
    assert pending["match"] == ["npm run test"]
    assert pending["answerable"] is True
    items = rt.db.query("SELECT * FROM activity WHERE kind='needs_input'")
    assert len(items) == 1
    assert items[0]["attention"] == 1


def test_posttooluse_edit_activity(rt):
    sid = _session(rt, state="working")
    claude.apply_event(rt, _row(rt, sid), _claude_event(
        "PostToolUse", ts=160.0, tool_name="Edit",
        tool_input={"file_path": "/home/node/projects/app/src/main.py"}))
    row = _row(rt, sid)
    assert row["state"] == "working"
    assert row["pending_json"] is None
    items = rt.db.query("SELECT * FROM activity WHERE kind='tool'")
    assert any(i["title"] == "Edited app/src/main.py" for i in items)


def test_posttoolusefailure_error_activity(rt):
    sid = _session(rt, state="working")
    claude.apply_event(rt, _row(rt, sid), _claude_event(
        "PostToolUseFailure", ts=160.0, tool_name="Bash", tool_input={"command": "x"}))
    assert _row(rt, sid)["state"] == "working"
    items = rt.db.query("SELECT * FROM activity WHERE kind='error'")
    assert len(items) == 1


def test_notification_permission_prompt_creates_pending(rt):
    sid = _session(rt, state="working")
    claude.apply_event(rt, _row(rt, sid), _claude_event(
        "Notification", ts=150.0, notification_type="permission_prompt", message="need perm"))
    row = _row(rt, sid)
    assert row["state"] == "needs_input"
    pending = loads(row["pending_json"])
    assert pending["answerable"] is False


def test_notification_keeps_existing_pending(rt):
    sid = _session(rt, state="needs_input", pending={"id": "keep", "match": ["a"], "answerable": True})
    claude.apply_event(rt, _row(rt, sid), _claude_event(
        "Notification", ts=200.0, notification_type="permission_prompt", message="need perm"))
    pending = loads(_row(rt, sid)["pending_json"])
    assert pending["id"] == "keep"


def test_notification_idle_prompt(rt):
    sid = _session(rt, state="working")
    claude.apply_event(rt, _row(rt, sid), _claude_event(
        "Notification", ts=150.0, notification_type="idle_prompt"))
    assert _row(rt, sid)["state"] == "idle"


def test_stop_sets_idle_and_turn_complete(rt):
    sid = _session(rt, state="working")
    claude.apply_event(rt, _row(rt, sid), _claude_event(
        "Stop", ts=170.0, last_assistant_message="all done", prompt_id="p1"))
    row = _row(rt, sid)
    assert row["state"] == "idle"
    assert row["last_message"] == "all done"
    assert row["pending_json"] is None
    items = rt.db.query("SELECT * FROM activity WHERE kind='turn_complete'")
    assert len(items) == 1


def test_stop_dedupe_by_prompt_id(rt):
    sid = _session(rt, state="working")
    ev = _claude_event("Stop", ts=170.0, last_assistant_message="x", prompt_id="p1")
    claude.apply_event(rt, _row(rt, sid), ev)
    claude.apply_event(rt, _row(rt, sid), ev)
    items = rt.db.query("SELECT * FROM activity WHERE kind='turn_complete'")
    assert len(items) == 1


def test_stopfailure_error(rt):
    sid = _session(rt, state="working")
    claude.apply_event(rt, _row(rt, sid), _claude_event("StopFailure", ts=170.0, error="rate_limit"))
    assert _row(rt, sid)["state"] == "idle"
    assert rt.db.query("SELECT * FROM activity WHERE kind='error'")


def test_session_end_exited(rt):
    sid = _session(rt, state="working")
    claude.apply_event(rt, _row(rt, sid), _claude_event("SessionEnd", ts=170.0, reason="other"))
    assert _row(rt, sid)["state"] == "exited"


def test_stale_event_ignored(rt):
    sid = _session(rt, state="idle", state_at=500.0)
    # an older Stop event must not rewind or record
    claude.apply_event(rt, _row(rt, sid), _claude_event("Stop", ts=100.0, last_assistant_message="late"))
    row = _row(rt, sid)
    assert row["last_message"] is None
    assert rt.db.query("SELECT * FROM activity WHERE kind='turn_complete'") == []


def test_session_start_not_ignored_when_older(rt):
    sid = _session(rt, state="working", state_at=500.0)
    claude.apply_event(rt, _row(rt, sid), _claude_event("SessionStart", ts=100.0, session_id="a2"))
    assert _row(rt, sid)["state"] == "idle"


def test_summarize_tool():
    assert claude.summarize_tool("Bash", {"command": "ls -la"}) == ("ls -la", ["ls -la"])
    summary, match = claude.summarize_tool("Edit", {"file_path": "/a/b/c.py"})
    assert match == ["c.py"]
    assert claude.summarize_tool("Bash", {}) == ("Bash", [])


# -- codex -------------------------------------------------------------------

def test_codex_title_turn_is_ignored(rt):
    # Live codex-cli 0.159 shape: right after the user's turn, Codex reports its
    # thread-title turn as another agent-turn-complete with a JSON title message.
    sid = _session(rt, kind="codex", env="work2", state="working")
    real = {"v": 1, "source": "codex", "ts": 120.0, "payload": {
        "type": "agent-turn-complete", "thread-id": "th1", "turn-id": "tn1", "last-assistant-message": "alpha"}}
    title = {"v": 1, "source": "codex", "ts": 121.5, "payload": {
        "type": "agent-turn-complete", "thread-id": "th1", "turn-id": "tn2",
        "last-assistant-message": '{"title":"Reply alpha"}'}}
    codex.apply_notify(rt, _row(rt, sid), real)
    codex.apply_notify(rt, _row(rt, sid), title)
    row = _row(rt, sid)
    assert row["last_message"] == "alpha"
    items = rt.db.query("SELECT body FROM activity WHERE kind='turn_complete'")
    assert [i["body"] for i in items] == ["alpha"]


def test_codex_turn_complete(rt):
    sid = _session(rt, kind="codex", env="work2", state="working")
    codex.apply_notify(rt, _row(rt, sid), {
        "v": 1, "source": "codex", "ts": 120.0,
        "payload": {"type": "agent-turn-complete", "thread-id": "th1", "turn-id": "tn1",
                    "last-assistant-message": "codex done"},
    })
    row = _row(rt, sid)
    assert row["state"] == "idle"
    assert row["state_source"] == "notify"
    assert row["agent_session_id"] == "th1"
    assert row["last_message"] == "codex done"
    items = rt.db.query("SELECT * FROM activity WHERE kind='turn_complete'")
    assert len(items) == 1


def test_codex_dedupe_by_turn_id(rt):
    sid = _session(rt, kind="codex", env="work2", state="working")
    ev = {"v": 1, "source": "codex", "ts": 120.0,
          "payload": {"type": "agent-turn-complete", "thread-id": "th1", "turn-id": "tn1"}}
    codex.apply_notify(rt, _row(rt, sid), ev)
    codex.apply_notify(rt, _row(rt, sid), ev)
    assert len(rt.db.query("SELECT * FROM activity WHERE kind='turn_complete'")) == 1


def test_codex_ignores_other_types(rt):
    sid = _session(rt, kind="codex", env="work2", state="working")
    codex.apply_notify(rt, _row(rt, sid), {"v": 1, "source": "codex", "ts": 1.0,
                                           "payload": {"type": "something-else"}})
    assert _row(rt, sid)["state"] == "working"


# -- dispatch and resolution -------------------------------------------------

@pytest.mark.asyncio
async def test_handle_event_resolves_by_relay_id(rt):
    sid = _session(rt, state="working")
    await handle_event(rt, "work1", _claude_event("Stop", ts=200.0, session_id=sid, last_assistant_message="hi"))
    assert _row(rt, sid)["state"] == "idle"


@pytest.mark.asyncio
async def test_handle_event_resolves_by_pane(rt):
    # a laptop-started session: no relay_session_id, resolve via TMUX + pane
    sid = _session(rt, state="working", socket="/tmp/tmux-1000/default", pane_id="%5", server_pid=42)
    event = _claude_event("Stop", ts=200.0, tmux="/tmp/tmux-1000/default,42,0", tmux_pane="%5",
                          last_assistant_message="done")
    await handle_event(rt, "work1", event)
    assert _row(rt, sid)["state"] == "idle"


@pytest.mark.asyncio
async def test_handle_event_wrong_env_not_resolved(rt):
    sid = _session(rt, env="work1", state="working")
    # relay id exists but the event arrived in the wrong env inbox
    await handle_event(rt, "work2", _claude_event("Stop", ts=200.0, session_id=sid))
    assert _row(rt, sid)["state"] == "working"
    rows = rt.db.query("SELECT * FROM hook_events")
    assert rows[0]["session_id"] is None


# -- answer_permission -------------------------------------------------------

_YES_SCREEN = (
    "Do you want to proceed?\n"
    "\u276f 1. Yes\n"
    "  2. No, and tell Claude what to do differently (esc)\n"
    "Running: npm run test\n"
)
_UNHIGHLIGHTED = (
    "Do you want to proceed?\n"
    "  1. Yes\n"
    "  2. No\n"
    "Running: npm run test\n"
)


@pytest.mark.asyncio
async def test_answer_allow_sends_enter(rt):
    tmux = FakeTmux(_YES_SCREEN)
    _install_tmux(rt, "work1", tmux)
    sid = _session(rt, state="needs_input",
                   pending={"id": "req1", "match": ["npm run test"], "answerable": True})
    await claude.answer_permission(rt, sid, "req1", "allow")
    assert tmux.sent == [("/tmp/tmux-1000/default", "%1", ("Enter",))]
    row = _row(rt, sid)
    assert row["pending_json"] is None
    assert row["state"] == "working"


@pytest.mark.asyncio
async def test_answer_deny_sends_escape(rt):
    tmux = FakeTmux(_YES_SCREEN)
    _install_tmux(rt, "work1", tmux)
    sid = _session(rt, state="needs_input",
                   pending={"id": "req1", "match": ["npm run test"], "answerable": True})
    await claude.answer_permission(rt, sid, "req1", "deny")
    assert tmux.sent == [("/tmp/tmux-1000/default", "%1", ("Escape",))]
    assert _row(rt, sid)["pending_json"] is None


@pytest.mark.asyncio
async def test_answer_not_visible_409(rt):
    tmux = FakeTmux("some unrelated screen output\n")
    _install_tmux(rt, "work1", tmux)
    sid = _session(rt, state="needs_input",
                   pending={"id": "req1", "match": ["npm run test"], "answerable": True})
    with pytest.raises(ApiError) as exc:
        await claude.answer_permission(rt, sid, "req1", "allow")
    assert exc.value.status == 409
    assert exc.value.code == "prompt_not_visible"
    assert tmux.sent == []


@pytest.mark.asyncio
async def test_answer_allow_not_highlighted_409(rt):
    tmux = FakeTmux(_UNHIGHLIGHTED)
    _install_tmux(rt, "work1", tmux)
    sid = _session(rt, state="needs_input",
                   pending={"id": "req1", "match": ["npm run test"], "answerable": True})
    with pytest.raises(ApiError) as exc:
        await claude.answer_permission(rt, sid, "req1", "allow")
    assert exc.value.status == 409
    assert exc.value.code == "prompt_not_visible"
    assert tmux.sent == []


@pytest.mark.asyncio
async def test_answer_stale_id_409(rt):
    tmux = FakeTmux(_YES_SCREEN)
    _install_tmux(rt, "work1", tmux)
    sid = _session(rt, state="needs_input",
                   pending={"id": "current", "match": ["npm run test"], "answerable": True})
    with pytest.raises(ApiError) as exc:
        await claude.answer_permission(rt, sid, "old", "allow")
    assert exc.value.status == 409
    assert exc.value.code == "stale_request"


@pytest.mark.asyncio
async def test_answer_no_pending_409(rt):
    tmux = FakeTmux(_YES_SCREEN)
    _install_tmux(rt, "work1", tmux)
    sid = _session(rt, state="idle")
    with pytest.raises(ApiError) as exc:
        await claude.answer_permission(rt, sid, "req1", "allow")
    assert exc.value.status == 409
    assert exc.value.code == "no_pending_request"


# -- hooks_install -----------------------------------------------------------

def _settings_file(env):
    return env.host_home / ".claude" / "settings.json"


def test_hooks_install_merges_and_backs_up(rt):
    env = rt.settings.env("work1")
    path = _settings_file(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"hooks": [{"type": "command", "command": "other.sh"}]}
    ]}, "other_setting": True}, indent=2))

    result = hooks_install.install(env)
    assert result["changed"] is True
    assert result["backup"] is not None

    data = json.loads(path.read_text())
    # existing non-relay hook preserved
    assert data["other_setting"] is True
    pre = data["hooks"]["PreToolUse"]
    assert any(g["hooks"][0].get("command") == "other.sh" for g in pre)
    # relay hook added
    assert any(env.hook_script in g["hooks"][0].get("args", []) for g in pre)
    assert "SessionStart" in data["hooks"]


def test_hooks_install_idempotent(rt):
    env = rt.settings.env("work1")
    first = hooks_install.install(env)
    assert first["changed"] is True
    second = hooks_install.install(env)
    assert second["changed"] is False
    assert second["backup"] is None


def test_hooks_install_dry_run(rt):
    env = rt.settings.env("work1")
    result = hooks_install.install(env, dry_run=True)
    assert result["changed"] is True
    assert result["dry_run"] is True
    # nothing written
    assert not _settings_file(env).exists()


def test_hooks_uninstall(rt):
    env = rt.settings.env("work1")
    path = _settings_file(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"hooks": [{"type": "command", "command": "other.sh"}]}
    ]}}, indent=2))
    hooks_install.install(env)
    assert hooks_install.status(env)["installed"] is True

    result = hooks_install.uninstall(env)
    assert result["changed"] is True
    assert hooks_install.status(env)["installed"] is False
    data = json.loads(path.read_text())
    # the pre-existing hook survives uninstall
    pre = data["hooks"]["PreToolUse"]
    assert any(g["hooks"][0].get("command") == "other.sh" for g in pre)


def test_hooks_status_no_file(rt):
    env = rt.settings.env("work1")
    st = hooks_install.status(env)
    assert st["exists"] is False
    assert st["installed"] is False
