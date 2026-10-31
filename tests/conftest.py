import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    """The app with a temporary data dir, a fake mouse and no browser pop-up."""
    monkeypatch.setenv("CS2R_DATA", str(tmp_path))
    monkeypatch.setenv("CS2R_DPI_BACKEND", "fake")
    monkeypatch.setenv("CS2R_NO_BROWSER", "1")
    from app import main as m

    with TestClient(m.create_app(), base_url="http://127.0.0.1") as c:
        yield c
