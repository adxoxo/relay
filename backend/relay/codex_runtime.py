"""Codex runtime integration: rollout reader, verified thread binding, lifecycle.

Work 2 Codex TUIs share one app-server daemon. The daemon runs the ``notify``
program, so a notify event carries the Codex thread id but no tmux identity.
relay therefore links a dashboard row to one exact Codex thread, and only from
evidence that names that thread:

- operator binding: a native ``/status`` reading on a known pane, checked here
  against the exact relay row id, the live tmux session and pane, the TUI
  process (boot id, start ticks, parent chain, tty), the Codex state index,
  and the rollout metadata (``python -m relay.codex_runtime bind --evidence FILE``);
- launch binding: a notify event that carries the row's explicit relay id in
  its argv (``--relay-session-id=...``, see ``provision.codex_notify_override``),
  for a thread that the Codex state index and the rollout metadata confirm as
  a top-level thread. A relay id from the notify environment or a tmux pane
  never links a thread, because the shared daemon's environment runs notify;
- resume binding: a dashboard row started with ``codex resume <thread>``;
- native binding (``_try_native_link``, every live Codex pane, also panes
  started without relay): native state that names the thread the TUI in this
  pane shows, then the operator checks above (pane, process, thread):

  - writer lock: a TUI with an embedded app server (every relay launch, since
    ``-c notify=...`` selects embedded mode) holds the kernel ``flock`` of
    ``~/.codex/thread-writer-locks/<thread>.lock`` of its thread;
  - ``/status``: a TUI of the shared daemon holds no lock. Each such TUI
    process (boot id, pid, start ticks) that no verified link proves gets one
    guarded ``/status`` when its screen is idle with an empty composer; relay
    reads the native ``Session:`` line of the new card (``_status_bootstrap``).
    No model prompt is sent, and relay never types it twice for one process
    (an attempt marker in the row meta keeps this across a relay restart).
    The daemon can keep the old thread's lock after a ``/new``, so right before
    the write a title from this process must name the same thread, or the
    screen must still hash the same as the card capture (only the hash is kept);
  - terminal title: with the native ``thread-id`` item as the last title item
    (``hooks_install`` appends it in ``[tui]``) the TUI title, which tmux keeps
    as ``pane_title``, ends with its thread. tmux keeps a title after its
    process ends, so relay uses a title only after it saw the live process
    write a new one; this proves ``/new`` and ``/resume`` without /status.
    After a relay restart, a verified title link with this exact process proof
    (checked again) restores that trust; a /status link restores it only when
    the title hash differs from the one stored at link time.
    Codex cuts the item to 29 id characters, so the prefix must name exactly
    one thread that the daemon holds and exactly one thread of the state index.

Native evidence is read again after the process and index checks; the link is
written only when the process, the source, and the thread are unchanged.
A cwd, a name, a modification time, or a newest file never selects a thread.
Fresh native evidence of another thread after ``/new`` or ``/resume`` replaces
the link; a new thread that Codex has not saved yet marks it stale until it is
saved. A process or pane change marks the link stale; native evidence links the
new process again. Daemon hooks, like notify, run without the pane's identity.

Rollout format (Codex CLI 0.156 to 0.163, checked on the installed files):

- line 1: ``{"type": "session_meta", "payload": {"id": <thread uuid>, ...}}``;
  ``payload.id`` equals the UUID at the end of the file name. ``session_id``
  is the root thread of a subagent tree and is not the thread id.
- ``response_item`` records hold the conversation (``message`` with role user,
  assistant or developer; ``function_call``; ``custom_tool_call``; their
  outputs; ``reasoning``). ``event_msg`` records hold lifecycle events and
  duplicate item copies (``item_completed``).
- lifecycle: ``event_msg`` with ``payload.type`` ``task_started``,
  ``task_complete`` (``last_agent_message``) or ``turn_aborted``; each has
  ``payload.turn_id``; the record has an ISO ``timestamp``.
- No installed rollout records an approval or user-input request, so this
  source never reports ``needs_input``.

ConvItem shape (same as ``relay.transcript``)::

    {"id", "role": "user"|"assistant"|"tool", "text",
     "ts": epoch|null, "tool": {"name", "summary"}|null}
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import time
import tomllib
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from . import screen
from .config import EnvConfig
from .db import dumps, loads, new_id, now
from .podman import CommandError
from .transcript import TranscriptError, bounded_items

if TYPE_CHECKING:  # pragma: no cover
    from .runtime import Runtime
    from .tmux import Snapshot

log = logging.getLogger("relay.codex_runtime")

MAX_READ_BYTES = 4 * 1024 * 1024  # one source window (reader page, tail, poll increment)
MAX_HEADER_BYTES = 1024 * 1024  # session_meta line (about 23 KB on the installed files)
MAX_ITEMS = 1000
MAX_ITEM_CHARS = 100_000
TOOL_SUMMARY_CHARS = 200
LAST_MESSAGE_CHARS = 4000
_MAX_ARGUMENT_BYTES = 64 * 1024  # tool arguments parsed for a summary

# Rollout layouts that were checked against installed files. Lifecycle and
# binding refuse other versions until their layout is checked. 0.161 and 0.162
# were checked on work2's installed rollouts on 2026-10-11: same session_meta
# header shape (extra fields are additive and ignored) and the same
# task_started/task_complete/turn_aborted event_msg shape with turn_id.
# 0.163 was checked against work2's installed state index and rollouts on
# 2026-10-22: the same identity, conversation and lifecycle fields. The TUI
# uses source="vscode" with the shared daemon; that is a top-level source.
SUPPORTED_CLI_PREFIXES = ("0.156.", "0.157.", "0.158.", "0.159.", "0.160.", "0.161.", "0.162.", "0.163.")
STATE_DB_NAMES = ("state_5.sqlite",)

STATE_SOURCE = "codex_rollout"
EVENT_SOURCES = ("notify", STATE_SOURCE)
BINDING_KEY = "codex_binding"
STATUS_ATTEMPT_KEY = "codex_status_attempt"  # meta: the TUI process that got the one /status
LIFECYCLE_TYPES = ("task_started", "task_complete", "turn_aborted")
# Pending item of a Codex approval dialog seen on screen (see ``_screen_overlay``).
SCREEN_PENDING_SOURCE = "codex_screen"
# Pane identity of a link: binding.pane key -> sessions column. Every field is
# required in the link and in the row, and all must be equal.
PANE_IDENTITY = (
    ("socket", "socket_path"),
    ("server_pid", "server_pid"),
    ("tmux_session_id", "tmux_session_id"),
    ("pane_id", "pane_id"),
    ("pane_pid", "pane_pid"),
)

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
UUID_RE = re.compile(rf"^{_UUID}$")
RELAY_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_ROLLOUT_NAME_RE = re.compile(rf"^rollout-[0-9A-Za-z_:.-]+-({_UUID})\.jsonl$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_PANE_ID_RE = re.compile(r"^%\d+$")

# Native ``thread-id`` terminal title item (Codex 0.160, tui/src/bottom_pane/
# title_setup.rs, chatwidget/status_surfaces.rs, chatwidget/thread_title_status.rs):
# the value is cut to 32 characters (29 of the id, then ``...``); while Codex
# names the thread it gets a `` <spinner frame>`` suffix; items are joined with
# `` | ``, or a space next to the activity spinner. relay's config makes the
# item the last one (``hooks_install``). Only that last field in that exact
# shape is identity. A full id, or any text before the field (a thread name is
# free text), is never evidence.
TITLE_ID_ITEMS = ("thread-id", "session-id")
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_TITLE_PREFIX_RE = re.compile(r"^[0-9a-f-]{29}$")
_TITLE_ID_FIELD_RE = re.compile(
    rf"(?:^|(?<= \| )|(?<=[{_SPINNER}●] ))"
    rf"([0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{5}})\.\.\.(?: [{_SPINNER}])?$"
)
NATIVE_CHECK_INTERVAL = 10.0  # seconds between native checks of one pane
STATUS_BOOTSTRAP_MIN_AGE = 60.0  # a new TUI writes its own title id; /status is for older ones
_TUI_STATE_TTL = 86400.0  # forget a TUI process not seen for this long
_NATIVE_MAX_AGE = 60.0
_CONFIG_MAX_BYTES = 1024 * 1024

# Text the CLI injects as user input. It is context, not what the user typed.
_WRAPPER_PREFIXES = (
    "<environment_context>",
    "<recommended_plugins>",
    "<skill>",
    "<user_instructions>",
    "<user_action>",
    "<permissions instructions>",
    "# AGENTS.md instructions",
)
_SUMMARY_KEYS = ("command", "cmd", "file_path", "path", "pattern", "url", "query", "description")
_TOOL_TYPES = ("function_call", "custom_tool_call", "local_shell_call", "web_search_call")


class BindingError(ValueError):
    """A Codex thread link cannot be verified. The message holds no content."""


class ProcessProbeUnavailable(BindingError):
    """A failed process read gives no evidence that the TUI exited."""


class ThreadNotIndexed(BindingError):
    """The Codex state index has no row for the thread (yet)."""


class NativeEvidenceMissing(BindingError):
    """No native state names one thread for the pane now; a later poll retries."""


class TitleWithoutThread(NativeEvidenceMissing):
    """A shared-daemon TUI whose terminal title shows no thread id."""


class ThreadNotSaved(NativeEvidenceMissing):
    """The TUI shows a thread that Codex has not saved yet (no index row)."""

    def __init__(self, thread_id: str):
        super().__init__("the Codex TUI shows a thread that Codex has not saved yet")
        self.thread_id = thread_id


class NativeSourceChanged(NativeEvidenceMissing):
    """Native state changed between the first read and the write; a later poll retries."""


# -- rollout file access -------------------------------------------------------


def _check_id(value: Any) -> str:
    if not isinstance(value, str) or not UUID_RE.match(value):
        raise TranscriptError("invalid Codex thread id")
    return value


def _confine(env: EnvConfig, container_path: Any) -> tuple[str, str]:
    """Map a container rollout path to its real host path. Returns (real, file uuid).

    The path must sit under ``<home>/.codex/sessions``, carry a rollout name
    that ends with a thread UUID, and resolve to itself: any symbolic link
    below the sessions root is refused, as is a root outside the env home.
    """
    if not isinstance(container_path, str) or not container_path:
        raise TranscriptError("rollout path is required")
    path = PurePosixPath(container_path)
    root = PurePosixPath(env.home, ".codex", "sessions")
    if not path.is_absolute() or ".." in path.parts:
        raise TranscriptError("rollout path is invalid")
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise TranscriptError("rollout path is outside the Codex sessions directory") from None
    match = _ROLLOUT_NAME_RE.match(path.name)
    if not rel.parts or match is None:
        raise TranscriptError("rollout file name is invalid")
    try:
        real_home = os.path.realpath(env.host_home)
        real_root = os.path.realpath(env.host_home / ".codex" / "sessions")
        if not real_root.startswith(real_home + os.sep):
            raise TranscriptError("Codex sessions directory escapes the environment home")
        lexical = os.path.join(real_root, *rel.parts)
        real = os.path.realpath(lexical)
    except ValueError:  # a NUL byte in the path
        raise TranscriptError("rollout path is invalid") from None
    if real != lexical:
        raise TranscriptError("rollout path contains a symbolic link")
    return real, match.group(1)


def _open_checked(real: str) -> tuple[int, os.stat_result]:
    try:
        before = os.lstat(real)
    except OSError:
        raise TranscriptError("rollout file not found") from None
    if not stat.S_ISREG(before.st_mode):
        raise TranscriptError("rollout path is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(real, flags)
    except OSError:
        raise TranscriptError("rollout file cannot be opened") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
        os.close(fd)
        raise TranscriptError("rollout file changed while opening")
    return fd, info


def _meta_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Identity fields of ``session_meta`` (no instructions, no content)."""
    source = payload.get("source")
    out: dict[str, Any] = {
        "id": payload.get("id"),
        "cli_version": payload.get("cli_version") if isinstance(payload.get("cli_version"), str) else None,
        "source": source if isinstance(source, str) else ("subagent" if isinstance(source, dict) else None),
        "cwd": payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
        "originator": payload.get("originator") if isinstance(payload.get("originator"), str) else None,
        "parent_thread_id": payload.get("parent_thread_id") if isinstance(payload.get("parent_thread_id"), str) else None,
        "forked_from_id": payload.get("forked_from_id") if isinstance(payload.get("forked_from_id"), str) else None,
    }
    return out


