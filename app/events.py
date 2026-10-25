"""Tiny in-process pub/sub used to fan state out to SSE clients (overlays + control panel)."""
from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Callable


class Broker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = 0

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def publish(self, event: str, data: dict) -> None:
        """Publish from inside the event loop."""
        self._seq += 1
        msg = (self._seq, event, json.dumps(data, ensure_ascii=False))
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                # slow client: drop the oldest message
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def publish_threadsafe(self, event: str, data: dict) -> None:
        """Publish from a foreign thread (hotkey thread etc.)."""
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self.publish, event, data)

    def call_threadsafe(self, fn: Callable, *args) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(fn, *args)

    async def stream(self, snapshot: Callable[[], dict], heartbeat: float = 15.0) -> AsyncIterator[str]:
        """Async generator producing SSE frames. First frame is a full snapshot."""
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        try:
            yield "retry: 1000\n\n"
            yield _frame(0, "snapshot", json.dumps(snapshot(), ensure_ascii=False))
            last = time.monotonic()
            while True:
                try:
                    seq, event, data = await asyncio.wait_for(q.get(), timeout=heartbeat)
                    yield _frame(seq, event, data)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                if time.monotonic() - last > 3600:
                    # periodic full resync for very long-lived clients
                    last = time.monotonic()
                    yield _frame(0, "snapshot", json.dumps(snapshot(), ensure_ascii=False))
        finally:
            self._subscribers.discard(q)


def _frame(seq: int, event: str, data: str) -> str:
    return f"id: {seq}\nevent: {event}\ndata: {data}\n\n"
