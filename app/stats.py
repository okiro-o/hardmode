"""Per-stream statistics: DPI values vs kills/deaths/rounds/time and per-weapon buys, pickups,
kills, deaths. Fed by GSI events (gsi.py) and DPI hooks (service.py); persisted to data/stats.json
so a restart does not lose the stream; "Новый стрим" resets everything."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from . import weapons as W

log = logging.getLogger("hardmode.stats")

GRENADE_NAMES = {"hegrenade": "HE", "flashbang": "Flash", "smokegrenade": "Smoke",
                 "molotov": "Molotov", "incgrenade": "Incendiary", "decoy": "Decoy"}


def weapon_key(raw: str | None, weapon_id: str | None) -> str | None:
    if weapon_id:
        return weapon_id
    if raw and raw.startswith("weapon_"):
        return raw[len("weapon_"):]
    return raw or None


def weapon_name(key: str) -> str:
    if key in W.BY_ID:
        return W.BY_ID[key].name
    return GRENADE_NAMES.get(key, key)


def _fresh() -> dict:
    return {"started_at": time.time(), "dpi": {}, "weapons": {},
            "totals": {"kills": 0, "hs": 0, "deaths": 0, "rounds": 0, "rolls": 0, "best_round_kills": 0,
                       "bought": 0, "picked": 0}}


class StatsTracker:
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.data = _fresh()
        self.cur_dpi: int | None = None
        self._round: dict | None = None     # {"no", "dpi", "changed", "kills"}
        self._last_t: float | None = None
        self._last_save = 0.0
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------ store
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("totals"), dict):
                base = _fresh()
                base.update({k: d[k] for k in ("started_at", "dpi", "weapons") if k in d})
                base["totals"].update(d["totals"])
                self.data = base
        except (OSError, ValueError):
            pass

    def _save(self, force: bool = False) -> None:
        """Write at most every 2 s; anything skipped stays dirty until the next save or flush()."""
        now = time.time()
        if not force and now - self._last_save < 2.0:
            self._dirty = True
            return
        self._last_save = now
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            log.warning("cannot save stats", exc_info=True)

    def flush(self) -> None:
        with self._lock:
            if self._dirty:
                self._save(force=True)

    def _dpi(self, value: int) -> dict:
        return self.data["dpi"].setdefault(str(int(value)), {"rolls": 0, "kills": 0, "deaths": 0, "rounds": 0, "seconds": 0.0})

    def _weapon(self, key: str) -> dict:
        return self.data["weapons"].setdefault(key, {"name": weapon_name(key), "bought": 0, "picked": 0,
                                                     "kills": 0, "hs": 0, "deaths": 0})

    # ------------------------------------------------------------------ hooks
    def on_dpi_roll(self, value: int, reason: str = "") -> None:
        with self._lock:
            self._dpi(value)["rolls"] += 1
            self.data["totals"]["rolls"] += 1
            self._save()

    def on_dpi_applied(self, value: int) -> None:
        with self._lock:
            self.cur_dpi = int(value)
            if self._round and self._round.get("dpi") != self.cur_dpi:
                self._round["changed"] = True

    def handle_events(self, events: list[dict], status: Any, dpi_current: int | None, now: float | None = None) -> bool:
        """Consume one GSI event batch. Returns True when something countable changed."""
        now = time.time() if now is None else now
        changed = False
        with self._lock:
            if dpi_current is not None:
                self.cur_dpi = int(dpi_current)
            # time spent at the current DPI (live rounds only; gaps > 10 s are not counted)
            if getattr(status, "map_phase", None) == "live" and self._last_t is not None and self.cur_dpi is not None:
                dt = now - self._last_t
                if 0 < dt < 10:
                    self._dpi(self.cur_dpi)["seconds"] += dt
            self._last_t = now
            t = self.data["totals"]
            for ev in events:
                kind = ev.get("type")
                if kind == "kill":
                    if self.cur_dpi is not None:
                        self._dpi(self.cur_dpi)["kills"] += 1
                    key = weapon_key(ev.get("raw"), ev.get("weapon"))
                    if key:
                        w = self._weapon(key)
                        w["kills"] += 1
                        if ev.get("headshot"):
                            w["hs"] += 1
                    t["kills"] += 1
                    if ev.get("headshot"):
                        t["hs"] += 1
                    if self._round:
                        self._round["kills"] += 1
                        t["best_round_kills"] = max(t["best_round_kills"], self._round["kills"])
                    changed = True
                elif kind == "death":
                    if self.cur_dpi is not None:
                        self._dpi(self.cur_dpi)["deaths"] += 1
                    key = weapon_key(ev.get("raw"), ev.get("weapon"))
                    if key:
                        self._weapon(key)["deaths"] += 1
                    t["deaths"] += 1
                    changed = True
                elif kind == "pickup":
                    if ev.get("spawn"):
                        continue
                    key = weapon_key(ev.get("raw"), ev.get("weapon"))
                    if not key:
                        continue
                    field = "bought" if ev.get("bought") else "picked"
                    self._weapon(key)[field] += 1
                    t[field] += 1
                    changed = True
                elif kind == "round_start":
                    self._round = {"no": ev.get("round"), "dpi": self.cur_dpi, "changed": False, "kills": 0}
                elif kind == "round_end":
                    if self._round is not None:
                        t["rounds"] += 1
                        if not self._round["changed"] and self._round["dpi"] is not None:
                            self._dpi(self._round["dpi"])["rounds"] += 1
                        self._round = None
                        changed = True
            if changed:
                self._save()
            elif self._dirty or now - self._last_save > 30:
                self._save()      # pending events or the time counters
        return changed

    # ---------------------------------------------------------------- control
    def reset(self) -> None:
        with self._lock:
            self.data = _fresh()
            self._round = None
            self._save(force=True)

    def snapshot(self) -> dict:
        with self._lock:
            dpi = [{"value": int(k), **v} for k, v in self.data["dpi"].items()]
            dpi.sort(key=lambda r: r["value"])
            weapons = [{"key": k, **v} for k, v in self.data["weapons"].items()]
            weapons.sort(key=lambda r: (-r["kills"], -r["bought"], -r["picked"], r["name"]))
            return {"started_at": self.data["started_at"], "totals": dict(self.data["totals"]),
                    "dpi": dpi, "weapons": weapons, "current_dpi": self.cur_dpi}
