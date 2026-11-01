"""FastAPI application: control panel, OBS overlays, SSE stream, GSI receiver.

All state lives in one ``AppState`` and is only touched from the event-loop thread:
every endpoint is ``async`` (they are all sub-millisecond); background threads (chat,
Twitch reader, DPI guard, in-game overlay) marshal through ``Broker.call_threadsafe`` /
``publish_threadsafe``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import autostart, config, docs, loadout as L, steam, weapons as W
from .events import Broker
from .gsi import GsiTracker, build_cfg
from .ingame import IngameOverlays
from .chat import ChatNotifier, ChatSettings
from .modules import dpi
from .modules.dpi import DpiService, DpiSettings
from .roulette import Roulette
from .stats import StatsTracker
from .twitch_irc import IrcMessage, TwitchChatReader
from .web import json_body as _json, page

from .version import VERSION

# Callables that stop this instance (set by the tray). Without them /api/quit raises SIGINT, which
# uvicorn in console mode turns into a graceful shutdown.
QUIT_HOOK: list = []
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
log = logging.getLogger("hardmode")


def _say(text: str) -> None:
    """Console print that survives odd Windows code pages / redirected stdout."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


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
        try:
            dpi_settings = DpiSettings.from_dict(self.settings.dpi)
        except ValueError:
            dpi_settings = DpiSettings()
        self.settings.dpi = dpi_settings.to_dict()
        self.dpi = DpiService(dpi_settings, config.dpi_state_path(),
                              publish=self.broker.publish, publish_threadsafe=self.broker.publish_threadsafe)
        try:
            chat_settings = ChatSettings.from_dict(self.settings.chat)
        except ValueError:
            chat_settings = ChatSettings()
        self.settings.chat = chat_settings.to_dict()
        self.chat = ChatNotifier(chat_settings, publish_threadsafe=self.broker.publish_threadsafe)
        self.stats = StatsTracker(os.path.join(config.data_dir(), "stats.json"))
        self.dpi.on_applied = lambda value, reason: (self.chat.notify_dpi(value, reason), self.stats.on_dpi_applied(value))
        self.dpi.on_spin = lambda value, reason: (self.stats.on_dpi_roll(value, reason),
                                                  self.broker.publish("stats", self.stats.snapshot()))
        self.dpi.on_viewer_added = lambda e: (self.chat.notify_viewer(e["user"], e["value"])
                                              if self.dpi.settings.viewer_announce else None)
        self.twitch = TwitchChatReader(on_message=self._twitch_from_thread,
                                       on_status=lambda: self.broker.publish_threadsafe("twitch", self.twitch.status()))
        self._announced: tuple[bool, int | None] = (self.roulette.running, self.roulette.current_uid())
        self.ingame = IngameOverlays(self.settings.ingame, self.ingame_state, self.ingame_colors)
        self._save_lock = threading.Lock()
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
        return {"dpi": dpi, "weapons": weapons}

    def ingame_colors(self) -> dict:
        o = self.settings.overlay
        return {"color": o.color, "accent": o.accent, "match_color": o.match_color,
                "home_color": self.dpi.settings.home_color}

    def ingame_info(self) -> dict:
        return {**self.settings.ingame.__dict__, **self.ingame.status()}

    def persist_dpi_settings(self) -> None:
        self.settings.dpi = self.dpi.settings.to_dict()
        self.settings.chat = self.chat.settings.to_dict()
        try:
            self.settings.save()
        except OSError:
            log.warning("cannot save settings.json", exc_info=True)

    # ------------------------------------------------------------ twitch chat
    def sync_twitch(self) -> None:
        """Start/stop the anonymous chat reader according to the settings."""
        channel = self.chat.settings.channel_login
        if self.dpi.settings.viewer_enabled and channel:
            self.twitch.start(channel)
        else:
            self.twitch.stop()

    def _twitch_from_thread(self, msg: IrcMessage) -> None:
        self.broker.call_threadsafe(self.handle_chat_message, msg)

    def handle_chat_message(self, msg: IrcMessage) -> None:
        s = self.dpi.settings
        if not s.viewer_enabled:
            return
        text = (msg.trailing or "").strip()
        user = msg.display_name or msg.user
        if msg.reward_id:
            if self.dpi.learn_reward:
                self.dpi.learn_reward = False
                self.dpi.update_settings({"viewer_reward_id": msg.reward_id})
                self.persist_dpi_settings()
                self.dpi._log("viewer", f"Награда определена по сообщению {user}: {msg.reward_id[:8]}…")
            elif s.viewer_reward_id and msg.reward_id != s.viewer_reward_id:
                return
            res = self.dpi.add_viewer_value(text, user, source="reward")
            if not res["ok"]:
                self._viewer_rejected(user, text, res["error"])
            return
        cmd = s.viewer_command.lower()
        if cmd and text.lower().startswith(cmd) and (len(text) == len(cmd) or text[len(cmd)] == " "):
            res = self.dpi.add_viewer_value(text[len(cmd):].strip(), user, source="command")
            if not res["ok"]:
                self._viewer_rejected(user, text, res["error"])

    def _viewer_rejected(self, user: str, text: str, error: str) -> None:
        self.dpi._log("viewer", f"{user}: «{text[:30]}» не принято ({error})")
        self.dpi._changed()
        if self.dpi.settings.viewer_announce:
            s = self.dpi.settings
            reason = {"не число": f"нужно число от {s.viewer_min} до {s.viewer_max}"}.get(error, error)
            self.chat.notify_reject(user, reason)

    def urls(self) -> dict:
        base = f"http://127.0.0.1:{self.settings.port}"
        return {
            "panel": base + "/",
            "overlay_weapons": base + "/overlay/weapons",
            "overlay_loadout": base + "/overlay/loadout",
            "overlay_dpi": base + "/overlay/dpi",
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

    def settings_event(self) -> dict:
        return {"overlay": self.settings.overlay.__dict__,
                "ingame": self.ingame_info(),
                "roulette": self.roulette.settings.to_dict(), "dpi": self.dpi.settings.to_dict(),
                "chat": self.chat.settings.public_dict(),
                "gsi_token": self.settings.gsi_token, "port": self.settings.port}

    def publish_gsi(self, force: bool = False) -> None:
        st = self.gsi.status
        key = (st.connected, st.own_player, st.team, st.held, st.map_phase, st.round_phase, st.round,
               st.activity, st.health, st.round_kills, st.match_kills, st.rejected_auth, st.name, st.rejected_name)
        now = time.monotonic()
        if force or key != self._last_gsi_key or now - self._last_gsi_publish > 2.0:
            self._last_gsi_key = key
            self._last_gsi_publish = now
            self.broker.publish("gsi", st.to_dict())

    def handle_gsi(self, payload: dict) -> None:
        events = self.gsi.handle(payload)
        st = self.gsi.status
        if st.own_player and st.team in (W.T, W.CT):
            # idempotent; no-op when side_source == "manual" or the side already matches
            self.roulette.set_side(st.team, source="gsi")
        batch_advanced = False
        # statistics first: a death must be booked at the DPI that was current before the new spin
        if self.stats.handle_events(events, st, self.dpi.current_dpi):
            self.broker.publish("stats", self.stats.snapshot())
        if any(ev["type"] == "kill" for ev in events):
            self.dpi.on_kill()
        for ev in events:
            if ev["type"] == "death":
                self.dpi.on_death()
                continue
            if ev["type"] != "kill":
                continue
            if batch_advanced and ev["index"] > 0:
                # a double/collateral in one payload: the extra kill belongs to the weapon that
                # just finished, it must not be judged against the next one
                self.roulette._log("kill", "Дополнительный килл в том же пакете (коллатерал) — не считается")
                self.roulette._changed()
                continue
            if self.register_kill(ev["weapon"], ev["headshot"], bool(ev["ambiguous"]), ev["raw"])["advanced"]:
                batch_advanced = True
        self.publish_gsi()

    def register_kill(self, weapon: str | None, headshot: bool, ambiguous: bool, raw: str | None = None) -> dict:
        """Book a kill in the roulette and tell the overlays (GSI path and the fake-kill API)."""
        result = self.roulette.on_kill(weapon, headshot=headshot, ambiguous=ambiguous, raw_name=raw)
        self.broker.publish("kill", {**result, "headshot": headshot, "raw": raw, "t": time.time()})
        return result


def _loopback_origin(origin: str) -> bool:
    try:
        host = urlsplit(origin).hostname or ""
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


class SameOriginOnly:
    """Everything is local: refuse requests that browsers send on behalf of other sites.

    * a cross-site page (Origin header not loopback, or ``null``) cannot read or change state;
    * DNS-rebinding (Host header that is not loopback) is refused as well.
    CS2's GSI client, curl and the panel/overlays (same origin) send no Origin or a loopback one.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            origin = headers.get("origin")
            host = (headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
            reason = None
            if origin is not None and not _loopback_origin(origin):
                reason = "cross-site requests are not allowed"
            elif host and host not in LOOPBACK_HOSTS and host != "testserver":
                reason = "unexpected Host header"
            if reason:
                response = JSONResponse({"detail": reason}, status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    state = AppState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        state.broker.attach_loop(loop)
        state.dpi.start(loop)
        state.ingame.start()
        state.chat.start()
        state.sync_twitch()
        stop = asyncio.Event()

        async def housekeeping() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                if state.gsi.tick():
                    state.publish_gsi(force=True)

        task = asyncio.create_task(housekeeping())
        if state.settings.open_browser and os.environ.get("CS2R_NO_BROWSER") != "1":
            loop.call_later(0.7, lambda: webbrowser.open(state.urls()["panel"]))
        _say(f"[hardmode] панель:   {state.urls()['panel']}")
        _say(f"[hardmode] виджет:   {state.urls()['overlay_weapons']}")
        _say(f"[hardmode] лоадаут:  {state.urls()['overlay_loadout']}")
        _say(f"[hardmode] DPI:      {state.urls()['overlay_dpi']}  (backend: {state.dpi.backend.name})")
        _say(f"[hardmode] данные:   {config.data_dir()}")
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            state.dpi.stop()
            state.ingame.stop()
            state.chat.stop()
            state.twitch.stop()
            state.stats.flush()
            state.save_state()

    app = FastAPI(title="Hardmode", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.app = state

    # Pure ASGI middleware (not BaseHTTPMiddleware: that one buffers/wraps streaming bodies and
    # keeps the infinite SSE stream alive after the client is gone).
    app.add_middleware(SameOriginOnly)

    app.mount("/static", StaticFiles(directory=os.path.join(config.package_dir(), "static")), name="static")
    app.include_router(dpi.router)

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def panel() -> HTMLResponse:
        return page("panel.html")

    @app.get("/overlay/weapons", response_class=HTMLResponse)
    async def overlay_weapons() -> HTMLResponse:
        return page("overlay_weapons.html")

    @app.get("/overlay/loadout", response_class=HTMLResponse)
    async def overlay_loadout() -> HTMLResponse:
        return page("overlay_loadout.html")

    # -------------------------------------------------------------------- SSE
    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        # Who is listening: which page (from the Referer) and whether it is an OBS browser source
        # (obs-browser appends "OBS/x.y.z" to its user agent). Used by the Startup checklist.
        ua = request.headers.get("user-agent", "")
        ref = urlsplit(request.headers.get("referer", "")).path
        page = "panel" if ref in ("", "/") else ref.rsplit("/", 1)[-1] or "other"
        meta = {"page": page, "obs": "OBS/" in ua}
        gen = state.broker.stream(state.full_snapshot, meta=meta)
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -------------------------------------------------------------------- API
    @app.get("/api/state")
    async def api_state() -> dict:
        return state.full_snapshot()

    @app.get("/api/docs")
    async def api_docs() -> dict:
        return docs.load_help()

    @app.get("/api/weapons")
    async def api_weapons() -> dict:
        return {"weapons": W.all_as_dicts(), "slots": W.SLOT_COUNTS, "titles": W.CATEGORY_TITLES_RU}

    @app.post("/api/loadout/generate")
    async def api_generate(request: Request) -> dict:
        body = await _json(request)
        sides = body.get("sides") or [W.T, W.CT]
        if not isinstance(sides, list):
            raise HTTPException(400, "sides must be a list")
        sides = tuple(s for s in sides if s in (W.T, W.CT))
        if not sides:
            raise HTTPException(400, "sides must contain T and/or CT")
        try:
            state.roulette.generate_loadout(sides)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"loadout": state.roulette.loadout}

    @app.post("/api/loadout/replace")
    async def api_replace(request: Request) -> dict:
        body = await _json(request)
        try:
            state.roulette.replace_weapon(str(body.get("side")), str(body.get("category")), str(body.get("weapon")))
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))
        return {"loadout": state.roulette.loadout}

    @app.post("/api/loadout/slot")
    async def api_loadout_slot(request: Request) -> dict:
        body = await _json(request)
        try:
            state.roulette.set_slot(str(body.get("side")), str(body.get("category")), str(body.get("old")),
                                    str(body.get("new")))
        except (ValueError, KeyError) as e:
            raise HTTPException(400, str(e))
        return {"loadout": state.roulette.loadout}

    @app.post("/api/loadout/view")
    async def api_loadout_view(request: Request) -> dict:
        body = await _json(request)
        try:
            state.roulette.set_view_side(str(body.get("side")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return state.roulette.snapshot()

    @app.get("/api/loadout/text", response_class=PlainTextResponse)
    async def api_loadout_text(side: str | None = None) -> str:
        if not state.roulette.loadout:
            return ""
        return L.as_text(state.roulette.loadout, side if side in (W.T, W.CT) else None)

    @app.post("/api/session/start")
    async def api_start() -> dict:
        try:
            state.roulette.start()
        except ValueError as e:
            raise HTTPException(400, str(e))
        return state.roulette.snapshot()

    @app.post("/api/session/stop")
    async def api_stop() -> dict:
        state.roulette.stop()
        return state.roulette.snapshot()

    @app.post("/api/session/reset")
    async def api_reset() -> dict:
        state.roulette.reset()
        return state.roulette.snapshot()

    @app.post("/api/roulette/next")
    async def api_next() -> dict:
        if not state.roulette.running:
            raise HTTPException(400, "Сессия не запущена")
        state.roulette.advance(reason="manual")
        return state.roulette.snapshot()

    @app.post("/api/roulette/reroll")
    async def api_reroll() -> dict:
        if not state.roulette.running:
            raise HTTPException(400, "Сессия не запущена")
        state.roulette.reroll_current()
        return state.roulette.snapshot()

    @app.post("/api/roulette/undo")
    async def api_undo() -> dict:
        state.roulette.undo()
        return state.roulette.snapshot()

    @app.post("/api/roulette/kill")
    async def api_fake_kill(request: Request) -> dict:
        """Simulate a kill (testing without CS2)."""
        body = await _json(request)
        weapon = body.get("weapon") or state.roulette.current()
        if weapon is not None and (not isinstance(weapon, str) or weapon not in W.BY_ID):
            raise HTTPException(400, "unknown weapon")
        return state.register_kill(weapon, bool(body.get("headshot")), bool(body.get("ambiguous")))

    @app.post("/api/side")
    async def api_side(request: Request) -> dict:
        body = await _json(request)
        side = body.get("side")
        if side not in (W.T, W.CT):
            raise HTTPException(400, "side must be T or CT")
        state.roulette.set_side(side, source="manual")
        return state.roulette.snapshot()

    @app.get("/api/settings")
    async def api_get_settings() -> dict:
        d = state.settings.to_dict()
        d["roulette"] = state.roulette.settings.to_dict()
        d["dpi"] = state.dpi.settings.to_dict()
        d["chat"] = state.chat.settings.public_dict()   # never expose the JWT
        return d

    @app.post("/api/settings")
    async def api_set_settings(request: Request) -> dict:
        body = await _json(request)
        s = state.settings
        rejected: list[str] = []
        for section in ("roulette", "overlay", "dpi", "chat", "ingame"):
            if section in body and body[section] is not None and not isinstance(body[section], dict):
                raise HTTPException(400, f"{section}: нужен объект")
        # sections validated by their own module: the module keeps the typed settings, AppSettings the dict copy
        for section, owner, label in (("chat", state.chat, "чата"), ("dpi", state.dpi, "DPI"), ("roulette", state.roulette, "рулетки")):
            if section in body:
                try:
                    owner.update_settings(body[section] or {})
                except ValueError as e:
                    raise HTTPException(400, f"Некорректные настройки {label}: {e}")
                setattr(s, section, owner.settings.to_dict())
        if "dpi" in body or "chat" in body:
            state.sync_twitch()
        if "overlay" in body:
            rejected += [f"overlay.{k}" for k in s.overlay.apply(body["overlay"] or {})]
        if "ingame" in body:
            rejected += [f"ingame.{k}" for k in s.ingame.apply(body["ingame"] or {})]
            state.ingame.update_settings(s.ingame)
        if body.get("regenerate_token"):
            s.gsi_token = secrets.token_urlsafe(12)
            state.gsi.token = s.gsi_token
        if "port" in body:
            try:
                port = int(body["port"])
                if 1024 <= port <= 65535:
                    s.port = port
                else:
                    rejected.append("port")
            except (TypeError, ValueError):
                rejected.append("port")
        if "open_browser" in body:
            s.open_browser = bool(body["open_browser"])
        s.save()
        state.broker.publish("settings", state.settings_event())
        out = await api_get_settings()
        out["rejected"] = rejected
        return out

    # -------------------------------------------------------------------- GSI
    @app.post("/gsi")
    async def gsi_endpoint(request: Request) -> JSONResponse:
        """Always answers 2xx quickly: the game only computes deltas after a 2xx."""
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"ok": False}, status_code=200)
        if isinstance(payload, dict):
            try:
                state.handle_gsi(payload)
            except Exception:
                now = time.monotonic()
                if now - state._last_gsi_error_log > 5.0:
                    state._last_gsi_error_log = now
                    log.exception("GSI payload handling failed")
                return JSONResponse({"ok": False})
        return JSONResponse({"ok": True})

    @app.get("/api/gsi/cfg", response_class=PlainTextResponse)
    async def gsi_cfg() -> PlainTextResponse:
        return PlainTextResponse(state.cfg_text(), headers={
            "Content-Disposition": f'attachment; filename="{steam.CFG_NAME}"'})


    @app.post("/api/gsi/install")
    async def gsi_install(request: Request) -> dict:
        body = await _json(request)
        cfg_dir = body.get("cfg_dir") or None
        if cfg_dir is not None and not isinstance(cfg_dir, str):
            raise HTTPException(400, "cfg_dir must be a string")
        try:
            path = steam.install_cfg(state.cfg_text(), cfg_dir)
        except (FileNotFoundError, OSError) as e:
            raise HTTPException(400, str(e))
        state.broker.publish("settings", state.settings_event())
        return {"path": path}

    @app.post("/api/autostart")
    async def api_autostart(request: Request) -> dict:
        body = await _json(request)
        if not autostart.available():
            raise HTTPException(400, "Автозапуск доступен только в Windows")
        try:
            enabled = autostart.set_enabled(bool(body.get("enabled")))
        except OSError as e:
            raise HTTPException(500, f"Не удалось изменить автозапуск: {e}")
        return {"enabled": enabled, "command": autostart.launch_command()}

    # ------------------------------------------------------------------- chat
    @app.post("/api/chat/verify")
    async def chat_verify() -> dict:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, state.chat.verify)
        if res.get("ok"):
            state.persist_dpi_settings()   # channel_login may have been filled from the SE profile
            state.sync_twitch()
            state.broker.publish("settings", state.settings_event())
        return res

    @app.post("/api/twitch/chat")
    async def twitch_fake_chat(request: Request) -> dict:
        """Simulate a chat line (testing without Twitch): {user, text, reward_id?}."""
        body = await _json(request)
        tags = {"display-name": str(body.get("user") or "viewer")}
        if body.get("reward_id"):
            tags["custom-reward-id"] = str(body["reward_id"])
        msg = IrcMessage(tags=tags, prefix=f"{tags['display-name'].lower()}!x@x", command="PRIVMSG",
                         params=["#test"], trailing=str(body.get("text") or ""))
        state.handle_chat_message(msg)
        return {"ok": True}

    @app.get("/api/stats")
    async def api_stats() -> dict:
        return state.stats.snapshot()

    @app.post("/api/stats/reset")
    async def api_stats_reset() -> dict:
        state.stats.reset()
        state.broker.publish("stats", state.stats.snapshot())
        return {"ok": True}

    @app.post("/api/quit")
    async def api_quit() -> dict:
        """Stop this instance: used by the panel and by a newer launcher taking over the port."""
        def do_quit() -> None:
            if QUIT_HOOK:
                for fn in list(QUIT_HOOK):
                    try:
                        fn()
                    except Exception:
                        log.exception("quit hook failed")
            else:
                signal.raise_signal(signal.SIGINT)

        log.info("quit requested via API")
        threading.Timer(0.3, do_quit).start()   # let the response leave first
        return {"ok": True, "version": VERSION}

    @app.get("/api/ingame/render")
    async def ingame_render(kind: str = "dpi") -> Response:
        if kind not in ("dpi", "weapons"):
            raise HTTPException(400, "kind: dpi | weapons")
        loop = asyncio.get_running_loop()
        png = await loop.run_in_executor(None, state.ingame.render_png, kind)
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/ingame/preview")
    async def ingame_preview(request: Request) -> dict:
        body = await _json(request)
        kind = str(body.get("kind") or "dpi")
        if kind not in ("dpi", "weapons"):
            raise HTTPException(400, "kind: dpi | weapons")
        if not state.ingame.available:
            return {"ok": False, "error": "Оверлей поверх игры работает только в Windows"}
        state.ingame.preview(kind, 5.0)
        return {"ok": True}

    @app.post("/api/chat/join")
    async def chat_join() -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, state.chat.join_bot)

    @app.post("/api/chat/test")
    async def chat_test(request: Request) -> dict:
        body = await _json(request)
        text = str(body.get("message") or "✅ hardmode: тестовое сообщение").strip()[:450]
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, state.chat.send_now, text)
        if res.get("ok"):
            res["response"] = state.chat.last_response
            res["bot_problem"] = state.chat.bot_problem()
        return res

    return app




app = create_app()
