"""Application state: one ``AppState`` wires the modules together (roulette, DPI, stats, queue, donations, chat,
Twitch, OBS, predictions, in-game overlay, updater) and holds the glue between them; ``app.main`` builds the
FastAPI app around it.

State is changed from the event-loop thread: every endpoint is ``async`` (blocking I/O goes to an executor);
background threads (chat, Twitch, DPI guard, OBS, updater) marshal changes through ``Broker.call_threadsafe`` /
``publish_threadsafe``. The in-game overlay thread only reads (``ingame_state``), so the reads it uses must not
repair or rewrite state.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from . import autostart, config, loadout as L, steam, weapons as W
from .events import Broker
from .gsi import GsiTracker, build_cfg
from .obs import ObsService, ObsSettings
from .predictions import PredictionService, PredictionSettings
from .gsi_record import GsiRecorder
from .updater import Updater
from .twitch_api import TwitchService
from .ingame import IngameOverlays
from .chat import ChatNotifier, ChatSettings
from .modules.donations.queue import SIDE_TWINS, ViewerQueue
from .modules.donations.service import DonationService, DonationSettings
from .modules.dpi import DpiService, DpiSettings
from .roulette import Roulette
from .stats import StatsTracker
from .twitch_irc import IrcMessage, TwitchChatReader
from .version import VERSION

log = logging.getLogger("hardmode")


class AppState:
    def __init__(self) -> None:
        self.settings = config.AppSettings.load()
        self.roulette = Roulette.load(config.state_path())
        try:
            self.roulette.update_settings(self.settings.roulette)  # settings.json wins over state.json
        except ValueError:
            self.settings.roulette = self.roulette.settings.to_dict()
        self.gsi = GsiTracker(token=self.settings.gsi_token)
        self.broker = Broker()
        self.dpi = DpiService(self._typed("dpi", DpiSettings), config.dpi_state_path(),
                              publish=self.broker.publish, publish_threadsafe=self.broker.publish_threadsafe)
        self.chat = ChatNotifier(self._typed("chat", ChatSettings, strict=False), publish_threadsafe=self.broker.publish_threadsafe)
        self.stats = StatsTracker(os.path.join(config.data_dir(), "stats.json"))
        self.dpi.on_applied = lambda value, reason: (self.chat.notify_dpi(value, reason), self.stats.on_dpi_applied(value))
        self.dpi.on_spin = lambda value, reason: (self.stats.on_dpi_roll(value, reason),
                                                  self.broker.publish("stats", self.stats.snapshot()))
        # confirmations are switched in the chat settings («Зритель добавил DPI»), not in the DPI block
        self.dpi.on_viewer_added = lambda e: self.chat.notify_viewer(e["user"], e["value"])
        self.twitch = TwitchChatReader(on_message=self._twitch_from_thread,
                                       on_status=lambda: self.broker.publish_threadsafe("twitch", self.twitch.status()))
        self._announced: tuple[bool, int | None] = (self.roulette.running, self.roulette.current_uid())
        self.ingame = IngameOverlays(self.settings.ingame, self.ingame_state, self.ingame_colors)
        self.ingame.on_change = lambda: self.broker.publish_threadsafe("ingame", self.ingame_info())
        self.queue = ViewerQueue(os.path.join(config.data_dir(), "viewer_queue.json"), publish=self.broker.publish)
        self.queue.context = self.queue_context
        self.queue.allowed = self.queue_refusal
        self.donations = DonationService(self._typed("donations", DonationSettings), os.path.join(config.data_dir(), "donations.json"),
                                         publish=self.broker.publish, call_threadsafe=self.broker.call_threadsafe)
        self.obs = ObsService(self._typed("obs", ObsSettings), app_port=lambda: self.settings.port,
                              publish_threadsafe=self.broker.publish_threadsafe)
        # the streamer's Twitch account: Hardmode's own rewards with refunds, optionally chat as the streamer
        self.twitch_api = TwitchService(os.path.join(config.data_dir(), "twitch.json"),
                                        publish_threadsafe=self.broker.publish_threadsafe,
                                        call_threadsafe=self.broker.call_threadsafe)
        self.predictions = PredictionService(self._typed("predictions", PredictionSettings), self.twitch_api,
                                             os.path.join(config.data_dir(), "predictions.json"),
                                             publish_threadsafe=self.broker.publish_threadsafe)
        self.twitch_api.on_redemption = self._on_twitch_redemption
        self.twitch_api.on_login = lambda user: self.broker.call_threadsafe(self._on_twitch_login, user)
        self.twitch_api.on_stream = self._on_stream
        self.queue.on_close = self._on_request_closed
        self.chat.twitch_send = self.twitch_api.send_chat
        self.chat.twitch_ready = lambda: self.twitch_api.can_chat
        self.donations.add_wheel = lambda v, u: self.dpi.add_viewer_value(v, u, source="donation", per_user_limit=False)
        self.donations.queue_weapon = lambda w, text, user, amount, cur, by_side=None, source="donation", redemption=None: \
            self.queue.add(w, text, user, amount, cur, source=source, by_side=by_side, redemption=redemption)
        self.donations.current_side = lambda: self.gsi.status.team or self.roulette.side
        self.donations.in_loadout = self.loadout_has
        self.donations.chat_result = lambda user, text: self.chat.notify_donation(user, text, ok=True)
        self.donations.chat_reject = lambda user, text: self.chat.notify_donation(user, text, ok=False)
        self._save_lock = threading.Lock()
        # update from the panel: git pull + run.bat in the app folder (a git checkout)
        self.updater = Updater(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), VERSION,
                               publish_threadsafe=self.broker.publish_threadsafe)
        # run-gsi-record.bat: real payloads for the developer, never switched on from the panel
        self.gsi_recorder = (GsiRecorder(os.path.join(config.data_dir(), "gsi-record.jsonl"))
                             if os.environ.get("CS2R_GSI_RECORD") == "1" else None)
        self._last_gsi_publish = 0.0
        self._last_gsi_key: tuple = ()
        self._last_gsi_error_log = 0.0
        self.roulette.bind(self._on_roulette_change)
        self.broker.on_clients_changed = lambda: self.broker.publish("clients", self.broker.clients())

    # --------------------------------------------------------------- helpers
    def save_state(self) -> None:
        with self._save_lock:
            try:
                self.roulette.save(config.state_path())
            except OSError:
                log.warning("cannot save state.json", exc_info=True)

    def _on_roulette_change(self) -> None:
        self.save_state()
        self._announce_weapon()
        self.broker.publish("roulette", self.roulette.snapshot())
        self.queue.refresh()          # a new loadout or side reorders the viewer queue

    def queue_context(self) -> dict | None:
        """What the streamer can play right now, for the viewer queue order: the loadout of the side being played
        (knife and Zeus when they are in the race); without a loadout, what that side can buy at all."""
        side = self.donations.current_side()
        if side not in L.SIDES:
            return None
        other = W.CT if side == W.T else W.T
        lo, rs = self.roulette.loadout, self.roulette.settings
        if lo:
            def ids(s: str) -> set[str]:
                return set(L.side_pool_ids(lo, s, include_starting=True, include_knife=rs.include_knife,
                                           include_zeus=rs.include_zeus))
            now, later = ids(side), ids(other)
        else:
            now = {w.id for w in W.CATALOG if w.usable_by(side)}
            later = {w.id for w in W.CATALOG if w.usable_by(other)}
        return {"side": side, "other": other, "now": now, "later": later, "loadout": bool(lo)}

    def loadout_has(self, weapon: str) -> bool:
        """«Только оружие из лоадаута»: the weapon (or its side twin, G3SG1/SCAR-20) is in the loadout of either
        side, knife and Zeus when they are in the race. Without a loadout there is nothing to compare: allowed."""
        lo, rs = self.roulette.loadout, self.roulette.settings
        if not lo:
            return True
        ids: set[str] = set()
        for side in L.SIDES:
            ids |= set(L.side_pool_ids(lo, side, include_starting=True, include_knife=rs.include_knife,
                                       include_zeus=rs.include_zeus))
        return weapon in ids or SIDE_TWINS.get(weapon) in ids

    def queue_refusal(self, weapon: str) -> str | None:
        """Knife and Zeus go into the viewer queue only while they take part in the race (user's rule)."""
        rs = self.roulette.settings
        if (weapon == "knife" and not rs.include_knife) or (weapon == "taser" and not rs.include_zeus):
            return f"{W.get(weapon).name} сейчас не принимается"
        return None

    def _announce_weapon(self) -> None:
        """Chat notification when the current weapon changes (start, kill, button, reroll, side switch)."""
        r = self.roulette
        now = (r.running, r.current_uid())
        was = self._announced
        self._announced = now
        if not r.running or now == was or r.current() is None:
            return
        name = W.get(r.current()).name
        fresh_start = not was[0] and r.completed == 0 and not r.history
        if fresh_start:
            self.chat.notify_start(name, side=r.side)
        else:
            self.chat.notify_weapon(name, r.completed, side=r.side, kills=r.kills_total)

    # ------------------------------------------------------------ in-game overlay
    def ingame_state(self) -> dict:
        """Compact state for the in-game overlay thread (also used by the panel preview)."""
        d, r = self.dpi, self.roulette
        spin = d.last_spin or {}
        now = time.time()
        spinning = bool(spin) and spin.get("applied") is None and \
            now < float(spin.get("started_at", 0)) + float(spin.get("duration_ms", 0)) / 1000.0 + 0.5
        cur = d.current_dpi
        dpi = {"present": bool(d.settings.enabled and (cur is not None or spinning)), "current": cur,
               "spinning": spinning, "home": d.settings.home_dpi, "key": [cur, spinning], "hold": spinning,
               "reason": "DPI-рулетка выключена" if not d.settings.enabled else ("DPI ещё не применялся" if cur is None else "")}
        wid = r.current() if r.running else None
        n = int(self.settings.ingame.weapons_next)
        nxt = list(r.queue)[1:1 + n] if wid else []
        weapons = {"present": bool(wid),
                   "reason": "" if wid else ("гонка на паузе" if r.paused else "гонка не запущена"),
                   "current": {"id": wid, "name": W.get(wid).name} if wid else None,
                   "next": [{"id": i, "name": W.get(i).name} for i in nxt],
                   "kills": [r.current_kills, r.settings.kills_per_weapon],
                   "matched": bool(wid and self.gsi.status.held == wid),
                   "completed": r.completed, "key": [r.current_uid(), r.completed]}
        pend = self.queue.snapshot()["pending"]
        queue = {"present": bool(pend), "reason": "" if pend else "очередь пуста", "total": len(pend),
                 "items": [{"weapon": e.get("weapon"), "name": e.get("name"), "user": e.get("user"),
                            "note": e.get("note")} for e in pend[:int(self.settings.ingame.queue_max)]],
                 "key": [[e["id"], e.get("weapon")] for e in pend]}
        return {"dpi": dpi, "weapons": weapons, "queue": queue}

    def ingame_colors(self) -> dict:
        o = self.settings.overlay
        return {"color": o.color, "accent": o.accent, "match_color": o.match_color,
                "home_color": self.dpi.settings.home_color}

    def ingame_info(self) -> dict:
        return {**self.settings.ingame.__dict__, **self.ingame.status()}

    def persist_settings(self) -> None:
        """Save module settings changed outside /api/settings (DPI values and viewer values from the DPI tab)."""
        for section, attr, _ in self.MODULE_SETTINGS:
            setattr(self.settings, section, getattr(self, attr).settings.to_dict())
        try:
            self.settings.save()
        except OSError:
            log.warning("cannot save settings.json", exc_info=True)

    # ------------------------------------------------------------ twitch chat
    def sync_twitch(self) -> None:
        """Start/stop the anonymous chat reader. Only chat commands need it now: the DPI command (``!dpi``) and
        the queue command when somebody can answer it. Channel points come from the Twitch API (0.24.0)."""
        channel = (self.twitch_api.state["user"].get("login") or "") if self.twitch_api.logged_in else ""
        s = self.dpi.settings
        dpi_cmd = bool(s.viewer_enabled and s.viewer_command)
        queue_cmd = bool(self.donations.settings.queue_command and self.chat.can_send())
        if channel and (dpi_cmd or queue_cmd):
            self.twitch.start(channel)
        else:
            self.twitch.stop()

    def _twitch_from_thread(self, msg: IrcMessage) -> None:
        self.broker.call_threadsafe(self.handle_chat_message, msg)

    def handle_chat_message(self, msg: IrcMessage) -> None:
        if msg.reward_id:
            return       # channel points are read from the Twitch API (Hardmode's own rewards), never from chat
        if self._queue_command(msg):
            return
        s = self.dpi.settings
        if not s.viewer_enabled:
            return
        text = (msg.trailing or "").strip()
        user = msg.display_name or msg.user
        cmd = s.viewer_command.lower()
        if cmd and text.lower().startswith(cmd) and (len(text) == len(cmd) or text[len(cmd)] == " "):
            res = self.dpi.add_viewer_value(text[len(cmd):].strip(), user, source="command")
            if not res["ok"]:
                self._viewer_rejected(user, text, res["error"])

    def _queue_command(self, msg: IrcMessage) -> bool:
        """``!очередь``: the bot tells the viewer where their requests are. Returns True if handled."""
        cmd = self.donations.settings.queue_command
        text = (msg.trailing or "").strip().lower()
        if not cmd or not (text == cmd or text.startswith(cmd + " ")):
            return False
        user = msg.display_name or msg.user
        self.chat.notify_queue_reply(user, self.queue.describe_for(user, msg.user))
        return True

    def _viewer_rejected(self, user: str, text: str, error: str, refunded: bool = False) -> None:
        self.dpi._log("viewer", f"{user}: «{text[:30]}» не принято ({error})" + (", баллы возвращены" if refunded else ""))
        self.dpi._changed()
        s = self.dpi.settings
        reason = {"не число": f"нужно число от {s.viewer_min} до {s.viewer_max}"}.get(error, error)
        self.chat.notify_reject(user, reason + (". Баллы возвращены" if refunded else ""))

    # ------------------------------------------------------------ twitch account (own rewards)
    def _on_twitch_redemption(self, kind: str, r: dict) -> str:
        """A redemption of a reward made by Hardmode: "fulfill" | "cancel" (refund) | "keep" (waits in the queue)."""
        user, text = r.get("user") or "?", (r.get("text") or "").strip()
        if kind == "weapon":
            dn = self.donations
            if not dn.settings.reward_enabled:
                self.chat.notify_reject(user, "оружие за баллы сейчас выключено. Баллы возвращены")
                return "cancel"
            res = dn.reward_request(text, user, redemption={"reward_id": r["reward_id"], "id": r["id"]})
            if res["ok"]:
                self.chat.notify_request(user, res.get("weapon") or "оружие", res.get("position") or "?", res["text"])
                return "keep"
            self.chat.notify_reject(user, res["text"] + ". Баллы возвращены")
            return "cancel"
        if kind == "dpi":
            if not self.dpi.settings.viewer_enabled:
                self.chat.notify_reject(user, "DPI за баллы сейчас выключены. Баллы возвращены")
                return "cancel"
            res = self.dpi.add_viewer_value(text, user, source="reward")
            if res["ok"]:
                return "fulfill"
            self._viewer_rejected(user, text, res["error"], refunded=True)
            return "cancel"
        return "cancel"

    def _on_request_closed(self, e: dict) -> None:
        """A viewer request paid with Hardmode's reward was closed: ✓/kill fulfils it, × refunds the points."""
        red = e.get("redemption")
        if red:
            self.twitch_api.set_status(red, "FULFILLED" if e.get("status") == "done" else "CANCELED")

    def on_reward_created(self, kind: str) -> None:
        """A reward was just created in Twitch: switch its feature on (the streamer clearly wants it)."""
        if kind == "weapon" and not self.donations.settings.reward_enabled:
            self.donations.update_settings({"reward_enabled": True})
        if kind == "dpi" and not self.dpi.settings.viewer_enabled:
            self.dpi.update_settings({"viewer_enabled": True})
        self.persist_settings()
        self.sync_twitch()

    def _on_stream(self, info: dict) -> None:
        """The broadcast started or ended (Twitch, 0.37.0): play outside a stream and the stream itself are
        counted apart. Event-loop thread."""
        sess = self.stats.session
        if info.get("live"):
            if sess["kind"] == "stream" and sess.get("stream_id") == info.get("id"):
                return                                   # the same broadcast (app restarted during it)
            self.stats.start_session("stream", stream=info)
        elif sess["kind"] == "stream":
            self.stats.start_session("offstream")
        else:
            return
        self.broker.publish("stats", self.stats.snapshot())
        self.broker.publish("stats_history", {"changed": True})

    def _on_twitch_login(self, user: dict) -> None:
        """Signed in: the chat commands can be read from the streamer's channel now."""
        self.sync_twitch()
        self.broker.publish("chat", self.chat.status())

    def urls(self) -> dict:
        base = f"http://127.0.0.1:{self.settings.port}"
        return {
            "panel": base + "/",
            "overlay_weapons": base + "/overlay/weapons",
            "overlay_loadout": base + "/overlay/loadout",
            "overlay_dpi": base + "/overlay/dpi",
            "overlay_requests": base + "/overlay/requests",
            "gsi": base + "/gsi",
        }

    def cfg_text(self) -> str:
        return build_cfg(self.urls()["gsi"], self.settings.gsi_token)

    def full_snapshot(self) -> dict:
        cfg = steam.installed_cfg_status(self.cfg_text())
        return {
            "version": VERSION,
            "roulette": self.roulette.snapshot(),
            "dpi": self.dpi.snapshot(),
            "chat": self.chat.status(),
            "twitch": self.twitch.status(),
            "clients": self.broker.clients(),
            "gsi": self.gsi.status.to_dict(),
            "overlay": self.settings.overlay.__dict__,
            "ingame": self.ingame_info(),
            "stats": self.stats.snapshot(),
            "queue": self.queue.snapshot(),
            "donations": self.donations.status(),
            "obs": self.obs.status(),
            "twitch_account": self.twitch_api.status(),
            "predictions": self.predictions.status(),
            "gsi_record": bool(self.gsi_recorder and self.gsi_recorder.enabled),
            "update": self.updater.status(),
            "urls": self.urls(),
            "gsi_token": self.settings.gsi_token,
            "port": self.settings.port,
            "data_dir": config.data_dir(),
            "log_path": os.path.join(config.data_dir(), "app.log"),
            "mode": "tray" if os.environ.get("CS2R_MODE") == "tray" else "console",
            "autostart": {"available": autostart.available(), "enabled": autostart.is_enabled()},
            "cs2_cfg_dir": steam.cs2_cfg_dir(),
            "cfg_installed": cfg["path"],
            "cfg_state": cfg["state"],
        }

    def _typed(self, section: str, cls, **kw):
        """A module's typed settings from settings.json. Invalid ones fall back to defaults (and say so in the log:
        the streamer's values are gone), and AppSettings keeps the normalised dict copy."""
        try:
            typed = cls.from_dict(getattr(self.settings, section), **kw)
        except ValueError as e:
            log.warning("settings.%s are invalid (%s): defaults are used", section, e)
            typed = cls()
        setattr(self.settings, section, typed.to_dict())
        return typed

    # sections validated by their own module: (settings section, AppState attribute, label in errors);
    # the module keeps the typed settings, AppSettings the dict copy written to settings.json
    MODULE_SETTINGS = (("roulette", "roulette", "рулетки"), ("dpi", "dpi", "DPI"), ("chat", "chat", "чата"),
                       ("donations", "donations", "донатов"), ("obs", "obs", "OBS"), ("predictions", "predictions", "прогнозов"))

    def module_settings(self) -> dict:
        """What the panel may see: secrets (DonationAlerts secret, DonatePay key, OBS password) only as «*_set»."""
        out = {}
        for section, attr, _ in self.MODULE_SETTINGS:
            st = getattr(self, attr).settings
            out[section] = st.public_dict() if hasattr(st, "public_dict") else st.to_dict()
        return out

    def settings_event(self) -> dict:
        return {"overlay": self.settings.overlay.__dict__, "ingame": self.ingame_info(), **self.module_settings(),
                "gsi_token": self.settings.gsi_token, "port": self.settings.port}

    def publish_gsi(self, force: bool = False) -> None:
        st = self.gsi.status
        # what the pages show; HP, the round phase and the rejected-packet counter changed many times a second in a
        # fight and re-sent «gsi» for nothing (0.38.0) — the 2 s re-publish still refreshes the counters and ages
        key = (st.connected, st.own_player, st.team, st.held, st.map_phase, st.round,
               st.activity, st.round_kills, st.match_kills, bool(st.rejected_auth), st.name, st.rejected_name)
        now = time.monotonic()
        if force or key != self._last_gsi_key or now - self._last_gsi_publish > 2.0:
            self._last_gsi_key = key
            self._last_gsi_publish = now
            self.broker.publish("gsi", st.to_dict())

    def handle_gsi(self, payload: dict) -> None:
        if self.gsi_recorder:
            self.gsi_recorder.record(payload)
        events = self.gsi.handle(payload)
        st = self.gsi.status
        if st.own_player and st.team in (W.T, W.CT):
            # idempotent; no-op when side_source == "manual" or the side already matches
            self.roulette.set_side(st.team, source="gsi")
        batch_advanced = False
        # statistics first: a death must be booked at the DPI that was current before the new spin
        if self.stats.handle_events(events, st, self.dpi.current_dpi):
            self.broker.publish("stats", self.stats.snapshot())
        for ev in events:
            if ev["type"] == "match_start":
                self.predictions.on_match_start(ev["map"], ev["mode"])
            elif ev["type"] == "match_end":
                self.predictions.on_match_end(ev["result"])
        if any(ev["type"] == "kill" for ev in events):
            self.dpi.on_kill()
        for ev in events:
            if ev["type"] == "death":
                self.dpi.on_death()
                continue
            if ev["type"] != "kill":
                continue
            closed = self.queue.on_kill(ev["weapon"])     # a kill with a requested weapon fulfils the request
            if closed:
                self.chat.notify_request_done(closed["user"], closed["name"])
            if batch_advanced and ev["index"] > 0:
                # a double/collateral in one payload: the extra kill belongs to the weapon that
                # just finished, it must not be judged against the next one
                self.roulette._log("kill", "Дополнительный килл в том же пакете (коллатерал) — не считается")
                self.roulette._changed()
                continue
            if self.register_kill(ev["weapon"], ev["headshot"], bool(ev["ambiguous"]), ev["raw"])["advanced"]:
                batch_advanced = True
        self.queue.refresh()      # the team from the game may change without the race side (manual side mode)
        self.publish_gsi()

    def register_kill(self, weapon: str | None, headshot: bool, ambiguous: bool, raw: str | None = None) -> dict:
        """Book a kill in the roulette and tell the overlays (GSI path and the fake-kill API)."""
        result = self.roulette.on_kill(weapon, headshot=headshot, ambiguous=ambiguous, raw_name=raw)
        self.broker.publish("kill", {**result, "headshot": headshot, "raw": raw, "t": time.time()})
        return result
