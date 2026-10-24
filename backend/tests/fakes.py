"""Shared test doubles. Owned by the foundation; builders may add helpers at the end."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from relay.config import EnvConfig
from relay.podman import ContainerInfo, Podman, ProcResult

Handler = Callable[[list[str], bytes | None], ProcResult]


def ok(stdout: str = "", stderr: str = "") -> ProcResult:
    return ProcResult(0, stdout.encode(), stderr.encode())


def fail(code: int = 1, stderr: str = "error") -> ProcResult:
    return ProcResult(code, b"", stderr.encode())


@dataclass
class FakePodman(Podman):
    """A Podman double. Set ``handler`` to answer ``run`` calls; every call is recorded.

    ``calls`` holds ``(args, input)`` where ``args`` excludes the podman binary.
    ``containers`` answers ``inspect`` per container name.
    """

    handler: Handler | None = None
    calls: list[tuple[list[str], bytes | None]] = field(default_factory=list)
    containers: dict[str, ContainerInfo] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.binary = "podman"
        self.env = {}

    async def run(self, args: Sequence[str], *, input: bytes | None = None, timeout: float = 15.0) -> ProcResult:
        call = (list(args), input)
        self.calls.append(call)
        if self.handler is None:
            return ok()
        return self.handler(list(args), input)

    async def inspect(self, env: EnvConfig) -> ContainerInfo:
        info = self.containers.get(env.container)
        if info is None:
            return ContainerInfo(
                name=env.container,
                exists=True,
                running=True,
                status="running",
                id=f"id-{env.container}",
                started_at="2026-09-29T12:51:02+08:00",
                network_mode="host" if env.id == "work2" else "slirp4netns",
                ports={} if env.id == "work2" else {5173: 15173, 3000: 13000},
            )
        return info

    def exec_commands(self) -> list[list[str]]:
        """The in-container argv of every ``exec`` call (after the container name)."""
        out = []
        for args, _ in self.calls:
            if args and args[0] == "exec":
                # exec --user U [-i] [-t] [-w dir] [-e K=V]... CONTAINER cmd...
                i = 1
                while i < len(args):
                    a = args[i]
                    if a in ("--user", "-w", "-e"):
                        i += 2
                        continue
                    if a in ("-i", "-t"):
                        i += 1
                        continue
                    break
                out.append(args[i + 1 :])
        return out
