import json
import time

import pytest

from app.modules.donations import aliases as A
from app.modules.donations import providers as P
from app.modules.donations.queue import ViewerQueue
from app.modules.donations.service import DEFAULT_RULES, DonationService, DonationSettings


# ----------------------------------------------------------------------------- aliases
@pytest.mark.parametrize("text,weapon", [
    ("закинь крокодила", "xm1014"), ("AWP пожалуйста", "awp"), ("калашом сыграй", "ak47"), ("хочу дигл", "deagle"),
    ("м4а1-с", "m4a1_silencer"), ("эмку с глушителем", "m4a1_silencer"), ("эмка", "m4a1"), ("p 90", "p90"),
    ("р250 давай", "p250"), ("ump-45", "ump45"), ("морковку", "mp5sd"), ("чешка", "cz75a"), ("aк47", "ak47"),
    ("скаутом", "ssg08"), ("зевс", "taser"), ("ножом", "knife"), ("five-seven", "fiveseven"), ("галю", "galilar"),
    ("awp и дигл", "awp"), ("Desert Eagle", "deagle"), ("Nova", "nova"), ("авапу", "awp"), ("петухом", "p90"), ("ревиком", "revolver"),
])
def test_alias_finds_weapon(text, weapon):
    assert A.AliasIndex().find(text)[0] == weapon


@pytest.mark.parametrize("text", ["Как дела", "хм, ну давай", "новая пушка", "текст", "магазин", "1600 dpi", "57 лайков", ""])
def test_alias_ignores_ordinary_words(text):
    assert A.AliasIndex().find(text) is None


def test_every_default_alias_points_to_a_catalog_weapon_and_resolves_to_it():
    ix = A.AliasIndex()
    for wid, names in A.DEFAULT_ALIASES.items():
        for name in names:
            assert wid in ix.match(name.rstrip("*"))[0], (wid, name)


def test_side_specific_alias_picks_by_side():
    ix = A.AliasIndex()
    assert ix.match("плётку дай")[0] == ["g3sg1", "scar20"]
    assert ix.find("плётку дай", "T")[0] == "g3sg1" and ix.find("плеткой", "CT")[0] == "scar20"
    assert ix.find("плетка")[0] == "g3sg1"                               # side unknown: the first one
    assert A.AliasIndex({"плетка": "scar20"}).match("плетка")[0] == ["scar20"]   # user mapping wins


def test_extra_aliases_and_errors():
    extra, errors = A.parse_extra("бабах = awp\nжелезка = m4a1-s\n# коммент\nкапец\nxx = нечто\nхм = калаш")
    assert extra == {"бабах": "awp", "железка": "m4a1_silencer", "хм": "ak47"}
    assert len(errors) == 2
    ix = A.AliasIndex(extra)
    assert ix.find("бабах!")[0] == "awp" and ix.find("хм")[0] == "ak47"


@pytest.mark.parametrize("text,n", [("1600", 1600), ("закинь 1 600", 1600), ("dpi 2.400 пж", 2400), ("п90 1600", 1600),
                                    ("р250 1600", 1600), ("1600dpi", 1600), ("без чисел", None)])
def test_find_number(text, n):
    assert A.find_number(text) == n


# ----------------------------------------------------------------------------- providers
def test_parse_da_and_dp():
    d = P.parse_da({"id": 7, "username": "Alice", "message": "awp", "amount": 5, "currency": "USD",
                    "amount_in_user_currency": 460.5, "created_at": "2026-11-01 10:00:00", "created_at_ts": 1793440000})
    assert (d.id, d.user, d.amount, d.currency, d.created_ts) == ("7", "Alice", 460.5, "USD", 1793440000.0)
    assert P.parse_da({"id": 8, "amount": "100", "created_at": "2026-11-01 10:00:00"}).created_ts is not None
    dp = P.parse_dp({"id": 11, "sum": "150.00", "status": "success", "type": "donation", "vars": {"name": "Bob", "comment": "1600"},
                     "created_at": {"date": "2026-11-01 13:00:00.000000", "timezone": "Europe/Moscow"}})
    assert (dp.id, dp.user, dp.amount, dp.message, dp.test) == ("11", "Bob", 150.0, "1600", False)
    assert dp.created_ts == P.parse_da({"id": 1, "created_at": "2026-11-01 10:00:00"}).created_ts   # 13:00 MSK = 10:00 UTC
    assert P.parse_dp({"id": 12, "sum": "1", "status": "user", "type": "donation"}) is None   # dashboard test alert
    assert P.parse_dp({"id": 13, "sum": "1", "status": "cancel", "type": "donation"}) is None
    assert P.parse_dp({"id": 14, "sum": "1", "status": "success", "type": "cashout"}) is None


