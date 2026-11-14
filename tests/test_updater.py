import pytest

from app import updater as U


class FakeGit:
    """Answers the git commands the updater runs; records them."""

    def __init__(self, behind=2, dirty="", fail=None):
        self.behind, self.dirty, self.fail = behind, dirty, fail or {}
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd, timeout):
        cmd = args[1:]
        self.calls.append(cmd)
        if cmd[0] in self.fail:
            return 1, self.fail[cmd[0]]
        if cmd[0] == "rev-list":
            return 0, f"{self.behind}\n"
        if cmd[0] == "log":
            return 0, "v0.36.1: исправление\nv0.36.0: обновление из панели\n"[: 200 if self.behind else 0]
        if cmd[0] == "show":
            return 0, '"""x"""\nVERSION = "0.36.1"\n'
        if cmd[0] == "status":
            return 0, self.dirty
        return 0, ""


def _up(tmp_path, monkeypatch, git, mode="tray", platform="win32"):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(U.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.delenv("CS2R_NO_UPDATE", raising=False)
    monkeypatch.setenv("CS2R_MODE", mode)
    monkeypatch.setattr(U.sys, "platform", platform)
    published, spawned = [], []
    u = U.Updater(str(tmp_path), "0.35.0", publish_threadsafe=lambda ev, d: published.append((ev, d)),
                  run=git, spawn=lambda root: spawned.append(root))
    return u, published, spawned


def test_check_finds_the_new_version_and_its_changes(tmp_path, monkeypatch):
    git = FakeGit()
    u, published, _ = _up(tmp_path, monkeypatch, git)
    st = u.check()
    assert st["available"] and st["behind"] == 2 and st["latest"] == "0.36.1"
    assert st["changes"] == ["v0.36.1: исправление", "v0.36.0: обновление из панели"]
    assert ["fetch", "--quiet", "--tags", "origin"] in git.calls and published[-1][0] == "update"
    git.behind = 0
    st = u.check()
    assert st["behind"] == 0 and st["latest"] is None and st["changes"] == []


def test_update_pulls_and_restarts_in_tray_mode(tmp_path, monkeypatch):
    git = FakeGit()
    u, _, spawned = _up(tmp_path, monkeypatch, git)
    u.check()
    st = u.update()
    assert ["pull", "--ff-only", "--quiet"] in git.calls and spawned == [str(tmp_path)]
    assert st["behind"] == 0 and "перезапускаюсь" in st["text"] and not st["busy"]


def test_console_mode_updates_without_restarting(tmp_path, monkeypatch):
    u, _, spawned = _up(tmp_path, monkeypatch, FakeGit(), mode="console")
    st = u.update()
    assert spawned == [] and "run-console.bat" in st["text"]


def test_changed_files_or_a_failed_pull_are_reported(tmp_path, monkeypatch):
    u, _, spawned = _up(tmp_path, monkeypatch, FakeGit(dirty=" M app/main.py\n"))
    with pytest.raises(RuntimeError, match="изменены файлы"):
        u.update()
    assert spawned == [] and "app/main.py" in u.status()["error"] and not u.status()["busy"]
    (tmp_path / "b").mkdir()
    u2, _, spawned2 = _up(tmp_path / "b", monkeypatch, FakeGit(fail={"pull": "fatal: Not possible to fast-forward"}))
    with pytest.raises(RuntimeError, match="fast-forward"):
        u2.update()
    assert spawned2 == []


def test_check_error_is_kept_quietly(tmp_path, monkeypatch):
    u, _, _ = _up(tmp_path, monkeypatch, FakeGit(fail={"fetch": "fatal: unable to access 'https://github.com/'"}))
    st = u.check()
    assert st["behind"] == 0 and "не удалось проверить" in st["error"]


def test_not_available_without_git_checkout_or_in_tests(tmp_path, monkeypatch):
    monkeypatch.setattr(U.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.delenv("CS2R_NO_UPDATE", raising=False)
    assert not U.Updater(str(tmp_path), "0.35.0", lambda ev, d: None).available          # no .git folder
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("CS2R_NO_UPDATE", "1")
    assert not U.Updater(str(tmp_path), "0.35.0", lambda ev, d: None).available


def test_api_update_routes(client):
    assert client.get("/api/update").json()["available"] is False
    assert client.get("/api/state").json()["update"]["available"] is False
    r = client.post("/api/update/apply").json()
    assert r["ok"] is False and "git" in r["error"]
