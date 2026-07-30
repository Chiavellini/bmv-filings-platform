"""Tiny persistent JSON cache for LLM results, keyed by (model, text).

The linear search view renders many occurrences; LLM-scoring each one every rerun would be slow
and costly. This caches results on disk under ``<project root>/.llm_cache/<namespace>/`` (already
gitignored) so a passage is scored once and re-reads are instant. Best-effort — any I/O error is
swallowed (the caller recomputes).
"""
from __future__ import annotations

import hashlib
import json

from src.shared.paths import PROJECT_ROOT

_CACHE_ROOT = PROJECT_ROOT / ".llm_cache"


def _key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode("utf-8")).hexdigest()


def _path(namespace: str, model: str, text: str):
    return _CACHE_ROOT / namespace / f"{_key(model, text)}.json"


def get(namespace: str, model: str, text: str) -> dict | None:
    """Return the cached dict for (model, text), or ``None`` on miss/error."""
    p = _path(namespace, model, text)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return None


def put(namespace: str, model: str, text: str, value: dict) -> None:
    """Persist ``value`` for (model, text). Silently no-ops on I/O error."""
    p = _path(namespace, model, text)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value), encoding="utf-8")
    except OSError:
        pass
