"""The hard gate in build_coverage.finalize(): a clean model emits; a broken one is quarantined."""
from __future__ import annotations

import importlib
from pathlib import Path

from src.bloomberg.schema import BloombergPack
from src.coverage.valuation import build_model

from tests.test_math_audit import _fund, _pack, _spec

build_coverage = importlib.import_module("scripts.build_coverage")


def test_clean_build_emits(tmp_path: Path):
    model = build_model(_spec(), _fund(), _pack())
    emitted, report = build_coverage.finalize(model, _spec(), _fund(), _pack(), tmp_path, gate=True)
    assert emitted and report.passed
    assert (tmp_path / "excel" / "Walmex.xlsx").exists()
    assert (tmp_path / "csv" / "walmex_coverage.csv").exists()
    assert (tmp_path / "validation" / "walmex_math.md").exists()
    assert not (tmp_path / "validation" / "walmex_MATH_FAILED.md").exists()


def test_broken_build_is_blocked(tmp_path: Path):
    model = build_model(_spec(), _fund(), _pack())
    # corrupt a ratio
    snap = next(b for b in model.blocks if b.id == "snapshot_multiples")
    next(c for c in snap.rows if c.label == "EV/EBITDA").value = 12345.0

    emitted, report = build_coverage.finalize(model, _spec(), _fund(), _pack(), tmp_path, gate=True)
    assert not emitted and not report.passed
    # nothing shipped, only the failure marker
    assert not (tmp_path / "excel" / "Walmex.xlsx").exists()
    assert not (tmp_path / "csv" / "walmex_coverage.csv").exists()
    assert (tmp_path / "validation" / "walmex_MATH_FAILED.md").exists()


def test_no_gate_escape_hatch_emits_broken(tmp_path: Path):
    model = build_model(_spec(), _fund(), _pack())
    snap = next(b for b in model.blocks if b.id == "snapshot_multiples")
    next(c for c in snap.rows if c.label == "EV/EBITDA").value = 12345.0
    emitted, report = build_coverage.finalize(model, _spec(), _fund(), _pack(), tmp_path, gate=False)
    assert emitted and not report.passed
    assert (tmp_path / "excel" / "Walmex.xlsx").exists()


def test_stale_workbook_removed_on_failure(tmp_path: Path):
    # first a clean emit, then a broken rebuild must remove the stale workbook.
    model = build_model(_spec(), _fund(), _pack())
    build_coverage.finalize(model, _spec(), _fund(), _pack(), tmp_path, gate=True)
    assert (tmp_path / "excel" / "Walmex.xlsx").exists()

    broken = build_model(_spec(), _fund(), _pack())
    snap = next(b for b in broken.blocks if b.id == "snapshot_multiples")
    next(c for c in snap.rows if c.label == "EV/EBITDA").value = 12345.0
    emitted, _ = build_coverage.finalize(broken, _spec(), _fund(), _pack(), tmp_path, gate=True)
    assert not emitted
    assert not (tmp_path / "excel" / "Walmex.xlsx").exists()
