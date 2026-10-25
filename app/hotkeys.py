"""Global hotkeys on Windows.

Primary mechanism: ``RegisterHotKey`` on a dedicated message-loop thread. It is
system-wide, needs no hook/DLL injection (nothing touches cs2.exe), keeps working while
a fullscreen game has focus, and consumes the key so the game never sees it.
Fallback (if a combination is already taken by another program, error 1409):
``GetAsyncKeyState`` polling - the same technique OBS uses for its own hotkeys.

The thread also watches for the one thing that silently breaks global hotkeys on
Windows: UIPI. A non-elevated process receives no input while an elevated window
(Steam/CS2 "run as administrator") is in the foreground. We detect that and expose a
warning so the panel can show it.

On non-Windows platforms the manager reports ``available = False`` and does nothing,
so the server can still be developed/tested elsewhere.
"""
from __future__ import annotations

import ctypes
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

IS_WINDOWS = sys.platform.startswith("win")

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT, WM_USER = 0x0312, 0x0012, 0x0400
WM_REREGISTER = WM_USER + 1
ERROR_HOTKEY_ALREADY_REGISTERED = 1409

_VK: dict[str, int] = {}
for _i in range(1, 25):
    _VK[f"F{_i}"] = 0x6F + _i          # F1 = 0x70
for _i in range(10):
    _VK[f"NUMPAD{_i}"] = 0x60 + _i
    _VK[f"NUM{_i}"] = 0x60 + _i
    _VK[str(_i)] = 0x30 + _i
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _VK[_c] = ord(_c)
_VK.update({
    "ADD": 0x6B, "NUMPAD+": 0x6B, "NUM+": 0x6B, "PLUS": 0x6B, "+": 0x6B,
    "SUBTRACT": 0x6D, "NUMPAD-": 0x6D, "NUM-": 0x6D, "MINUS": 0x6D,
    "MULTIPLY": 0x6A, "NUMPAD*": 0x6A, "NUM*": 0x6A,
    "DIVIDE": 0x6F, "NUMPAD/": 0x6F, "NUM/": 0x6F,
    "DECIMAL": 0x6E, "NUMPAD.": 0x6E,
    "INSERT": 0x2D, "INS": 0x2D, "DELETE": 0x2E, "DEL": 0x2E,
    "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PGUP": 0x21, "PAGEDOWN": 0x22, "PGDN": 0x22,
    "PAUSE": 0x13, "SCROLLLOCK": 0x91, "SCROLL": 0x91, "NUMLOCK": 0x90,
    "SPACE": 0x20, "TAB": 0x09, "BACKSPACE": 0x08, "ENTER": 0x0D,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "`": 0xC0, "TILDE": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, ";": 0xBA, "'": 0xDE,
    ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC,
})
_MODS = {"CTRL": MOD_CONTROL, "CONTROL": MOD_CONTROL, "ALT": MOD_ALT, "SHIFT": MOD_SHIFT, "WIN": MOD_WIN}


def parse_hotkey(spec: str) -> tuple[int, int] | None:
    """'ctrl+alt+F9' -> (modifiers, vk). None if empty/unknown/not a string.

    A trailing '+' belongs to the key name ('numpad+', 'ctrl++'), so '+' only separates
    tokens when something follows it.
    """
    if not isinstance(spec, str) or not spec.strip():
        return None
    mods = 0
    key = None
    for part in re.split(r"\+(?=.)", spec.replace(" ", "")):
        if not part:
            continue
        up = part.upper()
        if up in _MODS:
            mods |= _MODS[up]
        elif key is None:
            key = up
        else:
            return None  # two keys
    if key is None or key not in _VK:
        return None
    return mods, _VK[key]


