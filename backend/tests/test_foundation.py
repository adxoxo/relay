from relay import activity
from relay.tmux import parse_snapshot, detect_kind, SESSION_FORMAT
def test_foundation_smoke(rt):
    sid = rt.db.insert_session(dict(env="work1", kind="shell", display_name="a", tmux_name="a", origin="dashboard", status="live", socket_path="/tmp/tmux-1000/default"))
    assert rt.db.get_session(sid)["kind"] == "shell"
    assert activity.record(rt.db, kind="turn_complete", title="t", dedupe_key="k") is not None
    assert activity.record(rt.db, kind="turn_complete", title="t", dedupe_key="k") is None
    assert activity.unread_count(rt.db) == 1
    text = "@@SOCKET\t/s\n@@SESSIONS\n9|~|$1|~|100|~|1|~|0|~|100|~||~|a|~|b\n@@PANES\n$1|~|@1|~|0|~|1|~|%3|~|0|~|1|~|50|~|0|~|80|~|24|~|100|~|claude|~|/home/node\n@@PS\n50 1 -bash\n51 50 claude --x\n@@END\n"
    snap = parse_snapshot("work1", text)
    s = snap.sessions[0]
    assert s.name == "a|~|b" and s.server_pid == 9 and s.target_pane.pane_id == "%3"
    assert detect_kind(snap, s.target_pane) == "claude"
    assert rt.settings.envs["work1"].host_path("/home/node/.claude/x.jsonl") == rt.settings.envs["work1"].host_home / ".claude/x.jsonl"
    assert rt.settings.envs["work1"].host_path("/home/node/../etc") is None