def test_da_authorize_url_has_scopes_and_state():
    url = P.da_authorize_url("42", "http://127.0.0.1:17890/donations/da/callback", "st")
    assert url.startswith("https://www.donationalerts.com/oauth/authorize?")
    assert "client_id=42" in url and "response_type=code" in url and "state=st" in url
    assert "scope=oauth-user-show%20oauth-donation-index" in url and "redirect_uri=http%3A%2F%2F127.0.0.1" in url


# ----------------------------------------------------------------------------- queue
def test_queue_add_close_resolve_move_and_autocomplete(tmp_path):
    published = []
    q = ViewerQueue(str(tmp_path / "q.json"), publish=lambda e, d: published.append(e))
    a = q.add("awp", "awp плиз", "Alice", 300, "RUB")
    b = q.add(None, "что-то странное", "Bob", 300, "RUB")
    c = q.add("awp", "ещё awp", "Carl", 300, "RUB")
    assert (a["position"], b["position"], c["position"]) == (1, 2, 3) and a["name"] == "AWP" and b["name"] is None
    assert q.on_kill("ak47") is None
    closed = q.on_kill("awp")
    assert closed["id"] == a["id"] and closed["auto"] is True          # the oldest AWP request
    q.resolve(b["id"], "deagle")
    assert q.move(c["id"], -1) is True
    assert [e["user"] for e in q.snapshot()["pending"]] == ["Carl", "Bob"]
    q.dismiss(b["id"])
    snap = q.snapshot()
    assert [e["user"] for e in snap["pending"]] == ["Carl"] and [e["status"] for e in snap["history"]] == ["dismissed", "done"]
    again = ViewerQueue(str(tmp_path / "q.json"))
    assert [e["user"] for e in again.snapshot()["pending"]] == ["Carl"]
    assert again.add("knife", "", "Dan")["id"] == c["id"] + 1
    q.clear_history()
    assert q.snapshot()["history"] == [] and published.count("queue") >= 6


# ----------------------------------------------------------------------------- service
class Harness:
    def __init__(self, tmp_path, **settings):
        self.calls = []
        self.chat = []
        self.svc = DonationService(DonationSettings.from_dict({"enabled": True, **settings}), str(tmp_path / "don.json"),
                                   publish=lambda e, d: None, call_threadsafe=lambda fn, *a: fn(*a))
        self.svc.add_wheel = lambda v, u: (self.calls.append(("wheel", v, u)) or
                                           ({"ok": False, "error": "вне диапазона 400–6400"} if v > 6400 else {"ok": True, "value": v}))
        self.svc.queue_weapon = lambda w, text, user, amount, cur, by_side=None, source="donation": (
            self.calls.append(("weapon", w, user, by_side)) or {"name": {"awp": "AWP"}.get(w), "position": 1})
        self.svc.chat_result = lambda u, t: self.chat.append(("ok", u, t))
        self.svc.chat_reject = lambda u, t: self.chat.append(("no", u, t))

    def donate(self, amount, message, user="Alice", provider="test", id_="1", ts=None):
        return self.svc.handle(P.Donation(provider=provider, id=id_, user=user, amount=amount, currency="RUB",
                                          message=message, created_ts=ts))


def test_default_rules_route_by_amount_and_content(tmp_path):
    h = Harness(tmp_path)
    assert h.donate(300, "awp плиз")["text"] == "AWP в очереди, №1"
    assert h.donate(600, "1600")["text"] == "1600 DPI на колесе"         # no «next DPI» any more (0.21.1)
    assert h.donate(100, "1600")["text"] == "1600 DPI на колесе"
    assert h.donate(600, "awp 1600")["action"] == "weapon"                 # the weapon rule is first
    low = h.donate(100, "awp")                                            # weapon recognised, too cheap
    assert low["ok"] is False and low["text"] == "оружие в зрительскую очередь — от 300 ₽"
    assert h.donate(20, "спасибо за стрим")["text"] == "без действия"
    bad = h.donate(700, "99999")
    assert bad["ok"] is False and "вне диапазона" in bad["text"]
    assert [c[0] for c in h.calls] == ["weapon", "wheel", "wheel", "weapon", "wheel"]
    assert [r["action"] for r in h.svc.settings.rules] == ["weapon", "wheel_value"]
    assert ("no", "Alice", "оружие в зрительскую очередь — от 300 ₽") in h.chat
    assert not any("спасибо" in t for _, _, t in h.chat)                  # no action, no hint: silence
    assert len(h.svc.state["log"]) == 7


