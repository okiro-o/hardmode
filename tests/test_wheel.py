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
    async def run():
        svc.start(asyncio.get_running_loop())
        try:
            seen = []
            for _ in range(5):
                before = set(svc.values())
                spin = svc.spin("manual")
                seen.append(spin["value"])
                assert spin["value"] in before
                assert spin["value"] not in svc.values() or len(svc.values()) == 5
            assert sorted(seen) == [800, 900, 1000, 1100, 1200]
            assert len(svc.values()) == 5           # cycle exhausted -> refilled
            assert svc.wheel_info()["consumed"] == []
            svc.update_settings({"pool_mode": "random"})
            svc.spin("manual")
            assert len(svc.values()) == 5
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
    svc.update_settings({"viewer_min": 400, "viewer_max": 6400, "viewer_max_values": 2, "viewer_per_user": 1})
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
            svc._rng.choice = lambda options: options[svc.values().index(1600)]
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
