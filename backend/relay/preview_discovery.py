"""Bounded discovery of web endpoints for the Previews page.

Sources:

- Container processes. One ``podman exec`` per environment runs a static
  Python script. It reads the LISTEN sockets of the container's network
  namespace, maps each socket inode to a process that the container's own PID
  namespace shows, and sends one ``HEAD /`` (then TLS) to each owned port. A
  socket that no process inside the container owns is never listed. That rule
  matters for Work 2: it shares the host network, so relay itself and laptop
  services listen in the same table, but their processes are not visible in
  the container.
- Docker. relay reads ``GET /containers/json`` on the host Docker socket. A
  container joins a workspace only through a verified host path (the Compose
  ``working_dir`` label or a bind-mount source that exists under an
  environment's host projects folder) or a registered preview on the same
  laptop port. A ``/home/node/projects/...`` path alone is ambiguous: both
  work containers use it.

Only HTTP or HTTPS answers count. The scan is cached, bounded by time, and
reports a partial or error state per source instead of failing the page.

App metadata (for the app choice in ``previews``): a frontend tool name from
the process argv, or the "next-server" title (argv or comm); and for
``python -m http.server`` the folder it serves (its --directory/-d option,
else the cwd) with a bounded check for ``index.html`` in that folder only.
A project group is the nearest ``.git`` folder under the projects root.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import stat
import time
from pathlib import PurePosixPath
from typing import Any

import httpx

from .config import EnvConfig, Settings
from .runtime import Runtime

SCAN_TTL = 10.0            # seconds a scan result is reused
SCAN_MIN_INTERVAL = 2.0    # a forced rescan waits at least this long after the last one
EXEC_TIMEOUT = 12.0
DOCKER_TIMEOUT = 3.0
PROBE_TIMEOUT = 0.8
MAX_DOCKER_CONTAINERS = 200
MAX_DOCKER_PROBES = 24
MAX_GROUP_DEPTH = 32       # folders checked for a .git marker, from the deepest up

# Tools whose HTTP server is a web UI. An HTTP answer alone never makes an app:
# API servers (uvicorn, gunicorn, ...) and generic file servers (http.server,
# serve, http-server) stay "type unknown" until the owner chooses them.
FRONTEND_TOOLS = frozenset(
    {
        "vite", "next", "nuxt", "astro", "remix", "svelte-kit", "webpack", "webpack-dev-server", "react-scripts",
        "storybook", "parcel", "hugo", "jekyll", "live-server", "streamlit",
    }
)

# Runs inside the container as the exec user. Static text: no user value is
# ever part of it. Bounds: 4000 processes, 4096 fds each, 32 probed ports, 6 s
# of probing.
CONTAINER_SCRIPT = r'''
import json, os, socket, ssl, time
T0 = time.monotonic()
def kind(fam, h):
    try:
        raw = bytes.fromhex(h)
    except ValueError:
        return "other"
    if fam == 4:
        b = raw[::-1]
        if b == bytes(4): return "any"
        return "loopback" if b[0] == 127 else "other"
    b = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    if b == bytes(16): return "any"
    if b == bytes(15) + b"\x01": return "loopback6"
    if b[:12] == bytes(10) + b"\xff\xff":
        if b[12:] == bytes(4): return "any"
        return "loopback" if b[12] == 127 else "other"
    return "other"
socks = {}
for name, fam in (("/proc/net/tcp", 4), ("/proc/net/tcp6", 6)):
    try:
        with open(name) as fh:
            next(fh, None)
            for line in fh:
                p = line.split()
                if len(p) < 10 or p[3] != "0A":
                    continue
                addr, port = p[1].rsplit(":", 1)
                socks[p[9]] = (int(port, 16), kind(fam, addr))
    except OSError:
        pass
owners = {}
me = os.getpid()
pids = sorted(int(d) for d in os.listdir("/proc") if d.isdigit())[:4000]
for pid in pids:
    if pid == me:
        continue
    try:
        fds = os.listdir("/proc/%d/fd" % pid)[:4096]
    except OSError:
        continue
    for fd in fds:
        try:
            link = os.readlink("/proc/%d/fd/%s" % (pid, fd))
        except OSError:
            continue
        if link.startswith("socket:["):
            ino = link[8:-1]
            if ino in socks and ino not in owners:
                owners[ino] = pid
TOOLS = {"vite", "next", "nuxt", "astro", "remix", "webpack", "webpack-dev-server", "react-scripts",
         "storybook", "svelte-kit", "uvicorn", "gunicorn", "hypercorn", "flask", "http.server", "rails",
         "puma", "hugo", "jekyll", "serve", "http-server", "live-server", "parcel", "esbuild", "wrangler",
         "streamlit", "jupyter", "jupyter-lab", "fastapi", "granian", "daphne", "manage.py"}
def tool(argv):
    for a in argv[:12]:
        if a.startswith("next-server"):
            return "next"  # the Next.js dev worker renames itself "next-server (vX)"
        base = a.rsplit("/", 1)[-1].split("@", 1)[0]
        for suffix in (".js", ".mjs", ".cjs"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        if base in TOOLS:
            return "django" if base == "manage.py" else base
    return None
def served_dir(argv, cwd):
    # The folder that "python -m http.server" serves: its --directory or -d
    # option (argparse forms), else the process cwd. Interpreter flags before
    # the module name are not module options.
    if not cwd or "http.server" not in argv[:12]:
        return None
    args = argv[argv.index("http.server") + 1:][:40]
    d = None
    i = 0
    while i < len(args):
        a = args[i]
        name = a[2:].split("=", 1)[0] if a.startswith("--") else ""
        if a == "-d" or (name and "=" not in a and "directory".startswith(name)):
            d = args[i + 1] if i + 1 < len(args) else d
            i += 2
            continue
        if name and "directory".startswith(name):
            d = a.split("=", 1)[1]
        elif a.startswith("-d") and not a.startswith("--"):
            d = a[2:]
        i += 1
    return os.path.realpath(os.path.join(cwd, d if d else "."))
def html_entry(folder):
    # Two stats in the served folder only: no crawl and no file content.
    for name in ("index.html", "index.htm"):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            return os.path.realpath(path)
    return None
try:
    btime = 0.0
    with open("/proc/stat") as fh:
        for line in fh:
            if line.startswith("btime "):
                btime = float(line.split()[1])
    tick = os.sysconf("SC_CLK_TCK")
except (OSError, ValueError):
    btime, tick = 0.0, 100
def proc(pid):
    info = {"pid": pid, "comm": None, "tool": None, "cwd": None, "started_at": None}
    try:
        with open("/proc/%d/comm" % pid) as fh:
            info["comm"] = fh.read().strip()[:40]
    except OSError:
        pass
    argv = []
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            argv = [a.decode("utf-8", "replace") for a in fh.read(8192).split(b"\0") if a]
        info["tool"] = tool(argv)
    except OSError:
        pass
    if info["tool"] is None and (info["comm"] or "").startswith("next-server"):
        info["tool"] = "next"  # the kernel keeps 15 bytes of the renamed title: "next-server (v1"
    try:
        info["cwd"] = os.readlink("/proc/%d/cwd" % pid)
    except OSError:
        pass
    if info["tool"] == "http.server":
        try:
            info["served_dir"] = served_dir(argv, info["cwd"])
            info["html_entry"] = html_entry(info["served_dir"]) if info["served_dir"] else None
        except (OSError, ValueError):
            pass
    try:
        with open("/proc/%d/stat" % pid) as fh:
            rest = fh.read().rsplit(")", 1)[1].split()
        if btime:
            info["started_at"] = btime + int(rest[19]) / tick
    except (OSError, IndexError, ValueError):
        pass
    return info
REQ = b"HEAD / HTTP/1.0\r\nHost: localhost:%d\r\nUser-Agent: relay-discovery\r\nConnection: close\r\n\r\n"
def probe(port, v6):
    host = "::1" if v6 else "127.0.0.1"
    data = b""
    try:
        with socket.create_connection((host, port), timeout=0.8) as s:
            s.sendall(REQ % port)
            data = s.recv(16)
    except OSError:
        return None
    if data.startswith(b"HTTP/"):
        return "http"
    if data and data[:1] != b"\x15":
        return None
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=0.8) as raw:
            with ctx.wrap_socket(raw, server_hostname="localhost") as s:
                s.sendall(REQ % port)
                data = s.recv(16)
        if data.startswith(b"HTTP/"):
            return "https"
    except (OSError, ValueError):
        pass
    return None
ports = {}
for ino, (port, k) in socks.items():
    pid = owners.get(ino)
    if pid is None:
        continue
    entry = ports.setdefault(port, {"port": port, "binds": [], "pid": pid})
    if k not in entry["binds"]:
        entry["binds"].append(k)
out = []
partial = False
for port in sorted(ports)[:64]:
    entry = ports[port]
    binds = entry["binds"]
    if not any(b in ("any", "loopback", "loopback6") for b in binds):
        entry["protocol"] = None
        entry["skipped"] = "bound to another address"
    elif len(out) >= 32 or time.monotonic() - T0 > 6.0:
        entry["protocol"] = "unchecked"
        partial = True
    else:
        v6 = "loopback6" in binds and "any" not in binds and "loopback" not in binds
        entry["protocol"] = probe(port, v6)
    entry.update(proc(entry.pop("pid")))
    out.append(entry)
if len(ports) > 64:
    partial = True
listen_ports = sorted(set(port for port, _ in socks.values()))[:2000]
print(json.dumps({"listeners": out, "listen_ports": listen_ports, "partial": partial, "processes": len(pids)}))
'''


# -- container processes -----------------------------------------------------

async def scan_env(rt: Runtime, env: EnvConfig) -> dict[str, Any]:
    status = rt.env_status.get(env.id)
    info = status.container if status else None
    if info is None:
        info = await rt.podman.inspect(env)
    if not info.running:
        return {"status": "unavailable", "message": f"{env.label} is not running.", "listeners": [], "info": info}
    try:
        res = await rt.podman.exec(env, ["python3", "-c", CONTAINER_SCRIPT], timeout=EXEC_TIMEOUT)
    except (asyncio.TimeoutError, TimeoutError):
        return {"status": "error", "message": f"The scan of {env.label} timed out.", "listeners": [], "info": info}
    except OSError as exc:
        return {"status": "error", "message": f"podman failed: {type(exc).__name__}", "listeners": [], "info": info}
    if not res.ok:
        return {"status": "error", "message": f"The scan of {env.label} failed.", "listeners": [], "info": info}
    try:
        data = json.loads(res.out.strip().splitlines()[-1])
        listeners = [x for x in data.get("listeners", []) if isinstance(x, dict)]
    except (ValueError, IndexError, AttributeError):
        return {"status": "error", "message": f"The scan of {env.label} returned unreadable output.", "listeners": [], "info": info}
    ports = data.get("listen_ports") if isinstance(data.get("listen_ports"), list) else []
    return {
        "status": "partial" if data.get("partial") else "ok",
        "message": "Some ports were not checked in time." if data.get("partial") else None,
        "listeners": listeners,
        "listen_ports": [p for p in ports if isinstance(p, int)],
        "info": info,
    }


def _project_of(env: EnvConfig, cwd: str | None) -> tuple[str | None, str | None]:
    root = env.projects_root.rstrip("/")
    if not cwd:
        return None, None
    if cwd == root:
        return "", cwd
    if cwd.startswith(root + "/"):
        return cwd[len(root) + 1:], cwd
    return None, cwd


def _inside_root(env: EnvConfig, path: str) -> bool:
    """A container path strictly under the projects root, without '..'."""
    root = env.projects_root.rstrip("/")
    return path.startswith(root + "/") and ".." not in PurePosixPath(path).parts


def is_frontend(endpoint: dict[str, Any] | None) -> bool:
    """A clear web UI: a frontend tool, or a static server whose verified served
    folder has an HTML entry. An HTTP answer alone never counts."""
    proc = (endpoint or {}).get("process") or {}
    tool = proc.get("tool")
    if (endpoint or {}).get("source") != "process":
        return False
    return tool in FRONTEND_TOOLS or (tool == "http.server" and proc.get("html_entry") is True)


# -- project groups ----------------------------------------------------------

def _git_marker(host_dir: str) -> bool:
    """True for a ``.git`` folder with ``HEAD``, or a ``.git`` file (a worktree or
    submodule) that starts with ``gitdir:``. A symlinked ``.git`` never counts."""
    marker = os.path.join(host_dir, ".git")
    try:
        st = os.lstat(marker)
    except OSError:
        return False
    if stat.S_ISDIR(st.st_mode):
        return os.path.isfile(os.path.join(marker, "HEAD"))
    if not (stat.S_ISREG(st.st_mode) and st.st_size <= 4096):
        return False
    # The file can change after lstat: no symlink, no blocking open (a FIFO),
    # and a regular file again on the open descriptor.
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        return stat.S_ISREG(os.fstat(fd).st_mode) and os.read(fd, 7) == b"gitdir:"
    except OSError:
        return False
    finally:
        os.close(fd)


def git_group(env: EnvConfig, folder: str | None) -> str | None:
    """The nearest folder with a ``.git`` marker from ``folder`` up to, but not
    including, the projects root. Returns a container path or None.

    ``folder`` is a verified container path (a process cwd, a registered
    project folder, or a Docker bind or Compose folder). The walk reads the
    host side of the bind-mounted home and skips any folder whose real path
    leaves the projects root.
    """
    root = env.projects_root.rstrip("/")
    if not folder or not folder.startswith(root + "/"):
        return None
    parts = PurePosixPath(folder).relative_to(root).parts
    if not parts or ".." in parts:
        return None
    host_root = env.host_path(root)
    if host_root is None:
        return None
    real_root = os.path.realpath(host_root)
    for n in range(len(parts), max(0, len(parts) - MAX_GROUP_DEPTH), -1):
        real = os.path.realpath(os.path.join(host_root, *parts[:n]))
        if not real.startswith(real_root + "/"):
            continue
        if _git_marker(real):
            return root + "/" + "/".join(parts[:n])
    return None


def process_endpoints(settings: Settings, env: EnvConfig, scan: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn container listeners into endpoints. Only HTTP or HTTPS answers count."""
    info = scan.get("info")
    host_net = info is not None and info.network_mode == "host"
    published = info.ports if info is not None else {}
    gateway_ports = {gw.listen_port for gw in settings.preview_gateways}
    out = []
    for item in scan.get("listeners", []):
        try:
            port = int(item["port"])
        except (KeyError, TypeError, ValueError):
            continue
        protocol = item.get("protocol")
        if protocol not in ("http", "https"):
            continue  # not answered, not HTTP (a database), or not checked in time
        if port == settings.port or port in gateway_ports:
            continue
        host_port = port if host_net else published.get(port)
        cwd = item.get("cwd")
        static_entry = False
        if item.get("tool") == "http.server":
            # A static server belongs to the folder it serves, not to its cwd.
            served = item.get("served_dir")
            cwd = served if isinstance(served, str) and served.startswith("/") else None
            entry = item.get("html_entry")
            static_entry = bool(cwd) and isinstance(entry, str) and _inside_root(env, cwd) and _inside_root(env, entry)
        project, folder = _project_of(env, cwd)
        out.append(
            {
                "key": f"{env.id}:{host_port if host_port else 'c' + str(port)}",
                "env": env.id,
                "port": port,
                "host_port": host_port,
                "reachable": host_port is not None,
                "network": "host" if host_net else "published" if host_port else "unpublished",
                "protocol": protocol,
                "source": "process",
                "process": {
                    "pid": item.get("pid"),
                    "name": item.get("comm"),
                    "tool": item.get("tool"),
                    "started_at": item.get("started_at"),
                    "html_entry": static_entry,
                },
                "project": project,
                "folder": folder,
                "docker": None,
            }
        )
    return out