def test_keyword_rule_queues_unrecognised_weapon_and_max_bound(tmp_path):
    rules = [{"enabled": True, "min": 100, "max": 0, "keyword": "!ствол", "action": "weapon"},
             {"enabled": True, "min": 50, "max": 199, "keyword": "", "action": "wheel_value"},
             {"enabled": False, "min": 0, "max": 0, "keyword": "", "action": "next_dpi"}]
    h = Harness(tmp_path, rules=rules)
    assert len(h.svc.settings.rules) == 2                                  # an old «next DPI» rule is dropped
    assert h.donate(150, "!ствол базука")["text"] == "оружие (уточнит стример) в очереди, №1"
    assert h.calls[-1] == ("weapon", None, "Alice", None)
    assert h.donate(150, "awp")["text"] == "без действия"                  # no keyword: weapon rule skipped
    assert h.donate(250, "1600")["text"] == "без действия"                 # above max of the wheel rule
    assert h.donate(150, "1600")["action"] == "wheel_value"


def test_ambiguous_weapon_uses_the_current_side(tmp_path):
    h = Harness(tmp_path)
    h.svc.current_side = lambda: "CT"
    h.donate(300, "плетку плиз")
    assert h.calls[-1] == ("weapon", "scar20", "Alice", "CT")
    h.svc.current_side = lambda: "T"
    h.donate(300, "плетку")
    assert h.calls[-1] == ("weapon", "g3sg1", "Alice", "T")
    h.donate(300, "awp")
    assert h.calls[-1] == ("weapon", "awp", "Alice", None)


def test_rules_validation():
    with pytest.raises(ValueError):
        DonationSettings.from_dict({"rules": [{"action": "reroll"}]})
    s = DonationSettings.from_dict({"rules": [{"action": "weapon", "min": "-5", "keyword": "  !w  "}]})
    assert s.rules == [{"enabled": True, "min": 0.0, "max": 0.0, "keyword": "!w", "action": "weapon"}]
    assert DonationSettings().rules == DEFAULT_RULES and DonationSettings.from_dict({"dp_poll_s": 1}).dp_poll_s == 20.0


def test_ingest_baseline_dedup_and_replay_window(tmp_path):
    h = Harness(tmp_path)
    now = time.time()
    mk = lambda i, msg="1600", age=0: P.Donation("dp", str(i), "Bob", 100, "RUB", msg, created_ts=now - age)
    assert h.svc.ingest("dp", [mk(5), mk(4)], now=now) == []              # first poll: remember only
    assert h.svc.state["dp"]["last_id"] == 5
    got = h.svc.ingest("dp", [mk(7), mk(6), mk(5)], now=now)
    assert [d.id for d in got] == ["6", "7"] and h.svc.state["dp"]["last_id"] == 7
    assert h.svc.ingest("dp", [mk(7), mk(6)], now=now) == []              # already seen
    # restart: state on disk, a donation that came while the app was off is replayed, an old one is not
    h2 = Harness(tmp_path)
    got = h2.svc.ingest("dp", [mk(9, age=30), mk(8, age=3600)], now=now)
    assert [d.id for d in got] == ["9"] and h2.svc.state["dp"]["last_id"] == 9
    assert [c for c in h2.calls if c[0] == "wheel"] == [("wheel", 1600, "Bob")]


def test_secrets_are_kept_hidden_and_preserved(tmp_path):
    s = DonationSettings.from_dict({"da_client_secret": "S", "dp_api_key": "K"})
    pub = s.public_dict()
    assert "da_client_secret" not in pub and pub["da_client_secret_set"] and pub["dp_api_key_set"]
    h = Harness(tmp_path, da_client_secret="S", dp_api_key="K", da_client_id="1")
    h.svc.update_settings({"rules": DEFAULT_RULES, "da_client_secret_set": True})   # panel echoes public keys
    assert h.svc.settings.da_client_secret == "S" and h.svc.settings.dp_api_key == "K"
    h.svc.update_settings({"dp_api_key": ""})
    assert h.svc.settings.dp_api_key == ""
    h.svc.state["da"] = {"access_token": "t", "last_id": 3}
    h.svc.update_settings({"da_client_id": "2"})                          # another app: the old token is useless
    assert h.svc.state["da"] == {}


