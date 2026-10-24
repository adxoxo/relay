# relay architecture and build contract

relay is a private dashboard for agent sessions that run in tmux inside the
two Podman work containers (`job-work1`, `job-work2`). It runs on the Ubuntu
host as user `adyu`, listens on `127.0.0.1:8765`, and is reached from a phone
through Tailscale Serve. Scope and rules: `plans/agent-dashboard-mockup/BUILD_PLAN.md`.
Approved UI design: `plans/agent-dashboard-mockup/index.html` (+ screenshots).

This file is the contract between modules. Builders implement against it.

## 1. Layout

```
relay/
  backend/relay/        Python package (FastAPI app, run from backend/)
  backend/tests/        pytest suite (asyncio_mode=auto)
  frontend/             React + TypeScript + Vite, built into frontend/dist
  bin/relay             CLI wrapper
  deploy/               systemd user units
  docs/                 operating guide
  e2e/                  live Playwright checks against a running service
  .venv/                Python 3.14 venv (fastapi, uvicorn, playwright, pytest, pytest-asyncio, httpx)
```

Run tests: `cd backend && /home/adyu/Work/work2/projects/personal-projects/relay/.venv/bin/python -m pytest -q`
(use the absolute venv python path).

## 2. Verified environment facts (2026-09-30)

- Host: Python 3.14, podman 5.7, no ttyd, no tailscale, no sqlite3 CLI, Brave at
  `/opt/brave.com/brave/brave` (Chromium-compatible, used by Playwright).
- Containers: Python 3.11, tmux 3.3a, `ps`, `jq`, `claude` 2.1.285 in both, `codex` 0.159.2 in work2 only.
  `LANG=C.UTF-8`, `WORK_ENV=work1|work2`, hostnames `work1`/`work2`.
- Host uid 1000 maps to container uid 1000 (`node`). `/home/adyu/Work/<env>` is mounted at `/home/node`.
  Files that the host writes under the host home appear owned by `node` in the container.
- work1: `slirp4netns`, ports 3000,3001,4321,5173,8000,8080,8787 published to host 127.0.0.1 as 1xxxx.
  work1 cannot reach host loopback. work2: host network, host Docker socket mounted.
- Agent tmux sessions live on `/tmp/tmux-1000/default` in each container. work2 also has many stale
  `overmind-*` sockets: they are development process managers and are not scanned.
- Both containers have sessions named `agent1`, `agent2`, `agent3`, started from the laptop.
  work1 panes run `claude` as a child of `-bash`; work2 agent1/agent2 run `node /home/node/.local/bin/codex`.
- `podman exec` costs about 75 ms. One snapshot exec per env per poll is cheap.
- Terminal bridge findings (tested live):
  - The PTY slave must become the controlling TTY of `podman exec` (`start_new_session=True` plus
    `preexec_fn` doing `ioctl(0, TIOCSCTTY, 0)`), else resizes never reach tmux.
  - When the local `podman exec` client closes or dies, the tmux client inside the container keeps
    running. The bridge must learn the container PID and send it `SIGHUP`. Solution (verified): run
    `sh -c 'printf "\033]7770;relay-client;%s\007" "$$"; exec tmux -u -S "$1" attach-session -t "$2"' relay-attach <socket> <session_id>`,
    parse the marker from the first output bytes, strip it before forwarding, and on close run
    `podman exec <ctr> kill -HUP <pid>`.

## 3. Foundation modules (already written; do not rewrite)

`paths.py`, `config.py`, `db.py`, `podman.py`, `tmux.py`, `bus.py`, `activity.py`, `errors.py`,
`runtime.py`, `screen.py`. Read them before you start. You may add a small helper to a foundation
file with a targeted edit (never recreate the file) and you must list it in your report.

Key APIs:

- `config.load_settings(path=None, **overrides) -> Settings`; `Settings.envs[env_id] -> EnvConfig`.
  `EnvConfig.host_path(container_path)` maps `/home/node/...` to the host path (lexical only).
- `db.Database(path)`: `execute/query/one/tx/insert/update`, `get_session`, `list_sessions`,
  `insert_session`, `update_session(id, **fields)` (sets `updated_at`), `find_live_session_by_pane`.
  `db.now()`, `db.new_id()` (uuid4 hex), `db.dumps/loads` (JSON columns), `row_to_dict`.
- `podman.Podman`: `run(args)`, `exec(env, cmd, input=, timeout=, workdir=, extra_env=)`,
  `exec_args(...)`, `argv(args)`, `inspect(env) -> ContainerInfo(running, id, started_at, network_mode, ports)`.
