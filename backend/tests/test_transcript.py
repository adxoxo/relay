"""Tests for the transcript reader on synthetic JSONL that mirrors real data."""

from __future__ import annotations

import json
import os

import pytest

from relay import transcript


def _projects_dir(env):
    d = env.host_home / ".claude" / "projects" / "proj"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_read_conversation_shapes(rt):
    env = rt.settings.env("work1")
    d = _projects_dir(env)
    tpath = d / "t.jsonl"
    rows = [
        {"type": "user", "uuid": "u1", "timestamp": "2026-09-30T10:00:00.000Z",
         "message": {"role": "user", "content": "Hello there"}},
        {"type": "user", "uuid": "u2", "timestamp": "2026-09-30T10:00:01.000Z", "isMeta": True,
         "message": {"role": "user", "content": "meta line"}},
        {"type": "user", "uuid": "u3", "timestamp": "2026-09-30T10:00:02.000Z",
         "message": {"role": "user", "content": "<command-name>foo</command-name>"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-09-30T10:00:03.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "secret reasoning"},
             {"type": "text", "text": "Here is the answer"},
             {"type": "tool_use", "id": "tu1", "name": "Bash", "input": {"command": "npm test"}},
         ]}},
        {"type": "user", "uuid": "u4", "timestamp": "2026-09-30T10:00:04.000Z", "isSidechain": True,
         "message": {"role": "user", "content": "sidechain"}},
        {"type": "user", "uuid": "u5", "timestamp": "2026-09-30T10:00:05.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "tu1", "content": "ok"},
             {"type": "text", "text": "Next question"},
         ]}},
        {"type": "file-history-snapshot", "uuid": "f1"},
    ]
    _write_jsonl(tpath, rows)

    items = transcript.read_conversation(env, "/home/node/.claude/projects/proj/t.jsonl")
    roles = [(i["role"], i.get("text") or (i["tool"]["summary"] if i["tool"] else "")) for i in items]
    assert ("user", "Hello there") in roles
    assert ("assistant", "Here is the answer") in roles
    assert ("tool", "npm test") in roles
    assert ("user", "Next question") in roles
    # excluded content
    texts = [i["text"] for i in items]
    assert "secret reasoning" not in texts  # thinking skipped
    assert "meta line" not in texts
    assert "sidechain" not in texts
    assert not any("command-name" in (t or "") for t in texts)
    # tool item shape
    tool_items = [i for i in items if i["role"] == "tool"]
    assert tool_items[0]["tool"] == {"name": "Bash", "summary": "npm test"}
    # ts parsed to epoch
    assert all(i["ts"] is None or isinstance(i["ts"], float) for i in items)


def test_limit(rt):
    env = rt.settings.env("work1")
    d = _projects_dir(env)
    tpath = d / "big.jsonl"
    rows = [
        {"type": "assistant", "uuid": f"a{i}", "timestamp": "2026-09-30T10:00:00.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": f"msg {i}"}]}}
        for i in range(10)
    ]
    _write_jsonl(tpath, rows)
    items = transcript.read_conversation(env, "/home/node/.claude/projects/proj/big.jsonl", limit=3)
    assert len(items) == 3
    assert items[-1]["text"] == "msg 9"  # keeps the last N


def test_rejects_non_jsonl(rt):
    env = rt.settings.env("work1")
    with pytest.raises(transcript.TranscriptError):
        transcript.read_conversation(env, "/home/node/.claude/projects/proj/t.txt")


def test_rejects_outside_home(rt):
    env = rt.settings.env("work1")
    with pytest.raises(transcript.TranscriptError):
        transcript.read_conversation(env, "/etc/passwd.jsonl")


def test_symlink_escape_rejected(rt, tmp_path):
    env = rt.settings.env("work1")
    d = _projects_dir(env)
    # a secret outside the projects tree
    secret = env.host_home / "secret.jsonl"
    secret.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "leak"}}) + "\n")
    link = d / "escape.jsonl"
    os.symlink(secret, link)
    with pytest.raises(transcript.TranscriptError):
        transcript.read_conversation(env, "/home/node/.claude/projects/proj/escape.jsonl")


def test_missing_file_rejected(rt):
    env = rt.settings.env("work1")
    _projects_dir(env)
    with pytest.raises(transcript.TranscriptError):
        transcript.read_conversation(env, "/home/node/.claude/projects/proj/nope.jsonl")