@dataclass
class HotkeyManager:
    on_action: Callable[[str], None]
    bindings: dict[str, str] = field(default_factory=dict)   # action -> "F9"
    enabled: bool = True
    available: bool = IS_WINDOWS
    status: dict = field(default_factory=dict)              # action -> "ok" | "polling" | "..."
    elevation_warning: str = ""                             # non-empty when UIPI would block input
    on_status: Callable[[], None] | None = None             # called from the thread after (re)registration
    _thread: threading.Thread | None = None
    _thread_id: int | None = None
    _ready: threading.Event = field(default_factory=threading.Event)
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ public
    def start(self) -> None:
        if not self.available:
            self.status = {a: ("не назначено" if not spec else "недоступно (не Windows)")
                           for a, spec in self.bindings.items()}
            return
        if self._thread and self._thread.is_alive():
            self._post(WM_REREGISTER)
            return
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()

    def update(self, bindings: dict[str, str], enabled: bool) -> None:
        with self._lock:
            self.bindings = dict(bindings)
            self.enabled = enabled
        self.start()

    def stop(self) -> None:
        self._stop.set()
        self._post(WM_QUIT)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def describe(self) -> dict:
        return {
            "available": self.available,
            "enabled": self.enabled,
            "bindings": dict(self.bindings),
            "status": dict(self.status),
            "elevation_warning": self.elevation_warning,
        }

    # ----------------------------------------------------------------- windows
    def _post(self, msg: int) -> None:
        if IS_WINDOWS and self._thread_id and self._ready.wait(timeout=2.0):
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, msg, 0, 0)

    def _notify(self) -> None:
        if self.on_status:
            try:
                self.on_status()
            except Exception:
                pass

    def _run(self) -> None:  # pragma: no cover - Windows only
        try:
            self._run_inner()
        except Exception as e:  # keep a readable status instead of a dead thread
            self.status = {a: f"ошибка потока хоткеев: {e!r}" for a in self.bindings}
            self._notify()

    def _run_inner(self) -> None:  # pragma: no cover - Windows only
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._thread_id = kernel32.GetCurrentThreadId()

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint), ("wParam", ctypes.c_size_t),
                        ("lParam", ctypes.c_ssize_t), ("time", ctypes.c_uint), ("pt", POINT)]

        user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
                                        ctypes.c_uint]
        user32.RegisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
        user32.UnregisterHotKey.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetAsyncKeyState.restype = ctypes.c_short
        user32.MsgWaitForMultipleObjects.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
                                                     ctypes.c_uint]

        registered: dict[int, str] = {}      # id -> action
        polled: dict[str, tuple[int, int, bool]] = {}  # action -> (mods, vk, was_down)
        msg = MSG()
        # create the thread message queue before anybody posts to it
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._ready.set()

        def unregister_all() -> None:
            for hid in list(registered):
                user32.UnregisterHotKey(None, hid)
            registered.clear()
            polled.clear()

        def register_all() -> None:
            unregister_all()
            with self._lock:
                bindings = dict(self.bindings)
                enabled = self.enabled
            status: dict[str, str] = {}
            if not enabled:
                self.status = {a: "выключено" for a in bindings}
                self._notify()
                return
            next_id = 1
            for action, spec in bindings.items():
                if not spec:
                    status[action] = "не назначено"
                    continue
                parsed = parse_hotkey(spec)
                if parsed is None:
                    status[action] = f"неизвестная клавиша: {spec}"
                    continue
                mods, vk = parsed
                if user32.RegisterHotKey(None, next_id, mods | MOD_NOREPEAT, vk):
                    registered[next_id] = action
                    status[action] = "ok"
                    next_id += 1
                else:
                    err = ctypes.get_last_error()
                    polled[action] = (mods, vk, False)
                    status[action] = "polling" if err == ERROR_HOTKEY_ALREADY_REGISTERED else f"polling (ошибка {err})"
            self.status = status
            self._notify()

        register_all()
        PM_REMOVE = 0x0001
        QS_ALLINPUT = 0x04FF
        last_elevation_check = 0.0
        while not self._stop.is_set():
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    unregister_all()
                    return
                if msg.message == WM_HOTKEY:
                    action = registered.get(int(msg.wParam))
                    if action:
                        self._fire(action)
                elif msg.message == WM_REREGISTER:
                    register_all()
            if polled:
                for action, (mods, vk, was_down) in list(polled.items()):
                    down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                    if down and not was_down and _mods_down(user32, mods):
                        self._fire(action)
                    polled[action] = (mods, vk, down)
                time.sleep(0.012)
            else:
                user32.MsgWaitForMultipleObjects(0, None, 0, 250, QS_ALLINPUT)
            now = time.monotonic()
            if now - last_elevation_check > 2.0:
                last_elevation_check = now
                warning = _elevation_warning(user32, kernel32)
                if warning != self.elevation_warning:
                    self.elevation_warning = warning
                    self._notify()
        unregister_all()

    def _fire(self, action: str) -> None:
        try:
            self.on_action(action)
        except Exception:  # never let a callback kill the hotkey thread
            pass


def _mods_down(user32, mods: int) -> bool:  # pragma: no cover - Windows only
    checks = ((MOD_CONTROL, 0x11), (MOD_ALT, 0x12), (MOD_SHIFT, 0x10), (MOD_WIN, 0x5B))
    for flag, vk in checks:
        if mods & flag and not (user32.GetAsyncKeyState(vk) & 0x8000):
            return False
    return True


def _is_elevated(advapi32, handle) -> bool | None:  # pragma: no cover - Windows only
    TOKEN_QUERY = 0x0008
    TokenElevation = 20
    token = ctypes.c_void_p()
    if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        elevation = ctypes.c_uint(0)
        size = ctypes.c_uint(0)
        ok = advapi32.GetTokenInformation(token, TokenElevation, ctypes.byref(elevation),
                                          ctypes.sizeof(elevation), ctypes.byref(size))
        return bool(elevation.value) if ok else None
    finally:
        ctypes.windll.kernel32.CloseHandle(token)


def _elevation_warning(user32, kernel32) -> str:  # pragma: no cover - Windows only
    """Non-empty text when the foreground window belongs to an elevated process and we are not."""
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi32.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)]
        advapi32.GetTokenInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
                                                 ctypes.POINTER(ctypes.c_uint)]
        me = _is_elevated(advapi32, kernel32.GetCurrentProcess())
        if me:
            return ""
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = ctypes.c_uint(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value == kernel32.GetCurrentProcessId():
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            other = _is_elevated(advapi32, handle)
        finally:
            kernel32.CloseHandle(handle)
        if other:
            return ("Активное окно запущено от имени администратора: хоткеи в нём не сработают. "
                    "Запустите run.bat от администратора или не запускайте Steam/CS2 от админа.")
        return ""
    except Exception:
        return ""
