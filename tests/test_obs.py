import json
import socket
import threading

import pytest
from websockets.sync.server import serve

from app import obs


class FakeObs:
    """A tiny obs-websocket 5 server: Hello (with or without auth), Identify, three request types."""

    def __init__(self, password: str | None = "secret", inputs: dict | None = None, port: int = 0) -> None:
        self.password = password
        self.inputs = inputs or {}
        self.pressed: list[tuple[str, str]] = []
        self.server = serve(self.handler, "127.0.0.1", port)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handler(self, ws) -> None:
        hello = {"op": 0, "d": {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}}
        if self.password:
            hello["d"]["authentication"] = {"challenge": "ch4llenge", "salt": "s4lt"}
        ws.send(json.dumps(hello))
        ident = json.loads(ws.recv())
        assert ident["op"] == 1 and ident["d"]["rpcVersion"] == 1
        if self.password and ident["d"].get("authentication") != obs.auth_string(self.password, "s4lt", "ch4llenge"):
            ws.close(4009, "Authentication failed.")
            return
        ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        for raw in ws:
            m = json.loads(raw)["d"]
            kind, data, resp = m["requestType"], m.get("requestData") or {}, {}
            if kind == "GetInputList":
                assert data == {"inputKind": "browser_source"}
                resp = {"inputs": [{"inputName": n, "inputKind": "browser_source"} for n in self.inputs]}
            elif kind == "GetInputSettings":
                resp = {"inputSettings": self.inputs[data["inputName"]]}
            elif kind == "PressInputPropertiesButton":
                self.pressed.append((data["inputName"], data["propertyName"]))
            ws.send(json.dumps({"op": 7, "d": {"requestType": kind, "requestId": m["requestId"],
                                               "requestStatus": {"result": True, "code": 100}, "responseData": resp}}))

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def fake_obs():
    servers = []

    def make(**kw):
        s = FakeObs(**kw)
        servers.append(s)
        return s
    yield make
    for s in servers:
        s.close()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_auth_string_matches_the_protocol_docs_example():
    assert obs.auth_string("supersecretpassword", "lM1GncleQOaCu9lT1yeUZhFYnqhsLLP1G5lAGo3ixaI=",
                           "+IxH4CnCiqpX1rM9scsNynZzbOe4KhDeYcTNS3PDaeY=") == "1Ct943GAT+6YQUUX47Ia/ncufilbe6+oD6lY+5kaCu4="


@pytest.mark.parametrize("url,ok", [
    ("http://127.0.0.1:17890/overlay/weapons?scale=1.2", True), ("http://localhost:17890/overlay/dpi", True),
    ("http://127.0.0.1:17891/overlay/weapons", False), ("https://streamelements.com/overlay/x", False),
    ("http://192.168.1.5:17890/overlay/weapons", False), ("", False), ("not a url", False),
])
def test_is_our_url(url, ok):
    assert obs.is_our_url(url, 17890) is ok


def test_refresh_reloads_only_our_browser_sources(fake_obs):
    o = fake_obs(inputs={
        "Оружие": {"url": "http://127.0.0.1:17890/overlay/weapons", "width": 800},
        "DPI": {"url": "http://localhost:17890/overlay/dpi"},
        "Алерты": {"url": "https://www.donationalerts.com/widget/alerts?token=x"},
        "Локальный": {"is_local_file": True, "local_file": "C:/x.html"},
    })
    res = obs.refresh_widgets(o.port, "secret", 17890)
    assert res == {"refreshed": ["Оружие", "DPI"], "browser_sources": 4}
    assert o.pressed == [("Оружие", "refreshnocache"), ("DPI", "refreshnocache")]


def test_refresh_without_auth_and_errors(fake_obs):
    o = fake_obs(password=None, inputs={"W": {"url": "http://127.0.0.1:17890/overlay/requests"}})
    assert obs.refresh_widgets(o.port, "", 17890)["refreshed"] == ["W"]          # OBS without a password
    locked = fake_obs(password="secret")
    with pytest.raises(obs.ObsError) as e:
        obs.refresh_widgets(locked.port, "wrong", 17890)
    assert e.value.kind == "auth" and "неверный пароль" in str(e.value)
    with pytest.raises(obs.ObsError) as e:
        obs.refresh_widgets(locked.port, "", 17890)
    assert e.value.kind == "auth" and "пароль" in str(e.value)
    with pytest.raises(obs.ObsError) as e:
        obs.refresh_widgets(_free_port(), "secret", 17890, timeout=1)
    assert e.value.kind == "offline"


