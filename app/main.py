"""FastAPI application: control panel, OBS overlays, SSE stream, GSI receiver, hotkeys.

All state lives in one ``AppState`` and is only touched from the event-loop thread:
every endpoint is ``async`` (they are all sub-millisecond), the hotkey thread marshals
through ``Broker.call_threadsafe``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, loadout as L, steam, weapons as W
from .events import Broker
from .gsi import GsiTracker, build_cfg
from .hotkeys import HotkeyManager
from .chat import ChatNotifier, ChatSettings
from .modules import dpi
from .modules.dpi import DpiService, DpiSettings
from .roulette import Roulette
from .twitch_irc import IrcMessage, TwitchChatReader

VERSION = "0.6.2"
KILL_ADVANCE_GRACE = 1.5   # seconds: ignore a manual "next" right after a kill already advanced
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
log = logging.getLogger("cs2roulette")


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
        self.dpi.on_applied = lambda value, reason: self.chat.notify_dpi(value, reason)
        self.dpi.on_viewer_added = lambda e: (self.chat.notify_viewer(e["user"], e["value"])
                                              if self.dpi.settings.viewer_announce else None)
        self.twitch = TwitchChatReader(on_message=self._twitch_from_thread,
                                       on_status=lambda: self.broker.publish_threadsafe("twitch", self.twitch.status()))
        self._announced: tuple[bool, int | None] = (self.roulette.running, self.roulette.current_uid())
        self.hotkeys = HotkeyManager(on_action=self._hotkey_from_thread,
                                     bindings=self.settings.hotkeys.bindings(),
                                     enabled=self.settings.hotkeys.enabled)
        self.hotkeys.on_status = lambda: self.broker.publish_threadsafe("hotkeys", self.hotkeys.describe())
        self._save_lock = threading.Lock()
        self._last_gsi_publish = 0.0
        self._last_gsi_key: tuple = ()
        self._last_kill_advance = 0.0
        self._last_gsi_error_log = 0.0
        self.roulette.bind(self._on_roulette_change)

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
        """Chat notification when the current weapon changes (start, kill, hotkey, reroll, side switch)."""
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

    def _hotkey_from_thread(self, action: str) -> None:
        self.broker.call_threadsafe(self.hotkey_action, action)

    def hotkey_action(self, action: str) -> None:
        r = self.roulette
        ignored = False
        if action == "next":
            if not r.running and r.loadout:
                r.start()  # fresh start, or resume after pause
            elif time.monotonic() - self._last_kill_advance < KILL_ADVANCE_GRACE:
                ignored = True  # the kill already advanced; a reflexive press must not skip a weapon
            else:
                r.advance(reason="hotkey")
        elif action == "reroll":
            r.reroll_current()
        elif action == "undo":
            r.undo()
        elif action == "dpi_spin":
            try:
                self.dpi.spin(reason="hotkey")
            except ValueError:
                ignored = True
        self.broker.publish("hotkey", {"action": action, "ignored": ignored, "t": time.time()})

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
                self.dpi._log("viewer", f"{user}: «{text[:30]}» не принято ({res['error']})")
                self.dpi._changed()
            return
        cmd = s.viewer_command.lower()
        if cmd and text.lower().startswith(cmd) and (len(text) == len(cmd) or text[len(cmd)] == " "):
            res = self.dpi.add_viewer_value(text[len(cmd):].strip(), user, source="command")
            if not res["ok"]:
                self.dpi._log("viewer", f"{user}: «{text[:30]}» не принято ({res['error']})")
                self.dpi._changed()

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
            "platform": sys.platform,
            "roulette": self.roulette.snapshot(),
            "dpi": self.dpi.snapshot(),
            "chat": self.chat.status(),
            "twitch": self.twitch.status(),
            "gsi": self.gsi.status.to_dict(),
            "hotkeys": self.hotkeys.describe(),
            "overlay": self.settings.overlay.__dict__,
            "urls": self.urls(),
            "gsi_token": self.settings.gsi_token,
            "port": self.settings.port,
            "data_dir": config.data_dir(),
            "cs2_cfg_dir": steam.cs2_cfg_dir(),
            "cfg_installed": cfg["path"],
            "cfg_state": cfg["state"],
        }

    def settings_event(self) -> dict:
        return {"overlay": self.settings.overlay.__dict__, "hotkeys": self.hotkeys.describe(),
                "roulette": self.roulette.settings.to_dict(), "dpi": self.dpi.settings.to_dict(),
                "chat": self.chat.settings.public_dict(),
                "gsi_token": self.settings.gsi_token, "port": self.settings.port}

    def publish_gsi(self, force: bool = False) -> None:
        st = self.gsi.status
        key = (st.connected, st.own_player, st.team, st.held, st.map_phase, st.round_phase, st.round,
               st.activity, st.health, st.round_kills, st.match_kills, st.rejected_auth)
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
            result = self.roulette.on_kill(ev["weapon"], headshot=ev["headshot"],
                                           ambiguous=bool(ev["ambiguous"]), raw_name=ev["raw"])
            if result["advanced"]:
                batch_advanced = True
                self._last_kill_advance = time.monotonic()
            self.broker.publish("kill", {**result, "headshot": ev["headshot"], "raw": ev["raw"],
                                         "t": time.time()})
        self.publish_gsi()


def _template(name: str) -> str:
    path = os.path.join(config.package_dir(), "templates", name)
    with open(path, encoding="utf-8") as f:
        return f.read()


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
        state.hotkeys.start()
        state.dpi.start(loop)
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
        _say(f"[cs2-roulette] панель:   {state.urls()['panel']}")
        _say(f"[cs2-roulette] виджет:   {state.urls()['overlay_weapons']}")
        _say(f"[cs2-roulette] лоадаут:  {state.urls()['overlay_loadout']}")
        _say(f"[cs2-roulette] DPI:      {state.urls()['overlay_dpi']}  (backend: {state.dpi.backend.name})")
        _say(f"[cs2-roulette] данные:   {config.data_dir()}")
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            state.hotkeys.stop()
            state.dpi.stop()
            state.chat.stop()
            state.twitch.stop()
            state.save_state()

    app = FastAPI(title="CS2 Roulette", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.app = state

    # Pure ASGI middleware (not BaseHTTPMiddleware: that one buffers/wraps streaming bodies and
    # keeps the infinite SSE stream alive after the client is gone).
    app.add_middleware(SameOriginOnly)

    app.mount("/static", StaticFiles(directory=os.path.join(config.package_dir(), "static")), name="static")
    app.include_router(dpi.router)

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def panel() -> str:
        return _template("panel.html")

    @app.get("/overlay/weapons", response_class=HTMLResponse)
    async def overlay_weapons() -> str:
        return _template("overlay_weapons.html")

    @app.get("/overlay/loadout", response_class=HTMLResponse)
    async def overlay_loadout() -> str:
        return _template("overlay_loadout.html")

    # -------------------------------------------------------------------- SSE
    @app.get("/events")
    async def events() -> StreamingResponse:
        gen = state.broker.stream(state.full_snapshot)
        return StreamingResponse(gen, media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -------------------------------------------------------------------- API
    @app.get("/api/state")
    async def api_state() -> dict:
        return state.full_snapshot()

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
        result = state.roulette.on_kill(weapon, headshot=bool(body.get("headshot")),
                                        ambiguous=bool(body.get("ambiguous")))
        if result["advanced"]:
            state._last_kill_advance = time.monotonic()
        state.broker.publish("kill", {**result, "headshot": bool(body.get("headshot")), "raw": None,
                                      "t": time.time()})
        return result

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
        for section in ("roulette", "overlay", "hotkeys", "dpi", "chat"):
            if section in body and body[section] is not None and not isinstance(body[section], dict):
                raise HTTPException(400, f"{section}: нужен объект")
        if "chat" in body:
            try:
                state.chat.update_settings(body["chat"] or {})
            except ValueError as e:
                raise HTTPException(400, f"Некорректные настройки чата: {e}")
            s.chat = state.chat.settings.to_dict()
        if "dpi" in body:
            try:
                state.dpi.update_settings(body["dpi"] or {})
            except ValueError as e:
                raise HTTPException(400, f"Некорректные настройки DPI: {e}")
            s.dpi = state.dpi.settings.to_dict()
        if "dpi" in body or "chat" in body:
            state.sync_twitch()
        if "roulette" in body:
            try:
                state.roulette.update_settings(body["roulette"] or {})
            except ValueError as e:
                raise HTTPException(400, f"Некорректные настройки рулетки: {e}")
            s.roulette = state.roulette.settings.to_dict()
        if "overlay" in body:
            rejected += [f"overlay.{k}" for k in s.overlay.apply(body["overlay"] or {})]
        if "hotkeys" in body:
            rejected += [f"hotkeys.{k}" for k in s.hotkeys.apply(body["hotkeys"] or {})]
            state.hotkeys.update(s.hotkeys.bindings(), s.hotkeys.enabled)
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

    @app.get("/api/gsi/info")
    async def gsi_info() -> dict:
        cfg = steam.installed_cfg_status(state.cfg_text())
        return {"cfg_dir": steam.cs2_cfg_dir(), "installed": cfg["path"], "cfg_state": cfg["state"],
                "cfg_name": steam.CFG_NAME, "uri": state.urls()["gsi"], "status": state.gsi.status.to_dict()}

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

    @app.get("/api/hotkeys")
    async def api_hotkeys() -> dict:
        return state.hotkeys.describe()

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

    @app.post("/api/chat/test")
    async def chat_test(request: Request) -> dict:
        body = await _json(request)
        text = str(body.get("message") or "✅ cs2-roulette: тестовое сообщение").strip()[:450]
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, state.chat.send_now, text)

    return app


async def _json(request: Request) -> dict:
    """JSON body for /api POSTs. Requires an explicit JSON content type (browser CSRF guard)."""
    ctype = request.headers.get("content-type", "")
    if not ctype.lower().startswith("application/json"):
        raise HTTPException(415, "Content-Type must be application/json")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Некорректный JSON")
    return body if isinstance(body, dict) else {}


app = create_app()
