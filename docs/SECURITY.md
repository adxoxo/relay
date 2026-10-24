# relay security

relay is a single user private dashboard. It gives full terminal access to two
work containers. This document states the threat model, the controls, and the
residual risks. Read it before you expose relay to a network.

## Threat model

relay assumes one trusted user (Adam) on one trusted laptop and one trusted
tailnet. The main threats are:

- A device on the local network reaches the service port.
- A malicious web page in the laptop browser tries a cross site request.
- A DNS rebinding attack points a hostname at the loopback service.
- A leak of the state database.
- A hostile agent writes crafted event files to the inbox.
- A project file or a symlink tries to lead relay outside a workspace.
- A preview app, on another port of the same host, tries to read or use the
  relay login.

relay does not defend against a fully compromised laptop account. A user who
can run commands as `adyu` already has the same access relay offers.

## Controls

### Loopback bind

The service binds `127.0.0.1` only. The config refuses any non loopback host.
Remote access comes through Tailscale Serve, which proxies from the tailnet to
loopback. No relay port is open on the LAN.

### Token and cookie

- One secret token grants access. It lives at the token path with mode 0600.
- A browser exchanges the token once at `/api/auth/login` for a session
  cookie. Scripts send the token as a bearer credential.
- The cookie value is random. The database stores only its SHA-256 digest, so
  a database leak yields no usable cookie.
- The cookie is HttpOnly, SameSite=Strict, and Path=/.
- Over https the cookie is `__Host-relay_session`. It is Secure and has no
  Domain. A page on the same host (a preview app on another port) cannot set
  a narrower copy, and HttpOnly stops it from overwriting the cookie. An https
  request reads only this name.
- Over local http (the laptop at 127.0.0.1) the cookie is `relay_session`,
  because a browser refuses a `__Host-` cookie without Secure.
- An https login also expires an old plain `relay_session` cookie from before
  the `__Host-` prefix.
- Failed logins are rate limited per client address.
- `relay token --rotate` makes a new token and drops every browser login.

### Origin and Host checks

- A state changing request with cookie auth needs an allowed `Origin`. The
  comparison is exact. This blocks cross site requests from another web page.
- WebSocket handshakes need an allowed `Origin` too.
- A request with an unknown `Host` header is rejected. This blocks DNS
  rebinding, where an attacker points a hostname at `127.0.0.1`.
- A bearer request does not need an Origin, because a browser never attaches
  the `Authorization` header on its own.
- A cookie request that the browser marks `Sec-Fetch-Site: same-site` or
  `cross-site` is refused. A preview app on another port is same-site, so its
  page cannot use the relay cookie even for a read.
- Every response has `Cross-Origin-Resource-Policy: same-origin`, so another
  origin cannot embed relay media or JSON.

### Tailscale identity header (defense in depth)

When `tailscale_logins` is set, relay checks the `Tailscale-User-Login` header
that Tailscale Serve injects. A request from an identity that is not on the
list is rejected. This is a second layer behind the token, not a replacement.

### Argv only execution

- relay never uses a host shell. Every subprocess uses an argument vector.
- User text (session names, project paths, prompts) is never part of a shell
  string. A prompt is an argv element or tmux paste input only.
- In-container scripts take user values as positional arguments, never inside
  the script body.
- Every id is validated against its pattern (session `$N`, pane `%N`, hex
  ids). Every file path is confined under its expected root with a real path
  check, and inbox files reject symlinks.

### Inbox trust boundary

Agents in a container report events by writing one JSON file per event to an
inbox directory under the bind mounted home. The host polls the inbox and
deletes each file. The environment of an event comes from the inbox location,
never from the file content. There is no network path from a container to the
host, so work1 (slirp) and work2 (host network) behave the same. relay treats
event content as untrusted and trims it.

### Terminal access equals shell access

A live terminal in the browser is a real shell inside a container. Anyone who
holds the token has full shell access to both work containers. Treat the token
like an SSH key.

### work2 Docker socket

work2 mounts the host Docker socket. A shell in work2 can control host Docker.
That makes a work2 terminal effectively host root. Guard the token
accordingly. Do not share a work2 terminal with anyone you would not give root
on the laptop.

## Files

- Every Files route needs the relay login. A save also needs an allowed Origin.
- A path is relative to the projects folder of one workspace. `..`, an absolute
  path, and NUL are refused. Work 1 and Work 2 have separate roots.
- relay resolves every path in the container path space. It opens each folder
  with `O_NOFOLLOW` relative to the previous folder, and reads each symlink
  itself. An absolute link target is a container path. relay never reads it as
  a host path. A target outside the projects folder, another filesystem, or a
  loop is refused. A folder that changes between the check and the open is
  detected by its inode and refused.
- Only regular files open. A FIFO, a socket, or a device is never read.
- A save sends the raw text with a base revision. A stale revision gets 409 and
  the file stays unchanged. relay writes a temp file, flushes it, keeps the
  owner and mode bits, checks the revision again, and renames it over the file.
- Images and videos show inline only for an allowlisted media type. SVG, HTML,
  XML, PDF, and unknown files are attachments with a sandbox CSP. They never run
  in the relay origin.
- A media URL names the revision that the page shows (`?v=`). When the file
  changed, the route answers 412, so a player never mixes bytes from two
  versions. The page loads the new version at its next change check.
- The Files page applies a response only to the workspace, file, and request
  that started it. A late answer for another file or workspace is dropped.

