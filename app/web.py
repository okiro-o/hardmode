"""Small HTTP helpers shared by ``app.main`` and the module routers, and the same-origin guard."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import config

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
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
