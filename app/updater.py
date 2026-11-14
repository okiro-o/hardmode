"""Update from the panel (0.36.0, user request): the app runs from a git checkout on the streamer's PC and is
updated with ``git pull``. The updater fetches the remote every few hours, shows the new version and the
commit subjects, and on «Обновить» pulls (fast-forward only) and starts ``run.bat`` again: the new
instance asks this one to quit through ``/api/quit`` (the launcher's port take-over) and takes the port.

Disabled in the PyInstaller build (no git checkout) and when ``git`` is not installed. The restart starts the
same launcher the app runs from: ``run.bat`` (tray) or ``run-console.bat`` (console, 0.37.1 — the user runs it
that way; the old console window closes when its instance quits on the take-over).
Windows process flags cannot be exercised on macOS: tests replace ``run``/``spawn``."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable

log = logging.getLogger("hardmode.updater")

CHECK_EVERY_S = 3 * 3600
FIRST_CHECK_S = 20
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)          # pythonw: git must not flash a console
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)       # run.bat shows pip progress when it installs


def _git_env() -> dict:
    # never wait for a password prompt or a credential window: the repository is public
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}


def default_run(args: list[str], cwd: str, timeout: float) -> tuple[int, str]:
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env=_git_env(), creationflags=NO_WINDOW)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def default_spawn(root: str, console: bool) -> None:
    env = {**os.environ, "CS2R_NO_BROWSER": "1"}      # the open panel reconnects by itself, no new tab
    bat = "run-console.bat" if console else "run.bat"
    subprocess.Popen(["cmd.exe", "/c", bat], cwd=root, env=env, creationflags=NEW_CONSOLE, close_fds=True)


class Updater:
    def __init__(self, root: str, current: str, publish_threadsafe: Callable[[str, dict], None],
                 run: Callable[[list[str], str, float], tuple[int, str]] = default_run,
                 spawn: Callable[[str, bool], None] = default_spawn) -> None:
        self.root = root
        self.current = current
        self._publish = publish_threadsafe
        self._run = run
        self._spawn = spawn
        self._lock = threading.Lock()
        self.git = shutil.which("git")
        self.available = bool(self.git) and os.path.isdir(os.path.join(root, ".git")) \
            and not getattr(sys, "frozen", False) and os.environ.get("CS2R_NO_UPDATE") != "1"
        self.state: dict = {"behind": 0, "latest": None, "changes": [], "checked_at": None, "error": None,
                            "busy": False, "text": None}
        self._stop = threading.Event()

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {"available": self.available, "current": self.current, "restart_self": sys.platform.startswith("win"),
                "console": os.environ.get("CS2R_MODE") != "tray",
                **{k: (list(v) if isinstance(v, list) else v) for k, v in self.state.items()}}

    def _changed(self) -> None:
        self._publish("update", self.status())

    def _git(self, *args: str, timeout: float = 60.0) -> str:
        code, out = self._run([self.git or "git", *args], self.root, timeout)
        if code != 0:
            raise RuntimeError(out.strip().splitlines()[-1] if out.strip() else f"git {args[0]}: код {code}")
        return out

    # ------------------------------------------------------------------- check
    def check(self) -> dict:
        """Blocking: fetch and compare with the upstream branch."""
        if not self.available:
            return self.status()
        with self._lock:
            try:
                self._git("fetch", "--quiet", "--tags", "origin", timeout=60)
                up = "@{u}"
                try:
                    self._git("rev-parse", "--verify", "--quiet", up, timeout=10)
                except RuntimeError:
                    up = "origin/main"
                behind = int(self._git("rev-list", "--count", f"HEAD..{up}", timeout=10).strip() or 0)
                changes = [s for s in self._git("log", "--format=%s", f"HEAD..{up}", timeout=10).splitlines() if s.strip()]
                latest = None
                if behind:
                    m = re.search(r'VERSION\s*=\s*"([^"]+)"', self._git("show", f"{up}:app/version.py", timeout=10))
                    latest = m.group(1) if m else None
                self.state.update(behind=behind, latest=latest, changes=changes[:20], error=None, checked_at=time.time())
            except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as e:
                self.state.update(error=f"не удалось проверить обновления: {e}", checked_at=time.time())
                log.warning("update check failed: %s", e)
        self._changed()
        return self.status()

    # ------------------------------------------------------------------ update
    def update(self) -> dict:
        """Blocking: ``git pull --ff-only`` and, in tray mode, start run.bat (it replaces this instance)."""
        if not self.available:
            raise RuntimeError("обновление из панели работает только в папке, скачанной через git")
        with self._lock:
            if self.state["busy"]:
                raise RuntimeError("обновление уже идёт")
            self.state.update(busy=True, error=None, text="скачиваю обновление…")
        self._changed()
        try:
            # porcelain lines are «XY path»: the first column may be a space, so no strip() before slicing
            dirty = [line[3:] for line in self._git("status", "--porcelain", "--untracked-files=no", timeout=20).splitlines()
                     if line.strip()]
            if dirty:
                raise RuntimeError("в папке приложения изменены файлы, git не может обновить их сам: " + ", ".join(dirty[:3]))
            self._git("pull", "--ff-only", "--quiet", timeout=180)
            st = self.status()
            restart, console = st["restart_self"], st["console"]
            self.state.update(busy=False, behind=0, changes=[],
                              text=("обновлено, перезапускаюсь: откроется новое окно консоли" if console else
                                    "обновлено, перезапускаюсь…") if restart else "обновлено: перезапустите приложение")
            self._changed()
            if restart:
                self._spawn(self.root, console)
            log.info("updated by git pull%s", ", restarting" if restart else "")
            return self.status()
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            self.state.update(busy=False, error=f"обновление не удалось: {e}", text=None)
            self._changed()
            raise RuntimeError(str(e)) from None
        finally:
            self.state["busy"] = False

    # ------------------------------------------------------------------ worker
    def start(self) -> None:
        if not self.available:
            return
        threading.Thread(target=self._loop, name="updater", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        if self._stop.wait(FIRST_CHECK_S):
            return
        while True:
            try:
                self.check()
            except Exception:  # noqa: BLE001 - never kill the thread
                log.exception("update check crashed")
            if self._stop.wait(CHECK_EVERY_S):
                return
