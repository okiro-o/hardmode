"""Paths and persisted application settings."""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
from dataclasses import asdict, dataclass, field, fields

APP_NAME = "hardmode"
DEFAULT_PORT = 17890

_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
LAYOUTS = ("horizontal", "vertical")
ALIGNS = ("left", "center", "right", "top", "middle", "bottom")


def app_root() -> str:
    """Directory that contains the ``app`` package (works both from source and PyInstaller)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def package_dir() -> str:
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        return os.path.join(base, "app")
    return os.path.dirname(os.path.abspath(__file__))


def data_dir() -> str:
    """Writable folder for settings/state. ``CS2R_DATA`` overrides; default ``<root>/data``."""
    override = os.environ.get("CS2R_DATA")
    path = override or os.path.join(app_root(), "data")
    os.makedirs(path, exist_ok=True)
    return path


def settings_path() -> str:
    return os.path.join(data_dir(), "settings.json")


def state_path() -> str:
    return os.path.join(data_dir(), "state.json")


def dpi_state_path() -> str:
    return os.path.join(data_dir(), "dpi_state.json")


# ----------------------------------------------------------------- shared helpers
# Every settings dataclass (here, roulette.py, chat.py, modules/dpi/service.py) and every JSON state
# file use these, so the lenient parsing and the atomic write live in exactly one place.

def to_bool(v) -> bool:
    """Lenient boolean for settings coming from JSON/forms ("no", "0", "false" are False)."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off", "нет")
    return bool(v)


def to_int(v, name: str) -> int:
    """Strict int for settings: bools and junk raise ``ValueError("<name>: нужно число")``."""
    try:
        if isinstance(v, bool):
            raise TypeError
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: нужно число") from None


