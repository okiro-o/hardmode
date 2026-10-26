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
