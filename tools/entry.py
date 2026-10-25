"""PyInstaller entry point (see build.bat)."""
import os
import sys
import traceback

# make `app` importable when frozen
sys.path.insert(0, getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))))

from app.__main__ import main  # noqa: E402

if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0) and getattr(sys, "frozen", False):
            input("Нажмите Enter, чтобы закрыть...")
        raise
    except Exception:
        traceback.print_exc()
        if getattr(sys, "frozen", False):
            input("Нажмите Enter, чтобы закрыть...")
        raise
