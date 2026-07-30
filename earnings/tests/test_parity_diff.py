"""Parity comparator classification on synthetic frames."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pandas as pd

EARNINGS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EARNINGS_ROOT))

spec = importlib.util.spec_from_file_location(
    "parity_check", EARNINGS_ROOT / "scripts" / "parity_check.py")
pc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pc)


def _frames(tmp_path, frozen_rows, v3_rows):
    cols = ["slug", "period", "metric", "current", "prior", "source_concept"]
    frozen = pd.DataFrame(frozen_rows, columns=cols)
    v3 = pd.DataFrame(v3_rows, columns=cols)
    frozen.to_parquet(tmp_path / "metrics.parquet", index=False)
    v3.to_parquet(tmp_path / "metrics_v3.parquet", index=False)
    return frozen, v3


def test_metric_classes(tmp_path, monkeypatch):
    frozen_rows = [
        ("a", "2023-1T", "revenue", 10.0, 9.0, "[xbrl] X"),        # identical
        ("a", "2023-1T", "net_income", 5.0, 4.0, "[xbrl] X"),      # value_changed
        ("a", "2023-1T", "eps", 1.0, 0.9, "[xbrl] X"),             # concept_changed
        ("a", "2023-2T", "revenue", 11.0, 10.0, "[xbrl] X"),       # only_frozen
        ("b", "2023-1T", "depreciation", 0.0, 0.0, "[xbrl] zeroC"),  # improvement
        ("b", "2023-1T", "ebitda", 7.0, 6.0, "[calc]"),            # improvement (derived)
    ]
    v3_rows = [
        ("a", "2023-1T", "revenue", 10.0, 9.0, "[xbrl] X"),
        ("a", "2023-1T", "net_income", 5.5, 4.0, "[xbrl] X"),
        ("a", "2023-1T", "eps", 1.0, 0.9, "[xbrl] Y"),
        ("a", "2023-3T", "revenue", 12.0, 11.0, "[xbrl] X"),       # only_v3
        ("b", "2023-1T", "depreciation", 0.4, 0.3, "[xbrl] realC"),
        ("b", "2023-1T", "ebitda", 7.4, 6.3, "[calc]"),
    ]
    _frames(tmp_path, frozen_rows, v3_rows)
    monkeypatch.setattr(pc.bs, "OUTPUTS_DIR", tmp_path)
    diff, summary = pc.compare_metrics()
    by_class = diff.groupby("class")["metric"].count().to_dict()
    assert summary["value_changed"] == 1
    assert summary["engine_improvement"] == 2       # dep + derived ebitda
    assert summary["concept_changed"] == 1
    assert summary["only_frozen"] == 1 and summary["only_v3"] == 1
    assert by_class["engine_improvement"] == 2


def test_surprises_ripple_tolerance(tmp_path, monkeypatch):
    frozen = pd.DataFrame({
        "slug": ["a", "b", "c"], "period": ["2023-1T"] * 3,
        "s_cs": [1.0, -1.0, 0.0], "s_ts": [1.0, -1.0, 0.0]})
    v3 = pd.DataFrame({
        "slug": ["a", "b", "c"], "period": ["2023-1T"] * 3,
        "s_cs": [1.5, -1.01, 0.5], "s_ts": [1.0, -1.0, 0.0]})
    frozen.to_parquet(tmp_path / "surprises_v2.parquet", index=False)
    v3.to_parquet(tmp_path / "surprises_v3.parquet", index=False)
    monkeypatch.setattr(pc.bs, "OUTPUTS_DIR", tmp_path)
    diff, summary = pc.compare_surprises(improved_slugs=["a"])
    # a: improved slug (0.5 shift ok); b: 0.01 <= tol ripple; c: 0.5 > tol blocks
    assert summary["improved_slug_changes"] == 1
    assert summary["improvement_ripple"] == 1
    assert summary["value_changed"] == 1
