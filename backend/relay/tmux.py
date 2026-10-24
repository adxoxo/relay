"""tmux operations inside a work container, executed through ``podman exec``.

Targets are always tmux IDs (``$3`` for a session, ``%5`` for a pane) on an
explicit socket. Display names are never used to route an operation, because
Work 1 and Work 2 both have sessions called ``agent1`` and a name can be
reused after a session ends.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .config import EnvConfig
from .podman import CommandError, Podman

DELIM = "|~|"
SHELLS = frozenset({"bash", "-bash", "sh", "-sh", "zsh", "-zsh", "fish", "dash", "ash", "ksh", "login"})
AGENT_BINARIES = {"claude": "claude", "codex": "codex"}

SESSION_FIELDS = [
    "#{pid}",
    "#{session_id}",
    "#{session_created}",
    "#{session_windows}",
    "#{session_attached}",
    "#{session_activity}",
    "#{@relay_id}",
    "#{session_name}",  # last: a name may contain the delimiter
]
PANE_FIELDS = [
    "#{session_id}",
    "#{window_id}",
    "#{window_index}",
    "#{window_active}",
    "#{pane_id}",
    "#{pane_index}",
    "#{pane_active}",
    "#{pane_pid}",
    "#{pane_dead}",
    "#{pane_width}",
    "#{pane_height}",
    "#{window_activity}",
    "#{pane_current_command}",
    "#{pane_current_path}",  # last: a path may contain the delimiter
]
SESSION_FORMAT = DELIM.join(SESSION_FIELDS)
PANE_FORMAT = DELIM.join(PANE_FIELDS)

# One ``podman exec`` per poll: every socket's sessions and panes, plus the
# process table (used to tell Claude and Codex panes from plain shells).
SNAPSHOT_SCRIPT = r"""
sf=$1; pf=$2; shift 2
for s in "$@"; do
  printf '@@SOCKET\t%s\n' "$s"
  if [ -S "$s" ]; then
    if out=$(tmux -S "$s" list-sessions -F "$sf" 2>/dev/null); then
      printf '@@SESSIONS\n%s\n@@PANES\n' "$out"
      tmux -S "$s" list-panes -a -F "$pf" 2>/dev/null
    else
      printf '@@NOSERVER\n'
    fi
  else
    printf '@@NOSOCKET\n'
  fi
done
printf '@@PS\n'
if out=$(ps -eo pid=,ppid=,args= 2>/dev/null); then
  printf '%s\n' "$out" | cut -c1-300
  printf '@@PS_OK\n'
else
  printf '@@PS_FAILED\n'
fi
printf '@@END\n'
"""

PASTE_SCRIPT = r"""
b=$1; s=$2; p=$3; submit=$4
tmux -S "$s" load-buffer -b "$b" - || exit 3
tmux -S "$s" paste-buffer -d -p -b "$b" -t "$p" || { tmux -S "$s" delete-buffer -b "$b" 2>/dev/null; exit 4; }
if [ "$submit" = 1 ]; then
  sleep 0.3
  tmux -S "$s" send-keys -t "$p" Enter || exit 5
