"""``python -m app`` entry point."""
from __future__ import annotations

import os
import socket
import sys
import webbrowser

import uvicorn

from . import config


def _port_free(port: int) -> bool:
    """True when nothing answers on the port (a bind() probe would be fooled by TIME_WAIT sockets)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return False
    except OSError:
        return True


def main() -> None:
    if "--no-browser" in sys.argv:
        os.environ["CS2R_NO_BROWSER"] = "1"
    settings = config.AppSettings.load()
    if not _port_free(settings.port):
        url = f"http://127.0.0.1:{settings.port}/"
        print(f"[cs2-roulette] port {settings.port} is busy: the app is probably already running. Opening {url}")
        if os.environ.get("CS2R_NO_BROWSER") != "1":
            webbrowser.open(url)
        return
    # Loopback only: no firewall prompt, nothing exposed to the LAN.
    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