def _read_header(fd: int, thread_id: str) -> tuple[int, dict[str, Any]]:
    """Validate the first line. Returns (offset after it, metadata summary)."""
    data = os.pread(fd, MAX_HEADER_BYTES + 1, 0)
    end = data.find(b"\n")
    if end < 0:
        raise TranscriptError("rollout metadata is missing or too large")
    try:
        row = json.loads(data[:end])
    except ValueError:
        raise TranscriptError("rollout metadata is unreadable") from None
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(row, dict) or row.get("type") != "session_meta" or not isinstance(payload, dict):
        raise TranscriptError("rollout does not start with session metadata")
    if payload.get("id") != thread_id:
        raise TranscriptError("rollout metadata id does not match the thread id")
    return end + 1, _meta_summary(payload)


class _Rollout:
    """One validated, open rollout file. Use as a context manager."""

    def __init__(self, env: EnvConfig, container_path: Any, expected_session_id: str | None):
        real, file_id = _confine(env, container_path)
        if expected_session_id is not None and _check_id(expected_session_id) != file_id:
            raise TranscriptError("rollout file name does not match the thread id")
        self.container_path: str = container_path
        self.thread_id = file_id
        self.real = real
        self.fd, info = _open_checked(real)
        try:
            self.header_end, self.meta = _read_header(self.fd, file_id)
        except BaseException:
            self.close()
            raise
        self.size = info.st_size
        self.dev = info.st_dev
        self.ino = info.st_ino

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "_Rollout":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def _window(fd: int, start: int, end: int, header_end: int) -> tuple[list[tuple[int, bytes]], int, int | None]:
    """Complete lines inside the byte window ``[start, end)``.

    Returns ``(lines, older, tail)``. ``lines`` holds ``(offset, raw)`` oldest
    first. Bytes after the last newline belong to a line that ends at or after
    ``end`` and are dropped; when ``start`` is not a record boundary the bytes
    before the first newline are dropped too. ``older`` is the cursor for the
    next older window: the start of the oldest complete line, or the window
    start when the window lies inside one line longer than the window (so a
    giant line is skipped and the cursor always moves back). ``tail`` is the
    offset after the last newline, or ``None`` when the window has none.
    """
    if end <= start:
        return [], start, None
    data = os.pread(fd, end - start, start)
    if len(data) != end - start:
        raise TranscriptError("rollout changed while reading")
    at_boundary = start <= header_end
    pos = 0
    first = -1
    if not at_boundary:
        first = data.find(b"\n")
        if first < 0:
            return [], start, None
        pos = first + 1
    lines: list[tuple[int, bytes]] = []
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        lines.append((start + pos, data[pos:nl]))
        pos = nl + 1
    tail = start + pos if (lines or first >= 0) else None
    if lines:
        older = lines[0][0]
    elif first >= 0 and first + 1 < len(data):
        older = start + first + 1  # the next window ends with the line that crosses ``start``
    else:
        older = start
    return lines, older, tail


def _epoch(ts: Any) -> float | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# -- conversation reader ---------------------------------------------------------


def _is_wrapper(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(p) for p in _WRAPPER_PREFIXES)


def _short(value: Any) -> str | None:
    if isinstance(value, list):
        value = " ".join(str(v) for v in value if isinstance(v, (str, int, float)))
    if not isinstance(value, str):
        return None
    for line in value.splitlines():
        if line.strip():
            return line.strip()[:TOOL_SUMMARY_CHARS]
    return None


