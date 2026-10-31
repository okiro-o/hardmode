import asyncio
import json
import time

import pytest

from app.gsi import GsiTracker
from app.modules.dpi import grid
from app.modules.dpi.backends import FakeBackend, NoneBackend, make_backend
from app.modules.dpi.hidpp import Candidate, HidppDevice, HidppError, HOST_MODE
from app.modules.dpi.service import DpiService, DpiSettings
from tests.test_gsi import ME, MATE, payload


# ------------------------------------------------------------------- grid
def test_default_grid_matches_sensor_steps():
    vals = grid.DEFAULT_VALUES
    assert vals[0] == 100 and vals[-1] == 44000
    assert 1234 not in vals and 1230 in vals and 1240 in vals
    assert 2010 not in vals and 2020 in vals
    assert 805 in vals and 802 not in vals


def test_snap_and_bounds():
    assert grid.snap(1234) == 1230
    assert grid.snap(1236) == 1240
    assert grid.snap(50) == 100
    assert grid.snap(99999) == 44000


def test_wheel_values():
    vals = grid.wheel_values(800, 3200, 100)
    assert vals[0] == 800 and vals[-1] == 3200 and len(vals) == 25
    assert all(v in grid.DEFAULT_VALUES for v in vals)
    assert grid.wheel_values(3200, 800, 100) == vals  # swapped bounds
    assert len(grid.wheel_values(100, 44000, 10)) <= 60


def test_parse_dpi_words_like_solaar():
    # the real Superlight 2 answer, spread over pages and terminated by 0
    words = [100, 0xE001, 200, 0xE002, 500, 0xE005, 1000, 0xE00A, 2000, 0xE014, 5000, 0xE032, 10000,
             0xE064, 20000, 0xE07D, 32000, 0xE0C8, 44000, 0, 0]
    vals = grid.parse_dpi_words(words)
    assert vals[0] == 100 and vals[-1] == 44000
    assert 1230 in vals and 1234 not in vals and 2020 in vals and 2010 not in vals and 43800 in vals
    assert grid.parse_dpi_words([400, 800, 1600, 0]) == [400, 800, 1600]
    assert grid.parse_dpi_words([0xE001, 200]) == []          # step without a start value


# ------------------------------------------------------------------ hidpp
class FakeHandle:
    """Scripted HID handle: answers the next queued response for any write."""

    def __init__(self, script):
        self.script = script
        self.writes = []
        self.pending = []

    def open_path(self, path):
        pass

    def set_nonblocking(self, flag):
        pass

    def write(self, data):
        self.writes.append(bytes(data))
        fn = self.script(bytes(data))
        if fn is not None:
            self.pending.append(fn)
        return len(data)

    def read(self, n):
        return list(self.pending.pop(0)) if self.pending else []

    def close(self):
        pass


def make_device(monkeypatch, script):
    import app.modules.dpi.hidpp as H

    class FakeHid:
        @staticmethod
        def device():
            return FakeHandle(script)

    monkeypatch.setattr(H, "_hid", lambda: FakeHid)
    cand = Candidate(0x046D, 0xC54D, "Lightspeed receiver", paths={1: b"short", 2: b"long"})
    dev = HidppDevice(cand, device_index=1, timeout=0.2)
    return dev


