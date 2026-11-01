from PIL import Image

from app import ingame, weapons as W
from app.config import AppSettings, IngameSettings
from app.modules.dpi.service import DpiService, DpiSettings

COLORS = {"color": "#ffffff", "accent": "#ffd166", "match_color": "#ff6b6b", "home_color": "#4cc9f0"}


def _coverage(mask):
    hist = mask.histogram()
    total = mask.size[0] * mask.size[1]
    return sum(hist[128:]) / total


def test_every_weapon_icon_rasterizes_to_a_sane_silhouette():
    for w in W.CATALOG:
        m = ingame.svg_mask(w.id, 48)
        assert m is not None and m.mode == "L", w.id
        assert m.size[1] == 48 and m.size[0] >= 8, w.id
        cov = _coverage(m)
        assert 0.03 < cov < 0.95, (w.id, cov)


def test_path_polygons_handles_relative_and_smooth_curves():
    polys = ingame.path_polygons("M10 10 h20 v10 H10 z m5 5 l5 0 c2 0 4 2 4 4 s-2 4 -4 4 L15 23 Z")
    assert len(polys) == 2
    assert all(len(p) >= 4 for p in polys)


def test_render_dpi_and_weapons_sample():
    s = IngameSettings()
    img = ingame.render("dpi", ingame.SAMPLE_STATE["dpi"], s, COLORS)
    assert img is not None and img.mode == "RGBA" and img.size[0] > 80 and img.size[1] == 80
    assert img.getchannel("A").getextrema()[1] == 255
    spinning = ingame.render("dpi", {**ingame.SAMPLE_STATE["dpi"], "spinning": True, "current": None}, s, COLORS)
    assert spinning is not None
    wimg = ingame.render("weapons", ingame.SAMPLE_STATE["weapons"], s, COLORS)
    assert wimg is not None and wimg.size[0] > 150 and wimg.size[1] > 60
    # the matched current weapon is drawn with the match colour
    px = {wimg.getpixel((x, y))[:3] for x in range(0, wimg.size[0], 3) for y in range(0, wimg.size[1], 3)}
    assert (255, 107, 107) in px
    assert ingame.render("weapons", {"present": False}, s, COLORS) is None
    assert ingame.render("dpi", {"present": True, "current": None, "spinning": False}, s, COLORS) is None


def test_place_corners():
    s = IngameSettings(dpi_corner="br", dpi_x=10, dpi_y=20)
    assert ingame.place("dpi", s, (100, 50), (0, 0, 1920, 1080)) == (1810, 1010)
    s = IngameSettings(weapons_corner="tl", weapons_x=5, weapons_y=7)
    assert ingame.place("weapons", s, (100, 50), (2560, 0, 1920, 1080)) == (2565, 7)


def test_ingame_settings_validation_and_persistence():
    s = IngameSettings()
    rejected = s.apply({"dpi_corner": "middle", "dpi_scale": "9", "weapons_next": 7, "dpi_mode": "always",
                        "weapons_enabled": "0", "bogus": 1, "dpi_opacity": 0.01})
    assert set(rejected) == {"dpi_corner", "bogus"}
    assert s.dpi_scale == 3.0 and s.weapons_next == 3 and s.dpi_mode == "always"
    assert s.weapons_enabled is False and s.dpi_opacity == 0.2
    d = AppSettings.from_dict({"ingame": {"dpi_corner": "br", "weapons_y": 333}}).to_dict()
    assert d["ingame"]["dpi_corner"] == "br" and d["ingame"]["weapons_y"] == 333


def test_single_value_wheel_changes_without_spinning(tmp_path, monkeypatch):
    monkeypatch.setenv("CS2R_DPI_BACKEND", "fake")
    st = DpiSettings.from_dict({"enabled": True, "use_range": False, "custom_values": [1600], "pool_mode": "random", "home_dpi": 1600})
    published = []
    svc = DpiService(st, str(tmp_path / "dpi.json"), publish=lambda e, d: published.append((e, d)),
                     publish_threadsafe=lambda e, d: None)
    assert svc.values() == [1600]
    spin = svc.spin("death")
    assert spin["instant"] is True and spin["duration_ms"] == 0 and spin["value"] == 1600
    assert any(e == "dpi_spin" and d["instant"] for e, d in published)
    st2 = DpiSettings.from_dict({"enabled": True, "use_range": False, "custom_values": [800, 1600], "pool_mode": "random", "home_dpi": 1600})
    svc2 = DpiService(st2, str(tmp_path / "dpi2.json"), publish=lambda e, d: None, publish_threadsafe=lambda e, d: None)
    assert svc2.spin()["instant"] is False




