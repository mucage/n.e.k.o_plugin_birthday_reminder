"""Shared sys.path setup for the plugin's pytest/unittest tests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUB = Path(__file__).resolve().parent / "stub"

for path in (str(ROOT.parent), str(STUB)):
    if path not in sys.path:
        sys.path.insert(0, path)
