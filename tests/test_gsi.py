from app.gsi import GsiTracker, build_cfg

ME = "76561198000000001"
MATE = "76561198000000002"


def payload(*, steamid=ME, team="T", round_=3, rk=0, hs=0, active="weapon_ak47", phase="live",
            rphase="live", weapons=None, previously=None, grenades=None, map_name="de_mirage"):
    ws = {}
    if weapons is None:
        weapons = {
            "weapon_0": {"name": "weapon_knife_t", "type": "Knife", "state": "holstered"},
            "weapon_1": {"name": "weapon_glock", "type": "Pistol", "state": "holstered"},
            "weapon_2": {"name": "weapon_ak47", "type": "Rifle", "state": "holstered"},
        }
        for g, n in (grenades or {}).items():
            weapons[f"weapon_{len(weapons)}"] = {"name": g, "type": "Grenade", "state": "holstered", "ammo_reserve": n}
    for k, v in weapons.items():
        v = dict(v)
        v["state"] = "active" if v["name"] == active else ("holstered" if v.get("state") != "reloading" else v["state"])
        ws[k] = v
    p = {
        "provider": {"name": "Counter-Strike 2", "appid": 730, "steamid": ME, "timestamp": 1},
        "map": {"mode": "competitive", "name": map_name, "phase": phase, "round": round_},
        "round": {"phase": rphase},
        "player": {
            "steamid": steamid, "name": "me" if steamid == ME else "mate", "team": team, "activity": "playing",
            "state": {"health": 100, "round_kills": rk, "round_killhs": hs},
            "weapons": ws,
            "match_stats": {"kills": 10 + rk},
        },
    }
    if previously is not None:
        p["previously"] = previously
    return p


def kills(events):
    return [e for e in events if e["type"] == "kill"]


def test_kill_attributed_to_previous_active_weapon():
    t = GsiTracker()
    t.handle(payload(rk=0, active="weapon_ak47"), now=10.0)
    ev = kills(t.handle(payload(rk=1, hs=1, active="weapon_ak47"), now=10.1))
    assert len(ev) == 1
    assert ev[0]["weapon"] == "ak47" and ev[0]["headshot"] and ev[0]["ambiguous"] is None


def test_quick_switch_after_kill_uses_previous_weapon():
    t = GsiTracker()
    t.handle(payload(rk=0, active="weapon_ak47"), now=1.0)
    ev = kills(t.handle(payload(rk=1, active="weapon_glock"), now=1.1))
    assert ev[0]["weapon"] == "ak47"


def test_first_payload_never_fires():
    t = GsiTracker()
    assert kills(t.handle(payload(rk=3), now=1.0)) == []


def test_side_event_and_team_change():
    t = GsiTracker()
    ev = t.handle(payload(team="T"), now=1.0)
    assert {"type": "side", "side": "T"} in ev
    ev = t.handle(payload(team="T"), now=1.1)
    assert not any(e["type"] == "side" for e in ev)
    ev = t.handle(payload(team="CT"), now=1.2)
    assert {"type": "side", "side": "CT"} in ev


def test_spectated_teammate_is_ignored():
    t = GsiTracker()
    t.handle(payload(rk=0), now=1.0)
    ev = t.handle(payload(steamid=MATE, rk=2, team="CT"), now=1.1)
    assert kills(ev) == []
    assert t.status.own_player is False
    assert not any(e["type"] == "side" for e in ev)  # teammate's team must not switch our side
    # back alive next round: baseline re-established, no phantom kill
    ev = t.handle(payload(rk=0, round_=4), now=60.0)
    assert kills(ev) == []


def test_round_reset_is_not_a_kill():
    t = GsiTracker()
    t.handle(payload(rk=2, round_=3), now=1.0)
    ev = t.handle(payload(rk=0, round_=4, rphase="freezetime"), now=2.0)
    assert kills(ev) == []
    ev = t.handle(payload(rk=1, round_=4), now=3.0)
    assert len(kills(ev)) == 1