def test_api_ingame_render_and_settings(client):
    r = client.get("/api/ingame/render?kind=weapons")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = Image.open(__import__("io").BytesIO(r.content))
    assert img.size[0] > 100
    assert client.get("/api/ingame/render?kind=nope").status_code == 400
    r = client.post("/api/settings", json={"ingame": {"dpi_corner": "tr", "weapons_scale": 1.5, "dpi_corner_x": 1}})
    assert r.status_code == 200
    body = r.json()
    assert body["rejected"] == ["ingame.dpi_corner_x"]
    st = client.get("/api/state").json()
    assert st["ingame"]["dpi_corner"] == "tr" and st["ingame"]["weapons_scale"] == 1.5
    assert "available" in st["ingame"]
    r = client.post("/api/ingame/preview", json={"kind": "dpi"})
    assert r.status_code == 200 and "ok" in r.json()


def test_weapons_render_falls_back_to_text_when_silhouettes_fail(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no icons")
    monkeypatch.setattr(ingame, "svg_mask", boom)
    img = ingame.render("weapons", ingame.SAMPLE_STATE["weapons"], IngameSettings(), COLORS)
    assert img is not None and img.size[0] > 60


def test_status_exposes_per_kind_diagnostics():
    ov = ingame.IngameOverlays(IngameSettings(), lambda: {"dpi": {"present": False, "reason": "x"}, "weapons": {"present": False}}, lambda: COLORS)
    st = ov.status()
    assert set(st["errors"]) == set(ingame.KINDS) == {"dpi", "weapons", "queue"} and set(st["present"]) == set(ingame.KINDS) and "reason" in st


def test_state_reason_when_race_not_running(client):
    st = client.get("/api/state").json()["ingame"]
    assert "present" in st and "reason" in st


# ----------------------------------------------------------------------------- viewer queue block
def test_render_queue_sample_and_options():
    s = IngameSettings()
    q = ingame.SAMPLE_STATE["queue"]
    img = ingame.render("queue", q, s, COLORS)
    assert img is not None and img.mode == "RGBA" and img.size[0] > 200 and img.size[1] > 150
    px = {img.getpixel((x, y))[:3] for x in range(0, img.size[0], 2) for y in range(0, img.size[1], 2)}
    assert (255, 209, 102) in px                                   # first request in the accent colour
    s.apply({"queue_max": 1, "queue_names": False})
    small = ingame.render("queue", q, s, COLORS)
    assert small.size[1] < img.size[1] - 60                        # one short row + «+ ещё 3»
    unknown = {**q, "items": [{"weapon": None, "name": None, "user": "X"}], "total": 1}
    assert ingame.render("queue", unknown, s, COLORS) is not None
    assert ingame.render("queue", {"present": False, "items": []}, s, COLORS) is None
    assert ingame.render("queue", {"present": True, "items": []}, s, COLORS) is None
    long_name = {**q, "items": [{"weapon": "awp", "name": "AWP", "user": "очень_длинный_ник_зрителя" * 3}], "total": 1}
    s.apply({"queue_names": True})
    assert ingame.render("queue", long_name, s, COLORS).size[0] < 420     # names are cut with «…»


def test_queue_ingame_settings_validation():
    s = IngameSettings()
    assert (s.queue_enabled, s.queue_mode, s.queue_max) == (True, "always", 3)
    rejected = s.apply({"queue_max": 99, "queue_corner": "center", "queue_mode": "change", "queue_names": "0", "queue_next": 1})
    assert set(rejected) == {"queue_corner", "queue_next"}
    assert (s.queue_max, s.queue_mode, s.queue_names) == (10, "change", False)
    assert ingame.place("queue", IngameSettings(queue_corner="bl", queue_x=5, queue_y=6), (100, 50), (0, 0, 1920, 1080)) == (5, 1024)


def test_api_ingame_queue_state_and_render(client):
    st = client.app.state.app
    assert st.ingame_state()["queue"] == {"present": False, "reason": "очередь пуста", "total": 0, "items": [], "key": []}
    r = client.get("/api/ingame/render?kind=queue")                  # empty queue: the sample is drawn
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    for w, u in (("awp", "A"), ("ak47", "B"), (None, "C"), ("nova", "D")):
        client.post("/api/queue/add", json={"weapon": w, "user": u, "text": "?"})
    q = st.ingame_state()["queue"]
    assert q["present"] and q["total"] == 4 and len(q["items"]) == 3
    assert q["items"][0] == {"weapon": "awp", "name": "AWP", "user": "A", "note": None}
    client.post("/api/settings", json={"ingame": {"queue_max": 5}})
    assert len(st.ingame_state()["queue"]["items"]) == 4
    assert client.post("/api/ingame/preview", json={"kind": "queue"}).status_code == 200
    assert "queue" in client.get("/api/state").json()["ingame"]["present"]
