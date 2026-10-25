"""HTTP routes of the DPI module (mounted by ``app.main``)."""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from ... import config
from .service import DpiService

router = APIRouter()


def _svc(request: Request) -> DpiService:
    return request.app.state.app.dpi


async def _json(request: Request) -> dict:
    ctype = request.headers.get("content-type", "")
    if not ctype.lower().startswith("application/json"):
        raise HTTPException(415, "Content-Type must be application/json")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Некорректный JSON")
    return body if isinstance(body, dict) else {}


@router.get("/overlay/dpi", response_class=HTMLResponse)
async def dpi_overlay() -> str:
    path = os.path.join(config.package_dir(), "templates", "overlay_dpi.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


@router.get("/api/dpi/status")
async def dpi_status(request: Request) -> dict:
    return _svc(request).snapshot()


@router.post("/api/dpi/spin")
async def dpi_spin(request: Request) -> dict:
    body = await _json(request)
    reason = str(body.get("reason") or "manual")[:20]
    try:
        return _svc(request).spin(reason=reason)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/dpi/apply")
async def dpi_apply(request: Request) -> dict:
    body = await _json(request)
    try:
        value = int(body.get("value"))
    except (TypeError, ValueError):
        raise HTTPException(400, "value: нужно число")
    if not 100 <= value <= 44000:
        raise HTTPException(400, "value: от 100 до 44000")
    svc = _svc(request)
    if svc._pending_apply:
        svc._pending_apply.cancel()
        svc._pending_apply = None
    return await svc.apply(value, reason="manual")


@router.post("/api/dpi/home")
async def dpi_home(request: Request) -> dict:
    svc = _svc(request)
    if svc._pending_apply:
        svc._pending_apply.cancel()
        svc._pending_apply = None
    return await svc.apply(svc.settings.home_dpi, reason="home")


@router.post("/api/dpi/probe")
async def dpi_probe(request: Request) -> dict:
    return await _svc(request).probe()


@router.post("/api/dpi/hostmode")
async def dpi_hostmode(request: Request) -> dict:
    return await _svc(request).set_host_mode()


@router.post("/api/dpi/death")
async def dpi_fake_death(request: Request) -> dict:
    """Simulate a death (testing without CS2)."""
    return {"spun": _svc(request).on_death()}
