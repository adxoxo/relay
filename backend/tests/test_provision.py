"""Tests for provisioning the hook artifacts on the host."""

from __future__ import annotations

import json
import os
import tomllib

import pytest

from relay import provision


def test_provision_writes_files_with_modes(rt):
    result = provision.provision_env(rt, "work1")
    assert result["ok"] is True
    assert result["changed"] is True
    assert result["error"] is None

    env = rt.settings.env("work1")
    share = env.host_share_dir
    hook = share / "hook.py"
    settings = share / "claude-settings.json"
    inbox = env.host_inbox_dir

    assert hook.exists() and (os.stat(hook).st_mode & 0o777) == 0o700
    assert settings.exists() and (os.stat(settings).st_mode & 0o777) == 0o600
    assert inbox.is_dir() and (os.stat(inbox).st_mode & 0o777) == 0o700

    data = json.loads(settings.read_text())
    hooks = data["hooks"]
    for event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
                  "PostToolUse", "PostToolUseFailure", "PermissionDenied", "Notification",
                  "Stop", "StopFailure", "SessionEnd"):
        assert event in hooks
        handler = hooks[event][0]["hooks"][0]
        assert handler["command"] == "python3"
        assert handler["args"] == [env.hook_script, "claude"]
        assert handler["timeout"] == 10
    # async only on the observing events
    assert hooks["PreToolUse"][0]["hooks"][0]["async"] is True
    assert hooks["PostToolUse"][0]["hooks"][0]["async"] is True
    assert "async" not in hooks["SessionStart"][0]["hooks"][0]

    # env_status records the provision result
    assert rt.env_status["work1"].provision == result


def test_provision_idempotent(rt):
    first = provision.provision_env(rt, "work1")
    assert first["changed"] is True
    second = provision.provision_env(rt, "work1")
    assert second["ok"] is True
    assert second["changed"] is False


def test_provision_refuses_symlink(rt):
    env = rt.settings.env("work1")
    share = env.host_share_dir
    share.mkdir(parents=True, exist_ok=True)
    os.chmod(share, 0o700)
    # plant a symlink where hook.py should be written
    target = share / "elsewhere"
    target.write_text("x")
    link = share / "hook.py"
    os.symlink(target, link)

    result = provision.provision_env(rt, "work1")
    assert result["ok"] is False
    assert "symlink" in (result["error"] or "")


def test_provision_unknown_env(rt):
    result = provision.provision_env(rt, "nope")
    assert result["ok"] is False
    assert result["error"]


def test_codex_notify_override(rt):
    env = rt.settings.env("work2")
    override = provision.codex_notify_override(env)
    assert override.startswith("notify=")
    argv = json.loads(override[len("notify="):])
    assert argv == ["python3", env.hook_script, "codex"]


@pytest.mark.asyncio
async def test_provision_all(rt):
    out = await provision.provision_all(rt)
    assert set(out) == set(rt.settings.envs)
    assert all(r["ok"] for r in out.values())


@pytest.mark.asyncio
async def test_startup_provisioning_puts_the_codex_thread_id_title_in_every_env(rt):
    work2 = rt.settings.env("work2").host_codex_config
    work2.parent.mkdir()
    work2.write_text('model = "gpt-6.1-sol"\n[tui]\nterminal_title = ["project-name"]\n')

    out = await provision.provision_all(rt)

    for env_id, result in out.items():
        env = rt.settings.env(env_id)
        assert result["codex_title"]["terminal_title"] == "relay"
        data = tomllib.loads(env.host_codex_config.read_text())
        assert data["tui"]["terminal_title"][-1] == "thread-id"
        assert "notify" not in data  # notify stays an explicit opt-in
        assert not (env.host_home / ".claude" / "settings.json").exists()
    assert tomllib.loads(work2.read_text())["tui"]["terminal_title"] == ["project-name", "thread-id"]
    assert (os.stat(rt.settings.env("work1").host_home / ".codex").st_mode & 0o777) == 0o700  # Codex not installed yet
    again = await provision.provision_all(rt)
    assert not any(r["codex_title"]["changed"] for r in again.values())
