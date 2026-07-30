"""test_night_fixes.py — Regression tests for the overnight Phase-3 bug fixes.

Bug 1: LACOMER revenue 3Q16 must read the quarter column (3,713), not the YTD
       column (10,829); and the [Actual,Anterior] Q1 layout (1Q26) must stay correct.
Bug 3: SPORT ebitda_sin_ifrs must read the clean quarterly pre-IFRS-16 figure, not
       the interleaved cash balance or the acumulado/YTD figure.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.model.financial_model import METRICS, apply_config, load_config
from src.extract.extract_metrics import extract_metrics_segmented


def _extract(company: str, filename: str) -> dict:
    cfg = load_config(ROOT / "configs" / f"{company}.yaml")
    defs = apply_config(METRICS, cfg)
    path = ROOT / "data" / "reports" / company / filename
    if not path.exists():
        pytest.skip(f"Report not found: {path}")
    return extract_metrics_segmented(path.read_text(encoding="utf-8"), defs, cfg)


# ---------------------------------------------------------------------------
# Bug 1 — LACOMER revenue column selection
# ---------------------------------------------------------------------------

def test_lacomer_3q16_revenue_is_quarter_not_ytd():
    """3Q16 [Acumulado, Trimestre] table: must capture 3,713 (Q3), not 10,829 (YTD)."""
    rows = _extract("lacomer", "3t16_bmv.md")
    assert rows["revenue"].current == pytest.approx(3_713.261, rel=1e-4)


def test_lacomer_1q26_revenue_is_current_not_prior():
    """1Q26 [Actual, Anterior] table: must stay 12,007 (current), not 11,076 (prior)."""
    rows = _extract("lacomer", "BMV_1T26.md")
    assert rows["revenue"].current == pytest.approx(12_006.524, rel=1e-4)


# ---------------------------------------------------------------------------
# Bug 3 — SPORT ebitda_sin_ifrs (values in miles_mxn; "$104.8 millones" -> 104_800)
# ---------------------------------------------------------------------------

def test_sport_3q25_ebitda_sin_ifrs_not_cash():
    """2025-3T two-column interleave: must read $104.8 (104_800), not cash $358.7."""
    rows = _extract("sport", "2025-3T.md")
    v = rows.get("ebitda_sin_ifrs")
    assert v is not None and v.current == pytest.approx(104_800.0, rel=1e-3)
    assert abs(v.current - 358_700.0) > 1_000  # explicitly not the cash balance


@pytest.mark.parametrize("filename,expected", [
    ("2026-1T.md", 97_300.0),   # clean 2025+ bullet
    ("2023-2T.md", 38_200.0),   # 2023-24 inline phrasing
    ("2016-2T.md", 52_000.0),   # quarter-anchored "en el 2T16" (not the acumulado 97.2)
])
def test_sport_ebitda_sin_ifrs_quarterly(filename, expected):
    rows = _extract("sport", filename)
    v = rows.get("ebitda_sin_ifrs")
    assert v is not None and v.current == pytest.approx(expected, rel=1e-3)


# ---------------------------------------------------------------------------
# Phase D — active validator: quarantine + flagged_metrics
# ---------------------------------------------------------------------------

def test_validator_flags_segment_exceeding_total():
    from src.shared.validator import validate, flagged_metrics
    from src.extract.extract_metrics import MetricRow

    def row(k, v):
        return MetricRow(metric=k, label_es="", current=v, prior=None,
                         var_pct=None, unit="currency", source_line="[prose]")

    metrics = {"revenue": row("revenue", 1_000), "revenue_mexico": row("revenue_mexico", 5_000)}
    results = validate(metrics)
    assert any(r.rule == "segment_not_exceeds_total" and not r.passed for r in results)
    assert {"revenue", "revenue_mexico"} <= flagged_metrics(results)

    # Healthy case: no flag.
    ok = {"revenue": row("revenue", 1_000), "revenue_mexico": row("revenue_mexico", 600)}
    assert not any(r.rule == "segment_not_exceeds_total" for r in validate(ok))