def _tool_meta(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Tool name and a one-line summary from bounded call metadata."""
    ptype = payload.get("type")
    name = payload.get("name") if isinstance(payload.get("name"), str) else None
    summary: str | None = None
    if ptype == "local_shell_call":
        name = "shell"
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        summary = _short(action.get("command"))
    elif ptype == "web_search_call":
        name = "web_search"
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        summary = _short(action.get("query"))
    elif ptype == "function_call":
        args = payload.get("arguments")
        parsed: Any = None
        if isinstance(args, str) and len(args) <= _MAX_ARGUMENT_BYTES:
            try:
                parsed = json.loads(args)
            except ValueError:
                parsed = None
        if isinstance(parsed, dict):
            for key in _SUMMARY_KEYS:
                summary = _short(parsed.get(key))
                if summary:
                    break
    elif ptype == "custom_tool_call":
        raw = payload.get("input")
        if isinstance(raw, str):
            summary = _short(raw[:_MAX_ARGUMENT_BYTES])
    if not name:
        return None
    return name[:TOOL_SUMMARY_CHARS], summary or name[:TOOL_SUMMARY_CHARS]


def _items_from_record(raw: bytes, offset: int) -> list[dict[str, Any]]:
    """ConvItems from one rollout line. Only ``response_item`` records count.

    ``event_msg`` copies (``item_completed``), reasoning, developer and base
    instructions, injected context, and tool outputs are skipped.
    """
    if b'"response_item"' not in raw:
        return []
    try:
        row = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(row, dict) or row.get("type") != "response_item":
        return []
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return []
    ts = _epoch(row.get("timestamp"))
    pid = payload.get("id")
    base = pid if isinstance(pid, str) and _SAFE_ID_RE.match(pid) else f"o{offset}"
    ptype = payload.get("type")
    if ptype == "message":
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return []
        want = "input_text" if role == "user" else "output_text"
        content = payload.get("content")
        items: list[dict[str, Any]] = []
        for index, block in enumerate(content if isinstance(content, list) else []):
            if not isinstance(block, dict) or block.get("type") != want:
                continue
            text = block.get("text")
            if not isinstance(text, str) or (role == "user" and _is_wrapper(text)):
                continue
            cleaned = text.strip()
            if cleaned:
                items.append(
                    {"id": f"{base}-{index}", "role": role, "text": cleaned[:MAX_ITEM_CHARS], "ts": ts, "tool": None,
                     "phase": payload.get("phase") if payload.get("phase") in ("commentary", "final_answer") else None}
                )
        return items
    if ptype in _TOOL_TYPES:
        meta = _tool_meta(payload)
        if meta is None:
            return []
        return [{"id": f"{base}-0", "role": "tool", "text": "", "ts": ts, "tool": {"name": meta[0], "summary": meta[1]}}]
    return []


def _clamp(limit: Any, default: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_ITEMS))


def read_codex_conversation(
    env: EnvConfig,
    container_path: str,
    *,
    limit: int = 200,
    expected_session_id: str | None = None,
    expected_file_id: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """The last ``limit`` ConvItems (oldest first) from the last 4 MiB of a rollout.

    Raises ``TranscriptError`` when the path is not a confined rollout or when
    its metadata id differs from the file UUID or ``expected_session_id``.
    """
    limit = _clamp(limit, 200)
    with _Rollout(env, container_path, expected_session_id) as ro:
        if expected_file_id is not None and {"dev": ro.dev, "ino": ro.ino} != expected_file_id:
            raise TranscriptError("rollout file identity changed")
        start = max(ro.header_end, ro.size - MAX_READ_BYTES)
        lines, _, _ = _window(ro.fd, start, ro.size, ro.header_end)
        after = os.stat(ro.real, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (ro.dev, ro.ino) or after.st_size < ro.size:
            raise TranscriptError("rollout changed while reading")
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for offset, raw in lines:
        for item in _items_from_record(raw, offset):
            if item["id"] not in seen:
                seen.add(item["id"])
                items.append(item)
    return bounded_items(items, limit)


def read_codex_page(
    env: EnvConfig,
    container_path: str,
    *,
    before: int | None = None,
    limit: int = 40,
    expected_session_id: str | None = None,
    expected_file_id: dict[str, int] | None = None,
) -> dict[str, Any]:
    """One page of older ConvItems that start before byte offset ``before``.

    ``before=None`` means the end of the file. Each call validates the path and
    the metadata again and reads at most one 4 MiB window that ends at
    ``before``. Returns ``{"items": [...] oldest first, "before": int,
    "more": bool}``: pass ``before`` back for the next older page. A window
    with fewer readable items than ``limit`` still returns its cursor, so a
    page can be empty while ``more`` is true.
    """
    limit = _clamp(limit, 40)
    with _Rollout(env, container_path, expected_session_id) as ro:
        if expected_file_id is not None and {"dev": ro.dev, "ino": ro.ino} != expected_file_id:
            raise TranscriptError("rollout file identity changed")
        if before is None:
            end = ro.size
        elif isinstance(before, bool) or not isinstance(before, int):
            raise TranscriptError("history cursor is invalid")
        else:
            end = before
        if end > ro.size or end < 0:
            raise TranscriptError("history cursor is outside the rollout")
        if end <= ro.header_end:
            return {"items": [], "before": ro.header_end, "more": False}
        start = max(ro.header_end, end - MAX_READ_BYTES)
        lines, older, _ = _window(ro.fd, start, end, ro.header_end)
        header_end = ro.header_end
        after = os.stat(ro.real, follow_symlinks=False)
        if (after.st_dev, after.st_ino) != (ro.dev, ro.ino) or after.st_size < ro.size:
            raise TranscriptError("rollout changed while reading")

    pages: list[list[dict[str, Any]]] = []
    count = 0
    cursor = older
    for offset, raw in reversed(lines):
        items = _items_from_record(raw, offset)
        if count + len(items) > limit:
            if count == 0:
                pages.append(items[-limit:])  # one record holds more blocks than a page
                cursor = offset
            break
        if items:
            pages.append(items)
            count += len(items)
        cursor = offset
        if count >= limit:
            break
    else:
        cursor = older
    out = [item for page in reversed(pages) for item in page]
    return {"items": out, "before": cursor, "more": cursor > header_end}


# -- Codex state index and thread verification --------------------------------


def _state_db(env: EnvConfig) -> str:
    """Host path of the known Codex state index. No other layout is guessed."""
    base = env.host_home / ".codex"
    for name in STATE_DB_NAMES:
        path = base / name
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            return str(path)
    raise BindingError(f"no supported Codex state index ({', '.join(STATE_DB_NAMES)})")


def lookup_thread(env: EnvConfig, thread_id: str) -> dict[str, Any]:
    """One exact thread row from the Codex state index, read only."""
    try:
        _check_id(thread_id)
    except TranscriptError as exc:
        raise BindingError(str(exc)) from None
    uri = "file:" + urllib.parse.quote(_state_db(env)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(
                "SELECT id, rollout_path, archived, source, cwd FROM threads WHERE id = ?", (thread_id,)
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BindingError(f"Codex state index is unreadable: {type(exc).__name__}") from None
    if not rows:
        raise ThreadNotIndexed("thread is not in the Codex state index")
    if len(rows) != 1:
        raise BindingError("thread is not in the Codex state index")
    tid, rollout_path, archived, source, cwd = rows[0]
    return {"id": tid, "rollout_path": rollout_path, "archived": archived, "source": source, "cwd": cwd}


def count_thread_prefix(env: EnvConfig, prefix: str) -> int:
    """How many state index threads start with ``prefix``, read only."""
    if not _TITLE_PREFIX_RE.match(prefix):
        raise BindingError("invalid Codex thread id prefix")
    uri = "file:" + urllib.parse.quote(_state_db(env)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            conn.execute("PRAGMA query_only = ON")
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM threads WHERE substr(id, 1, ?) = ?", (len(prefix), prefix)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BindingError(f"Codex state index is unreadable: {type(exc).__name__}") from None
    return int(count)


def _supported(meta: dict[str, Any]) -> bool:
    version = meta.get("cli_version")
    return isinstance(version, str) and version.startswith(SUPPORTED_CLI_PREFIXES)


def verify_thread(env: EnvConfig, thread_id: str, *, expected_path: str | None = None) -> dict[str, Any]:
    """Prove a top-level thread: state index row, confined rollout, exact metadata id.

    Returns id-only proof fields. Raises ``BindingError`` for an unknown,
    archived, subagent or forked thread, a path that differs from
    ``expected_path``, or an unsupported rollout version.
    """
    info = lookup_thread(env, thread_id)
    path = info["rollout_path"]
    if info["archived"]:
        raise BindingError("thread is archived")
    if not isinstance(path, str):
        raise BindingError("thread has no rollout path")
    if expected_path is not None and path != expected_path:
        raise BindingError("rollout path differs from the Codex state index")
    if isinstance(info["source"], str) and info["source"].lstrip().startswith("{"):
        raise BindingError("thread is a subagent thread")
    try:
        with _Rollout(env, path, thread_id) as ro:
            meta, dev, ino = ro.meta, ro.dev, ro.ino
    except TranscriptError as exc:
        raise BindingError(str(exc)) from None
    if meta.get("parent_thread_id") or meta.get("forked_from_id") or meta.get("source") == "subagent":
        raise BindingError("thread is a subagent or forked thread")
    if not _supported(meta):
        raise BindingError("rollout version is not supported")
    return {
        "thread_id": thread_id,
        "transcript_path": path,
        "cwd": meta.get("cwd") or (info["cwd"] if isinstance(info["cwd"], str) else None),
        "cli_version": meta.get("cli_version"),
        "rollout": {"dev": dev, "ino": ino},
    }


# -- binding storage -------------------------------------------------------------


def binding_of(row: Any) -> dict[str, Any] | None:
    """The stored thread link of a session row, or ``None``."""
    try:
        meta = loads(row["meta_json"])
    except (IndexError, KeyError, TypeError):
        return None
    binding = meta.get(BINDING_KEY) if isinstance(meta, dict) else None
    if not isinstance(binding, dict):
        return None
    tid, path = binding.get("thread_id"), binding.get("transcript_path")
    if not isinstance(tid, str) or not UUID_RE.match(tid) or not isinstance(path, str):
        return None
    return binding


def is_verified(binding: dict[str, Any] | None) -> bool:
    return binding is not None and binding.get("status") == "verified"


def _with_binding(row: Any, binding: dict[str, Any]) -> str | None:
    meta = loads(row["meta_json"])
    meta = meta if isinstance(meta, dict) else {}
    meta[BINDING_KEY] = binding
    return dumps(meta)


def _pane_fields(row: Any) -> dict[str, Any]:
    return {key: row[col] for key, col in PANE_IDENTITY}


def _project_dir(env: EnvConfig, cwd: Any) -> str | None:
    if not isinstance(cwd, str) or ".." in PurePosixPath(cwd).parts:
        return None
    root = env.projects_root
    return cwd if cwd == root or cwd.startswith(root + "/") else None


def _other_live_rows(db: Any, thread_id: str, row_id: str) -> list[Any]:
    return db.query(
        "SELECT id FROM sessions WHERE status = 'live' AND agent_session_id = ? AND id != ?",
        (thread_id, row_id),
    )


def bind_from_launch(rt: "Runtime", row: Any, thread_id: str, *, via: str) -> dict[str, Any]:
    """Link a dashboard row to a verified top-level thread.

    ``via`` names the identity evidence: ``argv`` (explicit relay id in the
    notify argv) or ``resume`` (relay started ``codex resume <thread>``).
    """
    env = rt.settings.env(row["env"])
    if row["agent_session_id"] and row["agent_session_id"] != thread_id:
        raise BindingError("row already names another thread")
    pane = _pane_fields(row)
    if any(value is None for value in pane.values()):
        raise BindingError("the pane identity of the row is not known yet")
    if _other_live_rows(rt.db, thread_id, row["id"]):
        raise BindingError("another live row names this thread")
    proof = verify_thread(env, thread_id)
    binding = {
        "v": 1,
        "status": "verified",
        "source": "launch",
        "via": via,
        "env": row["env"],
        "thread_id": thread_id,
        "transcript_path": proof["transcript_path"],
        "cli_version": proof["cli_version"],
        "rollout": proof["rollout"],
        "pane": pane,
        "bound_at": now(),
    }
    fields: dict[str, Any] = {
        "agent_session_id": thread_id,
        "transcript_path": proof["transcript_path"],
        "meta_json": _with_binding(row, binding),
    }
    project = _project_dir(env, proof["cwd"])
    if project and not row["project_dir"]:
        fields["project_dir"] = project
    rt.db.update_session(row["id"], **fields)
    log.info("linked %s to Codex thread %s (%s)", row["id"], thread_id, via)
    return binding


def mark_stale(rt: "Runtime", row: Any, reason: str) -> None:
    """Stop trusting a link. Terminal heuristics own the state again."""
    binding = binding_of(row)
    if binding is None or binding.get("status") == "stale":
        return
    binding = dict(binding, status="stale", stale_reason=reason, stale_at=now())
    fields: dict[str, Any] = {"meta_json": _with_binding(row, binding)}
    if row["state_source"] in EVENT_SOURCES:
        fields["state_source"] = "terminal"
    if _has_overlay(row):
        fields["pending_json"] = None  # the screen overlay belongs to the followed link
    rt.db.update_session(row["id"], **fields)
    _cursors(rt).pop(row["id"], None)
    log.warning("Codex link of %s is stale: %s", row["id"], reason)
    rt.bus.publish("sessions")


def note_thread_conflict(rt: "Runtime", row: Any, thread_id: str) -> None:
    """An explicit-relay-id event named another top-level thread.

    That is a thread switch inside the relay-launched TUI. relay does not
    follow it (no selected-thread signal proves it); the link becomes stale.
    """
    env = rt.settings.env(row["env"])
    try:
        verify_thread(env, thread_id)
    except (BindingError, OSError):
        return  # a subagent or unknown thread is not a switch
    mark_stale(rt, row, "the agent reported another thread for this session")


# -- lifecycle ------------------------------------------------------------------


@dataclass
class LifecycleEvent:
    type: str  # one of LIFECYCLE_TYPES
    turn_id: str
    ts: float
    last_message: str | None = None
    reason: str | None = None
    title: bool = False  # completion of Codex's thread-title turn: ends the turn quietly


def _lifecycle_from_record(raw: bytes) -> LifecycleEvent | None:
    """A lifecycle event from one rollout line. Unknown records give ``None``."""
    if b'"event_msg"' not in raw or not any(t.encode() in raw for t in LIFECYCLE_TYPES):
        return None
    try:
        row = json.loads(raw)
    except ValueError:
        return None
    payload = row.get("payload") if isinstance(row, dict) else None
    if not isinstance(payload, dict) or row.get("type") != "event_msg":
        return None
    ptype = payload.get("type")
    turn_id = payload.get("turn_id")
    ts = _epoch(row.get("timestamp"))
    if ptype not in LIFECYCLE_TYPES or not isinstance(turn_id, str) or not _SAFE_ID_RE.match(turn_id) or ts is None:
        return None
    last = payload.get("last_agent_message") if ptype == "task_complete" else None
    reason = payload.get("reason") if ptype == "turn_aborted" else None
    title = ptype == "task_complete" and _is_title_message(last)
    return LifecycleEvent(
        type=ptype,
        turn_id=turn_id,
        ts=ts,
        last_message=last[:LAST_MESSAGE_CHARS] if isinstance(last, str) and not title else None,
        reason=reason[:64] if isinstance(reason, str) else None,
        title=title,
    )


def _is_title_message(last: Any) -> bool:
    if not isinstance(last, str) or not last.lstrip().startswith("{"):
        return False
    try:
        parsed = json.loads(last)
    except ValueError:
        return False
    return isinstance(parsed, dict) and set(parsed) == {"title"}


@dataclass
class _Cursor:
    env: str
    thread_id: str
    container_path: str
    dev: int
    ino: int
    offset: int
    skip_partial: bool = False
    active_turn: str | None = None
    active_started_at: float | None = None
    active_prompt_id: str | None = None
    last_user_id: str | None = None

    def observe_user(self, raw: bytes, offset: int) -> None:
        """Native record order links a user message to the current turn."""
        user_id = _user_item_id(raw, offset)
        if user_id is not None:
            self.note_user(user_id)

    def note_user(self, user_id: str) -> None:
        self.last_user_id = user_id
        if self.active_turn is not None:
            self.active_prompt_id = user_id

    def accept(self, ev: LifecycleEvent) -> bool:
        """Turn order guard: an end event counts only for the running turn.

        The guard runs before any title logic. A matching end, including the
        completion of Codex's thread-title turn (``ev.title``), closes the turn
        and is accepted, so the row returns to idle; the adapter records no
        Activity and no last message for a title completion.
        """
        if ev.type == "task_started":
            if self.active_turn != ev.turn_id:
                if self.active_turn is not None and self.last_user_id == self.active_prompt_id:
                    self.last_user_id = None
                self.active_started_at = ev.ts
                self.active_prompt_id = self.last_user_id
            self.active_turn = ev.turn_id
            return True
        if self.active_turn is not None and ev.turn_id != self.active_turn:
            return False  # another turn ended while the active turn still runs
        self.active_turn = None
        self.active_started_at = None
        self.active_prompt_id = None
        self.last_user_id = None
        return True


def _cursors(rt: "Runtime") -> dict[str, _Cursor]:
    return rt.cache.setdefault("codex_rollout_cursors", {})


def _user_item_id(raw: bytes, offset: int) -> str | None:
    items = _items_from_record(raw, offset)
    return items[0]["id"] if items and items[0]["role"] == "user" else None


def _page_markers(lines: list[tuple[int, bytes]]) -> tuple[list[tuple[str, Any]], bool]:
    """Replay markers of one page, oldest first, and whether it holds a task_started.

    A marker is ``("user", id)`` or ``("event", LifecycleEvent)``: no raw
    line and no message text is kept, so a long walk keeps only these.
    """
    markers: list[tuple[str, Any]] = []
    started = False
    for offset, raw in lines:
        user_id = _user_item_id(raw, offset)
        if user_id is not None:
            markers.append(("user", user_id))
            continue
        ev = _lifecycle_from_record(raw)
        if ev is not None:
            markers.append(("event", ev))
            started = started or ev.type == "task_started"
    return markers, started


def _bootstrap(ro: _Rollout, env_id: str) -> tuple[_Cursor, list[LifecycleEvent]]:
    """Latest state, read backward in pages of at most ``MAX_READ_BYTES``. Only the final event is returned.

    After a full replay the state depends only on the latest ``task_started``
    and the events after it, so the walk stops at the newest page that holds a
    ``task_started``, or at the header when no turn ever started. Each older
    page ends where the newer page's oldest complete line begins (``_window``'s
    ``older`` cursor), so no record at a page boundary is lost or read twice.
    Each page is reduced to lifecycle events and user-message ids before the
    next read, so memory follows one page, not the log size. The markers are
    then replayed oldest first through the same ``accept`` turn-order guard:
    an end of another turn never closes the running one. The newest page sets
    the cursor's offset and skip_partial, so the incremental reader continues
    from the same place as before.
    """
    start = max(ro.header_end, ro.size - MAX_READ_BYTES)
    lines, older, tail = _window(ro.fd, start, ro.size, ro.header_end)
    cursor = _Cursor(
        env=env_id,
        thread_id=ro.thread_id,
        container_path=ro.container_path,
        dev=ro.dev,
        ino=ro.ino,
        offset=tail if tail is not None else start,
        skip_partial=tail is None and start > ro.header_end,
    )
    markers, started = _page_markers(lines)
    pages = [markers]  # newest first
    del lines
    while not started and start > ro.header_end:
        end = older
        start = max(ro.header_end, end - MAX_READ_BYTES)
        lines, older, _ = _window(ro.fd, start, end, ro.header_end)
        markers, started = _page_markers(lines)
        pages.append(markers)
        del lines
        if older >= end:
            break  # no progress is possible; never loop on the same bytes
    last: LifecycleEvent | None = None
    for markers in reversed(pages):
        for kind, value in markers:
            if kind == "user":
                cursor.note_user(value)
            elif cursor.accept(value):
                last = value
    return cursor, [last] if last is not None else []


def scan_rollout(
    env: EnvConfig, thread_id: str, container_path: str, cursor: _Cursor | None
) -> tuple[_Cursor, list[LifecycleEvent], bool]:
    """Read new lifecycle events of one bound rollout. Runs in a worker thread.

    Returns ``(cursor, events, bootstrap)``. A new source, a replaced file
    (other inode), a shrunk file, or a backlog over 4 MiB starts again from
    the tail with ``bootstrap=True``. Otherwise only bytes after the cursor are
    read and only complete lines are consumed.
    """
    real, file_id = _confine(env, container_path)
    if file_id != thread_id:
        raise TranscriptError("rollout file name does not match the thread id")
    fresh = (
        cursor is None
        or cursor.thread_id != thread_id
        or cursor.container_path != container_path
    )
    if not fresh:
        assert cursor is not None
        fd, info = _open_checked(real)
        try:
            same = (info.st_dev, info.st_ino) == (cursor.dev, cursor.ino)
            backlog = info.st_size - cursor.offset
            if same and 0 <= backlog <= MAX_READ_BYTES:
                if backlog == 0:
                    return cursor, [], False
                data = os.pread(fd, backlog, cursor.offset)
                if len(data) != backlog:
                    raise TranscriptError("rollout changed while reading")
                return cursor, _consume(cursor, data), False
        finally:
            os.close(fd)
    with _Rollout(env, container_path, thread_id) as ro:
        if not _supported(ro.meta):
            raise TranscriptError("rollout version is not supported")
        if ro.meta.get("parent_thread_id") or ro.meta.get("forked_from_id"):
            raise TranscriptError("rollout belongs to a subagent thread")
        new_cursor, events = _bootstrap(ro, cursor.env if cursor else "")
    return new_cursor, events, True


def _consume(cursor: _Cursor, data: bytes) -> list[LifecycleEvent]:
    """Events from the complete lines of ``data`` (read at ``cursor.offset``). Moves the cursor."""
    events: list[LifecycleEvent] = []
    pos = 0
    if cursor.skip_partial:
        nl = data.find(b"\n")
        if nl < 0:
            cursor.offset += len(data)
            return events
        pos = nl + 1
        cursor.skip_partial = False
    while True:
        nl = data.find(b"\n", pos)
        if nl < 0:
            break
        raw = data[pos:nl]
        cursor.observe_user(raw, cursor.offset + pos)
        ev = _lifecycle_from_record(raw)
        if ev is not None and cursor.accept(ev):
            events.append(ev)
        pos = nl + 1
    cursor.offset += pos
    return events


def _event_for(row_id: str, thread_id: str, ev: LifecycleEvent, bootstrap: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": ev.type, "thread-id": thread_id, "turn-id": ev.turn_id}
    if ev.type == "task_complete":
        payload["last-assistant-message"] = ev.last_message
        if ev.title:
            # Trusted: only the internal rollout path reaches apply_lifecycle.
            payload["title-turn"] = True
    if ev.type == "turn_aborted":
        payload["reason"] = ev.reason
    return {
        "v": 1,
        "source": "codex",
        "origin": STATE_SOURCE,
        "ts": ev.ts,
        "relay_session_id": row_id,
        "bootstrap": bootstrap,
        "payload": payload,
    }


def _stale_reason(row: Any, binding: dict[str, Any], snapshot: "Snapshot | None") -> str | None:
    """Why a verified link no longer describes this pane, else ``None``."""
    if row["agent_session_id"] != binding["thread_id"] or row["transcript_path"] != binding["transcript_path"]:
        return "the session identity fields changed"
    if binding.get("env") is None or binding.get("env") != row["env"]:
        return "the link names no environment or another one"
    pane = binding.get("pane")
    if not isinstance(pane, dict):
        return "the link has no pane identity"
    for key, col in PANE_IDENTITY:
        expected = pane.get(key)
        if expected is None or row[col] is None:
            return f"the pane identity is incomplete ({key})"
        if row[col] != expected:
            return f"the tmux pane changed ({key})"
    tui_pid = binding.get("tui_pid")
    if snapshot is not None and isinstance(tui_pid, int) and snapshot.procs:
        proc = snapshot.procs.get(tui_pid)
        if proc is None:
            return "the linked Codex process exited" if snapshot.procs_complete else None
        argv0 = proc.args.split(None, 1)[0] if proc.args.strip() else ""
        if os.path.basename(argv0) != "codex":
            return "the linked process id now runs another program"
        if snapshot.procs_complete and not _descends(snapshot, tui_pid, row["pane_pid"]):
            return "the linked Codex process is no longer in this pane"
    return None


def _descends(snapshot: "Snapshot", pid: int, ancestor: Any) -> bool:
    if not isinstance(ancestor, int):
        return False
    for _ in range(64):
        if pid == ancestor:
            return True
        proc = snapshot.procs.get(pid)
        if proc is None or proc.ppid <= 1:
            return False
        pid = proc.ppid
    return False


async def poll_env(
    rt: "Runtime",
    env_id: str,
    snapshot: "Snapshot | None" = None,
    screens: dict[tuple[str, str], str] | None = None,
    screens_at: float | None = None,
) -> None:
    """Apply new lifecycle events of every verified Codex link in one env.

    Called by the inventory loop after each reconcile. Events go through
    ``adapters.handle_event`` (recorded in ``hook_events``, guarded, bus).
    ``screens`` (``{(socket, pane_id): text}``, captured from ``screens_at``
    on) feed the approval overlay (``_screen_overlay``). A thread named by
    more than one live row is skipped (ambiguous). One poll runs per env at a
    time; a second caller returns at once, because two polls would move the
    same cursor.
    """
    lock = rt.lock_for("codex-rollout:" + env_id)
    if lock.locked():
        return
    async with lock:
        await _poll_env(rt, env_id, snapshot, screens or {}, screens_at)


def _has_overlay(row: Any) -> bool:
    pending = loads(row["pending_json"])
    return isinstance(pending, dict) and pending.get("source") == SCREEN_PENDING_SOURCE


async def _screen_overlay(rt: "Runtime", row_id: str, cursor: _Cursor, dialog: bool, screens_at: float) -> None:
    """Show a Codex approval dialog on screen as needs_input on a rollout-owned row.

    No rollout record marks an approval request, so the existing screen parser
    (``screen.permission_dialog_visible``) is the only source. Ordinary screen
    text never changes the state. The overlay is a pending item with source
    ``codex_screen``; ``send_prompt`` refuses while it is set. When the dialog
    is gone, the overlay is removed and the latest rollout state returns: a
    running turn is working, a completed or aborted one is idle. A capture
    older than the row state (a newer event, prompt or interrupt) changes
    nothing, and another pending item (folder trust) is never touched.
    """
    async with rt.lock_for(row_id):
        row = rt.db.get_session(row_id)
        if row is None or row["status"] != "live" or row["state_source"] not in EVENT_SOURCES:
            return
        if row["state_at"] is not None and float(row["state_at"]) > screens_at:
            return  # the capture is older than the current state
        if dialog:
            if row["pending_json"]:
                return  # already shown, or another request holds the pending slot
            pending = {
                "id": new_id()[:16],
                "source": SCREEN_PENDING_SOURCE,
                "tool": "Codex approval",
                "summary": "",
                "detail": "Codex asks for approval. Answer it in the terminal.",
                "match": [],
                "created_at": screens_at,
                "answerable": False,
            }
            rt.db.update_session(
                row_id, state="needs_input", state_source=STATE_SOURCE, state_at=screens_at, pending_json=dumps(pending)
            )
        elif _has_overlay(row):
            state = "working" if cursor.active_turn is not None else "idle"
            rt.db.update_session(row_id, state=state, state_source=STATE_SOURCE, state_at=screens_at, pending_json=None)
        else:
            return
    rt.bus.publish("sessions")


async def _poll_env(
    rt: "Runtime",
    env_id: str,
    snapshot: "Snapshot | None",
    screens: dict[tuple[str, str], str],
    screens_at: float | None,
) -> None:
    from . import adapters

    env = rt.settings.env(env_id)
    cursors = _cursors(rt)
    rows = [r for r in rt.db.list_sessions(env=env_id, statuses=("live",)) if r["kind"] == "codex"]
    live_ids = {r["id"] for r in rows}
    for key in [k for k, c in cursors.items() if c.env == env_id and k not in live_ids]:
        cursors.pop(key, None)
    checked = rt.cache.setdefault("codex_native_checked:" + env_id, {})
    for key in [k for k in checked if k not in live_ids]:
        checked.pop(key, None)

    bound: list[tuple[Any, dict[str, Any]]] = []
    for row in rows:
        binding = binding_of(row)
        if binding is not None and binding.get("status") == "stale":
            if await _try_exact_recovery(rt, row, binding, snapshot):
                row = rt.db.get_session(row["id"])
                binding = binding_of(row)
        if await _try_native_link(rt, env, row, snapshot):
            row = rt.db.get_session(row["id"])
            if row is None or row["status"] != "live":
                continue
            binding = binding_of(row)
        if is_verified(binding):
            bound.append((row, binding))
            continue
        cursors.pop(row["id"], None)
        if row["state_source"] in EVENT_SOURCES or _has_overlay(row):
            # No followed link (for example a notify state from before links
            # existed): the terminal heuristic owns the state again.
            fields: dict[str, Any] = {"state_source": "terminal"}
            if _has_overlay(row):
                fields["pending_json"] = None
            rt.db.update_session(row["id"], **fields)
            rt.bus.publish("sessions")
        if binding is None:
            _try_resume_binding(rt, row)
    threads = [b["thread_id"] for _, b in bound]

    for row, binding in bound:
        thread_id = binding["thread_id"]
        if threads.count(thread_id) > 1 or _other_live_rows(rt.db, thread_id, row["id"]):
            log.warning("Codex thread %s is linked to more than one live row; skipped", thread_id)
            cursors.pop(row["id"], None)
            continue
        # A direct probe with the original boot/start/tty proof is stronger
        # than a process snapshot taken before reconciliation.
        reason = _stale_reason(row, binding, None if binding.get("tui_pid") is not None else snapshot)
        if reason is not None:
            mark_stale(rt, row, reason)
            continue
        if binding.get("tui_pid") is not None:
            try:
                await _verify_original_process(rt, env, row, binding)
            except (ProcessProbeUnavailable, TimeoutError, OSError):
                continue  # uncertainty must neither invalidate nor advance the source
            except BindingError as exc:
                fresh = rt.db.get_session(row["id"])
                if fresh is not None and _guard_identity(fresh) == _guard_identity(row):
                    mark_stale(rt, fresh, str(exc))
                continue
            fresh = rt.db.get_session(row["id"])
            if fresh is None or _guard_identity(fresh) != _guard_identity(row):
                continue
            row = fresh
        try:
            cursor, events, bootstrap = await asyncio.to_thread(
                scan_rollout, env, thread_id, binding["transcript_path"],
                replace(cursors[row["id"]]) if row["id"] in cursors else None,
            )
        except (TranscriptError, OSError) as exc:
            log.warning("Codex rollout of %s unreadable: %s", row["id"], exc)
            cursors.pop(row["id"], None)
            continue
        fresh = rt.db.get_session(row["id"])
        if fresh is None or _guard_identity(fresh) != _guard_identity(row):
            continue
        cursor.env = env_id
        cursors[row["id"]] = cursor
        for ev in events:
            await adapters.handle_event(rt, env_id, _event_for(row["id"], thread_id, ev, bootstrap), internal=True)
        text = screens.get((row["socket_path"], row["pane_id"]))
        if text is not None and screens_at is not None:
            await _screen_overlay(rt, row["id"], cursor, screen.permission_dialog_visible(text), screens_at)


def _try_resume_binding(rt: "Runtime", row: Any) -> None:
    """Link a row relay started with ``codex resume <thread>`` once its pane is known."""
    meta = loads(row["meta_json"])
    if (
        row["origin"] != "dashboard"
        or not isinstance(meta, dict)
        or not meta.get("resumed_from")
        or not row["agent_session_id"]
        or not row["pane_id"]
        or row["pane_pid"] is None
    ):
        return
    try:
        bind_from_launch(rt, row, row["agent_session_id"], via="resume")
    except (BindingError, OSError) as exc:
        log.debug("resume link for %s not verified: %s", row["id"], exc)


# -- operator binding -------------------------------------------------------------

# Runs inside the container with the TUI and pane pids as arguments. Prints one
# JSON object: boot id, stat fields (state, ppid, start ticks), the tty of fd 0,
# argv[0] of the TUI, and the parent chain from the TUI up to the pane process.
_PROC_PROBE = r"""
import json, os, sys
def stat(pid):
    with open("/proc/%d/stat" % pid, "rb") as fh:
        data = fh.read().decode("utf-8", "replace")
    rest = data[data.rindex(")") + 2:].split()
    return {"state": rest[0], "ppid": int(rest[1]), "start_ticks": int(rest[19])}
