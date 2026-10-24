"""Interactive terminal bridge from the browser to a tmux session.

A browser opens ``/api/sessions/{id}/terminal``. relay allocates a PTY,
attaches a ``podman exec`` tmux client to it, and forwards bytes both ways
over the WebSocket.

Two verified facts (ARCHITECTURE.md section 2) shape this bridge:

- The PTY slave must be the controlling TTY of the ``podman exec`` process,
  or window resizes never reach tmux. We set that with ``start_new_session``
  plus a ``preexec_fn`` that runs ``ioctl(0, TIOCSCTTY, 0)`` on the slave.
- When the local ``podman exec`` client dies, the tmux client inside the
  container keeps running. So the attach command first prints its own PID in
  an OSC marker, relay records that PID, and on close relay sends it SIGHUP
  through a fresh ``podman exec`` (only after confirming the PID still runs
  ``attach-session``).

A swipe up scrolls tmux history with copy mode. When copy mode has nothing
older to show (an agent TUI on the alternate screen), the bridge leaves copy
mode and sends bounded pages of the session's transcript instead
(``terminal_history``). The browser shows them as plain text over the live
terminal; they never go into the PTY.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import re
import signal
import struct
import subprocess
import termios
from typing import Any

from fastapi import APIRouter, Query, WebSocket

from . import terminal_history
from .config import EnvConfig
from .db import Database, now
from .errors import ApiError
from .runtime import Runtime, get_rt_ws
from .tmux import scroll_args

router = APIRouter()
log = logging.getLogger("relay.terminal")

WS_UNAUTHORIZED = 4401
WS_BAD_ORIGIN = 4403
WS_UNKNOWN_SESSION = 4404
WS_NOT_LIVE = 4409
WS_ERROR = 1011

MAX_INPUT_BYTES = 64 * 1024
READ_CHUNK = 64 * 1024
COALESCE_LIMIT = 64 * 1024
COLS_MIN, COLS_MAX = 20, 500
ROWS_MIN, ROWS_MAX = 5, 200
SCROLL_LIMIT = 500  # lines per queued scroll request
HISTORY_TIMEOUT = 8.0  # seconds for one bounded transcript read

# OSC 7770 marker: ESC ] 7770 ; relay-client ; <pid> BEL   (BEL or ESC \ terminator)
_MARKER_RE = re.compile(rb"\x1b\]7770;relay-client;(\d+)(?:\x07|\x1b\\)")

# Wrapper that prints the container tmux client PID, then execs the attach.
# The prompt text and ids never enter this script body; they are argv.
_ATTACH_WRAPPER = (
    'printf "\\033]7770;relay-client;%s\\007" "$$"; '
    'exec tmux -u -S "$1" attach-session -t "$2"'
)


def parse_marker(buffer: bytes) -> tuple[int | None, bytes, bytes]:
    """Scan ``buffer`` for the OSC 7770 marker.

    Returns ``(pid, cleaned, remainder)``:

    - ``pid`` is the parsed container PID, or ``None`` when no complete marker
      is present yet.
    - ``cleaned`` is the bytes to forward now, with the marker removed.
    - ``remainder`` is a trailing partial escape sequence to hold back until
      more bytes arrive (only when no marker was found yet).

    The marker can split across reads, so a caller keeps a small buffer and
    feeds accumulated bytes until ``pid`` is found.
    """
    match = _MARKER_RE.search(buffer)
    if match is not None:
        pid = int(match.group(1))
        cleaned = buffer[: match.start()] + buffer[match.end() :]
        return pid, cleaned, b""
    # No complete marker. Hold back a trailing partial ESC sequence so we do
    # not forward the first half of the marker.
    idx = buffer.rfind(b"\x1b")
    if idx != -1 and (len(buffer) - idx) < 64:
        return None, buffer[:idx], buffer[idx:]
    return None, buffer, b""


def _valid_size(cols: Any, rows: Any) -> tuple[int, int] | None:
    try:
        c = int(cols)
        r = int(rows)
    except (TypeError, ValueError):
        return None
    if COLS_MIN <= c <= COLS_MAX and ROWS_MIN <= r <= ROWS_MAX:
        return c, r
    return None


def _set_winsize(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _epoch(payload: dict) -> int:
    """The browser's history epoch from a request (0 when absent or invalid)."""
    value = payload.get("epoch")
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**53:
        return value
    return 0


