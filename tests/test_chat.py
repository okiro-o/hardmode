import base64
import json
import time

import pytest

from app.chat import ChatNotifier, ChatSettings, jwt_claims, render_template


def fake_jwt(channel="5f00000000000000000000aa"):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return b64({"alg": "HS256"}) + "." + b64({"user": "u1", "role": "owner", "channel": channel, "provider": "twitch"}) + ".sig"


def test_jwt_claims_and_settings():
    tok = fake_jwt()
    assert jwt_claims(tok)["channel"] == "5f00000000000000000000aa"
    assert jwt_claims("garbage") == {}
    s = ChatSettings.from_dict({"enabled": "yes", "se_jwt": " " + tok + " ", "min_interval_s": "0.5", "on_dpi": "no"})
    assert s.enabled and s.se_jwt == tok and s.min_interval_s == 0.5 and s.on_dpi is False
    assert "se_jwt" not in s.public_dict() and s.public_dict()["se_jwt_set"] is True
    with pytest.raises(ValueError):
        ChatSettings.from_dict({"weapon_template": 5})
    with pytest.raises(ValueError):
        ChatSettings.from_dict("x")


def test_render_template_tolerates_unknown_placeholders():
    assert render_template("Оружие: {weapon} ({completed}) {nope}", weapon="AK-47", completed=3) == "Оружие: AK-47 (3) {nope}"
    assert render_template("{", weapon="x") == "{"


def make_notifier(**settings):
    sent = []
    events = []
    n = ChatNotifier(ChatSettings.from_dict({"enabled": True, "se_jwt": fake_jwt(), "min_interval_s": 0.5, **settings}),
                     publish_threadsafe=lambda ev, d: events.append((ev, d)),
                     sender=lambda jwt, cid, msg: sent.append((cid, msg)))
    n.sent, n.events = sent, events
    return n


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_notifier_sends_and_coalesces_bursts():
    n = make_notifier()
    for i in range(5):
        n.notify_weapon(f"W{i}", i)
    assert wait_for(lambda: n.sent_count >= 1)
    time.sleep(0.8)
    # bursts collapse: the first message goes out, later ones collapse to the newest
    msgs = [m for _, m in n.sent]
    assert msgs[0].startswith("🎲 Оружие: W0") and msgs[-1].startswith("🎲 Оружие: W4")
    assert len(msgs) <= 3
    assert n.sent[0][0] == "5f00000000000000000000aa"
    n.stop()


def test_notifier_respects_toggles_and_disabled():
    n = make_notifier(enabled=False)
    n.notify_weapon("AK-47", 1)
    n.notify_dpi(1600)
    time.sleep(0.2)
    assert n.sent == []
    n2 = make_notifier(on_dpi=False)
    n2.notify_dpi(1600)
    n2.notify_start("AWP")
    assert wait_for(lambda: n2.sent_count >= 1)
    assert all("DPI" not in m for _, m in n2.sent)
    n2.stop()


def test_send_now_reports_errors():
    n = make_notifier()
    n.settings.se_jwt = ""
    n.settings.enabled = False
    assert n.send_now("x")["ok"] is False
    n3 = ChatNotifier(ChatSettings.from_dict({"se_jwt": fake_jwt()}), lambda *a: None,
                      sender=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    res = n3.send_now("x")
    assert res["ok"] is False and "boom" in res["error"]


def test_update_settings_clears_channel_on_new_token():
    n = make_notifier()
    n.channel = {"id": "old"}
    n.update_settings({"se_jwt": fake_jwt("bb")})
    assert n.channel == {} and n.channel_id() == "bb"
    n.update_settings({"se_jwt": ""})
    assert n.settings.se_jwt == "" and n.channel_id() is None


def test_reject_messages_rate_limited_per_user():
    n = make_notifier(reject_cooldown_s=5)
    assert n.notify_reject("Spammer", "вне диапазона") is True
    assert n.notify_reject("spammer", "вне диапазона") is False     # same user within cooldown
    assert n.notify_reject("Other", "не число") is True
    assert wait_for(lambda: n.sent_count >= 1)
    n.settings.on_reject = False
    assert n.notify_reject("Third", "x") is False
    n.stop()


def test_verify_reads_bot_state_and_joins_when_absent(monkeypatch):
    calls = []
    state = {"joined": False}

    def bot_info(*a, **k):
        return {"bot": {"enabled": True, "name": "streamelements", "joined": state["joined"], "muted": False, "mod": True}}

    def join(*a, **k):
        state["joined"] = True
        return {"status": 200, "message": "successfully joined your channel"}

    def req(method, url, jwt, body=None, timeout=8.0):
        calls.append((method, url.split("/kappa/v2")[1]))
        if url.endswith("/channels/me"):
            return {"_id": "c1", "username": "streamer", "provider": "twitch"}
        if method == "GET" and url.endswith("/bot/c1"):
            return bot_info()
        if method == "POST" and url.endswith("/bot/c1/join"):
            return join()
        raise AssertionError(url)

    n = ChatNotifier(ChatSettings(enabled=True, se_jwt=fake_jwt("c1")), lambda *a: None)
    monkeypatch.setattr(ChatNotifier, "_request", staticmethod(req))
    res = n.verify()
    assert res["ok"] and res["bot"]["joined"] is True and res["bot_problem"] is None
    assert ("POST", "/bot/c1/join") in calls
    assert n.status()["bot"]["mod"] is True
    assert any("подключ" in e["text"] for e in n.log)


def test_bot_problem_reports_muted_and_join_already_ok(monkeypatch):
    n = ChatNotifier(ChatSettings(enabled=True, se_jwt=fake_jwt("c1")), lambda *a: None)
    n.bot = {"enabled": True, "joined": True, "muted": True, "mod": False}
    assert "muted" in n.bot_problem()
    n.bot = {"enabled": True, "joined": False, "muted": False}
    assert n.bot_problem() == "бот не в канале"

    def req(method, url, jwt, body=None, timeout=8.0):
        if url.endswith("/join"):
            raise RuntimeError("StreamElements HTTP 400: The bot is already in your channel")
        return {"bot": {"enabled": True, "joined": True, "muted": False, "mod": True}}
    monkeypatch.setattr(ChatNotifier, "_request", staticmethod(req))
    assert n.join_bot()["ok"] is True and n.bot["joined"] is True


def test_deliver_keeps_last_response():
    n = ChatNotifier(ChatSettings(enabled=True, se_jwt=fake_jwt("c1")), lambda *a: None,
                     sender=lambda jwt, cid, msg: {"status": 200, "channel": cid, "message": msg})
    assert n.send_now("hi")["ok"]
    assert n.last_response == {"status": 200, "channel": "c1", "message": "hi"}
    assert n.status()["last_response"]["status"] == 200