## Preview gateway

A project app that Tailscale serves on another port of the relay host name
would receive the relay cookie, because a cookie has no port scope. relay
therefore never serves a project port directly. Each approved app gets a
gateway listener inside relay on 127.0.0.1.

- The gateway accepts only its own Host values.
- It requires a valid relay session and an allowed Tailscale identity before
  it forwards anything.
- It forwards to one allowlisted loopback endpoint only.
- It removes the relay cookies, `Authorization`, and the forwarded identity
  headers (`Tailscale-*`, `X-Forwarded-*`, `Forwarded`, `X-Real-IP`).
- It drops a `Set-Cookie` for a relay cookie name from the app.
- It requires its own Origin for a state-changing request or a WebSocket, so
  another app's page cannot use it with the shared cookie.
- Its origin is never in relay's allowed origins. A project page cannot make a
  state-changing call or open a WebSocket to the relay API.
- The gateway refuses a request target that does not start with `/`. It builds
  the upstream URL itself, so the upstream host never changes.
- relay shows a phone link only after a route proof less than 60 s old. Two
  conditions must hold:
  - The public URL answers an HMAC challenge that only this relay process can
    answer.
  - `tailscale serve status --json` (read only) shows an HTTPS listener on the
    public port (`TCP.<port>.HTTPS` is exactly true; port 443 when the public URL
    has no port) and exactly one handler for the public host and port: `/` to
    this gateway's listen port. Funnel must be off for that port. If relay cannot
    read the status, the proof fails.
  The second condition stops a split route (one path to the gateway, the rest
  to the app) and a direct app route that forwards only the proof path.
- relay never creates or changes a Tailscale route.

## Browser checks

A check through a gateway uses its own short relay session, deleted after the
run. A check of a direct local URL never gets a relay cookie.

- The check browser blocks service workers.
- Before relay adds the cookie, a guard routes every request and WebSocket of
  the browser context. Only the exact gateway origin continues. Only `ws` or
  `wss` to the same host and port connects, so HMR keeps working. Every other
  request is aborted, and every other WebSocket is never connected.
- Playwright does not route the next hop of a redirect that the browser
  follows. The gateway therefore refuses, for a check session only, any
  redirect to another origin (502). Same-origin redirects still work.
- An attempt on the gateway's own host (another port or scheme) fails the
  check. A blocked third-party host (for example a font CDN) is a warning,
  because that host never gets the host-only cookie.
- The final page must stay on the gateway origin.

## Discovery

- Discovery runs one static script per container. It reads the container's
  socket and process tables and sends one `HEAD /` (then TLS) to each owned
  loopback listener. It never scans a port range and never probes another host.
- A socket that no process inside the container owns is not listed. In Work 2
  that excludes relay itself and other laptop services.
- Docker is read with a list call only. relay probes a Docker port only for a
  container with a verified workspace link or a registered preview.
- Start, Stop, and Remove stay limited to registered previews. relay never
  stops a process that it did not start.

## Logging

relay never logs tokens, cookie values, prompts, or transcript text. Log
output stays free of session content.

## Residual risks

- A compromised laptop account bypasses relay entirely. relay adds no
  protection there.
- The token grants full control. A token leak is a full compromise of both
  containers, and through work2 the host Docker socket. Rotate on any doubt.
- Tailscale Serve is trusted to inject a truthful identity header. A
  misconfigured serve chain could drop or forge it. The token is still
  required, so identity is defense in depth only.
- An agent inside a container can write large or malformed inbox files. relay
  trims and validates them, but a flood could slow the poll loop.
- A managed preview server runs project code on a laptop port. Treat preview
  code with the same trust as any code you run locally.
- Anyone with the relay login can use a configured gateway, and the app behind
  it sees the requests. The gateway adds no app-level access control.
- On local http, a page on another 127.0.0.1 port can set a narrower plain
  `relay_session` cookie. relay ignores an invalid value, so the effect is a
  failed request. The `__Host-` prefix closes this on https.
- On local http, a cookie for `127.0.0.1` reaches every `http://127.0.0.1:<port>`
  app that the same laptop browser opens. This existed before Files and
  Previews. The "Open on laptop" link therefore uses the other loopback name
  (`localhost` when relay runs at `127.0.0.1`). Open project apps on the other
  name, or log in to relay on the tailnet https URL. A Work 2 app shares the
  host network: with a stolen cookie it could call relay on 127.0.0.1.
- A login session row from before the cookie change stays valid until it
  expires (up to 30 days) or until a logout or `relay token --rotate`. The
  gateways and relay's https routes ignore the old plain cookie.
- A link from another site (a bookmark opened from another app, or an OAuth
  callback from an external provider) reaches a gateway without the
  SameSite=Strict cookie and gets the login page. Open apps from relay.
- A save compares the revision a second time and then calls `os.replace`.
  Linux has no rename that replaces only an unchanged file. A write by another
  program that lands between that second check and the rename is overwritten.
  The interval is one `stat` and one `rename` call, normally microseconds.
  relay does not add an exchange and rollback, because a rollback can overwrite
  a third writer. Any earlier change gets 409 and the file stays unchanged.
- A save replaces the file. Another hard link to the old file keeps the old
  content. Extended attributes and ACLs are not copied.
- The relay access log records Files request paths, for example a file path in
  the query string. It never records file content or the token.