def tty(pid):
    try:
        return os.readlink("/proc/%d/fd/0" % pid)
    except OSError:
        return None
out = {}
try:
    out["stage"] = "boot"
    with open("/proc/sys/kernel/random/boot_id") as fh:
        out["boot_id"] = fh.read().strip()
    tui, pane = int(sys.argv[1]), int(sys.argv[2])
    out["stage"] = "tui"
    with open("/proc/%d/cmdline" % tui, "rb") as fh:
        argv0 = fh.read(4096).split(b"\0", 1)[0].decode("utf-8", "replace")
    out["tui"] = dict(stat(tui), tty=tty(tui), argv0=argv0)
    out["stage"] = "pane"
    out["pane"] = dict(stat(pane), tty=tty(pane))
    out["stage"] = "chain"
    chain, pid = [], tui
    for _ in range(64):
        chain.append(pid)
        if pid == pane:
            break
        ppid = stat(pid)["ppid"]
        if ppid <= 1:
            break
        pid = ppid
    out["chain"] = chain
except (OSError, ValueError, IndexError) as exc:
    out["error"] = type(exc).__name__
    if out.get("stage") == "tui":
        out["tui_missing"] = not os.path.isdir("/proc/%d" % tui)
print(json.dumps(out))
"""


def _int_field(item: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = item.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    raise BindingError(f"evidence field {keys[0]} must be an integer")


def _str_field(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise BindingError(f"evidence field {key} must be a non-empty string")
    return value


def _captured_at(value: Any) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise BindingError("evidence captured_at must be an ISO timestamp with a time zone")
    return parsed.timestamp()


async def _probe_process(rt: "Runtime", env: EnvConfig, tui_pid: int, pane_pid: int) -> dict[str, Any]:
    res = await rt.podman.exec(env, ["python3", "-c", _PROC_PROBE, str(tui_pid), str(pane_pid)], timeout=15)
    if not res.ok:
        raise ProcessProbeUnavailable("process probe failed in the container")
    try:
        data = json.loads(res.out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ProcessProbeUnavailable("process probe output is unreadable") from None
    if not isinstance(data, dict) or data.get("error"):
        if (
            isinstance(data, dict) and data.get("error") == "FileNotFoundError"
            and data.get("stage") == "tui" and data.get("tui_missing") is True
        ):
            raise BindingError("the linked Codex process exited")
        raise ProcessProbeUnavailable("process identity is unavailable")
    return data


def _guard_identity(row: Any) -> tuple[Any, ...]:
    """All source proof must survive every asynchronous process/file read."""
    return _row_identity(row) + (row["env"], row["transcript_path"], dumps(binding_of(row)))


async def _verify_original_process(rt: "Runtime", env: EnvConfig, row: Any, binding: dict[str, Any]) -> None:
    """Verify only the original TUI; never discover or substitute a process."""
    pid, ticks, boot, tty = (binding.get(k) for k in ("tui_pid", "tui_start_ticks", "boot_id", "tui_tty"))
    if (
        not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
        or not isinstance(ticks, int) or isinstance(ticks, bool) or ticks <= 0
        or not isinstance(boot, str) or not boot
        or not isinstance(tty, str) or not tty.startswith("/dev/pts/")
        or not isinstance(row["pane_pid"], int) or row["pane_pid"] <= 0
    ):
        raise ProcessProbeUnavailable("the original process proof is incomplete")
    proc = await _probe_process(rt, env, pid, row["pane_pid"])
    tui, pane = proc.get("tui"), proc.get("pane")
    if not isinstance(tui, dict) or not isinstance(pane, dict):
        raise ProcessProbeUnavailable("process identity is unavailable")
    if proc.get("boot_id") != boot or tui.get("start_ticks") != ticks:
        raise BindingError("the linked Codex process identity changed")
    if tui.get("state") in ("Z", "X") or pane.get("state") in ("Z", "X"):
        raise BindingError("the linked Codex process exited")
    if os.path.basename(str(tui.get("argv0") or "")) != "codex":
        raise BindingError("the linked process id now runs another program")
    chain = proc.get("chain")
    if not isinstance(chain, list) or chain[:1] != [pid] or chain[-1:] != [row["pane_pid"]]:
        raise BindingError("the linked Codex process is no longer in this pane")
    if tui.get("tty") != tty or pane.get("tty") != tty:
        raise BindingError("the linked Codex terminal changed")
    if binding.get("pane_start_ticks") is not None and pane.get("start_ticks") != binding["pane_start_ticks"]:
        raise BindingError("the linked pane process identity changed")


async def _try_exact_recovery(
    rt: "Runtime", row: Any, binding: dict[str, Any], snapshot: "Snapshot | None",
) -> bool:
    """Repair only a false process-exit invalidation with the original proof."""
    if binding.get("stale_reason") != "the linked Codex process exited" or snapshot is None:
        return False
    if row["status"] != "live" or _stale_reason(row, binding, None) is not None:
        return False
    owner = snapshot.find(row["socket_path"], row["tmux_session_id"])
    pane = owner.pane(row["pane_id"]) if owner is not None else None
    socket = next((s for s in snapshot.sockets if s.path == row["socket_path"]), None)
    if (
        snapshot.env_id != row["env"] or owner is None or pane is None or pane.dead
        or socket is None or socket.status != "ok" or socket.server_pid != row["server_pid"]
        or owner.server_pid != row["server_pid"] or pane.pid != row["pane_pid"]
        or _other_live_rows(rt.db, binding["thread_id"], row["id"])
    ):
        return False
    try:
        env = rt.settings.env(row["env"])
        await _verify_original_process(rt, env, row, binding)
        proof = await asyncio.to_thread(verify_thread, env, binding["thread_id"], expected_path=binding["transcript_path"])
        if proof["rollout"] != binding.get("rollout") or proof["cli_version"] != binding.get("cli_version"):
            return False
        # Verify once more after the file/index read; a process could have
        # exited while the worker proved the original thread.
        await _verify_original_process(rt, env, row, binding)
    except (BindingError, OSError, TimeoutError, KeyError):
        return False
    with rt.db.tx():
        fresh = rt.db.get_session(row["id"])
        if (
            fresh is None or _guard_identity(fresh) != _guard_identity(row)
            or _other_live_rows(rt.db, binding["thread_id"], row["id"])
        ):
            return False
        recovered = dict(binding, status="verified", recovered_at=now())
        recovered.pop("stale_reason", None)
        recovered.pop("stale_at", None)
        rt.db.update_session(row["id"], meta_json=_with_binding(fresh, recovered))
    rt.bus.publish("sessions")
    log.info("recovered the original Codex thread link of %s", row["id"])
    return True


async def check_operator_binding(
    rt: "Runtime",
    evidence: dict[str, Any],
    item: dict[str, Any],
    *,
    snapshot: "Snapshot",
    max_age: float,
    rebind: bool = False,
) -> dict[str, Any]:
    """Validate one native-status binding against live state. Writes nothing.

    Returns a plan with the row id, the proof, and the fields to write.
    Raises ``BindingError`` with the first failed check.
    """
    env_id = _str_field(evidence, "env")
    if env_id not in rt.settings.envs:
        raise BindingError("unknown environment")
    env = rt.settings.env(env_id)
    socket = _str_field(evidence, "socket")
    server_pid = _int_field(evidence, "server_pid")
    if socket not in env.tmux_sockets:
        raise BindingError("socket is not a relay-managed tmux socket of this env")
    if evidence.get("container") not in (None, env.container):
        raise BindingError("container does not match the env")
    captured = _captured_at(evidence.get("captured_at"))
    age = now() - captured
    if age > max_age or age < -60:
        raise BindingError(f"evidence is not fresh (age {int(age)} s, limit {int(max_age)} s)")

    relay_id = _str_field(item, "relay_session_id")
    if not RELAY_ID_RE.match(relay_id):
        raise BindingError("relay_session_id is not a relay session id")
    tmux_session_id = _str_field(item, "tmux_session_id")
    pane_id = _str_field(item, "pane_id")
    pane_pid = _int_field(item, "pane_pid")
    tui_pid = _int_field(item, "tui_pid")
    tui_ticks = _int_field(item, "tui_start_ticks", "tui_starttime_ticks", "start_ticks")
    boot_id = item.get("boot_id") or evidence.get("boot_id")
    if not isinstance(boot_id, str) or not boot_id:
        raise BindingError("evidence field boot_id is required")
    thread_id = _str_field(item, "thread_id")
    transcript_path = _str_field(item, "transcript_path")
    if not UUID_RE.match(thread_id):
        raise BindingError("thread_id is not a Codex thread UUID")
    if item.get("meta_id") not in (None, thread_id):
        raise BindingError("meta_id differs from thread_id")

    # Live tmux: same server, the pane exists in the named tmux session, and its
    # process is the recorded one.
    sock = next((s for s in snapshot.sockets if s.path == socket), None)
    if sock is None or sock.status != "ok" or sock.server_pid != server_pid:
        raise BindingError("tmux server differs from the evidence")
    owner = next((s for s in snapshot.sessions if s.socket == socket and s.pane(pane_id) is not None), None)
    pane = owner.pane(pane_id) if owner is not None else None
    if owner is None or pane is None or pane.pid != pane_pid:
        raise BindingError("tmux pane differs from the evidence")
    if owner.id != tmux_session_id:
        raise BindingError("tmux session id differs from the evidence")
    if item.get("session_name") not in (None, owner.name):
        raise BindingError("tmux session name differs from the evidence")

    # Exactly one live Codex row for the pane, and it is the named relay row.
    rows = rt.db.query(
        "SELECT * FROM sessions WHERE status = 'live' AND env = ? AND socket_path = ? AND server_pid = ? AND pane_id = ?",
        (env_id, socket, server_pid, pane_id),
    )
    if len(rows) != 1:
        raise BindingError(f"expected one live row for the pane, found {len(rows)}")
    row = rows[0]
    if row["id"] != relay_id:
        raise BindingError("the pane row differs from the evidence relay_session_id")
    if row["tmux_session_id"] != tmux_session_id:
        raise BindingError("the row tmux session id differs from the evidence")
    if row["kind"] != "codex":
        raise BindingError("the pane row is not a Codex session")
    if row["pane_pid"] != pane_pid:
        raise BindingError("the row pane process differs from the evidence")
    current = binding_of(row)
    if not rebind:
        if is_verified(current) and current["thread_id"] != thread_id:
            raise BindingError("the row is linked to another thread; a new link needs --rebind")
        if row["agent_session_id"] and row["agent_session_id"] != thread_id:
            raise BindingError("the row names another thread; a new link needs --rebind")
    if _other_live_rows(rt.db, thread_id, row["id"]):
        raise BindingError("another live row names this thread")

    # The TUI process: same boot, same start, under the pane, on the pane tty.
    proc = await _probe_process(rt, env, tui_pid, pane_pid)
    tui = proc.get("tui") or {}
    pane_proc = proc.get("pane") or {}
    if proc.get("boot_id") != boot_id:
        raise BindingError("boot id differs from the evidence")
    if tui.get("start_ticks") != tui_ticks:
        raise BindingError("TUI process start differs from the evidence (pid reused or restarted)")
    if tui.get("state") == "Z":
        raise BindingError("TUI process has exited")
    if os.path.basename(str(tui.get("argv0") or "")) != "codex":
        raise BindingError("TUI process is not a Codex binary")
    chain = proc.get("chain")
    if not isinstance(chain, list) or chain[-1:] != [pane_pid]:
        raise BindingError("TUI process does not descend from the pane process")
    tty = tui.get("tty")
    if not isinstance(tty, str) or not tty.startswith("/dev/pts/") or tty != pane_proc.get("tty"):
        raise BindingError("TUI process is not on the pane terminal")
    pane_ticks = item.get("pane_start_ticks")
    if isinstance(pane_ticks, int) and pane_proc.get("start_ticks") != pane_ticks:
        raise BindingError("pane process start differs from the evidence")

    # The thread: state index row, exact path, metadata id, top level.
    proof = verify_thread(env, thread_id, expected_path=transcript_path)

    binding = {
        "v": 1,
        "status": "verified",
        "source": "operator",
        "env": env_id,
        "method": evidence.get("method") if isinstance(evidence.get("method"), str) else None,
        "captured_at": evidence.get("captured_at"),
        "thread_id": thread_id,
        "transcript_path": transcript_path,
        "cli_version": proof["cli_version"],
        "rollout": proof["rollout"],
        "pane": {
            "socket": socket,
            "server_pid": server_pid,
            "tmux_session_id": tmux_session_id,
            "pane_id": pane_id,
            "pane_pid": pane_pid,
        },
        "session_name": owner.name,
        "tui_pid": tui_pid,
        "tui_start_ticks": tui_ticks,
        "tui_tty": tty,
        "pane_start_ticks": pane_proc.get("start_ticks"),
        "boot_id": boot_id,
        "bound_at": now(),
    }
    if current is not None and current.get("thread_id") != thread_id:
        binding["replaced_thread_id"] = current.get("thread_id")
    fields: dict[str, Any] = {"agent_session_id": thread_id, "transcript_path": transcript_path}
    project = _project_dir(env, proof["cwd"])
    if project and not row["project_dir"]:
        fields["project_dir"] = project
    return {
        "row_id": row["id"],
        "env": env_id,
        "pane": binding["pane"],
        "session_name": owner.name,
        "thread_id": thread_id,
        "binding": binding,
        "fields": fields,
        "identity": _row_identity(row),
    }


def _row_identity(row: Any) -> tuple[Any, ...]:
    """Fields that must not change between validation and the write."""
    return (
        row["status"],
        row["kind"],
        row["socket_path"],
        row["server_pid"],
        row["pane_id"],
        row["pane_pid"],
        row["tmux_session_id"],
        row["agent_session_id"],
    )


async def bind_operator_evidence(
    rt: "Runtime",
    evidence: dict[str, Any],
    *,
    apply: bool = False,
    rebind: bool = False,
    max_age: float = 900.0,
) -> dict[str, Any]:
    """Validate every binding of an evidence file; write all of them or none.

    With ``apply=False`` (the default) nothing is written. The report holds
    ids and check results only.
    """
    report: dict[str, Any] = {"ok": False, "applied": False, "results": [], "error": None}
    items = evidence.get("bindings") if isinstance(evidence, dict) else None
    if not isinstance(items, list) or not items:
        report["error"] = "evidence has no bindings"
        return report
    try:
        env_id = _str_field(evidence, "env")
        if env_id not in rt.settings.envs:
            raise BindingError("unknown environment")
        snapshot = await rt.tmux_for(env_id).snapshot()
    except Exception as exc:  # noqa: BLE001 - reported, nothing written
        report["error"] = f"cannot read live tmux state: {exc}"
        return report

    plans: list[dict[str, Any]] = []
    for item in items:
        name = item.get("session_name") if isinstance(item, dict) else None
        try:
            if not isinstance(item, dict):
                raise BindingError("binding must be an object")
            plan = await check_operator_binding(rt, evidence, item, snapshot=snapshot, max_age=max_age, rebind=rebind)
        except (BindingError, OSError, TimeoutError) as exc:
            report["results"].append({"session_name": name, "ok": False, "error": str(exc)})
            continue
        plans.append(plan)
        report["results"].append(
            {"session_name": name, "ok": True, "row_id": plan["row_id"], "thread_id": plan["thread_id"], "pane": plan["pane"]}
        )
    rows = [p["row_id"] for p in plans]
    threads = [p["thread_id"] for p in plans]
    if len(plans) != len(items):
        report["error"] = "one or more bindings failed; nothing written"
        return report
    if len(set(rows)) != len(rows) or len(set(threads)) != len(threads):
        report["error"] = "evidence links one row or one thread twice; nothing written"
        return report
    report["ok"] = True
    if not apply:
        return report

    with rt.db.tx():
        for plan in plans:
            row = rt.db.get_session(plan["row_id"])
            if row is None or _row_identity(row) != plan["identity"]:
                raise BindingError("a row changed during validation; run the bind again")
        for plan in plans:
            row = rt.db.get_session(plan["row_id"])
            rt.db.update_session(plan["row_id"], meta_json=_with_binding(row, plan["binding"]), **plan["fields"])
    report["applied"] = True
    return report


# -- native binding ---------------------------------------------------------------

# Runs inside the container with the tmux socket, the pane id, the pane pid and
# the Codex home as arguments. Prints one JSON object of identity fields only:
# the boot id, the pane process, the Codex TUI processes under the pane (a codex
# process below another one is a child, not a TUI), the copy mode and title of
# the pane, and each thread writer lock with the role of its holder: ``tui``,
# ``daemon`` (``codex app-server --managed-daemon``) or ``other``. A lock file
# counts only as a regular file with one link and an inode no other lock name
# shares, so a symlink or hardlink alias cannot rename a thread.
_NATIVE_PROBE = r"""
import json, os, re, stat as st_mod, subprocess, sys
LOCK = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.lock$")
def lock_files(lockdir):
    if not st_mod.S_ISDIR(os.lstat(lockdir).st_mode):
        raise NotADirectoryError(lockdir)
    files, dups = {}, set()
    for name in sorted(os.listdir(lockdir))[:4096]:
        m = LOCK.match(name)
        if not m:
            continue
        info = os.lstat(os.path.join(lockdir, name))
        if not st_mod.S_ISREG(info.st_mode) or info.st_nlink != 1:
            continue
        key = (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)
        if key in files:
            dups.add(key)
        files[key] = m.group(1)
    for key in dups:
        files.pop(key, None)
    return files
