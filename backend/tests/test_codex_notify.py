"""Tests for the Codex notify install into a global config.toml.

The writer is line based and must never disturb an existing secret, comment,
or table key. These tests lock that safety property and the placement rule
(notify must sit in the top-level region, before the first table header).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from relay import hooks_install as H
from relay.config import EnvConfig


@pytest.fixture
def env(tmp_path: Path) -> EnvConfig:
    home = tmp_path / "work1"
    home.mkdir()
    return EnvConfig(id="work1", label="Work 1", container="job-work1", host_home=home)


def _config_path(env: EnvConfig) -> Path:
    return env.host_codex_config


def test_install_creates_config_when_missing(env: EnvConfig):
    res = H.codex_install(env)
    assert res["changed"] is True
    path = _config_path(env)
    assert path.exists()
    lines = path.read_text().split("\n")
    assert any(H._is_relay_notify(line) for line in lines)
    assert H.codex_status(env)["installed"] is True


def test_install_is_idempotent(env: EnvConfig):
    H.codex_install(env)
    second = H.codex_install(env)
    assert second["changed"] is False


def test_install_preserves_secret_and_tables(env: EnvConfig):
    # A realistic config: top-level keys, a table with a secret, more tables.
    original = (
        'approval_policy = "never"\n'
        'model = "gpt-6.1-sol"\n'
        "\n"
        '[projects."/home/node/projects"]\n'
        'trust_level = "trusted"\n'
        "\n"
        "[mcp_servers.honcho]\n"
        'http_headers = { "Authorization" = "Bearer SECRET_TOKEN_VALUE" }\n'
    )
    path = _config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(original)

    res = H.codex_install(env)
    assert res["changed"] is True
    assert res["backup"] is not None
    text = path.read_text()

    # The secret and every original line survive.
    assert "Bearer SECRET_TOKEN_VALUE" in text
    assert 'approval_policy = "never"' in text
    assert '[projects."/home/node/projects"]' in text
    assert "[mcp_servers.honcho]" in text

    # notify sits in the top-level region, before the first table header.
    lines = text.split("\n")
    notify_idx = next(i for i, l in enumerate(lines) if H._is_relay_notify(l))
    first_table = next(i for i, l in enumerate(lines) if l.startswith("["))
    assert notify_idx < first_table

    # The backup holds the exact original.
    backup = Path(res["backup"])
    assert backup.read_text() == original


def test_install_replaces_a_prior_relay_notify_in_place(env: EnvConfig):
    H.codex_install(env)
    first = _config_path(env).read_text()
    # Re-install after a hook path change would replace, not duplicate.
    second = H.codex_install(env)
    assert second["changed"] is False  # same env, same line
    assert _config_path(env).read_text().count("relay-managed notify") == 1
    assert first.count("relay-managed notify") == 1


def test_uninstall_removes_only_relay_notify(env: EnvConfig):
    original = (
        'model = "gpt-6.1-sol"\n'
        "\n"
        "[mcp_servers.honcho]\n"
        'http_headers = { "Authorization" = "Bearer SECRET_TOKEN_VALUE" }\n'
    )
    path = _config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(original)
    H.codex_install(env)
    assert H.codex_status(env)["installed"] is True

    res = H.codex_uninstall(env)
    assert res["changed"] is True
    text = path.read_text()
    assert "relay-managed notify" not in text
    assert "Bearer SECRET_TOKEN_VALUE" in text
    assert 'model = "gpt-6.1-sol"' in text
    assert H.codex_status(env)["installed"] is False


def test_install_adds_the_thread_id_terminal_title_to_the_tui_table(env: EnvConfig):
    original = (
        'model = "gpt-6.1-sol"\n'
        "\n"
        "[tui]\n"
        "screen_reader_detection_done = true\n"
        "\n"
        "[tui.model_availability_nux]\n"
        '"gpt-6.1-sol" = 1\n'
        "\n"
        "[mcp_servers.honcho]\n"
        'http_headers = { "Authorization" = "Bearer SECRET_TOKEN_VALUE" }\n'
    )
    path = _config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(original)

    res = H.codex_install(env)
    assert (res["changed"], res["terminal_title"]) == (True, "relay")
    data = tomllib.loads(path.read_text())
    assert data["tui"]["terminal_title"] == ["activity", "thread-name", "project-name", "thread-id"]
    assert data["tui"]["screen_reader_detection_done"] is True
    assert data["tui"]["model_availability_nux"] == {"gpt-6.1-sol": 1}
    assert "Bearer SECRET_TOKEN_VALUE" in path.read_text()
    assert H.codex_install(env)["changed"] is False
    assert H.codex_status(env)["terminal_title"] == "relay"

    H.codex_uninstall(env)
    data = tomllib.loads(path.read_text())
    assert "terminal_title" not in data["tui"] and data["tui"]["screen_reader_detection_done"] is True
    assert H.codex_status(env)["terminal_title"] == "missing"


@pytest.mark.parametrize("assignment, items", [
    ('terminal_title = ["spinner", "project"]  # mine', ["spinner", "project"]),
    ('terminal_title = [\n  "thread-id",\n  "git-branch",\n]', ["thread-id", "git-branch"]),
    ("terminal_title = []", []),
])
def test_title_install_appends_the_id_to_a_user_list_and_uninstall_restores_it(env: EnvConfig, assignment, items):
    original = (
        'model = "gpt-6.1-sol"\n'
        "[tui]\n"
        f"{assignment}\n"
        "screen_reader_detection_done = true\n"
        "[mcp_servers.honcho]\n"
        'http_headers = { "Authorization" = "Bearer SECRET_TOKEN_VALUE" }\n'
    )
    path = _config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(original)
    os.chmod(path, 0o600)

    res = H.codex_title_install(env)

    assert (res["changed"], res["terminal_title"]) == (True, "relay")
    assert Path(res["backup"]).read_text() == original
    data = tomllib.loads(path.read_text())
    assert data["tui"]["terminal_title"] == [*items, "thread-id"]
    assert data["tui"]["screen_reader_detection_done"] is True
    assert "Bearer SECRET_TOKEN_VALUE" in path.read_text() and "notify" not in data
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert H.codex_title_install(env)["changed"] is False
    assert H.codex_status(env)["terminal_title"] == "relay"

    H.codex_uninstall(env)
    assert path.read_text() == original


def test_title_install_keeps_a_user_list_that_ends_with_the_id(env: EnvConfig):
    original = '[tui]\nterminal_title = ["project-name", "session-id"]\n'
    path = _config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(original)

    res = H.codex_title_install(env)

    assert (res["changed"], res["terminal_title"]) == (False, "user")
    assert path.read_text() == original


@pytest.mark.parametrize("text", ["[tui\nbroken", "tui.terminal_title = 3\n", 'tui = { terminal_title = ["a"] }\n'])
def test_title_install_reports_an_unsupported_config_without_a_rewrite(env: EnvConfig, text):
    path = _config_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)

    res = H.codex_title_install(env)

    assert res["changed"] is False and res["terminal_title"] in ("unreadable", "unsupported")
    assert path.read_text() == text


def test_notify_line_value_matches_hook_script(env: EnvConfig):
    line = H.codex_notify_line(env)
    assert env.hook_script in line
    assert '"codex"' in line
    assert line.startswith("notify = [")
