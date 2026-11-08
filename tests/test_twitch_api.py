import time
import urllib.parse

import pytest

from app import twitch_api as T

CID = "abcdef0123456789xyz"


class FakeTwitch:
    """Just enough of id.twitch.tv and Helix for the service."""

    def __init__(self) -> None:
        self.pending_polls = 1
        self.rewards: dict[str, dict] = {}
        self.redemptions: dict[str, list[dict]] = {}
        self.statuses: dict[str, str] = {}
        self.chat: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.access_ok = {"tok-1"}
        self.refresh_ok = {"ref-1": ("tok-2", "ref-2")}
        self.scopes = list(T.SCOPES)
        self.seq = 0

    def __call__(self, method, url, *, headers=None, form=None, body=None, timeout=10.0):
        u = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(u.query))
        path = u.path
        self.calls.append((method, path))
        if path == "/oauth2/device":
            assert form["client_id"] == CID and form["scopes"] == " ".join(T.SCOPES)
            return {"device_code": "dev-1", "user_code": "ABCD-EFGH", "expires_in": 1800, "interval": 1,
                    "verification_uri": "https://www.twitch.tv/activate?public=true&device-code=ABCDEFGH"}
        if path == "/oauth2/token":
            if form["grant_type"] == T.DEVICE_GRANT:
                if self.pending_polls > 0:
                    self.pending_polls -= 1
                    raise T.TwitchError("authorization_pending", 400)
                return {"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 14000, "scope": self.scopes}
            assert form["grant_type"] == "refresh_token" and "client_secret" not in form
            if form["refresh_token"] not in self.refresh_ok:
                raise T.TwitchError("Invalid refresh token", 400)
            acc, ref = self.refresh_ok.pop(form["refresh_token"])
            self.access_ok.add(acc)
            return {"access_token": acc, "refresh_token": ref, "expires_in": 14000}
        if path == "/oauth2/validate":
            return {"client_id": CID, "login": "streamer", "user_id": "42", "scopes": self.scopes}
        if path == "/oauth2/revoke":
            return {}
        # Helix
        assert headers["Client-Id"] == CID
        token = headers["Authorization"].split(" ", 1)[1]
        if token not in self.access_ok:
            raise T.TwitchError("Invalid OAuth token", 401)
        if path == "/helix/users":
            return {"data": [{"id": "42", "login": "streamer", "display_name": "Streamer", "broadcaster_type": "affiliate"}]}
        if path == "/helix/channel_points/custom_rewards":
            assert q["broadcaster_id"] == "42"
            if method == "POST":
                if any(r["title"] == body["title"] for r in self.rewards.values()):
                    raise T.TwitchError("CREATE_CUSTOM_REWARD_DUPLICATE_REWARD", 400)
                assert body["is_user_input_required"] is True and body["should_redemptions_skip_request_queue"] is False
                self.seq += 1
                rid = f"rw-{self.seq}"
                self.rewards[rid] = {"id": rid, "title": body["title"], "cost": body["cost"]}
                return {"data": [self.rewards[rid]]}
            if method == "DELETE":
                if q["id"] not in self.rewards:
                    raise T.TwitchError("Not Found", 404)
                del self.rewards[q["id"]]
                return {}
            assert q.get("only_manageable_rewards") == "true"
            return {"data": list(self.rewards.values())}
        if path == "/helix/channel_points/custom_rewards/redemptions":
            if method == "GET":
                assert q["status"] == "UNFULFILLED"
                return {"data": [r for r in self.redemptions.get(q["reward_id"], []) if r["id"] not in self.statuses]}
            assert method == "PATCH" and body["status"] in ("FULFILLED", "CANCELED")
            self.statuses[q["id"]] = body["status"]
            return {"data": [{"id": q["id"], "status": body["status"]}]}
        if path == "/helix/chat/messages":
            assert body["broadcaster_id"] == body["sender_id"] == "42"
            self.chat.append(body["message"])
            return {"data": [{"message_id": "m1", "is_sent": True}]}
        raise AssertionError(f"unexpected {method} {url}")

    def redeem(self, reward_id, rid, user, text, age=0.0):
        t = time.gmtime(time.time() - age)
        self.redemptions.setdefault(reward_id, []).append({
            "id": rid, "user_login": user.lower(), "user_name": user, "user_input": text, "status": "UNFULFILLED",
            "redeemed_at": time.strftime("%Y-%m-%dT%H:%M:%S", t) + ".123456789Z"})


def _service(tmp_path, fake, **kw):
    published = []
    svc = T.TwitchService(T.TwitchSettings(client_id=CID), str(tmp_path / "twitch.json"),
                          publish_threadsafe=lambda ev, d: published.append((ev, d)),
                          call_threadsafe=lambda fn, *a: fn(*a))
    svc._http = fake
    return svc, published


def _signed_in(svc):
    svc._finish_login({"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 14000})


def test_settings_client_id():
    assert T.TwitchSettings.from_dict({"client_id": "  " + CID + " "}).client_id == CID
    with pytest.raises(ValueError):
        T.TwitchSettings.from_dict({"client_id": "not a client id!"})


def test_device_code_login(tmp_path):
    fake = FakeTwitch()
    svc, published = _service(tmp_path, fake)
    flow = svc.start_login()
    assert flow["user_code"] == "ABCD-EFGH" and "twitch.tv/activate" in flow["uri"]
    for _ in range(60):
        if svc.logged_in:
            break
        time.sleep(0.1)
    assert svc.logged_in and svc.can_chat and svc.login_flow is None
    st = svc.status()
    assert st["user"] == {"login": "streamer", "display_name": "Streamer", "broadcaster_type": "affiliate"}
    assert "tok-1" not in str(st) and "ref-1" not in str(st)          # tokens never leave the server
    # remembered across restarts
    again, _ = _service(tmp_path, fake)
    assert again.logged_in
    svc.logout()
    assert not svc.logged_in and ("POST", "/oauth2/revoke") in fake.calls
    svc._stop.set()


def test_expired_token_is_refreshed_and_a_dead_refresh_signs_out(tmp_path):
    fake = FakeTwitch()
    svc, _ = _service(tmp_path, fake)
    _signed_in(svc)
    svc.state["token"]["expires_at"] = time.time() - 5                 # expired: refreshed before the call
    assert svc.helix("GET", "users")["data"][0]["id"] == "42"
    assert svc.state["token"]["access_token"] == "tok-2" and svc.state["token"]["refresh_token"] == "ref-2"
    fake.access_ok.clear()                                             # revoked in Twitch: 401 → refresh fails
    with pytest.raises(T.TwitchError):
        svc.helix("GET", "users")
    assert not svc.logged_in and "войдите" in svc.status()["error"]


def test_create_duplicate_delete_and_sync_rewards(tmp_path):
    fake = FakeTwitch()
    svc, _ = _service(tmp_path, fake)
    with pytest.raises(T.TwitchError):
        svc.create_reward("weapon")                                    # not signed in
    _signed_in(svc)
    r = svc.create_reward("weapon", cost="250")
    assert r["title"] == "Оружие в очередь" and r["cost"] == 250 and svc.managed_kind(r["id"]) == "weapon"
    with pytest.raises(T.TwitchError) as e:
        svc.create_reward("dpi", title="Оружие в очередь")
    assert "уже есть награда" in str(e.value)
    d = svc.create_reward("dpi", title="DPI", cost=100)
    del fake.rewards[d["id"]]                                          # deleted in the Twitch dashboard
    svc._sync_rewards()
    assert set(svc.state["rewards"]) == {"weapon"}
    svc.delete_reward("weapon")
    assert svc.state["rewards"] == {} and fake.rewards == {}


def test_redemptions_are_answered_once_and_stale_ones_refunded(tmp_path):
    fake = FakeTwitch()
    svc, _ = _service(tmp_path, fake)
    _signed_in(svc)
    w = svc.create_reward("weapon")["id"]
    seen = []
    decisions = {"keep me": "keep", "ok": "fulfill", "bad": "cancel"}
    svc.on_redemption = lambda kind, r: seen.append((kind, r["user"], r["text"])) or decisions[r["text"]]
    fake.redeem(w, "r1", "Alice", "keep me")
    fake.redeem(w, "r2", "Bob", "ok")
    fake.redeem(w, "r3", "Carl", "bad")
    fake.redeem(w, "r4", "Dan", "ok", age=3600)                        # the app was off: refund, no handler
    assert svc.poll_once() == 4
    svc.flush_actions()
    assert seen == [("weapon", "Alice", "keep me"), ("weapon", "Bob", "ok"), ("weapon", "Carl", "bad")]
    assert fake.statuses == {"r2": "FULFILLED", "r3": "CANCELED", "r4": "CANCELED"}
    assert svc.poll_once() == 0                                       # r1 stays UNFULFILLED but is not repeated
    again, _ = _service(tmp_path, fake)                                # nor after a restart
    again.on_redemption = lambda kind, r: pytest.fail("handled twice")
    assert again.poll_once() == 0
    svc.set_status({"reward_id": w, "id": "r1"}, "CANCELED")           # the request was dismissed: refund
    svc.flush_actions()
    assert fake.statuses["r1"] == "CANCELED" and "r1" not in svc.state["kept"]


def test_send_chat_as_the_streamer(tmp_path):
    fake = FakeTwitch()
    svc, _ = _service(tmp_path, fake)
    with pytest.raises(T.TwitchError):
        svc.send_chat("hi")
    _signed_in(svc)
    svc.send_chat("🎲 привет")
    assert fake.chat == ["🎲 привет"]
    fake.scopes = ["channel:manage:redemptions"]                       # an old sign-in without the chat scope
    svc._finish_login({"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 14000})
    assert not svc.can_chat


# ----------------------------------------------------------------------------- the app
@pytest.fixture
def app_twitch(client, tmp_path):
    st = client.app.state.app
    fake = FakeTwitch()
    svc = st.twitch_api
    svc.stop()                                                         # no background polling: the test drives it
    if svc._thread:
        svc._thread.join(timeout=2)
    svc._http = fake
    svc._call = lambda fn, *a: fn(*a)                                  # dispatch synchronously in tests
    client.post("/api/settings", json={"twitch": {"client_id": CID}})
    _signed_in(svc)
    return st, svc, fake


def test_app_rewards_refund_rejected_and_dismissed_requests(client, app_twitch, monkeypatch):
    st, svc, fake = app_twitch
    said, rejected = [], []
    monkeypatch.setattr(st.chat, "notify_request", lambda user, weapon, pos, result="": said.append((user, weapon)))
    monkeypatch.setattr(st.chat, "notify_reject", lambda user, reason: rejected.append((user, reason)) or True)
    r = client.post("/api/twitch/reward", json={"kind": "weapon", "cost": 300}).json()
    assert r["ok"] and client.get("/api/settings").json()["donations"]["reward_id"] == r["reward"]["id"]
    assert client.get("/api/settings").json()["donations"]["reward_enabled"] is True
    w = r["reward"]["id"]
    d = client.post("/api/twitch/reward", json={"kind": "dpi"}).json()["reward"]["id"]
    assert client.get("/api/settings").json()["dpi"]["viewer_reward_id"] == d
    fake.redeem(w, "a1", "Alice", "дай авп")
    fake.redeem(w, "a2", "Bob", "что-нибудь мощное")
    fake.redeem(d, "d1", "Carl", "1600")
    fake.redeem(d, "d2", "Dan", "99999")
    svc.poll_once()
    svc.flush_actions()
    assert fake.statuses == {"a2": "CANCELED", "d1": "FULFILLED", "d2": "CANCELED"}   # a1 waits in the queue
    pend = client.get("/api/queue").json()["pending"]
    assert [(e["weapon"], e["user"], e["redemption"]["id"]) for e in pend] == [("awp", "Alice", "a1")]
    assert said == [("Alice", "AWP")]
    assert [u for u, _ in rejected] == ["Bob", "Dan"] and all("Баллы вернул" in t for _, t in rejected)
    assert any(v["value"] == 1600 and v["user"] == "Carl" for v in client.get("/api/state").json()["dpi"]["wheel"]["viewer"])
    # the streamer dismisses the request: the points go back
    client.post("/api/queue/dismiss", json={"id": pend[0]["id"]})
    svc.flush_actions()
    assert fake.statuses["a1"] == "CANCELED"
    # a fulfilled one (✓) is marked done in Twitch
    fake.redeem(w, "a3", "Eve", "калаш")
    svc.poll_once()
    e = client.get("/api/queue").json()["pending"][0]
    client.post("/api/queue/done", json={"id": e["id"]})
    svc.flush_actions()
    assert fake.statuses["a3"] == "FULFILLED"
    # the same reward seen in chat (IRC) is ignored: no double request
    client.post("/api/twitch/chat", json={"user": "Eve", "text": "калаш", "reward_id": w})
    assert client.get("/api/queue").json()["pending"] == []
    # switched off in the app: refund
    client.post("/api/settings", json={"donations": {"reward_enabled": False}})
    fake.redeem(w, "a4", "Fox", "awp")
    svc.poll_once()
    svc.flush_actions()
    assert fake.statuses["a4"] == "CANCELED"
    # deleting the reward clears the ids it set
    assert client.post("/api/twitch/reward/delete", json={"kind": "weapon"}).json()["ok"]
    assert client.get("/api/settings").json()["donations"]["reward_id"] == ""


def test_app_chat_from_the_streamer_account(client, app_twitch):
    st, svc, fake = app_twitch
    client.post("/api/settings", json={"chat": {"enabled": True, "provider": "twitch"}})
    assert st.chat.can_send()                                          # no StreamElements token needed
    assert client.post("/api/chat/test", json={}).json().get("ok") is True
    assert fake.chat and fake.chat[-1]
    svc.logout(revoke=False)
    assert not st.chat.can_send()
    client.post("/api/settings", json={"chat": {"provider": "se"}})
    assert client.get("/api/settings").json()["chat"]["provider"] == "se"


def test_app_login_route_without_client_id(client):
    r = client.post("/api/twitch/login").json()
    assert r["ok"] is False and "Client ID" in r["error"]
    assert client.post("/api/settings", json={"twitch": {"client_id": "bad id"}}).status_code == 400
    assert "access_token" not in client.get("/api/twitch/account").text
