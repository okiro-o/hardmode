import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def fast_twitch_sign_in(monkeypatch):
    """The device-code sign-in polls Twitch at most once a second; the fake Twitch answers at once."""
    from app import twitch_api
    monkeypatch.setattr(twitch_api, "MIN_LOGIN_POLL_S", 0.01)


def wait_until(cond, timeout: float = 3.0, step: float = 0.01) -> bool:
    """Poll ``cond`` until it is true or the time is up; returns its last value (for background threads)."""
    deadline = time.monotonic() + timeout
    while not cond() and time.monotonic() < deadline:
        time.sleep(step)
    return bool(cond())


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app with a temporary data dir, a fake mouse and no browser pop-up."""
    monkeypatch.setenv("CS2R_DATA", str(tmp_path))
    monkeypatch.setenv("CS2R_DPI_BACKEND", "fake")
    monkeypatch.setenv("CS2R_NO_BROWSER", "1")
    monkeypatch.setenv("CS2R_NO_UPDATE", "1")               # never git fetch from the tests
    from app import main as m

    with TestClient(m.create_app(), base_url="http://127.0.0.1") as c:
        yield c
