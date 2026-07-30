"""Unit coverage for Tier-2 auto scale-detection (resolve_table_scale).

The resolver recovers the printed→stored monetary scale for a company that does
NOT set `table_scale`, so an unseen filer scales correctly with no config. It is
deliberately conservative: it only returns a non-1.0 scale on strong, agreeing
XBRL evidence; otherwise it returns 1.0 (today's default), keeping companies that
already work at 1.0 untouched.
"""

from __future__ import annotations

import os

import pytest

from src.extract.parse_tables import resolve_table_scale, _snap_scale
from src.extract.extract_metrics import MetricRow
from src.model.financial_model import MetricDef


def _row(cur, src="[table]", unit="currency"):
    return MetricRow(metric="revenue", label_es="x", current=cur, prior=None,
                     var_pct=None, unit=unit, source_line=src)


def _mdef(key, unit="currency"):
    return MetricDef(key=key, label=key, label_es=key, section="income", unit=unit, patterns=[])


_CFG_MILLIONS = {"company": {"unit": "millions"}}
_MDEFS = [_mdef("revenue"), _mdef("gross_profit")]


def test_full_pesos_recovered_from_xbrl():
    # Table prints full pesos (246,254,000); XBRL says 246.254 (millions). Two
    # metrics agree on 1e-6 → resolver returns it.
    raw = {"revenue": _row(246_254_000.0), "gross_profit": _row(80_000_000.0)}
    xbrl = {"revenue": _row(246.254, "[xbrl]"), "gross_profit": _row(80.0, "[xbrl]")}
    assert resolve_table_scale(raw, xbrl, _CFG_MILLIONS, _MDEFS) == pytest.approx(1e-6)


def test_already_millions_returns_one():
    raw = {"revenue": _row(246.0), "gross_profit": _row(80.0)}
    xbrl = {"revenue": _row(246.0, "[xbrl]"), "gross_profit": _row(80.0, "[xbrl]")}
    assert resolve_table_scale(raw, xbrl, _CFG_MILLIONS, _MDEFS) == 1.0


def test_single_vote_is_too_weak_defaults_to_one():
    # Only one metric overlaps → not enough agreement for a non-default scale.
    raw = {"revenue": _row(246_254_000.0)}
    xbrl = {"revenue": _row(246.254, "[xbrl]")}
    assert resolve_table_scale(raw, xbrl, _CFG_MILLIONS, _MDEFS) == 1.0


def test_config_table_scale_always_wins():
    raw = {"revenue": _row(246_254_000.0), "gross_profit": _row(80_000_000.0)}
    xbrl = {"revenue": _row(246.254, "[xbrl]"), "gross_profit": _row(80.0, "[xbrl]")}
    assert resolve_table_scale(raw, xbrl, {"table_scale": 0.5}, _MDEFS) == 0.5


def test_no_xbrl_overlap_defaults_to_one():
    # Table values but no XBRL-sourced rows to calibrate against → 1.0.
    raw = {"revenue": _row(246_254_000.0), "gross_profit": _row(80_000_000.0)}
    assert resolve_table_scale(raw, {}, _CFG_MILLIONS, _MDEFS) == 1.0


def test_miles_mxn_company_candidate_set():
    # A thousands-stored company (sport): table printed in thousands == stored
    # unit, so ratio 1 → scale 1.0 (no spurious rescale).
    cfg = {"company": {"unit": "miles_mxn"}}
    mdefs = [_mdef("revenue", "miles_mxn"), _mdef("gross_profit", "miles_mxn")]
    raw = {"revenue": _row(50_000.0, unit="miles_mxn"), "gross_profit": _row(20_000.0, unit="miles_mxn")}
    xbrl = {"revenue": _row(50_000.0, "[xbrl]", "miles_mxn"),
            "gross_profit": _row(20_000.0, "[xbrl]", "miles_mxn")}
    assert resolve_table_scale(raw, xbrl, cfg, mdefs) == 1.0


def test_escape_hatch_disables_inference():
    raw = {"revenue": _row(246_254_000.0), "gross_profit": _row(80_000_000.0)}
    xbrl = {"revenue": _row(246.254, "[xbrl]"), "gross_profit": _row(80.0, "[xbrl]")}
    os.environ["AUTO_SCALE"] = "0"
    try:
        assert resolve_table_scale(raw, xbrl, _CFG_MILLIONS, _MDEFS) == 1.0
    finally:
        del os.environ["AUTO_SCALE"]


def test_snap_scale_log_space():
    assert _snap_scale(0.9e-6, [1e-6, 1e-3, 1.0]) == 1e-6
    assert _snap_scale(1.1, [1e-6, 1e-3, 1.0]) == 1.0
    assert _snap_scale(0.0012, [1e-6, 1e-3, 1.0]) == 1e-3
