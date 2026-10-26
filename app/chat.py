"""Twitch chat notifications through the StreamElements bot.

Why StreamElements: its bot API only needs the channel's JWT token from the SE dashboard
(Account -> channel -> "Show secrets" -> JWT Token), no OAuth application, no device flow.

* verify:  GET  https://api.streamelements.com/kappa/v2/channels/me   (Bearer JWT)
* send:    POST https://api.streamelements.com/kappa/v2/bot/{channelId}/say  {"message": "..."}

The channel id is read from the JWT payload (claim ``channel``), so sending works even when
the verify call is skipped. Messages are queued and sent from a worker thread with a minimum
interval; bursts of the same kind (e.g. three quick weapon changes) collapse into the latest.
The JWT is a secret: it is stored only in data/settings.json and never leaves the machine
except towards api.streamelements.com; the API/SSE expose only ``se_jwt_set``.
"""
from __future__ import annotations

import base64
import json
import queue
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from typing import Any, Callable

SE_API = "https://api.streamelements.com/kappa/v2"
MAX_LOG = 30


def _bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off", "нет")
    return bool(v)


def jwt_claims(token: str) -> dict:
    """Decode the payload of a JWT without verifying it (we only need the channel id)."""
    try:
        parts = token.strip().split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@dataclass
class ChatSettings:
    enabled: bool = False
    provider: str = "streamelements"
    se_jwt: str = ""                        # secret
    on_start: bool = True
    on_weapon: bool = True
    on_dpi: bool = True
    start_template: str = "🎲 Гонка вооружений началась! Первое оружие: {weapon}"
    weapon_template: str = "🎲 Оружие: {weapon} (пройдено: {completed})"
    dpi_template: str = "🎡 Новый DPI: {dpi}"
    on_viewer: bool = True
    viewer_template: str = "✅ {user} добавил {dpi} DPI на колесо"
    channel_login: str = ""                 # Twitch channel login (for the anonymous chat reader)
    min_interval_s: float = 2.0

    @classmethod
    def from_dict(cls, d: dict | None) -> "ChatSettings":
        if d is not None and not isinstance(d, dict):
            raise ValueError("chat: нужен объект")
        s = cls()
        names = {f.name for f in fields(cls)}
        for k, v in (d or {}).items():
            if k in names:
                setattr(s, k, v)
        for b in ("enabled", "on_start", "on_weapon", "on_dpi", "on_viewer"):
            setattr(s, b, _bool(getattr(s, b)))
        s.channel_login = s.channel_login.strip().lstrip("#").lower()[:40] if isinstance(s.channel_login, str) else ""
        if s.provider not in ("streamelements",):
            s.provider = "streamelements"
        s.se_jwt = s.se_jwt.strip() if isinstance(s.se_jwt, str) else ""
        for t in ("start_template", "weapon_template", "dpi_template", "viewer_template"):
            v = getattr(s, t)
            if not isinstance(v, str):
                raise ValueError(f"{t}: нужна строка")
            setattr(s, t, v.strip()[:400])
        try:
            s.min_interval_s = max(0.5, min(60.0, float(s.min_interval_s)))
        except (TypeError, ValueError):
            raise ValueError("min_interval_s: нужно число") from None
        return s

    def to_dict(self) -> dict:
        return asdict(self)

    def public_dict(self) -> dict:
        d = asdict(self)
        d.pop("se_jwt", None)
        d["se_jwt_set"] = bool(self.se_jwt)
        return d


