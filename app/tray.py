"""System-tray mode: the server runs in a background thread, the tray icon owns the main thread.

No console window is needed: ``run.bat`` starts ``pythonw.exe start_tray.pyw``; the frozen
``cs2-roulette.exe`` (built with ``--noconsole``) does the same. Errors go to ``data/app.log``
and to a tray notification.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import webbrowser

from . import autostart, config

log = logging.getLogger("cs2roulette.tray")


def icon_image(size: int = 64):
    """A roulette-like glyph drawn with Pillow (no image files needed)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = size // 16
    box = (pad, pad, size - pad, size - pad)
    accent, dark = (255, 209, 102, 255), (28, 31, 40, 255)
    d.ellipse(box, fill=dark)
    for i in range(8):
        if i % 2 == 0:
            d.pieslice(box, i * 45, (i + 1) * 45, fill=accent)
    c = size // 2
    r = size // 6
    d.ellipse((c - r, c - r, c + r, c + r), fill=dark, outline=accent, width=max(1, size // 32))
    return img


def open_folder(path: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        log.exception("cannot open folder %s", path)


class TrayApp:
    def __init__(self, panel_url: str, stop_server) -> None:
        self.panel_url = panel_url
        self.stop_server = stop_server
        self.icon = None

    # ------------------------------------------------------------------ menu
    def _open_panel(self, *_: object) -> None:
        webbrowser.open(self.panel_url)

    def _open_data(self, *_: object) -> None:
        open_folder(config.data_dir())

    def _toggle_autostart(self, *_: object) -> None:
        try:
            autostart.set_enabled(not autostart.is_enabled())
        except Exception as e:  # noqa: BLE001
            log.exception("autostart toggle failed")
            self.notify(f"Не удалось изменить автозапуск: {e}")

    def _quit(self, *_: object) -> None:
        try:
            self.stop_server()
        finally:
            if self.icon:
                self.icon.stop()

    def notify(self, text: str) -> None:
        try:
            if self.icon:
                self.icon.notify(text, "CS2 Roulette")
        except Exception:
            pass

    # ------------------------------------------------------------------- run
    def run(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Открыть панель", self._open_panel, default=True),
            pystray.MenuItem("Автозапуск с Windows", self._toggle_autostart,
                             checked=lambda item: autostart.is_enabled(), visible=autostart.available()),
            pystray.MenuItem("Открыть папку данных", self._open_data),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self._quit),
        )
        self.icon = pystray.Icon("cs2-roulette", icon_image(64), "CS2 Roulette", menu)
        self.icon.run()


def run_with_tray(app_import: str, host: str, port: int, panel_url: str, open_browser: bool) -> int:
    """Start uvicorn in a thread, then block on the tray loop. Returns an exit code."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app_import, host=host, port=port, log_level="info", log_config=None))
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            server.run()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
            log.exception("server crashed")

    thread = threading.Thread(target=serve, name="uvicorn", daemon=True)
    thread.start()

    def stop_server() -> None:
        server.should_exit = True
        thread.join(timeout=5.0)

    tray = TrayApp(panel_url, stop_server)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(panel_url)).start()

    def watchdog() -> None:
        thread.join()
        if errors or not server.should_exit:
            tray.notify("Сервер остановился с ошибкой, смотрите data/app.log")
            if tray.icon:
                tray.icon.stop()

    threading.Thread(target=watchdog, name="watchdog", daemon=True).start()
    tray.run()
    stop_server()
    return 1 if errors else 0