def test_service_status_start_and_settings(fake_obs):
    published = []
    o = fake_obs(password="pw-Q7x-secret", inputs={"W": {"url": "http://127.0.0.1:17890/overlay/weapons"}})
    svc = obs.ObsService(obs.ObsSettings(port=o.port, password="pw-Q7x-secret"), app_port=lambda: 17890,
                         publish_threadsafe=lambda ev, d: published.append((ev, d)))
    r = svc.refresh()
    assert r["ok"] and r["refreshed"] == ["W"] and "обновлено источников: 1" in r["text"]
    assert published[-1][0] == "obs" and "pw-Q7x" not in json.dumps(published[-1][1])
    # OBS not running when the app starts: not an error, the widgets will load by themselves
    svc.update_settings({"port": _free_port()})
    assert svc.settings.password == "pw-Q7x-secret"                # an absent key keeps the secret
    r = svc.refresh("start")
    assert r["ok"] and "ещё не был запущен" in r["text"]
    r = svc.refresh("manual")
    assert not r["ok"] and "не отвечает" in r["text"]
    with pytest.raises(ValueError):
        obs.ObsSettings.from_dict({"port": 0})
    assert obs.ObsSettings.from_dict({"refresh_on_start": "1", "port": "4455"}).port == 4455   # old key ignored


def test_api_settings_hide_the_password_and_refresh_route(client, fake_obs):
    o = fake_obs(password="pw-Q7x-secret", inputs={"W": {"url": "http://127.0.0.1:17890/overlay/weapons"}})
    r = client.post("/api/settings", json={"obs": {"port": o.port, "password": "pw-Q7x-secret"}})
    assert r.status_code == 200 and "pw-Q7x" not in r.text and r.json()["obs"]["password_set"] is True
    assert "pw-Q7x" not in client.get("/api/state").text and "pw-Q7x" not in client.get("/api/obs").text
    res = client.post("/api/obs/refresh").json()
    assert res["ok"] and res["refreshed"] == ["W"] and o.pressed == [("W", "refreshnocache")]
    assert client.get("/api/state").json()["obs"]["last"]["refreshed"] == ["W"]
    assert client.post("/api/settings", json={"obs": {"port": "abc"}}).status_code == 400
    client.post("/api/settings", json={"obs": {"password": ""}})
    assert client.get("/api/settings").json()["obs"]["password_set"] is False


def _service(port, password="pw"):
    published = []
    svc = obs.ObsService(obs.ObsSettings(port=port, password=password), app_port=lambda: 17890,
                         publish_threadsafe=lambda ev, d: published.append((ev, d)))
    return svc, published


def test_start_retries_until_obs_answers(fake_obs):
    """OBS and the app start together at Windows login: the WebSocket may answer only after a while."""
    import time
    port = _free_port()
    svc, _ = _service(port, "secret")
    started = []

    def later():
        time.sleep(0.4)
        started.append(fake_obs(inputs={"W": {"url": "http://127.0.0.1:17890/overlay/weapons"}}, port=port))
    threading.Thread(target=later, daemon=True).start()
    t0 = time.monotonic()
    res = svc.start_refresh(retry_for=5, every=0.1)
    assert res["ok"] and res["refreshed"] == ["W"] and svc.conn["state"] == "ok"
    assert started[0].pressed == [("W", "refreshnocache")] and time.monotonic() - t0 < 4


def test_start_gives_up_on_a_wrong_password_and_after_the_window(fake_obs):
    import time
    o = fake_obs(password="right")
    svc, _ = _service(o.port, "wrong")
    t0 = time.monotonic()
    res = svc.start_refresh(retry_for=5, every=0.1)
    assert not res["ok"] and svc.conn["state"] == "auth" and time.monotonic() - t0 < 2   # no pointless retries
    nobody, _ = _service(_free_port())
    t0 = time.monotonic()
    nobody.start_refresh(retry_for=0.3, every=0.1)
    assert nobody.conn["state"] == "offline" and time.monotonic() - t0 < 3
    assert "ещё не был запущен" in nobody.last["text"]


def test_connection_check_states_and_settings_change(fake_obs):
    import time
    o = fake_obs(password="right")
    svc, published = _service(o.port, "")
    assert svc.check(force=True)["state"] == "auth"                    # OBS wants a password, none saved
    svc.update_settings({"password": "right"})                         # a new password: checked by itself
    for _ in range(40):
        if svc.conn.get("state") == "ok":
            break
        time.sleep(0.05)
    assert svc.conn["state"] == "ok" and o.pressed == []                # a check never touches the sources
    assert svc.check()["state"] == "ok"                                 # throttled: the last answer
    svc.update_settings({"port": _free_port()})
    for _ in range(40):
        if svc.conn.get("state") == "offline":
            break
        time.sleep(0.05)
    assert svc.conn["state"] == "offline" and published[-1][1]["conn"]["state"] == "offline"


def test_api_obs_check_route(client, fake_obs):
    o = fake_obs(password=None)
    client.post("/api/settings", json={"obs": {"port": o.port}})
    assert client.post("/api/obs/check").json()["state"] == "ok"
    assert client.get("/api/state").json()["obs"]["conn"]["state"] == "ok"
