"""In-process change notifications.

Producers call ``bus.publish("sessions")`` after they change state. The
``/api/stream`` WebSocket forwards topics to browsers, which then refetch the
REST resource. Messages carry no data, so a missed message can never leave a
client with wrong data: the next fetch is always authoritative.

Topics: ``sessions``, ``activity``, ``envs``, ``previews``, ``checks``.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import Iterator

TOPICS = frozenset({"sessions", "activity", "envs", "previews", "checks"})


class Bus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[str]] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, *topics: str) -> None:
        """Notify subscribers. Safe to call from the event loop or a worker thread."""
        unknown = set(topics) - TOPICS
        if unknown:
            raise ValueError(f"unknown bus topic(s): {sorted(unknown)}")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is None and self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._deliver, topics)
        else:
            self._deliver(topics)

    def _deliver(self, topics: tuple[str, ...]) -> None:
        with self._lock:
            subs = list(self._subs)
        for queue in subs:
            for topic in topics:
                try:
                    queue.put_nowait(topic)
                except asyncio.QueueFull:
                    pass  # a slow client will refetch everything on its next message

    @contextmanager
    def subscribe(self, maxsize: int = 256) -> Iterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.add(queue)
        try:
            yield queue
        finally:
            with self._lock:
                self._subs.discard(queue)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)
