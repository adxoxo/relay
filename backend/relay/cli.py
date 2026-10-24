"""relay command line interface.

The CLI is the laptop side of relay. It runs the service, prints the login
URL and token, launches and attaches agent sessions, and installs the systemd
user units. Commands that talk to a running service use a small httpx client
against the loopback port with a bearer token read from ``settings.token_path``.

Nothing here uses a host shell. Every subprocess and every ``os.exec`` uses an
argument vector, so a session name or project path can never become shell
syntax.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import paths
from .config import Settings, load_settings

# Template placeholders that ``render_unit`` replaces with absolute paths.
PLACEHOLDER_REPO = "@REPO@"
PLACEHOLDER_PYTHON = "@PYTHON@"
PLACEHOLDER_PODMAN = "@PODMAN@"
PLACEHOLDER_HOME = "@HOME@"

DEPLOY_DIR = paths.REPO_ROOT / "deploy"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"


class CliError(Exception):
    """A user-facing error. ``main`` prints the message and returns exit code 2."""


# -- token and settings ------------------------------------------------------

def read_token(settings: Settings) -> str:
    """Return the access token from ``settings.token_path``.

    Raise ``CliError`` with a clear hint when the file is missing. The token is
    created by the service (``Auth.ensure_token``) or by ``relay token``.
    """
    path = settings.token_path
    if not path.exists():
        raise CliError(
            f"No token found at {path}.\n"
            "Start the service once (relay serve) or run 'relay token' to create it."
        )
    token = path.read_text().strip()
    if not token:
        raise CliError(f"Token file {path} is empty. Run 'relay token --rotate'.")
    return token


def base_url(settings: Settings) -> str:
    return f"http://127.0.0.1:{settings.port}"


# -- HTTP client -------------------------------------------------------------

@dataclass
class ApiResult:
    status: int
    data: Any


class ApiClient:
    """A tiny httpx wrapper that adds the bearer token and one clear error."""

    def __init__(self, settings: Settings, token: str | None = None):
        self.settings = settings
        self.base = base_url(settings)
        self.token = token if token is not None else read_token(settings)

    def _request(self, method: str, path: str, *, json_body: Any | None = None) -> ApiResult:
        import httpx

        url = f"{self.base}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            resp = httpx.request(method, url, headers=headers, json=json_body, timeout=15.0)
        except httpx.ConnectError as exc:
            raise CliError(
                f"Cannot reach the relay service at {self.base}.\n"
                "Start it with 'relay serve' or 'relay service install', then retry."
            ) from exc
        except httpx.HTTPError as exc:
            raise CliError(f"Request to {url} failed: {exc}") from exc
        try:
            data = resp.json() if resp.content else None
        except ValueError:
            data = None
        return ApiResult(resp.status_code, data)

    def get(self, path: str) -> ApiResult:
        return self._request("GET", path)

    def post(self, path: str, body: Any | None = None) -> ApiResult:
        return self._request("POST", path, json_body=body)

    def state(self) -> dict[str, Any]:
        result = self.get("/api/state")
        if result.status != 200 or not isinstance(result.data, dict):
            raise CliError(_api_error("Cannot read session state", result))
        return result.data


def _api_error(prefix: str, result: ApiResult) -> str:
    if isinstance(result.data, dict) and result.data.get("message"):
        return f"{prefix} ({result.status}): {result.data['message']}"
    return f"{prefix} (HTTP {result.status})."


# -- session resolution (pure, tested) ---------------------------------------

def resolve_session(sessions: Sequence[dict[str, Any]], env: str, needle: str) -> dict[str, Any]:
    """Find one live session in ``env`` by exact id, id prefix, or case-insensitive name.

    Resolution order:
    1. exact session id,
    2. case-insensitive name match among live sessions of the env,
    3. unique id prefix among live sessions of the env.

    Raise ``CliError`` when nothing matches or a name or prefix is ambiguous.
    """
    live_in_env = [
        s for s in sessions
        if s.get("env") == env and s.get("status") == "live"
    ]
    # 1. exact id (across the whole env list, live only).
    for s in live_in_env:
        if s.get("id") == needle:
            return s

    lowered = needle.lower()
    # 2. case-insensitive name match.
    by_name = [s for s in live_in_env if str(s.get("name", "")).lower() == lowered]
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise CliError(f"Name {needle!r} matches {len(by_name)} live sessions in {env}. Use the id.")

    # 3. unique id prefix.
    by_prefix = [s for s in live_in_env if str(s.get("id", "")).startswith(needle)]
    if len(by_prefix) == 1:
        return by_prefix[0]
    if len(by_prefix) > 1:
        raise CliError(f"Id prefix {needle!r} matches {len(by_prefix)} live sessions in {env}. Use more characters.")

    raise CliError(f"No live session named or id-prefixed {needle!r} in {env}.")


def build_url(settings: Settings, token: str, *, tailnet: bool) -> str:
    """Return the login URL with the token in the fragment.

    The token is in the URL fragment (``#token=``) so it is never sent to the
    server in a request line or logged by a proxy.
    """
    if tailnet:
        host = settings.tailscale_hostname
        if not host:
            raise CliError(
                "No tailscale_hostname is configured.\n"
                "Set server.tailscale_hostname in the config, or drop --tailnet for the local URL."
            )
        return f"https://{host}/#token={token}"
    return f"http://127.0.0.1:{settings.port}/#token={token}"


# -- service unit rendering (pure, tested) -----------------------------------

def render_unit(
    template_text: str,
    *,
    repo: Path,
    python: str,
    podman: str,
    home: Path | None = None,
) -> str:
    """Replace the ``@...@`` placeholders in a unit template with absolute paths."""
    out = template_text.replace(PLACEHOLDER_REPO, str(repo))
    out = out.replace(PLACEHOLDER_PYTHON, python)
    out = out.replace(PLACEHOLDER_PODMAN, podman)
    out = out.replace(PLACEHOLDER_HOME, str(home if home is not None else Path.home()))
    return out


def render_units(
    out_dir: Path,
    *,
    repo: Path,
    python: str,
    podman: str,
    home: Path | None = None,
    containers: bool = False,
    deploy_dir: Path | None = None,
) -> list[Path]:
    """Render the service templates into ``out_dir`` and return the written paths.

    Pure with respect to systemd: it only reads templates and writes files. The
    caller runs ``systemctl`` separately.
    """
    src_dir = deploy_dir if deploy_dir is not None else DEPLOY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    names = ["relay.service"]
    if containers:
        names.append("relay-containers.service")
    written: list[Path] = []
    for name in names:
        template = src_dir / f"{name}.in"
        text = template.read_text()
        rendered = render_unit(text, repo=repo, python=python, podman=podman, home=home)
        target = out_dir / name
        target.write_text(rendered)
        written.append(target)
    return written


# -- output helpers ----------------------------------------------------------

def _print(text: str = "") -> None:
    print(text)


def _format_table(rows: Sequence[Sequence[str]], headers: Sequence[str]) -> str:
    columns = list(zip(*([headers, *rows]))) if rows else [[h] for h in headers]
    widths = [max(len(str(cell)) for cell in col) for col in columns]
    line = "  ".join(str(h).ljust(w) for h, w in zip(headers, widths))
    out = [line]
    for row in rows:
        out.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))
    return "\n".join(out)


# -- commands ----------------------------------------------------------------

def cmd_serve(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    host = args.host or settings.host
    port = args.port or settings.port
    uvicorn.run(
        "relay.app:create_app",
        factory=True,
        host=host,
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        # A phone on a relayed tailnet path can stall for many seconds. Keep
        # protocol pings, but allow a slow pong before the socket is dropped.
        ws_ping_interval=20.0,
        ws_ping_timeout=30.0,
        log_level="info",
    )
    return 0


def cmd_token(args: argparse.Namespace, settings: Settings) -> int:
    if args.rotate:
        # Import lazily: auth belongs to the web builder and pulls in fastapi.
        from .auth import Auth
        from .db import Database

        db = Database(settings.db_path)
        try:
            token = Auth(settings, db).rotate_token()
        finally:
            db.close()
        _print(token)
        return 0
    _print(read_token(settings))
    return 0


def cmd_url(args: argparse.Namespace, settings: Settings) -> int:
    token = read_token(settings)
    _print(build_url(settings, token, tailnet=args.tailnet))
    return 0


def cmd_ls(args: argparse.Namespace, settings: Settings) -> int:
    client = ApiClient(settings)
    state = client.state()
    sessions = list(state.get("sessions", []))
    if args.env:
        sessions = [s for s in sessions if s.get("env") == args.env]
    if args.json:
        _print(json.dumps(sessions, indent=2))
        return 0
    rows = [
        [
            str(s.get("id", ""))[:8],
            str(s.get("env", "")),
            str(s.get("name", "")),
            str(s.get("kind", "")),
            str(s.get("status", "")),
            str(s.get("state", "")),
            str(s.get("project", "") or ""),
        ]
        for s in sessions
    ]
    _print(_format_table(rows, ["ID", "ENV", "NAME", "KIND", "STATUS", "STATE", "PROJECT"]))
    return 0


def cmd_new(args: argparse.Namespace, settings: Settings) -> int:
    body: dict[str, Any] = {"env": args.env, "kind": args.kind}
    if args.name:
        body["name"] = args.name
    if args.project is not None:
        body["project"] = args.project
    if args.prompt is not None:
        body["prompt"] = args.prompt
    if args.permission_mode is not None:
        body["permission_mode"] = args.permission_mode
    client = ApiClient(settings)
    result = client.post("/api/sessions", body)
    if result.status != 201 or not isinstance(result.data, dict):
        raise CliError(_api_error("Cannot create the session", result))
    session = result.data
    _print(f"created {session.get('name')} ({str(session.get('id'))[:8]}) in {session.get('env')}")
    if args.attach:
        return _attach_session(settings, args.env, session)
    return 0


def cmd_attach(args: argparse.Namespace, settings: Settings) -> int:
    client = ApiClient(settings)
    state = client.state()
    session = resolve_session(state.get("sessions", []), args.env, args.name_or_id)
    return _attach_session(settings, args.env, session)


def _attach_session(settings: Settings, env_id: str, session: dict[str, Any]) -> int:
    """Replace this process with a tmux client attached to ``session``."""
    from .podman import Podman, podman_environment
    from .tmux import Tmux

    tmux_info = session.get("tmux") or {}
    session_id = tmux_info.get("session_id")
    socket = tmux_info.get("socket")
    if not session_id or not socket:
        raise CliError("The session has no live tmux identity to attach to.")
    try:
        env = settings.env(env_id)
    except KeyError as exc:
        raise CliError(str(exc)) from exc
    argv = Tmux(Podman(settings.podman_binary), env).attach_argv(socket, session_id)
    # os.execvpe replaces the CLI process, so the terminal talks straight to tmux.
    os.execvpe(argv[0], argv, podman_environment())


def cmd_end(args: argparse.Namespace, settings: Settings) -> int:
    client = ApiClient(settings)
    state = client.state()
    session = resolve_session(state.get("sessions", []), args.env, args.name_or_id)
    tmux_info = session.get("tmux") or {}
    if not args.yes:
        name = session.get("name")
        if not _confirm(f"End session {name} in {args.env}?"):
            _print("cancelled")
            return 1
    body = {
        "tmux_session_id": tmux_info.get("session_id"),
        "tmux_created": tmux_info.get("created"),
        "pane_count": tmux_info.get("pane_count"),
    }
    result = client.post(f"/api/sessions/{session.get('id')}/end", body)
    if result.status != 200 or not isinstance(result.data, dict):
        raise CliError(_api_error("Cannot end the session", result))
    already = result.data.get("already")
    _print("already ended" if already else "ended")
    return 0


def cmd_hooks(args: argparse.Namespace, settings: Settings) -> int:
    # hooks_install belongs to the adapters builder. Import lazily so the rest
    # of the CLI works before that module lands.
    try:
        from . import hooks_install
    except ImportError as exc:
        raise CliError(f"Hook installation is not available yet: {exc}") from exc

    try:
        env = settings.env(args.env)
    except KeyError as exc:
        raise CliError(str(exc)) from exc

    _print(
        "Note: 'relay hooks' edits this environment's ~/.claude/settings.json and "
        "~/.codex/config.toml (a backup is kept). It is optional. It affects "
        "laptop-started Claude and Codex sessions, and it adds the thread id to "
        "the Codex terminal title so relay can link every Codex pane."
    )
    if args.action == "install":
        result = {
            "claude": hooks_install.install(env, dry_run=args.dry_run),
            "codex": hooks_install.codex_install(env, dry_run=args.dry_run),
        }
    elif args.action == "uninstall":
        result = {
            "claude": hooks_install.uninstall(env),
            "codex": hooks_install.codex_uninstall(env),
        }
    else:
        result = {
            "claude": hooks_install.status(env),
            "codex": hooks_install.codex_status(env),
        }
    _print(json.dumps(result, indent=2, default=str))
    return 0


def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    checks = list(run_doctor(settings))
    worst = 0
    for check in checks:
        _print(f"[{check.level:<4}] {check.name}: {check.detail}")
        worst = max(worst, {"OK": 0, "WARN": 1, "FAIL": 2}[check.level])
    return 0 if worst < 2 else 1


def cmd_service(args: argparse.Namespace, settings: Settings) -> int:
    if args.action == "install":
        return _service_install(args, settings)
    if args.action == "uninstall":
        return _service_uninstall(args, settings)
    return _service_status(args, settings)


# -- doctor checks -----------------------------------------------------------

@dataclass
class Check:
    name: str
    level: str  # 'OK' | 'WARN' | 'FAIL'
    detail: str


def run_doctor(settings: Settings) -> Iterable[Check]:
    podman_bin = shutil.which(settings.podman_binary)
    if podman_bin:
        yield Check("podman", "OK", podman_bin)
    else:
        yield Check("podman", "FAIL", f"{settings.podman_binary} not found on PATH")

    for env_id, env in settings.envs.items():
        yield _check_container(settings, env_id, env, podman_bin)

    for env_id, env in settings.envs.items():
        share = env.host_share_dir
        hook = share / "hook.py"
        settings_json = share / "claude-settings.json"
        if hook.exists() and settings_json.exists():
            yield Check(f"provisioning {env_id}", "OK", str(share))
        else:
            yield Check(f"provisioning {env_id}", "WARN", f"missing files under {share} (run 'relay hooks install')")

    dist = settings.frontend_dist
    if (dist / "index.html").exists():
        yield Check("frontend build", "OK", str(dist))
    else:
        yield Check("frontend build", "WARN", f"no build at {dist} (run 'npm run build' in frontend/)")

    if settings.token_path.exists() and settings.token_path.read_text().strip():
        yield Check("token", "OK", str(settings.token_path))
    else:
        yield Check("token", "WARN", "no token yet (run 'relay token' or start the service)")

    yield _check_service(settings)

    browser = settings.browser_executable
    if browser and Path(browser).exists():
        yield Check("browser for checks", "OK", browser)
    else:
        yield Check("browser for checks", "WARN", "no Chromium-compatible browser found (checks disabled)")

    if shutil.which("tailscale"):
        yield Check("tailscale", "OK", "installed")
    else:
        yield Check("tailscale", "WARN", "not installed (needed for remote phone access)")


def _check_container(settings: Settings, env_id: str, env: Any, podman_bin: str | None) -> Check:
    if not podman_bin:
        return Check(f"container {env_id}", "FAIL", "podman not available")
    from .podman import podman_environment

    try:
        proc = subprocess.run(
            [settings.podman_binary, "container", "inspect", "-f", "{{.State.Status}}", env.container],
            capture_output=True,
            text=True,
            timeout=15,
            env=podman_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(f"container {env_id}", "FAIL", f"inspect failed: {exc}")
    if proc.returncode != 0:
        return Check(f"container {env_id}", "FAIL", f"{env.container} not found")
    status = proc.stdout.strip()
    if status == "running":
        return Check(f"container {env_id}", "OK", f"{env.container} running")
    return Check(f"container {env_id}", "WARN", f"{env.container} is {status or 'stopped'}")


def _check_service(settings: Settings) -> Check:
    import httpx

    url = f"{base_url(settings)}/api/health"
    try:
        resp = httpx.get(url, timeout=5.0)
    except httpx.HTTPError:
        return Check("service", "WARN", f"not reachable at {url} (start with 'relay serve')")
    if resp.status_code == 200:
        return Check("service", "OK", url)
    return Check("service", "WARN", f"unhealthy at {url} (HTTP {resp.status_code})")


# -- service install helpers -------------------------------------------------

def _linger_enabled(user: str | None = None) -> bool:
    user = user or os.environ.get("USER") or ""
    try:
        proc = subprocess.run(
            ["loginctl", "show-user", user, "-p", "Linger"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.stdout.strip().lower() == "linger=yes"


def _systemctl(*args: str) -> None:
    try:
        subprocess.run(["systemctl", "--user", *args], check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CliError(f"systemctl --user {' '.join(args)} failed: {exc}") from exc


def _service_install(args: argparse.Namespace, settings: Settings) -> int:
    python = str(paths.REPO_ROOT / ".venv" / "bin" / "python")
    podman = shutil.which(settings.podman_binary) or settings.podman_binary
    written = render_units(
        SYSTEMD_USER_DIR,
        repo=paths.REPO_ROOT,
        python=python,
        podman=podman,
        containers=args.containers,
    )
    for path in written:
        _print(f"wrote {path}")
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", "relay.service")
    _print("enabled and started relay.service")

    if args.containers:
        if _linger_enabled():
            _systemctl("enable", "--now", "relay-containers.service")
            _print("enabled and started relay-containers.service")
        else:
            user = os.environ.get("USER") or "$USER"
            _print(
                "relay-containers.service is installed but NOT enabled.\n"
                "Reason: without linger the user manager stops at logout and would stop the containers.\n"
                f"Enable linger first, then enable the unit:\n"
                f"  sudo loginctl enable-linger {user}\n"
                "  systemctl --user enable --now relay-containers.service"
            )
    return 0


def _service_uninstall(args: argparse.Namespace, settings: Settings) -> int:
    for unit in ("relay.service", "relay-containers.service"):
        target = SYSTEMD_USER_DIR / unit
        if target.exists():
            try:
                subprocess.run(["systemctl", "--user", "disable", "--now", unit], timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
            target.unlink()
            _print(f"removed {target}")
    _systemctl("daemon-reload")
    _print("uninstalled relay services")
    return 0


def _service_status(args: argparse.Namespace, settings: Settings) -> int:
    for unit in ("relay.service", "relay-containers.service"):
        if not (SYSTEMD_USER_DIR / unit).exists():
            _print(f"{unit}: not installed")
            continue
        try:
            proc = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True,
                text=True,
                timeout=15,
            )
            _print(f"{unit}: {proc.stdout.strip() or 'unknown'}")
        except (OSError, subprocess.SubprocessError) as exc:
            _print(f"{unit}: status unavailable ({exc})")
    return 0


# -- confirmation ------------------------------------------------------------

def _confirm(question: str) -> bool:
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


# -- argument parser ---------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="relay", description="Private dashboard for agent sessions.")
    parser.add_argument("--config", type=Path, default=None, help="Path to a config.toml (default: auto).")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="Run the web service (uvicorn).")
    p_serve.add_argument("--host", default=None, help="Bind host (loopback only; default from config).")
    p_serve.add_argument("--port", type=int, default=None, help="Bind port (default from config).")
    p_serve.set_defaults(func=cmd_serve)

    p_token = sub.add_parser("token", help="Print the access token, or rotate it.")
    p_token.add_argument("--rotate", action="store_true", help="Create a new token and drop all sessions.")
    p_token.set_defaults(func=cmd_token)

    p_url = sub.add_parser("url", help="Print the login URL with the token.")
    p_url.add_argument("--tailnet", action="store_true", help="Use the Tailscale https hostname.")
    p_url.set_defaults(func=cmd_url)

    p_ls = sub.add_parser("ls", help="List sessions.")
    p_ls.add_argument("--env", default=None, help="Filter by environment id.")
    p_ls.add_argument("--json", action="store_true", help="Print JSON instead of a table.")
    p_ls.set_defaults(func=cmd_ls)

    p_new = sub.add_parser("new", help="Create a session.")
    p_new.add_argument("env", help="Environment id (for example work1).")
    p_new.add_argument("kind", choices=["claude", "codex", "shell"], help="Session kind.")
    p_new.add_argument("--name", default=None, help="Session name (unique per env).")
    p_new.add_argument("--project", default=None, help="Project path relative to the projects root.")
    p_new.add_argument("--prompt", default=None, help="Initial prompt for an agent.")
    p_new.add_argument(
        "--permission-mode",
        default=None,
        choices=["manual", "acceptEdits", "auto", "plan"],
        help="Claude permission mode.",
    )
    p_new.add_argument("--attach", action="store_true", help="Attach after creating.")
    p_new.set_defaults(func=cmd_new)

    p_attach = sub.add_parser("attach", help="Attach a terminal to a live session.")
    p_attach.add_argument("env", help="Environment id.")
    p_attach.add_argument("name_or_id", help="Session name or id prefix.")
    p_attach.set_defaults(func=cmd_attach)

    p_end = sub.add_parser("end", help="End a live session.")
    p_end.add_argument("env", help="Environment id.")
    p_end.add_argument("name_or_id", help="Session name or id prefix.")
    p_end.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
    p_end.set_defaults(func=cmd_end)

    p_hooks = sub.add_parser("hooks", help="Install or remove Claude hooks in an environment (optional).")
    p_hooks.add_argument("action", choices=["install", "uninstall", "status"])
    p_hooks.add_argument("--env", required=True, help="Environment id.")
    p_hooks.add_argument("--dry-run", action="store_true", help="Show changes without writing.")
    p_hooks.set_defaults(func=cmd_hooks)

    p_doctor = sub.add_parser("doctor", help="Check the environment and the service.")
    p_doctor.set_defaults(func=cmd_doctor)

    p_service = sub.add_parser("service", help="Install, remove, or query the systemd user units.")
    p_service.add_argument("action", choices=["install", "uninstall", "status"])
    p_service.add_argument(
        "--containers",
        action="store_true",
        help="Also install the unit that starts job-work1 and job-work2.",
    )
    p_service.set_defaults(func=cmd_service)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = load_settings(args.config)
    except Exception as exc:  # config errors should be readable, not a traceback
        print(f"relay: configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        return args.func(args, settings)
    except CliError as exc:
        print(f"relay: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
