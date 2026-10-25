import json
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CS2R_DATA", str(tmp_path))
    monkeypatch.setenv("CS2R_NO_BROWSER", "1")
    from app import main as m

    app = m.create_app()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def test_pages_render(client):
    for path in ("/", "/overlay/weapons", "/overlay/loadout", "/overlay/dpi"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "text/html" in r.headers["content-type"]
    assert client.get("/static/icons/ak47.svg").status_code == 200


def test_full_flow(client):
    s = client.get("/api/state").json()
    assert s["roulette"]["loadout"] is None
    r = client.post("/api/loadout/generate", json={"sides": ["T", "CT"]})
    assert r.status_code == 200
    lo = r.json()["loadout"]
    assert len(lo["T"]["rifle"]) == 5

    r = client.post("/api/session/start")
    assert r.status_code == 200
    snap = r.json()
    assert snap["running"] and len(snap["queue"]) == 3
    cur = snap["queue"][0]["id"]

    # GSI: two payloads, second one has a kill with the current weapon
    token = s["gsi_token"]

    def gsi(rk, active):
        return {
            "auth": {"token": token},
            "provider": {"steamid": "1"},
            "map": {"name": "de_dust2", "phase": "live", "round": 1, "mode": "competitive"},
            "round": {"phase": "live"},
            "player": {"steamid": "1", "team": "T", "activity": "playing",
                       "state": {"health": 100, "round_kills": rk, "round_killhs": 0},
                       "weapons": {"weapon_0": {"name": f"weapon_{active}", "state": "active"}},
                       "match_stats": {"kills": rk}},
        }

    assert client.post("/gsi", json=gsi(0, cur)).json()["ok"]
    assert client.post("/gsi", json=gsi(1, cur)).status_code == 200
    snap = client.get("/api/state").json()["roulette"]
    assert snap["completed"] == 1
    assert snap["queue"][0]["id"] != cur or snap["kills_total"] == 1

    # wrong weapon does not advance in strict mode
    cur2 = snap["queue"][0]["id"]
    other = next(w for w in snap["pool"] if w != cur2)
    client.post("/gsi", json=gsi(2, other))
    snap = client.get("/api/state").json()["roulette"]
    assert snap["completed"] == 1 and snap["kills_wrong_weapon"] == 1

    # manual controls
    assert client.post("/api/roulette/next").status_code == 200
    assert client.post("/api/roulette/reroll").status_code == 200
    assert client.post("/api/roulette/undo").status_code == 200
    # manual switch is always allowed (GSI will switch it back on the next payload if side_source == gsi)
    assert client.post("/api/side", json={"side": "CT"}).json()["side"] == "CT"


def test_manual_side_switch_allowed(client):
    client.post("/api/loadout/generate", json={})
    client.post("/api/session/start")
    assert client.post("/api/side", json={"side": "CT"}).json()["side"] == "CT"


def test_settings_roundtrip_and_persistence(client, tmp_path):
    r = client.post("/api/settings", json={"roulette": {"kills_per_weapon": 2, "auto_advance": "any"},
                                           "overlay": {"color": "#00ff00"}, "hotkeys": {"next": "F10"}})
    assert r.status_code == 200
    d = r.json()
    assert d["roulette"]["kills_per_weapon"] == 2 and d["overlay"]["color"] == "#00ff00" and d["hotkeys"]["next"] == "F10"
    with open(os.path.join(str(tmp_path), "settings.json"), encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["roulette"]["auto_advance"] == "any"


def test_gsi_cfg_download_and_install_to_custom_dir(client, tmp_path):
    r = client.get("/api/gsi/cfg")
    assert r.status_code == 200 and '"uri"' in r.text
    target = tmp_path / "cfg"
    target.mkdir()
    r = client.post("/api/gsi/install", json={"cfg_dir": str(target)})
    assert r.status_code == 200
    path = r.json()["path"]
    assert os.path.isfile(path)
    with open(path, "rb") as f:
        assert not f.read().startswith(b"\xef\xbb\xbf")


def test_sse_snapshot_first(client):
    with client.stream("GET", "/events") as r:
        assert r.status_code == 200
        chunks = []
        for line in r.iter_lines():
            chunks.append(line)
            if line.startswith("data:"):
                break
        joined = "\n".join(chunks)
        assert "event: snapshot" in joined
        data = json.loads(joined.split("data:", 1)[1].strip())
        assert "roulette" in data and "overlay" in data


def test_dpi_status_and_spin(client, monkeypatch):
    d = client.get("/api/dpi/status").json()
    assert "backend" in d and "values" in d and d["values"][0] == 800
    r = client.post("/api/settings", json={"dpi": {"backend": "fake", "spin_duration_ms": 600}})
    assert r.status_code == 200 and r.json()["dpi"]["backend"] == "fake"
    sp = client.post("/api/dpi/spin", json={"reason": "manual"}).json()
    assert sp["value"] in sp["values"]
    assert client.get("/api/state").json()["dpi"]["last_spin"]["id"] == sp["id"]
    res = client.post("/api/dpi/apply", json={"value": 1234}).json()
    assert res["ok"] and res["value"] == 1230
    assert client.post("/api/dpi/apply", json={"value": "x"}).status_code == 400
    assert client.post("/api/dpi/probe", json={}).json()["found"] is True
    assert client.post("/api/dpi/death", json={}).json()["spun"] in (True, False)
    assert client.post("/api/settings", json={"dpi": {"min_dpi": None}}).status_code == 400


def test_cross_site_requests_rejected(client):
    r = client.post("/api/session/reset", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/side", content='{"side":"CT"}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = client.get("/api/state", headers={"Host": "evil.example"})
    assert r.status_code == 403
    with client.stream("GET", "/events", headers={"Origin": "http://127.0.0.1:17890"}) as r:
        assert r.status_code == 200
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}
        for line in r.iter_lines():
            if line.startswith("data:"):
                break


def test_settings_validation_errors(client):
    assert client.post("/api/settings", json={"roulette": {"kills_per_weapon": None}}).status_code == 400
    assert client.post("/api/settings", json={"overlay": "abc"}).status_code == 400
    r = client.post("/api/settings", json={"overlay": {"scale": None, "layout": "diagonal"},
                                           "hotkeys": {"next": 5}, "roulette": {"to_dict": 1}})
    assert r.status_code == 200
    assert set(r.json()["rejected"]) == {"overlay.scale", "overlay.layout", "hotkeys.next"}
    assert client.get("/api/state").status_code == 200


def test_gsi_endpoint_never_500(client):
    s = client.get("/api/state").json()
    for body in ({"auth": True}, {"auth": "x"}, {"auth": {"token": s["gsi_token"]}, "player": True, "map": {"name": "x"}}):
        assert client.post("/gsi", json=body).status_code == 200
    assert client.post("/gsi", content="not json", headers={"Content-Type": "application/json"}).status_code == 200


def test_single_side_generate_via_api_on_fresh_state(client):
    assert client.post("/api/loadout/generate", json={"sides": ["CT"]}).status_code == 200
    assert client.get("/api/state").status_code == 200
    assert client.post("/api/side", json={"side": "CT"}).status_code == 200


def test_gsi_reasserts_side_after_manual_click(client):
    client.post("/api/loadout/generate", json={})
    client.post("/api/session/start")
    token = client.get("/api/state").json()["gsi_token"]
    base = {"auth": {"token": token}, "provider": {"steamid": "1"},
            "map": {"name": "de_dust2", "phase": "live", "round": 13, "mode": "competitive"}, "round": {"phase": "live"}}
    p = {**base, "player": {"steamid": "1", "team": "CT", "activity": "playing",
                            "state": {"health": 100, "round_kills": 0, "round_killhs": 0}, "weapons": {}}}
    client.post("/gsi", json=p)
    assert client.get("/api/state").json()["roulette"]["side"] == "CT"
    client.post("/api/side", json={"side": "T"})
    client.post("/gsi", json=p)
    assert client.get("/api/state").json()["roulette"]["side"] == "CT"


def test_gsi_install_rejects_relative_dir(client):
    assert client.post("/api/gsi/install", json={"cfg_dir": "."}).status_code == 400


def test_kill_event_has_finished_uid_and_collateral_not_wrong(client):
    client.post("/api/loadout/generate", json={})
    snap = client.post("/api/session/start").json()
    cur = snap["queue"][0]["id"]
    uid = snap["queue"][0]["uid"]
    s = client.get("/api/state").json()
    token = s["gsi_token"]

    def gsi(rk):
        return {"auth": {"token": token}, "provider": {"steamid": "1"},
                "map": {"name": "de_dust2", "phase": "live", "round": 1, "mode": "competitive"},
                "round": {"phase": "live"},
                "player": {"steamid": "1", "team": "T", "activity": "playing",
                           "state": {"health": 100, "round_kills": rk, "round_killhs": 0},
                           "weapons": {"weapon_0": {"name": f"weapon_{cur}", "state": "active"}}}}

    client.post("/gsi", json=gsi(0))
    client.post("/gsi", json=gsi(2))  # collateral: two kills in one payload
    r = client.get("/api/state").json()["roulette"]
    assert r["completed"] == 1 and r["kills_wrong_weapon"] == 0
    assert r["history"][-1]["uid"] == uid
