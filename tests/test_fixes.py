"""Regression tests for defects found in the multi-lens review."""
import random

import pytest

from app import loadout as L
from app.config import AppSettings, HotkeySettings, OverlaySettings
from app.gsi import GsiTracker
from app.hotkeys import MOD_ALT, MOD_CONTROL, parse_hotkey
from app.roulette import Roulette, RouletteSettings
from tests.test_gsi import ME, MATE, kills, payload


def make(seed=1, **settings):
    r = Roulette(settings=RouletteSettings.from_dict(settings))
    r._rng = random.Random(seed)
    r.generate_loadout()
    return r


# ----------------------------------------------------------------- loadout / sides
def test_single_side_generate_on_fresh_state_produces_full_loadout():
    r = Roulette()
    r._rng = random.Random(0)
    r.generate_loadout(("CT",))
    assert L.validate(r.loadout) == []
    r.start(seed=1)
    r.set_side("CT")
    assert r.snapshot()["queue"]
    r.generate_loadout(("T",))  # only T regenerated, CT kept
    assert L.validate(r.loadout) == []


def test_set_loadout_rejects_half_loadout():
    r = make()
    with pytest.raises(ValueError):
        r.set_loadout({"T": r.loadout["T"]})


# ----------------------------------------------------------------------- undo
def test_undo_after_side_switch_keeps_current_in_pool():
    r = make()
    r.start(seed=5)
    r.queue[0] = "ak47"
    r.advance()
    r.set_side("CT", source="gsi")
    r.undo()
    assert r.side == "CT"
    assert all(q in r.pool_ids() for q in r.queue)


def test_undo_after_kill_advance_restores_counters():
    r = make(kills_per_weapon=2)
    r.start(seed=1)
    cur = r.queue[0]
    r.on_kill(cur)
    r.on_kill(cur)
    assert r.completed == 1
    r.undo()
    assert r.queue[0] == cur
    assert r.current_kills == 1 and r.kills_total == 1 and r.completed == 0


# --------------------------------------------------------------------- queue
@pytest.mark.parametrize("mode", ["bag", "random"])
def test_visible_next_zero_never_repeats_immediately(mode):
    r = make(pool_mode=mode, visible_next=0)
    r.start(seed=3)
    prev = r.queue[0]
    for _ in range(300):
        r.advance()
        assert r.queue[0] != prev
        prev = r.queue[0]
    for _ in range(100):
        before = r.queue[0]
        r.reroll_current()
        assert r.queue[0] != before


def test_bag_boundary_and_side_switch_show_no_duplicates_in_strip():
    for seed in range(60):
        r = make(seed=seed, visible_next=2)
        r.start(seed=seed)
        for _ in range(40):
            assert len(set(r.queue)) == len(r.queue), (seed, r.queue)
            r.advance()
        both = next(w for w in r.pool_ids("T") if w in L.side_pool_ids(r.loadout, "CT"))
        r.queue[0] = both
        r.set_side("CT")
        assert len(set(r.queue)) == len(r.queue), (seed, r.queue)
        r.update_settings({"include_extras": True})
        assert len(set(r.queue)) == len(r.queue)


def test_rebuild_resets_kill_counter_when_current_dropped():
    r = make(kills_per_weapon=3)
    r.start(seed=4)
    r.queue[0] = "ak47"
    r.on_kill("ak47")
    r.on_kill("ak47")
    assert r.current_kills == 2
    r.set_side("CT")
    assert r.current_kills == 0


# ------------------------------------------------------------------- session
def test_stop_then_start_resumes():
    r = make()
    r.start(seed=9)
    r.advance()
    r.advance()
    q = list(r.queue)
    r.stop()
    assert r.paused and not r.running
    r.start()
    assert r.running and r.queue == q and r.completed == 2
    r.reset()
    r.start()
    assert r.completed == 0 and r.queue != [] and not r.paused


def test_any_mode_counts_ambiguous_kills():
    r = make(auto_advance="any")
    r.start(seed=1)
    res = r.on_kill(None, ambiguous=True)
    assert res["counted"] and r.completed == 1


def test_from_dict_survives_garbage():
    r = Roulette.from_dict({"queue": "nope", "loadout": 5, "running": True, "history": "x", "uid_seq": "a",
                            "settings": {"kills_per_weapon": "abc"}})
    assert not r.running and r.queue == [] and r.loadout is None
    assert Roulette.from_dict("junk").loadout is None


def test_settings_from_dict_validation():
    with pytest.raises(ValueError):
        RouletteSettings.from_dict({"kills_per_weapon": None})
    with pytest.raises(ValueError):
        RouletteSettings.from_dict("abc")
    s = RouletteSettings.from_dict({"to_dict": 1, "visible_next": "3"})
    assert s.visible_next == 3 and callable(s.to_dict)


