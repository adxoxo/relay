"""Install relay hooks into an env's global Claude settings, on explicit opt-in.

Dashboard-launched Claude sessions get relay hooks through ``--settings``
(``provision.py``). A laptop-started session reads the global
``<home>/.claude/settings.json`` instead. ``relay hooks install`` merges relay
hook entries into that file so those sessions also report events.

Rules:

- Back up the file to ``settings.json.relay-backup-<ts>`` before a change.
- Merge relay entries (identified by the hook script path in ``args``) into the
  existing ``hooks`` map without touching other hooks.
- Write JSON with ``indent=2`` and keep the file's original mode.
- Idempotent: a second install makes no change and writes no backup.
- ``uninstall`` removes only relay entries. ``status`` reports the state.
"""

from __future__ import annotations

import json
import os
import re
import time
import tomllib
from pathlib import Path
from typing import Any

from .config import EnvConfig
from .provision import _HOOK_EVENTS, _HOOK_TIMEOUT


def _settings_path(env: EnvConfig) -> Path:
    return env.host_home / ".claude" / "settings.json"


def _relay_handler(env: EnvConfig, is_async: bool) -> dict[str, Any]:
    handler: dict[str, Any] = {
        "type": "command",
        "command": "python3",
        "args": [env.hook_script, "claude"],
        "timeout": _HOOK_TIMEOUT,
    }
    if is_async:
        handler["async"] = True
    return handler


def _is_relay_handler(env: EnvConfig, handler: Any) -> bool:
    if not isinstance(handler, dict):
        return False
    args = handler.get("args")
    return isinstance(args, list) and env.hook_script in args


