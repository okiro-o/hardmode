"""Recording of real GSI payloads for the developer (0.35.0, agreed with the user).

Started only by ``run-gsi-record.bat`` (environment ``CS2R_GSI_RECORD=1``), never from the panel. Payloads go
to ``data/gsi-record.jsonl`` as ``{"t": unix time, "p": payload}`` lines, without the auth token. Only
payloads where something the app reads changed are kept (phase, round, score, side, own/teammate block,
counters, inventory, the gun in hand), so a match takes a few MB instead of every packet at 20 per second.
The real packets let the developer check match results, predictions, MVPs and Deathmatch rounds against
the game instead of the documentation, and become test fixtures later."""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("hardmode.gsi_record")

MAX_BYTES = 50 * 1024 * 1024        # a stop for a recording left on by mistake


def _o(v) -> dict:
    return v if isinstance(v, dict) else {}


def payload_key(p: dict) -> tuple:
    """What the app reads from a payload; a new value means the payload is worth keeping."""
    m, r, pl, prov = _o(p.get("map")), _o(p.get("round")), _o(p.get("player")), _o(p.get("provider"))
    st, ms = _o(pl.get("state")), _o(pl.get("match_stats"))
    weapons = [w for w in _o(pl.get("weapons")).values() if isinstance(w, dict)]
    return (m.get("name"), m.get("mode"), m.get("phase"), m.get("round"),
            _o(m.get("team_ct")).get("score"), _o(m.get("team_t")).get("score"),
            r.get("phase"), r.get("win_team"), r.get("bomb"),
            pl.get("steamid") == prov.get("steamid"), pl.get("steamid"), pl.get("team"), pl.get("activity"),
            (st.get("health") or 0) > 0, st.get("round_kills"), st.get("round_killhs"),
            json.dumps(ms, sort_keys=True),
            tuple(sorted(f"{w.get('name')}:{w.get('ammo_reserve', '')}" for w in weapons if w.get("type") == "Grenade")),
            tuple(sorted(str(w.get("name")) for w in weapons)),
            next((w.get("name") for w in weapons if w.get("state") == "active"), None))


class GsiRecorder:
    def __init__(self, path: str, max_bytes: int = MAX_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.enabled = True
        self.saved = 0
        self._last: tuple | None = None
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        log.info("GSI payloads are recorded to %s", path)

    def record(self, payload: dict, now: float | None = None) -> bool:
        """Append the payload when it differs from the last kept one. Returns True when written."""
        if not self.enabled or not isinstance(payload, dict):
            return False
        key = payload_key(payload)
        if key == self._last:
            return False
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) >= self.max_bytes:
                self.enabled = False
                log.warning("GSI recording stopped: %s reached %d MB", self.path, self.max_bytes // (1024 * 1024))
                return False
            clean = {k: v for k, v in payload.items() if k != "auth"}        # never the token from the cfg
            line = json.dumps({"t": round(time.time() if now is None else now, 3), "p": clean}, ensure_ascii=False)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except (OSError, TypeError, ValueError):
            self.enabled = False
            log.warning("GSI recording stopped", exc_info=True)
            return False
        self._last = key
        self.saved += 1
        return True