class _Fmt(defaultdict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_template(template: str, **values: Any) -> str:
    try:
        return template.format_map(_Fmt(str, **{k: v for k, v in values.items()}))
    except (ValueError, IndexError):
        return template


class ChatNotifier:
    def __init__(self, settings: ChatSettings, publish_threadsafe: Callable[[str, dict], None],
                 sender: Callable[[str, str, str], None] | None = None) -> None:
        self.settings = settings
        self._publish = publish_threadsafe
        self._sender = sender or self._send_streamelements
        self._queue: "queue.Queue[tuple[str, str] | None]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self.channel: dict = {}
        self.sent_count = 0
        self.last_error: str | None = None
        self.last_sent_at: float | None = None
        self.log: list[dict] = []

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {
            "settings": self.settings.public_dict(),
            "channel": self.channel,
            "sent_count": self.sent_count,
            "last_error": self.last_error,
            "last_sent_at": self.last_sent_at,
            "log": self.log[-10:],
            "queued": self._queue.qsize(),
        }

    def _log(self, kind: str, text: str) -> None:
        self.log.append({"t": time.time(), "kind": kind, "text": text})
        del self.log[:-MAX_LOG]
        self._publish("chat", self.status())

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._worker, name="chat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)

    def update_settings(self, patch: dict) -> None:
        merged = {**self.settings.to_dict(), **(patch or {})}
        if "se_jwt" in (patch or {}) and not (patch or {}).get("se_jwt"):
            merged["se_jwt"] = ""          # explicit clear
        new = ChatSettings.from_dict(merged)   # may raise ValueError
        if new.se_jwt != self.settings.se_jwt:
            self.channel = {}
            self.last_error = None
        self.settings = new
        self._publish("chat", self.status())

    # ---------------------------------------------------------------- notifying
    def channel_id(self) -> str | None:
        cid = self.channel.get("id") or jwt_claims(self.settings.se_jwt).get("channel")
        return str(cid) if cid else None

    def _enqueue(self, kind: str, message: str) -> None:
        if not self.settings.enabled or not self.settings.se_jwt or not message.strip():
            return
        self._queue.put((kind, message.strip()[:450]))
        self.start()

    def notify_start(self, weapon: str, **extra: Any) -> None:
        if self.settings.on_start:
            self._enqueue("start", render_template(self.settings.start_template, weapon=weapon, **extra))

    def notify_weapon(self, weapon: str, completed: int, **extra: Any) -> None:
        if self.settings.on_weapon:
            self._enqueue("weapon", render_template(self.settings.weapon_template, weapon=weapon, completed=completed, **extra))

    def notify_dpi(self, dpi: int, reason: str = "", **extra: Any) -> None:
        if self.settings.on_dpi:
            self._enqueue("dpi", render_template(self.settings.dpi_template, dpi=dpi, reason=reason, **extra))

    def notify_viewer(self, user: str, dpi: int, **extra: Any) -> None:
        if self.settings.on_viewer:
            # one queue slot per viewer so confirmations for different people do not collapse
            self._enqueue(f"viewer:{user.lower()}", render_template(self.settings.viewer_template, user=user, dpi=dpi, **extra))

    def send_now(self, message: str) -> dict:
        """Synchronous send (test button). Returns {ok, error}."""
        try:
            self._deliver(message)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------ worker
    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            kind, message = item
            # collapse bursts: keep only the newest message of each kind that is already queued
            pending: dict[str, str] = {kind: message}
            order = [kind]
            try:
                while True:
                    nxt = self._queue.get_nowait()
                    if nxt is None:
                        return
                    k, m = nxt
                    if k not in pending:
                        order.append(k)
                    pending[k] = m
            except queue.Empty:
                pass
            for k in order:
                try:
                    self._deliver(pending[k])
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)
                    self._log("error", f"Не отправлено: {e}")
                time.sleep(self.settings.min_interval_s)

    def _deliver(self, message: str) -> None:
        cid = self.channel_id()
        if not cid:
            raise RuntimeError("не удалось определить канал: проверьте JWT-токен StreamElements")
        self._sender(self.settings.se_jwt, cid, message)
        self.sent_count += 1
        self.last_sent_at = time.time()
        self.last_error = None
        self._log("sent", message)

    # ------------------------------------------------------------- HTTP (SE)
    @staticmethod
    def _request(method: str, url: str, jwt: str, body: dict | None = None, timeout: float = 8.0) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "cs2-roulette/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code in (401, 403):
                raise RuntimeError("StreamElements отклонил токен (401/403): скопируйте JWT заново") from None
            raise RuntimeError(f"StreamElements HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"нет связи с api.streamelements.com: {e.reason}") from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {"raw": raw[:200]}

    @classmethod
    def _send_streamelements(cls, jwt: str, channel_id: str, message: str) -> None:
        cls._request("POST", f"{SE_API}/bot/{channel_id}/say", jwt, {"message": message})

    def verify(self) -> dict:
        """Check the token and remember the channel. Returns {ok, channel|error}."""
        jwt = self.settings.se_jwt
        if not jwt:
            return {"ok": False, "error": "JWT-токен не задан"}
        try:
            me = self._request("GET", f"{SE_API}/channels/me", jwt)
        except RuntimeError as e:
            self.last_error = str(e)
            self._publish("chat", self.status())
            return {"ok": False, "error": str(e)}
        cid = me.get("_id") or jwt_claims(jwt).get("channel")
        username = me.get("username") or me.get("displayName")
        self.channel = {"id": str(cid) if cid else None, "username": username,
                        "provider": me.get("provider"), "checked_at": time.time()}
        if username and not self.settings.channel_login and (me.get("provider") in (None, "twitch")):
            self.settings.channel_login = str(username).lower()
        self.last_error = None
        self._log("verify", f"Канал подтверждён: {self.channel.get('username') or cid}")
        return {"ok": True, "channel": self.channel}
