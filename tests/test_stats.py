import types

import pytest

from app.stats import StatsTracker, weapon_key, weapon_name


def status(phase="live"):
    return types.SimpleNamespace(map_phase=phase)


def test_stats_counts_kills_deaths_pickups_rounds_and_dpi(tmp_path):
    s = StatsTracker(str(tmp_path / "stats.json"))
    s.on_dpi_roll(1200, "death"); s.on_dpi_applied(1200)
    s.handle_events([{"type": "round_start", "round": 1}], status(), 1200, now=100.0)
    s.handle_events([{"type": "pickup", "raw": "weapon_ak47", "weapon": "ak47", "bought": True, "spawn": False},
                     {"type": "pickup", "raw": "weapon_glock", "weapon": "glock", "bought": False, "spawn": True}], status(), 1200, now=101.0)
    s.handle_events([{"type": "kill", "weapon": "ak47", "raw": "weapon_ak47", "headshot": True},
                     {"type": "kill", "weapon": None, "raw": "weapon_hegrenade", "headshot": False}], status(), 1200, now=105.0)
    s.handle_events([{"type": "round_end", "round": 1}], status(), 1200, now=110.0)
    # round 2: the DPI changes mid-round (death -> new value), so it is not a "full round" for either value
    s.handle_events([{"type": "round_start", "round": 2}], status(), 1200, now=120.0)
    s.handle_events([{"type": "death", "weapon": "ak47", "raw": "weapon_ak47"}], status(), 1200, now=125.0)
    s.on_dpi_roll(800, "death"); s.on_dpi_applied(800)
    s.handle_events([{"type": "pickup", "raw": "weapon_awp", "weapon": "awp", "bought": False, "spawn": False}], status(), 800, now=130.0)
    s.handle_events([{"type": "round_end", "round": 2}], status(), 800, now=140.0)
    snap = s.snapshot()
    t = snap["totals"]
    assert (t["kills"], t["hs"], t["deaths"], t["rounds"], t["rolls"], t["best_round_kills"], t["bought"], t["picked"]) == (2, 1, 1, 2, 2, 2, 1, 1)
    d = {r["value"]: r for r in snap["dpi"]}
    assert d[1200]["kills"] == 2 and d[1200]["deaths"] == 1 and d[1200]["rounds"] == 1 and d[1200]["rolls"] == 1
    assert d[800]["rounds"] == 0 and d[800]["rolls"] == 1
    assert d[1200]["seconds"] == pytest.approx(10.0 + 5.0)   # 100->101->105->110 and 120->125 (gaps >= 10 s skipped)
    w = {r["key"]: r for r in snap["weapons"]}
    g = {r["key"]: r for r in snap["grenades"]}
    assert w["ak47"]["kills"] == 1 and w["ak47"]["hs"] == 1 and w["ak47"]["bought"] == 1 and w["ak47"]["deaths"] == 1
    assert g["hegrenade"]["kills"] == 1 and g["hegrenade"]["name"] == "HE" and "hegrenade" not in w
    assert w["awp"]["picked"] == 1 and "glock" not in w          # spawn pistol is not counted
    assert snap["weapons"][0]["key"] == "ak47"   # sorted by kills; grenades live in their own list
    # persistence (writes are throttled: flush before reopening) and reset
    s.flush()
    s2 = StatsTracker(str(tmp_path / "stats.json"))
    assert s2.snapshot()["totals"]["kills"] == 2
    s2.reset()
    assert s2.snapshot()["totals"]["kills"] == 0 and StatsTracker(str(tmp_path / "stats.json")).snapshot()["totals"]["kills"] == 0


def test_weapon_key_and_name():
    assert weapon_key("weapon_ak47", "ak47") == "ak47" and weapon_key("weapon_molotov", None) == "molotov"
    assert weapon_key("weapon_healthshot", None) is None and weapon_key("weapon_knife_t", None) is None
    assert weapon_key("weapon_c4", None) is None
    assert weapon_name("ak47") == "AK-47" and weapon_name("molotov") == "Molotov" and weapon_name("x") == "x"