def test_da_oauth_flow_and_refresh(tmp_path, monkeypatch):
    h = Harness(tmp_path, da_client_id="1", da_client_secret="S")
    url = h.svc.da_authorize_url("http://127.0.0.1:17890/donations/da/callback")
    state = url.split("state=")[1].split("&")[0]
    monkeypatch.setattr(P, "da_token", lambda *a, **k: {"access_token": "A1", "refresh_token": "R1", "expires_in": 3600}
                        if k.get("code") else {"access_token": "A2", "expires_in": 3600})
    monkeypatch.setattr(P, "da_user", lambda tok: {"id": 9, "name": "streamer"})
    with pytest.raises(ValueError):
        h.svc.da_finish("c", "wrong", "x")
    h.svc.da_authorize_url("x")
    state = h.svc._oauth_state[0]
    assert h.svc.da_finish("c", state, "x")["name"] == "streamer"
    assert h.svc.status()["da"]["connected"] and h.svc.status()["da"]["user"] == "streamer"
    calls = []

    def donations(tok, page=1):
        calls.append(tok)
        if tok == "A1":
            raise P.ProviderError("HTTP 401", status=401)
        return [P.Donation("da", "50", "Eve", 100, "RUB", "1600")]
    monkeypatch.setattr(P, "da_donations", donations)
    h.svc._poll_da()                                                       # 401 -> refresh -> retry
    assert calls == ["A1", "A2"] and h.svc.state["da"]["access_token"] == "A2"
    assert h.svc.state["da"]["refresh_token"] == "R1" and h.svc.state["da"]["last_id"] == 50
    assert json.load(open(tmp_path / "don.json"))["da"]["access_token"] == "A2"


# ----------------------------------------------------------------------------- app wiring
def test_api_test_donation_reaches_queue_and_wheel(client):
    s = client.post("/api/settings", json={"donations": {"enabled": True, "dp_api_key": "secret-key"}})
    assert s.status_code == 200 and "secret-key" not in s.text and s.json()["donations"]["dp_api_key_set"] is True
    assert "secret-key" not in client.get("/api/state").text and "secret-key" not in client.get("/api/donations").text
    r = client.post("/api/donations/test", json={"user": "Alice", "amount": 300, "message": "крокодила плиз"}).json()
    assert r["ok"] and r["action"] == "weapon"
    q = client.get("/api/queue").json()["pending"]
    assert q[0]["weapon"] == "xm1014" and q[0]["user"] == "Alice"
    assert client.post("/api/donations/test", json={"user": "Bob", "amount": 100, "message": "1600"}).json()["action"] == "wheel_value"
    dpi = client.get("/api/state").json()["dpi"]
    assert any(e["value"] == 1600 and e["user"] == "Bob" for e in dpi["wheel"]["viewer"])
    assert client.post("/api/donations/test", json={"user": "Carl", "amount": 600, "message": "2400"}).json()["action"] == "wheel_value"
    assert "forced" not in client.get("/api/state").json()["dpi"]
    assert "forced_by" not in client.post("/api/dpi/spin", json={}).json()
    assert client.post("/api/dpi/forced/remove", json={"index": 0}).status_code in (404, 405)
    assert client.post("/api/donations/aliases/check", json={"text": "морковку 1600"}).json()["weapon"] == "mp5sd"
    lst = client.get("/api/donations/aliases").json()["weapons"]
    assert any(w["id"] == "xm1014" and "крокодил*" in w["aliases"] for w in lst)


