"""CS2 Game State Integration parsing and kill attribution.

GSI never says which weapon made a kill, so we infer it:

* only payloads about *our own* player count (``player.steamid == provider.steamid``;
  when dead, the ``player`` block follows the spectated teammate);
* a kill is ``player.state.round_kills`` increasing while ``map.phase == "live"``
  (within the same round, or across a round change when the counter is still higher);
* the weapon is the one that was active (or reloading) in the previous payload, falling
  back to the current payload;
* if a lethal grenade was thrown shortly before the kill the attribution is flagged
  ambiguous (HE detonates ~2 s after the throw, molotov burns for ~7 s).

The parser must never raise on odd payload shapes: everything is coerced defensively.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import weapons as W

HE_WINDOW = 3.0       # seconds after an HE throw during which a kill may be the HE
FIRE_WINDOW = 9.0     # seconds after a molotov/incendiary throw
ACTIVE_STATES = ("active", "reloading")


def _obj(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _int(v: Any, default: int | None = None) -> int | None:
    try:
        if isinstance(v, bool):
            return int(v)
        return int(v)
    except (TypeError, ValueError):
        return default


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) else (str(v) if isinstance(v, (int, float)) else None)


# map.phase values in which kills and deaths are real: "intermission" is the half-time screen and the
# last kill/death of a half arrives with it already set; "gameover" likewise for the final round.
ACTIVE_PHASES = ("live", "intermission", "gameover")


@dataclass
class GsiStatus:
    connected: bool = False
    last_payload_at: float | None = None
    own_player: bool | None = None
    activity: str | None = None
    map_name: str | None = None
    map_phase: str | None = None
    round_phase: str | None = None
    round: int | None = None
    mode: str | None = None
    team: str | None = None
    health: int | None = None
    held: str | None = None          # weapon id currently in hand (catalog id) or None
    held_raw: str | None = None      # raw GSI name (weapon_ak47, weapon_hegrenade, ...)
    round_kills: int | None = None
    match_kills: int | None = None
    steamid: str | None = None
    name: str | None = None
    payloads: int = 0
    rejected_auth: int = 0
    last_rejected_at: float | None = None
    rejected_name: str | None = None     # who is posting with a wrong token (diagnostics)
    last_name_at: float | None = None

    def clear_player(self) -> None:
        self.own_player = None
        self.health = None
        self.held = None
        self.held_raw = None
        self.round_kills = None
        self.match_kills = None

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        now = time.time()
        d["age"] = (now - self.last_payload_at) if self.last_payload_at else None
        d["rejected_age"] = (now - self.last_rejected_at) if self.last_rejected_at else None
        return d


@dataclass
class _OwnSnapshot:
    t: float
    map_name: str | None
    round: int | None
    round_kills: int
    round_killhs: int
    match_kills: int | None
    active_raw: str | None
    grenades: dict[str, int]   # gsi name -> count in inventory
    alive: bool
    round_phase: str | None = None
    names: tuple[str, ...] = ()   # gsi names of everything in the inventory
    money: int | None = None


@dataclass
class GsiTracker:
    token: str | None = None
    status: GsiStatus = field(default_factory=GsiStatus)
    _prev: _OwnSnapshot | None = None
    _thrown: list[tuple[str, float]] = field(default_factory=list)
    _was_alive: bool = False   # last own payload had health > 0 (death detection)

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _weapon_entries(weapons: Any) -> list[dict]:
        if isinstance(weapons, dict):
            return [e for e in weapons.values() if isinstance(e, dict)]
        if isinstance(weapons, list):
            return [e for e in weapons if isinstance(e, dict)]
        return []

    @classmethod
    def _active_weapon(cls, weapons: Any) -> dict | None:
        for entry in cls._weapon_entries(weapons):
            if str(entry.get("state", "")).lower() in ACTIVE_STATES:
                return entry
        return None

    @classmethod
    def _grenades(cls, weapons: Any) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in cls._weapon_entries(weapons):
            name = _str(entry.get("name"))
            if W.is_grenade(name):
                count = _int(entry.get("ammo_reserve"), 1) or 1
                out[name] = out.get(name, 0) + count
        return out

    def _note_throws(self, prev: dict[str, int], cur: dict[str, int], now: float) -> None:
        for name, count in prev.items():
            if name in W.LETHAL_GRENADES_HE or name in W.LETHAL_GRENADES_FIRE:
                if cur.get(name, 0) < count:
                    self._thrown.append((name, now))
        self._thrown = [(n, t) for n, t in self._thrown if now - t < FIRE_WINDOW]

    def _grenade_ambiguity(self, now: float) -> str | None:
        for name, t in self._thrown:
            if name in W.LETHAL_GRENADES_HE and now - t <= HE_WINDOW:
                return name
            if name in W.LETHAL_GRENADES_FIRE and now - t <= FIRE_WINDOW:
                return name
        return None

    # ------------------------------------------------------------------- handle
    def handle(self, payload: dict, now: float | None = None) -> list[dict]:
        """Process one GSI POST. Returns a list of events:

        ``{"type": "kill", "weapon": id|None, "raw": gsi_name, "headshot": bool,
           "ambiguous": grenade_name|None, "index": i, "count": n}``
        ``{"type": "side", "side": "T"|"CT"}``
        ``{"type": "death"}`` (our player just died in a live round / live DM)
        ``{"type": "status"}`` (always last, status object updated)
        """
        now = time.time() if now is None else now
        events: list[dict] = []
        st = self.status
        payload = _obj(payload)

        if self.token:
            got = _obj(payload.get("auth")).get("token")
            if got != self.token:
                st.rejected_auth += 1
                st.last_rejected_at = now
                st.rejected_name = _str(_obj(payload.get("player")).get("name")) or st.rejected_name
                return events

        st.connected = True
        st.last_payload_at = now
        st.payloads += 1

        provider = _obj(payload.get("provider"))
        player = _obj(payload.get("player"))
        map_ = _obj(payload.get("map"))
        round_ = _obj(payload.get("round"))

        provider_sid = _str(provider.get("steamid"))
        player_sid = _str(player.get("steamid"))
        own = bool(player) and (provider_sid is None or player_sid == provider_sid)

        st.own_player = own if player else None
        st.activity = _str(player.get("activity"))
        st.map_name = _str(map_.get("name"))
        st.map_phase = _str(map_.get("phase"))
        st.mode = _str(map_.get("mode"))
        st.round = _int(map_.get("round"))
        st.round_phase = _str(round_.get("phase"))
        st.steamid = provider_sid
        if own and _str(player.get("name")):
            st.name = _str(player.get("name"))
            st.last_name_at = now

        if not player or not map_:
            # main menu / loading: nothing to track, reset per-round memory and stale fields
            if not map_:
                self._prev = None
                self._was_alive = False
                st.clear_player()
                st.team = None
            events.append({"type": "status"})
            return events

        if not own:
            # the player block switched to a teammate: that only happens when we are dead
            if self._was_alive and st.map_phase in ACTIVE_PHASES:
                self._was_alive = False
                events.append(self._death_event(self._prev.active_raw if self._prev else None))
            events.append({"type": "status"})
            return events

        state = _obj(player.get("state"))
        weapons = player.get("weapons")
        active = self._active_weapon(weapons)
        active_raw = _str(active.get("name")) if active else None
        held = W.from_gsi_name(active_raw)
        st.held = held.id if held else None
        st.held_raw = active_raw
        st.health = _int(state.get("health"))
        if st.health is not None:
            if st.health > 0:
                self._was_alive = True
            elif self._was_alive:
                self._was_alive = False
                if st.map_phase in ACTIVE_PHASES:
                    events.append(self._death_event((self._prev.active_raw if self._prev else None) or active_raw))
        team = _str(player.get("team"))
        if team in (W.T, W.CT) and team != st.team:
            st.team = team
            events.append({"type": "side", "side": team})
        elif team in (W.T, W.CT):
            st.team = team

        round_kills = _int(state.get("round_kills"), 0) or 0
        round_killhs = _int(state.get("round_killhs"), 0) or 0
        match_kills = _int(_obj(player.get("match_stats")).get("kills"))
        st.round_kills = round_kills
        st.match_kills = match_kills
        grenades = self._grenades(weapons)
        alive = (st.health is None or st.health > 0) and bool(self._weapon_entries(weapons))
        money = _int(state.get("money"))
        names = tuple(sorted({n for n in (_str(e.get("name")) for e in self._weapon_entries(weapons)) if n}))

        prev = self._prev
        same_map = prev is not None and prev.map_name == st.map_name
        same_round = same_map and prev is not None and prev.round == st.round

        # round boundaries (statistics): new round number, or the phase turning "over"
        if st.map_phase in ACTIVE_PHASES and st.round is not None:
            if prev is None or not same_map:
                events.append({"type": "round_start", "round": st.round})
            elif prev.round != st.round:
                if prev.round_phase != "over" and prev.round is not None:
                    events.append({"type": "round_end", "round": prev.round})
                events.append({"type": "round_start", "round": st.round})
            elif prev.round_phase != "over" and st.round_phase == "over":
                events.append({"type": "round_end", "round": st.round})

        # inventory changes (statistics): bought = money went down in the same payload; items that
        # appear right after a death / with a new round and without spending are spawn equipment
        if prev is not None and same_map and st.map_phase in ACTIVE_PHASES and prev.names:
            money_down = money is not None and prev.money is not None and money < prev.money
            spawned = (not prev.alive) or (not same_round and not money_down)
            added: list[str] = [n for n in names if n not in prev.names]
            for gname, count in grenades.items():       # extra grenades of a kind already carried
                extra = count - prev.grenades.get(gname, 0) - (0 if gname in prev.names else 1)
                added += [gname] * max(0, extra)
            for name in added:
                if W.is_non_weapon(name):
                    continue
                w = W.from_gsi_name(name)
                events.append({"type": "pickup", "raw": name, "weapon": w.id if w else None,
                               "bought": money_down, "spawn": spawned and not money_down})
        if prev is not None and same_round and alive and prev.alive:
            # a grenade disappearing while alive means it was thrown (death/drop would be noise)
            self._note_throws(prev.grenades, grenades, now)

        # "previously" carries the old value even if we missed an intermediate POST, but only
        # when it describes the same player: after switching back from a spectated teammate
        # previously.player holds the TEAMMATE's values (signalled by steamid/name present).
        prev_player = _obj(_obj(payload.get("previously")).get("player"))
        if "steamid" in prev_player or "name" in prev_player:
            prev_from_payload = None
        else:
            prev_from_payload = _int(_obj(prev_player.get("state")).get("round_kills"))

        old_kills: int | None = None
        if prev is not None and same_round:
            old_kills = prev.round_kills
            if prev_from_payload is not None and prev_from_payload >= prev.round_kills:
                old_kills = prev_from_payload
        elif prev is not None and same_map:
            # round number changed between two own payloads: the counter can only be higher
            # than our last value if kills happened (late round-ending kills or new-round kills)
            if prev_from_payload is not None and prev_from_payload < round_kills:
                old_kills = prev_from_payload
            elif round_kills > prev.round_kills:
                old_kills = prev.round_kills

        live = st.map_phase in ACTIVE_PHASES and st.round_phase != "freezetime"
        if live and old_kills is not None and round_kills > old_kills:
            delta = round_kills - old_kills
            hs_delta = max(0, round_killhs - prev.round_killhs) if (same_round and prev) else 0
            weapon_raw = (prev.active_raw if same_map and prev and prev.active_raw else None) or active_raw
            if same_map and prev and prev.active_raw and active_raw and prev.active_raw != active_raw:
                # quick-switched right after the kill: the previous weapon is the killer,
                # unless the previous one was a grenade and we are now on a gun.
                if W.is_grenade(prev.active_raw):
                    weapon_raw = active_raw
            weapon = W.from_gsi_name(weapon_raw)
            ambiguous = weapon_raw if W.is_grenade(weapon_raw) else self._grenade_ambiguity(now)
            for i in range(delta):
                events.append({
                    "type": "kill",
                    "weapon": weapon.id if weapon else None,
                    "raw": weapon_raw,
                    "headshot": i < hs_delta,
                    "ambiguous": ambiguous,
                    "index": i,
                    "count": delta,
                })

        self._prev = _OwnSnapshot(
            t=now,
            map_name=st.map_name,
            round=st.round,
            round_kills=round_kills,
            round_killhs=round_killhs,
            match_kills=match_kills,
            active_raw=active_raw,
            grenades=grenades,
            alive=alive,
            round_phase=st.round_phase,
            names=names,
            money=money,
        )
        events.append({"type": "status"})
        return events

    @staticmethod
    def _death_event(raw: str | None) -> dict:
        w = W.from_gsi_name(raw)
        return {"type": "death", "weapon": w.id if w else None, "raw": raw}

    def tick(self, now: float | None = None, timeout: float = 30.0) -> bool:
        """Mark the connection stale when no payload arrived for ``timeout`` seconds.
        Returns True if the status changed."""
        now = time.time() if now is None else now
        st = self.status
        if st.connected and st.last_payload_at and now - st.last_payload_at > timeout:
            st.connected = False
            st.clear_player()
            self._prev = None
            self._was_alive = False
            return True
        return False


def build_cfg(uri: str, token: str) -> str:
    """Valve KeyValues config for CS2 (no BOM!) tuned for low latency on localhost."""
    return (
        '"Hardmode"\n'
        "{\n"
        f'  "uri"        "{uri}"\n'
        '  "timeout"    "1.0"\n'    # how long the game waits for our 2xx; short so a dead endpoint never stalls it
        '  "buffer"     "0.03"\n'   # coalesce changes within 30 ms into one payload (a kill touches several fields)
        '  "throttle"   "0.05"\n'   # at most ~20 payloads/s instead of a burst per field
        '  "heartbeat"  "10.0"\n'
        '  "auth"\n'
        "  {\n"
        f'    "token"  "{token}"\n'
        "  }\n"
        '  "data"\n'
        "  {\n"
        '    "provider"            "1"\n'
        '    "map"                 "1"\n'
        '    "round"               "1"\n'
        '    "player_id"           "1"\n'
        '    "player_state"        "1"\n'
        '    "player_weapons"      "1"\n'
        '    "player_match_stats"  "1"\n'
        "  }\n"
        "}\n"
    )
