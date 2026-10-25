"""Re-download the official CS2 weapon silhouettes (plain white single-path SVGs).

Source: Juknum/counter-strike-icons (auto-extracted daily from the game's pak01_dir.vpk).
Usage:  python tools/fetch_icons.py
"""
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import weapons as W  # noqa: E402

BASE = "https://raw.githubusercontent.com/Juknum/counter-strike-icons/main/cs2/panorama/images/icons/equipment/"
EXTRA = ["knife_t", "hegrenade", "flashbang", "smokegrenade", "molotov", "incgrenade", "decoy", "c4"]
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static", "icons")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    names = [w.id for w in W.CATALOG] + EXTRA
    for name in names:
        url = BASE + name + ".svg"
        dst = os.path.join(OUT, name + ".svg")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as r, open(dst, "wb") as f:
                f.write(r.read())
            print("ok  ", name)
        except Exception as e:  # noqa: BLE001
            print("FAIL", name, e)


if __name__ == "__main__":
    main()
