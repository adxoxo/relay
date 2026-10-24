"""Older agent output for the Terminal tab, read page by page from the session's own transcript.

Agent TUIs (Claude, Codex) draw on the alternate screen, so tmux keeps no
history of their output and copy mode has nothing older to show. The terminal
bridge then reads the agent transcript of that session and sends it to the
browser as pages of plain text. The browser shows them over the live terminal
and never writes them into it.

Trust rules. ``agent_session_id`` and ``transcript_path`` come from hook and
notify events, so they are checked for every page, and no file is ever
guessed from a folder, a name, or a modification time:

- The row is live and belongs to the bridge's env, tmux socket, and tmux session.
- The row kind is ``claude`` or ``codex``, with both identity fields set.
- Claude: the file is ``<home>/.claude/projects/<slug>/<agent_session_id>.jsonl``,
  its real path (symlinks resolved) has the same name under the real root, and
  the newest message row in the file carries ``sessionId == agent_session_id``.
- Codex: the row has a verified ``codex_runtime`` thread link (``binding_of``,
  ``is_verified``) whose env, thread id, transcript path, and stored pane
  fingerprint (socket, server pid, tmux session id, pane id, pane pid) are all
  present and equal the row and the bridge env now. The file is
  ``<home>/.codex/sessions/.../rollout-*-<agent_session_id>.jsonl``, and
  ``relay.codex_runtime.read_codex_page`` checks the file metadata id.
- Older pages must come from the same file (device and inode) as the first page.

Paging. A page ends at a byte offset ``before`` (``None`` for the end of the
file) and reads at most ``READ_BYTES`` backwards. Only lines that end with a
newline and start inside the window are parsed: the partial first line and an
unterminated last line are dropped. The next page ends at the start of the
oldest line used, so no line repeats and none is skipped. A line longer than
the window gets a note, and the cursor still moves back. ``more`` is false only
at the start of the file.

Bounds: per request ``READ_BYTES`` read, ``PAGE_ITEMS`` items, and about
``PAGE_BYTES`` of text; each item at most ``ITEM_BYTES``. Escape sequences and
control characters are removed. Notes for the user never contain a path or an
error text.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Callable

from . import transcript
from .config import EnvConfig

log = logging.getLogger("relay.terminal_history")

READ_BYTES = 4 * 1024 * 1024  # per page request, the same bound as transcript.MAX_READ_BYTES
FIRST_READ_BYTES = 256 * 1024  # most pages fit in this; the window grows to READ_BYTES when not
PAGE_ITEMS = 40
PAGE_BYTES = 64 * 1024
ITEM_BYTES = 8 * 1024
CODEX_PAGE_ITEMS = 15  # the Codex reader cannot stop at a byte size: 15 x ITEM_BYTES < 128 KiB
ROW_ITEMS = 20  # text and tool parts kept from one transcript row

NOTE_NO_SOURCE = "This screen keeps no older output, and this session has no saved agent transcript."
NOTE_NOT_LINKED = "Older output is not available yet. Relay has not linked a transcript to this session."
NOTE_UNVERIFIED = "Older output is not available. Relay could not verify the transcript of this session."
NOTE_CODEX_PENDING = "Older output for Codex sessions is not available in this version of Relay."
NOTE_CODEX_RELINK = "The Codex transcript link is stale. Relay needs fresh proof for this pane and thread before it can show saved messages."
NOTE_CHANGED = "The saved transcript changed. Go back to live, then swipe again."
NOTE_FAILED = "Older output could not be loaded. Try again."
NOTE_EMPTY = "No earlier messages are saved for this session yet."
NOTE_START = "Start of the saved conversation."
NOTE_GAP = "This part of the transcript has no messages to show. Older reads further back."
NOTE_TOO_LARGE = "One saved entry is too large to show here. Older continues before it."
SHORTENED = "\n[… message shortened]"
MORE_PARTS = "[… more parts of this message are not shown]"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,127}$")
# CSI, OSC (BEL or ST end), and two-byte ESC sequences; then the remaining C0/C1
# controls except tab and newline.
_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]?|\x9b[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class HistoryUnavailable(Exception):
    """No page can be shown. ``note`` is safe to show to the user."""

    def __init__(self, note: str):
        super().__init__(note)
        self.note = note


@dataclass(frozen=True)
class Source:
    """The identity fields of one row, read in the event loop."""

    kind: str
    agent_session_id: str
    transcript_path: str
    identity: tuple[Any, ...] = ()
    binding_identity: str = ""


def conversation_source_for(row: Any, env: EnvConfig) -> Source:
    """Share exact transcript and binding guards with the Conversation route."""
    if row is None or row["env"] != env.id:
        raise HistoryUnavailable(NOTE_UNVERIFIED)
    if row["kind"] not in ("claude", "codex"):
        raise HistoryUnavailable("Conversation is available for Claude and verified Codex sessions.")
    agent_id, path = row["agent_session_id"], row["transcript_path"]
    if not isinstance(agent_id, str) or not _ID_RE.fullmatch(agent_id) or not isinstance(path, str) or not path:
        raise HistoryUnavailable(NOTE_NOT_LINKED)
    binding_identity = ""
    if row["kind"] == "codex":
        _check_codex_binding(row, env.id, agent_id, path)
        from .codex_runtime import binding_of

        binding_identity = json.dumps(binding_of(row), sort_keys=True)
    identity = tuple(row[key] for key in (
        "env", "status", "socket_path", "server_pid", "tmux_session_id", "pane_id", "pane_pid",
    ))
    return Source(row["kind"], agent_id, path, identity, binding_identity)


@dataclass
class History:
    """One open history view: its source and the cursor for the next older page."""

    source: Source
    before: int | None = None  # None: the first page reads from the end of the file
    more: bool = True
    file_id: tuple[int, int] | None = None  # (st_dev, st_ino) of the first page
    verified: bool = False  # the Claude sessionId check passed


@dataclass
class Page:
    entries: list[dict[str, str]] = field(default_factory=list)  # oldest first
    before: int = 0
    more: bool = False
    note: str | None = None
    file_id: tuple[int, int] | None = None
    verified: bool = False

    def frame(self) -> dict[str, Any]:
        return {"items": self.entries, "before": self.before, "more": self.more, "note": self.note}


def source_for(row: Any, env: EnvConfig, socket_path: str, tmux_session_id: str) -> Source:
    """Check that ``row`` is this terminal's live agent session with a linked transcript."""
    if (
        row is None
        or row["status"] != "live"
        or row["env"] != env.id
        or row["socket_path"] != socket_path
        or row["tmux_session_id"] != tmux_session_id
    ):
        raise HistoryUnavailable(NOTE_UNVERIFIED)
    if row["kind"] not in ("claude", "codex"):
        raise HistoryUnavailable(NOTE_NO_SOURCE)
    return conversation_source_for(row, env)


