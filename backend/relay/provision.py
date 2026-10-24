"""Provision the relay hook files inside each environment's home.

relay writes three things on the host, under an env's ``host_home``. Because
the host home is bind-mounted into the container, the container sees them
under ``<home>/.local/...`` owned by ``node``:

- ``.local/share/relay/hook.py`` (0700): the event hook script.
- ``.local/share/relay/claude-settings.json`` (0600): the ``--settings`` file a
  dashboard-launched Claude gets, wiring every hook event to the script.
- ``.local/state/relay/inbox/`` (0700): the directory the host polls.

Only files whose content differs are rewritten, so provisioning is cheap to
repeat. Every target is checked for a symlink and refused if it is one, so a
planted symlink can never redirect a write outside the home.

relay never touches the global ``~/.claude/settings.json`` or Codex ``notify``
here. That is the job of ``relay hooks install`` (module ``hooks_install``), an
explicit opt-in. Provisioning does ensure one Codex setting: the native
``thread-id`` terminal title item in ``~/.codex/config.toml``, which relay
needs to link every Codex pane (``hooks_install.codex_title_install``).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .adapters import hook_script
from .config import EnvConfig

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime

# Hook events and whether the handler runs async (non-blocking). The observing
# events run async so they never delay the agent; the input and stop events run
# synchronously so their state is recorded promptly.
_HOOK_EVENTS: tuple[tuple[str, bool], ...] = (
    ("SessionStart", False),
    ("UserPromptSubmit", False),
    ("PreToolUse", True),
    ("PermissionRequest", False),
    ("PostToolUse", True),
    ("PostToolUseFailure", True),
    ("PermissionDenied", False),
    ("Notification", False),
    ("Stop", False),
    ("StopFailure", False),
    ("SessionEnd", False),
)
_HOOK_TIMEOUT = 10
_RELAY_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _hook_script_source() -> str:
    return Path(hook_script.__file__).read_text(encoding="utf-8")


def claude_settings_content(env: EnvConfig) -> str:
    """The JSON body of the ``--settings`` file for a dashboard Claude session."""
    hooks: dict[str, list[dict[str, Any]]] = {}
    for event, is_async in _HOOK_EVENTS:
        handler: dict[str, Any] = {
            "type": "command",
            "command": "python3",
            "args": [env.hook_script, "claude"],
            "timeout": _HOOK_TIMEOUT,
        }
        if is_async:
            handler["async"] = True
        hooks[event] = [{"hooks": [handler]}]
    return json.dumps({"hooks": hooks}, indent=2, sort_keys=True) + "\n"


def codex_notify_override(env: EnvConfig, relay_session_id: str | None = None) -> str:
    """The ``notify = [...]`` TOML assignment passed to ``codex -c``.

    Built with ``json.dumps`` so the array literal quotes each element
    correctly. TOML and JSON share this array-of-strings syntax.

    ``relay_session_id`` adds ``--relay-session-id=<id>`` before the payload
    that Codex appends. The shared Codex daemon runs ``notify`` without the
    session's environment, so this argument is how the event names its row.
    """
    argv = ["python3", env.hook_script, "codex"]
    if relay_session_id is not None:
        if not _RELAY_ID_RE.match(relay_session_id):
            raise ValueError("relay session id must be 32 lowercase hex characters")
        argv.append(f"--relay-session-id={relay_session_id}")
    return "notify=" + json.dumps(argv)


def _refuse_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ProvisionError(f"refusing to write through a symlink: {path}")


class ProvisionError(RuntimeError):
    pass


def _ensure_dir(path: Path, mode: int) -> bool:
    """Create ``path`` with ``mode`` if missing. Returns True when it changed."""
    _refuse_symlink(path)
    if path.is_dir():
        if (path.stat().st_mode & 0o777) != mode:
            os.chmod(path, mode)
            return True
        return False
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return True


def _write_if_changed(path: Path, content: str, mode: int) -> bool:
    """Write ``content`` to ``path`` only when it differs. Returns True on write."""
    _refuse_symlink(path)
    if path.exists():
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            current = None
        if current == content:
            if (path.stat().st_mode & 0o777) != mode:
                os.chmod(path, mode)
                return True
            return False
    parent = path.parent
    _refuse_symlink(parent)
    parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def provision_env(rt: "Runtime", env_id: str) -> dict[str, Any]:
    """Write the hook artifacts for one env. Records the result in env_status."""
    result: dict[str, Any] = {"ok": False, "changed": False, "error": None}
    try:
        env = rt.settings.env(env_id)
    except KeyError as exc:
        result["error"] = str(exc)
        _store(rt, env_id, result)
        return result

    try:
        share_dir = env.host_share_dir
        inbox_dir = env.host_inbox_dir
        hook_path = share_dir / "hook.py"
        settings_path = share_dir / "claude-settings.json"

        changed = False
        changed |= _ensure_dir(share_dir, 0o700)
        changed |= _write_if_changed(hook_path, _hook_script_source(), 0o700)
        changed |= _write_if_changed(settings_path, claude_settings_content(env), 0o600)
        changed |= _ensure_dir(inbox_dir, 0o700)

        result["ok"] = True
        result["changed"] = changed
    except (ProvisionError, OSError) as exc:
        result["error"] = str(exc)
    result["codex_title"] = _ensure_codex_title(env)
    result["changed"] = bool(result["changed"] or result["codex_title"].get("changed"))
    _store(rt, env_id, result)
    return result


def _ensure_codex_title(env: EnvConfig) -> dict[str, Any]:
    """The native thread-id title item in the env's Codex config (see ``hooks_install``)."""
    from .hooks_install import codex_title_install  # hooks_install imports this module

    try:
        return codex_title_install(env)
    except (OSError, ValueError) as exc:
        return {"changed": False, "terminal_title": None, "error": str(exc)}


def _store(rt: "Runtime", env_id: str, result: dict[str, Any]) -> None:
    status = rt.env_status.get(env_id)
    if status is not None:
        status.provision = result


async def provision_all(rt: "Runtime") -> dict[str, dict[str, Any]]:
    """Provision every configured environment. Runs synchronously per env."""
    out: dict[str, dict[str, Any]] = {}
    for env_id in rt.settings.envs:
        out[env_id] = provision_env(rt, env_id)
    return out