def test_stats_api_and_gsi_feed(client):
    from tests.test_gsi import payload
    assert client.get("/api/stats").json()["totals"]["kills"] == 0
    token = client.get("/api/state").json()["gsi_token"]
    for p in (payload(rk=0, active="weapon_ak47"), payload(rk=1, hs=1, active="weapon_ak47")):
        p["auth"] = {"token": token}
        client.post("/gsi", json=p)
    st = client.get("/api/stats").json()
    assert st["totals"]["kills"] == 1 and st["weapons"][0]["key"] == "ak47"
    assert client.get("/api/state").json()["stats"]["totals"]["kills"] == 1
    assert client.post("/api/stats/reset", json={}).json()["ok"]
    assert client.get("/api/stats").json()["totals"]["kills"] == 0


def test_non_weapons_are_not_booked(tmp_path):
    s = StatsTracker(str(tmp_path / "stats.json"))
    s.handle_events([{"type": "pickup", "raw": "weapon_healthshot", "weapon": None, "bought": True, "spawn": False},
                     {"type": "death", "weapon": None, "raw": "weapon_healthshot"}], status(), 800, now=1.0)
    snap = s.snapshot()
    assert snap["weapons"] == [] and snap["totals"]["deaths"] == 1 and snap["totals"]["bought"] == 0   # not a weapon purchase
    # legacy entries are dropped on load
    s.data["weapons"]["healthshot"] = {"name": "healthshot", "bought": 3, "picked": 0, "kills": 0, "hs": 0, "deaths": 0}
    s.flush()
    assert "healthshot" not in StatsTracker(str(tmp_path / "stats.json")).data["weapons"]


def test_assists_and_maps(tmp_path):
    s = StatsTracker(str(tmp_path / "stats.json"))
    mir = types.SimpleNamespace(map_phase="live", map_name="de_mirage")
    s.handle_events([{"type": "round_start", "round": 1}], mir, 800, now=1.0)
    s.handle_events([{"type": "kill", "weapon": "ak47", "raw": "weapon_ak47", "headshot": True},
                     {"type": "assist", "count": 1}], mir, 800, now=2.0)
    s.handle_events([{"type": "round_end", "round": 1}], mir, 800, now=3.0)
    inf = types.SimpleNamespace(map_phase="live", map_name="de_inferno")
    s.handle_events([{"type": "round_start", "round": 1}], inf, 800, now=10.0)
    s.handle_events([{"type": "death", "weapon": "awp", "raw": "weapon_awp"}], inf, 800, now=11.0)
    snap = s.snapshot()
    t = snap["totals"]
    assert (t["assists"], t["kills"], t["deaths"]) == (1, 1, 1) and "damage" not in t     # no damage since 0.29.0
    assert "damage" not in snap["dpi"][0]
    maps = {r["name"]: r for r in snap["maps"]}
    assert maps["de_mirage"]["title"] == "Mirage" and maps["de_mirage"]["matches"] == 1
    assert (maps["de_mirage"]["rounds"], maps["de_mirage"]["kills"], maps["de_mirage"]["assists"]) == (1, 1, 1)
    assert (maps["de_inferno"]["deaths"], maps["de_inferno"]["rounds"]) == (1, 0) and "damage" not in maps["de_inferno"]
    assert snap["maps"][0]["name"] == "de_mirage"        # sorted by rounds
    s.reset()
    assert s.snapshot()["maps"][0]["matches"] == 1 and s.snapshot()["maps"][0]["name"] == "de_inferno"