def test_warmup_kills_ignored():
    t = GsiTracker()
    t.handle(payload(rk=0, phase="warmup"), now=1.0)
    assert kills(t.handle(payload(rk=1, phase="warmup"), now=1.1)) == []


def test_round_over_phase_still_counts_final_kill():
    t = GsiTracker()
    t.handle(payload(rk=1), now=1.0)
    ev = kills(t.handle(payload(rk=2, rphase="over"), now=1.1))
    assert len(ev) == 1


def test_multi_kill_delta_emits_multiple_events():
    t = GsiTracker()
    t.handle(payload(rk=0, hs=0), now=1.0)
    ev = kills(t.handle(payload(rk=2, hs=1), now=1.1))
    assert len(ev) == 2
    assert ev[0]["headshot"] is True and ev[1]["headshot"] is False
    assert ev[0]["count"] == 2


def test_previously_block_recovers_missed_payload():
    t = GsiTracker()
    t.handle(payload(rk=0), now=1.0)
    # we missed the payload with rk=1; the game tells us the old value was 1
    ev = kills(t.handle(payload(rk=2, previously={"player": {"state": {"round_kills": 1}}}), now=2.0))
    assert len(ev) == 1


def test_he_throw_makes_following_kill_ambiguous():
    t = GsiTracker()
    t.handle(payload(rk=0, active="weapon_hegrenade", grenades={"weapon_hegrenade": 1}), now=1.0)
    # grenade thrown: entry disappears, auto-switch to ak
    t.handle(payload(rk=0, active="weapon_ak47", grenades={}), now=1.2)
    ev = kills(t.handle(payload(rk=1, active="weapon_ak47", grenades={}), now=2.5))
    assert len(ev) == 1 and ev[0]["ambiguous"] == "weapon_hegrenade"
    # much later the window is closed
    t.handle(payload(rk=1, active="weapon_ak47"), now=20.0)
    ev = kills(t.handle(payload(rk=2, active="weapon_ak47"), now=20.1))
    assert ev[0]["ambiguous"] is None


def test_molotov_window_is_longer():
    t = GsiTracker()
    t.handle(payload(rk=0, active="weapon_molotov", grenades={"weapon_molotov": 1}), now=1.0)
    t.handle(payload(rk=0, active="weapon_ak47", grenades={}), now=1.3)
    ev = kills(t.handle(payload(rk=1, active="weapon_ak47"), now=7.0))
    assert ev[0]["ambiguous"] == "weapon_molotov"


def test_knife_kill_maps_to_knife():
    t = GsiTracker()
    t.handle(payload(rk=0, active="weapon_knife_t"), now=1.0)
    ev = kills(t.handle(payload(rk=1, active="weapon_knife_t"), now=1.1))
    assert ev[0]["weapon"] == "knife"


def test_auth_token_rejects_mismatch():
    t = GsiTracker(token="secret")
    p = payload(rk=0)
    assert t.handle(p, now=1.0) == []
    assert t.status.rejected_auth == 1
    p["auth"] = {"token": "secret"}
    assert t.handle(p, now=1.1)


def test_status_and_stale_tick():
    t = GsiTracker()
    t.handle(payload(rk=0, active="weapon_ak47"), now=100.0)
    st = t.status
    assert st.connected and st.held == "ak47" and st.team == "T" and st.map_name == "de_mirage"
    assert not t.tick(now=110.0)
    assert t.tick(now=200.0)
    assert not st.connected and st.held is None


def test_menu_payload_resets_baseline():
    t = GsiTracker()
    t.handle(payload(rk=2), now=1.0)
    t.handle({"provider": {"steamid": ME}, "player": {"steamid": ME, "activity": "menu"}}, now=2.0)
    ev = kills(t.handle(payload(rk=3), now=3.0))
    assert ev == []


def test_cfg_has_no_bom_and_required_sections():
    cfg = build_cfg("http://127.0.0.1:17890/gsi", "tok")
    assert not cfg.startswith("﻿")
    for key in ("player_weapons", "player_state", "player_match_stats", "provider", "map", "round", "player_id"):
        assert f'"{key}"' in cfg
    assert '"token"  "tok"' in cfg