class TerminalBridge:
    """One PTY plus one ``podman exec`` tmux client, wired to one WebSocket."""

    def __init__(self, rt: Runtime, env: EnvConfig, socket_path: str, tmux_session_id: str, session_id: str):
        self.rt = rt
        self.env = env
        self.socket_path = socket_path
        self.tmux_session_id = tmux_session_id
        self.session_id = session_id
        self.client_row_id = os.urandom(16).hex()

        self.master_fd: int = -1
        self.slave_fd: int = -1
        self.proc: subprocess.Popen | None = None
        self.container_pid: int | None = None
        self._marker_done = False
        self._marker_buf = b""
        self._closed = False
        self._ws: WebSocket | None = None
        self._reader_registered = False
        # Two tasks write to the socket (output pump, input handler replies),
        # so every send takes this lock.
        self._send_lock = asyncio.Lock()
        # History scroll (tmux copy mode). Requests coalesce while one runs.
        self._scroll_lock = asyncio.Lock()
        self._scroll_pending = 0
        self._scroll_bottom = False
        self._scroll_task: asyncio.Task | None = None
        self._in_scroll = False  # copy mode, as the last tmux call reported it
        # Set once this viewer scrolled. A scroll call that is cancelled at
        # close can already have entered copy mode in the container, so close
        # then always sends a cancel (a no-op when the pane is live).
        self._scroll_used = False
        # What the viewer sees: 'live', 'tmux' (copy mode shows older tmux
        # history), or 'history' (transcript pages over the live terminal).
        self._view = "live"
        # The browser numbers each history view (epoch). A history frame
        # carries the epoch of the swipe that opened it. The token changes
        # when a view ends, so a read that finishes later is dropped.
        self._epoch = 0
        self._history_token = 0
        self._history_epoch: int | None = None
        self._history: terminal_history.History | None = None
        self._history_task: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------
    def spawn(self, cols: int, rows: int) -> None:
        """Open a PTY and start the attach client on it."""
        self.master_fd, self.slave_fd = os.openpty()
        _set_winsize(self.slave_fd, cols, rows)

        def _preexec() -> None:  # pragma: no cover - runs in the child
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        argv = self.rt.podman.argv(
            self.rt.podman.exec_args(
                self.env,
                ["sh", "-c", _ATTACH_WRAPPER, "relay-attach", self.socket_path, self.tmux_session_id],
                interactive=True,
                tty=True,
                extra_env={"TERM": "xterm-256color"},
            )
        )
        self.proc = subprocess.Popen(
            argv,
            stdin=self.slave_fd,
            stdout=self.slave_fd,
            stderr=self.slave_fd,
            start_new_session=True,
            preexec_fn=_preexec,
            close_fds=True,
            env=self.rt.podman.env,
        )
        os.close(self.slave_fd)
        self.slave_fd = -1
        # The container PID is learned from the marker; record the row now.
        self.rt.db.insert(
            "terminal_clients",
            {
                "id": self.client_row_id,
                "env": self.env.id,
                "container_pid": None,
                "socket_path": self.socket_path,
                "tmux_session_id": self.tmux_session_id,
                "session_id": self.session_id,
                "started_at": now(),
            },
        )

    async def run(self, websocket: WebSocket) -> None:
        """Forward bytes until the socket or the process closes."""
        self._ws = websocket
        loop = asyncio.get_running_loop()
        data_ready: asyncio.Queue[bytes | None] = asyncio.Queue()

        def _on_readable() -> None:
            try:
                chunk = os.read(self.master_fd, READ_CHUNK)
            except OSError:
                chunk = b""
            data_ready.put_nowait(chunk or None)

        loop.add_reader(self.master_fd, _on_readable)
        self._reader_registered = True
        await self._send_json({"type": "ready"})

        sender = asyncio.create_task(self._pump_output(websocket, data_ready))
        try:
            await self._pump_input(websocket)
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            if self._scroll_task is not None and not self._scroll_task.done():
                # Let the running tmux call finish (about 0.1 s), and queue no
                # more. A call cancelled mid-flight can still land in the
                # container after the close-time cancel and leave copy mode on.
                self._scroll_pending = 0
                self._scroll_bottom = False
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(self._scroll_task, timeout=3.0)
            # After the scroll task: its last call can have opened a history view.
            self._end_history()
            if self._history_task is not None and not self._history_task.done():
                self._history_task.cancel()  # a file read only; nothing to undo

    async def _send_json(self, obj: dict) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send_json(obj)

    async def _send_bytes(self, data: bytes) -> None:
        if self._ws is None:
            return
        async with self._send_lock:
            await self._ws.send_bytes(data)

    async def _pump_output(self, websocket: WebSocket, data_ready: "asyncio.Queue[bytes | None]") -> None:
        while True:
            chunk = await data_ready.get()
            if chunk is None:
                # PTY closed: the attach client exited.
                with contextlib.suppress(Exception):
                    await self._send_exit(websocket, "detached")
                await websocket.close()
                return
            out = self._process_output(chunk)
            # Coalesce whatever else is immediately available, up to the limit.
            while len(out) < COALESCE_LIMIT and not data_ready.empty():
                extra = data_ready.get_nowait()
                if extra is None:
                    if out:
                        await self._send_bytes(out)
                    with contextlib.suppress(Exception):
                        await self._send_exit(websocket, "detached")
                    await websocket.close()
                    return
                out += self._process_output(extra)
            if out:
                await self._send_bytes(out)

    def _process_output(self, chunk: bytes) -> bytes:
        """Strip the OSC marker from the first bytes, then forward the rest."""
        if self._marker_done:
            return chunk
        self._marker_buf += chunk
        pid, cleaned, remainder = parse_marker(self._marker_buf)
        if pid is not None:
            self.container_pid = pid
            self._marker_done = True
            self._marker_buf = b""
            self.rt.db.update("terminal_clients", self.client_row_id, {"container_pid": pid})
            return cleaned
        self._marker_buf = remainder
        return cleaned

    async def _pump_input(self, websocket: WebSocket) -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                await self._leave_scroll_for_input()
                self._write(data[:MAX_INPUT_BYTES])
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                payload = json.loads(text)
            except (TypeError, ValueError):
                continue
            await self._handle_json(payload)

    async def _handle_json(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        kind = payload.get("type")
        if kind == "input":
            data = payload.get("data")
            if isinstance(data, str):
                await self._leave_scroll_for_input()
                self._write(data.encode("utf-8", "replace")[:MAX_INPUT_BYTES])
        elif kind == "resize":
            size = _valid_size(payload.get("cols"), payload.get("rows"))
            if size and self.master_fd >= 0:
                with contextlib.suppress(OSError):
                    _set_winsize(self.master_fd, size[0], size[1])
        elif kind == "ping":
            # Application heartbeat: the browser cannot see protocol pings, so
            # it sends its own and treats a missing pong as a dead socket. A
            # wake probe carries a nonce; the echo lets the browser ignore a
            # reply to an older ping. A ping without one gets the plain pong.
            pong: dict[str, Any] = {"type": "pong"}
            nonce = payload.get("nonce")
            if isinstance(nonce, str) and 0 < len(nonce) <= 64:
                pong["nonce"] = nonce
            with contextlib.suppress(Exception):
                await self._send_json(pong)
        elif kind == "scroll":
            self._queue_scroll(payload)
        elif kind == "history":
            self._queue_older_page(payload)

    # -- history scroll (tmux copy mode, else the agent transcript) -----------
    #
    # swipe up -> tmux.scroll -> copy mode shows older tmux history -> 'tmux' view
    #                          -> nothing older (alternate screen, or no history)
    #                             -> copy mode off -> transcript pages -> 'history' view
    # Back to live or a key ends either view before anything else happens.

    def _queue_scroll(self, payload: dict) -> None:
        """Add a scroll request. ``lines`` > 0 is older output; ``to: 'bottom'`` goes live."""
        self._scroll_used = True
        if payload.get("to") == "bottom":
            self._end_history()
            self._scroll_pending = 0
            self._scroll_bottom = True
        else:
            try:
                lines = int(payload.get("lines", 0))
            except (TypeError, ValueError):
                return
            if lines == 0 or self._view == "history":
                return  # in a history view the browser scrolls the loaded text
            self._epoch = _epoch(payload)
            self._scroll_pending = max(-SCROLL_LIMIT, min(SCROLL_LIMIT, self._scroll_pending + lines))
        if self._scroll_task is None or self._scroll_task.done():
            self._scroll_task = asyncio.get_running_loop().create_task(self._run_scroll())

    async def _run_scroll(self) -> None:
        """Apply queued scroll requests one tmux call at a time, merging new ones."""
        tmux = self.rt.tmux_for(self.env.id)
        while not self._closed:
            if self._scroll_bottom:
                self._scroll_bottom = False
                lines = 0
            elif self._scroll_pending:
                lines, self._scroll_pending = self._scroll_pending, 0
            else:
                return
            async with self._scroll_lock:
                if lines == 0:
                    self._end_history()  # a view that the call before this one opened
                elif self._view == "history":
                    continue  # swipes merged after the switch to transcript pages
                try:
                    state = await tmux.scroll(self.socket_path, self.tmux_session_id, lines)
                except Exception:  # noqa: BLE001 - a failed scroll leaves the view as is
                    continue
                no_history = lines > 0 and self._view == "live" and not state.shows_history
                if no_history and state.in_mode:
                    # Copy mode shows nothing older here and freezes the frame: leave it at once.
                    with contextlib.suppress(Exception):
                        state = await tmux.scroll(self.socket_path, self.tmux_session_id, 0)
                self._in_scroll = state.in_mode
                self._view = "tmux" if state.in_mode and not no_history else "live"
                frame: dict[str, Any] = {"type": "scroll", "active": self._view == "tmux"}
                if self._view == "tmux":
                    frame.update(position=state.position, history=state.history)
                with contextlib.suppress(Exception):
                    await self._send_json(frame)
                if no_history:
                    self._scroll_pending = 0
                    self._start_history()

    async def _leave_scroll_for_input(self) -> None:
        """Leave copy mode and any history view before typed input, so keys reach the program.

        In copy mode tmux reads keys as copy-mode commands. The cancel runs
        before the write, so the order of keys is kept.
        """
        busy = self._scroll_task is not None and not self._scroll_task.done()
        if not (self._in_scroll or busy or self._scroll_pending or self._scroll_bottom):
            self._end_history()
            return
        self._scroll_pending = 0
        self._scroll_bottom = False
        async with self._scroll_lock:
            # Inside the lock: a scroll call that was running can have opened a view.
            self._end_history()
            with contextlib.suppress(Exception):
                await self.rt.tmux_for(self.env.id).scroll(self.socket_path, self.tmux_session_id, 0)
            self._in_scroll = False
            self._view = "live"
            with contextlib.suppress(Exception):
                await self._send_json({"type": "scroll", "active": False})

    # -- transcript history -------------------------------------------------------
    def _start_history(self) -> None:
        """Open a history view. Its first page is read off the event loop."""
        self._view = "history"
        self._history_token += 1
        self._history_epoch = self._epoch
        self._history = None
        self._history_task = asyncio.get_running_loop().create_task(
            self._load_page(self._history_token, self._epoch, None)
        )

    def _end_history(self) -> None:
        """End a history view. A read in flight finishes in its thread, and its result is dropped."""
        self._history_token += 1
        self._history = None
        self._history_epoch = None
        if self._view == "history":
            self._view = "live"

    def _queue_older_page(self, payload: dict) -> None:
        """Answer the Older button: one read at a time, for the open view and its current cursor."""
        history = self._history
        if history is None or self._view != "history" or _epoch(payload) != self._history_epoch:
            return
        if not history.more or payload.get("before") != history.before:
            return  # a repeated or stale request
        if self._history_task is not None and not self._history_task.done():
            return
        self._history_task = asyncio.get_running_loop().create_task(
            self._load_page(self._history_token, self._history_epoch, history)
        )

    def _row_source(self) -> terminal_history.Source:
        """The row identity now: hooks can link or change a transcript at any time."""
        row = self.rt.db.get_session(self.session_id)
        return terminal_history.source_for(row, self.env, self.socket_path, self.tmux_session_id)

    async def _load_page(self, token: int, epoch: int, history: terminal_history.History | None) -> None:
        """Read one page (the first one when ``history`` is None) and send it if the view is still open."""
        first = history is None
        if first:
            await self._send_history(token, {"type": "history", "epoch": epoch, "status": "loading"})
            if token != self._history_token:
                return  # a key or Back to live came first
        page: terminal_history.Page | None = None
        note = terminal_history.NOTE_FAILED
        try:
            source = self._row_source()
            if history is None:
                history = terminal_history.History(source)
            elif history.source != source:
                raise terminal_history.HistoryUnavailable(terminal_history.NOTE_CHANGED)
            page = await asyncio.wait_for(
                asyncio.to_thread(terminal_history.read_page, self.env, history), timeout=HISTORY_TIMEOUT
            )
            if self._row_source() != source:  # checked again after the read
                raise terminal_history.HistoryUnavailable(terminal_history.NOTE_CHANGED)
        except terminal_history.HistoryUnavailable as exc:
            page, note = None, exc.note
        except Exception as exc:  # noqa: BLE001 - timeout or reader error; the user sees NOTE_FAILED
            page = None
            log.warning("terminal history page failed for session %s: %s", self.session_id, type(exc).__name__)
        if token != self._history_token or self._closed:
            return  # the view ended while the page was read
        if first:
            if page is None or (not page.entries and not page.more):
                # Nothing to show: stay live, and let a later swipe try again.
                self._view = "live"
                note = note if page is None else terminal_history.NOTE_EMPTY
                await self._send_history(token, {"type": "history", "epoch": epoch, "status": "none", "note": note})
                return
            self._history = history
        elif page is None:
            # A failed read keeps the cursor, so Older can try again; a changed source ends paging.
            if note != terminal_history.NOTE_FAILED:
                history.more = False
            frame = {"items": [], "before": history.before, "more": history.more, "note": note}
            await self._send_history(token, {"type": "history", "epoch": epoch, "status": "page", "first": False, **frame})
            return
        history.before, history.more = page.before, page.more
        history.file_id, history.verified = page.file_id, page.verified
        await self._send_history(
            token, {"type": "history", "epoch": epoch, "status": "page", "first": first, **page.frame()}
        )

    async def _send_history(self, token: int, frame: dict) -> None:
        """Send a history frame only while its view is still open (checked under the send lock)."""
        if self._ws is None:
            return
        with contextlib.suppress(Exception):
            async with self._send_lock:
                if token == self._history_token and not self._closed:
                    await self._ws.send_json(frame)

    def _write(self, data: bytes) -> None:
        if self.master_fd >= 0 and data:
            with contextlib.suppress(OSError):
                os.write(self.master_fd, data)

    async def _send_exit(self, websocket: WebSocket, reason: str, message: str = "") -> None:
        frame = {"type": "exit", "reason": reason, "message": message}
        if self._ws is websocket:
            await self._send_json(frame)
        else:
            await websocket.send_json(frame)

    # -- teardown ------------------------------------------------------------
    async def close(self) -> None:
        """Tear down the bridge.

        This runs no ``await`` and never yields, because the WS task is often
        cancelled during teardown; a suspension point would abort the cleanup
        before the surviving container client is hung up.
        """
        if self._closed:
            return
        self._closed = True
        if self._reader_registered and self.master_fd >= 0:
            with contextlib.suppress(Exception):
                asyncio.get_running_loop().remove_reader(self.master_fd)
        pid = self.container_pid

        # A viewer that closes while scrolled up must not leave the pane in
        # copy mode: the next viewer (or the laptop) would see old output and
        # its keys would go to copy mode instead of the program.
        if self._scroll_used:
            with contextlib.suppress(Exception):
                _leave_scroll_sync(self.rt, self.env, self.socket_path, self.tmux_session_id)

        # Hang up the surviving container client first, while the local podman
        # process is still alive and the client still runs attach-session (the
        # cmdline check passes). Blocking call, one-shot (~150 ms).
        if pid is not None:
            with contextlib.suppress(Exception):
                _hup_container_client_sync(self.rt, self.env, pid)

        # Terminate the local podman process.
        if self.proc is not None and self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.proc.terminate()
            with contextlib.suppress(Exception):
                self.proc.wait(timeout=3)
            if self.proc.poll() is None:
                with contextlib.suppress(Exception):
                    self.proc.kill()
        if self.master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.master_fd)
            self.master_fd = -1
        with contextlib.suppress(Exception):
            self.rt.db.execute("DELETE FROM terminal_clients WHERE id = ?", (self.client_row_id,))


