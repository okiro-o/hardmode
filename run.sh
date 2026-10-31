#!/usr/bin/env bash
# Dev launcher for macOS/Linux (mouse control, tray and the in-game overlay are Windows-only).
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -r requirements.txt; }
exec .venv/bin/python -m app "$@"
