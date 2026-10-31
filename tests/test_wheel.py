import asyncio

import pytest

from app.modules.dpi.service import DpiService, DpiSettings
from app.twitch_irc import parse_line


@pytest.fixture()
def svc(tmp_path, monkeypatch):
    monkeypatch.setenv("CS2R_DPI_BACKEND", "fake")
    events = []
    s = DpiService(DpiSettings.from_dict({"min_dpi": 800, "max_dpi": 1200, "step": 100, "spin_duration_ms": 500}),
                   str(tmp_path / "dpi.json"), publish=lambda e, d: events.append((e, d)),
                   publish_threadsafe=lambda e, d: events.append((e, d)))
    s.events = events
    return s


def test_base_values_range_and_custom(svc):
    assert svc.base_values() == [800, 900, 1000, 1100, 1200]
    svc.update_settings({"custom_values": [1600, 1234]})
    assert svc.base_values() == [800, 900, 1000, 1100, 1200, 1230, 1600]
    svc.update_settings({"use_range": False})
    assert svc.base_values() == [1230, 1600]
    svc.update_settings({"use_range": False, "custom_values": []})
    assert svc.values() == [3200]   # nothing configured -> home DPI keeps the wheel non-empty


def test_bag_mode_removes_rolled_values_until_exhausted(svc):
    # the home value (3200 by default) is always on the wheel and never consumed; rig the picker to skip it
    svc._rng.choices = lambda population, weights=None, k=1: [next(i for i in population if svc.segments()[0][i] != 3200)]
    async def run():
        svc.start(asyncio.get_running_loop())
        try:
            assert svc.values() == [800, 900, 1000, 1100, 1200, 3200]
            seen = []
            for _ in range(5):
                before = set(svc.values())
                spin = svc.spin("manual")
                seen.append(spin["value"])
                assert spin["value"] in before
            assert seen == [800, 900, 1000, 1100, 1200]
            assert len(svc.values()) == 6           # cycle exhausted -> refilled, home still there
            assert svc.wheel_info()["consumed"] == [] and svc.wheel_info()["home"] == 3200
            svc.update_settings({"pool_mode": "random"})
            svc.spin("manual")
            assert len(svc.values()) == 6
        finally:
            svc.stop()
    asyncio.run(run())


def test_home_value_is_never_consumed_even_when_in_range(svc):
    svc.update_settings({"home_dpi": 1000, "pool_mode": "bag"})
    svc._rng.choices = lambda population, weights=None, k=1: [svc.segments()[0].index(1000)]
    async def run():
        svc.start(asyncio.get_running_loop())
        try:
            svc.spin("manual")
            assert 1000 in svc.values() and svc.consumed == []
        finally:
            svc.stop()
    asyncio.run(run())


def test_reset_cycle_and_custom_add_remove(svc):
    svc.consumed = [800, 900]
    assert 800 not in svc.values()
    svc.reset_cycle()
    assert 800 in svc.values()
    assert svc.add_custom_value(1555)["value"] == 1550
    assert 1550 in svc.settings.custom_values and 1550 in svc.values()
    assert svc.add_custom_value("abc")["ok"] is False
    svc.remove_custom_value(1550)
    assert 1550 not in svc.values()


def test_viewer_values_validation_limits_and_consumption(svc):
    svc.update_settings({"viewer_min": 400, "viewer_max": 6400, "viewer_max_values": 2, "viewer_per_user": 1,
                         "pool_mode": "bag"})
    assert svc.add_viewer_value("1600", "Alice")["ok"]
    assert svc.add_viewer_value("1600", "alice")["ok"] is False          # per-user limit
    assert svc.add_viewer_value("20", "Bob")["ok"] is False               # out of range
    assert svc.add_viewer_value("x", "Bob")["ok"] is False
    assert svc.add_viewer_value(" 2 400 ", "Bob")["ok"]                   # spaces tolerated
    assert svc.add_viewer_value("3000", "Carl")["ok"] is False            # wheel full
    assert 1600 in svc.values() and 2400 in svc.values()
    # a viewer value disappears once rolled
    async def run():
        svc.start(asyncio.get_running_loop())
        try:
            svc._rng.choices = lambda population, weights=None, k=1: [list(population)[svc.values().index(1600)]]
            spin = svc.spin("death")
            assert spin["value"] == 1600
            assert all(e["value"] != 1600 for e in svc.viewer_values)
            assert 2400 in svc.values()
        finally:
            svc.stop()
    asyncio.run(run())
    svc.remove_viewer_value(0)
    assert svc.viewer_values == []


def test_viewer_values_persist(svc, tmp_path):
    svc.add_viewer_value("1600", "Alice")
    svc.consumed = [800]
    svc._save()
    other = DpiService(DpiSettings(), str(tmp_path / "dpi.json"), lambda *a: None, lambda *a: None)
    assert other.viewer_values[0]["value"] == 1600 and other.consumed == [800]


