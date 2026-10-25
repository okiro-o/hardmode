"""Locate the CS2 install (Windows) and install the GSI config file."""
from __future__ import annotations

import os
import re
import sys

CS2_FOLDER = "Counter-Strike Global Offensive"
CFG_SUBDIR = os.path.join("game", "csgo", "cfg")
CFG_NAME = "gamestate_integration_cs2roulette.cfg"


def steam_root() -> str | None:
    if sys.platform.startswith("win"):
        try:
            import winreg  # type: ignore

            for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                              (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam")):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        for value in ("SteamPath", "InstallPath"):
                            try:
                                path, _ = winreg.QueryValueEx(k, value)
                                if path and os.path.isdir(path):
                                    return os.path.normpath(path)
                            except OSError:
                                continue
                except OSError:
                    continue
        except ImportError:
            pass
        for candidate in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
            if os.path.isdir(candidate):
                return candidate
        return None
    # dev convenience on other OSes
    for candidate in (os.path.expanduser("~/Library/Application Support/Steam"),
                      os.path.expanduser("~/.steam/steam"), os.path.expanduser("~/.local/share/Steam")):
        if os.path.isdir(candidate):
            return candidate
    return None


def steam_libraries(root: str | None = None) -> list[str]:
    root = root or steam_root()
    if not root:
        return []
    libs = [root]
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError:
        return libs
    for m in re.finditer(r'"path"\s+"([^"]+)"', text):
        path = m.group(1).replace("\\\\", "\\")
        if path not in libs and os.path.isdir(path):
            libs.append(path)
    return libs


def cs2_cfg_dir() -> str | None:
    """``.../Counter-Strike Global Offensive/game/csgo/cfg`` or None."""
    for lib in steam_libraries():
        cfg = os.path.join(lib, "steamapps", "common", CS2_FOLDER, CFG_SUBDIR)
        if os.path.isdir(cfg):
            return cfg
    return None


def install_cfg(content: str, cfg_dir: str | None = None) -> str:
    """Write the GSI config (UTF-8 without BOM). Returns the written path.

    ``cfg_dir`` (optional override) must be an absolute path to an existing directory.
    """
    if cfg_dir:
        if not os.path.isabs(cfg_dir) or not os.path.isdir(cfg_dir):
            raise FileNotFoundError("Укажите абсолютный путь к существующей папке game/csgo/cfg")
    else:
        cfg_dir = cs2_cfg_dir()
    if not cfg_dir:
        raise FileNotFoundError("Папка CS2 game/csgo/cfg не найдена")
    path = os.path.join(cfg_dir, CFG_NAME)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    return path


def installed_cfg_path() -> str | None:
    cfg_dir = cs2_cfg_dir()
    if not cfg_dir:
        return None
    path = os.path.join(cfg_dir, CFG_NAME)
    return path if os.path.isfile(path) else None


def installed_cfg_status(expected: str) -> dict:
    """{"path": str|None, "state": "missing"|"ok"|"stale"} - stale = token/uri differ."""
    path = installed_cfg_path()
    if not path:
        return {"path": None, "state": "missing"}
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            current = f.read()
    except OSError:
        return {"path": path, "state": "stale"}
    return {"path": path, "state": "ok" if current.strip() == expected.strip() else "stale"}
