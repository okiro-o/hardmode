import time

import pytest

from app.chat import ChatNotifier, ChatSettings, render_template


def test_settings():
    s = ChatSettings.from_dict({"enabled": "yes", "min_interval_s": "0.5", "on_dpi": "no",
                                "se_jwt": "old", "provider": "se", "channel_login": "x"})   # removed keys: ignored
    assert s.enabled and s.min_interval_s == 0.5 and s.on_dpi is False
    assert not hasattr(s, "se_jwt") and "provider" not in s.to_dict()
    with pytest.raises(ValueError):
        ChatSettings.from_dict({"weapon_template": 5})
    with pytest.raises(ValueError):
        ChatSettings.from_dict("x")


def test_render_template_tolerates_unknown_placeholders():
    assert render_template("Оружие: {weapon} ({completed}) {nope}", weapon="AK-47", completed=3) == "Оружие: AK-47 (3) {nope}"
    assert render_template("{", weapon="x") is None                  # cannot be filled: no message at all


def make_notifier(ready=True, **settings):
    sent = []
    events = []
    n = ChatNotifier(ChatSettings.from_dict({"enabled": True, "min_interval_s": 0.5, **settings}),
                     publish_threadsafe=lambda ev, d: events.append((ev, d)))
    n.twitch_send = lambda msg: sent.append(msg) or {"message_id": "m", "is_sent": True}
    n.twitch_ready = lambda: ready
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
    assert n.sent[0].startswith("🎲 Оружие: W0") and n.sent[-1].startswith("🎲 Оружие: W4")
    assert len(n.sent) <= 3
    n.stop()


def test_notifier_needs_messages_on_and_a_twitch_sign_in():
    n = make_notifier(enabled=False)
    n.notify_weapon("AK-47", 1)
    n.notify_dpi(1600)
    time.sleep(0.2)
    assert n.sent == [] and not n.can_send()
    signed_out = make_notifier(ready=False)
    signed_out.notify_start("AWP")
    time.sleep(0.2)
    assert signed_out.sent == [] and not signed_out.can_send()
    n2 = make_notifier(on_dpi=False)
    n2.notify_dpi(1600)
    n2.notify_start("AWP")
    assert wait_for(lambda: n2.sent_count >= 1)
    assert all("DPI" not in m for m in n2.sent)
    n2.stop()


def test_send_now_reports_errors():
    n = make_notifier(ready=False)
    res = n.send_now("x")
    assert res["ok"] is False and "Войти через Twitch" in res["error"]
    n3 = make_notifier()
    n3.twitch_send = lambda msg: (_ for _ in ()).throw(RuntimeError("boom"))
    res = n3.send_now("x")
    assert res["ok"] is False and "boom" in res["error"]


def test_reject_messages_rate_limited_per_user():
    n = make_notifier(reject_cooldown_s=5)
    assert n.notify_reject("Spammer", "вне диапазона") is True
    assert n.notify_reject("spammer", "вне диапазона") is False     # same user within cooldown
    assert n.notify_reject("Other", "не число") is True
    assert wait_for(lambda: n.sent_count >= 1)
    n.settings.on_reject = False
    assert n.notify_reject("Third", "x") is False
    n.stop()


def test_deliver_keeps_last_response():
    n = make_notifier()
    assert n.send_now("hi")["ok"]
    assert n.sent == ["hi"] and n.last_response == {"message_id": "m", "is_sent": True}
    assert n.status()["ready"] is True and n.status()["sent_count"] == 1


def test_broken_templates_never_raise_and_are_refused_on_save():
    """0.38.0: {user.x} raised AttributeError inside the reward handler: the viewer got the weapon and the refund."""
    from app import chat as C
    assert C.render_template("{user.foo} {user[0]}", user="Alice") is None
    assert C.render_template("{user} {unknown}", user="Alice") == "Alice {unknown}"
    for bad in ("{user.foo}", "{user[x]}", "{", "}"):
        with pytest.raises(ValueError):
            C.ChatSettings.from_dict({"request_template": bad})
    # a settings.json from an older version: only that template falls back, the rest stays
    s = C.ChatSettings.from_dict({"request_template": "{", "enabled": True}, strict=False)
    assert s.request_template == C.ChatSettings.request_template and s.enabled
