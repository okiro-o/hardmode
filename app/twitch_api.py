"""Twitch account (optional, 0.23.0): rewards created by Hardmode with automatic point refunds.

Why: the anonymous chat reader (twitch_irc.py) sees redemptions but cannot give points back, and Twitch
lets an application change a redemption's status only for rewards **created by the same client id**
(403 otherwise). So the streamer signs in once and Hardmode creates its own rewards («Оружие в очередь»,
«DPI на колесо»). Their redemptions are read here (not from chat) and answered:
* rejected (unknown weapon, number out of range, knife switched off…) → ``CANCELED`` = points refunded;
* DPI accepted → ``FULFILLED``;
* weapon accepted → stays ``UNFULFILLED`` (it waits in the viewer queue, like in Twitch's request queue);
  closing the request ✓ / by a kill → ``FULFILLED``, × → ``CANCELED`` (refund).
Chat: the streamer may also choose to send the app's chat messages from their own account
(``send_chat``, Helix «Send Chat Message», scope ``user:write:chat``) instead of the StreamElements bot
(user's decision 2026-11-08: «чат будет от моего имени»).

Login: OAuth device code flow, public client (no secret, no server, no redirect): ``POST /oauth2/device``
→ the streamer confirms on twitch.tv/activate → ``POST /oauth2/token`` with the device code. Public-client
refresh tokens rotate and live 30 days, so the account stays signed in while the app is used at least
monthly. Tokens are validated at start and hourly (Twitch requirement). Scopes: ``channel:manage:redemptions``
(also covers reading redemptions) and ``user:write:chat``. The client id comes from an application the streamer registers once
in dev.twitch.tv/console (Client Type «Public»).

Redemptions are polled (``GET …/custom_rewards/redemptions?status=UNFULFILLED``, every ``POLL_S``) instead of
EventSub: the request queue itself is the source of truth, nothing is lost across restarts, and handled ids
are remembered in data/twitch.json. Redemptions older than ``FRESH_S`` when first seen (the app was off)
are refunded. One worker thread does all HTTP; decisions are taken on the event-loop thread.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable

from . import config

log = logging.getLogger("hardmode.twitch")

ID_BASE = "https://id.twitch.tv/oauth2"
HELIX = "https://api.twitch.tv/helix"
SCOPES = ("channel:manage:redemptions", "user:write:chat")
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
POLL_S = 3.0
VALIDATE_S = 3600.0
FRESH_S = 600.0
MAX_HANDLED = 1000
USER_AGENT = "hardmode/1.0"

# the rewards Hardmode can create: kind -> defaults (title ≤ 45 chars, prompt ≤ 200)
REWARDS = {
    "weapon": {"title": "Оружие в очередь", "cost": 500, "prompt": "Какое оружие? Например: AWP, калаш, плётка"},
    "dpi": {"title": "DPI на колесо", "cost": 300, "prompt": "Число DPI, например 1600"},
}


class TwitchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class TwitchSettings:
    client_id: str = ""             # of the streamer's own Twitch application (public, not a secret)

    @classmethod
    def from_dict(cls, d: dict | None) -> "TwitchSettings":
        s = cls()
        config.assign_fields(s, d, "twitch")
        cid = s.client_id.strip() if isinstance(s.client_id, str) else ""
        if cid and not re.fullmatch(r"[A-Za-z0-9]{10,64}", cid):
            raise ValueError("client_id: это строка из латинских букв и цифр со страницы приложения Twitch")
        s.client_id = cid
        return s

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------- HTTP
def _http(method: str, url: str, *, headers: dict | None = None, form: dict | None = None,
          body: dict | None = None, timeout: float = 10.0) -> dict:
    data, h = None, {"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})}
    if form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(detail).get("message") or detail
        except ValueError:
            msg = detail[:200] or e.reason
        raise TwitchError(str(msg)[:300], status=e.code) from None
    except (urllib.error.URLError, OSError) as e:
        raise TwitchError(f"нет связи с Twitch: {getattr(e, 'reason', e)}") from None
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except ValueError:
        raise TwitchError(f"Twitch прислал не JSON: {raw[:120]}") from None
    return out if isinstance(out, dict) else {"data": out}


def _ts(value: Any) -> float | None:
    """RFC 3339 (``2026-11-08T12:00:00.123456Z``) → epoch seconds."""
    try:
        s = str(value).replace("Z", "+00:00")
        s = re.sub(r"\.(\d{6})\d+", r".\1", s)          # Twitch may send nanoseconds
        return _dt.datetime.fromisoformat(s).timestamp()
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------------- service
class TwitchService:
    def __init__(self, settings: TwitchSettings, state_path: str,
                 publish_threadsafe: Callable[[str, dict], None], call_threadsafe: Callable[..., None]) -> None:
        self.settings = settings
        self.state_path = state_path
        self._publish = publish_threadsafe
        self._call = call_threadsafe
        self._lock = threading.RLock()
        self.state: dict = {"token": {}, "user": {}, "rewards": {}, "handled": [], "kept": {}}
        self.login_flow: dict | None = None      # {user_code, uri, expires_at}
        self.error: str | None = None
        self.last_poll: float | None = None
        self._login_gen = 0
        self._actions: "queue.Queue[tuple[str, str, str]]" = queue.Queue()   # (reward_id, redemption_id, status)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_validate = 0.0
        # wired by app.main, called on the event-loop thread: (kind, redemption) -> "fulfill" | "cancel" | "keep"
        self.on_redemption: Callable[[str, dict], str] = lambda kind, r: "cancel"
        self._http = _http                       # tests replace it
        self._load()

    # ------------------------------------------------------------------ state
    def _load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(d, dict):
            for k in ("token", "user", "rewards", "kept"):
                if isinstance(d.get(k), dict):
                    self.state[k] = d[k]
            if isinstance(d.get("handled"), list):
                self.state["handled"] = [str(x) for x in d["handled"]][-MAX_HANDLED:]

    def _save(self) -> None:
        with self._lock:
            try:
                config.write_json_atomic(self.state_path, self.state)
            except OSError:
                log.warning("cannot save twitch state", exc_info=True)

    def update_settings(self, patch: dict) -> None:
        new = TwitchSettings.from_dict({**self.settings.to_dict(), **(patch or {})})
        changed = new.client_id != self.settings.client_id
        self.settings = new
        if changed and self.state["token"]:
            self.logout(revoke=False)         # tokens belong to the old application
        self._changed()

    @property
    def logged_in(self) -> bool:
        return bool(self.state["token"].get("access_token") and self.state["user"].get("id"))

    @property
    def can_chat(self) -> bool:
        return self.logged_in and "user:write:chat" in (self.state["user"].get("scopes") or SCOPES)

    def status(self) -> dict:
        with self._lock:
            u = self.state["user"]
            return {"client_id": self.settings.client_id, "logged_in": self.logged_in,
                    "user": {"login": u.get("login"), "display_name": u.get("display_name"),
                             "broadcaster_type": u.get("broadcaster_type")} if u else None,
                    "can_chat": self.can_chat,
                    "login_flow": dict(self.login_flow) if self.login_flow else None,
                    "rewards": {k: dict(v) for k, v in self.state["rewards"].items()},
                    "defaults": REWARDS, "error": self.error, "last_poll": self.last_poll}

    def _changed(self) -> None:
        self._save()
        self._publish("twitch_account", self.status())

    def managed_kind(self, reward_id: str | None) -> str | None:
        """Which of our rewards this is (``weapon``/``dpi``), None for any other reward."""
        if not reward_id:
            return None
        return next((k for k, r in self.state["rewards"].items() if r.get("id") == reward_id), None)

    # ------------------------------------------------------------------ login
    def start_login(self) -> dict:
        """Blocking (HTTP). Returns the code the streamer confirms on twitch.tv/activate."""
        cid = self.settings.client_id
        if not cid:
            raise TwitchError("сначала вставьте Client ID своего приложения Twitch")
        d = self._http("POST", f"{ID_BASE}/device", form={"client_id": cid, "scopes": " ".join(SCOPES)})
        if not d.get("device_code"):
            raise TwitchError(f"Twitch не выдал код: {str(d)[:200]}")
        with self._lock:
            self._login_gen += 1
            gen = self._login_gen
            self.login_flow = {"user_code": d.get("user_code"), "uri": d.get("verification_uri"),
                               "expires_at": time.time() + float(d.get("expires_in") or 1800)}
            self.error = None
        self._changed()
        threading.Thread(target=self._login_run, args=(gen, cid, d["device_code"], float(d.get("interval") or 5)),
                         name="twitch-login", daemon=True).start()
        return dict(self.login_flow)

    def _login_run(self, gen: int, cid: str, device_code: str, interval: float) -> None:
        while not self._stop.wait(max(1.0, interval)):
            with self._lock:
                if gen != self._login_gen or not self.login_flow:
                    return                                   # cancelled or restarted
                expired = time.time() > self.login_flow["expires_at"]
            if expired:
                self._login_failed(gen, "код входа устарел, нажмите «Войти» ещё раз")
                return
            try:
                tok = device_poll_with(self._http, cid, device_code)
            except TwitchError as e:
                self._login_failed(gen, f"вход не удался: {e}")
                return
            if tok is None:
                continue
            try:
                self._finish_login(tok)
            except TwitchError as e:
                self._login_failed(gen, f"вход не удался: {e}")
                return
            with self._lock:
                if gen == self._login_gen:
                    self.login_flow = None
            self._changed()
            return

    def _login_failed(self, gen: int, text: str) -> None:
        with self._lock:
            if gen != self._login_gen:
                return
            self.login_flow = None
            self.error = text
        self._changed()

    def _finish_login(self, tok: dict) -> None:
        if not tok.get("access_token"):
            raise TwitchError(f"Twitch не выдал токен: {str(tok)[:200]}")
        token = {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", ""),
                 "expires_at": time.time() + float(tok.get("expires_in") or 3600)}
        info = self._http("GET", f"{ID_BASE}/validate", headers={"Authorization": f"OAuth {token['access_token']}"})
        users = self._http("GET", f"{HELIX}/users", headers=self._headers(token["access_token"])).get("data") or [{}]
        u = users[0] if users else {}
        with self._lock:
            self.state["token"] = token
            self.state["user"] = {"id": info.get("user_id") or u.get("id"), "login": info.get("login") or u.get("login"),
                                  "display_name": u.get("display_name") or info.get("login"),
                                  "broadcaster_type": u.get("broadcaster_type") or "",
                                  "scopes": list(info.get("scopes") or SCOPES)}
            self.error = None
        self._next_validate = time.monotonic() + VALIDATE_S
        log.info("twitch: signed in as %s", self.state["user"].get("login"))

    def logout(self, revoke: bool = True) -> None:
        with self._lock:
            token = self.state["token"].get("access_token")
            self._login_gen += 1
            self.login_flow = None
            self.state["token"], self.state["user"] = {}, {}
            self.error = None
        if revoke and token and self.settings.client_id:
            try:
                self._http("POST", f"{ID_BASE}/revoke", form={"client_id": self.settings.client_id, "token": token})
            except TwitchError:
                pass
        self._changed()

    # ------------------------------------------------------------------ tokens & helix
    def _headers(self, access_token: str) -> dict:
        return {"Authorization": f"Bearer {access_token}", "Client-Id": self.settings.client_id}

    def _refresh(self) -> str:
        with self._lock:
            rt = self.state["token"].get("refresh_token")
        if not rt:
            raise TwitchError("вход устарел: войдите через Twitch ещё раз", 401)
        try:
            tok = self._http("POST", f"{ID_BASE}/token", form={"client_id": self.settings.client_id,
                                                               "grant_type": "refresh_token", "refresh_token": rt})
        except TwitchError as e:
            if e.status in (400, 401):
                self._signed_out("вход устарел (Twitch не продлил его): войдите через Twitch ещё раз")
            raise
        with self._lock:
            self.state["token"] = {"access_token": tok["access_token"],
                                   "refresh_token": tok.get("refresh_token") or rt,
                                   "expires_at": time.time() + float(tok.get("expires_in") or 3600)}
        self._save()
        return tok["access_token"]

    def _signed_out(self, text: str) -> None:
        with self._lock:
            self.state["token"], self.state["user"] = {}, {}
            self.error = text
        self._changed()

    def _access_token(self) -> str:
        with self._lock:
            t = self.state["token"]
            token, exp = t.get("access_token"), float(t.get("expires_at") or 0)
        if not token:
            raise TwitchError("не выполнен вход в Twitch", 401)
        return self._refresh() if time.time() > exp - 60 else token

    def helix(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = f"{HELIX}/{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
        try:
            return self._http(method, url, headers=self._headers(self._access_token()), body=body)
        except TwitchError as e:
            if e.status != 401:
                raise
            return self._http(method, url, headers=self._headers(self._refresh()), body=body)

    # ------------------------------------------------------------------ chat
    def send_chat(self, message: str) -> dict:
        """Blocking: post to the streamer's own chat from the streamer's account."""
        if not self.can_chat:
            raise TwitchError("нет входа в Twitch с разрешением на чат: войдите через Twitch")
        uid = self.state["user"]["id"]
        try:
            data = self.helix("POST", "chat/messages", body={"broadcaster_id": uid, "sender_id": uid,
                                                              "message": message[:500]}).get("data") or [{}]
        except TwitchError as e:
            if e.status in (401, 403):
                raise TwitchError(f"Twitch не дал написать в чат: войдите через Twitch заново ({e})", e.status) from None
            raise
        d = data[0] if data else {}
        if d.get("is_sent") is False:
            reason = (d.get("drop_reason") or {}).get("message") or "сообщение отклонено"
            raise TwitchError(f"Twitch не отправил сообщение: {reason}")
        return d

    # ------------------------------------------------------------------ rewards
    def create_reward(self, kind: str, title: str | None = None, cost: Any = None, prompt: str | None = None) -> dict:
        """Blocking. Creates the reward in the channel (owned by our client id) and remembers it."""
        if kind not in REWARDS:
            raise TwitchError("неизвестная награда")
        if not self.logged_in:
            raise TwitchError("сначала войдите через Twitch")
        d = REWARDS[kind]
        title = (str(title or "").strip() or d["title"])[:45]
        try:
            cost = max(1, int(float(cost if cost not in (None, "") else d["cost"])))
        except (TypeError, ValueError):
            raise TwitchError("стоимость: нужно число") from None
        body = {"title": title, "cost": cost, "prompt": (prompt or d["prompt"])[:200], "is_enabled": True,
                "is_user_input_required": True, "should_redemptions_skip_request_queue": False}
        try:
            data = self.helix("POST", "channel_points/custom_rewards", {"broadcaster_id": self.state["user"]["id"]},
                              body).get("data") or []
        except TwitchError as e:
            if "DUPLICATE" in str(e).upper():
                raise TwitchError(f"в канале уже есть награда «{title}»: удалите её в Twitch или назовите иначе",
                                  e.status) from None
            if e.status == 403:
                raise TwitchError("Twitch не дал создать награду: баллы канала есть только у компаньонов и "
                                  f"партнёров ({e})", e.status) from None
            raise
        if not data:
            raise TwitchError("Twitch не вернул созданную награду")
        r = data[0]
        with self._lock:
            self.state["rewards"][kind] = {"id": r.get("id"), "title": r.get("title", title), "cost": r.get("cost", cost)}
        self._changed()
        log.info("twitch: reward %s created: %s", kind, r.get("id"))
        return dict(self.state["rewards"][kind])

    def delete_reward(self, kind: str) -> None:
        """Blocking. Deletes our reward in Twitch (if it is still there) and forgets it."""
        with self._lock:
            r = self.state["rewards"].get(kind)
        if not r:
            return
        if self.logged_in:
            try:
                self.helix("DELETE", "channel_points/custom_rewards",
                           {"broadcaster_id": self.state["user"]["id"], "id": r["id"]})
            except TwitchError as e:
                if e.status != 404:
                    raise
        with self._lock:
            self.state["rewards"].pop(kind, None)
        self._changed()

    def _sync_rewards(self) -> None:
        """Forget our rewards that were deleted in the Twitch dashboard."""
        if not self.state["rewards"]:
            return
        data = self.helix("GET", "channel_points/custom_rewards",
                          {"broadcaster_id": self.state["user"]["id"], "only_manageable_rewards": "true"}).get("data") or []
        alive = {r.get("id"): r for r in data}
        with self._lock:
            gone = [k for k, r in self.state["rewards"].items() if r.get("id") not in alive]
            for k in gone:
                self.state["rewards"].pop(k)
            for k, r in self.state["rewards"].items():          # title/cost edited in Twitch
                a = alive.get(r["id"]) or {}
                r["title"], r["cost"] = a.get("title", r.get("title")), a.get("cost", r.get("cost"))
        if gone:
            log.info("twitch: rewards deleted in Twitch: %s", gone)
        self._changed()

    # ------------------------------------------------------------------ redemptions
    def set_status(self, redemption: dict | None, status: str) -> None:
        """Thread-safe: queue FULFILLED / CANCELED for a redemption {reward_id, id}."""
        if redemption and redemption.get("id") and redemption.get("reward_id"):
            with self._lock:
                self.state["kept"].pop(redemption["id"], None)
            self._actions.put((redemption["reward_id"], redemption["id"], status))

    def poll_once(self, now: float | None = None) -> int:
        """Fetch new redemptions of our rewards and hand them to the loop thread. Returns how many."""
        now = time.time() if now is None else now
        if not (self.logged_in and self.state["rewards"]):
            return 0
        found = 0
        for kind, r in list(self.state["rewards"].items()):
            data = self.helix("GET", "channel_points/custom_rewards/redemptions",
                              {"broadcaster_id": self.state["user"]["id"], "reward_id": r["id"],
                               "status": "UNFULFILLED", "sort": "OLDEST", "first": 50}).get("data") or []
            for red in data:
                rid = str(red.get("id") or "")
                with self._lock:
                    if not rid or rid in self.state["handled"] or rid in self.state["kept"]:
                        continue
                    self.state["handled"].append(rid)
                    del self.state["handled"][:-MAX_HANDLED]
                found += 1
                item = {"id": rid, "reward_id": r["id"], "user": red.get("user_name") or red.get("user_login") or "?",
                        "login": red.get("user_login") or "", "text": str(red.get("user_input") or ""),
                        "t": _ts(red.get("redeemed_at"))}
                if item["t"] is not None and now - item["t"] > FRESH_S:
                    log.info("twitch: stale redemption %s of %s refunded", rid, item["user"])
                    self._actions.put((r["id"], rid, "CANCELED"))    # the app was off: give the points back
                    continue
                self._call(self._dispatch, kind, item)
        self.last_poll = now
        if found:
            self._save()
        return found

    def _dispatch(self, kind: str, item: dict) -> None:
        """Event-loop thread: let the app decide, then queue the status change."""
        try:
            decision = self.on_redemption(kind, item)
        except Exception:  # noqa: BLE001 - a bug must not keep the viewer's points
            log.exception("twitch: redemption handler failed")
            decision = "cancel"
        if decision == "keep":
            with self._lock:
                self.state["kept"][item["id"]] = item["reward_id"]
            self._save()
        else:
            self._actions.put((item["reward_id"], item["id"], "FULFILLED" if decision == "fulfill" else "CANCELED"))

    def flush_actions(self) -> int:
        n = 0
        while True:
            try:
                reward_id, rid, status = self._actions.get_nowait()
            except queue.Empty:
                return n
            try:
                self.helix("PATCH", "channel_points/custom_rewards/redemptions",
                           {"broadcaster_id": self.state["user"]["id"], "reward_id": reward_id, "id": rid},
                           {"status": status})
                n += 1
                log.info("twitch: redemption %s → %s", rid, status)
            except TwitchError as e:
                # 400/404: already handled in the Twitch dashboard; anything else is logged and dropped
                log.warning("twitch: redemption %s → %s failed: %s", rid, status, e)

    # ------------------------------------------------------------------ worker
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="twitch-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        next_poll = 0.0
        fails = 0
        while not self._stop.wait(0.5):
            if not self.logged_in:
                continue
            try:
                if time.monotonic() >= self._next_validate:
                    self._next_validate = time.monotonic() + VALIDATE_S
                    self.helix("GET", "users")         # cheap call that also refreshes an expired token
                    self._sync_rewards()
                self.flush_actions()
                if time.monotonic() >= next_poll:
                    self.poll_once()
                    next_poll = time.monotonic() + POLL_S * min(2 ** fails, 10)
                    fails = 0
                    if self.error:
                        self.error = None
                        self._changed()
            except TwitchError as e:
                fails += 1
                next_poll = time.monotonic() + POLL_S * min(2 ** fails, 10)
                if self.error != str(e):
                    self.error = str(e)
                    self._changed()
                log.warning("twitch: %s", e)
            except Exception:  # noqa: BLE001 - never kill the thread
                log.exception("twitch worker failed")


def device_poll_with(http: Callable[..., dict], client_id: str, device_code: str) -> dict | None:
    """``device_poll`` through a replaceable HTTP function (tests)."""
    try:
        return http("POST", f"{ID_BASE}/token", form={"client_id": client_id, "scopes": " ".join(SCOPES),
                                                      "device_code": device_code, "grant_type": DEVICE_GRANT})
    except TwitchError as e:
        msg = str(e).lower()
        if e.status == 400 and ("pending" in msg or "slow" in msg):
            return None
        raise
