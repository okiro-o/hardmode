"""Twitch predictions on the match result (0.31.0, user request «заводить прогноз на победу или луз»).

The streamer's own Twitch account (``twitch_api.TwitchService``, scope ``channel:manage:predictions``) opens a
prediction «Победа на Mirage?» with two outcomes when a match goes live (first round, GSI event
``match_start``), Twitch closes the bets after ``window_min`` minutes by itself, and the final screen of the
match (GSI ``match_end``) resolves it: win/loss → the winning outcome, a draw or a mode without a team score
→ cancelled, points go back. A prediction left open by an abandoned match is cancelled before the next one
(Twitch allows one per channel). The panel can start, resolve and cancel by hand.

Only what the streamer sees anyway is used (the final score), see SPEC §9 «честная игра»."""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable

from . import config
from .gsi import NO_TEAM_RESULT
from .stats import map_title
from .twitch_api import TwitchError, TwitchService

log = logging.getLogger("hardmode.predictions")

SCOPE = "channel:manage:predictions"
NO_MAP = "этой карте"          # «Победа на этой карте?» when the map is not known (manual start in the menu)


@dataclass
class PredictionSettings:
    enabled: bool = False
    title: str = "Победа на {map}?"     # Twitch: at most 45 characters after {map} is filled in
    win: str = "Победа"                 # outcome titles, at most 25 characters each
    loss: str = "Поражение"
    window_min: int = 3                 # bets are taken this long (Twitch: 30 s … 30 min)

    @classmethod
    def from_dict(cls, d: dict | None) -> "PredictionSettings":
        s = cls()
        config.assign_fields(s, d, "predictions")
        s.enabled = config.to_bool(s.enabled)
        s.title = (str(s.title or "").strip() or cls.title)[:60]
        s.win = (str(s.win or "").strip() or cls.win)[:25]
        s.loss = (str(s.loss or "").strip() or cls.loss)[:25]
        if s.win.casefold() == s.loss.casefold():
            raise ValueError("ответы должны различаться")
        s.window_min = config.to_num_int(s.window_min, "window_min")
        if not 1 <= s.window_min <= 30:
            raise ValueError("window_min: от 1 до 30 минут")
        return s

    def to_dict(self) -> dict:
        return asdict(self)

    def title_for(self, map_name: str | None) -> str:
        return self.title.replace("{map}", map_title(map_name) if map_name else NO_MAP)[:45]


