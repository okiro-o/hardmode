"""``python -m app`` entry point.

Modes:
* console (default when run from a terminal): uvicorn in the foreground, logs on screen;
* tray (``--tray``, and the default for the frozen exe): no console, icon in the system tray,
  logs in ``data/app.log``.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import sys
import webbrowser

from . import config


def _port_free(port: int) -> bool:
    """True when nothing answers on the port (a bind() probe would be fooled by TIME_WAIT sockets)."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return False
    except OSError:
        return True


def want_tray(argv: list[str], frozen: bool) -> bool:
    if "--console" in argv:
        return False
    return "--tray" in argv or frozen


def setup_logging(to_console: bool) -> str:
    """Rotating log file in data/, plus console when there is one. Returns the log path."""
    path = os.path.join(config.data_dir(), "app.log")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if to_console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    return path


def main() -> None:
    argv = sys.argv[1:]
    if "--no-browser" in argv:
        os.environ["CS2R_NO_BROWSER"] = "1"
    frozen = bool(getattr(sys, "frozen", False))
    tray = want_tray(argv, frozen)
    if sys.platform.startswith("win"):
        # per-monitor DPI awareness: the in-game overlay must not be scaled/blurred by Windows
        try:
            import ctypes
            try:
                ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    log_path = setup_logging(to_console=not tray)
    log = logging.getLogger("cs2roulette")
    settings = config.AppSettings.load()
    url = f"http://127.0.0.1:{settings.port}/"
    open_browser = settings.open_browser and os.environ.get("CS2R_NO_BROWSER") != "1"
    if not _port_free(settings.port):
        log.warning("port %s is busy: the app is probably already running, opening %s", settings.port, url)
        print(f"[cs2-roulette] port {settings.port} is busy: the app is probably already running. Opening {url}")
        if open_browser:
            webbrowser.open(url)
        return
    log.info("starting (mode=%s, log=%s)", "tray" if tray else "console", log_path)
    os.environ["CS2R_MODE"] = "tray" if tray else "console"
    if tray:
        os.environ["CS2R_NO_BROWSER"] = "1"   # the tray opens the panel itself (after the server is up)
        from .tray import run_with_tray

        code = run_with_tray("app.main:app", "127.0.0.1", settings.port, url, open_browser)
        logging.shutdown()
        # Hard exit: background threads (uvicorn loop, hidapi, pystray helpers) must never keep a
        # tray application alive after "Выход". Everything persistent is already saved.
        os._exit(code)
    import uvicorn

    # Loopback only: no firewall prompt, nothing exposed to the LAN.
    uvicorn.run("app.main:app", host="127.0.0.1", port=settings.port, log_level="warning", log_config=None,
                timeout_graceful_shutdown=2.0)


if __name__ == "__main__":
    main()
