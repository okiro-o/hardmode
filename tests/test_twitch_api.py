import time
import urllib.parse

import pytest

from app import twitch_api as T
from tests.conftest import wait_until as _wait

CID = T.BUILTIN_CLIENT_ID


class FakeTwitch:
    """Just enough of id.twitch.tv and Helix for the service."""

    def __init__(self, cid: str = CID) -> None:
        self.cid = cid
        self.pending_polls = 1
        self.rewards: dict[str, dict] = {}
        self.redemptions: dict[str, list[dict]] = {}
        self.statuses: dict[str, str] = {}
        self.chat: list[str] = []
        self.patches: list[dict] = []
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
            assert form["client_id"] == self.cid and form["scopes"] == " ".join(T.SCOPES)
            return {"device_code": "dev-1", "user_code": "ABCD-EFGH", "expires_in": 1800, "interval": 0.01,
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
            return {"client_id": self.cid, "login": "streamer", "user_id": "42", "scopes": self.scopes}
        if path == "/oauth2/revoke":
            return {}
        # Helix
        assert headers["Client-Id"] == self.cid
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
                self.rewards[rid] = {"id": rid, "title": body["title"], "cost": body["cost"], "prompt": body["prompt"],
                                     "is_enabled": True, "is_paused": False,
                                     "is_user_input_required": body["is_user_input_required"],
                                     "should_redemptions_skip_request_queue": body["should_redemptions_skip_request_queue"]}
                return {"data": [dict(self.rewards[rid])]}
            if method == "PATCH":
                self.patches.append(dict(body))
                self.rewards[q["id"]].update(body)
                return {"data": [dict(self.rewards[q["id"]])]}
            if method == "DELETE":
                if q["id"] not in self.rewards:
                    raise T.TwitchError("Not Found", 404)
                del self.rewards[q["id"]]
                return {}
            assert q.get("only_manageable_rewards") == "true"
            return {"data": [dict(r) for r in self.rewards.values()]}
        if path == "/helix/channel_points/custom_rewards/redemptions":
            if method == "GET":
                assert q["status"] == "UNFULFILLED"
                rows = [r for r in self.redemptions.get(q["reward_id"], []) if r["id"] not in self.statuses]
                page = getattr(self, "page_size", 0)
                if page:                                                   # Helix pagination by cursor
                    start = int(q.get("after") or 0)
                    more = start + page < len(rows)
                    return {"data": rows[start:start + page], "pagination": {"cursor": str(start + page)} if more else {}}
                return {"data": rows}
            if getattr(self, "patch_fail", None):
                raise self.patch_fail
            assert method == "PATCH" and body["status"] in ("FULFILLED", "CANCELED")
            self.statuses[q["id"]] = body["status"]
            return {"data": [{"id": q["id"], "status": body["status"]}]}
        if path == "/helix/streams":
            assert q["user_id"] == "42"
            return {"data": [dict(self.live)] if getattr(self, "live", None) else []}
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
    svc = T.TwitchService(str(tmp_path / "twitch.json"),
                          publish_threadsafe=lambda ev, d: published.append((ev, d)),
                          call_threadsafe=lambda fn, *a: fn(*a))
    svc._http = fake
    return svc, published


def _signed_in(svc):
    svc._finish_login({"access_token": "tok-1", "refresh_token": "ref-1", "expires_in": 14000})


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
    assert r["title"] == "Добавить оружие в очередь" and r["cost"] == 250 and svc.state["rewards"]["weapon"]["id"] == r["id"]
    with pytest.raises(T.TwitchError) as e:
        svc.create_reward("weapon")                                    # the same fixed title is already there
    assert "уже есть награда «Добавить оружие в очередь»" in str(e.value)
    d = svc.create_reward("dpi", cost=100)
    assert d["title"] == "Добавить DPI в рулетку"
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
    _signed_in(svc)
    return st, svc, fake


def test_app_rewards_refund_rejected_and_dismissed_requests(client, app_twitch, monkeypatch):
    st, svc, fake = app_twitch
    said, rejected = [], []
    monkeypatch.setattr(st.chat, "notify_request", lambda user, weapon, pos, result="": said.append((user, weapon)))
    monkeypatch.setattr(st.chat, "notify_reject", lambda user, reason: rejected.append((user, reason)) or True)
    r = client.post("/api/twitch/reward", json={"kind": "weapon", "cost": 300}).json()
    assert r["ok"] and r["reward"]["title"] == "Добавить оружие в очередь"
    assert client.get("/api/settings").json()["donations"]["reward_enabled"] is True     # switched on with it
    w = r["reward"]["id"]
    d = client.post("/api/twitch/reward", json={"kind": "dpi"}).json()["reward"]["id"]
    assert client.get("/api/settings").json()["dpi"]["viewer_enabled"] is True
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
    assert [u for u, _ in rejected] == ["Bob", "Dan"] and all("Баллы возвращены" in t for _, t in rejected)
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
    # deleting the reward forgets it
    assert client.post("/api/twitch/reward/delete", json={"kind": "weapon"}).json()["ok"]
    assert "weapon" not in client.get("/api/twitch/account").json()["rewards"]


def test_app_chat_from_the_streamer_account(client, app_twitch):
    st, svc, fake = app_twitch
    client.post("/api/settings", json={"chat": {"enabled": True}})
    assert st.chat.can_send()                                          # the Twitch sign-in is all it needs
    assert client.post("/api/chat/test", json={}).json().get("ok") is True
    assert fake.chat and fake.chat[-1]
    svc.logout(revoke=False)
    assert not st.chat.can_send()
    assert client.post("/api/chat/test", json={}).json()["ok"] is False
    assert client.post("/api/chat/verify").status_code in (404, 405)   # the StreamElements routes are gone


def test_app_login_with_the_builtin_client_id(client):
    st = client.app.state.app
    svc = st.twitch_api
    svc.stop()
    if svc._thread:
        svc._thread.join(timeout=2)
    svc._stop.clear()                                                  # the sign-in thread waits on the same event
    fake = FakeTwitch(cid=T.BUILTIN_CLIENT_ID)
    svc._http = fake
    assert "client_id" not in client.get("/api/twitch/account").json()   # nothing to paste: one button
    r = client.post("/api/twitch/login").json()
    assert r["ok"] and r["user_code"] == "ABCD-EFGH" and "twitch.tv/activate" in r["uri"]
    for _ in range(60):
        if svc.logged_in:
            break
        time.sleep(0.1)
    assert svc.logged_in and "access_token" not in client.get("/api/twitch/account").text
    svc.logout(revoke=False)
    # an old settings file with the removed «twitch» section still loads
    assert client.post("/api/settings", json={"twitch": {"client_id": "x"}}).status_code == 200
    assert "twitch" not in client.get("/api/settings").json()
    svc._stop.set()


def test_reward_description_pause_and_guarded_settings(tmp_path):
    fake = FakeTwitch()
    svc, published = _service(tmp_path, fake)
    _signed_in(svc)
    r = svc.create_reward("weapon", cost=400, prompt="  Пиши оружие  ")
    assert r["prompt"] == "Пиши оружие" and fake.rewards[r["id"]]["prompt"] == "Пиши оружие"
    assert fake.rewards[r["id"]]["is_user_input_required"] is True
    assert fake.rewards[r["id"]]["should_redemptions_skip_request_queue"] is False
    svc.update_reward("weapon", prompt="AWP, калаш, плётка\nили дигл")
    assert fake.rewards[r["id"]]["prompt"] == "AWP, калаш, плётка\nили дигл"     # line breaks kept
    svc.update_reward("weapon", cost="750")
    assert fake.rewards[r["id"]]["cost"] == 750 and svc.state["rewards"]["weapon"]["cost"] == 750
    assert fake.patches[-1] == {"cost": 750}                           # only what changed is sent
    with pytest.raises(T.TwitchError):
        svc.update_reward("weapon", cost="много")
    with pytest.raises(T.TwitchError):
        svc.update_reward("dpi", prompt="x")                            # not created yet
    # end of stream: the streamer pauses it in Twitch and (by mistake) lets it skip the queue without text
    fake.rewards[r["id"]].update({"is_paused": True, "is_enabled": False, "is_user_input_required": False,
                                  "should_redemptions_skip_request_queue": True, "cost": 600})
    fake.patches.clear()
    svc._sync_rewards()
    own = svc.state["rewards"]["weapon"]
    assert (own["paused"], own["enabled"], own["cost"]) == (True, False, 600)      # taken over, not undone
    assert fake.patches == [{"is_user_input_required": True, "should_redemptions_skip_request_queue": False}]
    assert fake.rewards[r["id"]]["is_paused"] is True and fake.rewards[r["id"]]["is_enabled"] is False
    fake.patches.clear()
    n = len(published)
    svc._sync_rewards()                                                  # nothing changed: no writes, no events
    assert fake.patches == [] and len(published) == n


def test_app_reward_routes_with_description(client, app_twitch):
    st, svc, fake = app_twitch
    r = client.post("/api/twitch/reward", json={"kind": "dpi", "cost": 200}).json()
    assert r["ok"] and r["reward"]["prompt"].startswith("Число DPI от ")          # range from the DPI settings
    r = client.post("/api/twitch/reward", json={"kind": "weapon", "prompt": "Какую пушку?"}).json()
    assert r["reward"]["prompt"] == "Какую пушку?"
    r = client.post("/api/twitch/reward/update", json={"kind": "dpi", "cost": 450, "prompt": "Пиши число"}).json()
    assert r["ok"] and (r["reward"]["cost"], r["reward"]["prompt"]) == (450, "Пиши число")
    own = client.get("/api/twitch/account").json()["rewards"]["dpi"]
    assert (own["cost"], own["prompt"]) == (450, "Пиши число")
    assert client.post("/api/twitch/reward/update", json={"kind": "nope", "prompt": "x"}).json()["ok"] is False


def test_sync_now_shows_a_reward_switched_off_in_twitch(client, app_twitch):
    st, svc, fake = app_twitch
    r = client.post("/api/twitch/reward", json={"kind": "dpi"}).json()["reward"]
    fake.rewards[r["id"]]["is_enabled"] = False                         # «деактивировал» in the Twitch dashboard
    svc._last_sync = 0.0
    acc = client.post("/api/twitch/sync").json()
    assert acc["rewards"]["dpi"]["enabled"] is False                    # at once, not after the next round
    fake.rewards[r["id"]]["is_enabled"] = True
    calls = len(fake.calls)
    acc = client.post("/api/twitch/sync").json()                        # asked again right away: throttled
    assert acc["rewards"]["dpi"]["enabled"] is False and len(fake.calls) == calls
    svc._last_sync = 0.0
    assert client.post("/api/twitch/sync").json()["rewards"]["dpi"]["enabled"] is True
    svc.logout(revoke=False)
    assert client.post("/api/twitch/sync").json()["logged_in"] is False  # signed out: nothing to read



def test_app_switch_turns_the_reward_on_and_off_in_twitch(client, app_twitch):
    st, svc, fake = app_twitch
    rid = client.post("/api/twitch/reward", json={"kind": "dpi"}).json()["reward"]["id"]
    assert client.get("/api/settings").json()["dpi"]["viewer_enabled"] is True     # created → switched on
    fake.patches.clear()
    client.post("/api/settings", json={"dpi": {"viewer_min": 500}})               # another setting: untouched
    time.sleep(0.05)      # a wrongly started on/off thread would have called Twitch by now
    assert fake.patches == []
    assert client.post("/api/settings", json={"dpi": {"viewer_enabled": False}}).status_code == 200
    assert _wait(lambda: fake.rewards[rid]["is_enabled"] is False)
    assert fake.patches == [{"is_enabled": False}] and svc.state["rewards"]["dpi"]["enabled"] is False
    client.post("/api/settings", json={"dpi": {"viewer_enabled": True}})
    assert _wait(lambda: fake.rewards[rid]["is_enabled"] is True)
    # switched off in Twitch while the app switch stays on: the app does not switch it back on
    fake.rewards[rid]["is_enabled"] = False
    svc._last_sync = 0.0
    client.post("/api/twitch/sync")
    fake.patches.clear()
    client.post("/api/settings", json={"dpi": {"viewer_max": 6000}})
    time.sleep(0.05)      # a wrongly started on/off thread would have called Twitch by now
    assert fake.patches == [] and fake.rewards[rid]["is_enabled"] is False
    # the weapon switch follows its own reward; no reward yet: nothing to switch
    client.post("/api/settings", json={"donations": {"reward_enabled": True}})
    time.sleep(0.05)      # a wrongly started on/off thread would have called Twitch by now
    assert fake.patches == []
    wid = client.post("/api/twitch/reward", json={"kind": "weapon"}).json()["reward"]["id"]
    client.post("/api/settings", json={"donations": {"reward_enabled": False}})
    assert _wait(lambda: fake.rewards[wid]["is_enabled"] is False)


def test_refund_all_points_requests_at_the_end_of_a_stream(client, app_twitch):
    st, svc, fake = app_twitch
    assert client.post("/api/queue/refund").json() == {"ok": True, "count": 0}       # nothing to give back
    w = client.post("/api/twitch/reward", json={"kind": "weapon"}).json()["reward"]["id"]
    fake.redeem(w, "p1", "Alice", "авп")
    fake.redeem(w, "p2", "Bob", "калаш")
    svc.poll_once()
    client.post("/api/queue/add", json={"weapon": "nova", "user": "Донатер"})            # not paid with points
    st.queue.add("deagle", "дигл", "Carl", 300, "RUB")                                   # a donation
    published = []
    real = st.queue._publish
    st.queue._publish = lambda ev, d: (published.append(ev), real(ev, d))
    r = client.post("/api/queue/refund").json()
    st.queue._publish = real
    assert r == {"ok": True, "count": 2} and published == ["queue"]                    # one update for all
    svc.flush_actions()
    assert fake.statuses == {"p1": "CANCELED", "p2": "CANCELED"}
    assert sorted(e["user"] for e in st.queue.pending()) == ["Carl", "Донатер"]
    hist = {e["user"]: e["status"] for e in client.get("/api/queue").json()["history"]}
    assert hist["Alice"] == hist["Bob"] == "dismissed"
    # signed out: refusing is better than dropping requests without giving the points back
    fake.redeem(w, "p3", "Dan", "awp")
    svc.poll_once()
    svc.logout(revoke=False)
    res = client.post("/api/queue/refund")
    assert res.status_code == 400 and "Twitch" in res.text
    assert any(e["user"] == "Dan" for e in st.queue.pending())


def test_stream_start_and_end_with_a_grace_for_dropped_connections(tmp_path):
    fake = FakeTwitch()
    svc, _ = _service(tmp_path, fake)
    _signed_in(svc)
    seen = []
    svc.on_stream = seen.append
    svc.poll_stream(now=0)                                             # first answer: offline, said once
    svc.poll_stream(now=60)
    assert seen == [{"live": False}]
    fake.live = {"id": "s1", "title": "Хардмод", "started_at": "2026-11-14T18:00:00Z", "type": "live"}
    svc.poll_stream(now=120)
    assert seen[-1]["live"] and seen[-1]["id"] == "s1" and seen[-1]["title"] == "Хардмод" and seen[-1]["started_at"] > 0
    fake.live = None                                                   # the connection dropped
    svc.poll_stream(now=180)
    fake.live = {"id": "s1", "title": "Хардмод", "started_at": "2026-11-14T18:00:00Z"}
    svc.poll_stream(now=300)                                           # back within the grace: the same stream
    assert len(seen) == 2
    fake.live = None
    svc.poll_stream(now=400)
    svc.poll_stream(now=400 + T.OFFLINE_GRACE_S - 1)
    assert len(seen) == 2
    svc.poll_stream(now=400 + T.OFFLINE_GRACE_S)
    assert seen[-1] == {"live": False} and len(seen) == 3
    svc._stop.set()


def test_status_changes_survive_network_errors_and_restarts(tmp_path):
    """0.38.0: a refund that failed on the network is kept in twitch.json and sent later, never dropped."""
    fake = FakeTwitch()
    svc, _ = _service(tmp_path, fake)
    _signed_in(svc)
    svc.set_status({"reward_id": "rw-1", "id": "r1"}, "CANCELED")
    svc.set_status({"reward_id": "rw-1", "id": "r2"}, "FULFILLED")
    fake.patch_fail = T.TwitchError("network down", None)
    with pytest.raises(T.TwitchError):
        svc.flush_actions()
    assert fake.statuses == {} and len(svc.state["actions"]) == 2
    again, _ = _service(tmp_path, fake)                                # the app restarted meanwhile
    assert again.state["actions"] == [["rw-1", "r1", "CANCELED"], ["rw-1", "r2", "FULFILLED"]]
    fake.patch_fail = None
    assert again.flush_actions() == 2 and fake.statuses == {"r1": "CANCELED", "r2": "FULFILLED"}
    assert again.state["actions"] == []
    # Twitch refuses (already handled in the dashboard): dropped, not retried forever
    again.set_status({"reward_id": "rw-1", "id": "r3"}, "CANCELED")
    fake.patch_fail = T.TwitchError("redemption is not UNFULFILLED", 400)
    assert again.flush_actions() == 0 and again.state["actions"] == []
    svc._stop.set()


def test_all_pages_of_open_redemptions_are_read(tmp_path):
    fake = FakeTwitch()
    svc, _ = _service(tmp_path, fake)
    _signed_in(svc)
    svc.create_reward("weapon")
    rid = svc.state["rewards"]["weapon"]["id"]
    for i in range(7):
        fake.redeem(rid, f"r{i}", "Viewer", "awp")
    svc.state["kept"] = {f"r{i}": rid for i in range(5)}                # accepted earlier, still waiting in the queue
    fake.page_size = 2
    seen = []
    svc.on_redemption = lambda kind, item: seen.append(item["id"]) or "keep"
    svc._call = lambda fn, *a: fn(*a)
    assert svc.poll_once() == 2 and seen == ["r5", "r6"]
    svc._stop.set()