# -- registry so app shutdown can close every bridge -------------------------
_BRIDGES: "set[TerminalBridge]" = set()


def _register(bridge: TerminalBridge) -> None:
    _BRIDGES.add(bridge)


def _unregister(bridge: TerminalBridge) -> None:
    _BRIDGES.discard(bridge)


async def close_all_bridges() -> None:
    for bridge in list(_BRIDGES):
        with contextlib.suppress(Exception):
            await bridge.close()
        _unregister(bridge)


async def _hup_container_client(rt: Runtime, env: EnvConfig, pid: int) -> None:
    """Send SIGHUP to a container PID, but only when it still runs ``attach-session``."""
    if pid <= 0:
        return
    check = await rt.podman.exec(env, ["cat", f"/proc/{pid}/cmdline"], timeout=10)
    if not check.ok or b"attach-session" not in check.stdout:
        return
    await rt.podman.exec(env, ["kill", "-HUP", str(pid)], timeout=10)


def _hup_container_client_sync(rt: Runtime, env: EnvConfig, pid: int) -> None:
    """Blocking version of the SIGHUP path, safe to run in a worker thread.

    Used on disconnect, where the event loop task may be cancelled and would
    abort an awaited coroutine before the SIGHUP is sent.
    """
    if pid <= 0:
        return
    check = subprocess.run(
        rt.podman.argv(rt.podman.exec_args(env, ["cat", f"/proc/{pid}/cmdline"])),
        capture_output=True,
        env=rt.podman.env,
        timeout=10,
    )
    if check.returncode != 0 or b"attach-session" not in check.stdout:
        return
    subprocess.run(
        rt.podman.argv(rt.podman.exec_args(env, ["kill", "-HUP", str(pid)])),
        capture_output=True,
        env=rt.podman.env,
        timeout=10,
    )


