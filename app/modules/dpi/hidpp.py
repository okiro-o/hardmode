"""Minimal HID++ 2.0 client for Logitech gaming mice (G PRO X Superlight 2).

Only what the DPI roulette needs:

* ROOT (0x0000)                getFeature()            -> feature index lookup
* DEVICE NAME (0x0005)         getDeviceName()         -> diagnostics ("PRO X 2")
* ONBOARD PROFILES (0x8100)    get/setOnboardMode()    -> the sensor only accepts DPI writes in
                                                          HOST mode (2); ONBOARD mode (1) refuses
* EXTENDED ADJUSTABLE DPI (0x2202)
      fn2 getSensorDpiRanges   -> the legal DPI grid
      fn5 getSensorDpiParameters -> current X/Y DPI + LOD
      fn6 setSensorDpiParameters -> apply

Transport: hidapi (``pip install hidapi``). On Windows every HID top-level collection is its
own path; HID++ lives on usage page 0xFF00 (usage 1 = short 7-byte reports 0x10, usage 2 =
long 20-byte reports 0x11). Vendor collections are opened with shared access, so this works
while G HUB is running. Nothing here touches the game.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import grid

VENDOR_LOGITECH = 0x046D
PID_LIGHTSPEED_RECEIVER_C54D = 0xC54D   # Superlight 2 / newer LIGHTSPEED receiver
PID_SUPERLIGHT2_WIRED = 0xC09B
KNOWN_PIDS = (PID_LIGHTSPEED_RECEIVER_C54D, PID_SUPERLIGHT2_WIRED, 0xC547, 0xC539)  # + older receivers

REPORT_SHORT, REPORT_LONG = 0x10, 0x11
LEN_SHORT, LEN_LONG = 7, 20
HIDPP_USAGE_PAGE = 0xFF00
DEVICE_INDEX_WIRED = 0xFF
SOFTWARE_ID = 0x0C          # 4-bit tag so we recognise our own replies

FEATURE_ROOT = 0x0000
FEATURE_DEVICE_NAME = 0x0005
FEATURE_ONBOARD_PROFILES = 0x8100
FEATURE_EXT_ADJUSTABLE_DPI = 0x2202   # HERO 2 mice (Superlight 2, G502 X, ...)
FEATURE_ADJUSTABLE_DPI = 0x2201       # older mice (G304/G305, G Pro Wireless, G403, ...)

ERROR_CODES = {
    0x01: "unknown",
    0x02: "invalid argument",
    0x03: "out of range",
    0x04: "hardware error",
    0x05: "not allowed (mouse is in onboard mode?)",
    0x06: "invalid feature index",
    0x07: "invalid function",
    0x08: "busy / device not connected",
    0x09: "unsupported",
}
HOST_MODE = 0x02


class HidppError(RuntimeError):
    pass


class DeviceNotFound(HidppError):
    pass


def _hid():
    try:
        import hid  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise HidppError("Модуль hidapi не установлен (pip install hidapi)") from e
    return hid


@dataclass
class Candidate:
    """One physical Logitech device = one or more HID paths (short/long collections)."""
    vendor_id: int
    product_id: int
    product: str
    paths: dict[int, bytes] = field(default_factory=dict)   # usage -> path
    serial: str = ""

    @property
    def is_receiver(self) -> bool:
        return self.product_id in (PID_LIGHTSPEED_RECEIVER_C54D, 0xC547, 0xC539) or "receiver" in self.product.lower()

    def describe(self) -> dict:
        return {"vendor_id": f"{self.vendor_id:04X}", "product_id": f"{self.product_id:04X}", "product": self.product,
                "usages": sorted(self.paths), "receiver": self.is_receiver}


def enumerate_candidates() -> list[Candidate]:
    """All Logitech devices exposing HID++ vendor collections, known PIDs first."""
    hid = _hid()
    found: dict[tuple[int, int, str], Candidate] = {}
    for d in hid.enumerate(VENDOR_LOGITECH, 0):
        if d.get("usage_page") != HIDPP_USAGE_PAGE:
            continue
        key = (d["vendor_id"], d["product_id"], str(d.get("serial_number") or ""))
        c = found.setdefault(key, Candidate(d["vendor_id"], d["product_id"], str(d.get("product_string") or ""),
                                            serial=str(d.get("serial_number") or "")))
        c.paths[int(d.get("usage") or 0)] = d["path"]
    cands = list(found.values())
    cands.sort(key=lambda c: (c.product_id not in KNOWN_PIDS, c.product_id != PID_LIGHTSPEED_RECEIVER_C54D))
    return cands


class HidppDevice:
    """A HID++ 2.0 conversation with one device (through a receiver or wired)."""

    def __init__(self, candidate: Candidate, device_index: int | None = None, timeout: float = 1.0) -> None:
        self.candidate = candidate
        self.device_index = device_index if device_index is not None else (1 if candidate.is_receiver else DEVICE_INDEX_WIRED)
        self.timeout = timeout
        self._handles: list = []
        self._lock = threading.RLock()
        self._feature_cache: dict[int, int] = {}
        self._swid = SOFTWARE_ID

    # ------------------------------------------------------------- transport
    def open(self) -> None:
        hid = _hid()
        with self._lock:
            self.close()
            paths = [self.candidate.paths[u] for u in sorted(self.candidate.paths)]
            if not paths:
                raise DeviceNotFound("У устройства нет HID++ коллекций")
            for path in paths:
                h = hid.device()
                h.open_path(path)
                h.set_nonblocking(True)
                self._handles.append(h)

    def close(self) -> None:
        with self._lock:
            for h in self._handles:
                try:
                    h.close()
                except Exception:
                    pass
            self._handles = []
            self._feature_cache = {}
            self._dpi_feature = None

    @property
    def is_open(self) -> bool:
        return bool(self._handles)

    def _write(self, data: bytes) -> None:
        # Write the long report through the long collection when we have it; hidapi needs the
        # report id as the first byte (Windows wants the exact report length).
        target = None
        for h, usage in zip(self._handles, sorted(self.candidate.paths)):
            if (data[0] == REPORT_LONG and usage == 2) or (data[0] == REPORT_SHORT and usage == 1):
                target = h
                break
        target = target or self._handles[-1]
        n = target.write(data)
        if n is not None and n < 0:
            raise HidppError("HID write failed")

    def _read_any(self, deadline: float) -> bytes | None:
        while time.monotonic() < deadline:
            for h in self._handles:
                try:
                    data = h.read(64)
                except Exception as e:
                    raise HidppError(f"HID read failed: {e}") from e
                if data:
                    return bytes(data)
            time.sleep(0.004)
        return None

    def request(self, feature_index: int, function: int, params: bytes = b"", long: bool = True) -> bytes:
        """Send one HID++ 2.0 request and return the response parameters (bytes after the header)."""
        with self._lock:
            if not self._handles:
                self.open()
            self._swid = (self._swid % 15) + 1  # 1..15, never 0
            fn_byte = ((function & 0x0F) << 4) | self._swid
            header = bytes([REPORT_LONG if long else REPORT_SHORT, self.device_index & 0xFF, feature_index & 0xFF, fn_byte])
            size = LEN_LONG if long else LEN_SHORT
            payload = header + params
            if len(payload) > size:
                raise HidppError("params too long")
            payload = payload + bytes(size - len(payload))
            self._write(payload)
            deadline = time.monotonic() + self.timeout
            while True:
                data = self._read_any(deadline)
                if data is None:
                    raise HidppError("Мышь не ответила (спит? выключена? нет связи с ресивером?)")
                if len(data) < 4 or data[1] != (self.device_index & 0xFF):
                    continue
                # HID++ 2.0 error: feature byte 0xFF, then [feature_index, fn|swid, error]
                if data[2] == 0xFF and len(data) >= 6 and data[3] == (feature_index & 0xFF) and data[4] == fn_byte:
                    code = data[5]
                    raise HidppError(f"HID++ error 0x{code:02X}: {ERROR_CODES.get(code, 'unknown')}")
                # HID++ 1.0 style error (receivers): 0x8F
                if data[2] == 0x8F and len(data) >= 6 and data[3] == (feature_index & 0xFF) and data[4] == fn_byte:
                    code = data[5]
                    raise HidppError(f"HID++ 1.0 error 0x{code:02X} (device not connected / wrong index?)")
                if data[2] == (feature_index & 0xFF) and data[3] == fn_byte:
                    return data[4:]
                # anything else (notifications, other software's traffic): keep waiting

    # --------------------------------------------------------------- features
    def feature_index(self, feature_id: int) -> int:
        if feature_id == FEATURE_ROOT:
            return 0
        idx = self._feature_cache.get(feature_id)
        if idx is not None:
            return idx
        resp = self.request(0x00, 0, bytes([(feature_id >> 8) & 0xFF, feature_id & 0xFF]))
        idx = resp[0]
        if idx == 0:
            raise HidppError(f"Фича 0x{feature_id:04X} не поддерживается этой мышью")
        self._feature_cache[feature_id] = idx
        return idx

    def protocol_version(self) -> tuple[int, int]:
        resp = self.request(0x00, 1, bytes([0, 0, 0x90]))
        return resp[0], resp[1]

    def device_name(self) -> str:
        try:
            idx = self.feature_index(FEATURE_DEVICE_NAME)
        except HidppError:
            return ""
        count = self.request(idx, 0)[0]
        name = b""
        while len(name) < count:
            chunk = self.request(idx, 1, bytes([len(name)]))
            chunk = chunk.split(b"\x00", 1)[0]
            if not chunk:
                break
            name += chunk
        return name[:count].decode("ascii", "replace").strip()

    # onboard profiles ---------------------------------------------------------
    def get_onboard_mode(self) -> int:
        idx = self.feature_index(FEATURE_ONBOARD_PROFILES)
        return self.request(idx, 2)[0]

    def set_onboard_mode(self, mode: int) -> None:
        idx = self.feature_index(FEATURE_ONBOARD_PROFILES)
        self.request(idx, 1, bytes([mode & 0xFF]))

    def has_feature(self, feature_id: int) -> bool:
        try:
            self.feature_index(feature_id)
            return True
        except HidppError:
            return False

    def ensure_host_mode(self) -> bool:
        """Returns True if the mode had to be switched. Mice without onboard profiles need nothing."""
        if not self.has_feature(FEATURE_ONBOARD_PROFILES):
            return False
        if self.get_onboard_mode() == HOST_MODE:
            return False
        self.set_onboard_mode(HOST_MODE)
        return True

    # DPI: 0x2202 Extended Adjustable DPI, or 0x2201 Adjustable DPI on older mice --------------
    _dpi_feature: tuple[int, int] | None = None

    def dpi_feature(self) -> tuple[int, int]:
        """(feature id, feature index) of the DPI feature this mouse speaks."""
        if self._dpi_feature is None:
            for fid in (FEATURE_EXT_ADJUSTABLE_DPI, FEATURE_ADJUSTABLE_DPI):
                try:
                    self._dpi_feature = (fid, self.feature_index(fid))
                    break
                except HidppError:
                    continue
            else:
                raise HidppError("Мышь не поддерживает управление DPI по HID++ (нет фич 0x2202/0x2201)")
        return self._dpi_feature

    def dpi_parameters(self, sensor: int = 0) -> dict:
        """{dpi_x, default_x, dpi_y, default_y, lod, feature}"""
        fid, idx = self.dpi_feature()
        if fid == FEATURE_EXT_ADJUSTABLE_DPI:
            r = self.request(idx, 5, bytes([sensor]))
            return {
                "sensor": r[0],
                "dpi_x": (r[1] << 8) | r[2],
                "default_x": (r[3] << 8) | r[4],
                "dpi_y": (r[5] << 8) | r[6],
                "default_y": (r[7] << 8) | r[8],
                "lod": r[9],
                "feature": "0x2202",
            }
        # 0x2201 getSensorDpi(sensorIdx) -> [sensorIdx, dpi(2), defaultDpi(2)]
        r = self.request(idx, 2, bytes([sensor]))
        dpi = (r[1] << 8) | r[2]
        default = (r[3] << 8) | r[4]
        return {"sensor": r[0], "dpi_x": dpi, "default_x": default, "dpi_y": dpi, "default_y": default,
                "lod": None, "feature": "0x2201"}

    def set_dpi(self, dpi: int, lod: int | None = None, sensor: int = 0, dpi_y: int | None = None) -> dict:
        fid, idx = self.dpi_feature()
        if fid == FEATURE_EXT_ADJUSTABLE_DPI:
            if lod is None or lod == 0:
                current = self.dpi_parameters(sensor)
                lod = current["lod"] or 2
            y = dpi if dpi_y is None else dpi_y
            params = bytes([sensor, (dpi >> 8) & 0xFF, dpi & 0xFF, (y >> 8) & 0xFF, y & 0xFF, lod & 0xFF])
            self.request(idx, 6, params)
        else:
            # 0x2201 setSensorDpi(sensorIdx, dpi)
            self.request(idx, 3, bytes([sensor, (dpi >> 8) & 0xFF, dpi & 0xFF]))
        return self.dpi_parameters(sensor)

    def dpi_ranges(self, sensor: int = 0, direction: int = 0) -> list[int]:
        """Legal DPI values read from the device (falls back to the known grid on failure).

        getSensorDpiRanges(sensorIdx, direction, dpiRangeReqIdx): the list is split over several
        replies (pages); each reply echoes the three request bytes and carries 16-bit words after
        them. The list ends with a 0x0000 word (Solaar: produce_dpi_list). A word with the top
        three bits set encodes a step between the neighbouring values.
        """
        fid, idx = self.dpi_feature()
        # 0x2202 fn2 getSensorDpiRanges(sensor, direction, page) echoes 3 bytes;
        # 0x2201 fn1 getSensorDpiList(sensor, ..., page) echoes 1 byte (Solaar: ignore=3 / ignore=1)
        fn, skip = (2, 3) if fid == FEATURE_EXT_ADJUSTABLE_DPI else (1, 1)
        data = b""
        words: list[int] = []
        for page in range(0, 32):
            try:
                r = self.request(idx, fn, bytes([sensor, direction, page]))
            except HidppError:
                break
            body = bytes(r[skip:])       # payload bytes per page form one continuous byte stream
            if not body or not any(body):
                break
            data += body
            words = [(data[i] << 8) | data[i + 1] for i in range(0, len(data) - 1, 2)]
            if 0 in words:               # word-aligned terminator seen
                break
        values = grid.parse_dpi_words(words)
        return values or list(grid.DEFAULT_VALUES)