def test_api_queue_actions_and_kill_autocomplete(client):
    from tests.test_gsi import payload
    a = client.post("/api/queue/add", json={"weapon": "ak47", "user": "Zed"}).json()
    b = client.post("/api/queue/add", json={"weapon": None, "text": "что-то", "user": "Yan"}).json()
    assert client.post("/api/queue/resolve", json={"id": b["id"], "weapon": "deagle"}).json()["ok"]
    token = client.get("/api/state").json()["gsi_token"]
    for p in (payload(rk=0, active="weapon_ak47"), payload(rk=1, active="weapon_ak47")):
        p["auth"] = {"token": token}
        client.post("/gsi", json=p)
    pend = client.get("/api/queue").json()["pending"]
    assert [e["id"] for e in pend] == [b["id"]]                            # the AK request closed by the kill
    assert client.post("/api/queue/done", json={"id": b["id"]}).json()["ok"]
    assert client.get("/api/queue").json()["pending"] == []
    assert client.post("/api/queue/resolve", json={"id": a["id"], "weapon": "nope"}).status_code == 400


def test_da_callback_page_reports_bad_state(client):
    r = client.get("/donations/da/callback?code=x&state=y")
    assert r.status_code == 200 and "устарела" in r.text and "no-store" in r.headers.get("cache-control", "")
    assert client.post("/api/donations/da/connect", json={}).status_code == 400   # no app id/secret yet


# ----------------------------------------------------------------------------- queue widget & chat command
def test_queue_describe_for_viewer():
    import tempfile, os
    q = ViewerQueue(os.path.join(tempfile.mkdtemp(), "q.json"))
    assert q.describe_for("Alice") == "очередь пуста"
    q.add("awp", "", "Bob"); q.add(None, "?", "alice"); q.add("ak47", "", "Carl"); q.add("nova", "", "Dan")
    assert q.describe_for("Alice", "alice_login") == "твои заявки: оружие уточняется — №2"
    assert q.describe_for("Eve") == "у тебя нет заявок. В очереди 4: AWP, оружие уточняется, AK-47…"


def test_queue_chat_command_answers_and_starts_reader(client, monkeypatch):
    st = client.app.state.app
    replies, started = [], []
    monkeypatch.setattr(st.chat, "notify_queue_reply", lambda user, result: replies.append((user, result)) or True)
    monkeypatch.setattr(st.twitch, "start", lambda channel: started.append(channel))
    client.post("/api/queue/add", json={"weapon": "awp", "user": "Viewer_One"})
    client.post("/api/twitch/chat", json={"user": "Viewer_One", "text": "!очередь"})
    client.post("/api/twitch/chat", json={"user": "Other", "text": "!ОЧЕРЕДЬ пж"})
    client.post("/api/twitch/chat", json={"user": "Other", "text": "!очередьх"})
    assert replies == [("Viewer_One", "твои заявки: AWP — №1"), ("Other", "у тебя нет заявок. В очереди 1: AWP")]
    # the reader runs for the command only when the SE bot can answer and a channel is set
    client.post("/api/settings", json={"chat": {"channel_login": "mychannel"}})
    assert started == []
    client.post("/api/settings", json={"chat": {"enabled": True, "se_jwt": "x.e30.y"}})
    assert started == ["mychannel"]
    client.post("/api/settings", json={"donations": {"queue_command": ""}})
    client.post("/api/twitch/chat", json={"user": "Viewer_One", "text": "!очередь"})
    assert len(replies) == 2


def test_queue_reply_is_rate_limited_per_viewer():
    from app.chat import ChatNotifier, ChatSettings
    sent = []
    n = ChatNotifier(ChatSettings(enabled=True, se_jwt="x"), lambda *a: None)
    n._enqueue = lambda kind, msg: sent.append(msg)
    assert n.notify_queue_reply("Alice", "очередь пуста") is True
    assert n.notify_queue_reply("alice", "очередь пуста") is False
    assert n.notify_queue_reply("Bob", "очередь пуста") is True
    assert sent == ["📋 @Alice: очередь пуста", "📋 @Bob: очередь пуста"]
    assert ChatNotifier(ChatSettings(), lambda *a: None).notify_queue_reply("A", "x") is False   # bot off


def test_queue_widget_page_and_settings(client):
    r = client.get("/overlay/requests")
    assert r.status_code == 200 and "Очередь зрителей" in r.text and "no-store" in r.headers["cache-control"]
    body = client.post("/api/settings", json={"overlay": {"queue_max": 99, "queue_amount": "1", "queue_title": False}}).json()
    assert (body["overlay"]["queue_max"], body["overlay"]["queue_amount"], body["overlay"]["queue_title"]) == (10, True, False)
    assert client.get("/api/state").json()["urls"]["overlay_requests"].endswith("/overlay/requests")