class PredictionService:
    def __init__(self, settings: PredictionSettings, twitch: TwitchService, state_path: str,
                 publish_threadsafe: Callable[[str, dict], None]) -> None:
        self.settings = settings
        self.twitch = twitch
        self.state_path = state_path
        self._publish = publish_threadsafe
        self._lock = threading.RLock()
        self._op = threading.Lock()          # one Twitch operation at a time: a result waits for the opening
        # current: the prediction this app opened and has not finished yet
        # {id, title, map, outcomes: {win: id, loss: id}, started_at, window_s}
        self.current: dict | None = None
        self.last: dict | None = None       # {title, text, t}: how the previous one ended
        self.error: str | None = None
        self.busy = False
        self._load()

    # ------------------------------------------------------------------ store
    def _load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if isinstance(d, dict):
            self.current = d.get("current") if isinstance(d.get("current"), dict) and d["current"].get("id") else None
            self.last = d.get("last") if isinstance(d.get("last"), dict) else None

    def _changed(self) -> None:
        try:
            config.write_json_atomic(self.state_path, {"current": self.current, "last": self.last})
        except OSError:
            log.warning("cannot save predictions state", exc_info=True)
        self._publish("predictions", self.status())

    def update_settings(self, d: dict) -> None:
        merged = {**self.settings.to_dict(), **(d or {})}
        self.settings = PredictionSettings.from_dict(merged)
        self._publish("predictions", self.status())

    # ----------------------------------------------------------------- status
    @property
    def allowed(self) -> bool:
        """Signed in to Twitch with the predictions permission (older sign-ins lack it)."""
        return self.twitch.logged_in and SCOPE in (self.twitch.state["user"].get("scopes") or [])

    def status(self) -> dict:
        with self._lock:
            cur = dict(self.current) if self.current else None
            if cur:
                cur["locks_at"] = cur["started_at"] + cur["window_s"]
            return {"settings": self.settings.to_dict(), "logged_in": self.twitch.logged_in, "allowed": self.allowed,
                    "current": cur, "last": dict(self.last) if self.last else None, "error": self.error, "busy": self.busy}

    # ----------------------------------------------------------- GSI hooks
    def on_match_start(self, map_name: str | None, mode: str | None) -> None:
        """Event-loop thread: a match went live. Opens the prediction in the background."""
        if not self.settings.enabled or mode in NO_TEAM_RESULT or not self.allowed:
            return
        threading.Thread(target=self._safe, args=(self.start, map_name, True), name="twitch-prediction", daemon=True).start()

    def on_match_end(self, result: str | None) -> None:
        """Event-loop thread: the final screen. win/loss pays out, anything else gives the points back."""
        if not (self.current or self.busy):
            return
        threading.Thread(target=self._safe, args=(self.finish, result), name="twitch-prediction", daemon=True).start()

    def _safe(self, fn: Callable, *args) -> None:
        try:
            fn(*args)
        except (TwitchError, ValueError) as e:
            log.warning("twitch prediction: %s", e)

    # --------------------------------------------------------------- actions
    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._publish("predictions", self.status())

    def _fail(self, e: TwitchError) -> TwitchError:
        if e.status in (401, 403):
            text = "Twitch не дал управлять прогнозами: войдите через Twitch заново и разрешите прогнозы"
        else:
            text = f"Twitch: {e}"
        self.error = text
        self._changed()
        return TwitchError(text, e.status)

    def start(self, map_name: str | None = None, wait: bool = False) -> dict:
        """Blocking. Opens a prediction; an unfinished one of ours is cancelled first. ``wait``: the automatic
        start waits for a result still being sent; a click in the panel is refused instead."""
        if not self.allowed:
            raise ValueError("нужен вход в Twitch с разрешением на прогнозы")
        if not (self._op.acquire(timeout=30) if wait else self._op.acquire(blocking=False)):
            raise ValueError("подождите: Twitch ещё отвечает на прошлый запрос")
        self._set_busy(True)
        try:
            if self.current:
                self._end("CANCELED", None, "отменён: начался новый матч")
            s = self.settings
            title = s.title_for(map_name)
            try:
                data = self.twitch.helix("POST", "predictions", body={
                    "broadcaster_id": self.twitch._uid(), "title": title,
                    "outcomes": [{"title": s.win}, {"title": s.loss}], "prediction_window": s.window_min * 60,
                }).get("data") or [{}]
            except TwitchError as e:
                raise self._fail(e) from None
            p = data[0] if data else {}
            outs = p.get("outcomes") or []
            ids = {o.get("title"): o.get("id") for o in outs}
            with self._lock:
                self.current = {"id": p.get("id"), "title": p.get("title", title), "map": map_name,
                                "outcomes": {"win": ids.get(s.win) or (outs[0].get("id") if outs else None),
                                             "loss": ids.get(s.loss) or (outs[1].get("id") if len(outs) > 1 else None)},
                                "names": {"win": s.win, "loss": s.loss},
                                "started_at": time.time(), "window_s": s.window_min * 60}
                self.error = None
                opened = dict(self.current)
            log.info("twitch: prediction «%s» opened", title)
            self._changed()
            return opened
        finally:
            self._op.release()
            self._set_busy(False)

    def finish(self, result: str | None) -> None:
        """Blocking. ``win``/``loss`` resolves, anything else (draw, unknown) cancels with a refund."""
        with self._op:
            if not self.current:
                return
            if result in ("win", "loss"):
                name = self.current["names"][result]
                self._end("RESOLVED", self.current["outcomes"][result], f"итог: {name}")
            else:
                self._end("CANCELED", None, "отменён, баллы возвращены" + (": ничья" if result == "draw" else ""))

    def cancel(self) -> None:
        with self._op:
            if self.current:
                self._end("CANCELED", None, "отменён вручную, баллы возвращены")

    def _end(self, status: str, outcome_id: str | None, text: str) -> None:
        cur = self.current
        body = {"broadcaster_id": self.twitch._uid(), "id": cur["id"], "status": status}
        if outcome_id:
            body["winning_outcome_id"] = outcome_id
        try:
            self.twitch.helix("PATCH", "predictions", body=body)
        except TwitchError as e:
            if e.status not in (400, 404):          # 400/404: already finished or deleted in the Twitch dashboard
                raise self._fail(e) from None
            text += " (в Twitch он уже был завершён)"
        with self._lock:
            self.current = None
            self.last = {"title": cur["title"], "text": text, "t": time.time()}
            self.error = None
        log.info("twitch: prediction «%s» %s", cur["title"], text)
        self._changed()