def test_hidpp_request_layout_and_feature_lookup(monkeypatch):
    def script(req):
        rid, di, fidx, fn = req[0], req[1], req[2], req[3]
        assert rid == 0x11 and di == 1 and len(req) == 20
        if fidx == 0x00 and fn >> 4 == 0:
            feat = (req[4] << 8) | req[5]
            idx = {0x2202: 0x09, 0x8100: 0x0C}.get(feat, 0)
            return bytes([0x11, 1, 0x00, fn, idx, 0, 0]) + bytes(13)
        if fidx == 0x0C and fn >> 4 == 2:          # getOnboardMode
            return bytes([0x11, 1, 0x0C, fn, 0x01]) + bytes(15)
        if fidx == 0x0C and fn >> 4 == 1:          # setOnboardMode
            return bytes([0x11, 1, 0x0C, fn]) + bytes(16)
        if fidx == 0x09 and fn >> 4 == 5:          # getSensorDpiParameters
            return bytes([0x11, 1, 0x09, fn, 0, 0x06, 0x40, 0x03, 0x20, 0x06, 0x40, 0x03, 0x20, 0x02]) + bytes(6)
        if fidx == 0x09 and fn >> 4 == 6:          # setSensorDpiParameters echo
            assert req[4:10] == bytes([0, 0x06, 0x40, 0x06, 0x40, 0x02])
            return bytes([0x11, 1, 0x09, fn]) + req[4:10] + bytes(10)
        return None

    dev = make_device(monkeypatch, script)
    dev.open()
    assert dev.feature_index(0x2202) == 0x09
    assert dev.feature_index(0x8100) == 0x0C
    assert dev.get_onboard_mode() == 0x01
    assert dev.ensure_host_mode() is True
    p = dev.dpi_parameters()
    assert p["dpi_x"] == 1600 and p["default_x"] == 800 and p["lod"] == 2
    res = dev.set_dpi(1600)
    assert res["dpi_x"] == 1600
    # the set request must be a long report with [sensor, dpiX, dpiY, lod]
    writes = [w for h in dev._handles for w in h.writes]
    set_req = [w for w in writes if w[2] == 0x09 and (w[3] >> 4) == 6][0]
    assert set_req[4:10] == bytes([0, 0x06, 0x40, 0x06, 0x40, 0x02])


def test_hidpp_error_reports(monkeypatch):
    def script(req):
        fidx, fn = req[2], req[3]
        if fidx == 0x00:
            return bytes([0x11, 1, 0x00, fn, 0x09, 0, 0]) + bytes(13)
        return bytes([0x11, 1, 0xFF, fidx, fn, 0x05]) + bytes(14)   # NotAllowed

    dev = make_device(monkeypatch, script)
    dev.open()
    with pytest.raises(HidppError) as e:
        dev.dpi_parameters()
    assert "0x05" in str(e.value)


def test_hidpp_timeout(monkeypatch):
    dev = make_device(monkeypatch, lambda req: None)
    dev.open()
    with pytest.raises(HidppError):
        dev.feature_index(0x2202)


