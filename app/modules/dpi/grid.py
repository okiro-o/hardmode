"""DPI grid of the HERO 2 sensor (G PRO X Superlight 2) and snapping helpers.

The sensor only accepts values on a piecewise grid (step 1 up to 200 DPI, step 2 up to 500,
step 5 up to 1000, step 10 up to 2000, step 20 up to 5000, ...). The real list is read from
the mouse (feature 0x2202, getSensorDpiRanges); this fallback matches what was read from a
Superlight 2 with firmware 32.3+.
"""
from __future__ import annotations

from bisect import bisect_left

# (range_start, range_end, step) - inclusive ends, ranges chain into each other
DEFAULT_RANGES: tuple[tuple[int, int, int], ...] = (
    (100, 200, 1),
    (200, 500, 2),
    (500, 1000, 5),
    (1000, 2000, 10),
    (2000, 5000, 20),
    (5000, 10000, 50),
    (10000, 20000, 100),
    (20000, 32000, 125),
    (32000, 44000, 200),
)

STEP_FLAG = 0xE000   # in getSensorDpiRanges a word with the top three bits set encodes a step
STEP_MASK = 0x1FFF


def expand_ranges(ranges: tuple[tuple[int, int, int], ...] | list[tuple[int, int, int]]) -> list[int]:
    values: set[int] = set()
    for start, end, step in ranges:
        if step <= 0 or end < start:
            continue
        v = start
        while v <= end:
            values.add(v)
            v += step
        values.add(end)
    return sorted(values)


def parse_dpi_words(words: list[int]) -> list[int]:
    """Decode the getSensorDpiRanges word list into the sorted list of legal DPI values.

    Same algorithm as Solaar's ``produce_dpi_list``: plain words are values; a word with
    ``word >> 13 == 0b111`` is a step and the following word is the last value of the range
    ``previous + step .. last``. A zero word terminates the list.
    """
    values: list[int] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w == 0:
            break
        if (w >> 13) == 0b111:
            step = w & STEP_MASK
            last = words[i + 1] if i + 1 < len(words) else 0
            if values and step > 0 and last > values[-1]:
                values.extend(range(values[-1] + step, last + 1, step))
            i += 2
            continue
        values.append(w)
        i += 1
    return sorted(set(v for v in values if 0 < v < STEP_FLAG))


DEFAULT_VALUES: list[int] = expand_ranges(DEFAULT_RANGES)


def snap(value: int, allowed: list[int] | None = None) -> int:
    """Nearest allowed DPI to ``value`` (clamped to the ends of the grid)."""
    candidates = allowed or DEFAULT_VALUES
    if not candidates:
        return int(value)
    i = bisect_left(candidates, value)
    if i == 0:
        return candidates[0]
    if i >= len(candidates):
        return candidates[-1]
    before, after = candidates[i - 1], candidates[i]
    return before if value - before <= after - value else after


def wheel_values(min_dpi: int, max_dpi: int, step: int, allowed: list[int] | None = None,
                 max_segments: int = 60) -> list[int]:
    """Segment values for the wheel: min..max every ``step``, snapped to the sensor grid."""
    min_dpi, max_dpi = sorted((int(min_dpi), int(max_dpi)))
    step = max(1, int(step))
    out: list[int] = []
    v = min_dpi
    while v <= max_dpi and len(out) < max_segments:
        s = snap(v, allowed)
        if not out or s != out[-1]:
            out.append(s)
        v += step
    last = snap(max_dpi, allowed)
    if out and last != out[-1] and len(out) < max_segments:
        out.append(last)
    return out or [snap(min_dpi, allowed)]
