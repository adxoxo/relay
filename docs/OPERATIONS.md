# relay operations

This guide covers day to day operation. It uses short sentences. It uses the
`bin/relay` wrapper for CLI commands.

## Start, stop, status

After `bin/relay service install`, relay runs as a systemd user service.

```
systemctl --user start relay.service
systemctl --user stop relay.service
systemctl --user restart relay.service
systemctl --user status relay.service
```

To run relay in the foreground for a quick test, use `bin/relay serve`.

## Logs

Follow the service log with journalctl.

```
journalctl --user -u relay -f
```

## Login on a phone

1. Connect the phone to the tailnet with the Tailscale app.
2. On the laptop, print the URL.

   ```
   bin/relay url --tailnet
   ```

3. Open that URL on the phone. The token is in the URL fragment. relay logs
   in once and clears the fragment.
4. Save a browser bookmark for later. The session cookie keeps you logged in.

If you have no Tailscale yet, use the local URL on the laptop browser.

```
bin/relay url
```

## Tailscale setup (needs Adam's approval)

These steps install software and open remote access. Ask Adam first.

1. Install Tailscale.

   ```
   curl -fsSL https://tailscale.com/install.sh | sh
   ```

2. Bring the tailnet up.

   ```
   sudo tailscale up
   ```

3. Serve the relay port over https on the tailnet.

   ```
   tailscale serve --bg --https=443 http://127.0.0.1:8765
   ```

4. Tell relay its public origin and hostname. Edit
   `~/.config/relay/config.toml`.

   ```
   [server]
   public_origins = ["https://laptop.example.ts.net"]
   tailscale_hostname = "laptop.example.ts.net"
   ```

5. Restart relay.

   ```
   systemctl --user restart relay.service
   ```

Use your real tailnet hostname in place of `laptop.example.ts.net`. Run
`tailscale status` to find it.

## Files

The Files page browses the projects folder of each workspace. It edits text up
to 10,000,000 bytes and pages text up to 50,000,000 bytes. Larger files and
binary files are download only. To change the limits, edit
`~/.config/relay/config.toml` and restart relay.

```
[files]
edit_max_bytes = 10000000
paged_max_bytes = 50000000
```

A save replaces the file atomically. Another hard link to the file keeps the
old content.

## Previews and discovery

The Previews page shows one app per project and workspace. A saved choice wins.
Choose or Change app selects the app. A missing saved app stays Stopped until you change or forget the choice.
Details holds the server controls. Add app holds other servers and registration.
Use Rescan for a fresh scan. Open app requires a fresh verified phone gateway.
An app choice creates no route. Set up phone access explains the existing gateway steps.
Docker discovery reads `/run/docker.sock`. Turn it off with:

```
[previews]
docker_socket = ""
```

## Phone access to a preview (needs Adam's approval)

Never serve a project port directly over Tailscale, and never add a preview
origin to `server.public_origins`. The relay cookie would reach the project
app. Use a trusted gateway instead. These are two separate steps.

1. Configure the gateway and restart relay. This step opens no remote access.

   ```
   [[previews.gateways]]
   listen_port = 18501          # the gateway inside relay, on 127.0.0.1
   env = "work1"
   target_port = 15173          # the app's laptop port (Work 1 5173 -> 15173)
   public_origin = "https://laptop.example.ts.net:18501"
   ```

   ```
   systemctl --user restart relay.service
   ```

   The Previews card keeps Open app disabled until Relay verifies the phone route.

2. After approval, serve the gateway port (not the app port) on the tailnet.

   ```
   tailscale serve --bg --https=18501 http://127.0.0.1:18501
   ```

   Rescan the Previews page. The card enables Open app only after Relay proves that the public URL reaches this gateway, and
   `tailscale serve status --json` shows one handler for the port: `/` to the
   gateway. Do not add path handlers or Funnel for a gateway port.

   Open the app from the relay page. A bookmark opened from another app arrives
   without the SameSite=Strict cookie and shows the gateway login page.

To remove phone access, run `tailscale serve --https=18501 off`, then delete
the gateway block and restart relay.

## Optional global hooks

relay never edits the global `~/.claude/settings.json` or
`~/.codex/config.toml` on its own. Sessions started from relay get their hooks
through `--settings` (Claude) and `-c` (Codex).

To get live status for Claude sessions that you start by hand on the laptop,
install the hooks once per environment. This is opt in.

```
bin/relay hooks install --env work1
bin/relay hooks status --env work1
bin/relay hooks uninstall --env work1
```

The install edits that environment's `~/.claude/settings.json` and keeps a
backup. Use `--dry-run` to preview the change.

## Recovery after a reboot

A reboot stops the containers, so it stops every tmux session. relay marks
those sessions `interrupted`.

- Agent sessions that relay started keep their agent session id. Use the
  Resume action in the UI (or start again) to continue a Claude or Codex
  session where it stopped.
- Shell sessions and by-hand sessions cannot resume. Start a new one.

If you installed `relay-containers.service` and linger is on, the containers
start again on login. Otherwise start them by hand.

```
podman start job-work1 job-work2
```

## What survives a relay restart

- The token and all browser logins survive (they are on disk).
- The session database survives.
- Live tmux sessions in the containers survive. relay reconnects to them.
- Open browser terminals disconnect and reconnect on their own.
- A managed preview server keeps running; relay picks it up again.

A container stop or a reboot does not survive. It ends the tmux sessions.

## Updates

```
git pull
cd frontend && npm run build
systemctl --user restart relay.service
```

