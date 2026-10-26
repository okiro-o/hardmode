"""CS2 weapon catalog used by the loadout randomizer, the roulette and the overlays.

Facts encoded here (verified against items_game.txt and Valve's 2023-06-06 loadout
notes, still valid as of the 2026-10-22 patch):

* The CS2 loadout has, per team: 1 starting pistol slot, 4 pistol slots,
  5 mid-tier slots (SMG + heavy merged) and 5 rifle slots.
* No CS:GO-style mutual exclusions remain (CZ75 + Tec-9, Deagle + R8, M4A4 + M4A1-S
  can all be equipped together). The only either/or left is the CT starting pistol.
* GSI reports weapons by their internal name (``weapon_m4a1`` is the M4A4,
  ``weapon_m4a1_silencer`` is the M4A1-S, ``weapon_hkp2000`` is the P2000, ...).
* Icon files are the official Panorama kill-feed silhouettes, named after the
  internal name without the ``weapon_`` prefix.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

T, CT, BOTH = "T", "CT", "BOTH"

CATEGORY_STARTING = "starting"
CATEGORY_PISTOL = "pistol"
CATEGORY_MID = "mid"
CATEGORY_RIFLE = "rifle"
CATEGORY_EXTRA = "extra"  # knife / zeus - not part of the 15 loadout slots

LOADOUT_CATEGORIES = (CATEGORY_STARTING, CATEGORY_PISTOL, CATEGORY_MID, CATEGORY_RIFLE)

# How many slots each category has per team (starting pistol is exactly one weapon).
SLOT_COUNTS = {CATEGORY_PISTOL: 4, CATEGORY_MID: 5, CATEGORY_RIFLE: 5}

CATEGORY_TITLES_RU = {
    CATEGORY_STARTING: "Стартовый пистолет",
    CATEGORY_PISTOL: "Пистолеты",
    CATEGORY_MID: "Мид-тир (ПП и тяжёлое)",
    CATEGORY_RIFLE: "Винтовки",
    CATEGORY_EXTRA: "Прочее",
}


@dataclass(frozen=True)
class Weapon:
    id: str          # short id == icon basename, e.g. "ak47"
    name: str        # display name, e.g. "AK-47"
    team: str        # T | CT | BOTH
    category: str    # starting | pistol | mid | rifle | extra
    kind: str        # pistol | smg | shotgun | mg | rifle | sniper | melee | equipment

    @property
    def gsi_name(self) -> str:
        return "weapon_" + self.id

    @property
    def icon(self) -> str:
        return f"/static/icons/{self.id}.svg"

    def usable_by(self, side: str) -> bool:
        return self.team == BOTH or self.team == side

    @property
    def aspect(self) -> float:
        """width / height of the silhouette (from the SVG viewBox), used for tight overlay layout."""
        return icon_aspect(self.id)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "team": self.team,
            "category": self.category,
            "kind": self.kind,
            "gsi": self.gsi_name,
            "icon": self.icon,
            "aspect": round(self.aspect, 4),
        }


def _w(id: str, name: str, team: str, category: str, kind: str) -> Weapon:
    return Weapon(id, name, team, category, kind)


# Order matters: it is the canonical display order (roughly the in-game buy menu order).
CATALOG: tuple[Weapon, ...] = (
    # --- starting pistols -------------------------------------------------
    _w("glock", "Glock-18", T, CATEGORY_STARTING, "pistol"),
    _w("usp_silencer", "USP-S", CT, CATEGORY_STARTING, "pistol"),
    _w("hkp2000", "P2000", CT, CATEGORY_STARTING, "pistol"),
    # --- pistols (4 slots, pool of 6 per side) ---------------------------
    _w("p250", "P250", BOTH, CATEGORY_PISTOL, "pistol"),
    _w("elite", "Dual Berettas", BOTH, CATEGORY_PISTOL, "pistol"),
    _w("tec9", "Tec-9", T, CATEGORY_PISTOL, "pistol"),
    _w("fiveseven", "Five-SeveN", CT, CATEGORY_PISTOL, "pistol"),
    _w("cz75a", "CZ75-Auto", BOTH, CATEGORY_PISTOL, "pistol"),
    _w("deagle", "Desert Eagle", BOTH, CATEGORY_PISTOL, "pistol"),
    _w("revolver", "R8 Revolver", BOTH, CATEGORY_PISTOL, "pistol"),
    # --- mid-tier (5 slots, pool of 11 per side) --------------------------
    _w("mac10", "MAC-10", T, CATEGORY_MID, "smg"),
    _w("mp9", "MP9", CT, CATEGORY_MID, "smg"),
    _w("mp7", "MP7", BOTH, CATEGORY_MID, "smg"),
    _w("mp5sd", "MP5-SD", BOTH, CATEGORY_MID, "smg"),
    _w("ump45", "UMP-45", BOTH, CATEGORY_MID, "smg"),
    _w("p90", "P90", BOTH, CATEGORY_MID, "smg"),
    _w("bizon", "PP-Bizon", BOTH, CATEGORY_MID, "smg"),
    _w("nova", "Nova", BOTH, CATEGORY_MID, "shotgun"),
    _w("xm1014", "XM1014", BOTH, CATEGORY_MID, "shotgun"),
    _w("sawedoff", "Sawed-Off", T, CATEGORY_MID, "shotgun"),
    _w("mag7", "MAG-7", CT, CATEGORY_MID, "shotgun"),
    _w("m249", "M249", BOTH, CATEGORY_MID, "mg"),
    _w("negev", "Negev", BOTH, CATEGORY_MID, "mg"),
    # --- rifles (5 slots, pool of 6 on T / 7 on CT) ----------------------
    _w("galilar", "Galil AR", T, CATEGORY_RIFLE, "rifle"),
    _w("famas", "FAMAS", CT, CATEGORY_RIFLE, "rifle"),
    _w("ak47", "AK-47", T, CATEGORY_RIFLE, "rifle"),
    _w("m4a1", "M4A4", CT, CATEGORY_RIFLE, "rifle"),
    _w("m4a1_silencer", "M4A1-S", CT, CATEGORY_RIFLE, "rifle"),
    _w("sg556", "SG 553", T, CATEGORY_RIFLE, "rifle"),
    _w("aug", "AUG", CT, CATEGORY_RIFLE, "rifle"),
    _w("ssg08", "SSG 08", BOTH, CATEGORY_RIFLE, "sniper"),
    _w("awp", "AWP", BOTH, CATEGORY_RIFLE, "sniper"),
    _w("g3sg1", "G3SG1", T, CATEGORY_RIFLE, "sniper"),
    _w("scar20", "SCAR-20", CT, CATEGORY_RIFLE, "sniper"),
    # --- extras (own slots in game, optional in the roulette pool) --------
    _w("taser", "Zeus x27", BOTH, CATEGORY_EXTRA, "equipment"),
    _w("knife", "Нож", BOTH, CATEGORY_EXTRA, "melee"),
)

BY_ID: dict[str, Weapon] = {w.id: w for w in CATALOG}
ORDER: dict[str, int] = {w.id: i for i, w in enumerate(CATALOG)}

GRENADE_GSI_NAMES = {
    "weapon_hegrenade",
    "weapon_flashbang",
    "weapon_smokegrenade",
    "weapon_molotov",
    "weapon_incgrenade",
    "weapon_decoy",
}
LETHAL_GRENADES_HE = {"weapon_hegrenade"}
LETHAL_GRENADES_FIRE = {"weapon_molotov", "weapon_incgrenade"}


@lru_cache(maxsize=None)
def icon_aspect(weapon_id: str) -> float:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "icons", f"{weapon_id}.svg")
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            head = f.read(4000)
    except OSError:
        return 2.5
    m = re.search(r'viewBox="\s*[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)', head)
    if not m:
        return 2.5
    try:
        w, h = float(m.group(1)), float(m.group(2))
        return max(0.5, min(5.0, w / h)) if h > 0 else 2.5
    except ValueError:
        return 2.5


def get(weapon_id: str) -> Weapon:
    return BY_ID[weapon_id]


def pool(side: str, category: str) -> list[Weapon]:
    """All weapons of a category that the given side can equip, canonical order."""
    return [w for w in CATALOG if w.category == category and w.usable_by(side)]


def sort_ids(ids: Iterable[str]) -> list[str]:
    return sorted(ids, key=lambda i: ORDER.get(i, 10_000))


def from_gsi_name(gsi_name: str | None) -> Weapon | None:
    """Map a GSI ``weapon_*`` name to a catalog entry (None for grenades/C4/unknown)."""
    if not gsi_name or not gsi_name.startswith("weapon_"):
        return None
    short = gsi_name[len("weapon_"):]
    if short in BY_ID:
        return BY_ID[short]
    # Knives report their own model names: weapon_knife_karambit, weapon_bayonet, ...
    if short.startswith("knife") or short == "bayonet" or short.endswith("_knife"):
        return BY_ID["knife"]
    if short == "knife_t":
        return BY_ID["knife"]
    return None


def is_grenade(gsi_name: str | None) -> bool:
    return bool(gsi_name) and gsi_name in GRENADE_GSI_NAMES


def all_as_dicts() -> list[dict]:
    return [w.to_dict() for w in CATALOG]
