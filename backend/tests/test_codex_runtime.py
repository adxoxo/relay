"""Codex 0.160 compatibility of the verified thread link and its readers.

The rollout below has the layout that ``codex_runtime`` documents. The tests
check that a 0.160 thread passes the same state index, metadata, file identity
and top-level checks as before, and that other versions and subagent threads
still fail closed.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from relay import codex_runtime, routes_activity, terminal_history
from relay.adapters import handle_event
from relay.db import loads
from relay.transcript import TranscriptError

THREAD = "01a104a1-1f04-78c2-a039-29485a1efa0e"
CWD = "/home/node/projects/demo"


def _rollout_path(thread: str) -> str:
    return f"/home/node/.codex/sessions/2026/10/04/rollout-2026-10-04T10-00-00-{thread}.jsonl"


def _write_thread(env, *, thread=THREAD, version="0.160.0", index_source="cli", meta_source="cli", parent=None):
    """One state index row and one rollout file for ``thread``."""
    path = _rollout_path(thread)
    host = env.host_path(path)
    host.parent.mkdir(parents=True, exist_ok=True)
    meta = {"id": thread, "timestamp": "2026-10-04T10:00:00.000Z", "cwd": CWD, "originator": "codex_cli_rs",
            "cli_version": version, "source": meta_source, "model_provider": "openai"}
    if parent is not None:
        meta["parent_thread_id"] = parent
    records = [
        {"timestamp": "2026-10-04T10:00:00.000Z", "type": "session_meta", "payload": meta},
        {"timestamp": "2026-10-04T10:00:01.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello relay"}]}},
        {"timestamp": "2026-10-04T10:00:01.100Z", "type": "event_msg",
         "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"timestamp": "2026-10-04T10:00:02.000Z", "type": "response_item",
         "payload": {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"}},
        {"timestamp": "2026-10-04T10:00:03.000Z", "type": "response_item",
         "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                     "content": [{"type": "output_text", "text": "done"}]}},
        {"timestamp": "2026-10-04T10:00:03.100Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "done"}},
    ]
    host.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    db = env.host_home / ".codex" / "state_5.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, rollout_path TEXT, archived INTEGER, source TEXT, cwd TEXT)"
        )
        conn.execute("INSERT OR REPLACE INTO threads VALUES (?, ?, 0, ?, ?)", (thread, path, index_source, CWD))
        conn.commit()
    finally:
        conn.close()
    return path


def _codex_row(rt) -> str:
    return rt.db.insert_session(dict(
        env="work2", kind="codex", display_name="agent1", tmux_name="agent1", origin="dashboard", status="live",
        state="working", socket_path="/tmp/tmux-1000/default", server_pid=9, tmux_session_id="$1",
        pane_id="%1", pane_pid=100,
    ))


def _notify(row_id: str, *, thread=THREAD, via="argv") -> dict:
    return {"v": 1, "source": "codex", "ts": time.time(), "relay_session_id": row_id, "relay_id_from": via,
            "payload": {"type": "agent-turn-complete", "thread-id": thread, "turn-id": "turn-1",
                        "last-assistant-message": "done"}}


def test_verify_thread_accepts_codex_0160(rt):
    env = rt.settings.env("work2")
    path = _write_thread(env)
    proof = codex_runtime.verify_thread(env, THREAD, expected_path=path)
    st = env.host_path(path).stat()
    assert proof["thread_id"] == THREAD
    assert proof["transcript_path"] == path
    assert proof["cli_version"] == "0.160.0"
    assert proof["rollout"] == {"dev": st.st_dev, "ino": st.st_ino}
    with pytest.raises(codex_runtime.BindingError):
        codex_runtime.verify_thread(env, THREAD, expected_path=_rollout_path("01a104a1-1f04-78c2-a039-000000000000"))


@pytest.mark.parametrize("version", ["0.160.0", "0.163.0"])
async def test_argv_notify_links_thread_for_conversation_history_and_lifecycle(rt, version):
    env = rt.settings.env("work2")
    path = _write_thread(env, version=version)
    rid = _codex_row(rt)

    await handle_event(rt, "work2", _notify(rid))

    row = rt.db.get_session(rid)
    binding = codex_runtime.binding_of(row)
    assert codex_runtime.is_verified(binding)
    assert (binding["via"], binding["thread_id"], binding["cli_version"]) == ("argv", THREAD, version)
    assert binding["pane"] == {"socket": "/tmp/tmux-1000/default", "server_pid": 9, "tmux_session_id": "$1",
                               "pane_id": "%1", "pane_pid": 100}
    assert (row["agent_session_id"], row["transcript_path"], row["project_dir"]) == (THREAD, path, CWD)
    events = rt.db.query("SELECT session_id FROM hook_events")
    assert [e["session_id"] for e in events] == [rid]
    assert [a["body"] for a in rt.db.query("SELECT body FROM activity WHERE kind = 'turn_complete'")] == ["done"]

    # Conversation: same source guard and reader as the route.
    source = terminal_history.conversation_source_for(row, env)
    items = routes_activity._read_conversation(env, source, 50)
    assert [i["role"] for i in items] == ["user", "tool", "assistant"]
    assert (items[0]["text"], items[2]["text"]) == ("hello relay", "done")

    # Terminal history reader.
    page = terminal_history.read_page(env, terminal_history.History(source=source))
    assert page.entries == [{"role": "user", "text": "hello relay"}, {"role": "tool", "text": "exec_command: ls"},
                            {"role": "assistant", "text": "done"}]

    # Rollout lifecycle: the bootstrap restores idle without a second Activity.
    await codex_runtime.poll_env(rt, "work2")
    row = rt.db.get_session(rid)
    assert (row["state"], row["state_source"]) == ("idle", codex_runtime.STATE_SOURCE)
    assert len(rt.db.query("SELECT id FROM activity WHERE kind = 'turn_complete'")) == 1


@pytest.mark.parametrize("version", ["0.161.0", "0.162.0", "0.162.1", "0.163.0", "0.163.1"])
def test_verify_thread_accepts_newer_inspected_versions(rt, version):
    env = rt.settings.env("work2")
    path = _write_thread(env, version=version)
    proof = codex_runtime.verify_thread(env, THREAD, expected_path=path)
    assert proof["cli_version"] == version


@pytest.mark.parametrize("version", ["0.164.0", "0.155.0", "0.160", "0.1600.0", "1.160.0"])
async def test_unsupported_version_is_refused(rt, version):
    env = rt.settings.env("work2")
    path = _write_thread(env, version=version)
    rid = _codex_row(rt)
    with pytest.raises(codex_runtime.BindingError, match="not supported"):
        codex_runtime.verify_thread(env, THREAD)
    with pytest.raises(TranscriptError, match="not supported"):
        codex_runtime.scan_rollout(env, THREAD, path, None)

    await handle_event(rt, "work2", _notify(rid))

    row = rt.db.get_session(rid)
    assert codex_runtime.binding_of(row) is None
    assert row["agent_session_id"] is None
    assert rt.db.query("SELECT session_id FROM hook_events")[0]["session_id"] is None


@pytest.mark.parametrize("kind", ["index_source", "meta_source", "parent"])
async def test_codex_0160_subagent_thread_is_refused(rt, kind):
    env = rt.settings.env("work2")
    subagent = {"subagent": {"thread_spawn": {"depth": 1}}}
    _write_thread(env, **{
        "index_source": {"index_source": json.dumps(subagent)},
        "meta_source": {"meta_source": subagent},
        "parent": {"parent": "01a104a1-1f04-78c2-a039-111111111111"},
    }[kind])
    rid = _codex_row(rt)
    with pytest.raises(codex_runtime.BindingError, match="subagent"):
        codex_runtime.verify_thread(env, THREAD)

    await handle_event(rt, "work2", _notify(rid))

    assert codex_runtime.binding_of(rt.db.get_session(rid)) is None


async def test_codex_0160_relay_id_from_environment_never_links(rt):
    env = rt.settings.env("work2")
    _write_thread(env)
    rid = _codex_row(rt)

    await handle_event(rt, "work2", _notify(rid, via="env"))

    row = rt.db.get_session(rid)
    assert codex_runtime.binding_of(row) is None
    assert loads(row["meta_json"]) is None




# -- _bootstrap: lifecycle state beyond the newest read page -----------------
#
# A real agent2 rollout (29,377,199 bytes) auto-linked but its newest 4 MiB
# held no lifecycle event, so the old single-page bootstrap left the row on
# its terminal fallback. These fixtures put the latest task_started more than
# one page back. MAX_READ_BYTES is patched to a small page so they stay cheap;
# the walk logic does not depend on the page size.

BOOT_THREAD = "01a104a1-1f04-78c2-a039-2222222222aa"
PAGE = 64 * 1024


@pytest.fixture
def small_pages(monkeypatch):
    monkeypatch.setattr(codex_runtime, "MAX_READ_BYTES", PAGE)


def _meta_line(thread: str) -> str:
    meta = {"id": thread, "timestamp": "2026-10-11T10:00:00.000Z", "cwd": CWD, "originator": "codex_cli_rs",
            "cli_version": "0.162.1", "source": "cli", "model_provider": "openai"}
    return json.dumps({"timestamp": "2026-10-11T10:00:00.000Z", "type": "session_meta", "payload": meta})


def _event(kind: str, turn_id: str, ts: str = "2026-10-11T10:00:01.000Z", **extra: object) -> str:
    return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": kind, "turn_id": turn_id, **extra}})


def _user(text: str, item_id: str) -> str:
    return json.dumps({"timestamp": "2026-10-11T10:00:01.000Z", "type": "response_item",
                       "payload": {"type": "message", "role": "user", "id": item_id,
                                   "content": [{"type": "input_text", "text": text}]}})


def _output(size: int) -> str:
    """One tool-output line of about ``size`` bytes; it is neither a user item nor an event."""
    return json.dumps({"timestamp": "2026-10-11T10:00:02.000Z", "type": "response_item",
                       "payload": {"type": "function_call_output", "call_id": "c1", "output": "x" * size}})


def _outputs(total: int, line: int = 2048) -> list[str]:
    return [_output(line)] * (total // line + 1)


def _write(env, lines: list[str], *, final_newline: bool = True) -> str:
    path = _rollout_path(BOOT_THREAD)
    host = env.host_path(path)
    host.parent.mkdir(parents=True, exist_ok=True)
    host.write_text("\n".join(lines) + ("\n" if final_newline else ""), encoding="utf-8")
    return path


def _append(env, path: str, text: str) -> None:
    with open(env.host_path(path), "a", encoding="utf-8") as fh:
        fh.write(text)


def _boot(env, path):
    cursor, events, bootstrap = codex_runtime.scan_rollout(env, BOOT_THREAD, path, None)
    assert bootstrap is True
    return cursor, events


def test_bootstrap_unrelated_completion_in_the_newest_page_keeps_the_turn(rt, small_pages):
    env = rt.settings.env("work2")
    path = _write(env, [_meta_line(BOOT_THREAD), _event("task_started", "turn-a"), _user("go", "u-a"),
                        *_outputs(3 * PAGE), _event("task_complete", "turn-b", last_agent_message="other")])

    cursor, events = _boot(env, path)

    assert [e.type for e in events] == ["task_started"]
    assert (cursor.active_turn, cursor.active_prompt_id) == ("turn-a", "u-a-0")


def test_bootstrap_late_end_of_an_older_turn_keeps_the_newer_turn(rt, small_pages):
    env = rt.settings.env("work2")
    path = _write(env, [_meta_line(BOOT_THREAD), _event("task_started", "turn-a"),
                        _event("task_complete", "turn-a", last_agent_message="a done"),
                        _event("task_started", "turn-b"), *_outputs(3 * PAGE),
                        _event("task_complete", "turn-a", last_agent_message="late")])

    cursor, events = _boot(env, path)

    assert [(e.type, e.turn_id) for e in events] == [("task_started", "turn-b")]
    assert cursor.active_turn == "turn-b"


def test_bootstrap_matching_completion_past_a_long_gap_is_idle(rt, small_pages):
    env = rt.settings.env("work2")
    path = _write(env, [_meta_line(BOOT_THREAD), _event("task_started", "turn-a"),
                        _event("task_complete", "turn-a", last_agent_message="done"), *_outputs(3 * PAGE)])

    cursor, events = _boot(env, path)

    assert [(e.type, e.last_message) for e in events] == [("task_complete", "done")]
    assert cursor.active_turn is None


def test_bootstrap_turn_longer_than_eight_pages_with_giant_and_medium_lines(rt, small_pages):
    env = rt.settings.env("work2")
    path = _write(env, [_meta_line(BOOT_THREAD), _event("task_started", "turn-a"),
                        _output(3 * PAGE), *_outputs(10 * PAGE), _output(2 * PAGE),
                        _event("turn_aborted", "turn-b", reason="interrupted")])

    cursor, events = _boot(env, path)

    assert [(e.type, e.turn_id) for e in events] == [("task_started", "turn-a")]
    assert cursor.active_turn == "turn-a"


def test_bootstrap_without_any_event_preserves_unknown(rt, small_pages):
    env = rt.settings.env("work2")
    path = _write(env, [_meta_line(BOOT_THREAD), *_outputs(3 * PAGE)])

    cursor, events = _boot(env, path)

    assert events == []
    assert cursor.active_turn is None


def test_completion_after_bootstrap_is_consumed_once(rt, small_pages):
    env = rt.settings.env("work2")
    path = _write(env, [_meta_line(BOOT_THREAD), _event("task_started", "turn-a"), *_outputs(3 * PAGE)])
    cursor, events = _boot(env, path)
    assert cursor.active_turn == "turn-a"

    _append(env, path, _event("task_complete", "turn-a", last_agent_message="done") + "\n")
    cursor, events, bootstrap = codex_runtime.scan_rollout(env, BOOT_THREAD, path, cursor)
    assert (bootstrap, [e.type for e in events]) == (False, ["task_complete"])
    assert cursor.offset == env.host_path(path).stat().st_size

    cursor, events, bootstrap = codex_runtime.scan_rollout(env, BOOT_THREAD, path, cursor)
    assert (bootstrap, events) == (False, [])


def test_unterminated_tail_completion_is_read_once_after_it_ends(rt, small_pages):
    env = rt.settings.env("work2")
    end_line = _event("task_complete", "turn-a", last_agent_message="done")
    path = _write(env, [_meta_line(BOOT_THREAD), _event("task_started", "turn-a"), *_outputs(3 * PAGE), end_line],
                  final_newline=False)
    cursor, events = _boot(env, path)
    assert ([e.type for e in events], cursor.active_turn) == (["task_started"], "turn-a")

    _append(env, path, "\n")
    cursor, events, _ = codex_runtime.scan_rollout(env, BOOT_THREAD, path, cursor)
    assert [e.type for e in events] == ["task_complete"]
    cursor, events, _ = codex_runtime.scan_rollout(env, BOOT_THREAD, path, cursor)
    assert events == []


def test_bootstrap_memory_follows_one_page_not_the_log(rt, small_pages):
    import tracemalloc

    env = rt.settings.env("work2")

    def peak_for(pages: int) -> int:
        path = _write(env, [_meta_line(BOOT_THREAD), _event("task_started", "turn-a"), *_outputs(pages * PAGE)])
        tracemalloc.start()
        try:
            cursor, _ = _boot(env, path)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert cursor.active_turn == "turn-a"
        return peak

    small, large = peak_for(32), peak_for(160)
    # The fixed part is the session_meta read (MAX_HEADER_BYTES). Holding the
    # walked lines would add about 128 pages here.
    assert large < small + 4 * PAGE, f"peak grew from {small} to {large} bytes with the log size"
    assert large < codex_runtime.MAX_HEADER_BYTES + 8 * PAGE