# ----------------------------------------------------------------------------- weapon for channel points
def _reward_setup(client, monkeypatch):
    st = client.app.state.app
    said, rejected = [], []
    monkeypatch.setattr(st.chat, "notify_request", lambda user, weapon, pos, result="": said.append((user, weapon, pos)))
    monkeypatch.setattr(st.chat, "notify_reject", lambda user, reason: rejected.append((user, reason)) or True)
    return st, said, rejected


def test_weapon_reward_learn_and_queue(client, monkeypatch):
    st, said, rejected = _reward_setup(client, monkeypatch)
    started = []
    monkeypatch.setattr(st.twitch, "start", lambda channel: started.append(channel))
    client.post("/api/settings", json={"chat": {"channel_login": "mychannel"}})
    assert started == []                                           # nothing needs the reader yet
    assert client.post("/api/queue/reward/learn", json={"on": True}).json() == {"learn": True}
    assert started == ["mychannel"]                                # learning reads the chat even while off
    assert client.get("/api/donations").json()["learn_reward"] is True
    client.post("/api/twitch/chat", json={"user": "Viewer", "text": "дай авапу", "reward_id": "rw-weapon"})
    s = client.get("/api/settings").json()["donations"]
    assert s["reward_id"] == "rw-weapon" and client.get("/api/donations").json()["learn_reward"] is False
    assert st.queue.pending() == [] and said == []                 # learnt, but the reward itself is still off
    client.post("/api/settings", json={"donations": {"reward_enabled": True}})
    client.post("/api/twitch/chat", json={"user": "Viewer", "text": "дай авапу", "reward_id": "rw-weapon"})
    q = client.get("/api/queue").json()["pending"]
    assert [(e["weapon"], e["user"], e["source"], e["amount"]) for e in q] == [("awp", "Viewer", "reward", None)]
    assert said == [("Viewer", "AWP", 1)] and rejected == []
    # persisted: a restart keeps the reward id
    from app import config
    assert json.load(open(config.settings_path(), encoding="utf-8"))["donations"]["reward_id"] == "rw-weapon"


def test_weapon_reward_rejects_unknown_weapon(client, monkeypatch):
    st, said, rejected = _reward_setup(client, monkeypatch)
    client.post("/api/settings", json={"donations": {"reward_enabled": True, "reward_id": "rw-weapon"}})
    client.post("/api/twitch/chat", json={"user": "Viewer", "text": "лучшая пушка", "reward_id": "rw-weapon"})
    assert st.queue.pending() == [] and said == []
    assert rejected and rejected[0][0] == "Viewer" and "не понял" in rejected[0][1]


def test_weapon_reward_and_dpi_reward_do_not_take_each_other(client, monkeypatch):
    st, said, rejected = _reward_setup(client, monkeypatch)
    added = []
    monkeypatch.setattr(st.dpi, "add_viewer_value", lambda v, u, source="reward": added.append((v, u)) or {"ok": True})
    # DPI reward without an id takes any reward, but not the weapon one
    client.post("/api/settings", json={"dpi": {"viewer_enabled": True},
                                       "donations": {"reward_enabled": True, "reward_id": "rw-weapon"}})
    client.post("/api/twitch/chat", json={"user": "A", "text": "калаш", "reward_id": "rw-weapon"})
    client.post("/api/twitch/chat", json={"user": "B", "text": "2400", "reward_id": "rw-dpi"})
    assert [e["weapon"] for e in st.queue.pending()] == ["ak47"] and added == [("2400", "B")]
    # the weapon reward switched off: its redemptions are ignored, never read as a DPI value
    client.post("/api/settings", json={"donations": {"reward_enabled": False}})
    client.post("/api/twitch/chat", json={"user": "C", "text": "1600", "reward_id": "rw-weapon"})
    assert len(st.queue.pending()) == 1 and added == [("2400", "B")]
    # a plain chat line is never a weapon request
    client.post("/api/settings", json={"donations": {"reward_enabled": True}})
    client.post("/api/twitch/chat", json={"user": "D", "text": "awp"})
    assert len(st.queue.pending()) == 1