def held_locks(lines, files, roles):
    locks = []
    for line in lines:
        parts = line.split()
        if len(parts) < 6 or parts[1] != "FLOCK" or parts[3] != "WRITE":
            continue
        try:
            pid = int(parts[4])
            major, minor, ino = parts[5].split(":")
            key = (int(major, 16), int(minor, 16), int(ino))
        except ValueError:
            continue
        if key in files:
            locks.append({"thread": files[key], "pid": pid, "role": roles.get(pid, "other")})
    return locks[:256]
def stat(pid):
    with open("/proc/%d/stat" % pid, "rb") as fh:
        data = fh.read().decode("utf-8", "replace")
    rest = data[data.rindex(")") + 2:].split()
    return {"state": rest[0], "ppid": int(rest[1]), "start_ticks": int(rest[19])}
def tty(pid):
    try:
        return os.readlink("/proc/%d/fd/0" % pid)
    except OSError:
        return None
def argv(pid):
    with open("/proc/%d/cmdline" % pid, "rb") as fh:
        return [p.decode("utf-8", "replace") for p in fh.read(8192).split(b"\0")[:32]]
def main():
    out = {}
    try:
        out["stage"] = "boot"
        with open("/proc/sys/kernel/random/boot_id") as fh:
            out["boot_id"] = fh.read().strip()
        with open("/proc/uptime") as fh:
            out["uptime"] = float(fh.read().split()[0])
        out["clk_tck"] = os.sysconf("SC_CLK_TCK")
        sock, pane_id, pane, home = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
        out["stage"] = "pane"
        out["pane"] = dict(stat(pane), tty=tty(pane))
        out["stage"] = "procs"
        procs = {}
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    procs[int(name)] = stat(int(name))
                except (OSError, ValueError, IndexError):
                    pass
        def chain(pid):
            seen = []
            for _ in range(64):
                seen.append(pid)
                if pid == pane:
                    return seen
                pid = procs.get(pid, {}).get("ppid", 0)
                if pid <= 1:
                    return None
            return None
        roles, cands = {}, {}
        for pid in procs:
            try:
                args = argv(pid)
            except OSError:
                continue
            if not args or os.path.basename(args[0]) != "codex":
                continue
            if "app-server" in args[1:]:
                if "--managed-daemon" in args[1:]:
                    roles[pid] = "daemon"
                continue
            path = chain(pid)
            if path is not None and pid != pane:
                cands[pid] = path
        tuis = []
        for pid, path in cands.items():
            if not any(p in cands for p in path[1:]):
                tuis.append(dict(procs[pid], pid=pid, tty=tty(pid)))
                roles[pid] = "tui"
        out["tuis"] = tuis[:8]
        out["stage"] = "title"
        res = subprocess.run(["tmux", "-S", sock, "display-message", "-p", "-t", pane_id,
                              "#{pane_in_mode}|~|#{pane_title}"], capture_output=True, timeout=5)
        mode, sep, title = res.stdout.decode("utf-8", "replace").rstrip("\n").partition("|~|")
        out["in_mode"] = (mode == "1") if res.returncode == 0 and sep else None
        out["title"] = title[:512] if res.returncode == 0 and sep else None
        out["stage"] = "locks"
        files = lock_files(os.path.join(home, "thread-writer-locks"))
        with open("/proc/locks") as fh:
            out["locks"] = held_locks(fh, files, roles)
        out["stage"] = "done"
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        out["error"] = type(exc).__name__
    print(json.dumps(out))