_PANE_FINGERPRINT = (
    ("socket", "socket_path"),
    ("server_pid", "server_pid"),
    ("tmux_session_id", "tmux_session_id"),
    ("pane_id", "pane_id"),
    ("pane_pid", "pane_pid"),
)


def _check_codex_binding(row: Any, env_id: str, agent_id: str, path: str) -> None:
    """A Codex row needs a verified thread link for this exact env, thread, file, and pane.

    The Codex scanner also marks links stale, but this check does not wait for
    it: a link whose env or pane fingerprint no longer matches the row is
    refused now. A missing env, pane, or fingerprint field is refused too.
    """
    try:
        runtime = importlib.import_module("relay.codex_runtime")
        binding_of, is_verified = runtime.binding_of, runtime.is_verified
    except (ImportError, AttributeError):
        raise HistoryUnavailable(NOTE_CODEX_PENDING) from None
    try:
        binding = binding_of(row)
        verified = is_verified(binding)
    except Exception:  # noqa: BLE001 - an unreadable link is not a verified link
        raise HistoryUnavailable(NOTE_UNVERIFIED) from None
    if binding is None:
        raise HistoryUnavailable(NOTE_NOT_LINKED)
    if binding.get("status") == "stale":
        raise HistoryUnavailable(NOTE_CODEX_RELINK)
    if not verified or binding.get("thread_id") != agent_id or binding.get("transcript_path") != path:
        raise HistoryUnavailable(NOTE_UNVERIFIED)
    if not env_id or binding.get("env") != env_id or row["env"] != env_id:
        raise HistoryUnavailable(NOTE_UNVERIFIED)
    pane = binding.get("pane")
    if not isinstance(pane, dict):
        raise HistoryUnavailable(NOTE_UNVERIFIED)
    for key, column in _PANE_FINGERPRINT:
        # Fail closed: the field must be present in the link and in the row, and equal.
        expected = pane.get(key)
        if expected is None or row[column] is None or row[column] != expected:
            raise HistoryUnavailable(NOTE_UNVERIFIED)