def test_rejected_payload_keeps_player_name_for_diagnostics():
    t = GsiTracker(token="good")
    p = payload(rk=0)
    p["auth"] = {"token": "old"}
    t.handle(p, now=1.0)
    assert t.status.rejected_name == "me" and t.status.name is None
    p["auth"] = {"token": "good"}
    t.handle(p, now=2.0)
    assert t.status.name == "me"


def test_halftime_last_kill_and_death_still_count():
    # the final kill of a half arrives with map.phase already "intermission"
    t = GsiTracker()
    t.handle(payload(rk=1), now=1.0)
    ev = kills(t.handle(payload(rk=2, phase="intermission", rphase="over"), now=1.1))
    assert len(ev) == 1
    # dying as the last player of the last round of the half
    t2 = GsiTracker()
    t2.handle(payload(rk=0), now=1.0)
    p = payload(rk=0, phase="intermission", rphase="over")
    p["player"]["state"]["health"] = 0
    assert any(e["type"] == "death" for e in t2.handle(p, now=1.2))
    # warmup is still ignored
    t3 = GsiTracker()
    t3.handle(payload(rk=0, phase="warmup"), now=1.0)
    p = payload(rk=1, phase="warmup")
    p["player"]["state"]["health"] = 0
    ev = t3.handle(p, now=1.1)
    assert kills(ev) == [] and not any(e["type"] == "death" for e in ev)


def test_cfg_timing_is_gentle_on_the_game():
    cfg = build_cfg("http://127.0.0.1:17890/gsi", "tok")
    assert '"buffer"     "0.03"' in cfg and '"throttle"   "0.05"' in cfg and '"timeout"    "1.0"' in cfg


def _inv(*names, money=None):
    ws = {f"weapon_{i}": {"name": n, "type": "Rifle", "state": "holstered"} for i, n in enumerate(names)}
    p = payload(weapons=ws, active=names[-1])
    if money is not None:
        p["player"]["state"]["money"] = money
    return p


def test_inventory_events_bought_picked_and_spawn():
    t = GsiTracker()
    t.handle(_inv("weapon_knife_t", "weapon_glock", money=800), now=1.0)
    ev = t.handle(_inv("weapon_knife_t", "weapon_glock", "weapon_ak47", money=100), now=1.5)
    picks = [e for e in ev if e["type"] == "pickup"]
    assert picks == [{"type": "pickup", "raw": "weapon_ak47", "weapon": "ak47", "bought": True, "spawn": False}]
    ev = t.handle(_inv("weapon_knife_t", "weapon_glock", "weapon_ak47", "weapon_awp", money=100), now=2.0)
    picks = [e for e in ev if e["type"] == "pickup"]
    assert picks[0]["raw"] == "weapon_awp" and picks[0]["bought"] is False and picks[0]["spawn"] is False
    # death, then a fresh spawn with a default pistol: spawn equipment, not a pickup
    dead = _inv("weapon_knife_t", money=100); dead["player"]["state"]["health"] = 0
    t.handle(dead, now=3.0)
    p = _inv("weapon_knife_t", "weapon_glock", money=100); p["map"]["round"] = 4
    ev = t.handle(p, now=60.0)
    picks = [e for e in ev if e["type"] == "pickup"]
    assert picks and all(e["spawn"] for e in picks)
    # a second flashbang of the same kind counts as another purchase
    a = _inv("weapon_knife_t", "weapon_glock", money=1000)
    a["player"]["weapons"]["weapon_9"] = {"name": "weapon_flashbang", "type": "Grenade", "state": "holstered", "ammo_reserve": 1}
    t.handle(a, now=61.0)
    b = _inv("weapon_knife_t", "weapon_glock", money=800)
    b["player"]["weapons"]["weapon_9"] = {"name": "weapon_flashbang", "type": "Grenade", "state": "holstered", "ammo_reserve": 2}
    ev = t.handle(b, now=61.5)
    assert [e["raw"] for e in ev if e["type"] == "pickup"] == ["weapon_flashbang"]
    # warmup: nothing is counted
    w = GsiTracker()
    w.handle(_inv("weapon_knife_t", money=800), now=1.0)
    p = _inv("weapon_knife_t", "weapon_ak47", money=100); p["map"]["phase"] = "warmup"
    assert not [e for e in w.handle(p, now=1.1) if e["type"] == "pickup"]