# ---------------------------------------------------------------------- GSI
def test_previously_from_spectated_teammate_is_not_a_kill():
    t = GsiTracker()
    t.handle(payload(rk=1, round_=3), now=1.0)
    t.handle(payload(rk=1, round_=3), now=2.0)
    t.handle(payload(steamid=MATE, rk=0, round_=3), now=3.0)
    ev = t.handle(payload(rk=1, round_=4, rphase="over",
                          previously={"player": {"steamid": MATE, "state": {"round_kills": 0}}, "map": {"round": 3}}),
                  now=4.0)
    assert kills(ev) == []


def test_round_ending_kill_without_previously_is_counted():
    t = GsiTracker()
    t.handle(payload(rk=1, round_=3), now=1.0)
    ev = kills(t.handle(payload(rk=2, round_=4, rphase="over"), now=1.2))
    assert len(ev) == 1 and ev[0]["weapon"] == "ak47"
    # next round reset must not fire
    assert kills(t.handle(payload(rk=0, round_=4, rphase="freezetime"), now=5.0)) == []


def test_malformed_blocks_do_not_raise():
    t = GsiTracker(token="s")
    for p in ({"auth": True}, {"auth": "x"}, {"auth": {"token": "s"}, "player": True, "map": {"name": "x"}},
              {"auth": {"token": "s"}, "player": {"steamid": "1", "state": "x", "weapons": [1, {"name": "weapon_ak47", "state": "active"}]},
               "map": "m", "round": 5, "provider": "p", "previously": "x"}):
        t.handle(p, now=1.0)


def test_menu_payload_clears_stale_fields():
    t = GsiTracker()
    t.handle(payload(rk=2, active="weapon_ak47"), now=1.0)
    t.handle({"provider": {"steamid": ME}, "player": {"steamid": ME, "activity": "menu"}}, now=2.0)
    st = t.status
    assert st.held is None and st.health is None and st.round_kills is None and st.team is None


def test_death_does_not_register_grenade_throw():
    t = GsiTracker()
    t.handle(payload(rk=0, active="weapon_ak47", grenades={"weapon_hegrenade": 1}), now=1.0)
    dead = payload(rk=0, active="weapon_ak47", grenades={})
    dead["player"]["state"]["health"] = 0
    dead["player"]["weapons"] = {}
    t.handle(dead, now=1.5)
    t.handle(payload(rk=0, round_=4, active="weapon_ak47"), now=20.0)
    ev = kills(t.handle(payload(rk=1, round_=4, active="weapon_ak47"), now=20.5))
    assert ev[0]["ambiguous"] is None


# ------------------------------------------------------------------ hotkeys
def test_parse_hotkey_variants():
    assert parse_hotkey("F9") == (0, 0x78)
    assert parse_hotkey("ctrl+alt+K") == (MOD_CONTROL | MOD_ALT, ord("K"))
    assert parse_hotkey("numpad+") == (0, 0x6B)
    assert parse_hotkey("NUM+") == (0, 0x6B)
    assert parse_hotkey("ctrl++") == (MOD_CONTROL, 0x6B)
    assert parse_hotkey("numpad-") == (0, 0x6D)
    assert parse_hotkey("") is None and parse_hotkey(5) is None and parse_hotkey("ctrl+a+b") is None
    assert parse_hotkey("nosuchkey") is None


def test_hotkey_settings_reject_bad_and_duplicates():
    h = HotkeySettings()
    rejected = h.apply({"next": 5, "reroll": "F9", "undo": "", "enabled": "no"})
    assert "next" in rejected and h.next == "F9"
    assert h.reroll == "" and "reroll" in rejected  # duplicate of next
    assert h.enabled is False


def test_overlay_settings_validation():
    o = OverlaySettings()
    rejected = o.apply({"scale": None, "color": "red;x", "layout": "diagonal", "align": "center", "labels": 1})
    assert set(rejected) == {"scale", "color", "layout"}
    assert o.align == "center" and o.labels is True and o.scale == 1.0
    o.apply({"scale": 99})
    assert o.scale == 4.0
    with pytest.raises(ValueError):
        o.apply("abc")


def test_app_settings_default_undo_hotkey_and_corrupt_file(tmp_path):
    assert AppSettings().hotkeys.undo == "F7"
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    s = AppSettings.load(str(path))
    assert s.port == 17890
    assert (tmp_path / "settings.json.broken").exists()
    s2 = AppSettings.load(str(path))
    assert s2.gsi_token == s.gsi_token  # regenerated file persisted, token stable



def test_view_side_follows_tab_and_side_switch():
    r = make()
    r.start(seed=1)
    assert r.snapshot()["view_side"] == "T"
    r.set_view_side("CT")
    assert r.snapshot()["view_side"] == "CT" and r.side == "T"
    r.set_side("CT")
    r.set_view_side("T")
    assert r.snapshot()["view_side"] == "T"
    r.set_side("T")  # no change (already T) -> view stays
    r.generate_loadout(("CT",))
    assert r.snapshot()["view_side"] == "CT"
    with pytest.raises(ValueError):
        r.set_view_side("X")


def test_overlay_match_color_validation():
    o = OverlaySettings()
    assert o.apply({"match_color": "#00ff00"}) == [] and o.match_color == "#00ff00"
    assert o.apply({"match_color": "red"}) == ["match_color"]