- `tmux.Tmux(podman, env)`: `snapshot(sockets=None) -> Snapshot` (sessions with panes + process table),
  `new_session(socket, name=, cwd=, argv=, env_vars=, relay_id=) -> (session_id, pane_id, created, server_pid)`,
  `has_session`, `kill_session`, `send_keys(socket, pane_id, *keys)`, `paste_text(socket, pane_id, text, submit=True)`,
  `capture(socket, pane_id, lines=80)`, `attach_argv(socket, session_id)`, `set_session_option`.
  `tmux.detect_kind(snapshot, pane) -> 'claude'|'codex'|'shell'|'other'`. `TmuxSession.target_pane`.
- `bus.Bus.publish(*topics)` with topics `sessions`, `activity`, `envs`, `previews`, `checks`.
- `activity.record(db, kind=, title=, body=, session_id=, env=, attention=None, dedupe_key=, bus=)`.
- `errors.ApiError(status, code, message, **extra)` -> JSON `{"error": code, "message": ..., **extra}`.
- `runtime.Runtime` (on `app.state.rt`): `settings, db, bus, podman, tmux[env_id], env_status[env_id]
  (EnvStatus: container, snapshot, snapshot_at, error, provision), auth, lock_for(key), inventory_lock`.
  Dependencies: `get_rt(request)`, `get_rt_ws(websocket)`, `require_user(request) -> Principal`.
- `screen.permission_dialog_visible(text)`, `screen.claude_yes_highlighted(text)`, `screen.screen_mentions(text, needles)`.

## 4. Module ownership

| Owner | Files |
|---|---|
| sessions | `inventory.py`, `sessions.py`, `routes_sessions.py`, `tests/test_inventory.py`, `tests/test_sessions.py` |
| web | `auth.py`, `app.py`, `terminal.py`, `routes_core.py`, `tests/test_auth.py`, `tests/test_app.py`, `tests/test_terminal.py` |
| adapters | `adapters/__init__.py`, `adapters/claude.py`, `adapters/codex.py`, `adapters/hook_script.py`, `inbox.py`, `provision.py`, `transcript.py`, `hooks_install.py`, `routes_activity.py`, `screen.py` (refine), `tests/test_adapters.py`, `tests/test_inbox.py`, `tests/test_transcript.py`, `tests/test_hook_script.py` |
| previews | `previews.py`, `checks.py`, `routes_previews.py`, `preview_discovery.py`, `preview_gateway.py`, `tests/test_previews.py`, `tests/test_checks.py`, `tests/test_preview_discovery.py`, `tests/test_preview_gateway.py` |
| files | `files.py`, `routes_files.py`, `tests/test_files.py` |
| frontend | everything under `frontend/` |
| ops | `cli.py`, `__main__.py`, `bin/relay`, `deploy/*`, `docs/*`, `README.md`, `e2e/*`, `tests/test_cli.py` |

Each router module exports `router = APIRouter()` with full paths (for example `/api/sessions/{session_id}/end`).
Every protected route uses `Depends(require_user)`. Never import another builder's module at import
time unless this contract names the function; if you need one that is not ready, mock it in tests.

## 5. Data model and state ownership

Tables are in `db.py`. `sessions` columns are written by different modules:

- inventory: `status, tmux_name, container_id, container_started_at, server_pid, tmux_session_id, tmux_created,
  pane_id, pane_pid, pane_count, foreground, activity_at, ended_at, end_reason`, `kind` (discovered rows only),
  and `state` only when (a) `state_source` is NULL or `'terminal'` (fallback state), (b) an agent row's
  target pane shows a shell in the foreground (then `state='exited', state_source='terminal'`), or (c) the
  session ends (`state='exited'`).
- adapters: `state` with `state_source` `'hook'` (Claude), `'notify'` or `'codex_rollout'` (Codex), `state_at`, `last_message`,
  `pending_json`, `agent_session_id`, `transcript_path` (from any Claude hook event, so a session that began
  before the hooks were installed also gets them), and adapter keys inside `meta_json`.
- sessions (create/resume): the initial row: `origin='dashboard', kind, display_name, tmux_name, project_dir,
  initial_prompt, meta_json={"permission_mode": ..., "resumed_from": ...}, agent_session_id` (Claude: the
  dashed form of the relay id, passed as `--session-id`), `status='live', state='unknown'`.

