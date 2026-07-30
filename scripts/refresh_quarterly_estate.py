#!/usr/bin/env python3
"""Thin executable wrapper for :mod:`src.acquisition.cli`.

Scheduling belongs outside the engine. Cron, launchd, CI, or an operator may
invoke this same command without changing acquisition behavior.
"""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.acquisition.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
