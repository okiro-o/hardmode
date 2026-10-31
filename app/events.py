"""Tiny in-process pub/sub used to fan state out to SSE clients (overlays + control panel)."""
from __future__ import annotations

import asyncio
import json
import time
import weakref
from typing import AsyncIterator, Callable

_BROKERS: "weakref.WeakSet[Broker]" = weakref.WeakSet()


class Broker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._meta: dict[int, dict] = {}          # id(queue) -> {"page": ..., "obs": bool}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = 0
        self._closing = False
        self.on_clients_changed: Callable[[], None] | None = None
        _BROKERS.add(self)

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def clients(self) -> dict:
        """{"weapons": {"obs": n, "browser": n}, "loadout": ..., "dpi": ..., "panel": ...}"""
        out: dict[str, dict] = {}
        for meta in self._meta.values():
            page = meta.get("page") or "other"
            slot = out.setdefault(page, {"obs": 0, "browser": 0})
            slot["obs" if meta.get("obs") else "browser"] += 1
        return out

    def _clients_changed(self) -> None:
        if self.on_clients_changed:
            try:
                self.on_clients_changed()
            except Exception:
                pass

    def publish(self, event: str, data: dict) -> None:
        """Publish from inside the event loop."""
        self._seq += 1
        msg = (self._seq, event, json.dumps(data, ensure_ascii=False))
        for q in list(self._subscribers):
            self._offer(q, msg)

    @staticmethod
    def _offer(q: asyncio.Queue, item) -> None:
        """put_nowait; a slow client loses its oldest message instead of blocking the publisher."""
        try:
            q.put_nowait(item)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def publish_threadsafe(self, event: str, data: dict) -> None:
        """Publish from a foreign thread (chat, Twitch reader, DPI guard, in-game overlay)."""
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self.publish, event, data)

    def call_threadsafe(self, fn: Callable, *args) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(fn, *args)

    def close_streams(self) -> None:
        """End every SSE stream now and refuse new ones (shutdown). Call from the loop thread."""
        self._closing = True
        for q in list(self._subscribers):
            self._offer(q, None)

    async def stream(self, snapshot: Callable[[], dict], heartbeat: float = 15.0,
                     meta: dict | None = None) -> AsyncIterator[str]:
        """Async generator producing SSE frames. First frame is a full snapshot."""
        if self._closing:
            return
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(q)
        self._meta[id(q)] = dict(meta or {})
        self._clients_changed()
        try:
            yield "retry: 1000\n\n"
            yield _frame(0, "snapshot", json.dumps(snapshot(), ensure_ascii=False))
            last = time.monotonic()
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=heartbeat)
                    if item is None:          # close_streams(): finish the response cleanly
                        return
                    seq, event, data = item
                    yield _frame(seq, event, data)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                if time.monotonic() - last > 3600:
                    # periodic full resync for very long-lived clients
                    last = time.monotonic()
                    yield _frame(0, "snapshot", json.dumps(snapshot(), ensure_ascii=False))
        finally:
            self._subscribers.discard(q)
            self._meta.pop(id(q), None)
            self._clients_changed()


def close_all_streams_threadsafe() -> None:
    """Ask every live broker (normally one) to end its SSE streams. Safe from any thread."""
    for b in list(_BROKERS):
        b.call_threadsafe(b.close_streams)


def _frame(seq: int, event: str, data: str) -> str:
    return f"id: {seq}\nevent: {event}\ndata: {data}\n\n"