if __name__ == "__main__":
    main()
"""

# Codex 0.160 screen text used by the /status bootstrap (captured 2026-10-04):
# the empty composer, the composer with the typed command, the command echo,
# and the card line with the thread id.
_COMPOSER_EMPTY_RE = re.compile(r"^\s*›\s+Ask Codex to do anything\s*$", re.M)
_COMPOSER_STATUS_RE = re.compile(r"^\s*›\s+/status\s*$", re.M)
_TURN_RUNNING_RE = re.compile(r"esc to interrupt", re.I)
_CHOICE_RE = re.compile(r"^\s*(?:[›❯>▶]\s*)?\d+\.\s+\S", re.M)  # a numbered dialog choice
_STATUS_ECHO_RE = re.compile(r"^/status\s*$")
# The Session field of the card. A wide pane prints ``Session: <id>`` on one
# line. A narrow one (51 columns, Codex 0.162.1, captured 2026-10-11) prints
# the label alone, or with the start of the id, and wraps the rest of the id
# onto indented value lines. Only lines of id characters directly under the
# label are the field's value.
_STATUS_SESSION_RE = re.compile(r"^\s*Session:(.*)$")
_STATUS_VALUE_RE = re.compile(r"^\s+([0-9a-f-]{1,36})\s*$")
_STATUS_VALUE_HEAD_RE = re.compile(r"^(?:\s+([0-9a-f-]{1,36}))?\s*$")
_STATUS_WINDOW = 60  # scrollback lines in each /status capture


def _native_due(rt: "Runtime", row: Any) -> bool:
    checked = rt.cache.setdefault("codex_native_checked:" + row["env"], {})
    at = time.monotonic()
    last = checked.get(row["id"])
    if last is not None and at - last < NATIVE_CHECK_INTERVAL:
        return False
    checked[row["id"]] = at
    return True


async def _native_probe(rt: "Runtime", env: EnvConfig, row: Any) -> dict[str, Any]:
    socket, pane_id, pane_pid = row["socket_path"], row["pane_id"], row["pane_pid"]
    if (
        socket not in env.tmux_sockets
        or not isinstance(pane_id, str) or not _PANE_ID_RE.match(pane_id)
        or not isinstance(pane_pid, int) or isinstance(pane_pid, bool) or pane_pid <= 0
    ):
        raise NativeEvidenceMissing("the pane identity of the row is not known yet")
    res = await rt.podman.exec(
        env, ["python3", "-c", _NATIVE_PROBE, socket, pane_id, str(pane_pid), f"{env.home}/.codex"], timeout=15
    )
    if not res.ok:
        raise ProcessProbeUnavailable("native probe failed in the container")
    try:
        data = json.loads(res.out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ProcessProbeUnavailable("native probe output is unreadable") from None
    if not isinstance(data, dict) or data.get("error") or data.get("stage") != "done":
        raise ProcessProbeUnavailable("native process state is unavailable")
    return data


def _probe_tui(probe: dict[str, Any]) -> dict[str, Any]:
    """The one Codex TUI of the pane. It must run on the pane terminal."""
    tuis, pane = probe.get("tuis"), probe.get("pane")
    if not isinstance(tuis, list) or len(tuis) != 1 or not isinstance(tuis[0], dict) or not isinstance(pane, dict):
        raise NativeEvidenceMissing("expected exactly one Codex process in the pane")
    tui = tuis[0]
    pid, ticks, tty = tui.get("pid"), tui.get("start_ticks"), tui.get("tty")
    if (
        not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
        or not isinstance(ticks, int) or isinstance(ticks, bool) or ticks <= 0
        or not isinstance(tty, str) or not tty.startswith("/dev/pts/") or tty != pane.get("tty")
    ):
        raise NativeEvidenceMissing("the Codex process is not on the pane terminal")
    return tui


def _probe_locks(probe: dict[str, Any]) -> list[dict[str, Any]]:
    locks = probe.get("locks")
    if not isinstance(locks, list):
        return []
    return [lk for lk in locks if isinstance(lk, dict) and isinstance(lk.get("thread"), str) and UUID_RE.match(lk["thread"])]


def title_thread_prefix(title: Any) -> str | None:
    """The 29-character id prefix of the native ``thread-id`` field, else ``None``.

    The field must end the title, start after a native separator (or start
    the title), and have exactly the native cut shape. Every other part of the
    title is ignored, so a thread name that holds an id never names a thread.
    """
    if not isinstance(title, str):
        return None
    match = _TITLE_ID_FIELD_RE.search(title)
    return match.group(1) if match else None


def title_field_configured(env: EnvConfig) -> bool:
    """True when the env's Codex config ends ``tui.terminal_title`` with the native id item.

    Only that layout puts the id in the last field; any other layout gives no
    title evidence. The config is read only; no value is logged.
    """
    path = env.host_codex_config
    try:
        if path.stat().st_size > _CONFIG_MAX_BYTES:
            return False
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    tui = data.get("tui")
    items = tui.get("terminal_title") if isinstance(tui, dict) else None
    return isinstance(items, list) and bool(items) and items[-1] in TITLE_ID_ITEMS


def _tui_age(probe: dict[str, Any], tui: dict[str, Any]) -> float | None:
    """Seconds since the TUI process started, from the probe's uptime and clock ticks."""
    uptime, hz = probe.get("uptime"), probe.get("clk_tck")
    if not isinstance(uptime, (int, float)) or isinstance(uptime, bool) or not isinstance(hz, int) or hz <= 0:
        return None
    return float(uptime) - tui["start_ticks"] / hz


