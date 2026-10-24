"""Native Codex links for every pane: writer lock, guarded /status, managed title field.

The fake container answers relay's native probe and process probe with the
identity fields that the live Codex 0.160 probes returned on 2026-10-04.
"""

from __future__ import annotations

import json
import os

import pytest
from fakes import ok
from test_codex_runtime import CWD, _write_thread

from relay import codex_runtime as C
from relay import hooks_install, terminal_history
from relay.db import loads
from relay.tmux import Snapshot, SocketState, TmuxPane, TmuxSession

SOCK = "/tmp/tmux-1000/default"
A = "01a104cc-2e86-7f11-b02c-dbc4f8927f04"
B = "01a104d0-1111-7222-8333-444455556666"
NEW = "01a104d5-aaaa-7bbb-8ccc-dddd0000eeee"
UNSAVED = "01a104e9-0601-7b20-a7be-d810b02750b8"


def _title(thread: str, name: str = "Fix the build") -> str:
    """Codex 0.160 title with relay's items: activity, thread-name, project-name, thread-id (cut)."""
    return f"⠼ {name} | demo | {thread[:29]}..."


@pytest.fixture(autouse=True)
def managed_title(rt):
    """Startup provisioning puts the native thread-id item last in the env config."""
    assert hooks_install.codex_title_install(rt.settings.env("work2"))["terminal_title"] == "relay"


class World:
    """Container state behind the fake ``podman exec``."""

    def __init__(self) -> None:
        self.panes: dict[str, dict] = {}
        self.daemon: list[str] = []
        self.other: list[tuple[str, int]] = []
        self.screens: list[str] = []
        self.keys: list[list[str]] = []
        self.on_proc_probe = None

    def pane(self, pane_id: str, tui_pid: int, title: str = "", own: list[str] | None = None) -> None:
        self.panes[pane_id] = {"tui_pid": tui_pid, "ticks": tui_pid * 10, "title": title, "own": own or []}

    def handler(self, args, _input):
        if not args or args[0] != "exec":
            return ok()
        cmd = args[args.index("job-work2") + 1:]
        if cmd[:3] == ["python3", "-c", C._NATIVE_PROBE]:
            p, pane_pid = self.panes[cmd[4]], int(cmd[5])
            locks = [{"thread": t, "pid": p["tui_pid"], "role": "tui"} for t in p["own"]]
            locks += [{"thread": t, "pid": 7, "role": "daemon"} for t in self.daemon]
            locks += [{"thread": t, "pid": pid, "role": "other"} for t, pid in self.other]
            return ok(json.dumps({
                "stage": "done", "boot_id": "boot-1", "uptime": 10_000.0, "clk_tck": 100,
                "pane": {"state": "S", "ppid": 1, "start_ticks": 10, "tty": f"/dev/pts/{pane_pid}"},
                "tuis": [{"pid": p["tui_pid"], "ppid": pane_pid, "start_ticks": p["ticks"], "state": "S",
                          "tty": f"/dev/pts/{pane_pid}"}],
                "in_mode": False, "title": p["title"], "locks": locks,
            }))
        if cmd[:3] == ["python3", "-c", C._PROC_PROBE]:
            tui, pane = int(cmd[3]), int(cmd[4])
            if self.on_proc_probe is not None:
                self.on_proc_probe()
            p = next((v for v in self.panes.values() if v["tui_pid"] == tui), None)
            if p is None:
                return ok(json.dumps({"stage": "tui", "boot_id": "boot-1", "error": "FileNotFoundError",
                                      "tui_missing": True}))
            return ok(json.dumps({
                "stage": "chain", "boot_id": "boot-1",
                "tui": {"state": "S", "ppid": pane, "start_ticks": p["ticks"], "tty": f"/dev/pts/{pane}",
                        "argv0": "/usr/lib/codex/codex"},
                "pane": {"state": "S", "ppid": 1, "start_ticks": 10, "tty": f"/dev/pts/{pane}"},
                "chain": [tui, pane],
            }))
        if cmd[:1] == ["tmux"] and "capture-pane" in cmd:
            return ok(self.screens.pop(0) if self.screens else "")
        if cmd[:1] == ["tmux"] and "send-keys" in cmd:
            self.keys.append(cmd[cmd.index("send-keys") + 3:])
        return ok()


