"""Autostart with Windows (HKCU\\...\\Run) and the command line that launches the tray mode."""
from __future__ import annotations

import os
import sys

from . import config

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "hardmode"
LEGACY_VALUE_NAMES = ("cs2-roulette",)   # pre-rename registry value, cleaned up on toggle
IS_WINDOWS = sys.platform.startswith("win")


def launch_command() -> str:
    """How Windows should start the app in tray mode (frozen exe or pythonw + start_tray.pyw)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    root = config.app_root()
    pythonw = os.path.join(root, ".venv", "Scripts", "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    return f'"{pythonw}" "{os.path.join(root, "start_tray.pyw")}"'


def available() -> bool:
    return IS_WINDOWS


def is_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> bool:
    """Returns the resulting state. No-op (False) outside Windows."""
    if not IS_WINDOWS:
        return False
    import winreg  # type: ignore

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, launch_command())
            for legacy in LEGACY_VALUE_NAMES:
                try:
                    winreg.DeleteValue(key, legacy)
                except OSError:
                    pass
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
    return enabled
