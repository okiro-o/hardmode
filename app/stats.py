"""Per-stream statistics: DPI values vs kills/deaths/rounds/time and per-weapon buys, pickups,
kills, deaths. Fed by GSI events (gsi.py) and DPI hooks (service.py); persisted to data/stats.json
so a restart does not lose the stream; «Сброс» clears the current part.

Parts (0.37.0, user: «я играю с приложением и вне стрима»): the current statistics belong to a session,
``stream`` (a Twitch broadcast, id/title from Helix) or ``offstream``. When the broadcast starts or ends
(``start_session``), the session so far is archived to data/stats-history/*.json and counting starts again,
so play before and after a stream never mixes into it. The panel can open any archived session."""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any

from . import config, weapons as W

log = logging.getLogger("hardmode.stats")

GRENADE_NAMES = {"hegrenade": "HE", "flashbang": "Flash", "smokegrenade": "Smoke",
                 "molotov": "Molotov", "incgrenade": "Incendiary", "decoy": "Decoy"}


def weapon_key(raw: str | None, weapon_id: str | None) -> str | None:
    if weapon_id:
        return weapon_id
    if W.is_non_weapon(raw):
        return None          # healthshot, C4, knives...: not weapons for the statistics
    if raw and raw.startswith("weapon_"):
        return raw[len("weapon_"):]
    return raw or None


def weapon_name(key: str) -> str:
    if key in W.BY_ID:
        return W.BY_ID[key].name
    return GRENADE_NAMES.get(key, key)


MAX_HISTORY = 200                    # archived sessions kept in data/stats-history