@pytest.fixture
def world(fake_podman) -> World:
    w = World()
    fake_podman.handler = w.handler
    return w


def _row(rt, n: int) -> str:
    return rt.db.insert_session(dict(
        env="work2", kind="codex", display_name=f"agent{n}", tmux_name=f"agent{n}", origin="discovered",
        status="live", state="working", socket_path=SOCK, server_pid=9, tmux_session_id=f"${n}",
        pane_id=f"%{n}", pane_pid=100 + n,
    ))


def _snapshot(*ns: int) -> Snapshot:
    snap = Snapshot(env_id="work2", sockets=[SocketState(SOCK, "ok", 9)])
    for n in ns:
        session = TmuxSession(socket=SOCK, server_pid=9, id=f"${n}", created=0, windows=1, attached=0, activity=0,
                              relay_id=None, name=f"agent{n}")
        session.panes.append(TmuxPane(
            session_id=f"${n}", window_id=f"@{n}", window_index=0, window_active=True, pane_id=f"%{n}", pane_index=0,
            pane_active=True, pid=100 + n, dead=False, width=200, height=50, activity=0, current_command="node",
            current_path=CWD,
        ))
        snap.sessions.append(session)
    return snap


async def _poll(rt, *ns: int) -> None:
    rt.cache.pop("codex_native_checked:work2", None)  # the next poll is due at once
    await C.poll_env(rt, "work2", _snapshot(*ns))


def _binding(rt, rid: str) -> dict | None:
    return C.binding_of(rt.db.get_session(rid))


def _completions(rt) -> int:
    return len(rt.db.query("SELECT id FROM activity WHERE kind = 'turn_complete'"))


# -- title field --------------------------------------------------------------------


@pytest.mark.parametrize("title", [
    f"⠼ Fix | demo | {A[:29]}...",
    f"⠼ Fix ⠙ | demo | {A[:29]}... ⠙",  # thread naming in progress
    f"[ ! ] Action Required | Fix | demo | {A[:29]}...",
    f"demo | {A[:29]}...",
    f"⠼ {A[:29]}...",
    f"{A[:29]}...",
    f"⠼ talk about {B} | demo | {A[:29]}...",  # a foreign id in the thread name is ignored
])
def test_title_field_is_the_managed_last_field(title):
    assert C.title_thread_prefix(title) == A[:29]


@pytest.mark.parametrize("title", [
    f"⠼ Fix | demo | {A}",  # a full id is never the native field
    f"⠼ {A} | demo",  # pre-config title: thread name holds a full id
    f"⠼ {A[:29]}... | demo",  # cut id, but not the last field
    f"⠼ Fix | demo | {A[:28]}...",
    f"⠼ Fix | demo | {A[:29]}..x",
    f"⠼ Fix | demo | {A[:29]}... ⠙⠙",
    f"⠼ Fix | demo |{A[:29]}...",
    f"Fix-{A[:29]}...",
    "⠼ Revert and improve portfolio | projects",
])
def test_title_without_the_managed_field_names_no_thread(title):
    assert C.title_thread_prefix(title) is None


# -- full poll path -------------------------------------------------------------------


