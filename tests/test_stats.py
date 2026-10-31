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
    assert w["ak47"]["kills"] == 1 and w["ak47"]["hs"] == 1 and w["ak47"]["bought"] == 1 and w["ak47"]["deaths"] == 1
    assert w["hegrenade"]["kills"] == 1 and w["hegrenade"]["name"] == "HE"
    assert w["awp"]["picked"] == 1 and "glock" not in w          # spawn pistol is not counted
    assert snap["weapons"][0]["key"] in ("ak47", "hegrenade")   # sorted by kills
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