# -- one page ------------------------------------------------------------------------

def read_page(env: EnvConfig, history: History) -> Page:
    """Read the page that ends at ``history.before``. Runs in a worker thread; changes nothing."""
    source = history.source
    agent_id = source.agent_session_id
    if not _ID_RE.match(agent_id):
        raise HistoryUnavailable(NOTE_UNVERIFIED)
    try:
        if source.kind == "claude":
            real = transcript._resolve_transcript(env, source.transcript_path, agent_id)
            page = _claude_page(real, history)
        else:
            real = _confined(
                env, source.transcript_path, (".codex", "sessions"),
                lambda n: n.startswith("rollout-") and n.endswith(f"-{agent_id}.jsonl"),
            )
            page = _codex_page(env, real, history)
    except transcript.TranscriptError as exc:
        log.warning("terminal history refused for %s session %s: %s", source.kind, agent_id, exc)
        raise HistoryUnavailable(NOTE_UNVERIFIED) from None
    except OSError as exc:
        log.warning("terminal history read failed for %s session %s: %s", source.kind, agent_id, exc)
        raise HistoryUnavailable(NOTE_FAILED) from None
    if not page.more and page.note is None:
        page.note = NOTE_START
    elif page.more and not page.entries and page.note is None:
        page.note = NOTE_GAP
    return page


def _confined(env: EnvConfig, container_path: str, parts: tuple[str, ...], name_ok: Callable[[str], bool]) -> str:
    """The real host path, when the path and its real target stay under ``<home>/<parts>``."""
    path = PurePosixPath(container_path)
    host = env.host_path(container_path)
    if host is None or not path.is_relative_to(PurePosixPath(env.home, *parts)) or not name_ok(path.name):
        raise HistoryUnavailable(NOTE_UNVERIFIED)
    real = os.path.realpath(host)
    real_root = os.path.realpath(env.host_home.joinpath(*parts))
    if not real.startswith(real_root + os.sep) or os.path.basename(real) != path.name:
        raise HistoryUnavailable(NOTE_UNVERIFIED)
    if not os.path.isfile(real):
        raise HistoryUnavailable(NOTE_EMPTY)  # the agent has not written its transcript yet
    return real


def _file_id(st: os.stat_result) -> tuple[int, int]:
    return (st.st_dev, st.st_ino)


def _check_same_file(history: History, file_id: tuple[int, int], size: int) -> None:
    if history.file_id is not None and history.file_id != file_id:
        raise HistoryUnavailable(NOTE_CHANGED)
    if history.before is not None and history.before > size:
        raise HistoryUnavailable(NOTE_CHANGED)


