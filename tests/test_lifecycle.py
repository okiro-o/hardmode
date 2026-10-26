import logging

import pytest
from fastapi.testclient import TestClient

from app import __main__ as launcher
from app import main as m


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CS2R_DATA", str(tmp_path))
    monkeypatch.setenv("CS2R_DPI_BACKEND", "fake")
    monkeypatch.setenv("CS2R_NO_BROWSER", "1")
    app = m.create_app()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c


def test_api_quit_calls_registered_hook(client, monkeypatch):
    import threading

    called = threading.Event()
    monkeypatch.setattr(m, "QUIT_HOOK", [called.set])
    r = client.post("/api/quit", json={})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["version"] == m.VERSION
    assert called.wait(3)


def test_api_quit_is_blocked_cross_site(client):
    r = client.post("/api/quit", json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_take_over_decisions():
    log = logging.getLogger("test")
    assert launcher.take_over(1, "2.0", log, version_fn=lambda p: None) == "foreign"
    assert launcher.take_over(1, "2.0", log, version_fn=lambda p: "2.0") == "same"
    calls = []
    verdict = launcher.take_over(1, "2.0", log, version_fn=lambda p: "1.0",
                                 quit_fn=lambda p: calls.append("quit") or True, wait_fn=lambda p: True)
    assert verdict == "free" and calls == ["quit"]
    assert launcher.take_over(1, "2.0", log, version_fn=lambda p: "1.0", quit_fn=lambda p: False) == "stuck"
    assert launcher.take_over(1, "2.0", log, version_fn=lambda p: "1.0", quit_fn=lambda p: True, wait_fn=lambda p: False) == "stuck"


def test_version_single_source():
    from app.version import VERSION
    assert m.VERSION == VERSION == launcher.VERSION