def to_float(v, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: нужно число") from None
    if f != f:   # NaN
        raise ValueError(f"{name}: нужно число")
    return f


def require_object(d, label: str) -> None:
    if d is not None and not isinstance(d, dict):
        raise ValueError(f"{label}: нужен объект")


def assign_fields(obj, d, label: str) -> dict:
    """setattr every key of ``d`` that is a dataclass field of ``obj``; return the unknown keys."""
    require_object(d, label)
    names = {f.name for f in fields(obj)}
    unknown: dict = {}
    for k, v in (d or {}).items():
        if k in names:
            setattr(obj, k, v)
        else:
            unknown[k] = v
    return unknown


def write_json_atomic(path: str, data, indent: int | None = None) -> None:
    """dump to ``path + ".tmp"`` then ``os.replace``: a crash never leaves a half-written file."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)


@dataclass
class OverlaySettings:
    color: str = "#ffffff"        # silhouette colour
    accent: str = "#ffd166"       # highlight colour (current weapon glow / counter)
    scale: float = 1.0
    layout: str = "horizontal"    # horizontal | vertical
    labels: bool = False          # show weapon names under silhouettes
    show_counter: bool = True     # "completed weapons" badge
    show_kills: bool = True       # x/y kills badge on the current weapon (only if kills_per_weapon > 1)
    show_match: bool = True       # recolour the current weapon when it is the one in hand
    match_color: str = "#ff6b6b"  # colour used for that highlight
    align: str = "left"           # left | center | right (horizontal) == top | middle | bottom (vertical)
    loadout_layout: str = "rows"  # loadout overlay: "rows" (category per row) | "columns" (like the in-game screen)
    loadout_names: bool = True    # loadout overlay: weapon names under silhouettes

    def apply(self, patch: dict | None) -> list[str]:
        """Apply a validated patch. Returns a list of rejected keys (invalid values)."""
        require_object(patch, "overlay")
        rejected: list[str] = []
        for k, v in (patch or {}).items():
            if k in ("color", "accent", "match_color"):
                if isinstance(v, str) and _COLOR_RE.match(v.strip()):
                    setattr(self, k, v.strip().lower())
                else:
                    rejected.append(k)
            elif k == "scale":
                try:
                    f = float(v)
                except (TypeError, ValueError):
                    rejected.append(k)
                    continue
                if f != f:  # NaN
                    rejected.append(k)
                    continue
                self.scale = max(0.3, min(4.0, f))
            elif k == "layout":
                if v in LAYOUTS:
                    self.layout = v
                else:
                    rejected.append(k)
            elif k == "loadout_layout":
                if v in ("rows", "columns"):
                    self.loadout_layout = v
                else:
                    rejected.append(k)
            elif k == "align":
                if v in ALIGNS:
                    self.align = v
                else:
                    rejected.append(k)
            elif k in ("labels", "show_counter", "show_kills", "show_match", "loadout_names"):
                setattr(self, k, to_bool(v))
            else:
                rejected.append(k)
        return rejected


CORNERS = ("tl", "tr", "bl", "br")


@dataclass
class IngameSettings:
    """In-game overlay (always-on-top windows over CS2, Windows only). Keys are flat: <kind>_<option>."""
    dpi_enabled: bool = True
    dpi_mode: str = "change"        # change: a few seconds after each change | always
    dpi_show_s: float = 6.0
    dpi_corner: str = "tl"
    dpi_x: int = 24
    dpi_y: int = 100
    dpi_scale: float = 1.0
    dpi_opacity: float = 0.9
    weapons_enabled: bool = True
    weapons_mode: str = "change"
    weapons_show_s: float = 6.0
    weapons_corner: str = "tl"
    weapons_x: int = 24
    weapons_y: int = 200
    weapons_scale: float = 1.0
    weapons_opacity: float = 0.9
    weapons_next: int = 2           # how many upcoming weapons to show (0-3)
    weapons_names: bool = True
    monitor: int = 0                # 0 = primary

    # option suffix -> validation; the same rules apply to the dpi_* and weapons_* keys
    _BOOLS = ("enabled", "names")
    _CHOICES = {"mode": ("change", "always"), "corner": CORNERS}
    _FLOATS = {"show_s": (1.0, 120.0), "scale": (0.5, 3.0), "opacity": (0.2, 1.0)}
    _INTS = {"x": (0, 8000), "y": (0, 8000), "next": (0, 3)}

    def apply(self, patch: dict | None) -> list[str]:
        require_object(patch, "ingame")
        rejected: list[str] = []
        for k, v in (patch or {}).items():
            try:
                if k == "monitor":
                    self.monitor = max(0, min(8, int(float(v))))
                    continue
                kind, _, opt = k.partition("_")
                if kind not in ("dpi", "weapons") or k not in self.__dataclass_fields__:
                    raise ValueError(k)
                if opt in self._BOOLS:
                    val = to_bool(v)
                elif opt in self._CHOICES:
                    if v not in self._CHOICES[opt]:
                        raise ValueError(k)
                    val = v
                elif opt in self._FLOATS:
                    lo, hi = self._FLOATS[opt]
                    val = max(lo, min(hi, to_float(v, k)))
                elif opt in self._INTS:
                    lo, hi = self._INTS[opt]
                    val = max(lo, min(hi, int(float(v))))
                else:
                    raise ValueError(k)
                setattr(self, k, val)
            except (TypeError, ValueError):
                rejected.append(k)
        return rejected


@dataclass
class AppSettings:
    port: int = DEFAULT_PORT
    gsi_token: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    roulette: dict = field(default_factory=dict)      # RouletteSettings as dict
    dpi: dict = field(default_factory=dict)           # DpiSettings as dict (validated by the module)
    chat: dict = field(default_factory=dict)          # ChatSettings as dict (contains the SE JWT secret)
    donations: dict = field(default_factory=dict)     # DonationSettings as dict (contains DA/DonatePay secrets)
    overlay: OverlaySettings = field(default_factory=OverlaySettings)
    ingame: IngameSettings = field(default_factory=IngameSettings)
    open_browser: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "AppSettings":
        s = cls()
        if not isinstance(d, dict):
            return s
        try:
            port = int(d.get("port", s.port) or s.port)
            if 1024 <= port <= 65535:
                s.port = port
        except (TypeError, ValueError):
            pass
        tok = d.get("gsi_token")
        if isinstance(tok, str) and tok.strip():
            s.gsi_token = tok.strip()
        s.roulette = dict(d.get("roulette") or {}) if isinstance(d.get("roulette"), dict) else {}
        s.dpi = dict(d.get("dpi") or {}) if isinstance(d.get("dpi"), dict) else {}
        s.chat = dict(d.get("chat") or {}) if isinstance(d.get("chat"), dict) else {}
        s.donations = dict(d.get("donations") or {}) if isinstance(d.get("donations"), dict) else {}
        s.open_browser = to_bool(d.get("open_browser", True))
        if isinstance(d.get("overlay"), dict):
            s.overlay.apply(d["overlay"])
        if isinstance(d.get("ingame"), dict):
            s.ingame.apply(d["ingame"])
        return s

    def save(self, path: str | None = None) -> None:
        write_json_atomic(path or settings_path(), self.to_dict(), indent=2)

    @classmethod
    def load(cls, path: str | None = None) -> "AppSettings":
        path = path or settings_path()
        try:
            with open(path, encoding="utf-8") as f:
                s = cls.from_dict(json.load(f))
        except FileNotFoundError:
            s = cls()
            s.save(path)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            # corrupted file: keep a backup, regenerate (and persist, so the token stays stable)
            try:
                os.replace(path, path + ".broken")
            except OSError:
                pass
            s = cls()
            try:
                s.save(path)
            except OSError:
                pass
        env_port = os.environ.get("CS2R_PORT")
        if env_port and env_port.isdigit() and 1024 <= int(env_port) <= 65535:
            s.port = int(env_port)
        return s
