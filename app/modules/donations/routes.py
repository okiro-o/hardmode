"""HTTP routes: donations (DonationAlerts OAuth, DonatePay key, test donation, aliases) and the viewer queue."""
from __future__ import annotations

import asyncio
import html
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ... import weapons as W
from ...web import NO_STORE, json_body as _json
from . import aliases as A
from .providers import Donation, ProviderError

router = APIRouter()


def _state(request: Request):
    return request.app.state.app


def da_redirect_uri(request: Request) -> str:
    return f"http://127.0.0.1:{_state(request).settings.port}/donations/da/callback"


def _int(body: dict, key: str) -> int:
    try:
        return int(body.get(key))
    except (TypeError, ValueError):
        raise HTTPException(400, f"{key}: нужно число") from None


# ----------------------------------------------------------------------------- donations
@router.get("/api/donations")
async def donations_status(request: Request) -> dict:
    return {**_state(request).donations.status(), "redirect_uri": da_redirect_uri(request)}


@router.post("/api/donations/da/connect")
async def da_connect(request: Request) -> dict:
    try:
        url = _state(request).donations.da_authorize_url(da_redirect_uri(request))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"url": url}


@router.get("/donations/da/callback", response_class=HTMLResponse)
async def da_callback(request: Request) -> HTMLResponse:
    """DonationAlerts sends the browser here after «Разрешить» (top-level GET, no Origin header)."""
    q = request.query_params
    svc = _state(request).donations
    if q.get("error"):
        ok, text = False, f"DonationAlerts отказал: {q.get('error_description') or q.get('error')}"
    else:
        try:
            loop = asyncio.get_running_loop()
            user = await loop.run_in_executor(None, svc.da_finish, q.get("code") or "", q.get("state") or "",
                                              da_redirect_uri(request))
            ok, text = True, f"DonationAlerts подключён: {user.get('name') or user.get('code') or 'аккаунт'}"
        except (ValueError, ProviderError) as e:
            ok, text = False, f"Не получилось: {e}"
    color = "#7ee787" if ok else "#ff7b72"
    page = (f"<!doctype html><meta charset='utf-8'><title>Hardmode</title>"
            f"<body style='background:#14171f;color:#e8eaf0;font:16px system-ui;display:grid;place-items:center;height:100vh;margin:0'>"
            f"<div style='text-align:center'><div style='font-size:22px;color:{color};margin-bottom:10px'>{html.escape(text)}</div>"
            f"<div>Эту вкладку можно закрыть. <a style='color:#ffd166' href='/#donations'>Вернуться в панель</a></div></div>")
    return HTMLResponse(page, headers=NO_STORE)


@router.post("/api/donations/da/disconnect")
async def da_disconnect(request: Request) -> dict:
    _state(request).donations.disconnect_da()
    return {"ok": True}


@router.post("/api/donations/dp/verify")
async def dp_verify(request: Request) -> dict:
    svc = _state(request).donations
    try:
        user = await asyncio.get_running_loop().run_in_executor(None, svc.dp_verify)
    except (ValueError, ProviderError) as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "user": user.get("name")}


@router.post("/api/donations/test")
async def donation_test(request: Request) -> dict:
    """Run a fake donation through the rules (no money, no service involved)."""
    body = await _json(request)
    try:
        amount = float(str(body.get("amount", 0)).replace(",", "."))
    except ValueError:
        raise HTTPException(400, "amount: нужно число") from None
    d = Donation(provider="test", id=f"test-{time.time():.3f}", user=str(body.get("user") or "тест")[:40],
                 amount=amount, currency="RUB", message=str(body.get("message") or "")[:500], test=True)
    return _state(request).donations.handle(d)


@router.get("/api/donations/aliases")
async def aliases_list(request: Request) -> dict:
    svc = _state(request).donations
    extra, _ = A.parse_extra(svc.settings.aliases_extra)
    by_weapon: dict[str, list[str]] = {}
    for alias, wid in extra.items():
        by_weapon.setdefault(wid, []).append(alias)
    return {"weapons": [{"id": w.id, "name": w.name, "aliases": A.DEFAULT_ALIASES.get(w.id, []),
                         "extra": by_weapon.get(w.id, [])} for w in W.CATALOG],
            "errors": svc.alias_errors}


@router.post("/api/donations/aliases/check")
async def aliases_check(request: Request) -> dict:
    body = await _json(request)
    text = str(body.get("text") or "")[:500]
    found = _state(request).donations.aliases.find(text)
    return {"weapon": found[0] if found else None, "name": W.BY_ID[found[0]].name if found else None,
            "matched": found[1] if found else None, "number": A.find_number(text)}


# ----------------------------------------------------------------------------- viewer queue
@router.get("/api/queue")
async def queue_status(request: Request) -> dict:
    return _state(request).queue.snapshot()


@router.post("/api/queue/add")
async def queue_add(request: Request) -> dict:
    body = await _json(request)
    weapon = body.get("weapon")
    if weapon is not None and weapon not in W.BY_ID:
        raise HTTPException(400, "неизвестное оружие")
    try:
        return _state(request).queue.add(weapon, str(body.get("text") or ""), str(body.get("user") or "стример"),
                                         source="manual")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/queue/done")
async def queue_done(request: Request) -> dict:
    st = _state(request)
    e = st.queue.done(_int(await _json(request), "id"))
    if e and e.get("name"):
        st.chat.notify_request_done(e["user"], e["name"])
    return {"ok": bool(e)}


@router.post("/api/queue/dismiss")
async def queue_dismiss(request: Request) -> dict:
    return {"ok": bool(_state(request).queue.dismiss(_int(await _json(request), "id")))}


@router.post("/api/queue/resolve")
async def queue_resolve(request: Request) -> dict:
    body = await _json(request)
    try:
        e = _state(request).queue.resolve(_int(body, "id"), str(body.get("weapon") or ""))
    except ValueError as err:
        raise HTTPException(400, str(err))
    return {"ok": bool(e)}


@router.post("/api/queue/move")
async def queue_move(request: Request) -> dict:
    body = await _json(request)
    return {"ok": _state(request).queue.move(_int(body, "id"), _int(body, "delta"))}


@router.post("/api/queue/clear")
async def queue_clear(request: Request) -> dict:
    _state(request).queue.clear_history()
    return {"ok": True}
