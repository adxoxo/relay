# relay

relay is a private dashboard for agent sessions. The sessions run in tmux
inside two Podman work containers (`job-work1` and `job-work2`) on an Ubuntu
laptop. You watch them, send prompts, manage sessions, and test projects from
a phone or a browser.

The service listens on `127.0.0.1:8765`. You reach it from a phone through
Tailscale Serve. Access needs one secret token.

## What relay does

- Lists every agent and shell session in both work containers.
- Shows session state (working, needs input, finished).
- Opens a live terminal to any session in the browser.
- Creates, resumes, and ends sessions.
- Sends prompts and approves tool requests for Claude sessions.
- Browses project files in both workspaces: Markdown and code views, explicit
  edits with revision checks, a paged view for large text, images, and video.
- Lists web servers in the workspaces, runs preview servers and browser checks,
  and gives an approved app a trusted gateway for phone access.

## Layout

```
backend/relay/   Python package (FastAPI app, run from backend/)
backend/tests/   pytest suite
frontend/        React + TypeScript + Vite (built into frontend/dist)
bin/relay        CLI wrapper
deploy/          systemd user unit templates
docs/            operating and security guides
e2e/             live Playwright check against a running service
```

## Quick start

1. Build the frontend.

   ```
   cd frontend
   npm install
   npm run build
   ```

2. Run the tests.

   ```
   cd backend
   /home/adyu/Work/work2/projects/personal-projects/relay/.venv/bin/python -m pytest -q
   ```

3. Start the service.

   ```
   bin/relay serve
   ```

4. Print the login URL and open it on the laptop or a phone.

   ```
   bin/relay url            # local URL with the token
   bin/relay url --tailnet  # Tailscale https URL (after Tailscale setup)
   ```

5. Install the background service so relay starts on login.

   ```
   bin/relay service install
   ```

## Common commands

```
bin/relay ls                     # list all sessions
bin/relay ls --env work1 --json  # one environment as JSON
bin/relay new work1 shell --name scratch --attach
bin/relay attach work1 scratch   # attach a terminal to a session
bin/relay end work1 scratch      # end a session (asks to confirm)
bin/relay token                  # print the access token
bin/relay token --rotate         # rotate the token (drops all logins)
bin/relay doctor                 # check the environment and the service
```

## Links

- Operating guide: `docs/OPERATIONS.md`
- Security model: `docs/SECURITY.md`
- Module contract: `ARCHITECTURE.md`
