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


def test_stop_uvicorn_escalates_to_force_exit():
    import threading
    import time

    from app.tray import stop_uvicorn

    class FakeServer:
        should_exit = False
        force_exit = False

    srv = FakeServer()

    def serve():
        # like uvicorn with open SSE connections: should_exit alone is not enough
        while not srv.force_exit:
            time.sleep(0.02)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert stop_uvicorn(srv, t, grace=0.2, force_grace=2.0) is True
    assert srv.should_exit and srv.force_exit and not t.is_alive()


def test_stop_uvicorn_graceful_path_does_not_force():
    import threading
    import time

    from app.tray import stop_uvicorn

    class FakeServer:
        should_exit = False
        force_exit = False

    srv = FakeServer()

    def serve():
        while not srv.should_exit:
            time.sleep(0.02)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert stop_uvicorn(srv, t, grace=2.0, force_grace=1.0) is True
    assert srv.force_exit is False


def test_tray_quit_does_not_block_the_menu_thread():
    import threading
    import time

    from app.tray import TrayApp

    started = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def slow_stop():
        started.set()
        release.wait(5)

    class FakeIcon:
        def stop(self):
            stopped.set()

    app = TrayApp("http://127.0.0.1:1/", slow_stop)
    app.icon = FakeIcon()
    t0 = time.monotonic()
    app._quit()
    assert time.monotonic() - t0 < 0.5      # returned immediately
    assert started.wait(2)
    app._quit()                              # second click is ignored
    release.set()
    assert stopped.wait(2)