def test_dpi_ranges_parsing(monkeypatch):
    # the device streams the word list as a continuous byte stream, 13 payload bytes per page
    # (16 param bytes minus the 3 echoed request bytes), like Solaar's produce_dpi_list reads it
    words = [100, 0xE001, 200, 0xE002, 500, 0xE005, 1000, 0xE00A, 2000, 0xE014, 5000, 0, 0]
    stream = b"".join(bytes([w >> 8, w & 0xFF]) for w in words)
    pages = {i: stream[i * 13:(i + 1) * 13] for i in range((len(stream) + 12) // 13)}

    def script(req):
        fidx, fn = req[2], req[3]
        if fidx == 0x00:
            return bytes([0x11, 1, 0x00, fn, 0x09, 0, 0]) + bytes(13)
        if fidx == 0x09 and fn >> 4 == 2:
            assert req[4] == 0 and req[5] == 0, "params must be [sensor, direction, page]"
            page = req[6]
            body = pages.get(page)
            if body is None:
                return bytes([0x11, 1, 0xFF, fidx, fn, 0x03]) + bytes(14)
            body = body + bytes(13 - len(body))
            return bytes([0x11, 1, 0x09, fn, 0, 0, page]) + body
        return None

    dev = make_device(monkeypatch, script)
    dev.open()
    vals = dev.dpi_ranges()
    assert vals[0] == 100 and 1990 in vals and 2020 in vals and vals[-1] == 5000
    assert 2010 not in vals


# ---------------------------------------------------------------- backends
def test_make_backend_env(monkeypatch):
    monkeypatch.setenv("CS2R_DPI_BACKEND", "fake")
    assert make_backend("auto").name == "fake"
    monkeypatch.setenv("CS2R_DPI_BACKEND", "none")
    assert isinstance(make_backend("auto"), NoneBackend)


def test_fake_backend_snaps():
    b = FakeBackend()
    assert b.set_dpi(1234) == 1230 and b.get_dpi() == 1230
    assert b.probe()["found"]


# ----------------------------------------------------------------- service
@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("CS2R_DPI_BACKEND", "fake")
    events = []
    svc = DpiService(DpiSettings.from_dict({"spin_duration_ms": 500, "death_cooldown_s": 1.0}),
                     str(tmp_path / "dpi.json"),
                     publish=lambda ev, d: events.append((ev, d)),
                     publish_threadsafe=lambda ev, d: events.append((ev, d)))
    svc.events = events
    return svc


def test_spin_picks_from_wheel_and_applies_at_stop(service):
    async def run():
        loop = asyncio.get_running_loop()
        service.start(loop)
        try:
            spin = service.spin("manual")
            assert spin["value"] in spin["values"] and 0 <= spin["index"] < len(spin["values"])
            assert spin["values"][spin["index"]] == spin["value"]
            assert ("dpi_spin", spin) in service.events
            assert service.backend.calls == []          # not applied yet (apply_at = stop)
            await asyncio.sleep(0.8)
            assert service.backend.calls == [spin["value"]]
            assert service.current_dpi == spin["value"]
            assert service.last_spin["applied"] is not None
        finally:
            service.stop()
    asyncio.run(run())


def test_spin_excludes_current_and_death_cooldown(service):
    async def run():
        loop = asyncio.get_running_loop()
        service.start(loop)
        try:
            service.current_dpi = 1600
            for _ in range(50):
                idx, value, vals, _w = service.pick()
                assert value != 1600
            assert service.on_death() is True
            assert service.on_death() is False           # cooldown
            assert service.deaths == 2 and service.spins == 1
            service.update_settings({"auto_on_death": False})
            service._last_spin_at = 0
            assert service.on_death() is False
        finally:
            service.stop()
    asyncio.run(run())


def test_apply_at_start_and_manual_apply(service):
    async def run():
        loop = asyncio.get_running_loop()
        service.start(loop)
        try:
            service.update_settings({"apply_at": "start"})
            spin = service.spin("hotkey")
            await asyncio.sleep(0.05)
            assert service.backend.calls == [spin["value"]]
            res = await service.apply(1234, "manual")
            assert res["ok"] and res["value"] == 1230 and service.current_dpi == 1230
            info = await service.probe()
            assert info["found"]
        finally:
            service.stop()
    asyncio.run(run())


def test_service_persistence(service, tmp_path):
    async def run():
        loop = asyncio.get_running_loop()
        service.start(loop)
        try:
            service.spin("manual")
            await asyncio.sleep(0.8)
        finally:
            service.stop()
    asyncio.run(run())
    svc2 = DpiService(DpiSettings(), str(tmp_path / "dpi.json"), lambda *a: None, lambda *a: None)
    assert svc2.current_dpi == service.current_dpi and svc2.spins == 1


def test_guard_reapplies_after_revert(tmp_path):
    svc = DpiService(DpiSettings.from_dict({"guard": True, "backend": "fake", "spin_duration_ms": 500}),
                     str(tmp_path / "dpi.json"), lambda *a: None, lambda *a: None)
    svc.backend = FakeBackend(dpi=800, revert_after=0.2)
    svc.target_dpi = 1600
    svc.current_dpi = 1600
    svc.backend.set_dpi(1600)
    time.sleep(0.3)
    assert svc.backend.get_dpi() == 800   # "G HUB" reverted
    svc._guard_stop.clear()
    import threading
    t = threading.Thread(target=svc._guard_loop, daemon=True)
    # make the loop tick fast
    svc._guard_stop.wait = lambda timeout=None: False if svc.guard_reapplies == 0 else True
    t.start()
    t.join(timeout=2.0)
    assert svc.guard_reapplies >= 1 and svc.backend.get_dpi() == 1600


def test_dpi_settings_validation():
    s = DpiSettings.from_dict({"min_dpi": 3200, "max_dpi": 800, "step": "100", "apply_at": "later", "guard": "no"})
    assert s.min_dpi == 800 and s.max_dpi == 3200 and s.apply_at == "stop" and s.guard is False
    with pytest.raises(ValueError):
        DpiSettings.from_dict({"min_dpi": None})
    with pytest.raises(ValueError):
        DpiSettings.from_dict("x")


# -------------------------------------------------------------- GSI deaths
def deaths(events):
    return [e for e in events if e["type"] == "death"]


def test_death_detected_by_health_zero_once():
    t = GsiTracker()
    t.handle(payload(rk=0), now=1.0)
    dead = payload(rk=0)
    dead["player"]["state"]["health"] = 0
    assert len(deaths(t.handle(dead, now=1.1))) == 1
    assert deaths(t.handle(dead, now=1.2)) == []           # still dead: no second event
    assert deaths(t.handle(payload(steamid=MATE), now=1.3)) == []
    assert deaths(t.handle(payload(rk=0, round_=4), now=5.0)) == []   # respawn / new round
    dead2 = payload(rk=0, round_=4)
    dead2["player"]["state"]["health"] = 0
    assert len(deaths(t.handle(dead2, now=6.0))) == 1


def test_death_detected_by_switch_to_teammate():
    t = GsiTracker()
    t.handle(payload(rk=1), now=1.0)
    ev = t.handle(payload(steamid=MATE, rk=0), now=1.1)
    assert len(deaths(ev)) == 1
    assert deaths(t.handle(payload(steamid=MATE, rk=1), now=1.2)) == []


def test_death_in_warmup_ignored():
    t = GsiTracker()
    t.handle(payload(rk=0, phase="warmup"), now=1.0)
    dead = payload(rk=0, phase="warmup")
    dead["player"]["state"]["health"] = 0
    assert deaths(t.handle(dead, now=1.1)) == []


def test_legacy_0x2201_mouse_like_g304(monkeypatch):
    """G304/G305: no 0x2202, DPI via 0x2201 (fn2 get, fn3 set, fn1 list), onboard profiles present."""
    state = {"dpi": 400, "mode": 0x01}
    words = [200, 0xE032, 12000, 0, 0, 0, 0, 0]   # 200..12000 step 50
    stream = b"".join(bytes([w >> 8, w & 0xFF]) for w in words)
    pages = {i: stream[i * 15:(i + 1) * 15] for i in range((len(stream) + 14) // 15)}

    def script(req):
        fidx, fn = req[2], req[3]
        if fidx == 0x00:                                   # root getFeature
            feat = (req[4] << 8) | req[5]
            idx = {0x2201: 0x07, 0x8100: 0x0A}.get(feat, 0)
            return bytes([0x11, 1, 0x00, fn, idx, 0, 0]) + bytes(13)
        if fidx == 0x0A and fn >> 4 == 2:
            return bytes([0x11, 1, 0x0A, fn, state["mode"]]) + bytes(15)
        if fidx == 0x0A and fn >> 4 == 1:
            state["mode"] = req[4]
            return bytes([0x11, 1, 0x0A, fn]) + bytes(16)
        if fidx == 0x07 and fn >> 4 == 2:                  # getSensorDpi
            d = state["dpi"]
            return bytes([0x11, 1, 0x07, fn, 0, d >> 8, d & 0xFF, 0x03, 0x20]) + bytes(11)
        if fidx == 0x07 and fn >> 4 == 3:                  # setSensorDpi
            state["dpi"] = (req[5] << 8) | req[6]
            return bytes([0x11, 1, 0x07, fn]) + req[4:7] + bytes(13)
        if fidx == 0x07 and fn >> 4 == 1:                  # getSensorDpiList (1 echoed byte)
            page = req[6]
            body = pages.get(page)
            if body is None:
                return bytes([0x11, 1, 0xFF, fidx, fn, 0x02]) + bytes(14)
            return bytes([0x11, 1, 0x07, fn, 0]) + body + bytes(15 - len(body))
        return None

    dev = make_device(monkeypatch, script)
    dev.open()
    assert dev.dpi_feature() == (0x2201, 0x07)
    assert dev.ensure_host_mode() is True and state["mode"] == 0x02
    p = dev.dpi_parameters()
    assert p["dpi_x"] == 400 and p["default_x"] == 800 and p["lod"] is None and p["feature"] == "0x2201"
    assert dev.set_dpi(1600)["dpi_x"] == 1600
    vals = dev.dpi_ranges()
    assert vals[0] == 200 and vals[-1] == 12000 and 1650 in vals and 1620 not in vals


def test_backend_connect_error_message_is_readable(monkeypatch):
    import app.modules.dpi.backends as B
    from app.modules.dpi.hidpp import Candidate

    class Dev:
        def __init__(self, cand, device_index=None, timeout=1.0):
            self.is_open = False
        def open(self):
            self.is_open = True
        def dpi_feature(self):
            raise HidppError("нет фич")
        def close(self):
            pass

    monkeypatch.setattr(B, "enumerate_candidates", lambda: [Candidate(0x046D, 0xC539, "Lightspeed Receiver", {2: b"p"})])
    monkeypatch.setattr(B, "HidppDevice", Dev)
    with pytest.raises(HidppError) as e:
        B.HidppBackend()._connect()
    assert "Lightspeed Receiver@1" in str(e.value) and "format" not in str(e.value)