@pytest.mark.parametrize("version", ["0.160.0", "0.163.0"])
async def test_written_titles_link_same_cwd_daemon_panes_and_follow_new_threads(rt, world, version):
    env = rt.settings.env("work2")
    def write_thread(thread):
        return _write_thread(env, thread=thread, version=version, index_source="vscode", meta_source="vscode")

    path_a, path_b = write_thread(A), write_thread(B)
    r1, r2 = _row(rt, 1), _row(rt, 2)
    world.daemon = [A, B]
    world.pane("%1", 501, _title(A))
    world.pane("%2", 502, _title(B, "Other task"))

    await _poll(rt, 1, 2)  # first title of each process: tmux may have kept it from another process
    assert _binding(rt, r1) is None and _binding(rt, r2) is None

    world.panes["%1"]["title"] = _title(A, "Fix the build ⠙")
    world.panes["%2"]["title"] = _title(B, "Other task ⠙")
    await _poll(rt, 1, 2)

    b1, b2 = _binding(rt, r1), _binding(rt, r2)
    assert (b1["status"], b1["source"], b1["method"], b1["thread_id"], b1["tui_pid"]) == (
        "verified", "native", "terminal_title", A, 501)
    assert (b2["thread_id"], b2["tui_pid"]) == (B, 502)
    row1 = rt.db.get_session(r1)
    assert (row1["agent_session_id"], row1["transcript_path"]) == (A, path_a)
    assert rt.db.get_session(r2)["transcript_path"] == path_b
    assert terminal_history.conversation_source_for(row1, env).agent_session_id == A
    assert _completions(rt) == 0  # the rollout bootstrap records no Activity

    # /new in pane 1, saved by Codex: the process writes a new title.
    write_thread(NEW)
    world.daemon.append(NEW)
    world.panes["%1"]["title"] = _title(NEW, "New task")
    await _poll(rt, 1, 2)
    b1 = _binding(rt, r1)
    assert (b1["status"], b1["thread_id"], b1["replaced_thread_id"]) == ("verified", NEW, A)
    assert _binding(rt, r2)["thread_id"] == B
    assert _completions(rt) == 0

    # /new again, not saved yet: the old link stops; it links once Codex saves it.
    world.daemon.append(UNSAVED)
    world.panes["%1"]["title"] = _title(UNSAVED)
    await _poll(rt, 1, 2)
    assert _binding(rt, r1)["status"] == "stale"
    with pytest.raises(terminal_history.HistoryUnavailable):
        terminal_history.conversation_source_for(rt.db.get_session(r1), env)
    write_thread(UNSAVED)
    await _poll(rt, 1, 2)
    assert (_binding(rt, r1)["status"], _binding(rt, r1)["thread_id"]) == ("verified", UNSAVED)


