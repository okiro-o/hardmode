"""Random loadout generation following the CS2 loadout rules.

A loadout is a plain dict so it can be stored as JSON::

    {
      "T":  {"starting": "glock",        "pistol": [...4 ids], "mid": [...5 ids], "rifle": [...5 ids]},
      "CT": {"starting": "usp_silencer", "pistol": [...4 ids], "mid": [...5 ids], "rifle": [...5 ids]},
    }
"""
from __future__ import annotations

import random
from typing import Iterable

from . import weapons as W

SIDES = (W.T, W.CT)


def generate_side(side: str, rng: random.Random, ct_starting: str = "random") -> dict:
    """Generate one side's loadout.

    ``ct_starting``: "random" | "usp_silencer" | "hkp2000" (ignored for T).
    """
    if side not in SIDES:
        raise ValueError(f"unknown side {side!r}")
    starting_pool = W.pool(side, W.CATEGORY_STARTING)
    if side == W.CT and ct_starting in ("usp_silencer", "hkp2000"):
        starting = ct_starting
    else:
        starting = rng.choice(starting_pool).id
    result = {W.CATEGORY_STARTING: starting}
    for category, count in W.SLOT_COUNTS.items():
        ids = [w.id for w in W.pool(side, category)]
        if count > len(ids):
            raise RuntimeError(f"pool for {side}/{category} smaller than slot count")
        result[category] = W.sort_ids(rng.sample(ids, count))
    return result


def generate(rng: random.Random, ct_starting: str = "random", sides: Iterable[str] = SIDES) -> dict:
    return {side: generate_side(side, rng, ct_starting) for side in sides}


def replace_weapon(loadout: dict, side: str, category: str, weapon_id: str, rng: random.Random) -> dict:
    """Replace one weapon of a side/category with a random weapon not currently equipped.

    For the starting pistol on CT this toggles USP-S <-> P2000. Returns the new loadout
    (the input dict is not mutated).
    """
    if side not in loadout:
        raise ValueError(f"loadout has no side {side!r}")
    new = {s: {k: (list(v) if isinstance(v, list) else v) for k, v in d.items()} for s, d in loadout.items()}
    side_lo = new[side]
    if category == W.CATEGORY_STARTING:
        options = [w.id for w in W.pool(side, W.CATEGORY_STARTING) if w.id != side_lo[W.CATEGORY_STARTING]]
        if options:
            side_lo[W.CATEGORY_STARTING] = rng.choice(options)
        return new
    if category not in W.SLOT_COUNTS:
        raise ValueError(f"unknown category {category!r}")
    current = side_lo[category]
    if weapon_id not in current:
        raise ValueError(f"{weapon_id} is not in {side}/{category}")
    options = [w.id for w in W.pool(side, category) if w.id not in current]
    if not options:
        return new  # nothing to swap to (cannot happen with current pools)
    idx = current.index(weapon_id)
    current[idx] = rng.choice(options)
    side_lo[category] = W.sort_ids(current)
    return new


def validate(loadout: dict) -> list[str]:
    """Return a list of human-readable problems (empty list == valid)."""
    problems: list[str] = []
    for side in SIDES:
        side_lo = loadout.get(side)
        if not isinstance(side_lo, dict):
            problems.append(f"{side}: отсутствует")
            continue
        starting = side_lo.get(W.CATEGORY_STARTING)
        if starting not in {w.id for w in W.pool(side, W.CATEGORY_STARTING)}:
            problems.append(f"{side}: неверный стартовый пистолет {starting!r}")
        for category, count in W.SLOT_COUNTS.items():
            ids = side_lo.get(category)
            if not isinstance(ids, list) or len(ids) != count:
                problems.append(f"{side}/{category}: нужно {count} слотов")
                continue
            if len(set(ids)) != len(ids):
                problems.append(f"{side}/{category}: дубликаты")
            allowed = {w.id for w in W.pool(side, category)}
            for wid in ids:
                if wid not in allowed:
                    problems.append(f"{side}/{category}: {wid!r} недоступно этой стороне")
    return problems


def side_pool_ids(loadout: dict, side: str, include_starting: bool = True, include_extras: bool = False) -> list[str]:
    """Flat list of weapon ids the roulette may draw from for a side."""
    side_lo = loadout[side]
    ids: list[str] = []
    if include_starting:
        ids.append(side_lo[W.CATEGORY_STARTING])
    for category in (W.CATEGORY_PISTOL, W.CATEGORY_MID, W.CATEGORY_RIFLE):
        ids.extend(side_lo[category])
    if include_extras:
        ids.extend(w.id for w in W.pool(side, W.CATEGORY_EXTRA))
    return W.sort_ids(dict.fromkeys(ids))


def as_text(loadout: dict, side: str | None = None) -> str:
    """Human readable summary, handy to copy into chat / notes."""
    lines: list[str] = []
    for s in SIDES if side is None else (side,):
        if s not in loadout:
            continue
        lines.append(f"[{s}]")
        for category in W.LOADOUT_CATEGORIES:
            value = loadout[s][category]
            ids = [value] if isinstance(value, str) else value
            names = ", ".join(W.get(i).name for i in ids)
            lines.append(f"  {W.CATEGORY_TITLES_RU[category]}: {names}")
    return "\n".join(lines)
