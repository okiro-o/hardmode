"""Viewer weapon requests: a queue separate from the arms race.

Viewers may ask for any weapon, also one outside the current loadout, so requests never touch the
roulette. The streamer fulfils them when possible (picks the gun up in a round, or sets the loadout for
the next match) and closes them in the panel; a kill with the requested weapon closes the oldest open
request for it automatically (``autocomplete``). Requests whose weapon was not recognised keep the raw
text and wait until the streamer picks the weapon by hand.

Order: requests for a weapon outside the current loadout (``available``, both sides plus knife and zeus) go
after all the others, keeping their own order (user's rule, 0.20.1); unrecognised requests stay in place.
Without a loadout nothing is reordered. The stored order is the arrival order (plus manual moves), the
partition is applied on every read, so a new loadout reorders the queue at once (``refresh``).

Persisted in data/viewer_queue.json; ``done``/``dismissed`` entries are kept as a short history.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Callable

from ... import config, weapons as W

MAX_HISTORY = 30
MAX_PENDING = 100


class ViewerQueue:
    def __init__(self, path: str, publish: Callable[[str, dict], None] | None = None) -> None:
        self.path = path
        self._publish = publish
        self._lock = threading.Lock()
        self.entries: list[dict] = []
        self._next_id = 1
        # weapon ids the streamer can play now (the loadout), None = no loadout: wired by app.main
        self.available: Callable[[], set[str] | None] = lambda: None
        self._avail_key: frozenset | None = None
        self._load()

    # ------------------------------------------------------------------ store
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(d, dict):
            return
        rows = [e for e in d.get("entries") or [] if isinstance(e, dict) and isinstance(e.get("id"), int)]
        for e in rows:
            if e.get("weapon") not in W.BY_ID:
                e["weapon"] = None
            e.setdefault("status", "pending")
        self.entries = rows
        self._next_id = max([e["id"] + 1 for e in rows] + [int(d.get("next_id") or 1)])

    def _save(self) -> None:
        try:
            config.write_json_atomic(self.path, {"next_id": self._next_id, "entries": self.entries})
        except OSError:
            pass

    def _changed(self) -> None:
        self._save()
        if self._publish:
            self._publish("queue", self.snapshot())

    # ------------------------------------------------------------------ queries
    def _available(self) -> set[str] | None:
        try:
            avail = self.available()
        except Exception:  # noqa: BLE001 - ordering must never break the queue
            return None
        return set(avail) if avail else None

    @staticmethod
    def _outside(e: dict, avail: set[str] | None) -> bool:
        return avail is not None and e.get("weapon") is not None and e["weapon"] not in avail

    def pending(self, avail: set[str] | None = None, *, ordered: bool = True) -> list[dict]:
        """Open requests in display order: the ones outside the loadout after all the others."""
        pend = [e for e in self.entries if e["status"] == "pending"]
        if not ordered:
            return pend
        avail = self._available() if avail is None else avail
        if avail is None:
            return pend
        return [e for e in pend if not self._outside(e, avail)] + [e for e in pend if self._outside(e, avail)]

    def snapshot(self) -> dict:
        avail = self._available()
        with self._lock:
            pend = [self._view(e, avail) for e in self.pending(avail)]
            hist = [self._view(e) for e in self.entries if e["status"] != "pending"][-10:][::-1]
        return {"pending": pend, "history": hist}

    def _view(self, e: dict, avail: set[str] | None = None) -> dict:
        w = W.BY_ID.get(e.get("weapon") or "")
        return {**e, "name": w.name if w else None, "icon": w.icon if w else None,
                "in_loadout": not self._outside(e, avail)}

    def refresh(self) -> None:
        """The loadout may have changed: republish when the set of playable weapons is different."""
        avail = self._available()
        key = frozenset(avail) if avail is not None else None
        if key != self._avail_key:
            self._avail_key = key
            if self._publish and any(e["status"] == "pending" for e in self.entries):
                self._publish("queue", self.snapshot())

    # ------------------------------------------------------------------ changes
    def add(self, weapon: str | None, text: str, user: str, amount: float | None = None,
            currency: str = "", source: str = "donation", by_side: str | None = None) -> dict:
        """``by_side``: the alias named several weapons («плётка») and the one of this side was taken."""
        if weapon is not None and weapon not in W.BY_ID:
            weapon = None
        avail = self._available()
        with self._lock:
            if len(self.pending(ordered=False)) >= MAX_PENDING:
                raise ValueError("очередь переполнена")
            entry = {"id": self._next_id, "weapon": weapon, "text": str(text or "")[:200], "user": str(user or "?")[:40],
                     "amount": amount, "currency": currency, "source": source, "t": time.time(), "status": "pending",
                     "by_side": by_side}
            self._next_id += 1
            self.entries.append(entry)
            position = self.pending(avail).index(entry) + 1 if avail is not None else len(self.pending(ordered=False))
            self._trim()
        self._changed()
        return {**self._view(entry, avail), "position": position}

    def _find(self, entry_id: int) -> dict | None:
        return next((e for e in self.entries if e["id"] == entry_id), None)

    def _close(self, entry_id: int, status: str) -> dict | None:
        with self._lock:
            e = self._find(entry_id)
            if not e or e["status"] != "pending":
                return None
            e["status"] = status
            e["closed_t"] = time.time()
            self._trim()
        self._changed()
        return self._view(e)

    def done(self, entry_id: int) -> dict | None:
        return self._close(entry_id, "done")

    def dismiss(self, entry_id: int) -> dict | None:
        return self._close(entry_id, "dismissed")

    def resolve(self, entry_id: int, weapon: str) -> dict | None:
        """The streamer picked the weapon for a request that was not recognised (or fixes a wrong one)."""
        if weapon not in W.BY_ID:
            raise ValueError("неизвестное оружие")
        with self._lock:
            e = self._find(entry_id)
            if not e or e["status"] != "pending":
                return None
            e["weapon"] = weapon
            e["by_side"] = None
        self._changed()
        return self._view(e)

    def move(self, entry_id: int, delta: int) -> bool:
        """Move a pending request up (delta < 0) or down among the pending ones. A request outside the
        loadout cannot be lifted above the playable ones (and vice versa): the order would snap back."""
        avail = self._available()
        with self._lock:
            pend = self.pending(avail)
            e = self._find(entry_id)
            if e not in pend:
                return False
            i = pend.index(e)
            j = max(0, min(len(pend) - 1, i + delta))
            if i == j:
                return False
            other = pend[j]
            if self._outside(e, avail) != self._outside(other, avail):
                return False
            a, b = self.entries.index(e), self.entries.index(other)
            self.entries[a], self.entries[b] = other, e
        self._changed()
        return True

    def clear_history(self) -> None:
        with self._lock:
            self.entries = self.pending(ordered=False)
        self._changed()

    def describe_for(self, *names: str) -> str:
        """Answer to the chat command: this viewer's requests with positions, else a short overview."""
        wanted = {n.lower() for n in names if n}
        avail = self._available()
        with self._lock:
            pend = self.pending(avail)
            mine = [(i + 1, e) for i, e in enumerate(pend) if str(e.get("user", "")).lower() in wanted]
            label = lambda e: (W.BY_ID[e["weapon"]].name if e.get("weapon") in W.BY_ID else "оружие уточняется")
            if mine:
                return "твои заявки: " + ", ".join(f"{label(e)} — №{n}" for n, e in mine[:5])
            if not pend:
                return "очередь пуста"
            top = ", ".join(label(e) for e in pend[:3]) + ("…" if len(pend) > 3 else "")
            return f"у тебя нет заявок. В очереди {len(pend)}: {top}"

    def on_kill(self, weapon: str | None) -> dict | None:
        """A kill with ``weapon``: close the oldest open request for it. Returns the closed entry."""
        if not weapon:
            return None
        with self._lock:
            e = next((x for x in self.entries if x["status"] == "pending" and x.get("weapon") == weapon), None)
            if e is None:
                return None
            e["status"] = "done"
            e["closed_t"] = time.time()
            e["auto"] = True
            self._trim()
        self._changed()
        return self._view(e)

    def _trim(self) -> None:
        closed = [e for e in self.entries if e["status"] != "pending"]
        if len(closed) > MAX_HISTORY:
            drop = {id(e) for e in closed[:len(closed) - MAX_HISTORY]}
            self.entries = [e for e in self.entries if id(e) not in drop]
