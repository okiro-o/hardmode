"""In-game overlay: small always-on-top, click-through windows drawn over CS2 (Windows only).

Why a native window and not a browser: a layered Win32 window costs nothing, needs no runtime,
cannot steal focus or clicks (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE) and is drawn by the compositor
over any *windowed* game. Exclusive fullscreen bypasses the compositor, so the game must run in
"Fullscreen Windowed" mode; nothing is injected into the game process (VAC-safe).

Rendering is done with Pillow into an RGBA image (premultiplied BGRA for UpdateLayeredWindow).
Weapon silhouettes come from the same SVGs as the OBS widgets: a tiny path rasterizer flattens
M/L/H/V/C/S/Z commands into polygons (nested contours = holes), supersampled 3x.
The pure rendering functions work on any OS (used by tests and by the panel preview PNG).
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import sys
import threading
import time
from functools import lru_cache
from typing import Callable

from PIL import Image, ImageChops, ImageDraw, ImageFont

from . import config

log = logging.getLogger("hardmode.ingame")
IS_WINDOWS = sys.platform.startswith("win")
_RENDER_LOCK = threading.Lock()   # Pillow font objects are shared (lru_cache) between the overlay thread and HTTP previews
KINDS = ("dpi", "weapons")
SS = 3  # supersampling factor for silhouettes


# ----------------------------------------------------------------------------- fonts
_FONT_CANDIDATES = (
    "segoeuib.ttf", "seguisb.ttf", "arialbd.ttf", "Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
    "DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


@lru_cache(maxsize=64)
def font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    size = max(6, int(size))
    for name in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _text_w(f, text: str) -> float:
    return f.getlength(text)


# ------------------------------------------------------------------- SVG silhouettes
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_SEG = re.compile(r"([MmLlHhVvCcSsZzQqTtAa])([^MmLlHhVvCcSsZzQqTtAa]*)")


def _bezier(p0, p1, p2, p3, steps: int = 8):
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        a, b, c, d = mt * mt * mt, 3 * mt * mt * t, 3 * mt * t * t, t * t * t
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def path_polygons(d: str) -> list[list[tuple[float, float]]]:
    """Flatten an SVG path (M L H V C S Z, absolute/relative) into closed polygons."""
    polys: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    x = y = sx = sy = 0.0
    last_c: tuple[float, float] | None = None

    def flush() -> None:
        nonlocal cur
        if len(cur) >= 3:
            polys.append(cur)
        cur = []

    for m in _SEG.finditer(d):
        cmd, nums = m.group(1), [float(v) for v in _NUM.findall(m.group(2))]
        rel, c = cmd.islower(), cmd.upper()
        if c == "M":
            flush()
            for i in range(0, len(nums) - 1, 2):
                nx, ny = nums[i], nums[i + 1]
                if rel:
                    nx, ny = nx + x, ny + y
                x, y = nx, ny
                if i == 0:
                    sx, sy = x, y
                    cur = [(x, y)]
                else:
                    cur.append((x, y))
            last_c = None
        elif c == "L":
            for i in range(0, len(nums) - 1, 2):
                nx, ny = nums[i], nums[i + 1]
                if rel:
                    nx, ny = nx + x, ny + y
                x, y = nx, ny
                cur.append((x, y))
            last_c = None
        elif c == "H":
            for n in nums:
                x = n + x if rel else n
                cur.append((x, y))
            last_c = None
        elif c == "V":
            for n in nums:
                y = n + y if rel else n
                cur.append((x, y))
            last_c = None
        elif c in ("C", "S"):
            step = 6 if c == "C" else 4
            for i in range(0, len(nums) - step + 1, step):
                g = nums[i:i + step]
                if rel:
                    g = [g[j] + (x if j % 2 == 0 else y) for j in range(step)]
                if c == "C":
                    x1, y1, x2, y2, ex, ey = g
                else:
                    x2, y2, ex, ey = g
                    x1, y1 = (2 * x - last_c[0], 2 * y - last_c[1]) if last_c else (x, y)
                cur.extend(_bezier((x, y), (x1, y1), (x2, y2), (ex, ey)))
                last_c = (x2, y2)
                x, y = ex, ey
        elif c == "Z":
            cur.append((sx, sy))
            flush()
            x, y = sx, sy
            last_c = None
        else:  # Q/T/A are not used by the icon set: fall back to the end point
            if len(nums) >= 2:
                nx, ny = nums[-2], nums[-1]
                if rel:
                    nx, ny = nx + x, ny + y
                x, y = nx, ny
                cur.append((x, y))
            last_c = None
    flush()
    return polys


def _inside(pt: tuple[float, float], poly: list[tuple[float, float]], bbox) -> bool:
    px, py = pt
    if not (bbox[0] <= px <= bbox[2] and bbox[1] <= py <= bbox[3]):
        return False
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > py) != (yj > py):
            xcross = (xj - xi) * (py - yi) / ((yj - yi) or 1e-12) + xi
            if px < xcross:
                inside = not inside
        j = i
    return inside


def _bbox(poly):
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def _icon_file(weapon_id: str) -> str:
    return os.path.join(config.package_dir(), "static", "icons", f"{weapon_id}.svg")


@lru_cache(maxsize=256)
def svg_mask(weapon_id: str, height: int) -> Image.Image | None:
    """Rasterize the weapon silhouette to an 'L' mask of the given height (width from the viewBox)."""
    try:
        with open(_icon_file(weapon_id), encoding="utf-8", errors="ignore") as f:
            svg = f.read()
    except OSError:
        return None
    m = re.search(r'viewBox="\s*([-\d.]+)\s+([-\d.]+)\s+([\d.]+)\s+([\d.]+)', svg)
    if not m:
        return None
    minx, miny, vw, vh = (float(v) for v in m.groups())
    if vw <= 0 or vh <= 0:
        return None
    height = max(4, int(height))
    scale = height * SS / vh
    width = max(1, int(round(vw * scale)))
    mask = Image.new("L", (width, height * SS), 0)
    for d in re.findall(r'\sd="([^"]+)"', svg):
        polys = [[((px - minx) * scale, (py - miny) * scale) for px, py in poly] for poly in path_polygons(d)]
        if not polys:
            continue
        boxes = [_bbox(p) for p in polys]
        depth = [sum(1 for j, q in enumerate(polys) if j != i and _inside(p[0], q, boxes[j])) for i, p in enumerate(polys)]
        layer = Image.new("L", mask.size, 0)
        ld = ImageDraw.Draw(layer)
        for i in sorted(range(len(polys)), key=lambda k: depth[k]):
            ld.polygon(polys[i], fill=255 if depth[i] % 2 == 0 else 0)
        mask = ImageChops.lighter(mask, layer)
    return mask.resize((max(1, width // SS), height), Image.LANCZOS)


def tinted(mask: Image.Image, rgb: tuple[int, int, int], alpha: float = 1.0) -> Image.Image:
    img = Image.new("RGBA", mask.size, rgb + (255,))
    img.putalpha(mask if alpha >= 1 else mask.point(lambda v: int(v * alpha)))
    return img


def _rgb(color: str, default=(255, 255, 255)) -> tuple[int, int, int]:
    c = (color or "").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except (ValueError, IndexError):
        return default


BG = (12, 14, 20, 196)
MUTED = (168, 174, 188, 255)

SAMPLE_STATE = {
    "dpi": {"present": True, "current": 1200, "spinning": False, "home": 3200, "key": "sample"},
    "weapons": {"present": True, "current": {"id": "ak47", "name": "AK-47"},
                "next": [{"id": "m4a1", "name": "M4A4"}, {"id": "awp", "name": "AWP"}],
                "kills": [1, 2], "matched": True, "completed": 3, "key": "sample"},
}


# ----------------------------------------------------------------------- rendering
def render_dpi(st: dict, s: config.IngameSettings, colors: dict) -> Image.Image | None:
    if not st or not st.get("present"):
        return None
    k = float(s.dpi_scale)
    spinning = bool(st.get("spinning"))
    cur = st.get("current")
    if cur is None and not spinning:
        return None
    big, small = font(int(60 * k)), font(int(17 * k))
    text = "…" if spinning else str(cur)
    label = "крутится" if spinning else "DPI"
    pad = int(16 * k)
    tw, lw = _text_w(big, text), _text_w(small, label)
    w, h = int(tw + lw + pad * 3), int(80 * k)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(16 * k), fill=BG)
    home = (not spinning) and cur == st.get("home")
    color = _rgb(colors.get("home_color", "#ffd166")) if home else (255, 255, 255)
    d.text((pad, h / 2), text, font=big, fill=color + (255,), anchor="lm")
    d.text((pad * 2 + tw, h / 2 + 4 * k), label, font=small, fill=MUTED, anchor="lm")
    return img


def render_weapons(st: dict, s: config.IngameSettings, colors: dict) -> Image.Image | None:
    if not st or not st.get("present") or not st.get("current"):
        return None
    k = float(s.weapons_scale)
    H, h2 = int(64 * k), int(38 * k)
    pad, gap = int(14 * k), int(18 * k)
    color = _rgb(colors.get("match_color", "#ff6b6b")) if st.get("matched") else _rgb(colors.get("color", "#ffffff"))
    base = _rgb(colors.get("color", "#ffffff"))
    cur = st["current"]
    nxt = list(st.get("next") or [])[: max(0, int(s.weapons_next))]
    name_f, badge_f = font(int(15 * k)), font(int(13 * k))

    try:
        cur_mask = svg_mask(cur["id"], H)
        next_masks = [(n, svg_mask(n["id"], h2)) for n in nxt]
    except Exception:  # noqa: BLE001 - never lose the whole block because of one silhouette
        log.exception("silhouette rasterization failed, falling back to text")
        cur_mask, next_masks = None, [(n, None) for n in nxt]
    cur_w = cur_mask.size[0] if cur_mask else int(_text_w(name_f, cur["name"]))
    name_h = int(22 * k) if s.weapons_names else 0
    w = pad * 2 + max(cur_w, int(_text_w(name_f, cur["name"])) if s.weapons_names else 0)
    if next_masks:
        w += gap + sum((m.size[0] if m else int(40 * k)) + int(10 * k) for _, m in next_masks)
    h = pad * 2 + H + name_h
    img = Image.new("RGBA", (int(w), int(h)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], radius=int(14 * k), fill=BG)
    x = pad
    if cur_mask:
        img.alpha_composite(tinted(cur_mask, color), (x, pad))
    else:
        d.text((x, pad + H // 2 - int(10 * k)), cur["name"], font=font(int(22 * k)), fill=color + (255,))
    kills = st.get("kills") or [0, 1]
    if len(kills) == 2 and int(kills[1]) > 1:
        badge = f"{kills[0]}/{kills[1]}"
        bw = int(_text_w(badge_f, badge)) + int(10 * k)
        bh = int(18 * k)
        bx, by = x + cur_w - bw, pad + H - bh
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=int(6 * k), fill=_rgb(colors.get("accent", "#ffd166")) + (255,))
        d.text((bx + int(5 * k), by + int(2 * k)), badge, font=badge_f, fill=(20, 22, 28, 255))
    if s.weapons_names:
        d.text((x, pad + H + int(3 * k)), cur["name"], font=name_f, fill=(255, 255, 255, 255))
    x += cur_w + gap
    for n, m in next_masks:
        if m:
            img.alpha_composite(tinted(m, base, 0.5), (int(x), pad + (H - h2) // 2))
            x += m.size[0] + int(10 * k)
        else:
            d.text((x, pad + H // 2), n["name"], font=name_f, fill=MUTED)
            x += int(40 * k) + int(10 * k)
    return img


def render(kind: str, st: dict, s: config.IngameSettings, colors: dict) -> Image.Image | None:
    return render_dpi(st, s, colors) if kind == "dpi" else render_weapons(st, s, colors)


def place(kind: str, s: config.IngameSettings, size: tuple[int, int], mon: tuple[int, int, int, int]) -> tuple[int, int]:
    corner = getattr(s, f"{kind}_corner")
    ox, oy = int(getattr(s, f"{kind}_x")), int(getattr(s, f"{kind}_y"))
    mx, my, mw, mh = mon
    w, h = size
    x = mx + ox if corner in ("tl", "bl") else mx + mw - w - ox
    y = my + oy if corner in ("tl", "tr") else my + mh - h - oy
    return int(x), int(y)


# ------------------------------------------------------------------- Win32 window
if IS_WINDOWS:  # pragma: no cover - exercised only on Windows
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

    class BLENDFUNCTION(ctypes.Structure):
        _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                    ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT), ctypes.POINTER(wintypes.SIZE),
                                           wintypes.HDC, ctypes.POINTER(wintypes.POINT), wintypes.COLORREF,
                                           ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD]
    user32.UpdateLayeredWindow.restype = wintypes.BOOL
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), MONITORENUMPROC, wintypes.LPARAM]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    WS_POPUP = 0x80000000
    WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
    WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x08000000
    SW_HIDE, SW_SHOWNOACTIVATE = 0, 4
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x1, 0x2, 0x10, 0x40
    HWND_TOPMOST = ctypes.c_void_p(-1 & (2 ** (8 * ctypes.sizeof(ctypes.c_void_p)) - 1))
    ULW_ALPHA, AC_SRC_OVER, AC_SRC_ALPHA, PM_REMOVE = 2, 0, 1, 1
    CLASS_NAME = "HardmodeIngameOverlay"

    def set_dpi_aware() -> None:
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except Exception:
            try:
                user32.SetProcessDPIAware()
            except Exception:
                pass

    def monitors() -> list[tuple[int, int, int, int]]:
        rects: list[tuple[int, int, int, int]] = []

        def cb(hmon, hdc, lprc, lparam):
            r = lprc.contents
            rects.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return True

        proc = MONITORENUMPROC(cb)
        try:
            user32.EnumDisplayMonitors(None, None, proc, 0)
        except Exception:
            pass
        rects.sort(key=lambda r: not (r[0] <= 0 < r[0] + r[2] and r[1] <= 0 < r[1] + r[3]))   # primary first
        return rects or [(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))]

    class Win32Layer:
        """One layered window. Create/update/hide from the owning (message-loop) thread only."""

        _wndproc = None

        def __init__(self, title: str) -> None:
            hinst = kernel32.GetModuleHandleW(None)
            if Win32Layer._wndproc is None:
                Win32Layer._wndproc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
                wc = WNDCLASSW()
                wc.lpfnWndProc = Win32Layer._wndproc
                wc.hInstance = hinst
                wc.lpszClassName = CLASS_NAME
                user32.RegisterClassW(ctypes.byref(wc))  # ERROR_CLASS_ALREADY_EXISTS is fine
            ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
            self.hwnd = user32.CreateWindowExW(ex, CLASS_NAME, title, WS_POPUP, 0, 0, 1, 1, None, None, hinst, None)
            if not self.hwnd:
                raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
            self.visible = False

        def update(self, img: Image.Image, x: int, y: int, opacity: float) -> None:
            w, h = img.size
            r, g, b, a = img.convert("RGBA").split()
            data = Image.merge("RGBA", (ImageChops.multiply(b, a), ImageChops.multiply(g, a), ImageChops.multiply(r, a), a)).tobytes()
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth, bmi.bmiHeader.biHeight = w, -h
            bmi.bmiHeader.biPlanes, bmi.bmiHeader.biBitCount, bmi.bmiHeader.biCompression = 1, 32, 0
            hdc_screen = user32.GetDC(None)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            bits = ctypes.c_void_p()
            hbmp = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), 0, ctypes.byref(bits), None, 0)
            try:
                if not hbmp or not bits:
                    raise OSError("CreateDIBSection failed")
                ctypes.memmove(bits, data, len(data))
                old = gdi32.SelectObject(hdc_mem, hbmp)
                pt_dst, size, pt_src = wintypes.POINT(x, y), wintypes.SIZE(w, h), wintypes.POINT(0, 0)
                blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, int(opacity * 255))), AC_SRC_ALPHA)
                if not user32.UpdateLayeredWindow(self.hwnd, hdc_screen, ctypes.byref(pt_dst), ctypes.byref(size),
                                                  hdc_mem, ctypes.byref(pt_src), 0, ctypes.byref(blend), ULW_ALPHA):
                    raise OSError(f"UpdateLayeredWindow failed: {ctypes.get_last_error()}")
                gdi32.SelectObject(hdc_mem, old)
            finally:
                if hbmp:
                    gdi32.DeleteObject(hbmp)
                gdi32.DeleteDC(hdc_mem)
                user32.ReleaseDC(None, hdc_screen)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self.visible = True

        def raise_(self) -> None:
            if self.visible:   # SHOWWINDOW too: re-show if something hid the window behind our back
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW)

        def hide(self) -> None:
            if self.visible:
                user32.ShowWindow(self.hwnd, SW_HIDE)
                self.visible = False

        def destroy(self) -> None:
            try:
                user32.DestroyWindow(self.hwnd)
            except Exception:
                pass

    def pump_messages() -> None:
        msg = wintypes.MSG()
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


# -------------------------------------------------------------------------- manager
class IngameOverlays:
    """Owns the overlay thread. ``state_fn`` returns {"dpi": {...}, "weapons": {...}} (see main.py)."""

    def __init__(self, settings: config.IngameSettings, state_fn: Callable[[], dict], colors_fn: Callable[[], dict]) -> None:
        self.settings = settings
        self._state_fn = state_fn
        self._colors_fn = colors_fn
        self.available = IS_WINDOWS
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._preview_until: dict[str, float] = {k: 0.0 for k in KINDS}
        self._version = 0
        self.visible: dict[str, bool] = {k: False for k in KINDS}
        self.errors: dict[str, str | None] = {k: None for k in KINDS}
        self.present: dict[str, bool] = {k: False for k in KINDS}
        self.reason: dict[str, str] = {k: "" for k in KINDS}

    # -------------------------------------------------------------- control
    def start(self) -> None:
        if not self.available or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ingame-overlay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def update_settings(self, settings: config.IngameSettings) -> None:
        self.settings = settings
        self._version += 1

    def preview(self, kind: str, seconds: float = 5.0) -> None:
        if kind in KINDS:
            self._preview_until[kind] = time.monotonic() + seconds

    def status(self) -> dict:
        return {"available": self.available, "running": bool(self._thread and self._thread.is_alive()),
                "error": self.error, "visible": dict(self.visible), "errors": dict(self.errors),
                "present": dict(self.present), "reason": dict(self.reason)}

    # ------------------------------------------------------------ rendering
    def state_for(self, kind: str, sample_if_empty: bool = True) -> dict:
        try:
            st = (self._state_fn() or {}).get(kind) or {}
        except Exception:
            log.exception("ingame state failed")
            st = {}
        if not st.get("present") and sample_if_empty:
            return dict(SAMPLE_STATE[kind], sample=True)
        return st

    def render(self, kind: str, sample_if_empty: bool = True) -> Image.Image | None:
        st = self.state_for(kind, sample_if_empty)
        with _RENDER_LOCK:
            return render(kind, st, self.settings, self._colors_fn())

    def render_png(self, kind: str) -> bytes:
        img = self.render(kind)
        if img is None:
            img = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    # ---------------------------------------------------------------- thread
    def _run(self) -> None:  # pragma: no cover - Windows only
        try:
            set_dpi_aware()
            layers = {k: Win32Layer(f"Hardmode {k}") for k in KINDS}
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log.exception("ingame overlay: cannot create windows")
            return
        last_sig: dict[str, tuple] = {}
        change_key: dict[str, object] = {}
        shown_until: dict[str, float] = {k: 0.0 for k in KINDS}
        last_raise = 0.0
        mons = monitors()
        last_mon_check = time.monotonic()
        try:
            while not self._stop.is_set():
                pump_messages()
                now = time.monotonic()
                if now - last_mon_check > 10:
                    mons, last_mon_check = monitors(), now
                s = self.settings
                mon = mons[min(max(0, int(s.monitor)), len(mons) - 1)]
                try:
                    state = self._state_fn() or {}
                    colors = self._colors_fn()
                except Exception:
                    log.exception("ingame state failed")
                    state, colors = {}, {}
                for kind in KINDS:
                    layer = layers[kind]
                    st = state.get(kind) or {}
                    previewing = now < self._preview_until[kind]
                    if not st.get("present") and previewing:
                        st = dict(SAMPLE_STATE[kind], sample=True)
                    key = st.get("key")
                    if key != change_key.get(kind, object()):
                        change_key[kind] = key
                        shown_until[kind] = now + float(getattr(s, f"{kind}_show_s"))
                    enabled = bool(getattr(s, f"{kind}_enabled"))
                    mode = getattr(s, f"{kind}_mode")
                    self.present[kind] = bool(st.get("present"))
                    visible = st.get("present") and (previewing or (enabled and (
                        mode == "always" or st.get("hold") or now < shown_until[kind])))
                    if not visible:
                        self.reason[kind] = (st.get("reason") or "нет данных") if not st.get("present") else \
                            ("выключено" if not enabled else "ждёт смены")
                        if self.visible[kind]:
                            log.info("ingame %s: hide (%s)", kind, self.reason[kind])
                        layer.hide()
                        last_sig.pop(kind, None)
                        self.visible[kind] = False
                        continue
                    sig = (json.dumps(st, sort_keys=True, default=str), self._version, mon)
                    if sig != last_sig.get(kind):
                        try:
                            with _RENDER_LOCK:
                                img = render(kind, st, s, colors)
                            self.errors[kind] = None
                        except Exception as e:  # noqa: BLE001
                            self.errors[kind] = f"render: {type(e).__name__}: {e}"
                            log.exception("ingame %s: render failed", kind)
                            img = None
                        if img is None:
                            self.reason[kind] = self.errors[kind] or "нечего рисовать"
                            layer.hide()
                            self.visible[kind] = False
                            continue
                        x, y = place(kind, s, img.size, mon)
                        try:
                            layer.update(img, x, y, float(getattr(s, f"{kind}_opacity")))
                            last_sig[kind] = sig
                            if not self.visible[kind]:
                                log.info("ingame %s: show %dx%d at %d,%d", kind, img.size[0], img.size[1], x, y)
                            self.visible[kind] = True
                            self.reason[kind] = "на экране"
                            self.errors[kind] = None
                        except Exception as e:  # noqa: BLE001
                            self.errors[kind] = f"window: {type(e).__name__}: {e}"
                            self.error = self.errors[kind]
                            log.exception("ingame %s: window update failed", kind)
                if now - last_raise > 2.0:
                    last_raise = now
                    for layer in layers.values():
                        layer.raise_()
                time.sleep(0.1)
        finally:
            for layer in layers.values():
                layer.hide()
                layer.destroy()