Fallback state (no hook or notify data): shell kind with a shell foreground -> `idle`; shell kind with another
foreground -> `working`; claude/codex with a shell foreground -> `exited`; claude/codex with a pane capture ->
`screen.agent_screen_state` (approval dialog -> `needs_input`, Claude "· done" or Codex "Ask Codex to do
anything" -> `idle`, else `working`); otherwise `unknown`.

Snapshot races: pane captures run before the tmux snapshot, and the merge gets the snapshot start time. A live
row that changed during the snapshot (or a create in progress: no `tmux_session_id` yet, less than 60 s old) is
not ended by that snapshot, and its tmux name is not discovered. A tmux session that relay already has a row
for (by `@relay_id`, else by identity) is never discovered again: a live row is skipped, an `ended_by_user`
row is skipped (the kill is settling), and a row that relay ended by mistake is revived.

`status` values: `live`, `ended`, `interrupted`. End reasons: `ended_by_user`, `closed` (tmux session vanished
while the same server runs), `container_stopped`, `container_restarted`, `server_restarted`.
Identity of a tmux session = `(env, container_started_at, socket_path, server_pid, tmux_session_id, tmux_created)`,
plus the `@relay_id` session option for dashboard sessions. A new tmux session that reuses an old name is a new row.

`pending_json` (adapter-owned):
`{"id": hex, "source": "claude", "tool": "Bash", "summary": "npm run test", "detail": str|null,
  "match": ["npm run test"], "created_at": epoch, "answerable": bool}`

Screen-sourced pending (sessions module, `watch_startup`): after relay launches or resumes an agent, it
watches the pane for up to 45 s. A folder trust dialog (`screen.trust_dialog_visible`, patterns captured
live from Claude 2.1 and Codex 0.159) becomes `{"source": "screen", "tool": "Folder trust",
"answerable": false, ...}` with `state='needs_input', state_source='screen'`. It blocks prompts. It clears
when the dialog disappears or a Claude `SessionStart` hook arrives. Ended rows always clear `pending_json`.

State notes learned live:
- Codex notify reports `agent-turn-complete`. A verified link also lets `codex_runtime` read the exact thread's rollout at the inventory cadence.
- Rollout `task_started` sets `working`. Matching `task_complete` and `turn_aborted` set `idle`.
- Bootstrap restores the latest state without Activity. The incremental cursor reads only new complete lines.
- Title completions settle the matching turn without Activity or `last_message`. Old or conflicting turns do not replace a newer turn.
- `meta_json.codex_binding` holds the exact thread, confined transcript, environment, complete pane identity and provenance.
- Every live Codex pane links from native evidence through the importer's checks (exact Relay row, tmux session, process fingerprints, top-level thread):
  the embedded TUI's thread writer lock, or for a shared-daemon TUI one guarded `/status` per process and then the managed last `thread-id` title field.
- Future launches pass the Relay row id in notify argv. The first completion establishes a verified link.
- A verified Codex row also gets the existing permission screen check. A visible request sets `needs_input` and blocks prompts.
- Ordinary screen idle or busy text does not replace lifecycle state. A cleared request restores the current turn state.
- A `/new` or `/resume` inside a TUI relinks from fresh native evidence after Codex saves the new thread. Relay never guesses by cwd or modification time.
- The existing `interrupt` action sets `idle` and clears `pending_json`.

## 6. HTTP API (all JSON, all under `/api`, all protected except health and login)

Errors: `{"error": code, "message": text}`. Common codes: 400 `invalid_request`, 401 `unauthorized`,
403 `bad_origin` / `forbidden_identity`, 404 `not_found` / `unknown_env`, 409 conflicts, 422 validation
(FastAPI default shape is replaced by `{"error":"invalid_request","message":...,"details":[...]}`),
429 `rate_limited`, 502 `env_command_failed`, 503 `env_unavailable`.

### core (web)
- `GET /api/health` (public) -> `{"ok": true, "version": "0.1.0"}`
- `POST /api/auth/login` (public) `{"token": str}` -> 204 + cookie; 401 `invalid_token`; 429 `rate_limited`
- `POST /api/auth/logout` -> 204, cookie cleared, auth session deleted
- `GET /api/auth/me` -> `{"authenticated": true, "via": "cookie"|"bearer", "tailscale_login": str|null}` (401 when not)
- `WS /api/stream` -> `{"type":"hello","version":...}`, then `{"type":"changed","topics":[...]}` (coalesced 150 ms),
  `{"type":"ping"}` every 25 s. Clients refetch REST data on `changed`. A client `{"type":"ping"}` gets
  `{"type":"pong"}`; the browser sends one every 10 s and replaces a socket with no answer in 8 s, or in
  4 s after the page wakes. uvicorn protocol pings: interval 20 s, timeout 30 s.
- `WS /api/sessions/{id}/terminal?cols=&rows=` (see section 8)

### sessions (sessions)
- `GET /api/state` -> `{"generated_at", "version", "host": {"name"}, "tailscale_hostname": str|null,
  "envs": [EnvView], "sessions": [SessionView], "counts": {"sessions", "working", "needs_input",
  "finished_unread", "by_env": {env: n}}, "activity_unread": n}`.
  `sessions` holds live rows plus ended/interrupted rows from the last 24 h (newest first after live).
- `GET /api/sessions/{id}` -> SessionView
- `POST /api/sessions` `{"env", "kind": "claude"|"codex"|"shell", "name"?: str, "project"?: str,
  "prompt"?: str, "permission_mode"?: "manual"|"acceptEdits"|"auto"|"plan"}` -> 201 SessionView.
  400 `invalid_name` / `invalid_project` / `invalid_kind` / `kind_unavailable` (codex missing in env) /
  `invalid_prompt`; 409 `duplicate_name`; 503 `env_unavailable`.
- `POST /api/sessions/{id}/end` `{"tmux_session_id", "tmux_created", "pane_count"}` -> 200 `{"ended": true, "already": bool}`;
  409 `scope_changed` (identity or pane count differs; body includes current `pane_count`); 409 `not_live`.
- `POST /api/sessions/{id}/prompt` `{"text"}` -> 202 `{"sent": true}`; 409 `approval_pending`, `agent_not_running`,
  `not_live`; 400 `invalid_prompt` (empty or > 8000 chars); 422 `unsupported` (shell/other kinds).
- `POST /api/sessions/{id}/interrupt` -> 202; 409 `agent_not_running` / `not_live`; 422 `unsupported`.
- `POST /api/sessions/{id}/resume` -> 201 SessionView (new row); 409 `not_resumable`.
- `GET /api/envs/{env}/projects` -> `{"root": "/home/node/projects", "projects": [{"name", "path"}]}` (depth 1, dirs only, no dot-dirs, sorted, max 500).

### activity and conversation (adapters)
- `GET /api/activity?limit=50&before_id=&attention=1&unread=1&session_id=` -> `{"items": [ActivityItem], "unread": n}`
- `POST /api/activity/read` `{"ids"?: [int], "session_id"?: str, "all"?: bool}` -> `{"updated": n}`
- `GET /api/sessions/{id}/conversation?limit=200` -> `{"supported": bool, "reason": str|null, "items": [ConvItem], "active_started_at"?: epoch|null, "active_prompt_id"?: str|null, "state_at"?: epoch|null}`
  ConvItem = `{"id", "role": "user"|"assistant"|"tool", "text", "ts": epoch|null, "tool": {"name", "summary"}|null, "phase"?: "commentary"|"final_answer"}`
  Claude and verified Codex sources use the existing authenticated route. The route checks source identity before and after a bounded worker read.
  The phone shows the latest prompt and completed answer. Earlier recent turns stay collapsed. Tool items supply the latest tool summary, not a full tool feed.
  Current-turn evidence identifies the active user message and matches the session state timestamp. Until that evidence matches, the phone shows a prompt placeholder and keeps previous turns collapsed.
  The visible live view refreshes every two seconds while active, and every five seconds otherwise. It pauses when the document or session panel is hidden, and stops on unmount.
- `POST /api/sessions/{id}/approval` `{"request_id", "decision": "allow"|"deny"}` -> 202;
  409 `no_pending_request` / `stale_request` (id differs) / `prompt_not_visible` (screen check failed).

### files (files)
Every path is relative to the env's `projects_root` (`""` is the root). relay reads the host bind mount but
resolves every component in the container path space: each component opens with `O_NOFOLLOW` relative to the
previous directory descriptor, relay reads each symlink itself, a relative target continues from the link's
folder, and an absolute target is a container path that must lie under `projects_root` (never a host path).
`..` above the root, a target outside the root, another filesystem, or more than 40 links -> 403
`outside_root` / 400 `link_loop`. `..`, an absolute path, or NUL in the request -> 400 `invalid_path`.
FIFOs, sockets, and devices -> 400 `not_regular` (listed as `other`, never opened).
- `GET /api/files/{env}/list?path=&hidden=0|1` -> `{"path", "root", "entries": [FileEntry], "truncated"}`
  (5,000 entries max; dirs first; `items` counted up to 1,000 for the first 300 subfolders; a link shows its
  target type, `link_outside`, or `link_broken`).
- `GET /api/files/{env}/search?q=&hidden=` -> `{"query", "results": [{name, path, parent, kind, lang}], "total",
  "limit": 50, "partial", "reason"}`. Names only, case-insensitive, `.git` and `node_modules` skipped, symlinks
  not followed, 60,000 entries or 3 s walk limit (`walk_limit` / `time_limit`), 1,000 matches collected.
- `GET /api/files/{env}/meta?path=` -> FileMeta `{"path", "name", "size", "mtime", "revision", "writable",
  "hard_links", "kind", "lang", "mode": "edit"|"paged"|"download"|"image"|"video", "mime", "active", "reason"?,
  "limits": {"edit", "paged"}}`. `revision` is a hash of (dev, inode, size, mtime_ns, ctime_ns).
- `GET /api/files/{env}/text?path=` -> FileMeta + `{"text", "invalid_utf8", "line_ending": "lf"|"crlf"|"mixed",
  "editable", "edit_blocked"}`; 413 `too_large` over `files_edit_max_bytes` (10,000,000); 415 `not_text`.
- `PUT /api/files/{env}/text?path=&base_revision=` body = raw UTF-8 text -> `{"path", "revision", "size",
  "mtime", "hard_links_detached"}`. 413 over the limit (Content-Length checked before the body is read);
  409 `revision_changed` (body has the current `revision`, `size`, `mtime`; the file is unchanged);
  403 `read_only`; 409 `owner_not_preserved`. The save writes a temp file in the target's folder, fsyncs it,
  copies the owner and mode bits, compares the revision once more, then `os.replace` + folder fsync. A symlink
  keeps pointing at its target. Another hard link keeps the old content.
- `GET /api/files/{env}/page?path=&page=&revision=` -> FileMeta + `{"page", "pages", "start_line", "end_line",
  "total_lines", "continued", "continues", "text", "invalid_utf8"}`. Pages hold at most 200 lines and 256 KiB,
  never split a UTF-8 sequence, and continue a long line on the next page. A different `revision` -> 409
  `revision_changed`. The page index is cached per (env, path, revision), 4 entries.
- `GET|HEAD /api/files/{env}/raw?path=&download=0|1&v=<revision>` streams from the opened descriptor. A `v`
  that is not the current revision -> 412 `revision_changed` (a player never mixes two versions). Inline only for the
  allowlisted image and video types; everything else (SVG, HTML, XML, PDF, unknown) is an attachment with
  `application/octet-stream`. Always `nosniff`, `no-store`, `Cross-Origin-Resource-Policy: same-origin`, and a
  `sandbox` CSP. One `Range` -> 206 + `Content-Range`; an unsatisfiable range -> 416; `If-Range` uses the ETag.

### previews and checks (previews)
- `GET /api/previews` -> `{"previews": [PreviewView], "detected": [DetectedEndpoint], "scan": {"at", "envs":
  {env: {"status": "ok"|"partial"|"error"|"unavailable", "message"}}, "docker": {"status", "message"}}}`.
  Registered rows merge with discovery by endpoint key (`<env>:<laptop port>` or `<env>:c<container port>` for an
  unpublished Work 1 port). The response also has `app_choices`, grouped by verified project and workspace.
  The page shows one selected or clearly suggested app per group. `POST /api/previews/scan` forces a rescan
  (at most one per 2 s; a normal list reuses a scan for 10 s).
- `preview_app_choices` stores one choice per `(env, project_dir)`. `identity_json` holds the verified candidate identity; the snapshot is display only.
- `PUT /api/previews/app-choice` accepts `{env, project_dir, candidate}`. Relay recomputes ownership and accepts no URL.
- `DELETE /api/previews/app-choice` accepts `{env, project_dir}` and clears the choice.
- A missing selected app stays Stopped. A removed registered preview sets the nullable foreign key to null and keeps its identity and snapshot.
- Details retains start, stop, check, test, logs, remove and registration controls. Unknown servers stay under Add app.
- Open app uses only a fresh, verified existing gateway. An app choice creates no network route.
- Discovery (`preview_discovery.py`): one `podman exec python3 -c <static script>` per env reads the LISTEN
  sockets, maps socket inodes to processes in the container's PID namespace, and sends `HEAD /` (then TLS) to at
  most 32 owned ports in 6 s. A socket that no container process owns is not listed (Work 2 shares the host
  network). Docker: `GET /containers/json` on `docker_socket`; a container joins an env only through a Compose
  `working_dir` label or bind source that exists under that env's host projects folder, or a registered preview
  on the same laptop port. Only HTTP or HTTPS answers count.
- `POST /api/previews` -> 201 PreviewView;
  `PATCH /api/previews/{id}`; `DELETE /api/previews/{id}` (409 `running` while relay manages a live server)
- `POST /api/previews/{id}/start` -> 202; 409 `port_in_use` (`{"managed": bool}`) / `already_running`
- `POST /api/previews/{id}/stop` -> 200; 409 `not_managed`
- `GET /api/previews/{id}/logs?lines=200` -> `{"text"}`
- `POST /api/previews/{id}/check` `{"viewports"?: ["phone","desktop"]}` -> 202 CheckRunView (status `running`)
- `POST /api/previews/{id}/test` -> 202 CheckRunView (409 `no_test_command`)
- `GET /api/checks?preview_id=&limit=20` -> `{"runs": [CheckRunView]}`; `GET /api/checks/{run_id}` -> CheckRunView;
  `GET /api/checks/{run_id}/artifacts/{name}` -> file (`name` must match `^[a-z0-9_-]{1,40}\.(png|json|txt)$`)

### View shapes

EnvView: `{"id", "label", "container", "available": bool, "status": "running"|"stopped"|"missing"|"error",
"network": str|null, "projects_root", "error": str|null, "snapshot_at": epoch|null,
"agents": {"claude": bool, "codex": bool}, "provisioned": bool}`

SessionView:
```
{"id", "env", "env_label", "name", "tmux_name", "kind", "origin", "status", "state", "state_source",
 "state_at", "project_dir", "project", "last_message", "activity_at", "foreground",
 "pending": pending_json|null, "agent_session_id", "created_at", "ended_at", "end_reason",
 "tmux": {"session_id", "created", "pane_id", "pane_count", "socket", "server_pid"},
 "capabilities": {"terminal", "prompt", "approve", "interrupt", "conversation", "resume", "end"},
 "notes": [str]}
```
Capabilities: terminal = live; prompt = live and kind in (claude, codex) and agent foreground;
approve = kind claude and pending present and pending.answerable; interrupt = live and kind in (claude, codex)
and agent foreground; conversation = supported Claude source or exact verified Codex source; resume = not live and kind in
(claude, codex) and agent_session_id; end = live. `notes` explains unavailable capabilities in one short
sentence each (for example "Status comes from the terminal only. Start agents from relay for live status.").

PreviewView: `{"id", "env", "name", "project_dir", "command", "port", "host_port": int|null, "health_path",
"test_command", "listening": bool, "server": {"status", "started_at", "ready_at", "error"}|null,
"urls": {"local": str|null, "tailnet": str|null}, "last_check": CheckRunView|null,
"source": "managed"|"detected"|null, "detected": DetectedEndpoint|null, "phone": PhoneView|null}`.
`urls.tailnet` is the verified gateway URL only; relay never generates a tailnet URL from the hostname.

DetectedEndpoint: `{"key", "env", "port", "host_port", "reachable", "network": "host"|"published"|"unpublished"|"docker",
"protocol", "source": "process"|"docker", "process": {"pid", "name", "tool", "started_at"}, "project", "folder",
"docker": {"name", "image", "container_port", "link": "compose"|"mount"|"registered"}|null, "phone"}`.

PhoneView: `{"status": "verified"|"not_set_up"|"failed"|"blocked", "url", "gateway_port", "target",
"checked_at", "message"}`. `verified` needs a route proof younger than 60 s (see section 11).

Check target: the verified gateway URL when a fresh proof exists (relay proves it again before a check), else
the reachable local URL. A check through a gateway gets its own auth session (`checks.start_check_session`),
deleted after the run; a local URL gets no relay cookie. The gateway check runs with service workers blocked and
an `OriginGuard` installed before the cookie: only the exact gateway origin continues, only ws/wss to the same
host and port connects, everything else is aborted or never connected. The gateway refuses a cross-origin
redirect for a check session (502), because Playwright does not route a redirect's next hop. An attempt on the
gateway host (another port or scheme) or a final page elsewhere fails the check; a blocked third-party host is a
warning.

CheckRunView: `{"id", "preview_id", "kind": "smoke"|"test_command", "status": "running"|"passed"|"failed"|"error",
"target_url", "summary", "started_at", "finished_at", "result": object|null, "artifacts": [{"name", "url"}]}`

## 7. Agent launch (sessions module)

All launches: `tmux.new_session(env.tmux_sockets[0], name=<tmux name>, cwd=<validated project dir>,
argv=["bash", "-lc", <script>, "relay-launch", *agent_argv],
env_vars={"RELAY_SESSION_ID": relay_id, "RELAY_ENV": env.id}, relay_id=relay_id)`.

- `<script>` for a shell: `'umask 077; exec "$@"'`.
- `<script>` for claude/codex (`_AGENT_LAUNCH_SCRIPT`): `set -m` (the agent keeps its own foreground process
  group, so tmux still reports the agent as the pane command), run the agent as a child, then on a non-zero
  exit print `[relay] <agent> exited with code <n>. The terminal stays open.` and `exec bash -l`. Exit 0 ends
  the tmux session as before. The startup watch finds that line (`screen.launch_failure`), sets
  `state='exited'`, and records an `error` activity "<name> could not start" with the error line.

- claude: `["claude", "--session-id", <uuid.UUID(relay_id)>, "--name", <name>, "--settings", env.claude_settings,
  ("--permission-mode", mode)?, ("--", prompt)?]`
- codex: `["codex", "-c", provision.codex_notify_override(env, relay_id), ("--", prompt)?]`
- shell: `["bash", "-l"]`
- resume claude: `["claude", "--resume", agent_session_id, "--name", name, "--settings", env.claude_settings]`
- resume codex: `["codex", "resume", agent_session_id, "-c", provision.codex_notify_override(env, relay_id)]`

Names: `^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$`, unique (case-insensitive) among live sessions of the env
(check DB rows and the fresh snapshot). Empty name -> smallest free `agentN`. Project: `""` means
`projects_root`; otherwise a relative path without `..`, resolved in the container with
`realpath -e -- <root>/<project>` and required to equal the root or start with `root + "/"`, and be a directory.
Prompt text is only ever an argv element or tmux paste input. It is never part of a shell string.

## 8. Terminal WebSocket (web)

`/api/sessions/{id}/terminal?cols=120&rows=40`: authenticate (cookie or bearer) and check Origin before
`accept()`; an unauthenticated client never completes the handshake (4401, 4403). An authenticated client
is accepted first, then closed with 4404 unknown session or 4409 not live, so the browser receives the
code and stops reconnecting (a close before accept arrives as 1006). 1011 means a bridge error.
The session must be live and its tmux identity must still exist (`has_session`).

- server -> client: binary frames with raw terminal bytes; text frames `{"type":"ready"}`,
  `{"type":"exit","reason":"ended"|"detached"|"error","message"}`, `{"type":"pong"}`,
  `{"type":"scroll","active":bool,"position":int,"history":int}` (active only with real older normal-screen output),
  and bounded `history` frames with `epoch`, `status`, `items`, `before`, `more` and `note`.
- client -> server: text `{"type":"input","data":str}` (max 64 KiB), `{"type":"resize","cols","rows"}`
  (cols 20..500, rows 5..200), `{"type":"ping"}` (answered with a pong); binary frames are raw input.
- History: `{"type":"scroll","lines":n,"epoch":int}` requests older or newer output. Back to live sends `to: bottom` with the epoch.
- Normal-screen history uses the real tmux copy position. Alternate-screen agent history uses the session's exact verified transcript inside the Terminal tab.
- `{"type":"history","epoch":int,"before":int}` requests the next older source page. Reads use byte offsets and at most a 4 MiB window.
- Each page checks the live row and transcript identity before and after its worker read. Codex also requires the verified environment and complete pane binding.
- React renders plain text only. The view keeps at most 128 KiB of text and shows a note when it removes text.
- The live PTY stays active behind history. Key input or Back to live closes history first. Epoch checks discard late pages.
- No global tmux option changes. A bridge that used copy mode cancels it on close.
- The bridge uses the verified marker wrapper from section 2, records the client in `terminal_clients`,
  and on close sends `kill -HUP <pid>` in the container, terminates the local podman process, closes the fd,
  and deletes the row. At startup it cleans every leftover row the same way (only kill a PID whose
  `/proc/<pid>/cmdline` contains `attach-session`).

## 9. Hooks and events (adapters)

Provisioning writes, on the host under each env's `host_home`:
`.local/share/relay/hook.py` (0700), `.local/share/relay/claude-settings.json` (0600), `.local/state/relay/inbox/` (0700).
The hook script appends one JSON file per event to the inbox (tmp file + rename). The host polls the inbox
every second, processes files in name order, and deletes them. No network path from containers to the host
is needed, so work1 (slirp) and work2 (host network) behave the same.

Event file: `{"v":1, "source":"claude"|"codex", "ts": epoch, "relay_session_id": str|null,
"relay_env": str|null, "tmux": "$TMUX"|null, "tmux_pane": "%N"|null, "payload": {...trimmed...}}`.
The env comes from the inbox location, never from the payload.

relay never edits the global `~/.claude/settings.json` or `~/.codex/config.toml` in a work environment
automatically. Dashboard-launched Claude gets hooks through `--settings`; Codex gets `notify` through `-c`.
`relay hooks install` is an explicit opt-in for laptop-started sessions.

## 10. Security rules (all modules)

- Loopback bind only. Token auth (cookie session or bearer) on every route except health and login.
- Cookie: `__Host-relay_session` over https (Secure, Path=/, no Domain), `relay_session` over local http.
  HttpOnly and SameSite=Strict in both cases. A request reads only the name for its own scheme.
- State-changing requests with cookie auth need an allowed `Origin` (`Settings.allowed_origins`).
  WebSockets need an allowed Origin too. Requests with an unknown `Host` header are rejected (DNS rebinding).
- A cookie request with `Sec-Fetch-Site: same-site` or `cross-site` is refused (403 `bad_origin`): a preview
  app on another port of the same host is same-site. Every response has `Cross-Origin-Resource-Policy:
  same-origin`. A preview gateway origin is never added to `allowed_origins`.
- No host shell: argv lists only. No user text in `sh -c` script bodies (pass as positional args).
- Validate every id against its pattern (`$N`, `%N`, hex ids). Confine every file path (realpath under the
  expected root; reject symlinks for inbox files).
- Never log tokens, cookie values, prompts, or transcript text.
- Tests must not touch the real `default` tmux sockets, must not start `claude` or `codex`, and must not write
  into `/home/adyu/Work/*`. Live tests (marked `@pytest.mark.live`) may use the socket `/tmp/tmux-1000/relay-test`
  in `job-work2` and must clean up.

## 11. Trusted preview gateways (previews)

`[[previews.gateways]]` in the config: `listen_port`, `env`, `target_port` (the app's laptop loopback port), and
an optional https `public_origin`. Each gateway is a separate uvicorn listener in the relay process on
`127.0.0.1:<listen_port>`, started in the app lifespan (also with `background=False`) and stopped at shutdown.
Config validation refuses relay's own port, duplicate listeners, a target that is another listener, and a
`public_origin` that is not https or is one of relay's own origins.

Per request the gateway checks the Host (loopback or the public host, with its port) and that the request target
starts with `/` (it builds the upstream URL itself), answers
`/.well-known/relay-gateway-proof` itself, requires a valid relay session cookie (the scheme's name) and an
allowed Tailscale identity (401 page / 403), requires its own Origin on a state-changing request or a WebSocket,
and forwards to `127.0.0.1:<target_port>` only. It removes the relay cookies from `Cookie`, and drops
`Authorization`, `Proxy-Authorization`, `Forwarded`, `X-Forwarded-*`, `X-Real-IP`, and `Tailscale-*`. It sets
`Host` and a same-origin `Origin`/`Referer` to the upstream origin. In the response it drops hop-by-hop headers
and any `Set-Cookie` for a relay cookie name, and maps an absolute redirect to the upstream back to its own
origin. Bodies stream both ways (`aiter_raw`, no decoding). WebSockets keep the subprotocol and relay text and
binary messages. Gateway connections write no access log lines, so app paths and query strings stay out of
relay's log.

Route proof: relay requests `<public_origin>/.well-known/relay-gateway-proof?nonce=<32 hex>`; the gateway
answers HMAC-SHA256(process secret, `relay-gateway:<listen_port>:<nonce>`). For an https origin relay also
reads `tailscale serve status --json` and requires an HTTPS listener on the public port (`TCP["<port>"].HTTPS`
exactly true; 443 when the URL has no port) and exactly one handler for the public host and port, `/` to
`http://127.0.0.1:<listen_port>`, with Funnel off. Both together prove that every path of the public route ends
at this gateway now. Proofs are cached 30 s and count as fresh for 60 s (the card also drops the link after
60 s). relay never creates or changes a Tailscale route. A check session never gets a cross-origin redirect.
