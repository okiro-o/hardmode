import logging
import os

from app import autostart
from app.__main__ import setup_logging, want_tray
from app.tray import icon_image


def test_want_tray_rules():
    assert want_tray([], frozen=False) is False
    assert want_tray(["--tray"], frozen=False) is True
    assert want_tray([], frozen=True) is True
    assert want_tray(["--console"], frozen=True) is False
    assert want_tray(["--tray", "--console"], frozen=False) is False


def test_setup_logging_writes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CS2R_DATA", str(tmp_path))
    path = setup_logging(to_console=False)
    logging.getLogger("x").info("hello")
    for h in logging.getLogger().handlers:
        h.flush()
    assert os.path.isfile(path) and "hello" in open(path, encoding="utf-8").read()
    # restore a console handler for the rest of the test session
    setup_logging(to_console=True)


def test_icon_image_and_launch_command():
    img = icon_image(32)
    assert img.size == (32, 32) and img.mode == "RGBA"
    cmd = autostart.launch_command()
    assert "start_tray.pyw" in cmd and cmd.startswith('"')
    if not autostart.available():
        assert autostart.is_enabled() is False and autostart.set_enabled(True) is False