def _session(kind: str = "offstream", stream: dict | None = None, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    stream = stream or {}
    return {"kind": kind, "stream_id": stream.get("id"), "title": stream.get("title"),
            "started_at": stream.get("started_at") or now, "last_event_at": None}


def _fresh(session: dict | None = None) -> dict:
    return {"started_at": time.time(), "dpi": {}, "weapons": {}, "maps": {}, "session": session or _session(),
            "totals": {"kills": 0, "hs": 0, "deaths": 0, "assists": 0, "mvps": 0, "rounds": 0, "rolls": 0,
                       "best_round_kills": 0, "bought": 0, "picked": 0}}


def snapshot_of(data: dict, current_dpi: int | None = None) -> dict:
    """The panel's view of a statistics dict (the current one or an archived session)."""
    dpi = [{"value": int(k), **v} for k, v in data["dpi"].items()]
    dpi.sort(key=lambda r: r["value"])
    rows = [{"key": k, **v} for k, v in data["weapons"].items()]
    weapons = [r for r in rows if r["key"] not in GRENADE_NAMES]
    grenades = [r for r in rows if r["key"] in GRENADE_NAMES]
    weapons.sort(key=lambda r: (-r["kills"], -r["bought"], -r["picked"], r["name"]))
    grenades.sort(key=lambda r: (-r.get("thrown", 0), -r["bought"], r["name"]))
    maps = [{"name": k, "title": map_title(k), **v} for k, v in data["maps"].items()]
    maps.sort(key=lambda r: (-r["rounds"], r["name"]))
    return {"started_at": data["started_at"], "totals": dict(data["totals"]), "session": dict(data.get("session") or {}),
            "dpi": dpi, "weapons": weapons, "grenades": grenades, "maps": maps, "current_dpi": current_dpi}


# modes without real rounds: the whole match is one «round» for the game (deathmatch, arms race), so the best
# round would be the match total and every match one round (0.34.0)
ROUNDLESS_MODES = ("deathmatch", "gungameprogressive", "training")


def map_title(name: str) -> str:
    base = name.split("_", 1)[1] if "_" in name else name
    return base[:1].upper() + base[1:]


class StatsTracker:
    def __init__(self, path: str) -> None:
        self.path = path
        self.history_dir = os.path.join(os.path.dirname(path) or ".", "stats-history")
        self._lock = threading.Lock()
        self.data = _fresh()
        self.cur_dpi: int | None = None
        self._round: dict | None = None     # {"no", "dpi", "changed", "kills"}
        self._map: str | None = None        # map the current counters go to
        self._over = False                  # the match on self._map reached its final screen
        self._last_t: float | None = None
        self._last_save = 0.0
        self._dirty = False          # countable events not yet written
        self._time_dirty = False     # only the per-DPI time counters moved
        self._load()

    # ------------------------------------------------------------------ store
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("totals"), dict):
                base = _fresh()
                base.update({k: d[k] for k in ("started_at", "dpi", "weapons", "maps") if k in d})
                if isinstance(d.get("session"), dict):
                    base["session"].update(d["session"])
                else:                                  # files written before 0.37.0: play outside a stream
                    base["session"]["started_at"] = base["started_at"]
                base["totals"].update(d["totals"])
                # damage dropped (per weapon in 0.21.4, everywhere in 0.29.0: CS2 never sent it)
                for row in [base["totals"], *base["dpi"].values(), *base["maps"].values(), *base["weapons"].values()]:
                    row.pop("damage", None)
                for row in base["weapons"].values():
                    row.setdefault("thrown", 0)        # files written before 0.17.1
                for row in base["maps"].values():
                    for k in ("wins", "losses", "draws"):
                        row.setdefault(k, 0)           # match results since 0.29.0
                for key in [k for k in base["weapons"] if W.is_non_weapon("weapon_" + k)]:
                    del base["weapons"][key]      # entries booked before the non-weapon filter existed
                self.data = base
                match = d.get("match") if isinstance(d.get("match"), dict) else {}
                self._set_match(match.get("map") if isinstance(match.get("map"), str) else None, bool(match.get("over")))
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
            config.write_json_atomic(self.path, self.data)
            self._dirty = self._time_dirty = False
        except OSError:
            log.warning("cannot save stats", exc_info=True)

    def _set_match(self, map_name: str | None, over: bool) -> None:
        """The match the counters go to, kept in stats.json (0.38.0): a restart in the middle of a match (the
        update button restarts the app) must not count it a second time."""
        self._map, self._over = map_name, over
        self.data["match"] = {"map": map_name, "over": over}

    def flush(self) -> None:
        with self._lock:
            if self._dirty or self._time_dirty:      # time alone is saved twice a minute: never lose it at exit
                self._save(force=True)

    def _dpi(self, value: int) -> dict:
        return self.data["dpi"].setdefault(str(int(value)), {"rolls": 0, "kills": 0, "deaths": 0, "rounds": 0,
                                                             "seconds": 0.0})

    def _weapon(self, key: str) -> dict:
        return self.data["weapons"].setdefault(key, {"name": weapon_name(key), "bought": 0, "picked": 0,
                                                     "kills": 0, "hs": 0, "deaths": 0, "thrown": 0})

    def _mapstat(self) -> dict | None:
        if not self._map:
            return None
        return self.data["maps"].setdefault(self._map, {"matches": 0, "rounds": 0, "kills": 0, "hs": 0,
                                                        "deaths": 0, "assists": 0, "wins": 0, "losses": 0, "draws": 0})

    # ------------------------------------------------------------------ hooks
    def on_dpi_roll(self, value: int, reason: str = "") -> None:
        with self._lock:
            self._dpi(value)["rolls"] += 1
            self.data["totals"]["rolls"] += 1
            self.data["session"]["last_event_at"] = time.time()
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
                    self._time_dirty = True
            self._last_t = now
            t = self.data["totals"]
            map_name = getattr(status, "map_name", None)
            if map_name and (map_name != self._map or self._over) and getattr(status, "map_phase", None) in ("live", "intermission", "warmup"):
                self._set_match(map_name, False)
                self._mapstat()["matches"] += 1      # a new map, or the same map after a final screen = a new match
                changed = True
            m = self._mapstat()
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
                    if m is not None:
                        m["kills"] += 1
                        if ev.get("headshot"):
                            m["hs"] += 1
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
                    if m is not None:
                        m["deaths"] += 1
                    changed = True
                elif kind == "throw":
                    key = weapon_key(ev.get("raw"), ev.get("weapon"))
                    if key:
                        self._weapon(key)["thrown"] += 1
                        changed = True
                elif kind == "mvp":
                    t["mvps"] += int(ev.get("count") or 1)
                    changed = True
                elif kind == "assist":
                    n = int(ev.get("count") or 1)
                    t["assists"] += n
                    if m is not None:
                        m["assists"] += n
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
                elif kind == "match_end":
                    self._set_match(self._map, True)
                    changed = True                           # the finished match is saved even without a result
                    res = {"win": "wins", "loss": "losses", "draw": "draws"}.get(ev.get("result"))
                    if m is not None and res:
                        m[res] += 1
                elif kind == "round_start":
                    roundless = getattr(status, "mode", None) in ROUNDLESS_MODES
                    self._round = None if roundless else {"no": ev.get("round"), "dpi": self.cur_dpi, "changed": False, "kills": 0}
                elif kind == "round_end":
                    if self._round is not None:
                        t["rounds"] += 1
                        if m is not None:
                            m["rounds"] += 1
                        if not self._round["changed"] and self._round["dpi"] is not None:
                            self._dpi(self._round["dpi"])["rounds"] += 1
                        self._round = None
                        changed = True
            if changed:
                self.data["session"]["last_event_at"] = now
            if changed or self._dirty:
                self._save()
            elif self._time_dirty and now - self._last_save > 30:
                self._save()      # time counters alone: at most twice a minute
        return changed

    # ---------------------------------------------------------------- control
    @property
    def session(self) -> dict:
        return dict(self.data["session"])

    def has_data(self) -> bool:
        t = self.data["totals"]
        return any(t.get(k) for k in ("kills", "deaths", "rounds", "rolls", "bought", "picked")) or bool(self.data["maps"])

    def start_session(self, kind: str, stream: dict | None = None, now: float | None = None) -> str | None:
        """The broadcast started or ended: archive the session so far (when something was counted) and
        start counting again. Returns the archived session id."""
        now = time.time() if now is None else now
        with self._lock:
            archived = self._archive(now) if self.has_data() else None
            self.data = _fresh(_session(kind, stream, now))
            self._set_match(self._map, self._over)
            self._round = None
            if self._map and not self._over:          # a match in progress keeps counting in the new part
                self._mapstat()["matches"] = 1
            self._save(force=True)
        log.info("statistics: new %s session%s", kind, f", archived {archived}" if archived else "")
        return archived

    def _archive(self, now: float) -> str | None:
        sess = self.data["session"]
        sess["ended_at"] = sess.get("last_event_at") or now
        sid = time.strftime("%Y%m%d-%H%M%S", time.localtime(sess.get("started_at") or self.data["started_at"])) + "-" + sess["kind"]
        try:
            os.makedirs(self.history_dir, exist_ok=True)
            path = os.path.join(self.history_dir, sid + ".json")
            n = 2
            while os.path.exists(path):                # two parts started in the same second
                path = os.path.join(self.history_dir, f"{sid}-{n}.json")
                n += 1
            config.write_json_atomic(path, self.data)
            files = [os.path.join(self.history_dir, f) for f in os.listdir(self.history_dir) if f.endswith(".json")]
            for f in sorted(files, key=os.path.getmtime)[:-MAX_HISTORY]:      # the oldest archived go first
                os.remove(f)
            return os.path.basename(path)[:-5]
        except OSError:
            log.warning("cannot archive statistics", exc_info=True)
            return None

    def history(self) -> list[dict]:
        """Archived sessions, the last finished first: {id, kind, title, started_at, ended_at, totals}."""
        out: list[dict] = []
        try:
            names = [f for f in os.listdir(self.history_dir) if f.endswith(".json")]
        except OSError:
            return out
        for f in names:
            try:
                with open(os.path.join(self.history_dir, f), encoding="utf-8") as fh:
                    d = json.load(fh)
                sess = d.get("session") or {}
                out.append({"id": f[:-5], "kind": sess.get("kind", "offstream"), "title": sess.get("title"),
                            "started_at": sess.get("started_at") or d.get("started_at"), "ended_at": sess.get("ended_at"),
                            "totals": d.get("totals") or {}})
            except (OSError, ValueError):
                continue
        out.sort(key=lambda h: h.get("ended_at") or h.get("started_at") or 0, reverse=True)
        return out

    def history_item(self, sid: str) -> dict | None:
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-(stream|offstream)(-[0-9]+)?", sid or ""):
            return None                                  # an id from the list, never a path
        try:
            with open(os.path.join(self.history_dir, sid + ".json"), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return None
        base = _fresh(_session())
        base.update({k: d[k] for k in ("started_at", "dpi", "weapons", "maps", "session") if k in d})
        base["totals"].update(d.get("totals") or {})
        for row in base["maps"].values():
            for k in ("wins", "losses", "draws"):
                row.setdefault(k, 0)
        return {**snapshot_of(base), "id": sid}

    def reset(self) -> None:
        with self._lock:
            self.data = _fresh({**self.data["session"], "started_at": time.time(), "last_event_at": None})
            self._set_match(self._map, self._over)
            self._round = None
            if self._map and not self._over:     # the match in progress keeps counting after a reset
                self._mapstat()["matches"] = 1
            self._save(force=True)

    def snapshot(self) -> dict:
        with self._lock:
            return snapshot_of(self.data, self.cur_dpi)
