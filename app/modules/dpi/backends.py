"""DPI backends: how a chosen DPI value reaches the mouse.

* ``HidppBackend`` - talks HID++ 2.0 to the mouse directly (primary path, Windows + hidapi).
* ``FakeBackend``  - in-memory mouse for development/tests (``CS2R_DPI_BACKEND=fake``).
* ``NoneBackend``  - nothing available (non-Windows without the fake backend).

All backends are synchronous and may block for a few ms; the service calls them from a
thread. ``set_dpi`` returns the value actually applied (snapped to the sensor grid).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from typing import Protocol

from . import grid
from .hidpp import (
    HOST_MODE,
    Candidate,
    DeviceNotFound,
    HidppDevice,
    HidppError,
    enumerate_candidates,
)

IS_WINDOWS = sys.platform.startswith("win")


class DpiBackend(Protocol):
    name: str

    def probe(self) -> dict: ...
    def get_dpi(self) -> int | None: ...
    def set_dpi(self, value: int) -> int: ...
    def allowed_values(self) -> list[int]: ...
    def close(self) -> None: ...


class NoneBackend:
    name = "none"

    def __init__(self, reason: str = "DPI backend недоступен") -> None:
        self.reason = reason

    def probe(self) -> dict:
        return {"found": False, "error": self.reason}

    def get_dpi(self) -> int | None:
        return None

    def set_dpi(self, value: int) -> int:
        raise HidppError(self.reason)

    def allowed_values(self) -> list[int]:
        return list(grid.DEFAULT_VALUES)

    def close(self) -> None:
        pass


class FakeBackend:
    """Simulated Superlight 2: snaps to the real grid, remembers the value, optional 'G HUB' revert."""
    name = "fake"

    def __init__(self, dpi: int = 800, revert_after: float = 0.0) -> None:
        self._dpi = dpi
        self._mode = HOST_MODE
        self._revert_after = revert_after
        self._applied_at = 0.0
        self._profile_dpi = dpi
        self.calls: list[int] = []

    def probe(self) -> dict:
        return {"found": True, "product": "Fake Superlight 2", "name": "PRO X 2 (fake)", "mode": self._mode,
                "mode_text": "host", "dpi": self.get_dpi(), "lod": 2, "protocol": "4.2", "receiver": True}

    def get_dpi(self) -> int | None:
        if self._revert_after and self._applied_at and time.monotonic() - self._applied_at > self._revert_after:
            self._dpi = self._profile_dpi
            self._applied_at = 0.0
        return self._dpi

    def set_dpi(self, value: int) -> int:
        v = grid.snap(int(value))
        self._dpi = v
        self._applied_at = time.monotonic()
        self.calls.append(v)
        return v

    def allowed_values(self) -> list[int]:
        return list(grid.DEFAULT_VALUES)

    def close(self) -> None:
        pass


class HidppBackend:
    """Direct HID++ control. Reconnects lazily; safe to call from any thread."""
    name = "hidpp"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._dev: HidppDevice | None = None
        self._values: list[int] | None = None
        self._info: dict = {}

    # ------------------------------------------------------------------ connect
    def _connect(self) -> HidppDevice:
        with self._lock:
            if self._dev and self._dev.is_open:
                return self._dev
            cands = enumerate_candidates()
            if not cands:
                raise DeviceNotFound("Logitech-устройства с HID++ не найдены (мышь выключена? hidapi?)")
            errors: list[str] = []
            for cand in cands:
                indexes = [1, 2, 3] if cand.is_receiver else [0xFF]
                for di in indexes:
                    dev = HidppDevice(cand, device_index=di)
                    try:
                        dev.open()
                        fid, _ = dev.dpi_feature()  # 0x2202 or 0x2201; also proves the index is alive
                        self._dev = dev
                        self._info = {"candidate": cand.describe(), "device_index": di, "feature": f"0x{fid:04X}"}
                        return dev
                    except HidppError as e:
                        label = cand.product or f"{cand.product_id:#06x}"
                        errors.append(f"{label}@{di}: {e}")
                        dev.close()
            raise DeviceNotFound("Мышь с управлением DPI (0x2202/0x2201) не найдена: " + "; ".join(errors[-3:]))

    def _drop(self) -> None:
        with self._lock:
            if self._dev:
                self._dev.close()
            self._dev = None

    def close(self) -> None:
        self._drop()

    # ------------------------------------------------------------------ public
    def probe(self) -> dict:
        with self._lock:
            try:
                dev = self._connect()
                info = dict(self._info)
                try:
                    info["protocol"] = ".".join(str(x) for x in dev.protocol_version())
                except HidppError:
                    info["protocol"] = "?"
                try:
                    info["name"] = dev.device_name()
                except HidppError:
                    info["name"] = ""
                mode = dev.get_onboard_mode() if dev.has_feature(0x8100) else None
                params = dev.dpi_parameters()
                info.update({
                    "found": True,
                    "product": info.get("candidate", {}).get("product", ""),
                    "receiver": info.get("candidate", {}).get("receiver"),
                    "mode": mode,
                    "mode_text": "host" if mode == HOST_MODE else ("onboard" if mode is not None else "n/a"),
                    "feature": params.get("feature", info.get("feature")),
                    "dpi": params["dpi_x"],
                    "dpi_y": params["dpi_y"],
                    "lod": params["lod"],
                    "grid": f"{len(self.allowed_values())} значений, {self.allowed_values()[0]}–{self.allowed_values()[-1]}",
                })
                return info
            except HidppError as e:
                self._drop()
                return {"found": False, "error": str(e)}

    def allowed_values(self) -> list[int]:
        with self._lock:
            if self._values:
                return self._values
            try:
                dev = self._connect()
                self._values = dev.dpi_ranges()
            except HidppError:
                return list(grid.DEFAULT_VALUES)
            return self._values

    def get_dpi(self) -> int | None:
        with self._lock:
            try:
                return self._connect().dpi_parameters()["dpi_x"]
            except HidppError:
                self._drop()
                return None

    def set_dpi(self, value: int) -> int:
        with self._lock:
            v = grid.snap(int(value), self.allowed_values())
            try:
                dev = self._connect()
                dev.ensure_host_mode()
                params = dev.set_dpi(v)
            except HidppError:
                # one retry after a reconnect (receiver re-enumerated, mouse woke up, ...)
                self._drop()
                dev = self._connect()
                dev.ensure_host_mode()
                params = dev.set_dpi(v)
            if params["dpi_x"] != v:
                raise HidppError(f"Мышь ответила DPI {params['dpi_x']} вместо {v}")
            return params["dpi_x"]

    def set_host_mode(self) -> dict:
        with self._lock:
            dev = self._connect()
            switched = dev.ensure_host_mode()
            return {"switched": switched, "mode": dev.get_onboard_mode()}


def make_backend(kind: str = "auto") -> DpiBackend:
    kind = (kind or "auto").lower()
    env = os.environ.get("CS2R_DPI_BACKEND", "").lower()
    if env:
        kind = env
    if kind == "fake":
        return FakeBackend()
    if kind == "none":
        return NoneBackend("DPI-модуль выключен")
    if kind in ("auto", "hidpp"):
        try:
            import hid  # noqa: F401
        except ImportError as e:
            if getattr(sys, "frozen", False):
                return NoneBackend(f"В сборке нет модуля hidapi ({e}). Пересоберите build.bat: сборка не должна "
                                   f"писать «Hidden import 'hid' not found», а в dist\\cs2-roulette\\_internal должен лежать hid*.pyd")
            if IS_WINDOWS:
                return NoneBackend(f"Не установлен модуль hidapi: выполните  .venv\\Scripts\\pip install hidapi  ({e})")
            return NoneBackend("hidapi не установлен (на этой ОС управление мышью не используется)")
        return HidppBackend()
    return NoneBackend(f"Неизвестный backend {kind!r}")
