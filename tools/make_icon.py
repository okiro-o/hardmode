"""Generate app/static/icon.ico (exe/tray icon) from the tray glyph (needs Pillow)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.tray import icon_image  # noqa: E402

out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static")
img = icon_image(256)
img.save(os.path.join(out, "icon.ico"), sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icons written to", out)