def _thread_kind(env: EnvConfig, thread_id: str) -> str:
    """``top`` (verified top-level thread), ``unsaved`` (no index row yet) or ``other``."""
    try:
        verify_thread(env, thread_id)
    except ThreadNotIndexed:
        return "unsaved"
    except (BindingError, OSError):
        return "other"
    return "top"


def select_native_thread(env: EnvConfig, probe: dict[str, Any], *, title_trusted: bool) -> tuple[str, str]:
    """The thread that native state names for the pane's TUI, and the method.

    A TUI that holds thread writer locks has an embedded app server: its one
    top-level thread is the evidence (the title is not used). A TUI without
    locks is a shared-daemon client. Its title counts only when this process
    wrote it (``title_trusted``, see ``_observe_title``) and the env config
    ends with the native item (``title_field_configured``). Then the field
    (``title_thread_prefix``) must name exactly one thread that the daemon
    holds and exactly one thread of the state index. Runs in a worker thread.
    Raises ``NativeEvidenceMissing`` otherwise; ``TitleWithoutThread`` when the
    daemon client has no usable title field.
    """
    tui = _probe_tui(probe)
    locks = _probe_locks(probe)
    own = sorted({lk["thread"] for lk in locks if lk.get("pid") == tui["pid"]})
    if own:
        kinds = {t: _thread_kind(env, t) for t in own}
        candidates = [t for t, kind in kinds.items() if kind in ("top", "unsaved")]
        if len(candidates) != 1:
            raise NativeEvidenceMissing("the Codex process holds no single top-level thread")
        thread_id = candidates[0]
        if kinds[thread_id] == "unsaved":
            raise ThreadNotSaved(thread_id)
        return thread_id, "writer_lock"
    if not title_trusted:
        raise TitleWithoutThread("this Codex process has not written the pane title yet")
    prefix = title_thread_prefix(probe.get("title")) if title_field_configured(env) else None
    if prefix is None:
        raise TitleWithoutThread("the terminal title has no native thread-id field")
    held = sorted({lk["thread"] for lk in locks if lk.get("role") == "daemon" and lk["thread"].startswith(prefix)})
    if len(held) != 1:
        raise NativeEvidenceMissing("the shared Codex server holds no single thread for the terminal title")
    kind = _thread_kind(env, held[0])
    if kind == "unsaved":
        raise ThreadNotSaved(held[0])
    if kind != "top" or count_thread_prefix(env, prefix) != 1:
        raise NativeEvidenceMissing("the terminal title names no single top-level thread")
    return held[0], "terminal_title"


async def _bootstrap_useful(rt: "Runtime", env: EnvConfig, row: Any, probe: dict[str, Any]) -> bool:
    """True when the daemon holds a saved top-level thread that no other live row names.

    Without one, /status cannot link this pane (for example a new TUI before
    its first prompt), so relay does not type it.
    """
    held = sorted({lk["thread"] for lk in _probe_locks(probe) if lk.get("role") == "daemon"})
    free = [t for t in held if not _other_live_rows(rt.db, t, row["id"])]
    return await asyncio.to_thread(lambda: any(_thread_kind(env, t) == "top" for t in free))


def _tui_key(probe: dict[str, Any], tui: dict[str, Any]) -> tuple[Any, int, int]:
    """One TUI process: boot id, pid, start ticks."""
    return (probe.get("boot_id"), tui["pid"], tui["start_ticks"])


def _proves_tui(binding: dict[str, Any] | None, key: tuple[Any, int, int]) -> bool:
    """A verified link whose process proof is exactly this TUI process (survives a relay restart)."""
    return is_verified(binding) and (binding.get("boot_id"), binding.get("tui_pid"), binding.get("tui_start_ticks")) == key


def _tui_state(rt: "Runtime", env_id: str, key: tuple[Any, int, int]) -> dict[str, Any]:
    """In-memory state of one TUI process: first title seen, title written, /status tried."""
    states = rt.cache.setdefault("codex_tui_state:" + env_id, {})
    at = time.monotonic()
    for old in [k for k, s in states.items() if at - s["seen_at"] > _TUI_STATE_TTL]:
        states.pop(old, None)
    state = states.setdefault(key, {"first_title": None, "title_written": False, "status_tried": False})
    state["seen_at"] = at
    return state


def _restart_title_proof(binding: dict[str, Any] | None, key: tuple[Any, int, int], title: Any) -> bool:
    """After a relay restart, does a verified link prove that this process wrote the title?

    Only for exactly this process (boot id, pid, start ticks). A link by the
    managed title field proves it. A /status link proves it only when the
    title differs from the one at link time (``title_hash``), because an old
    process's or an old layout's title can remain. A writer-lock link never
    proves it. The caller checks the original process again before it trusts.
    """
    if not _proves_tui(binding, key):
        return False
    if binding.get("method") == "terminal_title":
        return True
    saved = binding.get("title_hash")
    return (
        binding.get("method") == "status" and isinstance(saved, str) and isinstance(title, str)
        and _text_hash(title) != saved
    )


def _status_attempted(row: Any, key: tuple[Any, int, int]) -> bool:
    """True when relay has typed /status (or was about to) for exactly this process."""
    meta = loads(row["meta_json"])
    mark = meta.get(STATUS_ATTEMPT_KEY) if isinstance(meta, dict) else None
    return isinstance(mark, dict) and (mark.get("boot_id"), mark.get("tui_pid"), mark.get("tui_start_ticks")) == key


def _mark_status_attempt(rt: "Runtime", row: Any, key: tuple[Any, int, int]) -> bool:
    """Persist the one /status attempt for this process before any key is sent.

    The marker replaces the one of an earlier process and keeps the other
    meta keys. Written only when the row identity is unchanged; a crash after
    this write leaves no second attempt (a later title proof still links).
    """
    with rt.db.tx():
        fresh = rt.db.get_session(row["id"])
        if fresh is None or fresh["status"] != "live" or _guard_identity(fresh) != _guard_identity(row):
            return False
        meta = loads(fresh["meta_json"])
        meta = meta if isinstance(meta, dict) else {}
        meta[STATUS_ATTEMPT_KEY] = {"boot_id": key[0], "tui_pid": key[1], "tui_start_ticks": key[2], "at": now()}
        rt.db.update_session(row["id"], meta_json=dumps(meta))
    return True


async def _check_tui_alive(rt: "Runtime", env: EnvConfig, row: Any, probe: dict[str, Any]) -> None:
    """The probed TUI is still exactly the same live process on the pane terminal."""
    tui = _probe_tui(probe)
    proc = await _probe_process(rt, env, tui["pid"], row["pane_pid"])
    now_tui = proc.get("tui") or {}
    chain = proc.get("chain")
    if (
        proc.get("boot_id") != probe.get("boot_id") or now_tui.get("start_ticks") != tui["start_ticks"]
        or now_tui.get("state") in ("Z", "X") or now_tui.get("tty") != tui["tty"]
        or os.path.basename(str(now_tui.get("argv0") or "")) != "codex"
        or not isinstance(chain, list) or chain[:1] != [tui["pid"]] or chain[-1:] != [row["pane_pid"]]
    ):
        raise NativeSourceChanged("the Codex process changed before /status")


def _observe_title(state: dict[str, Any], title: Any) -> bool:
    """True once this process has written the pane title.

    tmux keeps a title after its process ends, so the first title seen for a
    process may be an old one. A later different title was written by this
    live process (no other process writes this pane's title).
    """
    if isinstance(title, str):
        if state["first_title"] is None:
            state["first_title"] = title
        elif title != state["first_title"]:
            state["title_written"] = True
    return bool(state["title_written"])


def _same_tui(binding: dict[str, Any], tui: dict[str, Any]) -> bool:
    """A link without process proof (launch, resume) follows the thread only."""
    if binding.get("tui_pid") is None:
        return True
    return (binding.get("tui_pid"), binding.get("tui_start_ticks")) == (tui["pid"], tui["start_ticks"])


def _check_status_thread(env: EnvConfig, probe: dict[str, Any], thread_id: str) -> None:
    """A /status thread must be held by the shared daemon, and the TUI must be its client."""
    tui = _probe_tui(probe)
    locks = _probe_locks(probe)
    if any(lk.get("pid") == tui["pid"] for lk in locks):
        raise NativeEvidenceMissing("the Codex process holds its own thread locks")
    if thread_id not in {lk["thread"] for lk in locks if lk.get("role") == "daemon"}:
        raise NativeEvidenceMissing("the shared Codex server does not hold the /status thread")
    if _thread_kind(env, thread_id) == "unsaved":
        raise ThreadNotSaved(thread_id)


def _screen_tail(text: str, lines: int) -> str:
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])


def composer_idle_empty(text: str) -> bool:
    """True when a Codex screen shows no running turn, no dialog, and an empty composer.

    The empty composer must be the last prompt line of the bottom 12 lines. A
    Codex approval or trust dialog replaces the composer and offers numbered
    choices, so a dialog phrase counts in the composer area below it, or with
    a numbered choice in those 12 lines. Agent prose above the composer (for
    example "Do you approve this mockup?") is not a dialog.
    """
    lines = text.rstrip("\n").splitlines()[-12:]
    at = next((i for i in range(len(lines) - 1, -1, -1) if _COMPOSER_EMPTY_RE.match(lines[i])), None)
    if at is None or any(line.lstrip().startswith("›") for line in lines[at + 1:]):
        return False
    tail, pane = "\n".join(lines), "\n".join(lines[at:])

    def dialog(part: str) -> bool:
        return screen.permission_dialog_visible(part, tail_lines=12) or screen.trust_dialog_visible(part, tail_lines=12)

    return not _TURN_RUNNING_RE.search(tail) and not dialog(pane) and not (dialog(tail) and _CHOICE_RE.search(tail))