def _leave_scroll_sync(rt: Runtime, env: EnvConfig, socket_path: str, tmux_session_id: str) -> None:
    """Blocking copy-mode cancel for bridge teardown (one ``podman exec``, ~0.1 s)."""
    subprocess.run(
        rt.podman.argv(rt.podman.exec_args(env, ["tmux", "-S", socket_path, *scroll_args(tmux_session_id, 0)])),
        capture_output=True,
        env=rt.podman.env,
        timeout=10,
    )


async def cleanup_leftover_clients(rt: Runtime) -> None:
    """At startup, hang up every recorded container client we still own, then clear rows."""
    rows = rt.db.query("SELECT * FROM terminal_clients")
    for row in rows:
        env = rt.settings.envs.get(row["env"])
        pid = row["container_pid"]
        if env is not None and pid:
            with contextlib.suppress(Exception):
                await _hup_container_client(rt, env, int(pid))
    with contextlib.suppress(Exception):
        rt.db.execute("DELETE FROM terminal_clients")


def _resolve_live_session(db: Database, session_id: str) -> Any:
    """Return the session row, raising the right WS close code as ApiError.status."""
    row = db.get_session(session_id)
    if row is None:
        raise ApiError(WS_UNKNOWN_SESSION, "not_found", "Unknown session.")
    if row["status"] != "live":
        raise ApiError(WS_NOT_LIVE, "not_live", "Session is not live.")
    return row


