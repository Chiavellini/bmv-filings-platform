"""Regression tests for the extractable-obs exclusion mechanism in the eval
harness (src/eval/compare_extractions.excluded_reason).

The accuracy denominator counts only cells that are genuinely extractable from
the source. Documented data-unavailable cells — captured either by `era_gates`
(not reported before year Y) or an explicit `unavailable:` block with a proof —
must be EXCLUDED, not counted as MISS. These tests pin that contract.
"""
from src.eval.compare_extractions import excluded_reason, _period_year_quarter


def test_period_parsing():
    assert _period_year_quarter("1Q26A") == (2026, 1)
    assert _period_year_quarter("4Q16A") == (2016, 4)
    assert _period_year_quarter("3Q19") == (2019, 3)
    assert _period_year_quarter("garbage") is None


def test_no_config_blocks_means_not_excluded():
    cfg = {}
    assert excluded_reason(cfg, "revenue", "1Q26A") is None


def test_era_gate_excludes_before_year_only():
    cfg = {"era_gates": {"ebitda": {"available_from_year": 2019}}}
    # 4Q16 predates IFRS-16 adoption → excluded.
    r = excluded_reason(cfg, "ebitda", "4Q16A")
    assert r is not None and "era_gate" in r
    # 1Q19 onward is reported → not excluded.
    assert excluded_reason(cfg, "ebitda", "1Q19A") is None
    # A metric without a gate is unaffected.
    assert excluded_reason(cfg, "revenue", "4Q16A") is None


def test_unavailable_whole_metric():
    cfg = {"unavailable": {"cards_total": {
        "reason": "prose growth only, never an absolute",
        "proof": "data/reports/liverpool/2024-1T.md — '6.9 millones'",
    }}}
    r = excluded_reason(cfg, "cards_total", "2Q22A")
    assert r is not None
    assert "absolute" in r and "proof:" in r
    # Other metrics unaffected.
    assert excluded_reason(cfg, "revenue", "2Q22A") is None


def test_unavailable_before_year_scoped():
    cfg = {"unavailable": {"revenue_mexico": {
        "before_year": 2024,
        "reason": "MX/CAM split not published before 4Q23",
        "proof": "data/reports/walmex/3T23.md",
    }}}
    assert excluded_reason(cfg, "revenue_mexico", "3Q23A") is not None   # before 2024
    assert excluded_reason(cfg, "revenue_mexico", "1Q24A") is None       # 2024 onward reported


def test_unavailable_explicit_periods():
    cfg = {"unavailable": {"ebitda_sin_ifrs": {
        "periods": ["4Q16A", "3Q19A"],
        "reason": "ambiguous two-column acumulado prose",
        "proof": "data/reports/sport/2016-4T.md",
    }}}
    assert excluded_reason(cfg, "ebitda_sin_ifrs", "4Q16A") is not None
    assert excluded_reason(cfg, "ebitda_sin_ifrs", "3Q19A") is not None
    assert excluded_reason(cfg, "ebitda_sin_ifrs", "1Q20A") is None      # not in the list


def test_parse_gap_flag_is_tagged():
    cfg = {"unavailable": {"sales_floor_total": {
        "parse_gap": True,
        "reason": "present in PDF but lost in markdown parse",
        "proof": "data/reports/liverpool/2023-1T.pdf p.4",
    }}}
    r = excluded_reason(cfg, "sales_floor_total", "1Q23A")
    assert r is not None and r.startswith("[parse_gap]")
