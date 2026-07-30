#!/usr/bin/env python3
"""
parse_cache.py — transparent on-disk cache for parsed PDF table blocks.

The Tier-2 table extractor re-opens every sibling PDF with pdfplumber and
recomputes its table blocks on every eval run; for a full ``compare_extractions
all`` that is hundreds of PDF parses and 10+ minutes of pure I/O, none of it
dependent on the metric config. This module caches the *parsed structure* (the
``list[TableBlock]`` a PDF yields) keyed on the file's identity, so re-runs skip
the pdfplumber work entirely.

Design contract — the cache must be invisible:
  * It stores only the parsed blocks, never matched metrics. Any change to the
    semantic dictionary, scale, or metric defs still re-runs against fresh-loaded
    blocks, so results are identical to the uncached path.
  * It is keyed on (absolute path, mtime_ns, size). Touch/replace a PDF and it
    re-parses. A ``VERSION`` bump invalidates every entry when the block format
    or extraction logic changes.
  * It fails open: any miss, corruption, version mismatch, or I/O error falls
    through to a live parse and never raises.

Disable with ``PARSE_CACHE=0`` in the environment (e.g. for debugging a parse).
"""

from __future__ import annotations

import hashlib
import os
import pickle
import sys
from pathlib import Path
from typing import Callable

# Bump whenever TableBlock / BlockRow / the _blocks_* extraction logic changes,
# so stale pickles from an older block format are ignored instead of poisoning.
VERSION = 1

_CACHE_ROOT = Path(__file__).resolve().parents[2] / ".cache" / "parse_tables"


def _enabled() -> bool:
    return os.environ.get("PARSE_CACHE", "1") != "0"


def _key_path(pdf_path: Path) -> Path:
    digest = hashlib.sha1(str(pdf_path.resolve()).encode("utf-8")).hexdigest()
    return _CACHE_ROOT / f"{digest}.pkl"


def _stat_header(pdf_path: Path) -> dict | None:
    try:
        st = pdf_path.stat()
    except OSError:
        return None
    return {
        "path": str(pdf_path.resolve()),
        "mtime_ns": st.st_mtime_ns,
        "size": st.st_size,
        "version": VERSION,
    }


def cached_blocks(pdf_path, compute: Callable[[], list]) -> list:
    """Return parsed blocks for ``pdf_path``, using the cache when valid.

    ``compute`` is the live parser (called on miss); its result is cached and
    returned. On any cache problem we fall back to ``compute()`` directly.
    """
    pdf_path = Path(pdf_path)
    if not _enabled():
        return compute()

    header = _stat_header(pdf_path)
    if header is None:  # PDF unstattable — let compute() handle existence
        return compute()

    key = _key_path(pdf_path)
    # Try load
    try:
        if key.exists():
            with key.open("rb") as fh:
                payload = pickle.load(fh)
            if isinstance(payload, dict) and payload.get("header") == header:
                return payload["blocks"]
    except Exception:
        pass  # corrupt / unpicklable / version drift → re-parse

    blocks = compute()

    # Try store (best-effort; never let a write failure break extraction)
    try:
        _CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        tmp = key.with_suffix(".pkl.tmp")
        with tmp.open("wb") as fh:
            pickle.dump({"header": header, "blocks": blocks}, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(key)  # atomic
    except Exception as exc:
        print(f"parse_cache: could not write {key.name}: {exc}", file=sys.stderr)

    return blocks
