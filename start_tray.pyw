"""Tray-mode launcher for pythonw.exe (no console window). Used by run.bat and autostart."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if "--tray" not in sys.argv:
    sys.argv.append("--tray")

from app.__main__ import main  # noqa: E402

main()