def _claude_page(real: str, history: History) -> Page:
    before = os.lstat(real)
    fd = os.open(real, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or _file_id(st) != _file_id(before):
            raise HistoryUnavailable(NOTE_CHANGED)
        _check_same_file(history, _file_id(st), st.st_size)
        end = st.st_size if history.before is None else history.before
        start = max(0, end - FIRST_READ_BYTES)
        fh.seek(start)
        data = fh.read(end - start)
        page = _scan(data, start, history)
        if page is None:
            # Not enough lines in the small window: grow it once to the full bound.
            wide = max(0, end - READ_BYTES)
            fh.seek(wide)
            data = fh.read(start - wide) + data
            page = _scan(data, wide, history, final=True)
        after = os.stat(real, follow_symlinks=False)
        if _file_id(after) != _file_id(st) or after.st_size < st.st_size:
            raise HistoryUnavailable(NOTE_CHANGED)
    assert page is not None  # a final scan always returns a page
    page.file_id = _file_id(st)
    return page


def _scan(data: bytes, start: int, history: History, *, final: bool = False) -> Page | None:
    """Take complete lines backwards from the end of ``data`` (file bytes from ``start``).

    Returns ``None`` when the window ran out before the page filled and a wider
    window could add lines (only when not ``final`` and the window does not
    start the file).
    """
    last_nl = data.rfind(b"\n")
    first_nl = data.find(b"\n")
    if start > 0 and (last_nl == -1 or first_nl == last_nl):
        # No line both starts and ends inside the window.
        if not final:
            return None
        if last_nl != -1 and last_nl < len(data) - 1:
            # The bytes after the newline belong to a line that the last page
            # skipped. The line that ends at the newline is read next, whole.
            return Page(before=start + last_nl + 1, more=True, verified=history.verified)
        # One line is longer than the whole window: move back past this window.
        return Page(before=start, more=True, note=NOTE_TOO_LARGE, verified=history.verified)
    if last_nl == -1:
        return Page(before=0, more=False, verified=history.verified)  # one unterminated line, or empty
    region = 0 if start == 0 else first_nl + 1  # the first segment is partial unless the window starts the file
    lines = data[region:last_nl].split(b"\n")
    verified = history.verified
    rows: list[list[dict[str, str]]] = []
    count = size = 0
    off = start + last_nl + 1  # start offset of the newest line taken so far, moving back
    cursor = off
    full = False
    for raw in reversed(lines):
        line_start = off - len(raw) - 1
        entries, session_id = _claude_line(raw)
        if entries:
            # Every visible message must belong to this session, on every page.
            if session_id != history.source.agent_session_id:
                raise HistoryUnavailable(NOTE_UNVERIFIED)
            verified = True
        row_bytes = sum(len(e["text"].encode("utf-8")) for e in entries)
        if rows and entries and (count + len(entries) > PAGE_ITEMS or size + row_bytes > PAGE_BYTES):
            full = True
            break
        if entries:
            rows.append(entries)
            count += len(entries)
            size += row_bytes
        off = line_start
        cursor = line_start
    if not full and start > 0 and not final:
        return None
    entries = [e for row in reversed(rows) for e in row]
    return Page(entries=entries, before=cursor, more=cursor > 0, verified=verified)


def _claude_line(raw: bytes) -> tuple[list[dict[str, str]], str | None]:
    """Plain-text entries of one Claude JSONL row (as ``transcript.read_conversation``) and its sessionId."""
    try:
        row = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return [], None
    if not isinstance(row, dict) or row.get("type") not in ("user", "assistant"):
        return [], None
    if row.get("isMeta") or row.get("isSidechain"):
        return [], None
    message = row.get("message")
    if not isinstance(message, dict):
        return [], None
    role = row["type"]
    session_id = row.get("sessionId") if isinstance(row.get("sessionId"), str) else None
    items: list[dict[str, Any]] = []
    content = message.get("content")
    text = transcript._string_content(content)
    if text is not None:
        if not (role == "user" and transcript._is_wrapper_text(text)):
            items.append({"role": role, "text": text})
    else:
        for block in transcript._blocks(content):
            btype = block.get("type")
            if btype == "text" and isinstance(block.get("text"), str):
                if not (role == "user" and transcript._is_wrapper_text(block["text"])):
                    items.append({"role": role, "text": block["text"]})
            elif btype == "tool_use" and isinstance(block.get("name"), str) and block["name"]:
                summary = transcript._tool_summary(block["name"], block.get("input"))
                items.append({"role": "tool", "text": "", "tool": {"name": block["name"], "summary": summary}})
            # tool_result and thinking blocks are skipped, as in the Conversation view.
    entries = [e for e in (_entry(item) for item in items) if e is not None]
    if len(entries) > ROW_ITEMS:
        entries = entries[:ROW_ITEMS] + [{"role": entries[ROW_ITEMS]["role"], "text": MORE_PARTS}]
    return entries, session_id


def _codex_page(env: EnvConfig, real: str, history: History) -> Page:
    try:
        reader = importlib.import_module("relay.codex_runtime").read_codex_page
    except (ImportError, AttributeError):
        raise HistoryUnavailable(NOTE_CODEX_PENDING) from None
    st = os.stat(real)
    _check_same_file(history, _file_id(st), st.st_size)
    file_id = {"dev": st.st_dev, "ino": st.st_ino}
    if history.source.binding_identity:
        from .codex_runtime import BindingError, verify_thread

        binding = json.loads(history.source.binding_identity)
        try:
            proof = verify_thread(env, history.source.agent_session_id, expected_path=history.source.transcript_path)
        except BindingError:
            raise HistoryUnavailable(NOTE_UNVERIFIED) from None
        if proof["rollout"] != binding.get("rollout") or proof["rollout"] != file_id:
            raise HistoryUnavailable(NOTE_CHANGED)
    result = reader(
        env, history.source.transcript_path,
        before=history.before, limit=CODEX_PAGE_ITEMS, expected_session_id=history.source.agent_session_id,
        expected_file_id=file_id,
    )
    items = result.get("items") if isinstance(result, dict) else None
    before = result.get("before") if isinstance(result, dict) else None
    more = bool(result.get("more")) if isinstance(result, dict) else False
    if not isinstance(items, list) or not isinstance(before, int) or isinstance(before, bool) or before < 0:
        raise HistoryUnavailable(NOTE_FAILED)
    previous = st.st_size if history.before is None else history.before
    if more and before >= previous:
        raise HistoryUnavailable(NOTE_FAILED)  # a cursor that does not move back would repeat the page
    if _file_id(os.stat(real)) != _file_id(st):
        raise HistoryUnavailable(NOTE_CHANGED)
    entries = [e for e in (_entry(item) for item in items) if e is not None]
    return Page(entries=entries, before=before, more=more and before > 0, file_id=_file_id(st), verified=True)


# -- text --------------------------------------------------------------------------

def plain_text(text: str) -> str:
    """Remove terminal escape sequences and control characters (tab and newline stay)."""
    return _CONTROL_RE.sub("", _ESCAPE_RE.sub("", text))


def _cap(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", "ignore") + SHORTENED


def _entry(item: Any) -> dict[str, str] | None:
    """One ConvItem as ``{role, text}`` plain text, or ``None`` when it has no text."""
    if not isinstance(item, dict):
        return None
    role = item.get("role")
    if role == "tool":
        tool = item.get("tool") if isinstance(item.get("tool"), dict) else {}
        name, summary = str(tool.get("name") or "tool"), str(tool.get("summary") or "")
        text = f"{name}: {summary}" if summary and summary != name else name
    else:
        text = item.get("text") if isinstance(item.get("text"), str) else ""
    text = plain_text(text).strip()
    if not text:
        return None
    return {"role": role if role in ("user", "assistant", "tool") else "assistant", "text": _cap(text, ITEM_BYTES)}