def test_wins_per_map_and_a_rematch_on_the_same_map(tmp_path):
    s = StatsTracker(str(tmp_path / "stats.json"))
    live = types.SimpleNamespace(map_phase="live", map_name="de_mirage")
    over = types.SimpleNamespace(map_phase="gameover", map_name="de_mirage")
    s.handle_events([{"type": "round_start", "round": 1}], live, 800, now=1.0)
    s.handle_events([{"type": "match_end", "result": "win"}], over, 800, now=2.0)
    s.handle_events([], over, 800, now=3.0)
    s.handle_events([], types.SimpleNamespace(map_phase="warmup", map_name="de_mirage"), 800, now=4.0)   # requeued
    s.handle_events([{"type": "match_end", "result": "loss"}], over, 800, now=5.0)
    s.handle_events([], live, 800, now=6.0)
    s.handle_events([{"type": "match_end", "result": None}], over, 800, now=7.0)     # deathmatch: no result
    m = s.snapshot()["maps"][0]
    assert (m["matches"], m["wins"], m["losses"], m["draws"]) == (3, 1, 1, 0)


def test_mvps_and_no_rounds_in_deathmatch(tmp_path):
    s = StatsTracker(str(tmp_path / "stats.json"))
    comp = types.SimpleNamespace(map_phase="live", map_name="de_mirage", mode="competitive")
    s.handle_events([{"type": "round_start", "round": 1}], comp, 800, now=1.0)
    s.handle_events([{"type": "kill", "weapon": "ak47", "raw": "weapon_ak47"}] * 3, comp, 800, now=2.0)
    s.handle_events([{"type": "round_end", "round": 1}, {"type": "mvp", "count": 1}], comp, 800, now=3.0)
    dm = types.SimpleNamespace(map_phase="live", map_name="de_dust2", mode="deathmatch")
    s.handle_events([{"type": "round_start", "round": 0}], dm, 800, now=10.0)        # the whole DM match
    s.handle_events([{"type": "kill", "weapon": "ak47", "raw": "weapon_ak47"}] * 25, dm, 800, now=11.0)
    s.handle_events([{"type": "round_end", "round": 0}], dm, 800, now=12.0)
    t = s.snapshot()["totals"]
    assert (t["mvps"], t["best_round_kills"], t["rounds"], t["kills"]) == (1, 3, 1, 28)
    assert {r["name"]: r["rounds"] for r in s.snapshot()["maps"]} == {"de_mirage": 1, "de_dust2": 0}


def test_grenades_are_listed_separately_with_throws(tmp_path):
    s = StatsTracker(str(tmp_path / "stats.json"))
    s.handle_events([{"type": "pickup", "raw": "weapon_flashbang", "weapon": None, "bought": True, "spawn": False},
                     {"type": "throw", "raw": "weapon_flashbang", "weapon": None},
                     {"type": "throw", "raw": "weapon_flashbang", "weapon": None},
                     {"type": "pickup", "raw": "weapon_ak47", "weapon": "ak47", "bought": True, "spawn": False}], status(), 800, now=1.0)
    snap = s.snapshot()
    assert [r["key"] for r in snap["weapons"]] == ["ak47"]
    g = {r["key"]: r for r in snap["grenades"]}
    assert g["flashbang"]["thrown"] == 2 and g["flashbang"]["bought"] == 1
    assert snap["grenades"][0]["key"] == "flashbang"      # sorted by throws


def test_old_stats_file_loses_damage(tmp_path):
    import json
    p = tmp_path / "stats.json"
    row = {"name": "AK-47", "bought": 1, "picked": 0, "kills": 3, "hs": 1, "deaths": 0, "damage": 250}   # 0.17.0 file
    p.write_text(json.dumps({"totals": {"kills": 3, "damage": 250},
                             "dpi": {"800": {"rolls": 1, "kills": 3, "deaths": 0, "rounds": 0, "seconds": 0, "damage": 250}},
                             "maps": {"de_mirage": {"matches": 1, "rounds": 2, "kills": 3, "damage": 250}},
                             "weapons": {"ak47": row}}), encoding="utf-8")
    snap = StatsTracker(str(p)).snapshot()
    assert "damage" not in snap["totals"] and "damage" not in snap["dpi"][0] and "damage" not in snap["maps"][0]
    assert snap["totals"]["kills"] == 3 and snap["maps"][0]["rounds"] == 2 and snap["maps"][0]["wins"] == 0
    assert "damage" not in snap["weapons"][0] and snap["weapons"][0]["kills"] == 3
