import time

import pytest

from app import predictions as P
from app import twitch_api as T
from app.gsi import GsiTracker
from tests.test_gsi import payload
from tests.test_twitch_api import FakeTwitch, _service, _signed_in
from tests.conftest import wait_until as _wait


class FakePredictions(FakeTwitch):
    """FakeTwitch plus Helix «Create Prediction» / «End Prediction»."""

    def __init__(self) -> None:
        super().__init__()
        self.predictions: dict[str, dict] = {}

    def __call__(self, method, url, *, headers=None, form=None, body=None, timeout=10.0):
        if "/helix/predictions" not in url:
            return super().__call__(method, url, headers=headers, form=form, body=body, timeout=timeout)
        self.calls.append((method, "/helix/predictions"))
        if T.SCOPES[-1] not in self.scopes:
            raise T.TwitchError("Missing scope: channel:manage:predictions", 403)
        assert body["broadcaster_id"] == "42"
        if method == "POST":
            if any(p["status"] in ("ACTIVE", "LOCKED") for p in self.predictions.values()):
                raise T.TwitchError("prediction already active", 400)
            assert len(body["title"]) <= 45 and 30 <= body["prediction_window"] <= 1800
            self.seq += 1
            pid = f"pr-{self.seq}"
            self.predictions[pid] = {"id": pid, "title": body["title"], "status": "ACTIVE", "winner": None,
                                     "outcomes": [{"id": f"{pid}-{i}", "title": o["title"]} for i, o in enumerate(body["outcomes"])]}
            return {"data": [dict(self.predictions[pid])]}
        assert method == "PATCH"
        p = self.predictions.get(body["id"])
        if not p or p["status"] in ("RESOLVED", "CANCELED"):
            raise T.TwitchError("prediction is not active", 400)
        p["status"] = body["status"]
        p["winner"] = body.get("winning_outcome_id")
        return {"data": [dict(p)]}


def _svc(tmp_path, fake=None, **settings):
    fake = fake or FakePredictions()
    tw, _ = _service(tmp_path, fake)
    _signed_in(tw)
    published = []
    svc = P.PredictionService(P.PredictionSettings.from_dict({"enabled": True, **settings}), tw,
                              str(tmp_path / "predictions.json"), publish_threadsafe=lambda ev, d: published.append((ev, d)))
    return svc, fake, published



def test_settings_validation_and_title():
    s = P.PredictionSettings.from_dict({"enabled": "1", "window_min": "5", "win": "Затащу", "loss": " "})
    assert s.enabled and s.window_min == 5 and s.win == "Затащу" and s.loss == "Поражение"
    assert s.title_for("de_mirage") == "Победа на Mirage?" and s.title_for(None) == "Победа на этой карте?"
    assert len(P.PredictionSettings.from_dict({"title": "x" * 80 + " {map}"}).title_for("de_ancient")) == 45
    for bad in ({"window_min": 0}, {"window_min": 31}, {"window_min": "abc"}, {"win": "Да", "loss": "да"}):
        with pytest.raises(ValueError):
            P.PredictionSettings.from_dict(bad)


def test_win_resolves_and_draw_refunds(tmp_path):
    svc, fake, published = _svc(tmp_path, window_min=2)
    cur = svc.start("de_inferno")
    p = fake.predictions[cur["id"]]
    assert p["title"] == "Победа на Inferno?" and [o["title"] for o in p["outcomes"]] == ["Победа", "Поражение"]
    assert svc.status()["current"]["locks_at"] == pytest.approx(cur["started_at"] + 120)
    svc.finish("win")
    assert p["status"] == "RESOLVED" and p["winner"] == p["outcomes"][0]["id"]
    assert svc.current is None and svc.last["text"] == "итог: Победа" and published[-1][0] == "predictions"
    cur = svc.start("de_inferno")
    svc.finish("draw")
    assert fake.predictions[cur["id"]]["status"] == "CANCELED" and "ничья" in svc.last["text"]
    cur = svc.start(None)
    svc.finish(None)                                       # no team score: points back
    assert fake.predictions[cur["id"]]["status"] == "CANCELED"


