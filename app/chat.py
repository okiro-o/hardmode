"""Twitch chat notifications, sent from the streamer's own account through the Twitch API (app.twitch_api).

Until 0.24.0 messages went through the StreamElements bot (JWT token from the SE dashboard); the user dropped
it once Hardmode got its own Twitch sign-in (2026-11-08). Messages are queued and sent from a worker thread
with a minimum interval; bursts of the same kind (e.g. three quick weapon changes) collapse into the latest.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from . import config

MAX_LOG = 30
log = logging.getLogger("hardmode.chat")


@dataclass
class ChatSettings:
    enabled: bool = False
    on_start: bool = True
    on_weapon: bool = True
    on_dpi: bool = True
    start_template: str = "🎲 Гонка вооружений началась! Первое оружие: {weapon}"
    weapon_template: str = "🎲 Оружие: {weapon} (пройдено: {completed})"
    dpi_template: str = "🎡 Новый DPI: {dpi}"
    on_viewer: bool = True
    viewer_template: str = "✅ {user} добавил {dpi} DPI на колесо"
    on_reject: bool = True
    reject_template: str = "❌ {user}: {reason}"
    reject_cooldown_s: float = 30.0         # at most one rejection message per viewer per this interval
    on_donation: bool = True                # what a donation did (or why it did nothing)
    donation_template: str = "💸 {user}: {result}"
    on_request: bool = True                 # a weapon bought with channel points went into the queue
    request_template: str = "🎯 {user} заказал {weapon}, №{position} в очереди"
    on_request_done: bool = True            # a viewer weapon request was fulfilled
    request_done_template: str = "✅ {weapon} для {user} — выполнено"
    queue_template: str = "📋 @{user}: {result}"   # answer to the queue chat command
    min_interval_s: float = 2.0

    @classmethod
    def from_dict(cls, d: dict | None, strict: bool = True) -> "ChatSettings":
        """``strict``: a template that cannot be filled is an error (the panel shows it); on start (settings.json
        written by an older version) it falls back to its default instead of resetting every chat setting."""
        s = cls()
        config.assign_fields(s, d, "chat")
        for b in ("enabled", "on_start", "on_weapon", "on_dpi", "on_viewer", "on_reject", "on_donation", "on_request",
                  "on_request_done"):
            setattr(s, b, config.to_bool(getattr(s, b)))
        for t in ("start_template", "weapon_template", "dpi_template", "viewer_template", "reject_template",
                  "donation_template", "request_template", "request_done_template", "queue_template"):
            v = getattr(s, t)
            if not isinstance(v, str):
                raise ValueError(f"{t}: нужна строка")
            setattr(s, t, v.strip()[:400])
            if render_template(getattr(s, t), **_SAMPLE) is None:
                if strict:
                    raise ValueError(f"{t}: шаблон не разбирается, подстановки пишутся как {{user}}")
                setattr(s, t, getattr(cls, t))
        try:
            s.min_interval_s = max(0.5, min(60.0, float(s.min_interval_s)))
            s.reject_cooldown_s = max(0.0, min(3600.0, float(s.reject_cooldown_s)))
        except (TypeError, ValueError):
            raise ValueError("min_interval_s / reject_cooldown_s: нужно число") from None
        return s

    def to_dict(self) -> dict:
        return asdict(self)


class _Fmt(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


_SAMPLE = {k: "x" for k in ("weapon", "completed", "kills", "side", "dpi", "user", "reason", "result", "position")}


def render_template(template: str, **values: Any) -> str | None:
    """The message, or None for a template that cannot be filled ({user.x}, {user[0]}, a lone brace…)."""
    try:
        return template.format_map(_Fmt(values))
    except Exception:  # noqa: BLE001 - any formatting error: a chat line must never break the caller
        return None


class ChatNotifier:
    def __init__(self, settings: ChatSettings, publish_threadsafe: Callable[[str, dict], None]) -> None:
        self.settings = settings
        self._publish = publish_threadsafe
        # the streamer's own Twitch account (app.twitch_api), wired by app.state
        self.twitch_send: Callable[[str], dict] | None = None
        self.twitch_ready: Callable[[], bool] = lambda: False
        self._queue: "queue.Queue[tuple[str, str] | None]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self.last_response: dict | None = None
        self.sent_count = 0
        self.last_error: str | None = None
        self.log: list[dict] = []
        self._reject_seen: dict[str, float] = {}   # user -> last rejection message time
        self._donation_seq = 0
        self._queue_seen: dict[str, float] = {}     # user -> last answer to the queue command

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {
            "settings": self.settings.to_dict(),
            "ready": self.twitch_ready(),
            "sent_count": self.sent_count,
            "last_error": self.last_error,
            "log": self.log[-10:],
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
        self.settings = ChatSettings.from_dict({**self.settings.to_dict(), **(patch or {})})   # may raise ValueError
        self._publish("chat", self.status())

    # ---------------------------------------------------------------- notifying
    def can_send(self) -> bool:
        """Messages are on and the streamer is signed in to Twitch with the chat permission."""
        return bool(self.settings.enabled and self.twitch_ready())

    def _enqueue(self, kind: str, message: str | None) -> None:
        if not self.can_send() or not message or not message.strip():
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

    def notify_donation(self, user: str, result: str, ok: bool = True, **extra: Any) -> None:
        """What a donation did. Never rate-limited and never merged with other donations: the donor paid."""
        if not self.settings.on_donation:
            return
        self._donation_seq += 1
        template = self.settings.donation_template if ok else self.settings.reject_template
        self._enqueue(f"donation:{self._donation_seq}", render_template(template, user=user, result=result, reason=result, **extra))

    def notify_request(self, user: str, weapon: str, position: Any, result: str = "", **extra: Any) -> None:
        """A channel-points weapon request was queued. Each one is its own message: points were spent."""
        if self.settings.on_request:
            self._donation_seq += 1
            self._enqueue(f"request-add:{self._donation_seq}", render_template(
                self.settings.request_template, user=user, weapon=weapon, position=position, result=result, **extra))

    def notify_request_done(self, user: str, weapon: str, **extra: Any) -> None:
        if self.settings.on_request_done:
            self._donation_seq += 1
            self._enqueue(f"request:{self._donation_seq}",
                          render_template(self.settings.request_done_template, user=user, weapon=weapon, **extra))

    def notify_queue_reply(self, user: str, result: str) -> bool:
        """Answer the queue command; one answer per viewer per 15 s so the command cannot flood the chat."""
        if not self.can_send():
            return False
        key = (user or "").lower()
        now = time.monotonic()
        if now - self._queue_seen.get(key, 0.0) < 15.0:
            return False
        self._queue_seen[key] = now
        if len(self._queue_seen) > 500:
            for k, t in list(self._queue_seen.items()):
                if now - t > 600:
                    self._queue_seen.pop(k, None)
        self._enqueue(f"queue:{key}", render_template(self.settings.queue_template, user=user, result=result))
        return True

    def notify_reject(self, user: str, reason: str, **extra: Any) -> bool:
        """Tell a viewer why their value was not accepted (rate-limited per viewer)."""
        if not self.settings.on_reject:
            return False
        key = (user or "").lower()
        now = time.monotonic()
        last = self._reject_seen.get(key, 0.0)
        if now - last < self.settings.reject_cooldown_s:
            return False
        self._reject_seen[key] = now
        if len(self._reject_seen) > 500:   # forget old spammers
            for k, t in list(self._reject_seen.items()):
                if now - t > 3600:
                    self._reject_seen.pop(k, None)
        self._enqueue(f"reject:{key}", render_template(self.settings.reject_template, user=user, reason=reason, **extra))
        return True

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
                    log.warning("chat: not sent: %s", e)
                    self._log("error", f"Не отправлено: {e}")
                time.sleep(self.settings.min_interval_s)

    def _deliver(self, message: str) -> None:
        if not self.twitch_send or not self.twitch_ready():
            raise RuntimeError("нет входа в Twitch: нажмите «Войти через Twitch» справа сверху")
        res = self.twitch_send(message)
        self.last_response = res if isinstance(res, dict) else None
        self.sent_count += 1
        self.last_error = None
        log.info("chat: sent %r", message)
        self._log("sent", message)
