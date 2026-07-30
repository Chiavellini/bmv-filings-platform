"""test_silent_failure_flags.py — the three silent-failure modes must be visible.

1. [scale_unverified]: table scale defaulted to 1.0 with no positive evidence
   (no XBRL overlap, no caption) → monetary rows tagged, confidence capped ≤0.5.
2. [positional]: a period header existed but couldn't be aligned → the positional
   guess is tagged, confidence capped ≤0.6.
3. Series magnitude gate: a monetary cell ≥300× its series median is dropped to
   an honest MISS (unit artifacts, stray regex-table grabs); authoritative tiers
   and undersized values are untouched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.extract.extract_metrics import MetricRow
from src.extract.parse_tables import TableBlock, match_metrics_from_blocks, resolve_table_scale
from src.extract.tiered_extract import _tag_scale_unverified
from src.extract.pipeline import _quarantine_series_magnitude
from src.model.financial_model import METRICS
from src.shared.validator import score_confidence


def _row(key: str, current: float, source: str = "[table] fila") -> MetricRow:
    return MetricRow(metric=key, label_es=key, current=current, prior=None,
                     var_pct=None, unit="currency", source_line=source)


_REVENUE_DEF = [m for m in METRICS if m.key == "revenue"]


# ---------------------------------------------------------------------------
# 1. scale_unverified
# ---------------------------------------------------------------------------

def test_resolve_table_scale_reports_default_evidence():
    raw = {"revenue": _row("revenue", 1000.0)}
    scale, evidence = resolve_table_scale(raw, {}, {"company": {}}, _REVENUE_DEF,
                                          with_evidence=True)
    assert scale == 1.0 and evidence == "default"


def test_resolve_table_scale_reports_config_evidence():
    scale, evidence = resolve_table_scale({}, {}, {"table_scale": 1e-6}, _REVENUE_DEF,
                                          with_evidence=True)
    assert scale == pytest.approx(1e-6) and evidence == "config"


def test_tag_scale_unverified_marks_only_monetary():
    count_def = [m for m in METRICS if m.key == "revenue" or m.unit == "count"][:2]
    rows = {m.key: MetricRow(metric=m.key, label_es=m.key, current=5.0, prior=None,
                             var_pct=None, unit=m.unit, source_line="[table] x")
            for m in count_def}
    tagged = _tag_scale_unverified(rows, count_def)
    for key, row in tagged.items():
        if row.unit in ("currency", "miles_mxn"):
            assert "[scale_unverified]" in row.source_line
        else:
            assert "[scale_unverified]" not in row.source_line


def test_scale_unverified_caps_confidence():
    rows = {"revenue": _row("revenue", 1000.0, "[scale_unverified] [table] fila")}
    scores = score_confidence(rows, [])
    assert scores["revenue"] <= 0.5


# ---------------------------------------------------------------------------
# 2. positional degradation
# ---------------------------------------------------------------------------

def test_unalignable_header_tags_positional():
    # Header carries periods, but NOT the target (2020) → select fails → fallback.
    block = TableBlock(
        header_by_col={0: "1T16", 1: "1T15"},
        rows=[("Ingresos totales", [(0, "517,708"), (1, "480,001")])],
    )
    out = match_metrics_from_blocks([block], _REVENUE_DEF, period="2020-1T")
    assert out["revenue"].current == 517_708.0            # positional value kept
    assert "[positional]" in out["revenue"].source_line   # ...but tagged


def test_aligned_header_is_not_tagged():
    block = TableBlock(
        header_by_col={0: "1T16", 1: "1T15"},
        rows=[("Ingresos totales", [(0, "517,708"), (1, "480,001")])],
    )
    out = match_metrics_from_blocks([block], _REVENUE_DEF, period="2016-1T")
    assert "[positional]" not in out["revenue"].source_line


def test_headerless_block_is_not_tagged():
    block = TableBlock(header_by_col={}, rows=[("Ingresos totales", [(0, "100"), (1, "90")])])
    out = match_metrics_from_blocks([block], _REVENUE_DEF, period="2020-1T")
    assert "[positional]" not in out["revenue"].source_line


def test_positional_caps_confidence():
    rows = {"revenue": _row("revenue", 1000.0, "[table] [positional] fila")}
    scores = score_confidence(rows, [])
    assert scores["revenue"] <= 0.6


# ---------------------------------------------------------------------------
# 3. series magnitude gate
# ---------------------------------------------------------------------------

def _series(values: dict[str, float], source: str = "[table] fila") -> dict:
    return {p: {"revenue": _row("revenue", v, source)} for p, v in values.items()}


def test_magnitude_gate_drops_1000x_artifact():
    data = _series({"2020-1T": 100.0, "2020-2T": 110.0, "2020-3T": 105.0,
                    "2020-4T": 120.0, "2021-1T": 108_000.0, "2021-2T": 115.0})
    _quarantine_series_magnitude(data, _REVENUE_DEF)
    assert "revenue" not in data["2021-1T"]               # artifact dropped
    assert data["2020-1T"]["revenue"].current == 100.0    # rest untouched


def test_magnitude_gate_spares_authoritative_tiers():
    data = _series({"2020-1T": 100.0, "2020-2T": 110.0, "2020-3T": 105.0,
                    "2020-4T": 120.0, "2021-2T": 115.0})
    data["2021-1T"] = {"revenue": _row("revenue", 108_000.0, "[xbrl] Revenue")}
    _quarantine_series_magnitude(data, _REVENUE_DEF)
    assert "revenue" in data["2021-1T"]


def test_magnitude_gate_spares_small_values_and_short_series():
    # Near-zero quarters are legitimate (volatile metrics) — never dropped.
    data = _series({"2020-1T": 100.0, "2020-2T": 110.0, "2020-3T": 0.01,
                    "2020-4T": 120.0, "2021-1T": 115.0, "2021-2T": 105.0})
    _quarantine_series_magnitude(data, _REVENUE_DEF)
    assert data["2020-3T"]["revenue"].current == 0.01
    # <5 observations: no basis to call an outlier.
    short = _series({"2020-1T": 100.0, "2020-2T": 108_000.0})
    _quarantine_series_magnitude(short, _REVENUE_DEF)
    assert "revenue" in short["2020-2T"]