def test_weapon_reward_side_alias_and_clear(client, monkeypatch):
    st, said, rejected = _reward_setup(client, monkeypatch)
    started, stopped = [], []
    monkeypatch.setattr(st.twitch, "start", lambda channel: started.append(channel))
    monkeypatch.setattr(st.twitch, "stop", lambda: stopped.append(1))
    client.post("/api/settings", json={"chat": {"channel_login": "mychannel"},
                                       "donations": {"reward_enabled": True, "reward_id": "rw-weapon"}})
    assert started[-1] == "mychannel"
    st.roulette.side = "CT"
    client.post("/api/twitch/chat", json={"user": "A", "text": "плётку", "reward_id": "rw-weapon"})
    e = st.queue.pending()[0]
    assert (e["weapon"], e["by_side"]) == ("scar20", "CT")
    n = len(stopped)
    client.post("/api/queue/reward/learn", json={"on": False, "clear": True})
    assert client.get("/api/settings").json()["donations"]["reward_id"] == ""
    client.post("/api/settings", json={"donations": {"reward_enabled": False}})
    assert len(stopped) > n                                        # nothing needs the chat reader any more


def test_reward_request_chat_message():
    from app.chat import ChatNotifier, ChatSettings
    sent = []
    n = ChatNotifier(ChatSettings(enabled=True, se_jwt="x"), lambda *a: None)
    n._enqueue = lambda kind, msg: sent.append((kind, msg))
    n.notify_request("Alice", "AWP", 2)
    n.notify_request("Alice", "AK-47", 3)
    assert [m for _, m in sent] == ["🎯 Alice заказал AWP, №2 в очереди", "🎯 Alice заказал AK-47, №3 в очереди"]
    assert sent[0][0] != sent[1][0]                                # never merged: points were spent
    assert ChatSettings.from_dict({"on_request": "0"}).on_request is False


# ----------------------------------------------------------------------------- queue order by loadout
def _q(avail):
    import tempfile, os
    published = []
    q = ViewerQueue(os.path.join(tempfile.mkdtemp(), "q.json"), publish=lambda ev, d: published.append(d))
    q.available = lambda: avail["ids"]
    return q, published


def test_queue_puts_weapons_outside_loadout_last():
    avail = {"ids": {"awp", "ak47", "deagle", "knife"}}
    q, _ = _q(avail)
    assert q.add("awp", "", "A")["position"] == 1
    out = q.add("negev", "", "B")
    assert (out["position"], out["in_loadout"]) == (2, False)
    q.add(None, "базука", "C")                                      # unrecognised: stays where it came
    assert q.add("ak47", "", "D")["position"] == 3                   # jumps over the Negev
    q.add("nova", "", "E")
    snap = q.snapshot()["pending"]
    assert [(e["weapon"], e["in_loadout"]) for e in snap] == [
        ("awp", True), (None, True), ("ak47", True), ("negev", False), ("nova", False)]
    assert q.describe_for("B") == "твои заявки: Negev — №4"
    # ↑ cannot lift a request outside the loadout over the playable ones; inside its group it moves
    neg, nova = snap[3]["id"], snap[4]["id"]
    assert q.move(neg, -1) is False and q.move(snap[2]["id"], 1) is False
    assert q.move(nova, -1) is True
    assert [e["weapon"] for e in q.snapshot()["pending"]][3:] == ["nova", "negev"]
    # a kill with the weapon still closes it
    assert q.on_kill("negev")["user"] == "B"


def test_queue_without_loadout_keeps_arrival_order_and_refresh_republishes():
    avail = {"ids": None}
    q, published = _q(avail)
    q.add("negev", "", "A"); q.add("awp", "", "B")
    assert [e["weapon"] for e in q.snapshot()["pending"]] == ["negev", "awp"]
    assert all(e["in_loadout"] for e in q.snapshot()["pending"])
    n = len(published)
    q.refresh()                                                     # still no loadout: nothing to say
    assert len(published) == n
    avail["ids"] = {"awp"}
    q.refresh()
    assert len(published) == n + 1 and [e["weapon"] for e in published[-1]["pending"]] == ["awp", "negev"]
    q.refresh()                                                     # same loadout: no repeat
    assert len(published) == n + 1


