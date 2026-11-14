"""FastAPI application: control panel, OBS overlays, SSE stream, GSI receiver and the HTTP API around the
``AppState`` (app/state.py). See app/state.py for the threading rules.
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

from . import autostart, config, docs, ingame as ingame_mod, loadout as L, steam, weapons as W
from .modules import dpi
from .modules.donations import routes as donation_routes
from .state import AppState
from .twitch_api import TwitchError
from .twitch_irc import IrcMessage
from .version import VERSION
from .web import SameOriginOnly, json_body as _json, page

# Callables that stop this instance (set by the tray). Without them /api/quit raises SIGINT, which
# uvicorn in console mode turns into a graceful shutdown.
QUIT_HOOK: list = []
log = logging.getLogger("hardmode")


def _say(text: str) -> None:
    """Console print that survives odd Windows code pages / redirected stdout."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"))


def create_app() -> FastAPI:
    state = AppState()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()
        state.broker.attach_loop(loop)
        state.dpi.start(loop)
        state.ingame.start()
        state.chat.start()
        state.donations.start()
        state.obs.start()         # OBS may have been started first: reload its widget sources
        state.twitch_api.start()
        state.sync_twitch()
        state.updater.start()
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
            state.donations.stop()
            state.twitch_api.stop()
            state.updater.stop()
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
    app.include_router(donation_routes.router)

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
        return {**state.settings.to_dict(), **state.module_settings()}     # module secrets only as «*_set»

    @app.post("/api/settings")
    async def api_set_settings(request: Request) -> dict:
        body = await _json(request)
        s = state.settings
        rejected: list[str] = []
        for section in ("overlay", "ingame", *(m[0] for m in state.MODULE_SETTINGS)):
            if section in body and body[section] is not None and not isinstance(body[section], dict):
                raise HTTPException(400, f"{section}: нужен объект")
        # the app's switches of the channel-points features also switch Hardmode's rewards in Twitch
        was_on = {"weapon": state.donations.settings.reward_enabled, "dpi": state.dpi.settings.viewer_enabled}
        for section, attr, label in state.MODULE_SETTINGS:
            if section in body:
                owner = getattr(state, attr)
                try:
                    owner.update_settings(body[section] or {})
                except ValueError as e:
                    raise HTTPException(400, f"Некорректные настройки {label}: {e}")
                setattr(s, section, owner.settings.to_dict())
        if "dpi" in body or "chat" in body or "donations" in body:
            state.sync_twitch()
        now_on = {"weapon": state.donations.settings.reward_enabled, "dpi": state.dpi.settings.viewer_enabled}
        for kind in ("weapon", "dpi"):
            if now_on[kind] != was_on[kind] and kind in state.twitch_api.state["rewards"]:
                threading.Thread(target=state.twitch_api.set_enabled, args=(kind, now_on[kind]),
                                 name="twitch-reward-onoff", daemon=True).start()
        if "overlay" in body:
            rejected += [f"overlay.{k}" for k in s.overlay.apply(body["overlay"] or {})]
        if "ingame" in body:
            rejected += [f"ingame.{k}" for k in s.ingame.apply(body["ingame"] or {})]
            state.ingame.update_settings(s.ingame)
        if body.get("regenerate_token"):
            s.gsi_token = secrets.token_urlsafe(12)
            state.gsi.token = s.gsi_token
        if "port" in body:
            port = config.parse_port(body["port"])
            if port:
                s.port = port
            else:
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
    @app.post("/api/twitch/chat")
    async def twitch_fake_chat(request: Request) -> dict:
        """Simulate a chat line (testing without Twitch): {user, text, reward_id?} (reward lines are ignored)."""
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

    @app.get("/api/stats/history")
    async def api_stats_history() -> dict:
        """Archived parts (streams and play outside them), newest first."""
        return {"sessions": await asyncio.to_thread(state.stats.history)}

    @app.get("/api/stats/history/{sid}")
    async def api_stats_history_item(sid: str) -> dict:
        item = state.stats.history_item(sid)
        if item is None:
            raise HTTPException(404, "нет такой записи")
        return item

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

    @app.get("/api/twitch/account")
    async def twitch_account() -> dict:
        return state.twitch_api.status()

    @app.post("/api/twitch/login")
    async def twitch_login() -> dict:
        """Start the device-code sign-in: the panel shows the code and opens twitch.tv/activate."""
        try:
            flow = await asyncio.to_thread(state.twitch_api.start_login)
        except TwitchError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **flow}

    @app.get("/api/update")
    async def update_status() -> dict:
        return state.updater.status()

    @app.post("/api/update/check")
    async def update_check() -> dict:
        return await asyncio.to_thread(state.updater.check)

    @app.post("/api/update/apply")
    async def update_apply() -> dict:
        """git pull; in tray mode run.bat starts the new version, which asks this one to quit."""
        try:
            return {"ok": True, **await asyncio.to_thread(state.updater.update)}
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/predictions")
    async def predictions_status() -> dict:
        return state.predictions.status()

    @app.post("/api/predictions/start")
    async def predictions_start() -> dict:
        """By hand: when the app missed the start of the match (opened mid-match) or for a match it skips."""
        try:
            await asyncio.to_thread(state.predictions.start, state.gsi.status.map_name)
        except (TwitchError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    @app.post("/api/predictions/finish")
    async def predictions_finish(request: Request) -> dict:
        result = str((await _json(request)).get("result") or "")
        if result not in ("win", "loss", "cancel"):
            raise HTTPException(400, "result: win, loss или cancel")
        fn = state.predictions.cancel if result == "cancel" else (lambda: state.predictions.finish(result))
        try:
            await asyncio.to_thread(fn)
        except (TwitchError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    @app.post("/api/twitch/logout")
    async def twitch_logout() -> dict:
        await asyncio.to_thread(state.twitch_api.logout)
        state.sync_twitch()                      # no channel to read the commands from any more
        state.broker.publish("chat", state.chat.status())
        return {"ok": True}

    @app.post("/api/twitch/reward")
    async def twitch_reward_create(request: Request) -> dict:
        """{kind: weapon|dpi, cost?, prompt?}: create Hardmode's reward in the channel (refunds work only for it)."""
        body = await _json(request)
        kind = str(body.get("kind") or "")
        prompt = str(body.get("prompt") or "").strip() or None
        if kind == "dpi" and not prompt:
            prompt = f"Число DPI от {state.dpi.settings.viewer_min} до {state.dpi.settings.viewer_max}"
        try:
            r = await asyncio.to_thread(state.twitch_api.create_reward, kind, body.get("cost"), prompt)
        except TwitchError as e:
            return {"ok": False, "error": str(e)}
        state.on_reward_created(kind)
        return {"ok": True, "reward": r}

    @app.post("/api/twitch/sync")
    async def twitch_sync() -> dict:
        """Re-read Hardmode's rewards from Twitch now (the panel asks when the streamer looks at them)."""
        return await asyncio.to_thread(state.twitch_api.sync_now)

    @app.post("/api/twitch/reward/update")
    async def twitch_reward_update(request: Request) -> dict:
        """{kind, cost?, prompt?}: change the cost / the description of Hardmode's reward in Twitch."""
        body = await _json(request)
        prompt = body.get("prompt")
        try:
            r = await asyncio.to_thread(state.twitch_api.update_reward, str(body.get("kind") or ""), body.get("cost"),
                                        None if prompt is None else str(prompt))
        except (TwitchError, KeyError) as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "reward": r}

    @app.post("/api/twitch/reward/delete")
    async def twitch_reward_delete(request: Request) -> dict:
        body = await _json(request)
        kind = str(body.get("kind") or "")
        try:
            await asyncio.to_thread(state.twitch_api.delete_reward, kind)
        except TwitchError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    @app.get("/api/obs")
    async def obs_status() -> dict:
        return state.obs.status()

    @app.post("/api/obs/check")
    async def obs_check() -> dict:
        """Is OBS reachable with the saved port/password? (the panel asks when Startup or OBS is shown)"""
        return await asyncio.to_thread(state.obs.check)

    @app.post("/api/obs/refresh")
    async def obs_refresh() -> dict:
        """Reload the widget browser sources in OBS now (also the connection check of the panel)."""
        return await asyncio.to_thread(state.obs.refresh, "manual")

    @app.get("/api/ingame/render")
    async def ingame_render(kind: str = "dpi") -> Response:
        if kind not in ingame_mod.KINDS:
            raise HTTPException(400, "kind: dpi | weapons | queue")
        png = await asyncio.to_thread(state.ingame.render_png, kind)
        return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/ingame/preview")
    async def ingame_preview(request: Request) -> dict:
        body = await _json(request)
        kind = str(body.get("kind") or "dpi")
        if kind not in ingame_mod.KINDS:
            raise HTTPException(400, "kind: dpi | weapons | queue")
        if not state.ingame.available:
            return {"ok": False, "error": "Оверлей поверх игры работает только в Windows"}
        state.ingame.preview(kind, 5.0)
        return {"ok": True}

    @app.post("/api/chat/test")
    async def chat_test(request: Request) -> dict:
        body = await _json(request)
        text = str(body.get("message") or "✅ hardmode: тестовое сообщение").strip()[:450]
        res = await asyncio.to_thread(state.chat.send_now, text)
        if res.get("ok"):
            res["response"] = state.chat.last_response
        return res

    return app




app = create_app()