The service serves `frontend/dist`. To review a build first, build into
another folder (`npm run build -- --outDir /tmp/relay-build --emptyOutDir`).

After the update that adds Files and the preview gateway, a browser on https
must log in once more: the https cookie is now `__Host-relay_session`.

Run the tests after an update.

```
cd backend
/home/adyu/Work/work2/projects/personal-projects/relay/.venv/bin/python -m pytest -q
```

## Uninstall

```
bin/relay service uninstall
```

This disables and removes the user units. It leaves the token, the database,
and the code in place. Remove `~/.local/state/relay` to drop the state too.

## Troubleshooting

- VS Code Snap terminal cannot reach podman. The Snap launcher redirects
  `XDG_DATA_HOME`. relay restores it from `XDG_DATA_HOME_VSCODE_SNAP_ORIG`.
  Run relay from a normal terminal, or export the original value first.
- A stale tmux client keeps a session attached. relay sends `SIGHUP` to its
  own attach client on disconnect. If a by-hand client is stuck, detach it
  inside the container with `tmux -S <socket> detach-client`.
- An environment shows unavailable. Check the container.

  ```
  podman ps
  bin/relay doctor
  ```

  Start a stopped container with `podman start job-work1` (or `job-work2`).
- The service is unreachable. Check the log with
  `journalctl --user -u relay -f` and confirm the port with `bin/relay doctor`.
- A new session shows "Needs input: Folder trust". Claude and Codex ask once
  whether to trust a project folder they have not seen. relay cannot answer
  that question for you. Open the Terminal tab, answer it there, and the
  request clears by itself. relay blocks prompts until then, so a prompt can
  never be typed into the trust dialog.
- Discovered sessions (started on the laptop) show status "Unknown". relay gets
  live status only from agents that it starts, or from sessions after
  `bin/relay hooks install`. The terminal always works.
- A new Claude session in Work 1 shows "could not start" at once. Work 1
  exports `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` in `~/.bashrc`, and bubblewrap
  is not installed in the container. Claude then exits with "bubblewrap is
  required". relay keeps the tmux session open with a shell and the error.
  Install bubblewrap in the container, or set the variable to 0 (this removes
  subprocess isolation). Your `cr` alias already sets it to 0.
- Office: open Agents, then choose Office beside List. Relay remembers this choice.
  Each workspace has its own floor. Phones show one floor at a time.
  Choose a desk or row to open that exact session. Back to Office restores its floor and desk page.
  Pause motion stops the artwork. Reduced motion, hidden tabs and offscreen floors also stop continuous animation.
  An offline floor keeps its last-known agent identities. An expired snapshot shows Updates paused.
  The office does not send agent commands or answer permission requests.
- Terminal history: use a swipe or the mouse wheel to read older output.
  Normal shell output uses tmux history. Agent alternate screens use the verified saved transcript in the same Terminal tab.
  Use Older for earlier transcript pages. Tap Back to live, or type a key, to return.
  The live terminal continues behind the history view. A missing or stale link shows a note.
- Latest turn: Conversation shows the latest saved prompt and completed answer for Claude and verified Codex threads.
  Earlier recent turns stay collapsed. Open a turn to read it. Use Terminal for older output and permission requests.
  The visible page refreshes every two seconds during a turn, and every five seconds while idle.
  Refresh pauses when the page is hidden. A missing or stale Codex link must not expose an unrelated transcript.
- Codex status: linked threads use their rollout lifecycle records at the inventory cadence, about three seconds.
  A permission request uses the existing pane screen check. Answer it in the Terminal.
  Relay links every live Codex pane by itself from native evidence (`codex_runtime._try_native_link`, every 10 s per pane):
  the thread writer lock of an embedded TUI process (every relay launch), or for a shared-daemon TUI one guarded `/status`
  per process (idle screen, empty composer, never twice: an attempt marker in the row meta survives a restart)
  and then the managed last `thread-id` terminal title field.
  Startup provisioning appends that field to `[tui] terminal_title` in each env's `~/.codex/config.toml` (backup kept,
  user items kept; `relay hooks uninstall` restores the user's lines). A running TUI adopts it at its next `/new` or `/resume`.
  `/new` and `/resume` then relink automatically once Codex saves the thread. A title is trusted only after Relay sees
  the live process write it, because tmux keeps an old process's title.
  The manual importer stays available: `PYTHONPATH=backend .venv/bin/python -m relay.codex_runtime bind --evidence FILE --apply`.
  Keep native evidence private. Never reuse stale evidence or guess a thread from its folder.
  A missing process in a failed or incomplete snapshot does not prove an exit.
  Relay probes the original process before an exit invalidation. A probe failure leaves the identity uncertain.
  Relay can recover a false exit invalidation only with the original PID, start time, boot, terminal, pane, thread, and file proof.
  A real process exit or replacement still needs fresh evidence. Relay never substitutes another process or transcript.
- "Reconnecting" after the phone wakes: the page probes both sockets and
  replaces a dead one within about 4 s. A frozen link without a wake is found
  within about 18 s.

## Notes for operators

- relay runs podman with `--events-backend=none`. Its poll would otherwise
  write two journal lines per `podman exec`, about 115,000 lines a day.
- The containers unit (`relay-containers.service`) is not installed while
  linger is off. Without linger the user manager stops at logout, and the
  unit would stop the containers. Turn on linger first if you want it:
  `sudo loginctl enable-linger adyu`, then
  `bin/relay service install --containers`.
- The token never starts with `-` or `_`, so `--token "$(cat token)"` works on
  a command line. Rotate it with `bin/relay token --rotate`. Rotation signs
  out every browser.