# -- Docker ------------------------------------------------------------------

async def docker_containers(socket_path: str) -> list[dict[str, Any]]:
    transport = httpx.AsyncHTTPTransport(uds=socket_path)
    async with httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=DOCKER_TIMEOUT, trust_env=False) as client:
        resp = await client.get("/containers/json")
        resp.raise_for_status()
        data = resp.json()
    return data[:MAX_DOCKER_CONTAINERS] if isinstance(data, list) else []


def docker_link(settings: Settings, container: dict[str, Any]) -> dict[str, Any] | None:
    """A verified workspace link from host paths only. None when ambiguous."""
    labels = container.get("Labels") or {}
    candidates: list[tuple[str, str]] = []
    wd = labels.get("com.docker.compose.project.working_dir")
    if isinstance(wd, str) and wd:
        candidates.append(("compose", wd))
    for mount in container.get("Mounts") or []:
        if isinstance(mount, dict) and mount.get("Type") == "bind" and isinstance(mount.get("Source"), str):
            candidates.append(("mount", mount["Source"]))
    for how, path in candidates:
        if not path.startswith("/") or not os.path.exists(path):
            continue
        real = os.path.realpath(path)
        for env in settings.envs.values():
            host_root = env.host_path(env.projects_root)
            if host_root is None:
                continue
            root = os.path.realpath(host_root)
            if real == root or real.startswith(root + "/"):
                rel = os.path.relpath(real, root)
                return {"env": env.id, "project": "" if rel == "." else rel, "how": how}
    return None


