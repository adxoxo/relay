"""Tests for the ops CLI.

These tests cover the pure, tested pieces the contract asks for: argument
parsing, url output, the attach resolution function, unit template rendering
into a temporary directory (no systemctl), and the wrapper script.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from relay import cli, paths
from relay.config import Settings


# -- fixtures ----------------------------------------------------------------

def _live(session_id: str, env: str, name: str, **extra) -> dict:
    row = {
        "id": session_id,
        "env": env,
        "name": name,
        "kind": "shell",
        "status": "live",
        "state": "idle",
        "tmux": {"session_id": "$1", "created": 1, "socket": "/tmp/tmux-1000/default", "pane_count": 1},
    }
    row.update(extra)
    return row


# -- parsing -----------------------------------------------------------------

def test_parser_serve_defaults() -> None:
    args = cli.build_parser().parse_args(["serve"])
    assert args.command == "serve"
    assert args.host is None and args.port is None
    assert args.func is cli.cmd_serve


def test_parser_new_full() -> None:
    args = cli.build_parser().parse_args(
        ["new", "work1", "claude", "--name", "x", "--project", "p", "--prompt", "hi",
         "--permission-mode", "plan", "--attach"]
    )
    assert (args.env, args.kind, args.name, args.project) == ("work1", "claude", "x", "p")
    assert args.prompt == "hi" and args.permission_mode == "plan" and args.attach is True


def test_parser_new_rejects_bad_kind() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["new", "work1", "banana"])


def test_parser_hooks_requires_env() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["hooks", "install"])
    args = cli.build_parser().parse_args(["hooks", "install", "--env", "work2", "--dry-run"])
    assert args.action == "install" and args.env == "work2" and args.dry_run is True


def test_parser_requires_command() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


# -- url output --------------------------------------------------------------

def test_build_url_local(settings: Settings) -> None:
    url = cli.build_url(settings, "SECRET", tailnet=False)
    assert url == f"http://127.0.0.1:{settings.port}/#token=SECRET"


def test_build_url_tailnet(settings: Settings) -> None:
    tuned = settings.with_overrides(tailscale_hostname="laptop.example.ts.net")
    url = cli.build_url(tuned, "SECRET", tailnet=True)
    assert url == "https://laptop.example.ts.net/#token=SECRET"


def test_build_url_tailnet_without_hostname(settings: Settings) -> None:
    with pytest.raises(cli.CliError):
        cli.build_url(settings, "SECRET", tailnet=True)


# -- token reading -----------------------------------------------------------

def test_read_token_missing(settings: Settings) -> None:
    with pytest.raises(cli.CliError):
        cli.read_token(settings)


def test_read_token_present(settings: Settings) -> None:
    settings.token_path.write_text("tok\n")
    assert cli.read_token(settings) == "tok"


def test_read_token_empty(settings: Settings) -> None:
    settings.token_path.write_text("   ")
    with pytest.raises(cli.CliError):
        cli.read_token(settings)


# -- attach resolution -------------------------------------------------------

def test_resolve_by_exact_id() -> None:
    sessions = [_live("abcd1234", "work1", "agent1"), _live("ef567890", "work1", "agent2")]
    assert cli.resolve_session(sessions, "work1", "abcd1234")["name"] == "agent1"


def test_resolve_by_name_case_insensitive() -> None:
    sessions = [_live("abcd1234", "work1", "Agent1")]
    assert cli.resolve_session(sessions, "work1", "agent1")["id"] == "abcd1234"


def test_resolve_by_id_prefix() -> None:
    sessions = [_live("abcd1234", "work1", "agent1")]
    assert cli.resolve_session(sessions, "work1", "abcd")["name"] == "agent1"


def test_resolve_scopes_to_env() -> None:
    sessions = [_live("abcd1234", "work2", "agent1")]
    with pytest.raises(cli.CliError):
        cli.resolve_session(sessions, "work1", "agent1")


def test_resolve_ignores_ended_sessions() -> None:
    sessions = [_live("abcd1234", "work1", "agent1", status="ended")]
    with pytest.raises(cli.CliError):
        cli.resolve_session(sessions, "work1", "agent1")


def test_resolve_ambiguous_name() -> None:
    sessions = [_live("aaaa1111", "work1", "dup"), _live("bbbb2222", "work1", "DUP")]
    with pytest.raises(cli.CliError):
        cli.resolve_session(sessions, "work1", "dup")


def test_resolve_ambiguous_prefix() -> None:
    sessions = [_live("abcd1111", "work1", "one"), _live("abcd2222", "work1", "two")]
    with pytest.raises(cli.CliError):
        cli.resolve_session(sessions, "work1", "abcd")


def test_resolve_no_match() -> None:
    with pytest.raises(cli.CliError):
        cli.resolve_session([], "work1", "ghost")


def test_resolve_name_wins_over_prefix() -> None:
    # A session whose name equals the needle beats a different id-prefix match.
    sessions = [_live("11112222", "work1", "33"), _live("33334444", "work1", "other")]
    assert cli.resolve_session(sessions, "work1", "33")["id"] == "11112222"


# -- unit rendering ----------------------------------------------------------

def test_render_unit_replaces_placeholders() -> None:
    text = "ExecStart=@PYTHON@ -m relay serve\nWorkingDirectory=@REPO@/backend\n@PODMAN@ start\n@HOME@/x"
    out = cli.render_unit(text, repo=Path("/r"), python="/p/py", podman="/pod", home=Path("/h"))
    assert "ExecStart=/p/py -m relay serve" in out
    assert "WorkingDirectory=/r/backend" in out
    assert "/pod start" in out
    assert "/h/x" in out
    assert "@" not in out


def test_render_units_writes_service(tmp_path: Path) -> None:
    out_dir = tmp_path / "systemd"
    written = cli.render_units(
        out_dir,
        repo=Path("/repo"),
        python="/repo/.venv/bin/python",
        podman="/usr/bin/podman",
    )
    assert [p.name for p in written] == ["relay.service"]
    text = (out_dir / "relay.service").read_text()
    assert "ExecStart=/repo/.venv/bin/python -m relay serve" in text
    assert "WorkingDirectory=/repo/backend" in text
    assert "PYTHONPATH=/repo/backend" in text
    # These hardening options break rootless podman. They must not appear as directives.
    directives = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any(ln.startswith("NoNewPrivileges") for ln in directives)
    assert not any(ln.startswith("ProtectHome") for ln in directives)
    assert "@REPO@" not in text and "@PYTHON@" not in text


def test_render_units_with_containers(tmp_path: Path) -> None:
    out_dir = tmp_path / "systemd"
    written = cli.render_units(
        out_dir,
        repo=Path("/repo"),
        python="/repo/.venv/bin/python",
        podman="/usr/bin/podman",
        containers=True,
    )
    names = [p.name for p in written]
    assert names == ["relay.service", "relay-containers.service"]
    ctext = (out_dir / "relay-containers.service").read_text()
    assert "/usr/bin/podman start job-work1 job-work2" in ctext
    assert "/usr/bin/podman stop -t 20 job-work1 job-work2" in ctext
    assert "Type=oneshot" in ctext
    assert "/run/docker.sock" in ctext


def test_deploy_templates_exist() -> None:
    assert (cli.DEPLOY_DIR / "relay.service.in").exists()
    assert (cli.DEPLOY_DIR / "relay-containers.service.in").exists()


# -- wrapper script ----------------------------------------------------------

def test_wrapper_exists_and_executable() -> None:
    wrapper = paths.REPO_ROOT / "bin" / "relay"
    assert wrapper.exists()
    mode = wrapper.stat().st_mode
    assert mode & stat.S_IXUSR, "bin/relay must be executable"
    text = wrapper.read_text()
    assert "readlink -f" in text
    assert "PYTHONPATH=" in text
    assert "-m relay" in text


# -- main dispatch -----------------------------------------------------------

def test_main_url_prints(settings: Settings, monkeypatch, capsys) -> None:
    settings.token_path.write_text("tok123")
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)
    code = cli.main(["url"])
    out = capsys.readouterr().out.strip()
    assert code == 0
    assert out == f"http://127.0.0.1:{settings.port}/#token=tok123"


def test_main_reports_cli_error(settings: Settings, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "load_settings", lambda _path: settings)
    code = cli.main(["url"])  # no token file -> CliError
    err = capsys.readouterr().err
    assert code == 2
    assert "relay:" in err