# ------------------------------------------------------------------ IRC parsing
def test_parse_privmsg_with_reward_tag():
    line = ("@badge-info=;badges=;color=;custom-reward-id=abc-123;display-name=Viewer_One;mod=0;user-id=42 "
            ":viewer_one!viewer_one@viewer_one.tmi.twitch.tv PRIVMSG #streamer :1600")
    m = parse_line(line)
    assert m.command == "PRIVMSG" and m.params == ["#streamer"] and m.trailing == "1600"
    assert m.reward_id == "abc-123" and m.display_name == "Viewer_One" and m.user == "viewer_one"
    assert not m.is_mod


def test_parse_ping_join_and_escapes():
    assert parse_line("PING :tmi.twitch.tv").command == "PING"
    j = parse_line(":justinfan123!justinfan123@justinfan123.tmi.twitch.tv JOIN #streamer")
    assert j.command == "JOIN" and j.user == "justinfan123"
    m = parse_line("@display-name=A\\sB;mod=1 :a!a@a PRIVMSG #c :hello world")
    assert m.display_name == "A B" and m.is_mod and m.trailing == "hello world"
    assert parse_line("") is None


def test_viewer_rounding_duplicates_weights_and_pool_modes(svc):
    svc.update_settings({"viewer_min": 400, "viewer_max": 6400, "viewer_max_values": 10, "viewer_per_user": 3,
                         "pool_mode": "random", "use_range": False, "custom_values": [1000, 3000]})
    r = svc.add_viewer_value("228", "Alice")
    assert r["ok"] is False and "228 → 200" in r["error"]            # rounded first, then range-checked
    assert svc.add_viewer_value("666", "Alice")["value"] == 700
    assert svc.add_viewer_value("1250", "Alice")["value"] == 1300    # .5 rounds up, no banker's rounding
    # duplicates merge into one wider segment
    assert svc.add_viewer_value("3000", "Bob")["ok"] and svc.add_viewer_value("3000", "Carl")["ok"]
    vals, weights = svc.segments()
    assert vals == [700, 1000, 1300, 3000, 3200] and weights == [1, 1, 1, 3, 1]   # 3200 = home, always present
    assert svc.wheel_info()["weights"] == {"3000": 3}
    assert svc.snapshot()["weights"] == [1, 1, 1, 3, 1]
    # weighted pick honours the weights
    seen = {svc.pick()[1] for _ in range(300)}
    assert 3000 in seen
    svc._rng.choices = lambda population, weights=None, k=1: [list(population)[weights.index(max(weights))]]
    assert svc.pick()[1] == 3000
    # full random: a rolled viewer value stays on the wheel
    spin = svc.spin("manual")
    assert spin["value"] == 3000 and spin["weights"] == [1, 1, 1, 3, 1]
    assert svc.segments()[1] == [1, 1, 1, 3, 1]
    # bag: the base 3000 and one viewer ticket leave, the other viewer's 3000 stays
    svc.update_settings({"pool_mode": "bag"})
    svc.spin("manual")
    assert svc.segments() == ([700, 1000, 1300, 3000, 3200], [1, 1, 1, 1, 1])
    assert svc.consumed == [3000] and sum(1 for e in svc.viewer_values if e["value"] == 3000) == 1


def test_viewer_ticket_for_home_value_stacks_on_the_permanent_one(svc):
    svc.update_settings({"viewer_min": 400, "viewer_max": 6400, "viewer_max_values": 10, "viewer_per_user": 3,
                         "pool_mode": "bag"})
    assert svc.segments()[0] == [800, 900, 1000, 1100, 1200, 3200]
    assert svc.add_viewer_value("3200", "Alice")["ok"]
    vals, weights = svc.segments()
    assert weights[vals.index(3200)] == 2                      # permanent + viewer
    assert svc.wheel_info()["weights"] == {"3200": 2}
    # home inside the range: still one permanent ticket, not range + permanent
    svc.update_settings({"home_dpi": 1000})
    svc.clear_viewer_values()
    vals, weights = svc.segments()
    assert vals == [800, 900, 1000, 1100, 1200] and weights == [1, 1, 1, 1, 1]
    svc.add_viewer_value("1000", "Bob")
    assert svc.segments()[1][svc.segments()[0].index(1000)] == 2
    # rolling it in bag mode removes the viewer ticket, the permanent one stays
    svc._rng.choices = lambda population, weights=None, k=1: [svc.segments()[0].index(1000)]
    async def run():
        svc.start(asyncio.get_running_loop())
        try:
            svc.spin("manual")
            assert svc.segments()[1][svc.segments()[0].index(1000)] == 1 and svc.consumed == []
        finally:
            svc.stop()
    asyncio.run(run())
