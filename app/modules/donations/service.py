"""Donation service: settings, rules, polling thread, dispatch.

Flow: the polling thread asks DonationAlerts / DonatePay for their latest donations, keeps only the ones
newer than the remembered ``last_id`` and hands each of them to the event-loop thread (``handle``), where
the first matching rule turns it into an action. Actions only create effects that wait for their moment
(a value on the wheel, the next spin's result, a request in the viewer queue) — never «reroll now»: a CS2
round is over before the donation arrives (user's decision 2026-10-31).

Rules (top to bottom, the first one that fits wins):
``{enabled, min, max (0 = no limit), keyword (optional, substring), action}``. A rule fits when the
amount is in range, the keyword (if any) is in the message and the message carries what the action
needs: a number for ``wheel_value`` / ``next_dpi``, a recognised weapon for ``weapon`` (with a keyword a
request is queued even if the weapon is not recognised — the streamer picks it by hand).

Replays: the first poll of an account only remembers the newest id. After a restart, donations that came
in while the app was off are processed if they are at most ``FRESH_S`` old.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from ... import config
from . import aliases as A
from . import providers as P

log = logging.getLogger("hardmode.donations")

ACTIONS = {
    "weapon": "оружие в зрительскую очередь",
    "next_dpi": "следующий DPI",
    "wheel_value": "число на колесо DPI",
}
DEFAULT_RULES = [
    {"enabled": True, "min": 300.0, "max": 0.0, "keyword": "", "action": "weapon"},
    {"enabled": True, "min": 500.0, "max": 0.0, "keyword": "", "action": "next_dpi"},
    {"enabled": True, "min": 50.0, "max": 0.0, "keyword": "", "action": "wheel_value"},
]
MAX_RULES = 20
MAX_LOG = 50
FRESH_S = 600.0          # after a restart: donations older than this are not replayed
SECRETS = ("da_client_secret", "dp_api_key")


def _rules(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("rules: нужен список")
    out = []
    for i, r in enumerate(value[:MAX_RULES], start=1):
        if not isinstance(r, dict):
            raise ValueError(f"правило {i}: нужен объект")
        action = r.get("action")
        if action not in ACTIONS:
            raise ValueError(f"правило {i}: неизвестное действие {action!r}")
        lo = max(0.0, min(1e7, config.to_float(r.get("min", 0), f"правило {i}: от")))
        hi = max(0.0, min(1e7, config.to_float(r.get("max", 0), f"правило {i}: до")))
        out.append({"enabled": config.to_bool(r.get("enabled", True)), "min": lo, "max": hi,
                    "keyword": str(r.get("keyword") or "").strip()[:40], "action": action})
    return out


@dataclass
class DonationSettings:
    enabled: bool = False
    da_enabled: bool = True
    da_client_id: str = ""
    da_client_secret: str = ""          # secret
    dp_enabled: bool = True
    dp_api_key: str = ""                # secret
    dp_include_test: bool = False       # DonatePay «тестовое оповещение» (status=user) counts as a donation
    rules: list = field(default_factory=lambda: [dict(r) for r in DEFAULT_RULES])
    aliases_extra: str = ""             # user additions, «алиас = оружие» per line
    queue_autocomplete: bool = True     # a kill with the requested weapon closes the request
    da_poll_s: float = 5.0
    dp_poll_s: float = 20.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "DonationSettings":
        s = cls()
        config.assign_fields(s, d, "donations")
        for b in ("enabled", "da_enabled", "dp_enabled", "dp_include_test", "queue_autocomplete"):
            setattr(s, b, config.to_bool(getattr(s, b)))
        for k in ("da_client_id", "da_client_secret", "dp_api_key"):
            v = getattr(s, k)
            setattr(s, k, v.strip()[:200] if isinstance(v, str) else "")
        s.aliases_extra = s.aliases_extra[:5000] if isinstance(s.aliases_extra, str) else ""
        s.rules = _rules(s.rules)
        s.da_poll_s = max(2.0, min(120.0, config.to_float(s.da_poll_s, "da_poll_s")))
        s.dp_poll_s = max(20.0, min(300.0, config.to_float(s.dp_poll_s, "dp_poll_s")))
        return s

    def to_dict(self) -> dict:
        return asdict(self)

    def public_dict(self) -> dict:
        d = asdict(self)
        for k in SECRETS:
            d[k + "_set"] = bool(d.pop(k))
        return d


def currency_label(cur: str) -> str:
    return {"": "₽", "RUB": "₽", "USD": "$", "EUR": "€"}.get((cur or "").upper(), cur)


def fmt_amount(amount: float, cur: str) -> str:
    return f"{amount:g} {currency_label(cur)}"


class DonationService:
    def __init__(self, settings: DonationSettings, state_path: str,
                 publish: Callable[[str, dict], None], call_threadsafe: Callable[..., None]) -> None:
        self.settings = settings
        self.state_path = state_path
        self._publish = publish
        self._call = call_threadsafe
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._oauth_state: tuple[str, float] | None = None
        self.state: dict = {"da": {}, "dp": {}, "log": []}
        self.status_: dict = {"da": {"error": None, "last_poll": None}, "dp": {"error": None, "last_poll": None}}
        # actions, wired by app.main (all called on the event-loop thread)
        self.add_wheel: Callable[[int, str], dict] = lambda v, u: {"ok": False, "error": "колесо недоступно"}
        self.force_next: Callable[[int, str], dict] = lambda v, u: {"ok": False, "error": "колесо недоступно"}
        self.queue_weapon: Callable[..., dict] = lambda *a, **k: {}
        self.chat_result: Callable[[str, str], None] = lambda user, text: None
        self.chat_reject: Callable[[str, str], None] = lambda user, reason: None
        self.aliases = A.AliasIndex()
        self.alias_errors: list[str] = []
        self._load()
        self._rebuild_aliases()

    # ------------------------------------------------------------------ settings & state
    def update_settings(self, patch: dict) -> None:
        patch = dict(patch or {})
        merged = {**self.settings.to_dict(), **patch}
        for k in SECRETS:                    # an absent or "_set" key keeps the stored secret
            if k not in patch:
                merged[k] = getattr(self.settings, k)
        new = DonationSettings.from_dict(merged)
        reset_da = new.da_client_id != self.settings.da_client_id
        self.settings = new
        if reset_da:
            self.disconnect_da(publish=False)
        self._rebuild_aliases()
        self._changed()

    def _rebuild_aliases(self) -> None:
        extra, errors = A.parse_extra(self.settings.aliases_extra)
        self.aliases = A.AliasIndex(extra)
        self.alias_errors = errors

    def _load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(d, dict):
            self.state["da"] = d.get("da") if isinstance(d.get("da"), dict) else {}
            self.state["dp"] = d.get("dp") if isinstance(d.get("dp"), dict) else {}
            self.state["log"] = [e for e in d.get("log") or [] if isinstance(e, dict)][-MAX_LOG:]

    def _save(self) -> None:
        with self._lock:
            try:
                config.write_json_atomic(self.state_path, self.state)
            except OSError:
                log.warning("cannot save donations state", exc_info=True)

    def status(self) -> dict:
        with self._lock:
            da, dp = self.state["da"], self.state["dp"]
            return {
                "settings": self.settings.public_dict(),
                "da": {**self.status_["da"], "connected": bool(da.get("access_token")),
                       "user": (da.get("user") or {}).get("name"), "expires_at": da.get("expires_at")},
                "dp": {**self.status_["dp"], "verified": bool(dp.get("user")), "user": (dp.get("user") or {}).get("name")},
                "log": self.state["log"][-20:][::-1],
                "alias_errors": self.alias_errors,
                "actions": ACTIONS,
            }

    def _changed(self) -> None:
        self._save()
        self._publish("donations", self.status())

    def _changed_threadsafe(self) -> None:
        self._save()
        self._call(lambda: self._publish("donations", self.status()))

    # ------------------------------------------------------------------ DonationAlerts OAuth
    def da_authorize_url(self, redirect_uri: str) -> str:
        s = self.settings
        if not (s.da_client_id and s.da_client_secret):
            raise ValueError("сначала укажите ID и секрет приложения DonationAlerts")
        token = secrets.token_urlsafe(16)
        self._oauth_state = (token, time.time())
        return P.da_authorize_url(s.da_client_id, redirect_uri, token)

    def da_finish(self, code: str, state: str, redirect_uri: str) -> dict:
        """Blocking: exchange the code (run in a worker thread). Returns the DA user."""
        expected = self._oauth_state
        if not expected or not secrets.compare_digest(expected[0], str(state or "")) or time.time() - expected[1] > 900:
            raise ValueError("ссылка авторизации устарела: нажмите «Подключить» ещё раз")
        self._oauth_state = None
        s = self.settings
        tok = P.da_token(s.da_client_id, s.da_client_secret, code=code, redirect_uri=redirect_uri)
        user = P.da_user(tok["access_token"])
        with self._lock:
            self.state["da"] = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                                "expires_at": time.time() + float(tok.get("expires_in") or 0) if tok.get("expires_in") else None,
                                "user": {"id": user.get("id"), "name": user.get("name") or user.get("code")},
                                "last_id": None}
            self.status_["da"]["error"] = None
        self._changed_threadsafe()
        return user

    def disconnect_da(self, publish: bool = True) -> None:
        with self._lock:
            self.state["da"] = {}
            self.status_["da"] = {"error": None, "last_poll": None}
        if publish:
            self._changed()

    def _da_access_token(self) -> str:
        """Current DA token, refreshed when it is about to expire (polling thread)."""
        with self._lock:
            da = dict(self.state["da"])
        if not da.get("access_token"):
            raise P.ProviderError("DonationAlerts не подключён")
        exp = da.get("expires_at")
        if exp and time.time() > float(exp) - 120 and da.get("refresh_token"):
            return self._da_refresh(da["refresh_token"])
        return da["access_token"]

    def _da_refresh(self, refresh_token: str) -> str:
        s = self.settings
        tok = P.da_token(s.da_client_id, s.da_client_secret, refresh_token=refresh_token)
        with self._lock:
            self.state["da"].update(access_token=tok["access_token"],
                                    refresh_token=tok.get("refresh_token") or refresh_token,
                                    expires_at=time.time() + float(tok.get("expires_in") or 0) if tok.get("expires_in") else None)
        self._save()
        log.info("donations: DonationAlerts token refreshed")
        return tok["access_token"]

    # ------------------------------------------------------------------ DonatePay
    def dp_verify(self) -> dict:
        """Blocking: check the API key and remember the account name."""
        key = self.settings.dp_api_key
        if not key:
            raise ValueError("ключ DonatePay не задан")
        user = P.dp_user(key)
        with self._lock:
            if (self.state["dp"].get("user") or {}).get("id") != user.get("id"):
                self.state["dp"] = {"last_id": None}
            self.state["dp"]["user"] = {"id": user.get("id"), "name": user.get("name")}
            self.status_["dp"]["error"] = None
        self._changed_threadsafe()
        return user

    # ------------------------------------------------------------------ polling
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="donations", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        due = {"da": 0.0, "dp": 0.0}
        fails = {"da": 0, "dp": 0}
        while not self._stop.wait(1.0):
            s = self.settings
            if not s.enabled:
                continue
            now = time.monotonic()
            jobs = []
            if s.da_enabled and self.state["da"].get("access_token"):
                jobs.append(("da", s.da_poll_s, self._poll_da))
            if s.dp_enabled and s.dp_api_key:
                jobs.append(("dp", s.dp_poll_s, self._poll_dp))
            for name, period, fn in jobs:
                if now < due[name]:
                    continue
                try:
                    fn()
                    fails[name] = 0
                    self._set_status(name, None)
                except Exception as e:  # noqa: BLE001 - a broken service must not kill the thread
                    fails[name] += 1
                    self._set_status(name, str(e))
                    log.warning("donations: %s poll failed: %s", name, e)
                due[name] = time.monotonic() + period * min(2 ** fails[name], 12)

    def _set_status(self, name: str, error: str | None) -> None:
        with self._lock:
            changed = self.status_[name].get("error") != error
            self.status_[name] = {"error": error, "last_poll": time.time()}
        if changed:
            self._call(lambda: self._publish("donations", self.status()))

    def _poll_da(self) -> None:
        token = self._da_access_token()
        try:
            items = P.da_donations(token)
        except P.ProviderError as e:
            refresh = self.state["da"].get("refresh_token")
            if e.status != 401 or not refresh:
                raise
            items = P.da_donations(self._da_refresh(refresh))
        self.ingest("da", items)

    def _poll_dp(self) -> None:
        items = P.dp_transactions(self.settings.dp_api_key)
        if not self.settings.dp_include_test:
            items = [d for d in items if not d.test]
        self.ingest("dp", items)

    def ingest(self, provider: str, items: list[P.Donation], now: float | None = None) -> list[P.Donation]:
        """Pick the donations that were not seen yet and hand them to the loop thread. Returns them."""
        now = time.time() if now is None else now
        numbered = []
        for d in items:
            try:
                numbered.append((int(d.id), d))
            except ValueError:
                continue
        with self._lock:
            st = self.state.setdefault(provider, {})
            last = st.get("last_id")
            top = max([n for n, _ in numbered], default=0)
            if last is None:                      # first poll of this account: remember, do not replay
                st["last_id"] = top
                fresh: list[P.Donation] = []
            else:
                new = sorted((x for x in numbered if x[0] > int(last)), key=lambda x: x[0])
                st["last_id"] = max(int(last), top)
                fresh = [d for _, d in new if d.created_ts is None or now - d.created_ts <= FRESH_S]
        if last is None or fresh or top != last:
            self._save()
        for d in fresh:
            self._call(self.handle, d)
        return fresh

    # ------------------------------------------------------------------ dispatch (loop thread)
    def handle(self, d: P.Donation) -> dict:
        text = d.message or ""
        low = text.lower()
        hint: dict | None = None
        result: dict = {"ok": True, "action": None, "text": "без действия"}
        for rule in self.settings.rules:
            if not rule["enabled"]:
                continue
            kw = rule["keyword"].lower()
            if kw and kw not in low:
                continue
            body = low.replace(kw, " ", 1) if kw else text
            if rule["action"] == "weapon":
                found = self.aliases.find(body)
                if found is None and not kw:
                    continue
                payload: Any = found
            else:
                payload = A.find_number(body)
                if payload is None:
                    continue
            if d.amount < rule["min"] or (rule["max"] > 0 and d.amount > rule["max"]):
                if hint is None and d.amount < rule["min"]:
                    hint = rule
                continue
            result = self._perform(rule["action"], payload, d)
            break
        else:
            if hint is not None:
                result = {"ok": False, "action": None,
                          "text": f"{ACTIONS[hint['action']]} — от {fmt_amount(hint['min'], d.currency)}"}
        self._record(d, result)
        if result["action"] is not None or hint is not None:
            if result["ok"]:
                self.chat_result(d.user, result["text"])
            else:
                self.chat_reject(d.user, result["text"])
        return result

    def _perform(self, action: str, payload: Any, d: P.Donation) -> dict:
        if action == "weapon":
            wid = payload[0] if payload else None
            try:
                e = self.queue_weapon(wid, d.message, d.user, d.amount, d.currency)
            except ValueError as err:
                return {"ok": False, "action": action, "text": str(err)}
            what = e.get("name") or "оружие (уточнит стример)"
            return {"ok": True, "action": action, "text": f"{what} в очереди, №{e.get('position', '?')}"}
        res = (self.add_wheel if action == "wheel_value" else self.force_next)(int(payload), d.user)
        if not res.get("ok"):
            return {"ok": False, "action": action, "text": str(res.get("error") or "не принято")}
        if action == "wheel_value":
            return {"ok": True, "action": action, "text": f"{res['value']} DPI на колесе"}
        return {"ok": True, "action": action, "text": f"следующий DPI будет {res['value']}"}

    def _record(self, d: P.Donation, result: dict) -> None:
        entry = {"t": time.time(), "provider": d.provider, "user": d.user, "amount": d.amount, "currency": d.currency,
                 "message": d.message[:200], "result": result["text"], "ok": result["ok"], "action": result["action"],
                 "test": d.test}
        with self._lock:
            self.state["log"].append(entry)
            del self.state["log"][:-MAX_LOG]
        log.info("donation %s %s %s: %s", d.provider, d.user, fmt_amount(d.amount, d.currency), result["text"])
        self._changed()