def test_abandoned_match_is_cancelled_before_the_next_and_state_survives_a_restart(tmp_path):
    svc, fake, _ = _svc(tmp_path)
    first = svc.start("de_mirage")
    again = P.PredictionService(svc.settings, svc.twitch, svc.state_path, publish_threadsafe=lambda ev, d: None)
    assert again.current["id"] == first["id"]               # a restart mid-match can still resolve it
    second = again.start("de_dust2")
    assert fake.predictions[first["id"]]["status"] == "CANCELED" and fake.predictions[second["id"]]["status"] == "ACTIVE"
    fake.predictions[second["id"]]["status"] = "CANCELED"    # the streamer cancelled it in the Twitch dashboard
    again.finish("loss")
    assert again.current is None and "уже был завершён" in again.last["text"]


def test_hooks_respect_switch_mode_and_permission(tmp_path):
    svc, fake, _ = _svc(tmp_path)
    svc.on_match_start("de_mirage", "deathmatch")
    svc.settings.enabled = False
    svc.on_match_start("de_mirage", "competitive")
    time.sleep(0.2)
    assert not fake.predictions
    svc.settings.enabled = True
    svc.on_match_start("de_mirage", "competitive")
    assert _wait(lambda: svc.current is not None)
    svc.on_match_end("loss")
    assert _wait(lambda: svc.current is None)
    p = next(iter(fake.predictions.values()))
    assert p["status"] == "RESOLVED" and p["winner"] == p["outcomes"][1]["id"]
    # an older sign-in without the predictions permission: nothing is attempted, the panel asks to sign in again
    svc.twitch.state["user"]["scopes"] = ["channel:manage:redemptions", "user:write:chat"]
    assert not svc.allowed and not svc.status()["allowed"]
    with pytest.raises(ValueError):
        svc.start("de_mirage")


def test_twitch_refusal_is_shown(tmp_path):
    fake = FakePredictions()
    svc, _, _ = _svc(tmp_path, fake)
    fake.scopes = ["channel:manage:redemptions"]            # Twitch itself refuses (e.g. scope revoked)
    with pytest.raises(T.TwitchError):
        svc.start("de_mirage")
    assert "разрешите прогнозы" in svc.status()["error"] and svc.current is None and not svc.busy


def test_gsi_match_start_only_in_the_first_live_round():
    t = GsiTracker()
    t.handle(payload(phase="warmup", round_=0), now=1.0)
    ev = t.handle(payload(phase="live", round_=0), now=2.0)
    assert [e for e in ev if e["type"] == "match_start"] == [{"type": "match_start", "map": "de_mirage", "mode": "competitive"}]
    assert not [e for e in t.handle(payload(phase="live", round_=0), now=3.0) if e["type"] == "match_start"]
    t.handle(payload(phase="intermission", round_=12), now=4.0)                  # half-time
    assert not [e for e in t.handle(payload(phase="live", round_=12), now=5.0) if e["type"] == "match_start"]


def test_api_routes_and_settings(client, tmp_path):
    st = client.app.state.app
    fake = FakePredictions()
    tw = st.twitch_api
    tw.stop()
    if tw._thread:
        tw._thread.join(timeout=2)
    tw._http = fake
    r = client.post("/api/predictions/start").json()
    assert not r["ok"] and "Twitch" in r["error"]                # not signed in yet
    _signed_in(tw)
    r = client.post("/api/settings", json={"predictions": {"enabled": True, "window_min": 4, "title": "Затащу {map}?"}})
    assert r.status_code == 200 and r.json()["predictions"]["window_min"] == 4
    assert client.post("/api/settings", json={"predictions": {"window_min": 99}}).status_code == 400
    assert client.post("/api/predictions/start").json()["ok"]
    p = next(iter(fake.predictions.values()))
    assert p["title"] == "Затащу этой карте?" and p["status"] == "ACTIVE"
    assert client.get("/api/state").json()["predictions"]["current"]["title"] == p["title"]
    assert client.post("/api/predictions/finish", json={"result": "maybe"}).status_code == 400
    assert client.post("/api/predictions/finish", json={"result": "cancel"}).json()["ok"]
    assert p["status"] == "CANCELED" and client.get("/api/predictions").json()["current"] is None
