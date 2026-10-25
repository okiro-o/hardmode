import random

import pytest

from app import loadout as L
from app import weapons as W


@pytest.mark.parametrize("seed", range(25))
def test_generate_respects_cs2_slot_rules(seed):
    lo = L.generate(random.Random(seed))
    assert L.validate(lo) == []
    for side in ("T", "CT"):
        s = lo[side]
        assert len(s["pistol"]) == 4 and len(s["mid"]) == 5 and len(s["rifle"]) == 5
        for cat in ("pistol", "mid", "rifle"):
            assert len(set(s[cat])) == len(s[cat])
            for wid in s[cat]:
                assert W.get(wid).usable_by(side)
                assert W.get(wid).category == cat
    assert lo["T"]["starting"] == "glock"
    assert lo["CT"]["starting"] in ("usp_silencer", "hkp2000")


def test_pool_sizes_match_research():
    assert len(W.pool("T", W.CATEGORY_PISTOL)) == 6
    assert len(W.pool("CT", W.CATEGORY_PISTOL)) == 6
    assert len(W.pool("T", W.CATEGORY_MID)) == 11
    assert len(W.pool("CT", W.CATEGORY_MID)) == 11
    assert len(W.pool("T", W.CATEGORY_RIFLE)) == 6
    assert len(W.pool("CT", W.CATEGORY_RIFLE)) == 7
    assert [w.id for w in W.pool("T", W.CATEGORY_STARTING)] == ["glock"]
    assert {w.id for w in W.pool("CT", W.CATEGORY_STARTING)} == {"usp_silencer", "hkp2000"}


def test_side_exclusive_weapons():
    t_only = {"glock", "tec9", "mac10", "sawedoff", "galilar", "ak47", "sg556", "g3sg1"}
    ct_only = {"usp_silencer", "hkp2000", "fiveseven", "mp9", "mag7", "famas", "m4a1", "m4a1_silencer", "aug", "scar20"}
    for wid in t_only:
        assert W.get(wid).team == "T"
    for wid in ct_only:
        assert W.get(wid).team == "CT"


def test_ct_starting_forced():
    lo = L.generate(random.Random(1), ct_starting="hkp2000")
    assert lo["CT"]["starting"] == "hkp2000"
    lo = L.generate(random.Random(1), ct_starting="usp_silencer")
    assert lo["CT"]["starting"] == "usp_silencer"


def test_replace_weapon_swaps_to_unused_option():
    rng = random.Random(3)
    lo = L.generate(rng)
    before = list(lo["T"]["rifle"])
    victim = before[0]
    new = L.replace_weapon(lo, "T", "rifle", victim, rng)
    after = new["T"]["rifle"]
    assert victim not in after
    assert len(after) == 5 and len(set(after)) == 5
    assert L.validate(new) == []
    assert lo["T"]["rifle"] == before  # input not mutated


def test_replace_starting_toggles_ct_pistol():
    rng = random.Random(0)
    lo = L.generate(rng, ct_starting="usp_silencer")
    new = L.replace_weapon(lo, "CT", "starting", "usp_silencer", rng)
    assert new["CT"]["starting"] == "hkp2000"
    same = L.replace_weapon(lo, "T", "starting", "glock", rng)
    assert same["T"]["starting"] == "glock"


def test_side_pool_ids():
    lo = L.generate(random.Random(5))
    ids = L.side_pool_ids(lo, "T")
    assert len(ids) == 1 + 4 + 5 + 5
    assert "glock" in ids
    ids2 = L.side_pool_ids(lo, "T", include_starting=False, include_extras=True)
    assert "glock" not in ids2 and "knife" in ids2 and "taser" in ids2


def test_gsi_name_mapping():
    assert W.from_gsi_name("weapon_m4a1").name == "M4A4"
    assert W.from_gsi_name("weapon_m4a1_silencer").name == "M4A1-S"
    assert W.from_gsi_name("weapon_hkp2000").name == "P2000"
    assert W.from_gsi_name("weapon_knife_karambit").id == "knife"
    assert W.from_gsi_name("weapon_knife_t").id == "knife"
    assert W.from_gsi_name("weapon_bayonet").id == "knife"
    assert W.from_gsi_name("weapon_hegrenade") is None
    assert W.from_gsi_name(None) is None
    assert W.is_grenade("weapon_molotov")


def test_every_catalog_weapon_has_an_icon_file():
    import os
    from app import config
    icons = os.path.join(config.package_dir(), "static", "icons")
    for w in W.CATALOG:
        assert os.path.isfile(os.path.join(icons, w.id + ".svg")), w.id