def _status_session_value(lines: list[str], at: int) -> str | None:
    """The id in the Session field that starts at ``lines[at]``, else ``None`` (malformed).

    The value is the id characters after the label on the same line plus the
    value lines directly below it, joined until they hold 36 characters. It
    must then be exactly one UUID, and no further value line may follow.
    """
    head = _STATUS_VALUE_HEAD_RE.match(_STATUS_SESSION_RE.match(lines[at]).group(1))
    if head is None:
        return None  # text that is not an id after the label
    value = head.group(1) or ""
    i = at + 1
    while len(value) < 36 and i < len(lines) and (part := _STATUS_VALUE_RE.match(lines[i])):
        value += part.group(1)
        i += 1
    if i < len(lines) and _STATUS_VALUE_RE.match(lines[i]):
        return None  # the value goes on past one id
    return value if UUID_RE.match(value) else None


def status_session_id(before: str, after: str) -> str | None:
    """The thread id of the one new native /status card in ``after``, else ``None``.

    ``after`` must hold exactly one more command echo than ``before`` (same
    capture window), so an older card is never read. The card after the newest
    echo must name exactly one thread in its Session field (one line, or the
    label with the id wrapped onto value lines below it). A malformed or
    truncated Session value, or two different ids, gives ``None``. Ids in
    other fields or other text are never read.
    """
    old = [i for i, line in enumerate(before.splitlines()) if _STATUS_ECHO_RE.match(line)]
    lines = after.splitlines()
    new = [i for i, line in enumerate(lines) if _STATUS_ECHO_RE.match(line)]
    if len(new) != len(old) + 1:
        return None
    ids: set[str] = set()
    for at in range(new[-1] + 1, len(lines)):
        if _STATUS_SESSION_RE.match(lines[at]):
            value = _status_session_value(lines, at)
            if value is None:
                return None
            ids.add(value)
    return ids.pop() if len(ids) == 1 else None


async def _status_bootstrap(rt: "Runtime", row: Any, probe: dict[str, Any], state: dict[str, Any]) -> str | None:
    """Read the native /status thread of an idle shared-daemon TUI, once per process.

    Guard: the pane is not in copy mode, the screen shows no running turn and
    no dialog, and the composer is empty. relay types ``/status`` and presses
    Enter only when the composer then shows exactly that command, so a user's
    text is never submitted. The row lock keeps relay's own prompts out.
    Before the first key, relay checks the exact live process again and
    stores an attempt marker for it (boot id, pid, start ticks) in the row
    meta (``STATUS_ATTEMPT_KEY``). With that marker relay never types the
    command again for this process, also after a timeout, a composer change,
    or a relay restart; a later title written by the process can still prove
    its thread.
    """
    tui = _probe_tui(probe)
    age = _tui_age(probe, tui)
    if state["status_tried"] or probe.get("in_mode") is not False or age is None or age < STATUS_BOOTSTRAP_MIN_AGE:
        return None
    tmux = rt.tmux_for(row["env"])
    socket, pane_id = row["socket_path"], row["pane_id"]
    async with rt.lock_for(row["id"]):
        before = await tmux.capture(socket, pane_id, lines=_STATUS_WINDOW)
        if not composer_idle_empty(before):
            return None  # not an attempt: a later idle poll can try
        await _check_tui_alive(rt, rt.settings.env(row["env"]), row, probe)
        if not _mark_status_attempt(rt, row, _tui_key(probe, tui)):
            return None
        state["status_tried"] = True
        await tmux.send_keys(socket, pane_id, "-l", "/status")
        await asyncio.sleep(0.4)
        typed = await tmux.capture(socket, pane_id, lines=0)
        if not _COMPOSER_STATUS_RE.search(_screen_tail(typed, 12)):
            log.warning("Codex composer of %s changed; /status was not submitted", row["id"])
            return None
        await tmux.send_keys(socket, pane_id, "Enter")
        for _ in range(4):
            await asyncio.sleep(0.5)
            after = await tmux.capture(socket, pane_id, lines=_STATUS_WINDOW)
            thread_id = status_session_id(before, after)
            if thread_id is not None:
                state["status_screen"] = _text_hash(after)  # in memory only, never the text
                log.info("read the native /status thread of %s", row["id"])
                return thread_id
    return None


def _text_hash(text: str) -> str:
    """SHA-256 of a screen or title: relay keeps this, never the text."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


async def _recheck_native(
    rt: "Runtime", env: EnvConfig, row: Any, probe: dict[str, Any], state: dict[str, Any], thread_id: str, method: str,
) -> dict[str, Any]:
    """Read native state again; it must still name ``thread_id`` from the same process and source.

    Runs last before the write. For ``/status``, the daemon can keep holding
    the old thread after a ``/new``, so the lock alone proves nothing: a title
    this process wrote must name the same thread, or else the pane screen must
    still be exactly the screen that showed the /status card (same hash, same
    capture window, not in copy mode; no command, reset or typing since).
    """
    again = await _native_probe(rt, env, row)
    if _tui_key(again, _probe_tui(again)) != _tui_key(probe, _probe_tui(probe)):
        raise NativeSourceChanged("the Codex process changed during the checks")
    trusted = _observe_title(state, again.get("title"))
    if method == "status":
        await asyncio.to_thread(_check_status_thread, env, again, thread_id)
        configured = trusted and await asyncio.to_thread(title_field_configured, env)
        prefix = title_thread_prefix(again.get("title")) if configured else None
        if prefix is not None:
            if not thread_id.startswith(prefix):
                raise NativeSourceChanged("the Codex TUI shows another thread now")
            return again
        if again.get("in_mode") is not False or state.get("status_screen") is None:
            raise NativeSourceChanged("the /status screen cannot be confirmed")
        now_screen = await rt.tmux_for(row["env"]).capture(row["socket_path"], row["pane_id"], lines=_STATUS_WINDOW)
        if _text_hash(now_screen) != state["status_screen"]:
            raise NativeSourceChanged("the Codex screen changed after /status")
        return again
    if await asyncio.to_thread(select_native_thread, env, again, title_trusted=trusted) != (thread_id, method):
        raise NativeSourceChanged("the native source changed during the checks")
    return again


async def _link_native(
    rt: "Runtime", env: EnvConfig, row: Any, snapshot: "Snapshot", probe: dict[str, Any], state: dict[str, Any],
    thread_id: str, method: str,
) -> bool:
    """Write a native link after the operator checks (pane, process, thread) and a second native read."""
    tui = _probe_tui(probe)
    info = await asyncio.to_thread(lookup_thread, env, thread_id)
    evidence = {
        "env": row["env"], "socket": row["socket_path"], "server_pid": row["server_pid"], "container": env.container,
        "captured_at": datetime.now(timezone.utc).isoformat(), "method": method, "boot_id": probe.get("boot_id"),
    }
    item = {
        "relay_session_id": row["id"], "tmux_session_id": row["tmux_session_id"], "pane_id": row["pane_id"],
        "pane_pid": row["pane_pid"], "tui_pid": tui["pid"], "tui_start_ticks": tui["start_ticks"],
        "pane_start_ticks": probe["pane"].get("start_ticks"), "thread_id": thread_id,
        "transcript_path": info["rollout_path"],
    }
    plan = await check_operator_binding(rt, evidence, item, snapshot=snapshot, max_age=_NATIVE_MAX_AGE, rebind=True)
    again = await _recheck_native(rt, env, row, probe, state, thread_id, method)
    title = again.get("title")
    with rt.db.tx():
        fresh = rt.db.get_session(row["id"])
        if fresh is None or _guard_identity(fresh) != _guard_identity(row):
            return False
        # The title hash (never the text) lets a restart tell a title this
        # process wrote later from the one it showed at link time.
        binding = dict(plan["binding"], source="native", title_hash=_text_hash(title) if isinstance(title, str) else None)
        rt.db.update_session(row["id"], meta_json=_with_binding(fresh, binding), **plan["fields"])
    _cursors(rt).pop(row["id"], None)
    rt.bus.publish("sessions")
    log.info("linked %s to Codex thread %s (native %s)", row["id"], thread_id, method)
    return True


def _stale_if_unchanged(rt: "Runtime", row: Any, reason: str) -> bool:
    fresh = rt.db.get_session(row["id"])
    if fresh is None or _guard_identity(fresh) != _guard_identity(row):
        return False
    mark_stale(rt, fresh, reason)
    return True


async def _try_native_link(rt: "Runtime", env: EnvConfig, row: Any, snapshot: "Snapshot | None") -> bool:
    """Link, replace, or invalidate a pane's thread link from fresh native evidence.

    Returns True when the row changed. Missing or uncertain evidence changes
    nothing and a later poll retries (at most every ``NATIVE_CHECK_INTERVAL``).
    Evidence of another thread replaces a verified link after the full checks;
    when those checks fail, or the thread is not saved yet, the link is stale.
    A shared-daemon TUI process that no verified link proves gets one guarded
    /status; its title counts only after the process wrote one.
    """
    if snapshot is None or row["status"] != "live" or not _native_due(rt, row):
        return False
    binding = binding_of(row)
    verified = is_verified(binding)
    try:
        probe = await _native_probe(rt, env, row)
        tui = _probe_tui(probe)
        key = _tui_key(probe, tui)
        state = _tui_state(rt, row["env"], key)
        trusted = _observe_title(state, probe.get("title"))
        if not trusted and _restart_title_proof(binding, key, probe.get("title")):
            await _verify_original_process(rt, env, row, binding)
            state["title_written"] = trusted = True
        if _status_attempted(row, key):
            state["status_tried"] = True
        try:
            thread_id, method = await asyncio.to_thread(select_native_thread, env, probe, title_trusted=trusted)
        except TitleWithoutThread:
            if _proves_tui(binding, key) or not await _bootstrap_useful(rt, env, row, probe):
                return False
            status_thread = await _status_bootstrap(rt, row, probe, state)
            if status_thread is None:
                return False
            await asyncio.to_thread(_check_status_thread, env, probe, status_thread)
            thread_id, method = status_thread, "status"
        if verified and binding["thread_id"] == thread_id and _same_tui(binding, tui):
            return False
        try:
            return await _link_native(rt, env, row, snapshot, probe, state, thread_id, method)
        except (ProcessProbeUnavailable, NativeEvidenceMissing, OSError, TimeoutError, CommandError):
            raise
        except BindingError as exc:
            if not verified:
                raise
            return _stale_if_unchanged(rt, row, f"the Codex TUI shows another thread that is not verified: {exc}")
    except ThreadNotSaved as exc:
        if not verified or binding["thread_id"] == exc.thread_id:
            return False
        return _stale_if_unchanged(rt, row, "the Codex TUI switched to a thread that Codex has not saved yet")
    except (BindingError, OSError, TimeoutError, KeyError, ValueError, CommandError) as exc:
        log.debug("native Codex link for %s not verified: %s", row["id"], exc)
        return False


def _load_evidence(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 1024 * 1024:
        raise BindingError("evidence file is too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise BindingError("evidence must be a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    """``python -m relay.codex_runtime bind --evidence FILE [--apply] [--rebind]``."""
    parser = argparse.ArgumentParser(prog="python -m relay.codex_runtime")
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind", help="validate (and with --apply, store) native Codex thread links")
    bind.add_argument("--evidence", type=Path, required=True, help="native /status evidence JSON")
    bind.add_argument("--apply", action="store_true", help="write the links (default: check only)")
    bind.add_argument("--rebind", action="store_true", help="replace an existing link with this fresh proof")
    bind.add_argument("--max-age-seconds", type=float, default=900.0, help="maximum evidence age (default 900)")
    bind.add_argument("--config", type=Path, default=None, help="relay config file")
    args = parser.parse_args(argv)

    from .config import load_settings
    from .runtime import build_runtime

    try:
        evidence = _load_evidence(args.evidence)
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"evidence unreadable: {exc}"}))
        return 2
    rt = build_runtime(load_settings(args.config))
    try:
        report = asyncio.run(
            bind_operator_evidence(rt, evidence, apply=args.apply, rebind=args.rebind, max_age=args.max_age_seconds)
        )
    except BindingError as exc:
        report = {"ok": False, "applied": False, "error": str(exc)}
    finally:
        rt.db.close()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    sys.exit(main())