@router.websocket("/api/sessions/{session_id}/terminal")
async def terminal(
    websocket: WebSocket,
    session_id: str,
    cols: int = Query(default=120),
    rows: int = Query(default=40),
) -> None:
    rt: Runtime = get_rt_ws(websocket)

    # Authenticate and check Origin before accept(): an unauthenticated client
    # never completes the WebSocket handshake.
    try:
        principal = await rt.auth.authenticate_websocket(websocket)
    except ApiError:
        await websocket.close(code=WS_BAD_ORIGIN)
        return
    if principal is None:
        await websocket.close(code=WS_UNAUTHORIZED)
        return

    # The caller is authenticated. Accept first, so the browser receives the
    # 4404/4409 close codes below (a close before accept arrives as 1006, and
    # the client would keep reconnecting to an ended session).
    await websocket.accept()

    # Validate the session identity.
    try:
        row = _resolve_live_session(rt.db, session_id)
    except ApiError as exc:
        await websocket.close(code=exc.status)
        return

    env = rt.settings.envs.get(row["env"])
    tmux_session_id = row["tmux_session_id"]
    socket_path = row["socket_path"]
    if env is None or not tmux_session_id:
        await websocket.close(code=WS_NOT_LIVE)
        return

    tmux = rt.tmux.get(env.id)
    try:
        exists = tmux is not None and await tmux.has_session(socket_path, tmux_session_id)
    except Exception:
        exists = False
    if not exists:
        await websocket.close(code=WS_NOT_LIVE)
        return

    size = _valid_size(cols, rows) or (120, 40)
    bridge = TerminalBridge(rt, env, socket_path, tmux_session_id, session_id)
    _register(bridge)
    try:
        bridge.spawn(size[0], size[1])
        await bridge.run(websocket)
    except Exception:
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "exit", "reason": "error", "message": "terminal error"})
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_ERROR)
    finally:
        await bridge.close()
        _unregister(bridge)
