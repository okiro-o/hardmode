"""DPI roulette: picks a value, animates it on the overlay, applies it to the mouse.

Server-authoritative like the weapon roulette: the server chooses the result and the exact
animation parameters, the overlay only replays them, so an OBS reload mid-spin still lands
on the right number. Applying goes through a thread (HID I/O blocks); a guard thread re-applies
the value if G HUB silently reverts it (profile switch, mouse wake-up).
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import threading
import time
from dataclasses import asdict, dataclass, fields
from typing import Any, Callable

from . import grid
from .backends import DpiBackend, make_backend
from .hidpp import HidppError

MAX_HISTORY = 50
MAX_LOG = 40


def _int(v: Any, name: str) -> int:
    try:
        if isinstance(v, bool):
            raise TypeError
        return int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: нужно число") from None


def _float(v: Any, name: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: нужно число") from None
    if f != f:
        raise ValueError(f"{name}: нужно число")
    return f


@dataclass
class DpiSettings:
    enabled: bool = True
    min_dpi: int = 800
    max_dpi: int = 3200
    step: int = 100
    spin_duration_ms: int = 5000
    revolutions: int = 5
    apply_at: str = "stop"          # "stop" (when the wheel rests) | "start" (immediately)
    auto_on_death: bool = True
    death_cooldown_s: float = 8.0   # ignore deaths this soon after the previous spin
    exclude_current: bool = True    # never land on the value already applied
    guard: bool = True              # re-apply if G HUB reverts the DPI
    use_range: bool = True          # min..max/step values are part of the wheel
    custom_values: list = None      # manual extra values (snapped to the sensor grid)
    pool_mode: str = "bag"          # "bag": a value leaves the wheel once rolled, until the cycle is exhausted | "random"
    viewer_enabled: bool = False    # viewers add values through a channel points reward (chat is read anonymously)
    viewer_min: int = 400
    viewer_max: int = 6400
    viewer_max_values: int = 20     # how many viewer values may wait on the wheel
    viewer_per_user: int = 1        # pending values per viewer
    viewer_reward_id: str = ""      # Twitch custom reward id (learned from the first redemption)
    viewer_command: str = ""        # optional chat command, e.g. "!dpi" (empty = rewards only)
    viewer_announce: bool = True    # confirm additions in chat (through the StreamElements bot)
    home_dpi: int = 3200            # the streamer's normal DPI ("вернуть мой DPI"), highlighted on the wheel
    home_color: str = "#ff6b6b"     # overlay: colour of the home-DPI segment
    backend: str = "auto"           # auto | hidpp | fake | none
    wheel_mode: str = "spin"        # overlay: "spin" = wheel only while spinning, "always" = wheel always visible
    wheel_linger_s: float = 2.5     # overlay: keep the wheel on screen this long after it rests (spin mode)
    idle_hide_s: float = 0.0        # overlay: hide the number too, N seconds after a spin (0 = keep it)
    show_current: bool = True       # overlay: show the current value between spins

    @classmethod
    def from_dict(cls, d: dict | None) -> "DpiSettings":
        if d is not None and not isinstance(d, dict):
            raise ValueError("dpi: нужен объект")
        s = cls()
        names = {f.name for f in fields(cls)}
        for k, v in (d or {}).items():
            if k in names:
                setattr(s, k, v)
        s.min_dpi = max(100, min(44000, _int(s.min_dpi, "min_dpi")))
        s.max_dpi = max(100, min(44000, _int(s.max_dpi, "max_dpi")))
        if s.max_dpi < s.min_dpi:
            s.min_dpi, s.max_dpi = s.max_dpi, s.min_dpi
        s.step = max(1, min(10000, _int(s.step, "step")))
        s.spin_duration_ms = max(500, min(30000, _int(s.spin_duration_ms, "spin_duration_ms")))
        s.revolutions = max(1, min(20, _int(s.revolutions, "revolutions")))
        s.death_cooldown_s = max(0.0, min(300.0, _float(s.death_cooldown_s, "death_cooldown_s")))
        s.home_dpi = max(100, min(44000, _int(s.home_dpi, "home_dpi")))
        vals = s.custom_values if isinstance(s.custom_values, list) else []
        cleaned: list[int] = []
        for v in vals:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if 100 <= iv <= 44000:
                cleaned.append(grid.snap(iv))
        s.custom_values = sorted(set(cleaned))[:60]
        if s.pool_mode not in ("bag", "random"):
            s.pool_mode = "bag"
        s.viewer_min = max(100, min(44000, _int(s.viewer_min, "viewer_min")))
        s.viewer_max = max(100, min(44000, _int(s.viewer_max, "viewer_max")))
        if s.viewer_max < s.viewer_min:
            s.viewer_min, s.viewer_max = s.viewer_max, s.viewer_min
        s.viewer_max_values = max(1, min(60, _int(s.viewer_max_values, "viewer_max_values")))
        s.viewer_per_user = max(1, min(10, _int(s.viewer_per_user, "viewer_per_user")))
        s.viewer_reward_id = s.viewer_reward_id.strip()[:64] if isinstance(s.viewer_reward_id, str) else ""
        s.viewer_command = s.viewer_command.strip()[:20] if isinstance(s.viewer_command, str) else ""
        s.idle_hide_s = max(0.0, min(3600.0, _float(s.idle_hide_s, "idle_hide_s")))
        s.wheel_linger_s = max(0.0, min(60.0, _float(s.wheel_linger_s, "wheel_linger_s")))
        if not (isinstance(s.home_color, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", s.home_color.strip())):
            raise ValueError("home_color: нужен цвет вида #rrggbb")
        s.home_color = s.home_color.strip().lower()
        if s.wheel_mode not in ("spin", "always"):
            s.wheel_mode = "spin"
        for b in ("enabled", "auto_on_death", "exclude_current", "guard", "show_current", "use_range",
                  "viewer_enabled", "viewer_announce"):
            v = getattr(s, b)
            if isinstance(v, str):
                v = v.strip().lower() not in ("", "0", "false", "no", "off", "нет")
            setattr(s, b, bool(v))
        if s.apply_at not in ("stop", "start"):
            s.apply_at = "stop"
        if s.backend not in ("auto", "hidpp", "fake", "none"):
            s.backend = "auto"
        return s

    def to_dict(self) -> dict:
        d = asdict(self)
        d["custom_values"] = list(self.custom_values or [])
        return d


class DpiService:
    def __init__(self, settings: DpiSettings, state_path: str,
                 publish: Callable[[str, dict], None], publish_threadsafe: Callable[[str, dict], None]) -> None:
        self.settings = settings
        self.state_path = state_path
        self._publish = publish
        self._publish_threadsafe = publish_threadsafe
        self.backend: DpiBackend = make_backend(settings.backend)
        self.current_dpi: int | None = None      # last value confirmed by the mouse
        self.target_dpi: int | None = None       # value we want on the mouse (guard re-applies it)
        self.last_spin: dict | None = None
        self.history: list[dict] = []
        self.log: list[dict] = []
        self.spins = 0
        self.deaths = 0
        self.guard_reapplies = 0
        self.last_error: str | None = None
        self.mouse: dict = {"found": False}
        self.busy = False
        self.consumed: list[int] = []           # bag mode: values already rolled in this cycle
        self.viewer_values: list[dict] = []     # [{value, user, t}] one-shot values added by viewers
        self.learn_reward = False               # next reward redemption seen in chat becomes the reward id
        self.on_applied: Callable[[int, str], None] | None = None   # external hook (chat notifications)
        self.on_viewer_added: Callable[[dict], None] | None = None  # external hook (chat confirmation)
        self._last_spin_at = 0.0
        self._pending_apply: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._guard_thread: threading.Thread | None = None
        self._guard_stop = threading.Event()
        self._apply_lock = threading.Lock()
        self._rng = random.SystemRandom()
        self._load()

    # ------------------------------------------------------------ lifecycle
    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._guard_stop.clear()
        self._guard_thread = threading.Thread(target=self._guard_loop, name="dpi-guard", daemon=True)
        self._guard_thread.start()

    def stop(self) -> None:
        self._guard_stop.set()
        if self._pending_apply:
            self._pending_apply.cancel()
        try:
            self.backend.close()
        except Exception:
            pass
        self._save()

    # -------------------------------------------------------------- helpers
    def _log(self, kind: str, text: str, **extra: Any) -> None:
        self.log.append({"t": time.time(), "kind": kind, "text": text, **extra})
        del self.log[:-MAX_LOG]

    def _changed(self) -> None:
        self._save()
        self._publish("dpi", self.snapshot())

    def _allowed(self) -> list[int] | None:
        try:
            return self.backend.allowed_values()
        except Exception:
            return None

    def base_values(self) -> list[int]:
        """Values that belong to the wheel permanently: range (optional) + manual list."""
        s = self.settings
        allowed = self._allowed()
        vals: set[int] = set()
        if s.use_range:
            vals.update(grid.wheel_values(s.min_dpi, s.max_dpi, s.step, allowed))
        for v in s.custom_values or []:
            vals.add(grid.snap(v, allowed))
        return sorted(vals)

    def values(self) -> list[int]:
        """What is on the wheel right now: base minus consumed (bag mode) plus viewer values."""
        base = self.base_values()
        if self.settings.pool_mode == "bag":
            remaining = [v for v in base if v not in self.consumed]
            if not remaining and base:
                self.consumed = []        # cycle exhausted: everything comes back
                remaining = list(base)
            base = remaining
        allowed = self._allowed()
        viewer = [grid.snap(int(e["value"]), allowed) for e in self.viewer_values]
        wheel = sorted(set(base) | set(viewer))
        return wheel or [grid.snap(self.settings.home_dpi, allowed)]

    def wheel_info(self) -> dict:
        base = self.base_values()
        return {"base_total": len(base), "consumed": sorted(v for v in self.consumed if v in base),
                "viewer": list(self.viewer_values), "on_wheel": len(self.values())}

    def _consume(self, value: int) -> None:
        """Book-keeping after a spin landed on ``value``."""
        if self.settings.pool_mode == "bag" and value in self.base_values() and value not in self.consumed:
            self.consumed.append(value)
        for i, e in enumerate(self.viewer_values):
            if int(e["value"]) == value:
                del self.viewer_values[i]     # one viewer entry per roll
                break

    def reset_cycle(self) -> None:
        self.consumed = []
        self._log("cycle", "Круг колеса сброшен: все значения снова на колесе")
        self._changed()

    # ------------------------------------------------------------ viewer values
    def add_viewer_value(self, value: Any, user: str, source: str = "reward") -> dict:
        """Validate and add a viewer's DPI. Returns {ok, value|error}."""
        s = self.settings
        try:
            iv = int(str(value).strip().replace(" ", ""))
        except (TypeError, ValueError):
            return {"ok": False, "error": "не число"}
        if not (s.viewer_min <= iv <= s.viewer_max):
            return {"ok": False, "error": f"вне диапазона {s.viewer_min}–{s.viewer_max}"}
        iv = grid.snap(iv, self._allowed())
        user = (user or "?")[:40]
        if len(self.viewer_values) >= s.viewer_max_values:
            return {"ok": False, "error": "колесо переполнено"}
        if sum(1 for e in self.viewer_values if e.get("user", "").lower() == user.lower()) >= s.viewer_per_user:
            return {"ok": False, "error": "у тебя уже есть значение на колесе"}
        entry = {"value": iv, "user": user, "t": time.time(), "source": source}
        self.viewer_values.append(entry)
        self._log("viewer", f"{user} добавил {iv} DPI на колесо ({source})", value=iv, user=user)
        if self.on_viewer_added:
            try:
                self.on_viewer_added(entry)
            except Exception:
                pass
        self._changed()
        return {"ok": True, "value": iv, "user": user}

    def remove_viewer_value(self, index: int) -> bool:
        if 0 <= index < len(self.viewer_values):
            e = self.viewer_values.pop(index)
            self._log("viewer", f"Убрано зрительское значение {e['value']} ({e.get('user')})")
            self._changed()
            return True
        return False

    def clear_viewer_values(self) -> None:
        self.viewer_values = []
        self._log("viewer", "Зрительские значения очищены")
        self._changed()

    def add_custom_value(self, value: Any) -> dict:
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return {"ok": False, "error": "нужно число"}
        if not 100 <= iv <= 44000:
            return {"ok": False, "error": "от 100 до 44000"}
        iv = grid.snap(iv, self._allowed())
        vals = sorted(set((self.settings.custom_values or []) + [iv]))[:60]
        self.update_settings({"custom_values": vals})
        return {"ok": True, "value": iv}

    def remove_custom_value(self, value: Any) -> None:
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return
        self.update_settings({"custom_values": [v for v in (self.settings.custom_values or []) if v != iv]})
        if iv in self.consumed:
            self.consumed.remove(iv)

    def snapshot(self) -> dict:
        return {
            "enabled": self.settings.enabled,
            "backend": self.backend.name,
            "mouse": self.mouse,
            "current_dpi": self.current_dpi,
            "target_dpi": self.target_dpi,
            "values": self.values(),
            "wheel": self.wheel_info(),
            "learn_reward": self.learn_reward,
            "last_spin": self.last_spin,
            "history": self.history[-20:],
            "log": self.log[-20:],
            "spins": self.spins,
            "deaths": self.deaths,
            "guard_reapplies": self.guard_reapplies,
            "last_error": self.last_error,
            "busy": self.busy,
            "settings": self.settings.to_dict(),
            "now": time.time(),
        }

    # --------------------------------------------------------------- spinning
    def pick(self) -> tuple[int, int, list[int]]:
        vals = self.values()
        options = list(range(len(vals)))
        if self.settings.exclude_current and self.current_dpi in vals and len(vals) > 1:
            options = [i for i in options if vals[i] != self.current_dpi]
        idx = self._rng.choice(options)
        return idx, vals[idx], vals

    def spin(self, reason: str = "manual") -> dict:
        """Start a spin: choose the result, publish the animation, schedule the apply."""
        if not self.settings.enabled:
            raise ValueError("DPI-рулетка выключена в настройках")
        idx, value, vals = self.pick()
        instant = len(vals) <= 1          # nothing to spin: the number just changes on screen
        self.spins += 1
        self._last_spin_at = time.monotonic()
        spin = {
            "id": self.spins,
            "index": idx,
            "value": value,
            "values": vals,
            "duration_ms": 0 if instant else self.settings.spin_duration_ms,
            "revolutions": 0 if instant else self.settings.revolutions,
            "instant": instant,
            "offset": round(self._rng.uniform(0.15, 0.85), 3),   # where inside the segment the pointer rests
            "started_at": time.time(),
            "reason": reason,
            "apply_at": self.settings.apply_at,
            "applied": None,
        }
        self.last_spin = spin
        self._consume(value)
        if instant:
            self._log("spin", f"{value} DPI без кручения: на колесе одно значение ({reason})", value=value, reason=reason)
        else:
            self._log("spin", f"Крутим: {value} DPI ({reason})", value=value, reason=reason)
        self._publish("dpi_spin", spin)
        if self._pending_apply:
            self._pending_apply.cancel()
            self._pending_apply = None
        if instant or self.settings.apply_at == "start":
            self._schedule_apply(value, f"spin:{reason}", delay=0.0)
        else:
            self._schedule_apply(value, f"spin:{reason}", delay=self.settings.spin_duration_ms / 1000.0)
        self._changed()
        return spin

    def on_death(self) -> bool:
        """Called by the GSI pipeline. Returns True if a spin was started."""
        self.deaths += 1
        if not (self.settings.enabled and self.settings.auto_on_death):
            return False
        if time.monotonic() - self._last_spin_at < self.settings.death_cooldown_s:
            self._log("skip", "Смерть, но рулетка только что крутилась — пропуск")
            self._changed()
            return False
        try:
            self.spin(reason="death")
            return True
        except ValueError:
            return False

    # ---------------------------------------------------------------- apply
    def _schedule_apply(self, value: int, reason: str, delay: float) -> None:
        if self._loop is None:
            return

        def fire() -> None:
            self._pending_apply = None
            asyncio.ensure_future(self.apply(value, reason), loop=self._loop)

        if delay <= 0:
            self._loop.call_soon(fire)
        else:
            self._pending_apply = self._loop.call_later(delay, fire)

    async def apply(self, value: int, reason: str = "manual") -> dict:
        """Apply a DPI value to the mouse (blocking I/O runs in a worker thread)."""
        loop = asyncio.get_running_loop()
        self.busy = True
        self.target_dpi = int(value)
        self._publish("dpi", self.snapshot())
        try:
            actual = await loop.run_in_executor(None, self._apply_blocking, int(value))
            self.current_dpi = actual
            self.last_error = None
            if self.last_spin and self.last_spin.get("value") == actual and reason.startswith("spin"):
                self.last_spin["applied"] = time.time()
            self.history.append({"t": time.time(), "value": actual, "reason": reason})
            del self.history[:-MAX_HISTORY]
            self._log("apply", f"DPI {actual} применён ({reason})", value=actual)
            result = {"ok": True, "value": actual}
            if self.on_applied:
                try:
                    self.on_applied(actual, reason)
                except Exception:
                    pass
        except Exception as e:  # HidppError or anything from hidapi
            self.last_error = str(e)
            self._log("error", f"Не удалось применить DPI {value}: {e}")
            result = {"ok": False, "error": str(e), "value": int(value)}
        finally:
            self.busy = False
        self._changed()
        return result

    def _apply_blocking(self, value: int) -> int:
        with self._apply_lock:
            return self.backend.set_dpi(value)

    async def probe(self) -> dict:
        loop = asyncio.get_running_loop()
        self.busy = True
        self._publish("dpi", self.snapshot())
        try:
            info = await loop.run_in_executor(None, self._probe_blocking)
        finally:
            self.busy = False
        self.mouse = info
        if info.get("found"):
            self.current_dpi = info.get("dpi", self.current_dpi)
            self.last_error = None
            self._log("probe", f"Мышь найдена: {info.get('name') or info.get('product')} · режим {info.get('mode_text')} · DPI {info.get('dpi')}")
        else:
            self.last_error = info.get("error")
            self._log("error", f"Мышь не найдена: {info.get('error')}")
        self._changed()
        return info

    def _probe_blocking(self) -> dict:
        with self._apply_lock:
            try:
                return self.backend.probe()
            except Exception as e:
                return {"found": False, "error": str(e)}

    async def set_host_mode(self) -> dict:
        loop = asyncio.get_running_loop()
        fn = getattr(self.backend, "set_host_mode", None)
        if fn is None:
            return {"ok": False, "error": "backend не поддерживает смену режима"}
        try:
            res = await loop.run_in_executor(None, lambda: self._locked(fn))
            self._log("mode", "Мышь переведена в host-режим" if res.get("switched") else "Мышь уже в host-режиме")
            self._changed()
            return {"ok": True, **res}
        except Exception as e:
            self.last_error = str(e)
            self._changed()
            return {"ok": False, "error": str(e)}

    def _locked(self, fn):
        with self._apply_lock:
            return fn()

    # ---------------------------------------------------------------- guard
    def _guard_loop(self) -> None:
        """Re-apply the target DPI if the mouse reports something else (G HUB fought back)."""
        last_reapply = 0.0
        strikes = 0
        failures = 0          # consecutive reads without an answer (mouse asleep/unplugged) -> back off
        while not self._guard_stop.wait(3.0 if failures < 3 else min(60.0, 3.0 * 2 ** (failures - 2))):
            if not (self.settings.enabled and self.settings.guard) or self.target_dpi is None or self.busy:
                continue
            if self._pending_apply is not None:
                continue   # a spin is still animating: do not touch the mouse until it rests
            if self.backend.name == "none":
                continue
            try:
                with self._apply_lock:
                    current = self.backend.get_dpi()
            except Exception:
                failures += 1
                continue
            if current is None:
                failures += 1
                continue
            failures = 0
            if current != self.current_dpi and current == self.target_dpi:
                self.current_dpi = current
                self._publish_threadsafe("dpi", self.snapshot())
            if current != self.target_dpi:
                now = time.monotonic()
                if now - last_reapply < 5.0 or strikes >= 20:
                    continue
                last_reapply = now
                try:
                    with self._apply_lock:
                        actual = self.backend.set_dpi(self.target_dpi)
                    self.current_dpi = actual
                    self.guard_reapplies += 1
                    strikes += 1
                    self._log("guard", f"G HUB вернул {current} DPI — снова применил {actual}")
                    self._publish_threadsafe("dpi", self.snapshot())
                except Exception as e:
                    self.last_error = str(e)
            else:
                strikes = 0

    # ------------------------------------------------------------- settings
    def update_settings(self, patch: dict) -> None:
        old = self.settings
        new = DpiSettings.from_dict({**old.to_dict(), **(patch or {})})   # may raise ValueError
        self.settings = new
        if new.backend != old.backend:
            try:
                self.backend.close()
            except Exception:
                pass
            self.backend = make_backend(new.backend)
            self.mouse = {"found": False}
        self._changed()

    # ---------------------------------------------------------- persistence
    def _save(self) -> None:
        try:
            tmp = self.state_path + ".tmp"
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"current_dpi": self.current_dpi, "target_dpi": self.target_dpi,
                           "last_spin": self.last_spin, "history": self.history, "spins": self.spins,
                           "deaths": self.deaths, "log": self.log, "consumed": self.consumed,
                           "viewer_values": self.viewer_values}, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except OSError:
            pass

    def _load(self) -> None:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(d, dict):
            return
        self.current_dpi = d.get("current_dpi") if isinstance(d.get("current_dpi"), int) else None
        self.target_dpi = d.get("target_dpi") if isinstance(d.get("target_dpi"), int) else None
        self.last_spin = d.get("last_spin") if isinstance(d.get("last_spin"), dict) else None
        self.history = [h for h in (d.get("history") or []) if isinstance(h, dict)][-MAX_HISTORY:]
        self.log = [e for e in (d.get("log") or []) if isinstance(e, dict)][-MAX_LOG:]
        self.spins = int(d.get("spins", 0) or 0) if isinstance(d.get("spins", 0), int) else 0
        self.deaths = int(d.get("deaths", 0) or 0) if isinstance(d.get("deaths", 0), int) else 0
        self.consumed = [int(v) for v in (d.get("consumed") or []) if isinstance(v, (int, float))]
        self.viewer_values = [e for e in (d.get("viewer_values") or [])
                              if isinstance(e, dict) and isinstance(e.get("value"), (int, float))][:60]
