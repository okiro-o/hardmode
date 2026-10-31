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


def _field_names(cls) -> set[str]:
    return {f.name for f in fields(cls)}


def _to_bool(v) -> bool:
    """Lenient boolean for settings coming from JSON/forms ("no", "0", "false" are False)."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "no", "off", "нет")
    return bool(v)


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
        if patch is not None and not isinstance(patch, dict):
            raise ValueError("overlay: нужен объект")
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
                setattr(self, k, _to_bool(v))
            else:
                rejected.append(k)
        return rejected


@dataclass
class HotkeySettings:
    next: str = "F9"
    reroll: str = "F8"
    undo: str = "F7"
    dpi_spin: str = "F6"
    enabled: bool = False       # off by default: kills/deaths drive everything through GSI

    ACTIONS = ("next", "reroll", "undo", "dpi_spin")

    def apply(self, patch: dict | None) -> list[str]:
        """Apply a validated patch (strings only, parseable by hotkeys.parse_hotkey)."""
        from .hotkeys import parse_hotkey  # local import: keep config importable standalone

        if patch is not None and not isinstance(patch, dict):
            raise ValueError("hotkeys: нужен объект")
        rejected: list[str] = []
        for k, v in (patch or {}).items():
            if k == "enabled":
                self.enabled = _to_bool(v)
            elif k in self.ACTIONS:
                spec = (v or "").strip() if isinstance(v, str) else None
                if spec is None or (spec and parse_hotkey(spec) is None):
                    rejected.append(k)
                else:
                    setattr(self, k, spec)
            else:
                rejected.append(k)
        # duplicates would make two actions fire on one key
        seen: dict[str, str] = {}
        for action in self.ACTIONS:
            spec = getattr(self, action).upper().replace(" ", "")
            if spec and spec in seen:
                setattr(self, action, "")
                rejected.append(action)
            elif spec:
                seen[spec] = action
        return rejected

    def bindings(self) -> dict[str, str]:
        return {a: getattr(self, a) for a in self.ACTIONS}


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

    def apply(self, patch: dict | None) -> list[str]:
        if patch is not None and not isinstance(patch, dict):
            raise ValueError("ingame: нужен объект")
        rejected: list[str] = []
        for k, v in (patch or {}).items():
            try:
                if k in ("dpi_enabled", "weapons_enabled", "weapons_names"):
                    setattr(self, k, _to_bool(v))
                elif k in ("dpi_mode", "weapons_mode"):
                    if v not in ("change", "always"):
                        raise ValueError(k)
                    setattr(self, k, v)
                elif k in ("dpi_corner", "weapons_corner"):
                    if v not in CORNERS:
                        raise ValueError(k)
                    setattr(self, k, v)
                elif k in ("dpi_show_s", "weapons_show_s"):
                    f = float(v)
                    if f != f:
                        raise ValueError(k)
                    setattr(self, k, max(1.0, min(120.0, f)))
                elif k in ("dpi_x", "dpi_y", "weapons_x", "weapons_y"):
                    setattr(self, k, max(0, min(8000, int(float(v)))))
                elif k in ("dpi_scale", "weapons_scale"):
                    f = float(v)
                    if f != f:
                        raise ValueError(k)
                    setattr(self, k, max(0.5, min(3.0, f)))
                elif k in ("dpi_opacity", "weapons_opacity"):
                    f = float(v)
                    if f != f:
                        raise ValueError(k)
                    setattr(self, k, max(0.2, min(1.0, f)))
                elif k == "weapons_next":
                    self.weapons_next = max(0, min(3, int(float(v))))
                elif k == "monitor":
                    self.monitor = max(0, min(8, int(float(v))))
                else:
                    raise ValueError(k)
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
    overlay: OverlaySettings = field(default_factory=OverlaySettings)
    hotkeys: HotkeySettings = field(default_factory=HotkeySettings)
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
        s.open_browser = _to_bool(d.get("open_browser", True))
        if isinstance(d.get("overlay"), dict):
            s.overlay.apply(d["overlay"])
        if isinstance(d.get("hotkeys"), dict):
            s.hotkeys.apply(d["hotkeys"])
        if isinstance(d.get("ingame"), dict):
            s.ingame.apply(d["ingame"])
        return s

    def save(self, path: str | None = None) -> None:
        path = path or settings_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

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
