"""Straggler re-fetch pass in refresh_daily — the second-chance re-pricing that converts sweep-wide
Yahoo throttle gaps into fills. Offline: build_one / _read_price / parse_spec are mocked."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import build_dense
from scripts import refresh_daily


def _prices_iter(vals):
    """A _read_price stand-in returning the given values on successive calls (old_px, then new_px)."""
    it = iter(vals)
    return lambda slug, name: next(it)


def test_straggler_indices_selects_priceless_rows():
    rows = [
        ("A", "a", "emitted", 10.0, 12.0, 20.0, None),   # resolved → not a straggler
        ("B", "b", "emitted", 10.0, None, None, None),   # no price → straggler
        ("C", "c", "ERROR", None, None, None, "boom"),   # no price (errored) → straggler
    ]
    assert refresh_daily._straggler_indices(rows) == [1, 2]


def test_straggler_indices_empty_when_all_resolved():
    rows = [("A", "a", "emitted", 10.0, 12.0, 20.0, None)]
    assert refresh_daily._straggler_indices(rows) == []


def test_refresh_one_computes_move(monkeypatch):
    class _Res:
        error = None
        coverage_status = "emitted"
        emitted = True
    monkeypatch.setattr("src.coverage.spec.parse_spec", lambda p: type("S", (), {"name": "N"})())
    monkeypatch.setattr(refresh_daily, "_read_price", _prices_iter([10.0, 11.0]))  # old→new
    row = refresh_daily._refresh_one(lambda *a, **k: _Res(), "slug", "spec.md")
    name, slug, status, old, new, moved, err = row
    assert (old, new, status, err) == (10.0, 11.0, "emitted", None)
    assert abs(moved - 10.0) < 1e-9


def test_refresh_one_survives_build_exception(monkeypatch):
    monkeypatch.setattr("src.coverage.spec.parse_spec", lambda p: type("S", (), {"name": "N"})())
    monkeypatch.setattr(refresh_daily, "_read_price", _prices_iter([10.0, None]))

    def _boom(*a, **k):
        raise RuntimeError("kaboom")
    row = refresh_daily._refresh_one(_boom, "slug", "spec.md")
    # a single company blowing up becomes an ERROR row (never sinks the run), and is a straggler
    assert row[2] == "ERROR" and row[4] is None and "kaboom" in row[6]
    assert refresh_daily._straggler_indices([row]) == [0]


def test_required_dense_regeneration_propagates_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(refresh_daily, "ROOT", tmp_path)

    def fail_emit(*args, **kwargs):
        raise build_dense.DenseMatrixError("no qualifying companies")

    monkeypatch.setattr(build_dense, "emit", fail_emit)
    with pytest.raises(build_dense.DenseMatrixError, match="no qualifying"):
        refresh_daily._regenerate_dense_matrix()