def docker_ports(container: dict[str, Any]) -> list[tuple[int, int]]:
    """(laptop port, container port) pairs published on loopback or all addresses."""
    seen: dict[int, int] = {}
    for p in container.get("Ports") or []:
        if not isinstance(p, dict) or p.get("Type") != "tcp" or not p.get("PublicPort"):
            continue
        if (p.get("IP") or "0.0.0.0") not in ("0.0.0.0", "::", "127.0.0.1", "::1"):
            continue
        seen.setdefault(int(p["PublicPort"]), int(p.get("PrivatePort") or 0))
    return sorted(seen.items())


async def http_probe(port: int) -> str | None:
    """HEAD / on 127.0.0.1:port, then TLS. Return 'http', 'https', or None."""
    req = b"HEAD / HTTP/1.0\r\nHost: localhost:%d\r\nUser-Agent: relay-discovery\r\nConnection: close\r\n\r\n" % port

    async def attempt(tls: bool) -> bytes:
        ctx = None
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port, ssl=ctx, server_hostname="localhost" if tls else None),
            PROBE_TIMEOUT,
        )
        try:
            writer.write(req)
            await writer.drain()
            return await asyncio.wait_for(reader.read(16), PROBE_TIMEOUT)
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), PROBE_TIMEOUT)
            except (OSError, asyncio.TimeoutError, ssl.SSLError):
                pass

    try:
        data = await attempt(False)
    except (OSError, asyncio.TimeoutError):
        return None
    if data.startswith(b"HTTP/"):
        return "http"
    if data and data[:1] != b"\x15":
        return None
    try:
        data = await attempt(True)
    except (OSError, asyncio.TimeoutError, ssl.SSLError):
        return None
    return "https" if data.startswith(b"HTTP/") else None