fi
"""

SESSION_ID_RE = re.compile(r"^\$\d+$")
PANE_ID_RE = re.compile(r"^%\d+$")

SCROLL_MAX_LINES = 500
# Printed after every scroll call: copy mode, copy-mode offset, history lines,
# and whether the program uses the alternate screen (agent TUIs do).
SCROLL_STATE_FORMAT = DELIM.join(["#{pane_in_mode}", "#{scroll_position}", "#{history_size}", "#{alternate_on}"])
# Copy mode is entered only when it can show older program output: the normal
# screen with history. The alternate screen keeps no tmux history, and copy
# mode there only freezes the current frame (findings 2026-10-01).
_SCROLLABLE = "#{?alternate_on,0,#{history_size}}"


def scroll_args(session_id: str, lines: int) -> list[str]:
    """tmux argv that scrolls a session's visible pane with copy mode, then prints its state.

    - ``lines`` > 0 enters copy mode with ``-e`` and scrolls up into older
      output, but only on the normal screen with history (``_SCROLLABLE``).
    - ``lines`` < 0 scrolls down. ``scroll-down-and-cancel`` leaves copy mode at
      the bottom, so the pane returns to live output by itself.
    - ``lines`` == 0 leaves copy mode at once.

    The target is the session id, so tmux resolves the pane that its clients
    show. No global option (``mouse``, ``history-limit``) changes. The inner
    ``if-shell`` command is parsed by tmux, so the id is single-quoted there
    (``$`` starts an environment variable in tmux command syntax). The last
    command prints ``SCROLL_STATE_FORMAT`` (see ``parse_scroll_state``).
    """
    _check_target(session_id, SESSION_ID_RE, "session")
    n = max(-SCROLL_MAX_LINES, min(SCROLL_MAX_LINES, int(lines)))
    quoted = f"'{session_id}'"
    if n > 0:
        head = ["if-shell", "-F", "-t", session_id, _SCROLLABLE,
                f"copy-mode -e -t {quoted} ; send-keys -t {quoted} -X -N {n} scroll-up", ";"]
    elif n < 0:
        head = ["if-shell", "-F", "-t", session_id, "#{pane_in_mode}",
                f"send-keys -t {quoted} -X -N {-n} scroll-down-and-cancel", ";"]
    else:
        head = ["if-shell", "-F", "-t", session_id, "#{pane_in_mode}",
                f"send-keys -t {quoted} -X cancel", ";"]
    return [*head, "display-message", "-p", "-t", session_id, SCROLL_STATE_FORMAT]


@dataclass(frozen=True)
class ScrollState:
    """Pane state after a scroll call."""

    in_mode: bool  # copy mode is on
    position: int  # lines above the live bottom (0 when live)
    history: int  # tmux history lines of the program's screen
    alternate: bool  # the program uses the alternate screen

    @property
    def shows_history(self) -> bool:
        """True only when copy mode really shows older program output."""
        return self.in_mode and not self.alternate and self.position > 0


def parse_scroll_state(out: str) -> ScrollState:
    lines = [line for line in out.strip().splitlines() if line.strip()]
    parts = lines[-1].split(DELIM) if lines else []
    if len(parts) != 4:
        raise TmuxError(f"unexpected scroll state: {out[-200:]!r}")
    mode, position, history, alternate = parts
    return ScrollState(mode.strip() == "1", _int(position), _int(history), alternate.strip() == "1")


class TmuxError(RuntimeError):
    pass


@dataclass
class TmuxPane:
    session_id: str
    window_id: str
    window_index: int
    window_active: bool
    pane_id: str
    pane_index: int
    pane_active: bool
    pid: int
    dead: bool
    width: int
    height: int
    activity: int
    current_command: str
    current_path: str

    @property
    def shell_foreground(self) -> bool:
        return self.current_command in SHELLS


@dataclass
class TmuxSession:
    socket: str
    server_pid: int
    id: str
    created: int
    windows: int
    attached: int
    activity: int
    relay_id: str | None
    name: str
    panes: list[TmuxPane] = field(default_factory=list)

    @property
    def target_pane(self) -> TmuxPane | None:
        """The active pane of the active window (tmux's own default target)."""
        for p in self.panes:
            if p.window_active and p.pane_active:
                return p
        return self.panes[0] if self.panes else None

    def pane(self, pane_id: str | None) -> TmuxPane | None:
        return next((p for p in self.panes if p.pane_id == pane_id), None)


@dataclass
class ProcInfo:
    pid: int
    ppid: int
    args: str


@dataclass
class SocketState:
    path: str
    status: str  # 'ok' | 'no_socket' | 'no_server'
    server_pid: int | None = None


@dataclass
class Snapshot:
    env_id: str
    sockets: list[SocketState] = field(default_factory=list)
    sessions: list[TmuxSession] = field(default_factory=list)
    procs: dict[int, ProcInfo] = field(default_factory=dict)
    procs_complete: bool = False

    def find(self, socket: str, session_id: str) -> TmuxSession | None:
        return next((s for s in self.sessions if s.socket == socket and s.id == session_id), None)

    def children(self, pid: int) -> list[ProcInfo]:
        return [p for p in self.procs.values() if p.ppid == pid]

    def descendants(self, pid: int, limit: int = 200) -> list[ProcInfo]:
        out: list[ProcInfo] = []
        stack = [pid]
        seen = {pid}
        while stack and len(out) < limit:
            for child in self.children(stack.pop()):
                if child.pid not in seen:
                    seen.add(child.pid)
                    out.append(child)
                    stack.append(child.pid)
        return out


def _int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_snapshot(env_id: str, text: str) -> Snapshot:
    snap = Snapshot(env_id=env_id)
    ps_valid = True
    socket: SocketState | None = None
    section = ""
    for line in text.splitlines():
        if line.startswith("@@SOCKET\t"):
            socket = SocketState(path=line.split("\t", 1)[1], status="ok")
            snap.sockets.append(socket)
            section = ""
            continue
        if line == "@@NOSOCKET" and socket:
            socket.status = "no_socket"
            continue
        if line == "@@NOSERVER" and socket:
            socket.status = "no_server"
            continue
        if line in ("@@SESSIONS", "@@PANES", "@@PS", "@@END"):
            section = line
            continue
        if line == "@@PS_OK":
            snap.procs_complete = ps_valid and bool(snap.procs)
            section = ""
            continue
        if line == "@@PS_FAILED":
            snap.procs_complete = False
            section = ""
            continue
        if not line.strip():
            continue
        if section == "@@SESSIONS" and socket:
            parts = line.split(DELIM, len(SESSION_FIELDS) - 1)
            if len(parts) != len(SESSION_FIELDS):
                continue
            pid, sid, created, windows, attached, activity, relay_id, name = parts
            socket.server_pid = _int(pid) or None
            snap.sessions.append(
                TmuxSession(
                    socket=socket.path,
                    server_pid=_int(pid),
                    id=sid,
                    created=_int(created),
                    windows=_int(windows),
                    attached=_int(attached),
                    activity=_int(activity),
                    relay_id=relay_id or None,
                    name=name,
                )
            )
        elif section == "@@PANES" and socket:
            parts = line.split(DELIM, len(PANE_FIELDS) - 1)
            if len(parts) != len(PANE_FIELDS):
                continue
            (sid, wid, widx, wact, pid_, pidx, pact, ppid, dead, w, h, act, cmd, path) = parts
            pane = TmuxPane(
                session_id=sid,
                window_id=wid,
                window_index=_int(widx),
                window_active=wact == "1",
                pane_id=pid_,
                pane_index=_int(pidx),
                pane_active=pact == "1",
                pid=_int(ppid),
                dead=dead == "1",
                width=_int(w),
                height=_int(h),
                activity=_int(act),
                current_command=cmd,
                current_path=path,
            )
            owner = next((s for s in snap.sessions if s.socket == socket.path and s.id == sid), None)
            if owner is not None:
                owner.panes.append(pane)
        elif section == "@@PS":
            parts = line.split(None, 2)
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                snap.procs[int(parts[0])] = ProcInfo(int(parts[0]), int(parts[1]), parts[2] if len(parts) > 2 else "")
            else:
                ps_valid = False
    return snap


def _agent_in_args(args: str) -> str | None:
    for token in args.split()[:3]:
        base = token.rsplit("/", 1)[-1]
        if base in AGENT_BINARIES:
            return AGENT_BINARIES[base]
    return None


def detect_kind(snap: Snapshot, pane: TmuxPane | None) -> str:
    """Classify a pane as 'claude', 'codex', 'shell', or 'other' from its process tree."""
    if pane is None:
        return "other"
    chain: list[ProcInfo] = []
    root = snap.procs.get(pane.pid)
    if root is not None:
        chain.append(root)
    chain.extend(snap.descendants(pane.pid))
    for proc in chain:
        kind = _agent_in_args(proc.args)
        if kind:
            return kind
    kind = AGENT_BINARIES.get(pane.current_command)
    if kind:
        return kind
    if pane.current_command in SHELLS:
        return "shell"
    return "other"


def _check_target(value: str, pattern: re.Pattern[str], what: str) -> str:
    if not pattern.match(value or ""):
        raise TmuxError(f"invalid tmux {what} id: {value!r}")
    return value


class Tmux:
    """tmux commands for one environment."""

    def __init__(self, podman: Podman, env: EnvConfig):
        self.podman = podman
        self.env = env

    async def _tmux(self, socket: str, *args: str, input: bytes | None = None, timeout: float = 15.0) -> str:
        res = await self.podman.exec(self.env, ["tmux", "-S", socket, *args], input=input, timeout=timeout)
        if not res.ok:
            raise CommandError(["tmux", *args], res.code, res.err)
        return res.out

    async def snapshot(self, sockets: Sequence[str] | None = None, *, timeout: float = 15.0) -> Snapshot:
        socks = list(sockets if sockets is not None else self.env.tmux_sockets)
        res = await self.podman.exec(
            self.env,
            ["sh", "-c", SNAPSHOT_SCRIPT, "relay-snapshot", SESSION_FORMAT, PANE_FORMAT, *socks],
            timeout=timeout,
        )
        if not res.ok or "@@END" not in res.out:
            raise CommandError(["snapshot"], res.code, res.err or "incomplete snapshot output")
        return parse_snapshot(self.env.id, res.out)

    async def new_session(
        self,
        socket: str,
        *,
        name: str,
        cwd: str,
        argv: Sequence[str],
        env_vars: Mapping[str, str] | None = None,
        relay_id: str | None = None,
        width: int = 200,
        height: int = 50,
    ) -> tuple[str, str, int, int]:
        """Start a detached session running ``argv``. Returns (session_id, pane_id, created, server_pid)."""
        fmt = DELIM.join(["#{pid}", "#{session_id}", "#{session_created}", "#{pane_id}"])
        args = ["new-session", "-d", "-P", "-F", fmt, "-s", name, "-c", cwd, "-x", str(width), "-y", str(height)]
        for key, value in (env_vars or {}).items():
            args += ["-e", f"{key}={value}"]
        args += ["--", *argv]
        out = (await self._tmux(socket, *args)).strip().splitlines()
        parts = out[-1].split(DELIM) if out else []
        if len(parts) != 4:
            raise TmuxError(f"unexpected new-session output: {out!r}")
        server_pid, session_id, created, pane_id = parts
        if relay_id:
            await self.set_session_option(socket, session_id, "@relay_id", relay_id)
        return session_id, pane_id, _int(created), _int(server_pid)

    async def set_session_option(self, socket: str, session_id: str, name: str, value: str) -> None:
        _check_target(session_id, SESSION_ID_RE, "session")
        await self._tmux(socket, "set-option", "-t", session_id, name, value)

    async def has_session(self, socket: str, session_id: str) -> bool:
        _check_target(session_id, SESSION_ID_RE, "session")
        res = await self.podman.exec(self.env, ["tmux", "-S", socket, "has-session", "-t", session_id])
        return res.ok

    async def kill_session(self, socket: str, session_id: str) -> None:
        _check_target(session_id, SESSION_ID_RE, "session")
        await self._tmux(socket, "kill-session", "-t", session_id)

    async def send_keys(self, socket: str, pane_id: str, *keys: str) -> None:
        """Send tmux key names (for example ``Enter`` or ``Escape``) to a pane."""
        _check_target(pane_id, PANE_ID_RE, "pane")
        if not keys:
            return
        await self._tmux(socket, "send-keys", "-t", pane_id, *keys)

    async def paste_text(self, socket: str, pane_id: str, text: str, *, submit: bool = True) -> None:
        """Paste ``text`` as one bracketed paste, then press Enter if ``submit``.

        The text travels on stdin into a tmux buffer. It is never part of a
        command line, so it cannot become shell syntax on the host or in the
        container.
        """
        _check_target(pane_id, PANE_ID_RE, "pane")
        buffer = f"relay-{secrets.token_hex(6)}"
        res = await self.podman.exec(
            self.env,
            ["sh", "-c", PASTE_SCRIPT, "relay-paste", buffer, socket, pane_id, "1" if submit else "0"],
            input=text.encode("utf-8"),
        )
        if not res.ok:
            raise CommandError(["paste"], res.code, res.err)

    async def capture(self, socket: str, pane_id: str, *, lines: int = 80) -> str:
        """Return the visible screen plus up to ``lines`` of scrollback, wrapped lines joined."""
        _check_target(pane_id, PANE_ID_RE, "pane")
        return await self._tmux(socket, "capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{int(lines)}")

    async def scroll(self, socket: str, session_id: str, lines: int) -> ScrollState:
        """Scroll the session's visible pane through its history and return the pane state.

        See ``scroll_args``. One ``podman exec`` per call; the caller coalesces.
        """
        out = await self._tmux(socket, *scroll_args(session_id, lines))
        return parse_scroll_state(out)

    def attach_argv(self, socket: str, session_id: str) -> list[str]:
        """Full argv (including the podman binary) to attach a TTY client to a session.

        ``-u`` forces UTF-8 so agent TUIs render box-drawing characters. The
        attach does not use ``-d``, so clients on the laptop stay attached.
        """
        _check_target(session_id, SESSION_ID_RE, "session")
        args = self.podman.exec_args(
            self.env,
            ["tmux", "-u", "-S", socket, "attach-session", "-t", session_id],
            interactive=True,
            tty=True,
            extra_env={"TERM": "xterm-256color"},
        )
        return self.podman.argv(args)