def test_queue_order_follows_the_app_loadout(client):
    st = client.app.state.app
    assert st.loadout_weapons() is None                             # no loadout yet: arrival order
    client.post("/api/settings", json={"roulette": {"include_zeus": True}})   # Zeus may be requested
    lo = {"T": {"starting": "glock", "pistol": ["p250", "elite", "tec9", "cz75a"],
                "mid": ["mac10", "mp7", "mp5sd", "ump45", "p90"], "rifle": ["galilar", "ak47", "sg556", "ssg08", "awp"]},
          "CT": {"starting": "usp_silencer", "pistol": ["p250", "elite", "fiveseven", "cz75a"],
                 "mid": ["mp9", "mp7", "mp5sd", "ump45", "p90"], "rifle": ["famas", "m4a1", "m4a1_silencer", "aug", "ssg08"]}}
    client.post("/api/queue/add", json={"weapon": "negev", "user": "A"})
    client.post("/api/queue/add", json={"weapon": "hkp2000", "user": "B"})   # CT starts with the USP-S
    client.post("/api/queue/add", json={"weapon": "m4a1", "user": "C"})
    client.post("/api/queue/add", json={"weapon": "taser", "user": "D"})
    order = lambda: [e["weapon"] for e in client.get("/api/queue").json()["pending"]]
    assert order() == ["negev", "hkp2000", "m4a1", "taser"]
    st.roulette.set_loadout(lo)
    assert {"taser", "glock", "usp_silencer"} <= st.loadout_weapons() and "knife" not in st.loadout_weapons()
    assert order() == ["m4a1", "taser", "negev", "hkp2000"]
    # the streamer puts the requested weapon into the loadout: the request moves up at once
    st.roulette.set_loadout({**lo, "CT": {**lo["CT"], "mid": ["mp9", "mp7", "mp5sd", "ump45", "negev"]}})
    assert order() == ["negev", "m4a1", "taser", "hkp2000"]


# ----------------------------------------------------------------------------- knife and Zeus follow the race
def test_knife_and_zeus_only_when_in_the_race(client, monkeypatch):
    st = client.app.state.app
    rejected = []
    monkeypatch.setattr(st.chat, "notify_reject", lambda user, reason: rejected.append((user, reason)) or True)
    r = client.post("/api/queue/add", json={"weapon": "knife", "user": "A"})
    assert r.status_code == 400 and "Нож сейчас не принимается" in r.text
    # channel points and donations are refused with the reason in chat
    client.post("/api/settings", json={"donations": {"reward_enabled": True, "reward_id": "rw", "enabled": True}})
    client.post("/api/twitch/chat", json={"user": "B", "text": "зевс", "reward_id": "rw"})
    assert rejected == [("B", "Zeus x27 сейчас не принимается")]
    res = client.post("/api/donations/test", json={"user": "C", "amount": 500, "message": "ножом!"}).json()
    assert res["ok"] is False and res["text"] == "Нож сейчас не принимается"
    assert st.queue.pending() == []
    # an unrecognised request cannot be resolved to a knife either
    e = client.post("/api/queue/add", json={"weapon": None, "user": "D", "text": "?"}).json()
    assert client.post("/api/queue/resolve", json={"id": e["id"], "weapon": "knife"}).status_code == 400
    # switched on in the race settings: accepted
    client.post("/api/settings", json={"roulette": {"include_knife": True, "include_zeus": True}})
    assert client.post("/api/queue/add", json={"weapon": "knife", "user": "A"}).status_code == 200
    client.post("/api/twitch/chat", json={"user": "B", "text": "зевс", "reward_id": "rw"})
    assert [x["weapon"] for x in st.queue.pending()] == [None, "knife", "taser"]


def test_viewer_dpi_chat_follows_the_chat_switches_only(client, monkeypatch):
    st = client.app.state.app
    said, rejected = [], []
    monkeypatch.setattr(st.chat, "notify_viewer", lambda user, dpi: said.append((user, dpi)))
    monkeypatch.setattr(st.chat, "notify_reject", lambda user, reason: rejected.append((user, reason)) or True)
    # an old settings file with the removed DPI switch still loads (the key is ignored)
    assert client.post("/api/settings", json={"dpi": {"viewer_enabled": True, "viewer_announce": False}}).status_code == 200
    assert "viewer_announce" not in client.get("/api/settings").json()["dpi"]
    client.post("/api/twitch/chat", json={"user": "A", "text": "1600", "reward_id": "any"})
    client.post("/api/twitch/chat", json={"user": "B", "text": "много", "reward_id": "any"})
    assert said == [("A", 1600)] and rejected and rejected[0][0] == "B"
    assert "queue_autocomplete" not in client.get("/api/settings").json()["donations"]
