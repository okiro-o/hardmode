"""Small HTTP helpers shared by ``app.main`` and the DPI router."""
from __future__ import annotations

import os

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from . import config

NO_STORE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


def template(name: str) -> str:
    with open(os.path.join(config.package_dir(), "templates", name), encoding="utf-8") as f:
        return f.read()


def page(name: str) -> HTMLResponse:
    """HTML pages are never cached: OBS's browser (CEF) otherwise keeps old overlay JS after an update."""
    return HTMLResponse(template(name), headers=NO_STORE)


async def json_body(request: Request) -> dict:
    """JSON body for /api POSTs. Requires an explicit JSON content type (browser CSRF guard)."""
    ctype = request.headers.get("content-type", "")
    if not ctype.lower().startswith("application/json"):
        raise HTTPException(415, "Content-Type must be application/json")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Некорректный JSON")
    return body if isinstance(body, dict) else {}