def _is_relay_group(env: EnvConfig, group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    handlers = group.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(_is_relay_handler(env, h) for h in handlers)


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _strip_relay(env: EnvConfig, settings: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``settings`` with relay hook groups removed."""
    result = json.loads(json.dumps(settings))
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result
    for event in list(hooks.keys()):
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = [g for g in groups if not _is_relay_group(env, g)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        result.pop("hooks", None)
    return result


def _with_relay(env: EnvConfig, settings: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with relay hook groups merged in, other hooks untouched."""
    result = _strip_relay(env, settings)
    hooks = result.setdefault("hooks", {})
    for event, is_async in _HOOK_EVENTS:
        group = {"hooks": [_relay_handler(env, is_async)]}
        existing = hooks.get(event)
        if isinstance(existing, list):
            existing.append(group)
        else:
            hooks[event] = [group]
    return result


def status(env: EnvConfig) -> dict[str, Any]:
    """Report whether relay hooks are installed in the global settings file."""
    path = _settings_path(env)
    settings = _load(path)
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    installed = False
    if isinstance(hooks, dict):
        for groups in hooks.values():
            if isinstance(groups, list) and any(_is_relay_group(env, g) for g in groups):
                installed = True
                break
    return {"path": str(path), "exists": path.exists(), "installed": installed}


def _write(path: Path, settings: dict[str, Any], mode: int) -> None:
    content = json.dumps(settings, indent=2, sort_keys=True) + "\n"
    tmp = path.with_name(f".{path.name}.relay-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _backup(path: Path, mode: int) -> str | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.relay-backup-{int(time.time())}")
    backup.write_bytes(path.read_bytes())
    os.chmod(backup, mode)
    return str(backup)


def install(env: EnvConfig, *, dry_run: bool = False) -> dict[str, Any]:
    """Merge relay hooks into the global settings file. Idempotent."""
    path = _settings_path(env)
    current = _load(path)
    desired = _with_relay(env, current)
    changed = desired != current

    if not changed:
        return {"changed": False, "path": str(path), "backup": None, "dry_run": dry_run}
    if dry_run:
        return {"changed": True, "path": str(path), "backup": None, "dry_run": True}

    mode = (os.stat(path).st_mode & 0o777) if path.exists() else 0o600
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(path, mode)
    _write(path, desired, mode)
    return {"changed": True, "path": str(path), "backup": backup, "dry_run": False}


def uninstall(env: EnvConfig) -> dict[str, Any]:
    """Remove relay hooks from the global settings file. Idempotent."""
    path = _settings_path(env)
    if not path.exists():
        return {"changed": False, "path": str(path), "backup": None}
    current = _load(path)
    desired = _strip_relay(env, current)
    if desired == current:
        return {"changed": False, "path": str(path), "backup": None}
    mode = os.stat(path).st_mode & 0o777
    backup = _backup(path, mode)
    _write(path, desired, mode)
    return {"changed": True, "path": str(path), "backup": backup}


# -- Codex notify --------------------------------------------------------------
#
# A hand-started Codex session reads ``notify`` from its global
# ``~/.codex/config.toml``. relay sets that key to run the same hook script,
# so a by-hand Codex reports turn-complete events just like a relay-started one.
#
# The writer is line based on purpose. It never parses or rewrites the whole
# file, so it cannot drop a secret, a comment, or any other key. TOML requires
# a top-level key to sit before the first ``[table]`` header, so relay inserts
# or replaces ``notify`` only inside that top-level region.

_NOTIFY_MARKER = "# relay-managed notify"
_TABLE_HEADER = re.compile(r"^\s*\[")
_NOTIFY_ASSIGN = re.compile(r"^\s*notify\s*=")


def _codex_config_path(env: EnvConfig) -> Path:
    return env.host_codex_config


def codex_notify_line(env: EnvConfig) -> str:
    """The exact ``notify = [...]`` line relay writes, with its marker."""
    argv = ["python3", env.hook_script, "codex"]
    return f"notify = {json.dumps(argv)}  {_NOTIFY_MARKER}"


def _is_relay_notify(line: str) -> bool:
    return _NOTIFY_MARKER in line


def _is_toplevel_notify(line: str) -> bool:
    return bool(_NOTIFY_ASSIGN.match(line))


def _top_region_end(lines: list[str]) -> int:
    """Index of the first ``[table]`` header, or len(lines) if there is none."""
    for i, line in enumerate(lines):
        if _TABLE_HEADER.match(line):
            return i
    return len(lines)


def _desired_codex_lines(env: EnvConfig, lines: list[str]) -> list[str]:
    """Return the file lines with relay's ``notify`` set in the top-level region.

    Replaces a relay-managed notify in place. Removes a plain top-level notify
    only inside the top region and adds relay's line. Leaves everything else,
    including tables and their keys, untouched.
    """
    notify_line = codex_notify_line(env)
    end = _top_region_end(lines)
    out: list[str] = []
    inserted = False
    for i, line in enumerate(lines):
        if i < end and (_is_relay_notify(line) or _is_toplevel_notify(line)):
            if not inserted:
                out.append(notify_line)
                inserted = True
            continue  # drop the old notify line (relay or plain)
        out.append(line)
    if not inserted:
        # Insert at the end of the top-level region.
        end = _top_region_end(out)
        # Keep one blank line of separation if the region is non-empty and the
        # line before the table is not already blank.
        insert_at = end
        out.insert(insert_at, notify_line)
    return out


def _strip_codex_notify(lines: list[str]) -> list[str]:
    """Remove relay's managed notify line only. Leave a user's own notify."""
    return [line for line in lines if not _is_relay_notify(line)]


# -- Codex terminal title ---------------------------------------------------------
#
# A Codex TUI writes its terminal title to its own pty, and tmux keeps it as the
# pane title. With the native ``thread-id`` item as the last item, the title
# ends with the thread the TUI shows, so relay can link a pane that started
# without relay (the shared Codex daemon runs notify and hooks without the
# pane's identity; ``codex_runtime`` reads only that last field). Without a
# user setting, relay writes Codex 0.160's default items (``activity``,
# ``thread-name``, ``project-name``) plus ``thread-id``. A user's own list keeps
# its items and order and gains ``thread-id`` as the last item, unless its last
# item is already ``thread-id`` or ``session-id``; the user's original lines
# stay in the file as ``# relay-original`` comments, and uninstall restores
# them exactly. A TUI reads the setting at start and at each ``/new`` or
# ``/resume``. Service startup provisions this for every env
# (``provision.provision_env``).

_TITLE_MARKER = "# relay-managed terminal title"
_ORIGINAL_PREFIX = "# relay-original "
DEFAULT_TITLE_ITEMS = ("activity", "thread-name", "project-name")
TITLE_ID_ITEM = "thread-id"
_TITLE_ID_ITEMS = ("thread-id", "session-id")
_TUI_HEADER = re.compile(r"^\s*\[\s*tui\s*\]\s*(#.*)?$")
_ANY_HEADER = re.compile(r"^\s*\[")
_TITLE_ASSIGN = re.compile(r"^\s*terminal_title\s*=")


def codex_terminal_title_line(items: list[str]) -> str:
    """The exact ``terminal_title = [...]`` line relay writes in ``[tui]``."""
    return f"terminal_title = {json.dumps(items)}  {_TITLE_MARKER}"


def _parse_toml(lines: list[str]) -> dict[str, Any] | None:
    try:
        return tomllib.loads("\n".join(lines))
    except tomllib.TOMLDecodeError:
        return None


def _without_terminal_title(lines: list[str]) -> list[str]:
    """The lines without relay's title line, with the user's original lines restored."""
    out: list[str] = []
    for line in lines:
        if _TITLE_MARKER in line:
            continue
        out.append(line[len(_ORIGINAL_PREFIX):] if line.startswith(_ORIGINAL_PREFIX) else line)
    return out


def _user_title_lines(lines: list[str], header: int) -> tuple[int, int] | None:
    """Line range of the user's ``terminal_title`` assignment in the ``[tui]`` table."""
    end = next((i for i in range(header + 1, len(lines)) if _ANY_HEADER.match(lines[i])), len(lines))
    for i in range(header + 1, end):
        if _TITLE_ASSIGN.match(lines[i]):
            for j in range(i + 1, end + 1):  # a multi-line array ends where it parses
                data = _parse_toml(lines[i:j])
                if data is not None and "terminal_title" in data:
                    return i, j
            return None
    return None


def _title_state(lines: list[str]) -> str:
    """``relay``, ``user`` (own list ends with the id item), ``user-without-id``, ``missing``, ``unreadable``."""
    if any(_TITLE_MARKER in line for line in lines):
        return "relay"
    data = _parse_toml(lines)
    if data is None:
        return "unreadable"
    tui = data.get("tui")
    items = tui.get("terminal_title") if isinstance(tui, dict) else None
    if items is None:
        return "missing"
    return "user" if isinstance(items, list) and items and items[-1] in _TITLE_ID_ITEMS else "user-without-id"


def _with_terminal_title(lines: list[str]) -> tuple[list[str], str]:
    """Return the lines with a native ``thread-id`` last title item, and the title state.

    The result is computed from the user's own file (relay's earlier change
    undone), so a second install changes nothing. A file that does not parse,
    a ``tui`` table without a ``[tui]`` header, or a non-string list stays
    unchanged and is reported, never rewritten.
    """
    if _parse_toml(lines) is None:
        return lines, "unreadable"
    base = _without_terminal_title(lines)
    data = _parse_toml(base)
    if data is None:
        return lines, "unsupported"
    tui = data.get("tui")
    user = tui.get("terminal_title") if isinstance(tui, dict) else None
    header = next((i for i, line in enumerate(base) if _TUI_HEADER.match(line)), None)
    if user is None:
        items = [*DEFAULT_TITLE_ITEMS, TITLE_ID_ITEM]
        if header is not None:
            out = [*base[: header + 1], codex_terminal_title_line(items), *base[header + 1:]]
        elif tui is not None:
            return lines, "unsupported"  # ``tui`` from dotted keys; a [tui] header would be invalid
        else:
            while base and base[-1] == "":
                base = base[:-1]
            out = [*base, *([""] if base else []), "[tui]", codex_terminal_title_line(items), ""]
    elif not isinstance(user, list) or not all(isinstance(item, str) for item in user):
        return lines, "unsupported"
    elif user and user[-1] in _TITLE_ID_ITEMS:
        return base, "user"
    else:
        span = _user_title_lines(base, header) if header is not None else None
        if span is None:
            return lines, "unsupported"
        start, stop = span
        items = [*user, TITLE_ID_ITEM]
        out = [*base[:start], *(_ORIGINAL_PREFIX + line for line in base[start:stop]),
               codex_terminal_title_line(items), *base[stop:]]
    parsed = _parse_toml(out)
    tui_out = parsed.get("tui") if parsed is not None else None
    if not isinstance(tui_out, dict) or tui_out.get("terminal_title") != items:
        return lines, "unsupported"
    return out, "relay"


def _read_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    # splitlines drops the trailing newline; track it so we can restore it.
    return text.split("\n")


def _write_lines(path: Path, lines: list[str], mode: int) -> None:
    content = "\n".join(lines)
    tmp = path.with_name(f".{path.name}.relay-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def codex_status(env: EnvConfig) -> dict[str, Any]:
    """Report whether relay's notify line and terminal title are in the global Codex config."""
    path = _codex_config_path(env)
    installed = False
    title = "missing"
    if path.exists():
        lines = _read_lines(path)
        installed = any(_is_relay_notify(line) for line in lines)
        title = _title_state(lines)
    return {"path": str(path), "exists": path.exists(), "installed": installed, "terminal_title": title}


def codex_install(env: EnvConfig, *, dry_run: bool = False) -> dict[str, Any]:
    """Set relay's ``notify`` and terminal title in the global Codex config. Idempotent."""
    path = _codex_config_path(env)
    if path.exists():
        current = _read_lines(path)
        mode = os.stat(path).st_mode & 0o777
    else:
        current = []
        mode = 0o600
    desired, title = _with_terminal_title(_desired_codex_lines(env, current))
    changed = desired != current

    if not changed:
        return {"changed": False, "path": str(path), "backup": None, "dry_run": dry_run, "terminal_title": title}
    if dry_run:
        return {"changed": True, "path": str(path), "backup": None, "dry_run": True, "terminal_title": title}

    path.parent.mkdir(parents=True, exist_ok=True)
    backup = _backup(path, mode) if path.exists() else None
    _write_lines(path, desired, mode)
    return {"changed": True, "path": str(path), "backup": backup, "dry_run": False, "terminal_title": title}


def codex_title_install(env: EnvConfig) -> dict[str, Any]:
    """Ensure only the native ``thread-id`` title item in the env's Codex config. Idempotent.

    Service startup runs this for every env (``provision.provision_env``),
    also before Codex is installed there. notify and Claude settings are not
    touched. A real change keeps a backup and the file mode; a symlinked file
    or directory is refused.
    """
    path = _codex_config_path(env)
    result: dict[str, Any] = {"changed": False, "path": str(path), "backup": None, "terminal_title": None}
    if path.is_symlink() or path.parent.is_symlink():
        result["terminal_title"] = "unsupported"
        result["error"] = "refusing to write through a symlink"
        return result
    current = _read_lines(path) if path.exists() else []
    desired, result["terminal_title"] = _with_terminal_title(current)
    if desired == current:
        return result
    mode = (os.stat(path).st_mode & 0o777) if path.exists() else 0o600
    if not path.parent.exists():
        path.parent.mkdir(mode=0o700, parents=True)
    result["backup"] = _backup(path, mode) if path.exists() else None
    _write_lines(path, desired, mode)
    result["changed"] = True
    return result


def codex_uninstall(env: EnvConfig) -> dict[str, Any]:
    """Remove relay's managed notify and terminal title lines from the global Codex config."""
    path = _codex_config_path(env)
    if not path.exists():
        return {"changed": False, "path": str(path), "backup": None}
    current = _read_lines(path)
    desired = _without_terminal_title(_strip_codex_notify(current))
    if desired == current:
        return {"changed": False, "path": str(path), "backup": None}
    mode = os.stat(path).st_mode & 0o777
    backup = _backup(path, mode)
    _write_lines(path, desired, mode)
    return {"changed": True, "path": str(path), "backup": backup}
