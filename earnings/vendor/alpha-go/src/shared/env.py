"""Minimal ``.env`` loader (no third-party dependency).

Reads ``<project root>/.env`` once per process and injects ``KEY=VALUE`` pairs into
``os.environ`` **without overriding** already-set variables (a real shell export always wins).
Kept dependency-free on purpose — the only secrets today are LLM API keys used by the opt-in
sentiment/Q&A layers, and the file is gitignored.
"""
from __future__ import annotations

import functools
import os

from src.shared.paths import PROJECT_ROOT

ENV_PATH = PROJECT_ROOT / ".env"


@functools.lru_cache(maxsize=1)
def load_env() -> None:
    """Load ``.env`` into the environment once (idempotent, no-op if the file is absent)."""
    if not ENV_PATH.exists():
        return
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)
