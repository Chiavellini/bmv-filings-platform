"""Regression tests for the Herdez custom extractor + exclude_metrics config feature.

Covers: (1) segment/equity/MegaMex recovery on a real clean report, (2) the Domestic
coalescing across the Conservas+Impulso → Nacional labeling eras, and (3) that
`exclude_metrics` drops base metrics so they cannot poison cross-checks.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.extract.herdez import extract_herdez
from src.model.financial_model import apply_config

REPORTS = Path(__file__).resolve().parent.parent / "data" / "reports" / "herdez"


def _extract(period: str):
    md = (REPORTS / f"{period}.md").read_text(encoding="utf-8")
    return extract_herdez(md, [], period)


@pytest.mark.skipif(not (REPORTS / "2018-2T.md").exists(), reason="corpus not present")
def test_clean_period_segments_and_equity():
    """2018-2T (Conservas/Impulso era): consolidated + segments + equity + MegaMex."""
    out = _extract("2018-2T")
    assert out["revenue"].current == 5217
    assert out["gross_profit"].current == 2117
    assert out["operating_income"].current == 775
    assert out["ebitda"].current == 909
    # legacy segments reported directly
    assert out["ns_conservas"].current == 3929
    assert out["ns_impulso"].current == 873
    assert out["ns_export"].current == 415
    # equity-in-associates + MegaMex standalone
    assert out["equity_associates"].current == 245
    assert out["equity_megamex"].current == 237
    assert out["megamex_net_sales"].current == 3355
    # every value tagged [statement] so the cascade prefers it
    assert all(r.source_line.startswith("[statement]") for r in out.values())


@pytest.mark.skipif(not (REPORTS / "2018-2T.md").exists(), reason="corpus not present")
def test_domestic_coalesces_conservas_plus_impulso():
    """Pre-2025 reports have no 'Nacional' line; Domestic = Conservas + Impulso."""
    out = _extract("2018-2T")
    assert out["ns_domestic"].current == pytest.approx(3929 + 873)  # 4802


@pytest.mark.skipif(not (REPORTS / "2024-4T.md").exists(), reason="corpus not present")
def test_stacked_header_with_interleaved_narrative():
    """4Q24 layout: '%'/'NET SALES 4Q24 4Q23'/'change' stacked header with a
    two-column narrative line interleaved before 'Consolidated 9,897 …'. The
    quarterly table must win over the MegaMex-in-MXN 'Net sales' block (3,378),
    and the scan must stop at the 12M companion table instead of taking annual
    segment values."""
    out = _extract("2024-4T")
    assert out["revenue"].current == 9897
    for key in ("ns_conservas", "ns_impulso", "ns_export"):
        row = out.get(key)
        assert row is None or row.current is None or row.current < 20000, (
            f"{key} picked a 12M annual value: {row.current}"
        )


@pytest.mark.skipif(not (REPORTS / "2024-3T.md").exists(), reason="corpus not present")
def test_equity_ignores_wrapped_boilerplate_sentence():
    """3Q24: the page-1 boilerplate wraps so 'Equity Investments in Associated
    Companies.' lands at line start right before a NET SALES bar chart; the real
    equity table ('Consolidated 32 139 (76.9)') must be used instead."""
    out = _extract("2024-3T")
    assert out["equity_associates"].current == 32
    assert out["equity_megamex"].current == 47


def test_exclude_metrics_drops_base_keys():
    """`exclude_metrics` removes base metrics entirely (so they never extract/validate)."""
    from src.excel.segments_sheet import load_metric_defs

    base = load_metric_defs(None)
    assert any(m.key == "cogs" for m in base)  # present by default

    cfg = {"exclude_metrics": ["cogs", "ebitda_margin", "total_assets"]}
    kept = {m.key for m in apply_config(base, cfg)}
    assert "cogs" not in kept
    assert "ebitda_margin" not in kept
    assert "total_assets" not in kept
    # non-excluded base metrics survive
    assert "revenue" in kept and "ebitda" in kept
