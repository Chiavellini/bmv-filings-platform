"""A2 extraction fixes — offline, synthetic fixtures (no filings, no network).

Two blank-not-wrong FILLS validated here:
  1. Current ratio falls back to raw CA/CL (ratio-self-validated) when the revenue-sized gate rejects
     an asset-heavy, low-revenue issuer (Proteak-shaped) — instead of blanking a computable ratio.
  2. A REIT's net/EBITDA margin above 100% (non-cash investment-property fair-value gains, Fibra Vía)
     is KEPT with a fair-value annotation rather than blanked; a gross mis-scale (>300%) still blanks.
"""
from __future__ import annotations

from src.bloomberg.schema import BloombergPack
from src.coverage.fundamentals import Fundamentals
from src.coverage.spec import REIT_BLOCKS, CoverageSpec, Labelled, Peer
from src.coverage.valuation import build_model


def _cell(model, label):
    for b in model.blocks:
        for c in b.rows:
            if c.label == label:
                return c
    return None


# --- Fix 1: current ratio via raw CA/CL fallback ---------------------------------------------------

def _industrial_spec() -> CoverageSpec:
    return CoverageSpec(
        slug="proteak", name="Proteak", ticker="PROTEAK MM", currency="MXN", units="millions",
        template="industrial", blocks=["financial_analysis", "fcf_liquidity"], peers=[])


def _low_rev_fund(ca, cl) -> Fundamentals:
    # revenue 485; CL 3099 is 6.4× revenue → the revenue-sized _bs gate (hi=5×) rejects it.
    return Fundamentals(slug="proteak", frame=None, periods=["2026-1T"], current_period="2026-1T",
                        ltm={"revenue": 485.6, "net_income": -436.0, "total_equity": 1771.5,
                             "current_assets": ca, "current_liabilities": cl},
                        flow_keys={"revenue", "net_income"})


def test_current_ratio_fallback_fills_asset_heavy_lowrev():
    model = build_model(_industrial_spec(), _low_rev_fund(1801.0, 3099.9), BloombergPack(slug="proteak"))
    cr = _cell(model, "Current ratio")
    assert cr is not None and cr.value is not None
    assert abs(cr.value - (1801.0 / 3099.9)) < 1e-6      # ≈0.581, from RAW CA/CL


def test_current_ratio_stays_blank_when_ratio_insane():
    # CL 50000 is 103× revenue → rejected by the gate (cr None) → fallback cr_raw=13/50000=0.0003
    # is below the 0.02 sanity floor → stays blank. A wrong-scale ratio is never admitted.
    model = build_model(_industrial_spec(), _low_rev_fund(13.1, 50000.0), BloombergPack(slug="proteak"))
    cr = _cell(model, "Current ratio")
    assert cr is None or cr.value is None


# --- Fix 2: REIT margin annotate-and-keep ----------------------------------------------------------

def _reit_spec() -> CoverageSpec:
    return CoverageSpec(
        slug="fibravia", name="Fibra Via", ticker="FVIA MM", currency="MXN", units="millions",
        template="reit", blocks=list(REIT_BLOCKS),
        peers=[Peer("funo", "FUNO", "FUNO")],
        macro=[Labelled("policy_rate", "Banxico policy rate, %")])


def _reit_fund(net_income) -> Fundamentals:
    return Fundamentals(slug="fibravia", frame=None, periods=["2026-1T"], current_period="2026-1T",
                        ltm={"revenue": 3309.0, "operating_income": 2291.0, "net_income": net_income,
                             "total_equity": 35534.0, "total_assets": 60000.0,
                             "current_assets": 3480.0, "current_liabilities": 1839.0},
                        annual={2024: {"net_income": 3000.0, "revenue": 3100.0},
                                2025: {"net_income": net_income, "revenue": 3309.0}},
                        flow_keys={"revenue", "operating_income", "net_income"})


def _reit_pack() -> BloombergPack:
    return BloombergPack(slug="fibravia",
                         subject={"px_last": 20.0, "shares_out": 2000, "net_debt": 10000,
                                  "ebitda_ltm": 2400, "ffo": 2000, "affo": 1800},
                         peers={"funo": {"px_last": 28.0, "shares_out": 3900, "net_debt": 100000,
                                         "ffo": 14000, "ebitda_ltm": 24000}})


def test_reit_net_margin_over_100_kept_with_note():
    # net_income 3472 on revenue 3309 → 104.9% (fair-value gains) — kept, annotated, NOT blanked.
    model = build_model(_reit_spec(), _reit_fund(3472.0), _reit_pack())
    nm = _cell(model, "Net margin")
    assert nm is not None and nm.value is not None
    assert 104.0 < nm.value < 106.0
    assert "fair-value" in (nm.note or "").lower()


def test_reit_net_margin_gross_misscale_still_blanked():
    # net_income 20000 on revenue 3309 → 604% > ±300% cap → mis-scale, blanked (blank-not-wrong holds).
    model = build_model(_reit_spec(), _reit_fund(20000.0), _reit_pack())
    nm = _cell(model, "Net margin")
    assert nm is None or nm.value is None


def test_admit_cell_reit_margin_band_matches_valuation_layer():
    # The master-matrix guard (build_master._admit_cell) must widen for REITs too — else it re-blanks
    # the >100% margin the valuation layer kept. The two guards must agree.
    from scripts.build_master import _admit_cell
    assert _admit_cell("Net margin", "pct", 104.9, "reit") == 104.9      # REIT: kept
    assert _admit_cell("Net margin", "pct", 104.9, "industrial") is None  # non-REIT: still ±100
    assert _admit_cell("Net margin", "pct", 350.0, "reit") is None        # mis-scale: still blanked
