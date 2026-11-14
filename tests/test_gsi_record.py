import json

from app.gsi_record import GsiRecorder
from tests.test_gsi import payload


def _lines(path):
    return [json.loads(x) for x in open(path, encoding="utf-8")]


def test_only_changes_are_kept_and_the_token_never(tmp_path):
    path = tmp_path / "gsi-record.jsonl"
    rec = GsiRecorder(str(path))
    p = payload(); p["auth"] = {"token": "secret-token-123"}
    assert rec.record(p, now=1.0)
    assert not rec.record(dict(p), now=1.1)                     # the same state again: skipped
    p2 = payload(rk=1)                                          # a kill: the round counter moved
    assert rec.record(p2, now=1.2)
    p3 = payload(rk=1); p3["map"]["phase"] = "gameover"; p3["map"]["team_ct"] = {"score": 7}; p3["map"]["team_t"] = {"score": 13}
    assert rec.record(p3, now=1.3)
    rows = _lines(path)
    assert [r["t"] for r in rows] == [1.0, 1.2, 1.3] and rec.saved == 3
    assert "secret-token-123" not in path.read_text(encoding="utf-8") and "auth" not in rows[0]["p"]
    assert rows[2]["p"]["map"]["team_t"]["score"] == 13


def test_size_limit_stops_the_recording(tmp_path):
    path = tmp_path / "gsi-record.jsonl"
    rec = GsiRecorder(str(path), max_bytes=10)
    assert rec.record(payload(), now=1.0)                      # the first line goes over the limit
    assert not rec.record(payload(rk=1), now=2.0) and not rec.enabled
    assert len(_lines(path)) == 1


def test_app_records_only_when_started_for_it(client, tmp_path, monkeypatch):
    st = client.app.state.app
    assert st.gsi_recorder is None and client.get("/api/state").json()["gsi_record"] is False
    st.gsi_recorder = GsiRecorder(str(tmp_path / "rec.jsonl"))
    p = payload(); p["auth"] = {"token": st.settings.gsi_token}
    client.post("/gsi", json=p)
    assert len(_lines(tmp_path / "rec.jsonl")) == 1 and client.get("/api/state").json()["gsi_record"] is True