def test_round_boundary_events_and_death_weapon():
    t = GsiTracker()
    ev = t.handle(payload(round_=3), now=1.0)
    assert {"type": "round_start", "round": 3} in ev
    ev = t.handle(payload(round_=3, rphase="over"), now=2.0)
    assert {"type": "round_end", "round": 3} in ev
    ev = t.handle(payload(round_=4, rphase="freezetime"), now=3.0)
    assert {"type": "round_start", "round": 4} in ev and not any(e["type"] == "round_end" for e in ev)
    # round number jumps without a seen "over": the old round is closed first
    ev = t.handle(payload(round_=5), now=4.0)
    kinds = [(e["type"], e.get("round")) for e in ev if e["type"].startswith("round")]
    assert kinds == [("round_end", 4), ("round_start", 5)]
    # death carries the weapon that was in hand
    t.handle(payload(active="weapon_ak47"), now=5.0)
    dead = payload(active="weapon_ak47"); dead["player"]["state"]["health"] = 0
    ev = t.handle(dead, now=5.5)
    d = [e for e in ev if e["type"] == "death"]
    assert d == [{"type": "death", "weapon": "ak47", "raw": "weapon_ak47"}]


def test_healthshot_and_c4_are_not_pickups():
    t = GsiTracker()
    t.handle(_inv("weapon_knife_t", "weapon_glock", money=5000), now=1.0)
    ev = t.handle(_inv("weapon_knife_t", "weapon_glock", "weapon_healthshot", "weapon_c4", money=4000), now=1.5)
    assert not [e for e in ev if e["type"] == "pickup"]


def test_damage_and_assist_events():
    t = GsiTracker()
    p = payload(rk=0, active="weapon_ak47"); p["player"]["state"]["round_totaldmg"] = 0; p["player"]["match_stats"]["assists"] = 2
    t.handle(p, now=1.0)
    p = payload(rk=0, active="weapon_ak47"); p["player"]["state"]["round_totaldmg"] = 27; p["player"]["match_stats"]["assists"] = 2
    ev = t.handle(p, now=1.1)
    assert [e for e in ev if e["type"] == "damage"] == [{"type": "damage", "amount": 27, "weapon": "ak47", "raw": "weapon_ak47"}]
    # quick switch after the hit: the weapon of the previous packet gets the damage
    p = payload(rk=0, active="weapon_glock"); p["player"]["state"]["round_totaldmg"] = 60; p["player"]["match_stats"]["assists"] = 3
    ev = t.handle(p, now=1.2)
    d = [e for e in ev if e["type"] == "damage"]; a = [e for e in ev if e["type"] == "assist"]
    assert d[0]["amount"] == 33 and d[0]["weapon"] == "ak47" and a == [{"type": "assist", "count": 1}]
    # new round resets round_totaldmg: no damage event, a match reset never produces a negative assist
    p = payload(rk=0, round_=4, rphase="freezetime", active="weapon_glock"); p["player"]["state"]["round_totaldmg"] = 0; p["player"]["match_stats"]["assists"] = 0
    ev = t.handle(p, now=5.0)
    assert not [e for e in ev if e["type"] in ("damage", "assist")]


def test_throw_events_for_any_grenade():
    t = GsiTracker()
    a = payload(rk=0, grenades={"weapon_flashbang": 2, "weapon_smokegrenade": 1}); t.handle(a, now=1.0)
    b = payload(rk=0, grenades={"weapon_flashbang": 1, "weapon_smokegrenade": 1})
    ev = t.handle(b, now=1.5)
    assert [e["raw"] for e in ev if e["type"] == "throw"] == ["weapon_flashbang"]
    c = payload(rk=0, grenades={})
    ev = t.handle(c, now=2.0)
    assert sorted(e["raw"] for e in ev if e["type"] == "throw") == ["weapon_flashbang", "weapon_smokegrenade"]
