import random

from app import loadout as L
from app.roulette import Roulette, RouletteSettings


def make(seed=1, **settings):
    r = Roulette(settings=RouletteSettings.from_dict(settings))
    r._rng = random.Random(seed)
    r.generate_loadout()
    return r


def test_start_fills_visible_queue():
    r = make()
    r.start(seed=7)
    assert r.running
    assert len(r.queue) == 3
    assert len(r.queue_uids) == 3
    assert len(set(r.queue_uids)) == 3
    assert all(q in r.pool_ids() for q in r.queue)


def test_advance_moves_queue_and_counts():
    r = make()
    r.start(seed=7)
    first, second = r.queue[0], r.queue[1]
    uid_second = r.queue_uids[1]
    entry = r.advance("manual")
    assert entry["weapon"] == first
    assert r.queue[0] == second
    assert r.queue_uids[0] == uid_second
    assert len(r.queue) == 3
    assert r.completed == 1
    assert r.history[-1]["weapon"] == first


def test_bag_mode_has_no_repeats_until_exhausted():
    r = make(pool_mode="bag")
    r.start(seed=3)
    pool = r.pool_ids()
    seen = []
    for _ in range(len(pool)):
        seen.append(r.queue[0])
        r.advance()
    assert sorted(seen) == sorted(pool)


def test_random_mode_avoids_adjacent_repeats():
    r = make(pool_mode="random")
    r.start(seed=11)
    prev = None
    for _ in range(200):
        cur = r.queue[0]
        assert cur != prev
        prev = cur
        r.advance()


def test_kill_strict_mode():
    r = make(auto_advance="strict")
    r.start(seed=1)
    cur = r.queue[0]
    other = next(w for w in r.pool_ids() if w != cur)
    res = r.on_kill(other)
    assert not res["counted"] and not res["advanced"]
    assert r.kills_wrong_weapon == 1 and r.queue[0] == cur
    res = r.on_kill(cur, headshot=True)
    assert res["counted"] and res["advanced"]
    assert r.completed == 1


def test_kill_any_mode_and_kills_per_weapon():
    r = make(auto_advance="any", kills_per_weapon=2)
    r.start(seed=1)
    cur = r.queue[0]
    res = r.on_kill("knife")
    assert res["counted"] and not res["advanced"]
    assert r.current_kills == 1 and r.queue[0] == cur
    res = r.on_kill("knife")
    assert res["advanced"] and r.completed == 1


def test_kill_off_mode_never_advances():
    r = make(auto_advance="off")
    r.start(seed=1)
    cur = r.queue[0]
    for _ in range(3):
        r.on_kill(cur)
    assert r.queue[0] == cur and r.current_kills == 3


def test_ambiguous_kill_not_counted():
    r = make()
    r.start(seed=1)
    cur = r.queue[0]
    res = r.on_kill(cur, ambiguous=True)
    assert not res["counted"] and r.kills_ambiguous == 1 and r.queue[0] == cur


def test_side_switch_keeps_current_if_available():
    r = make()
    r.start(seed=5)
    # force a weapon usable by both sides as current
    both = next(w for w in r.pool_ids("T") if w in L.side_pool_ids(r.loadout, "CT"))
    r.queue[0] = both
    r.set_side("CT")
    assert r.side == "CT"
    assert r.queue[0] == both
    assert all(q in r.pool_ids("CT") for q in r.queue)


def test_side_switch_replaces_t_only_current():
    r = make()
    r.start(seed=5)
    r.queue[0] = "ak47"
    r.set_side("CT")
    assert r.queue[0] != "ak47"
    assert all(q in r.pool_ids("CT") for q in r.queue)


def test_side_from_gsi_ignored_in_manual_mode():
    r = make(side_source="manual")
    r.start(seed=5)
    assert r.set_side("CT", source="gsi") is False
    assert r.side == "T"
    assert r.set_side("CT", source="manual") is True


def test_reroll_and_undo():
    r = make()
    r.start(seed=2)
    before = list(r.queue)
    r.reroll_current()
    assert r.completed == 0
    assert r.undo()
    assert r.queue == before
    r.advance()
    assert r.completed == 1
    assert r.undo()
    assert r.completed == 0 and r.queue == before


def test_persistence_roundtrip(tmp_path):
    r = make()
    r.start(seed=9)
    r.advance()
    path = str(tmp_path / "state.json")
    r.save(path)
    r2 = Roulette.load(path)
    assert r2.running and r2.queue == r.queue and r2.queue_uids == r.queue_uids
    assert r2.completed == 1 and r2.loadout == r.loadout
    assert r2.snapshot()["queue"][0]["uid"] == r.queue_uids[0]


def test_update_settings_changes_visible_count_and_pool():
    r = make()
    r.start(seed=4)
    r.update_settings({"visible_next": 4})
    assert len(r.queue) == 5
    r.update_settings({"include_starting": False})
    assert "glock" not in r.queue
    assert all(q in r.pool_ids() for q in r.queue)


def test_replace_loadout_weapon_during_session_rebuilds_queue():
    r = make()
    r.start(seed=4)
    cur = r.queue[0]
    cat = next(c for c in ("pistol", "mid", "rifle") if cur in r.loadout["T"][c])
    r.replace_weapon("T", cat, cur)
    assert cur not in r.pool_ids()
    assert all(q in r.pool_ids() for q in r.queue)
