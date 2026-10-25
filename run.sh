#!/usr/bin/env bash
# Dev launcher for macOS/Linux (hotkeys are Windows-only; everything else works).
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -r requirements.txt; }
exec .venv/bin/python -m app "$@"