async def docker_endpoints(rt: Runtime, registered: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Docker endpoints with a verified link or a registered association."""
    socket_path = rt.settings.docker_socket
    if not socket_path:
        return {"status": "off", "message": None, "endpoints": []}
    if not os.path.exists(socket_path):
        return {"status": "off", "message": "Docker is not available on this laptop.", "endpoints": []}
    try:
        containers = await docker_containers(socket_path)
    except (httpx.HTTPError, OSError, ValueError) as exc:
        return {"status": "error", "message": f"relay cannot read Docker ({type(exc).__name__}).", "endpoints": []}
    candidates: list[dict[str, Any]] = []
    for c in containers:
        if c.get("State") not in (None, "running"):
            continue
        link = docker_link(rt.settings, c)
        name = ((c.get("Names") or ["?"])[0] or "?").lstrip("/")
        for host_port, private in docker_ports(c):
            reg_key = next((k for k, row in registered.items() if row["host_port"] == host_port), None)
            if link is None and reg_key is None:
                continue  # no verified workspace association
            env_id = link["env"] if link else registered[reg_key]["env"]
            candidates.append(
                {
                    "key": f"{env_id}:{host_port}",
                    "env": env_id,
                    "port": host_port,
                    "host_port": host_port,
                    "reachable": True,
                    "network": "docker",
                    "protocol": None,
                    "source": "docker",
                    "process": {"pid": None, "name": name, "tool": None, "started_at": c.get("Created")},
                    "project": link["project"] if link else None,
                    "folder": None,
                    "docker": {
                        "name": name,
                        "image": str(c.get("Image") or "")[:120],
                        "container_port": private,
                        "link": link["how"] if link else "registered",
                    },
                }
            )
    candidates = candidates[:MAX_DOCKER_PROBES]
    results = await asyncio.gather(*(http_probe(c["host_port"]) for c in candidates), return_exceptions=True)
    endpoints = []
    for c, proto in zip(candidates, results):
        if proto in ("http", "https"):
            c["protocol"] = proto
            endpoints.append(c)
    return {"status": "ok", "message": None, "endpoints": endpoints}


# -- the merged scan ---------------------------------------------------------

async def scan(rt: Runtime, registered: dict[str, dict[str, Any]], *, force: bool = False) -> dict[str, Any]:
    """Run (or reuse) one scan of every source. ``registered`` maps key -> row info."""
    lock: asyncio.Lock = rt.cache.setdefault("scan_lock", asyncio.Lock())
    async with lock:
        cached = rt.cache.get("scan")
        age = time.time() - cached["at"] if cached else None
        reg_sig = sorted(registered)
        if cached and cached.get("registered") == reg_sig:
            if age is not None and (age < SCAN_MIN_INTERVAL or (not force and age < SCAN_TTL)):
                return cached
        env_ids = list(rt.settings.envs)
        env_results, docker = await asyncio.gather(
            asyncio.gather(*(scan_env(rt, rt.settings.env(e)) for e in env_ids)),
            docker_endpoints(rt, registered),
        )
        envs: dict[str, Any] = {}
        endpoints: list[dict[str, Any]] = []
        for env_id, res in zip(env_ids, env_results):
            env = rt.settings.env(env_id)
            envs[env_id] = {
                "status": res["status"],
                "message": res["message"],
                "listen_ports": res.get("listen_ports", []),
            }
            endpoints.extend(process_endpoints(rt.settings, env, res))
        seen = {e["key"] for e in endpoints}
        for e in docker["endpoints"]:
            if e["key"] not in seen:
                endpoints.append(e)
                seen.add(e["key"])
        result = {
            "at": time.time(),
            "registered": reg_sig,
            "envs": envs,
            "docker": {"status": docker["status"], "message": docker["message"]},
            "endpoints": endpoints,
        }
        rt.cache["scan"] = result
        return result
