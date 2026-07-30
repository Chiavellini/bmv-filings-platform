"""
test_interface.py — extract phase: fuzzy metric resolution + the extract() API.

MetricResolver maps user-supplied names to canonical keys; silent mis-resolution
would feed the wrong metrics downstream, so its precedence is pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.model.financial_model import METRICS
from src.extract.interface import MetricResolver, extract

ROOT = Path(__file__).parent.parent


@pytest.fixture
def resolver():
    return MetricResolver(METRICS)


def test_exact_key_match(resolver):
    r = resolver.resolve("revenue")
    assert r is not None and r.keys[0] == "revenue" and r.method == "exact"


def test_case_and_space_normalized(resolver):
    r = resolver.resolve("EBITDA")
    assert r is not None and r.keys[0] == "ebitda"


def test_fuzzy_name_resolves(resolver):
    # "net income" is not a literal key (net_income is) — must still resolve to it.
    r = resolver.resolve("net income")
    assert r is not None and r.keys[0] == "net_income"


def test_unmatched_returns_none(resolver):
    assert resolver.resolve("qwerty zxcvb nonsense") is None


def test_resolve_many_and_all_keys_for(resolver):
    resolved = resolver.resolve_many(["revenue", "ebitda", "qwerty zxcvb nonsense"])
    assert len(resolved) == 3 and resolved[2] is None
    keys = resolver.all_keys_for(resolved)
    assert "revenue" in keys and "ebitda" in keys
    assert len(keys) == len(set(keys))            # de-duplicated


def test_search_returns_metric_rows(resolver):
    hits = resolver.search("revenue")
    assert any(key == "revenue" for key, *_ in hits)


@pytest.fixture
def sport_md():
    p = ROOT / "data" / "reports" / "sport" / "2026-1T.md"
    if not p.exists():
        pytest.skip(f"fixture report missing: {p}")
    return p


def test_extract_returns_requested_wide_columns(sport_md):
    df = extract(sport_md, metrics="revenue, ebitda", verbose=False)
    assert "period" in df.columns
    assert "revenue" in df.columns
    assert len(df) == 1