async def test_foreign_id_in_a_thread_name_never_links(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    _write_thread(env, thread=B)
    rid = _row(rt, 1)
    world.daemon = [A, B]  # A: another live thread that no relay row tracks
    world.pane("%1", 501, f"Ready | {A} | demo")
    await _poll(rt, 1)
    world.panes["%1"]["title"] = f"⠼ {A} | demo"  # written by the process, still no native field
    await _poll(rt, 1)
    world.panes["%1"]["title"] = f"⠼ {A[:29]}... | demo"
    await _poll(rt, 1)
    assert _binding(rt, rid) is None and world.keys == []

    world.panes["%1"]["title"] = f"⠼ {A} | demo | {B[:29]}..."  # the managed last field names B
    await _poll(rt, 1)
    assert (_binding(rt, rid)["thread_id"], _binding(rt, rid)["method"]) == (B, "terminal_title")


async def test_title_needs_the_managed_config(rt, world):
    env = rt.settings.env("work2")
    env.host_codex_config.write_text('[tui]\nterminal_title = ["thread-id", "project-name"]\n')
    _write_thread(env, thread=A)
    rid = _row(rt, 1)
    world.daemon = [A]
    world.pane("%1", 501, "Ready | demo")
    await _poll(rt, 1)
    world.panes["%1"]["title"] = f"⠼ demo | {A[:29]}..."
    await _poll(rt, 1)
    assert _binding(rt, rid) is None


async def test_retained_title_never_links_a_replaced_process(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    _write_thread(env, thread=B)
    rid = _row(rt, 1)
    world.daemon = [A, B]
    world.pane("%1", 501, _title(A))
    await _poll(rt, 1)
    world.panes["%1"]["title"] = _title(A, "Fix the build ⠙")
    await _poll(rt, 1)
    assert (_binding(rt, rid)["thread_id"], _binding(rt, rid)["tui_pid"]) == (A, 501)

    # A new Codex process in the pane; tmux still shows the old process's title.
    world.pane("%1", 503, _title(A, "Fix the build ⠙"))
    await _poll(rt, 1)
    binding = _binding(rt, rid)
    assert (binding["status"], binding["tui_pid"]) == ("stale", 501)

    # One guarded /status proves what the new process shows.
    world.screens = [_IDLE, _TYPED, _card(B), _card(B)]
    await _poll(rt, 1)
    binding = _binding(rt, rid)
    assert (binding["status"], binding["method"], binding["thread_id"], binding["tui_pid"]) == (
        "verified", "status", B, 503)


async def test_link_waits_when_native_state_changes_during_the_checks(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    _write_thread(env, thread=NEW)
    rid = _row(rt, 1)
    world.daemon = [A, NEW]
    world.pane("%1", 501, "Ready | demo")
    await _poll(rt, 1)
    world.panes["%1"]["title"] = _title(A)

    def new_thread_during_process_check():
        world.panes["%1"]["title"] = _title(NEW, "New task")

    world.on_proc_probe = new_thread_during_process_check
    await _poll(rt, 1)
    assert _binding(rt, rid) is None  # A was checked, but the TUI showed NEW before the write

    world.on_proc_probe = None
    await _poll(rt, 1)
    assert _binding(rt, rid)["thread_id"] == NEW


async def test_writer_lock_links_embedded_tui_and_refuses_a_lock_change_during_checks(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    _write_thread(env, thread=NEW)
    rid = _row(rt, 1)
    world.pane("%1", 501, "Start work session | projects", own=[A])
    world.on_proc_probe = lambda: world.panes["%1"].update(own=[NEW])

    await _poll(rt, 1)
    assert _binding(rt, rid) is None

    world.on_proc_probe = None
    await _poll(rt, 1)
    binding = _binding(rt, rid)
    assert (binding["method"], binding["thread_id"], binding["tui_start_ticks"]) == ("writer_lock", NEW, 5010)

    # A new Codex process in the same pane holds the lock: fresh proof, no /status.
    world.pane("%1", 503, "Start work session | projects", own=[NEW])
    await _poll(rt, 1)
    assert (_binding(rt, rid)["status"], _binding(rt, rid)["tui_pid"]) == ("verified", 503)
    assert world.keys == []


def _probe(title: str = "", own: list[str] | None = None, daemon: list[str] | None = None, other: list[str] | None = None,
           tuis: int = 1, tty: str = "/dev/pts/7") -> dict:
    locks = [{"thread": t, "pid": 50, "role": "tui"} for t in own or []]
    locks += [{"thread": t, "pid": 7, "role": "daemon"} for t in daemon or []]
    locks += [{"thread": t, "pid": 60, "role": "other"} for t in other or []]
    return {"stage": "done", "boot_id": "b", "uptime": 10_000.0, "clk_tck": 100,
            "pane": {"start_ticks": 1, "tty": "/dev/pts/7"},
            "tuis": [{"pid": 50 + i, "start_ticks": 9, "tty": tty} for i in range(tuis)], "title": title, "locks": locks}


@pytest.mark.parametrize("case", [
    "two_tuis", "other_tty", "not_held", "held_by_other_process", "subagent", "prefix_not_unique",
    "untrusted_title", "no_field",
])
def test_native_selection_fails_closed(rt, case):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    sub = "01a104d0-2222-7333-8444-555566667777"
    _write_thread(env, thread=sub, parent=B)
    probe = {
        "two_tuis": _probe(_title(A), daemon=[A], tuis=2),
        "other_tty": _probe(_title(A), daemon=[A], tty="/dev/pts/9"),
        "not_held": _probe(_title(A), daemon=[B]),
        "held_by_other_process": _probe(_title(A), other=[A]),
        "subagent": _probe(_title(sub), daemon=[sub]),
        "prefix_not_unique": _probe(_title(A), daemon=[A]),
        "untrusted_title": _probe(_title(A), daemon=[A]),
        "no_field": _probe("⠼ Revert and improve portfolio | projects", daemon=[A]),
    }[case]
    if case == "prefix_not_unique":
        _write_thread(env, thread=A[:29] + "0000000")
    with pytest.raises(C.NativeEvidenceMissing) as exc:
        C.select_native_thread(env, probe, title_trusted=case != "untrusted_title")
    assert isinstance(exc.value, C.TitleWithoutThread) == (case in ("untrusted_title", "no_field"))


def test_native_selection_names_the_lock_or_the_field(rt):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    assert C.select_native_thread(env, _probe(_title(A), daemon=[A, B]), title_trusted=True) == (A, "terminal_title")
    assert C.select_native_thread(env, _probe(_title(B), own=[A]), title_trusted=False) == (A, "writer_lock")
    with pytest.raises(C.ThreadNotSaved) as exc:
        C.select_native_thread(env, _probe(_title(UNSAVED), daemon=[UNSAVED]), title_trusted=True)
    assert exc.value.thread_id == UNSAVED


# -- lock map in the container probe ----------------------------------------------------


def test_lock_map_refuses_symlinks_hardlinks_and_other_files(tmp_path):
    probe: dict = {"__name__": "relay_probe_test"}
    exec(C._NATIVE_PROBE, probe)  # defines the probe functions; main() does not run
    locks = tmp_path / "thread-writer-locks"
    locks.mkdir()
    (locks / f"{A}.lock").write_text("")
    os.symlink(locks / f"{A}.lock", locks / f"{B}.lock")
    (locks / f"{NEW}.lock").write_text("")
    os.link(locks / f"{NEW}.lock", locks / f"{UNSAVED}.lock")
    (locks / "01a104d0-2222-7333-8444-555566667777.lock").mkdir()
    (locks / "notes.txt").write_text("")

    files = probe["lock_files"](str(locks))

    st = os.stat(locks / f"{A}.lock")
    key = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    assert files == {key: A}
    line = f"1: FLOCK  ADVISORY  WRITE 2950 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF"
    assert probe["held_locks"]([line], files, {2950: "tui"}) == [{"thread": A, "pid": 2950, "role": "tui"}]
    os.symlink(locks, tmp_path / "linked-locks")
    with pytest.raises(NotADirectoryError):
        probe["lock_files"](str(tmp_path / "linked-locks"))


# -- guarded /status --------------------------------------------------------------------

_IDLE = "  Shall we start?\n\n› Ask Codex to do anything\n\n  GPT-6.1-Sol high · ~/projects/demo\n"
_TYPED = "› /status      show current session configuration\n  /statusline  configure\n\n› /status\n\n  GPT-6.1-Sol high\n"


def _card(thread: str) -> str:
    return (f"  Shall we start?\n\n/status\n\n  >_ OpenAI Codex (v0.160.0)\n  Server:              Local background server\n"
            f"  Session:             {thread}\n\n› Ask Codex to do anything\n\n  GPT-6.1-Sol high\n")


_VALUE_INDENT = " " * 23


def _narrow_card(session_lines: str) -> str:
    """A /status card from a 51-column pane (Codex 0.162.1 layout, captured 2026-10-11)."""
    return (f"  Shall we start?\n\n/status\n\n  >_ OpenAI Codex (v0.162.1)\n  Server:\n{_VALUE_INDENT}Local background\n"
            f"{session_lines}\n› Ask Codex to do anything\n\n  GPT-6.1-Sol high\n")


def _wrapped(thread: str, cut: int = 28) -> str:
    """The Session label alone, the id wrapped onto two indented value lines."""
    return f"  Session:\n{_VALUE_INDENT}{thread[:cut]}\n{_VALUE_INDENT}{thread[cut:]}\n"


async def test_status_proves_a_first_seen_daemon_tui_once_even_with_a_title_field(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    _write_thread(env, thread=B)
    rid = _row(rt, 1)
    world.daemon = [A, B]
    world.pane("%1", 501, _title(B))  # an old title that this process may not have written
    world.screens = [_IDLE, _TYPED, _card(A), _card(A)]  # the last capture: unchanged before the write

    await _poll(rt, 1)

    assert world.keys == [["-l", "/status"], ["Enter"]]
    binding = _binding(rt, rid)
    assert (binding["status"], binding["method"], binding["thread_id"]) == ("verified", "status", A)

    world.screens = [_IDLE, _TYPED, _card(A)]
    await _poll(rt, 1)  # proven by its link: no second command
    assert world.keys == [["-l", "/status"], ["Enter"]]


async def test_status_links_an_old_pre_config_daemon_tui_like_agent2(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    agent1_thread = "01a104a1-1f04-78c2-a039-29485a1efa0e"
    _write_thread(env, thread=agent1_thread)
    rid = _row(rt, 1)
    world.daemon = [A]
    world.other = [(agent1_thread, 2950)]  # agent1's embedded TUI holds its own lock
    world.pane("%1", 13997, "⠼ Revert and improve portfolio | projects")
    world.screens = [_IDLE, _TYPED, _card(A), _card(A)]

    await _poll(rt, 1)

    binding = _binding(rt, rid)
    assert (binding["method"], binding["thread_id"], binding["tui_pid"]) == ("status", A, 13997)
    assert world.keys == [["-l", "/status"], ["Enter"]]


async def test_status_proof_is_not_written_after_a_new_thread_during_the_checks(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    _write_thread(env, thread=B)
    rid = _row(rt, 1)
    world.daemon = [A]  # the daemon keeps A's lock after /new
    world.pane("%1", 501, "⠼ Revert and improve portfolio | projects")  # no title from this process
    world.screens = [_IDLE, _TYPED, _card(A)]

    probes = []

    def new_thread_during_process_check():
        probes.append(1)
        if len(probes) == 2:  # the 1st probe is the check before /status; the 2nd is the link check
            world.daemon.append(B)
            world.screens = ["  New session\n\n› Ask Codex to do anything\n\n  GPT-6.1-Sol high\n"]

    world.on_proc_probe = new_thread_during_process_check
    await _poll(rt, 1)
    assert _binding(rt, rid) is None  # A is still held, but the /status screen is gone

    world.on_proc_probe = None
    world.screens = [_IDLE, _TYPED, _card(B), _card(B)]
    await _poll(rt, 1)
    assert _binding(rt, rid) is None and world.keys == [["-l", "/status"], ["Enter"]]  # no second /status


async def test_restart_trusts_the_exact_linked_process_and_follows_its_new_thread(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    _write_thread(env, thread=B)
    rid = _row(rt, 1)
    world.daemon = [A, B]
    world.pane("%1", 501, "Ready | demo")
    await _poll(rt, 1)
    world.panes["%1"]["title"] = _title(A)
    await _poll(rt, 1)
    assert (_binding(rt, rid)["thread_id"], _binding(rt, rid)["tui_pid"], _binding(rt, rid)["method"]) == (
        A, 501, "terminal_title")  # a title link proves that this process writes its title

    rt.cache.clear()  # relay restarts; the same TUI did /new to B meanwhile
    world.panes["%1"]["title"] = _title(B, "Other task")
    await _poll(rt, 1)

    binding = _binding(rt, rid)
    assert (binding["status"], binding["thread_id"], binding["method"], binding["replaced_thread_id"]) == (
        "verified", B, "terminal_title", A)
    assert world.keys == []


async def _status_link(rt, world, title: str, thread: str) -> str:
    """A pre-config daemon TUI (pid 501) linked to ``thread`` by its one /status."""
    rid = _row(rt, 1)
    world.pane("%1", 501, title)
    world.screens = [_IDLE, _TYPED, _card(thread), _card(thread)]
    await _poll(rt, 1)
    assert (_binding(rt, rid)["method"], _binding(rt, rid)["thread_id"]) == ("status", thread)
    return rid


async def test_restart_never_trusts_an_unchanged_title_of_a_status_link(rt, world):
    env = rt.settings.env("work2")
    foreign = "01a104d0-2222-7333-8444-555566667777"
    for thread in (A, foreign):
        _write_thread(env, thread=thread)
    world.daemon = [A, foreign]  # another live thread that no relay row tracks
    old_title = f"demo | {foreign[:29]}..."  # an old layout: a thread name shaped like the native field
    rid = await _status_link(rt, world, old_title, A)

    rt.cache.clear()  # relay restarts; the title did not change
    await _poll(rt, 1)

    binding = _binding(rt, rid)
    assert (binding["status"], binding["thread_id"], binding["method"]) == ("verified", A, "status")


async def test_restart_follows_a_new_managed_title_of_a_status_link(rt, world):
    env = rt.settings.env("work2")
    for thread in (A, B):
        _write_thread(env, thread=thread)
    world.daemon = [A, B]
    rid = await _status_link(rt, world, "⠼ Revert and improve portfolio | projects", A)

    rt.cache.clear()  # relay restarts; meanwhile the TUI did /new and reloaded the managed title
    world.panes["%1"]["title"] = _title(B, "Next task")
    await _poll(rt, 1)

    binding = _binding(rt, rid)
    assert (binding["status"], binding["thread_id"], binding["method"], binding["replaced_thread_id"]) == (
        "verified", B, "terminal_title", A)
    assert world.keys == [["-l", "/status"], ["Enter"]]


async def test_status_attempt_survives_a_restart_and_a_written_title_still_links(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    rid = _row(rt, 1)
    world.daemon = [A]
    world.pane("%1", 501, "Revert | projects")
    world.screens = [_IDLE, "› fix it/status\n\n  GPT-6.1-Sol high\n"]
    await _poll(rt, 1)
    assert world.keys == [["-l", "/status"]]  # composer changed: no Enter
    mark = loads(rt.db.get_session(rid)["meta_json"])[C.STATUS_ATTEMPT_KEY]
    assert (mark["boot_id"], mark["tui_pid"], mark["tui_start_ticks"]) == ("boot-1", 501, 5010)

    rt.cache.clear()  # relay restarts
    world.screens = [_IDLE, _TYPED, _card(A), _card(A)]
    await _poll(rt, 1)
    assert world.keys == [["-l", "/status"]] and _binding(rt, rid) is None  # never a second /status

    world.panes["%1"]["title"] = _title(A)  # the process writes its title
    await _poll(rt, 1)
    assert (_binding(rt, rid)["thread_id"], _binding(rt, rid)["method"]) == (A, "terminal_title")
    assert C.STATUS_ATTEMPT_KEY in loads(rt.db.get_session(rid)["meta_json"])  # other meta stays


async def test_a_busy_screen_is_not_an_attempt(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    rid = _row(rt, 1)
    world.daemon = [A]
    world.pane("%1", 501, "Revert | projects")
    world.screens = ["• Working (3s • esc to interrupt)\n\n› Ask Codex to do anything\n"]
    await _poll(rt, 1)
    assert world.keys == [] and C.STATUS_ATTEMPT_KEY not in (loads(rt.db.get_session(rid)["meta_json"]) or {})

    world.screens = [_IDLE, _TYPED, _card(A), _card(A)]
    await _poll(rt, 1)
    assert world.keys == [["-l", "/status"], ["Enter"]]
    assert _binding(rt, rid)["thread_id"] == A


# Live agent2 shape (2026-10-04): agent prose that asks for approval, then the empty composer and the footer.
_PROSE_IDLE = "\n".join([
    "• Updated the portfolio hero and the project cards.",
    "",
    "  Do you approve this mockup, or should I change the colors first?",
    "",
    "  If you approve this, I will apply the same layout to the about page and the contact page.",
    "",
    "  Reply with the changes you want.",
    "",
    "",
    "› Ask Codex to do anything",
    "",
    "  GPT-6.1-Sol high · ~/projects/personal-projects/adam-portfolio",
    "  ← for agents · ? for shortcuts",
]) + "\n"
_APPROVAL = (
    "  Would you like to run the following command?\n\n  $ rm -rf build\n\n› 1. Yes, proceed (y)\n"
    "  2. Yes, and don't ask again for this command (a)\n  3. No, and tell Codex what to do differently (esc)\n\n"
    "  Press enter to confirm or esc to cancel\n"
)


def test_agent_prose_about_approval_is_not_a_dialog():
    assert C.composer_idle_empty(_PROSE_IDLE)
    assert not C.composer_idle_empty(_APPROVAL)
    assert not C.composer_idle_empty("  Trust this folder?\n› 1. Trust and continue\n  2. Quit\n")
    assert not C.composer_idle_empty(_PROSE_IDLE + "› 1. Yes, proceed (y)\n")  # a choice below the composer


async def test_status_links_an_idle_tui_whose_last_answer_asks_for_approval(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    rid = _row(rt, 1)
    world.daemon = [A]
    world.pane("%1", 13997, "⠼ Revert and improve portfolio | projects")
    world.screens = [_PROSE_IDLE, _TYPED, _card(A), _card(A)]

    await _poll(rt, 1)

    assert world.keys == [["-l", "/status"], ["Enter"]]
    assert (_binding(rt, rid)["method"], _binding(rt, rid)["thread_id"]) == ("status", A)


@pytest.mark.parametrize("before", [
    "› fix the failing build\n\n  GPT-6.1-Sol high\n",
    "• Working (3s • esc to interrupt)\n\n› Ask Codex to do anything\n",
    "  Would you like to run the following command?\n› 1. Yes, proceed (y)\n› Ask Codex to do anything\n",
    _APPROVAL,
])
async def test_status_never_types_into_a_busy_or_typed_composer(rt, world, before):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    rid = _row(rt, 1)
    world.daemon = [A]
    world.pane("%1", 501, "Revert | projects")
    world.screens = [before]

    await _poll(rt, 1)

    assert world.keys == []
    assert _binding(rt, rid) is None


async def test_status_is_typed_once_per_process_and_a_written_title_still_links(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    rid = _row(rt, 1)
    world.daemon = [A]
    world.pane("%1", 501, "Revert | projects")
    world.screens = [_IDLE, "› fix it/status\n\n  GPT-6.1-Sol high\n"]

    await _poll(rt, 1)
    assert world.keys == [["-l", "/status"]]  # the composer changed: no Enter
    assert _binding(rt, rid) is None

    world.screens = [_IDLE, _TYPED, _card(A)]
    await _poll(rt, 1)
    assert world.keys == [["-l", "/status"]]  # never a second /status for this process

    world.panes["%1"]["title"] = _title(A)  # a later title written by the process
    await _poll(rt, 1)
    assert (_binding(rt, rid)["thread_id"], _binding(rt, rid)["method"]) == (A, "terminal_title")


def test_status_card_must_be_new():
    assert C.status_session_id(_IDLE, _card(A)) == A
    assert C.status_session_id(_card(B), _card(B)) is None  # an older card is never read


def test_status_card_reads_the_narrow_wrapped_session_value():
    # The live 0.162.1 shape: the label alone, then 28 + 8 id characters.
    assert C.status_session_id(_IDLE, _narrow_card(_wrapped(A))) == A
    # The label line can hold the start of the id.
    partial = f"  Session:  {A[:15]}\n{_VALUE_INDENT}{A[15:]}\n"
    assert C.status_session_id(_IDLE, _narrow_card(partial)) == A
    # Three value lines.
    three = f"  Session:\n{_VALUE_INDENT}{A[:12]}\n{_VALUE_INDENT}{A[12:24]}\n{_VALUE_INDENT}{A[24:]}\n"
    assert C.status_session_id(_IDLE, _narrow_card(three)) == A
    # Ids in other fields are not read.
    other = f"  Parent:\n{_VALUE_INDENT}{B}\n{_wrapped(A)}"
    assert C.status_session_id(_IDLE, _narrow_card(other)) == A


def test_status_card_rejects_unread_or_malformed_session_values():
    narrow = _narrow_card(_wrapped(A))
    cases = {
        "stale card": (narrow, narrow),
        "no Session field, an id in another field": (_IDLE, _narrow_card(f"  Thread:\n{_VALUE_INDENT}{A}\n")),
        "truncated value": (_IDLE, _narrow_card(f"  Session:\n{_VALUE_INDENT}{A[:28]}\n")),
        "value goes on past one id": (_IDLE, _narrow_card(f"{_wrapped(A)}{_VALUE_INDENT}abcd\n")),
        "blank line inside the value": (_IDLE, _narrow_card(f"  Session:\n{_VALUE_INDENT}{A[:28]}\n\n{_VALUE_INDENT}{A[28:]}\n")),
        "not an id after the label": (_IDLE, _narrow_card("  Session:  (none)\n")),
        "two ids": (_IDLE, _narrow_card(f"{_wrapped(A)}  Session:             {B}\n")),
        "one good and one malformed field": (_IDLE, _narrow_card(f"  Session:             {A}\n  Session:\n{_VALUE_INDENT}{B[:20]}\n")),
        "wrong hyphen places": (_IDLE, _narrow_card(f"  Session:\n{_VALUE_INDENT}{A.replace('-', '')[:28]}\n{_VALUE_INDENT}{A.replace('-', '')[28:]}----\n")),
    }
    for name, (before, after) in cases.items():
        assert C.status_session_id(before, after) is None, name
    # The same id twice (the full card repeated in one capture) is still one id.
    assert C.status_session_id(_IDLE, _narrow_card(f"{_wrapped(A)}  Session:             {A}\n")) == A


async def test_status_links_a_daemon_tui_from_the_narrow_card(rt, world):
    env = rt.settings.env("work2")
    _write_thread(env, thread=A)
    rid = _row(rt, 1)
    world.daemon = [A]
    world.pane("%1", 13997, "⠼ Revert and improve portfolio | projects")
    card = _narrow_card(_wrapped(A))
    world.screens = [_IDLE, _TYPED, card, card]

    await _poll(rt, 1)

    binding = _binding(rt, rid)
    assert (binding["method"], binding["thread_id"], binding["tui_pid"]) == ("status", A, 13997)
    assert world.keys == [["-l", "/status"], ["Enter"]]
