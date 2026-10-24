"""Podman subprocess helpers.

Every command is executed with an argument vector. Nothing is passed through
a host shell, so user text (prompts, names, paths) cannot become host shell
syntax. Commands inside a container use ``podman exec --user <uid:gid>``.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .config import EnvConfig


class CommandError(RuntimeError):
    def __init__(self, argv: Sequence[str], code: int, stderr: str):
        self.argv = list(argv)
        self.code = code
        self.stderr = stderr.strip()
        super().__init__(f"command failed ({code}): {' '.join(self.argv[:6])}...: {self.stderr[:300]}")


@dataclass
class ProcResult:
    code: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def out(self) -> str:
        return self.stdout.decode("utf-8", "replace")

    @property
    def err(self) -> str:
        return self.stderr.decode("utf-8", "replace")


@dataclass
class ContainerInfo:
    name: str
    exists: bool
    running: bool = False
    status: str = "missing"            # podman State.Status, or 'missing' / 'error'
    id: str | None = None
    started_at: str | None = None      # identity of the current container run
    network_mode: str | None = None
    ports: dict[int, int] = field(default_factory=dict)  # container tcp port -> host loopback port
    error: str | None = None


def podman_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for podman, matching the ``work`` launcher.

    VS Code's Snap launcher redirects ``XDG_DATA_HOME``. Restore it so Podman
    uses the same rootless storage as a normal terminal.
    """
    env = dict(os.environ if base is None else base)
    if "XDG_DATA_HOME_VSCODE_SNAP_ORIG" in env:
        orig = env["XDG_DATA_HOME_VSCODE_SNAP_ORIG"]
        if orig:
            env["XDG_DATA_HOME"] = orig
        else:
            env.pop("XDG_DATA_HOME", None)
    return env


class Podman:
    def __init__(self, binary: str = "podman", *, quiet_events: bool = True):
        self.binary = binary
        self.env = podman_environment()
        # relay polls every few seconds. With the default journald events
        # backend, each ``podman exec`` writes two journal entries (exec and
        # exec_died), roughly 115,000 lines a day. relay does not consume podman
        # events, so its own calls skip them. Container start/stop events from
        # other tools are unaffected.
        self.global_args: list[str] = ["--events-backend=none"] if quiet_events else []

    def argv(self, args: Sequence[str]) -> list[str]:
        return [self.binary, *self.global_args, *args]

    async def run(self, args: Sequence[str], *, input: bytes | None = None, timeout: float = 15.0) -> ProcResult:
        """Run ``podman <args>`` and capture output. Raises ``TimeoutError``."""
        proc = await asyncio.create_subprocess_exec(
            *self.argv(args),
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(input), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise
        return ProcResult(proc.returncode if proc.returncode is not None else -1, out, err)

    def exec_args(
        self,
        env: EnvConfig,
        cmd: Sequence[str],
        *,
        interactive: bool = False,
        tty: bool = False,
        workdir: str | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Arguments (without the podman binary) for ``podman exec`` in ``env``."""
        args = ["exec", "--user", env.exec_user]
        if interactive:
            args.append("-i")
        if tty:
            args.append("-t")
        if workdir:
            args += ["-w", workdir]
        for key, value in (extra_env or {}).items():
            args += ["-e", f"{key}={value}"]
        args.append(env.container)
        args.extend(cmd)
        return args

    async def exec(
        self,
        env: EnvConfig,
        cmd: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float = 15.0,
        workdir: str | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> ProcResult:
        args = self.exec_args(env, cmd, interactive=input is not None, workdir=workdir, extra_env=extra_env)
        return await self.run(args, input=input, timeout=timeout)

    async def inspect(self, env: EnvConfig) -> ContainerInfo:
        try:
            res = await self.run(["container", "inspect", env.container], timeout=15)
        except (asyncio.TimeoutError, OSError) as exc:
            return ContainerInfo(name=env.container, exists=False, status="error", error=f"podman inspect failed: {exc}")
        if not res.ok:
            text = res.err.lower()
            if "no such" in text or "not found" in text:
                return ContainerInfo(name=env.container, exists=False, status="missing", error="container not found")
            return ContainerInfo(name=env.container, exists=False, status="error", error=res.err.strip()[:300])
        try:
            data = json.loads(res.out)[0]
        except (ValueError, IndexError, TypeError):
            return ContainerInfo(name=env.container, exists=False, status="error", error="unreadable podman inspect output")
        state = data.get("State") or {}
        host_cfg = data.get("HostConfig") or {}
        ports: dict[int, int] = {}
        for spec, bindings in (host_cfg.get("PortBindings") or {}).items():
            port_s, _, proto = str(spec).partition("/")
            if proto not in ("", "tcp") or not bindings:
                continue
            for b in bindings:
                host_ip = (b or {}).get("HostIp") or "0.0.0.0"
                host_port = (b or {}).get("HostPort")
                if host_ip in ("127.0.0.1", "0.0.0.0", "") and host_port:
                    try:
                        ports[int(port_s)] = int(host_port)
                    except ValueError:
                        pass
                    break
        running = bool(state.get("Running"))
        return ContainerInfo(
            name=env.container,
            exists=True,
            running=running,
            status=str(state.get("Status") or ("running" if running else "unknown")),
            id=data.get("Id"),
            started_at=state.get("StartedAt"),
            network_mode=host_cfg.get("NetworkMode"),
            ports=ports,
        )
