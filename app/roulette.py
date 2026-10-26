"""Arms-race roulette state machine.

Server-authoritative state: the queue (current weapon + upcoming ones), the active side,
the loadout, statistics and a short history. The overlay and the control panel only
render snapshots of this object, so an OBS page reload never loses anything.
"""
from __future__ import annotations

import json
import os
import random
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Callable

from . import loadout as L
from . import weapons as W

MAX_HISTORY = 200
MAX_LOG = 60
MAX_UNDO = 20


def _to_int(value: Any, name: str) -> int:
    try:
        if isinstance(value, bool):
            raise TypeError
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: нужно число") from None


@dataclass
class RouletteSettings:
    pool_mode: str = "bag"              # "bag" (shuffle bag, no repeats until exhausted) | "random"
    include_starting: bool = True       # starting pistol is part of the pool
    include_knife: bool = False         # knife in the pool
    include_zeus: bool = False          # Zeus x27 in the pool
    kills_per_weapon: int = 1           # kills needed before the weapon is "done"
    auto_advance: str = "strict"        # "off" | "strict" (kill with assigned weapon) | "any"
    visible_next: int = 2               # upcoming weapons shown on the overlay
    ct_starting: str = "random"         # "random" | "usp_silencer" | "hkp2000"
    side_source: str = "gsi"            # "gsi" | "manual"

    @classmethod
    def from_dict(cls, d: dict | None) -> "RouletteSettings":
        """Build settings from untrusted JSON. Raises ValueError on bad values."""
        if d is not None and not isinstance(d, dict):
            raise ValueError("roulette: нужен объект")
        s = cls()
        names = {f.name for f in fields(cls)}
        for k, v in (d or {}).items():
            if k in names:
                setattr(s, k, v)
            elif k == "include_extras":   # legacy single toggle (< 0.4)
                s.include_knife = s.include_zeus = bool(v)
        s.kills_per_weapon = max(1, min(20, _to_int(s.kills_per_weapon, "kills_per_weapon")))
        s.visible_next = max(0, min(5, _to_int(s.visible_next, "visible_next")))
        s.include_starting = bool(s.include_starting)
        s.include_knife = bool(s.include_knife)
        s.include_zeus = bool(s.include_zeus)
        if s.pool_mode not in ("bag", "random"):
            s.pool_mode = "bag"
        if s.auto_advance not in ("off", "strict", "any"):
            s.auto_advance = "strict"
        if s.side_source not in ("gsi", "manual"):
            s.side_source = "gsi"
        if s.ct_starting not in ("random", "usp_silencer", "hkp2000"):
            s.ct_starting = "random"
        return s

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Roulette:
    settings: RouletteSettings = field(default_factory=RouletteSettings)
    loadout: dict | None = None
    side: str = W.T
    view_side: str | None = None   # side shown in the panel tab / loadout overlay (None = follow side)
    running: bool = False
    queue: list[str] = field(default_factory=list)
    queue_uids: list[int] = field(default_factory=list)   # stable per-slot ids for overlay animation
    uid_seq: int = 0
    bags: dict[str, list[str]] = field(default_factory=dict)
    current_kills: int = 0
    completed: int = 0
    kills_total: int = 0
    kills_wrong_weapon: int = 0
    kills_ambiguous: int = 0
    history: list[dict] = field(default_factory=list)   # finished weapons
    log: list[dict] = field(default_factory=list)       # recent events for the panel
    last_event: dict | None = None
    started_at: float | None = None
    seed: int | None = None
    _rng: random.Random = field(default_factory=random.Random, repr=False)
    _undo: list[dict] = field(default_factory=list, repr=False)
    _on_change: Callable[[], None] | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ helpers
    def bind(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change

    def _changed(self) -> None:
        if self._on_change:
            self._on_change()

    def _log(self, kind: str, text: str, **extra: Any) -> dict:
        entry = {"t": time.time(), "kind": kind, "text": text, **extra}
        self.log.append(entry)
        del self.log[:-MAX_LOG]
        self.last_event = entry
        return entry

    def visible_count(self) -> int:
        return 1 + self.settings.visible_next

    def _q_append(self, weapon_id: str) -> None:
        self.uid_seq += 1
        self.queue.append(weapon_id)
        self.queue_uids.append(self.uid_seq)

    def _q_pop0(self) -> str:
        self.queue_uids.pop(0)
        return self.queue.pop(0)

    def _q_clear(self) -> None:
        self.queue = []
        self.queue_uids = []

    def _q_sync(self) -> None:
        """Repair uids if they ever get out of step with the queue (e.g. old state files)."""
        if len(self.queue_uids) != len(self.queue):
            self.queue_uids = []
            for _ in self.queue:
                self.uid_seq += 1
                self.queue_uids.append(self.uid_seq)

    def pool_ids(self, side: str | None = None) -> list[str]:
        if not self.loadout:
            return []
        return L.side_pool_ids(
            self.loadout,
            side or self.side,
            include_starting=self.settings.include_starting,
            include_knife=self.settings.include_knife,
            include_zeus=self.settings.include_zeus,
        )

    # ----------------------------------------------------------------- loadout
    def generate_loadout(self, sides: tuple[str, ...] = (W.T, W.CT)) -> dict:
        new = dict(self.loadout or {})
        # A loadout always has both sides: generate the requested ones plus any missing one.
        generated = [s for s in L.SIDES if s in sides or s not in new]
        for side in generated:
            new[side] = L.generate_side(side, self._rng, self.settings.ct_starting)
        if len(sides) == 1 and sides[0] in L.SIDES:
            self.view_side = sides[0]   # "only CT" -> show CT in the panel and the loadout overlay
        self._log("loadout", "Лоадаут сгенерирован: " + ", ".join(generated))
        self.set_loadout(new)  # publishes the change (log entry included)
        return new

    def set_loadout(self, loadout: dict) -> None:
        problems = L.validate(loadout)
        if problems:
            raise ValueError("; ".join(problems))
        self.loadout = loadout
        self.bags = {}
        # Loadout changed under a running session: rebuild upcoming weapons but keep the
        # current one if it is still available.
        if self.running or self.queue:
            self._rebuild_queue(keep_current=True)
        else:
            self._q_clear()
        self._changed()

    def replace_weapon(self, side: str, category: str, weapon_id: str) -> dict:
        if not self.loadout:
            raise ValueError("Сначала сгенерируйте лоадаут")
        if weapon_id not in W.BY_ID:
            raise ValueError(f"Неизвестное оружие {weapon_id!r}")
        new = L.replace_weapon(self.loadout, side, category, weapon_id, self._rng)
        self._log("loadout", f"Заменено: {W.get(weapon_id).name} ({side})")
        self.set_loadout(new)  # publishes the change (log entry included)
        return new

    def set_slot(self, side: str, category: str, old_id: str, new_id: str) -> dict:
        if not self.loadout:
            raise ValueError("Сначала сгенерируйте лоадаут")
        new = L.set_slot(self.loadout, side, category, old_id, new_id)
        self._log("loadout", f"{W.get(old_id).name} → {W.get(new_id).name} ({side})")
        self.set_loadout(new)
        return new

    # -------------------------------------------------------------------- queue
    def _new_bag(self, exclude: set[str]) -> list[str]:
        """A shuffled bag of the side's pool, leaving out weapons that are still visible."""
        ids = self.pool_ids()
        bag = [i for i in ids if i not in exclude] or list(ids)
        self._rng.shuffle(bag)
        return bag

    def _draw(self, exclude: set[str]) -> str | None:
        ids = self.pool_ids()
        if not ids:
            return None
        if self.settings.pool_mode == "random":
            options = [i for i in ids if i not in exclude] or ids
            return self._rng.choice(options)
        bag = self.bags.get(self.side)
        if not bag:
            bag = self._new_bag(exclude)
            self.bags[self.side] = bag
        if len(bag) > 1 and bag[0] in exclude:
            # avoid an immediate repeat of something still on screen
            for j in range(1, len(bag)):
                if bag[j] not in exclude:
                    bag[0], bag[j] = bag[j], bag[0]
                    break
        return bag.pop(0)

    def _fill_queue(self, avoid: set[str] | None = None) -> None:
        """Top the queue up to the visible count.

        ``avoid``: weapons that just left the queue (so they are not drawn straight back
        even when the queue is empty, e.g. with visible_next = 0).
        """
        pool = set(self.pool_ids())
        self._q_sync()
        # drop upcoming weapons that are no longer in the pool (loadout/side changed)
        if self.queue:
            keep = [(u, i) for k, (u, i) in enumerate(zip(self.queue_uids, self.queue)) if k == 0 or i in pool]
            self.queue = [i for _, i in keep]
            self.queue_uids = [u for u, _ in keep]
        target = self.visible_count()
        guard = 0
        while len(self.queue) < target and guard < 50:
            guard += 1
            exclude = set(self.queue) | set(avoid or ())
            nxt = self._draw(exclude)
            if nxt is None:
                break
            self._q_append(nxt)

    def _rebuild_queue(self, keep_current: bool) -> None:
        """Rebuild upcoming weapons for the active side/loadout, keeping the current one if possible."""
        pool = set(self.pool_ids())
        self._q_sync()
        old_current = self.current()
        if keep_current and self.queue and self.queue[0] in pool:
            self.queue, self.queue_uids = [self.queue[0]], [self.queue_uids[0]]
        else:
            self._q_clear()
        self.bags.pop(self.side, None)
        self._fill_queue()
        if self.current() != old_current and old_current is not None and self.current_kills:
            self._log("info", f"{W.get(old_current).name} выбыло из пула, {self.current_kills} килл(ов) не засчитано")
            self.current_kills = 0

    # ------------------------------------------------------------------ session
    def start(self, seed: int | None = None) -> None:
        if not self.loadout:
            raise ValueError("Сначала сгенерируйте лоадаут")
        # Resume a paused session (Stop pressed earlier): keep queue, stats, history, undo, rng.
        # reset() clears the queue, so Reset -> Start still begins a fresh session.
        if seed is None and not self.running and self.queue:
            self.running = True
            self._fill_queue()
            self._log("session", f"Сессия продолжена ({self.side}). Текущее: {self.current_name()}")
            self._changed()
            return
        self.seed = seed if seed is not None else random.SystemRandom().randrange(1 << 31)
        self._rng = random.Random(self.seed)
        self.running = True
        self.started_at = time.time()
        self._q_clear()
        self.bags = {}
        self.current_kills = 0
        self.completed = 0
        self.kills_total = 0
        self.kills_wrong_weapon = 0
        self.kills_ambiguous = 0
        self.history = []
        self._undo = []
        self._fill_queue()
        self._log("session", f"Сессия запущена ({self.side}). Первое оружие: {self.current_name()}")
        self._changed()

    def stop(self) -> None:
        """Pause: keeps queue and statistics; start() resumes."""
        self.running = False
        self._log("session", "Пауза")
        self._changed()

    def reset(self) -> None:
        self.running = False
        self._q_clear()
        self.bags = {}
        self.current_kills = 0
        self.completed = 0
        self.kills_total = 0
        self.kills_wrong_weapon = 0
        self.kills_ambiguous = 0
        self.history = []
        self._undo = []
        self._log("session", "Сброс сессии")
        self._changed()

    @property
    def paused(self) -> bool:
        return not self.running and bool(self.queue)

    def current(self) -> str | None:
        return self.queue[0] if self.queue else None

    def current_uid(self) -> int | None:
        self._q_sync()
        return self.queue_uids[0] if self.queue_uids else None

    def current_name(self) -> str:
        cur = self.current()
        return W.get(cur).name if cur else "—"

    # --------------------------------------------------------------------- undo
    def _undo_snapshot(self) -> dict:
        return {
            "queue": list(self.queue),
            "queue_uids": list(self.queue_uids),
            "bags": {k: list(v) for k, v in self.bags.items()},
            "current_kills": self.current_kills,
            "completed": self.completed,
            "kills_total": self.kills_total,
            "kills_wrong_weapon": self.kills_wrong_weapon,
            "kills_ambiguous": self.kills_ambiguous,
            "history": [dict(h) for h in self.history],
            "side": self.side,
            "pool": self.pool_ids(),
        }

    def _push_undo(self, snap: dict | None = None) -> None:
        self._undo.append(snap or self._undo_snapshot())
        del self._undo[:-MAX_UNDO]

    def undo(self) -> bool:
        if not self._undo:
            return False
        snap = self._undo.pop()
        self.queue = snap["queue"]
        self.queue_uids = snap["queue_uids"]
        self.bags = snap["bags"]
        self.current_kills = snap["current_kills"]
        self.completed = snap["completed"]
        self.kills_total = snap["kills_total"]
        self.kills_wrong_weapon = snap["kills_wrong_weapon"]
        self.kills_ambiguous = snap["kills_ambiguous"]
        self.history = snap["history"]
        if snap.get("side") != self.side or snap.get("pool") != self.pool_ids():
            # side / loadout changed since the snapshot: keep what is still valid
            self._rebuild_queue(keep_current=True)
        self._log("undo", f"Отмена. Текущее: {self.current_name()}")
        self._changed()
        return True

    # ------------------------------------------------------------------ actions
    def advance(self, reason: str = "manual", *, undo_snapshot: dict | None = None) -> dict | None:
        """Finish the current weapon and move to the next one."""
        if not self.running or not self.queue:
            return None
        self._push_undo(undo_snapshot)
        finished_uid = self.queue_uids[0] if self.queue_uids else None
        finished = self._q_pop0()
        entry = {
            "weapon": finished,
            "uid": finished_uid,
            "name": W.get(finished).name,
            "kills": self.current_kills,
            "reason": reason,
            "t": time.time(),
            "side": self.side,
        }
        self.history.append(entry)
        del self.history[:-MAX_HISTORY]
        self.completed += 1
        self.current_kills = 0
        self._fill_queue(avoid={finished})
        self._log("advance", f"{entry['name']} → {self.current_name()}", reason=reason, finished=finished)
        self._changed()
        return entry

    def reroll_current(self) -> str | None:
        """Replace the current weapon without counting it as completed."""
        if not self.running or not self.queue:
            return None
        self._push_undo()
        old = self._q_pop0()
        self.current_kills = 0
        self._fill_queue(avoid={old})
        # put the skipped weapon back into the bag so it can come up later (never first)
        if self.settings.pool_mode == "bag" and old in set(self.pool_ids()):
            bag = self.bags.setdefault(self.side, [])
            if old not in bag and old not in self.queue:
                bag.insert(self._rng.randrange(1, len(bag) + 1) if bag else 0, old)
        self._log("reroll", f"Перекрут: {W.get(old).name} → {self.current_name()}")
        self._changed()
        return self.current()

    # --------------------------------------------------------------------- side
    def set_side(self, side: str, source: str = "manual") -> bool:
        if side not in (W.T, W.CT):
            raise ValueError("side must be T or CT")
        if source == "gsi" and self.settings.side_source != "gsi":
            return False
        if side == self.side:
            return False
        self.side = side
        self.view_side = side
        if self.running or self.queue:
            self._rebuild_queue(keep_current=True)
            self._log("side", f"Сторона: {side}. Текущее: {self.current_name()}", source=source)
        else:
            self._log("side", f"Сторона: {side}", source=source)
        self._changed()
        return True

    def set_view_side(self, side: str) -> None:
        """Which side the panel tab and the loadout overlay display (does not affect the roulette)."""
        if side not in (W.T, W.CT):
            raise ValueError("side must be T or CT")
        if self.view_side != side:
            self.view_side = side
            self._changed()

    # -------------------------------------------------------------------- kills
    def on_kill(self, weapon_id: str | None, *, headshot: bool = False, ambiguous: bool = False,
                raw_name: str | None = None) -> dict:
        """Register a kill detected by GSI. Returns a dict describing what happened."""
        result = {"counted": False, "advanced": False, "matched": False, "weapon": weapon_id,
                  "ambiguous": ambiguous, "finished_uid": None}
        if not self.running or not self.queue:
            return result
        snap = self._undo_snapshot()  # taken before any counter moves, so undo restores it all
        self.kills_total += 1
        cur = self.queue[0]
        matched = weapon_id == cur
        result["matched"] = matched
        label = W.get(weapon_id).name if weapon_id and weapon_id in W.BY_ID else (raw_name or "?")
        hs = " (в голову)" if headshot else ""
        mode = self.settings.auto_advance
        if ambiguous and mode != "any":
            self.kills_ambiguous += 1
            self._log("kill", f"Килл{hs}: возможно гранатой — не засчитан", matched=False, weapon=weapon_id)
            self._changed()
            return result
        if matched or mode == "any":
            self.current_kills += 1
            result["counted"] = True
            need = self.settings.kills_per_weapon
            self._log("kill", f"Килл{hs} с {label} ({self.current_kills}/{need})", matched=matched,
                      weapon=weapon_id)
            if mode != "off" and self.current_kills >= need:
                result["finished_uid"] = self.queue_uids[0] if self.queue_uids else None
                self.advance(reason="kill", undo_snapshot=snap)
                result["advanced"] = True
            else:
                self._changed()
        else:
            self.kills_wrong_weapon += 1
            self._log("kill", f"Килл{hs} с {label} — не то оружие (нужно {W.get(cur).name})", matched=False,
                      weapon=weapon_id)
            self._changed()
        return result

    # ----------------------------------------------------------------- settings
    def update_settings(self, patch: dict) -> None:
        old = self.settings
        new = RouletteSettings.from_dict({**old.to_dict(), **(patch or {})})  # may raise ValueError
        self.settings = new
        pool_changed = (
            old.include_starting != new.include_starting
            or old.include_knife != new.include_knife
            or old.include_zeus != new.include_zeus
            or old.pool_mode != new.pool_mode
        )
        if self.running or self.queue:
            if pool_changed:
                self._rebuild_queue(keep_current=True)
            else:
                self._fill_queue()
        self._changed()

    # ------------------------------------------------------------- persistence
    def to_dict(self) -> dict:
        return {
            "settings": self.settings.to_dict(),
            "loadout": self.loadout,
            "side": self.side,
            "view_side": self.view_side,
            "running": self.running,
            "queue": self.queue,
            "queue_uids": self.queue_uids,
            "uid_seq": self.uid_seq,
            "bags": self.bags,
            "current_kills": self.current_kills,
            "completed": self.completed,
            "kills_total": self.kills_total,
            "kills_wrong_weapon": self.kills_wrong_weapon,
            "kills_ambiguous": self.kills_ambiguous,
            "history": self.history,
            "log": self.log,
            "started_at": self.started_at,
            "seed": self.seed,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "Roulette":
        r = cls()
        if not isinstance(d, dict):
            return r
        try:
            r.settings = RouletteSettings.from_dict(d.get("settings") if isinstance(d.get("settings"), dict) else None)
        except ValueError:
            r.settings = RouletteSettings()
        lo = d.get("loadout")
        r.loadout = lo if isinstance(lo, dict) and not L.validate(lo) else None
        r.side = d.get("side") if d.get("side") in (W.T, W.CT) else W.T
        r.view_side = d.get("view_side") if d.get("view_side") in (W.T, W.CT) else None
        r.running = bool(d.get("running", False))
        queue_raw = d.get("queue") if isinstance(d.get("queue"), list) else []
        r.queue = [q for q in queue_raw if isinstance(q, str) and q in W.BY_ID]
        r.uid_seq = int(d.get("uid_seq", 0) or 0) if isinstance(d.get("uid_seq", 0), (int, float)) else 0
        uids = d.get("queue_uids") if isinstance(d.get("queue_uids"), list) else []
        try:
            r.queue_uids = [int(u) for u in uids] if len(uids) == len(queue_raw) == len(r.queue) else []
        except (TypeError, ValueError):
            r.queue_uids = []
        r._q_sync()
        bags = d.get("bags") if isinstance(d.get("bags"), dict) else {}
        r.bags = {k: [q for q in v if isinstance(q, str) and q in W.BY_ID]
                  for k, v in bags.items() if isinstance(v, list)}
        for name in ("current_kills", "completed", "kills_total", "kills_wrong_weapon", "kills_ambiguous"):
            try:
                setattr(r, name, int(d.get(name, 0) or 0))
            except (TypeError, ValueError):
                setattr(r, name, 0)
        r.history = [h for h in (d.get("history") or []) if isinstance(h, dict)][-MAX_HISTORY:]
        r.log = [e for e in (d.get("log") or []) if isinstance(e, dict)][-MAX_LOG:]
        r.started_at = d.get("started_at") if isinstance(d.get("started_at"), (int, float)) else None
        r.seed = d.get("seed") if isinstance(d.get("seed"), int) else None
        r._rng = random.Random(r.seed) if r.seed is not None else random.Random()
        if r.loadout is None:
            # no loadout -> there cannot be a session
            r.running = False
            r._q_clear()
            r.bags = {}
        elif r.running or r.queue:
            r._fill_queue()
        return r

    def save(self, path: str) -> None:
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Roulette":
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return cls()
        except (OSError, json.JSONDecodeError, ValueError):
            return cls()
        try:
            return cls.from_dict(data)
        except Exception:  # corrupted state must never prevent the app from starting
            return cls()

    # ------------------------------------------------------------------ snapshot
    def completed_ids(self) -> dict[str, list[str]]:
        return {s: list(dict.fromkeys(h["weapon"] for h in self.history if h.get("side") == s and h.get("weapon")))
                for s in (W.T, W.CT)}

    def snapshot(self) -> dict:
        self._q_sync()
        visible = list(zip(self.queue_uids, self.queue))[: self.visible_count()]
        return {
            "running": self.running,
            "paused": self.paused,
            "side": self.side,
            "view_side": self.view_side or self.side,
            "loadout": self.loadout,
            "queue": [{**W.get(i).to_dict(), "uid": u} for u, i in visible],
            "pool": self.pool_ids() if self.loadout else [],
            "current_kills": self.current_kills,
            "kills_per_weapon": self.settings.kills_per_weapon,
            "completed": self.completed,
            "completed_ids": self.completed_ids(),
            "kills_total": self.kills_total,
            "kills_wrong_weapon": self.kills_wrong_weapon,
            "kills_ambiguous": self.kills_ambiguous,
            "history": self.history[-10:],
            "log": self.log[-25:],
            "last_event": self.last_event,
            "can_undo": bool(self._undo),
            "settings": self.settings.to_dict(),
            "started_at": self.started_at,
        }
