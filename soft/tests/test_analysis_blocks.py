"""Analysis-block engine tests — margin/DuPont/liquidity/temporal/growth math over synthetic
fund.annual fixtures (no filings, no network), plus the shared _cagr helper edge cases."""
from __future__ import annotations

import math
import statistics

import pytest

from src.bloomberg.schema import BloombergPack
from src.coverage.fundamentals import Fundamentals
from src.coverage.spec import CoverageSpec, parse_spec
from src.coverage.valuation import (
    _cagr,
    block_fcf_liquidity,
    block_growth,
    block_profitability,
    block_temporal,
    build_model,
)


# --------------------------------------------------------------------------- fixtures
def _fund() -> Fundamentals:
    ltm = {
        "revenue": 800_000, "gross_profit": 180_000, "operating_income": 60_000,
        "ebitda": 80_000, "net_income": 40_000, "total_assets": 500_000,
        "cash": 30_000, "inventory": 70_000,
        "current_assets": 120_000, "current_liabilities": 150_000,
        "accounts_receivable": 20_000, "accounts_payable": 90_000,
        "cfo": 60_000, "capex": 20_000,
    }
    annual = {}
    for i, yr in enumerate((2020, 2021, 2022, 2023, 2024)):
        annual[yr] = {
            "revenue": 650_000 + i * 37_500,
            "gross_profit": 150_000 + i * 7_500,
            "operating_income": 45_000 + i * 3_750,
            "ebitda": 60_000 + i * 5_000,
            "net_income": 30_000 + i * 2_500,
            "cfo": 45_000 + i * 3_750,
            "capex": 15_000 + i * 1_250,
            "inventory": 55_000 + i * 3_750,
        }
    return Fundamentals(slug="acme", frame=None, periods=["2024-4T"],
                        current_period="2024-4T", ltm=ltm, annual=annual)


def _pack() -> BloombergPack:
    return BloombergPack(slug="acme",
                         subject={"px_last": 60.0, "shares_out": 17_400, "net_debt": 50_000,
                                  "minority_interest": 0.0, "total_equity": 250_000})


def _cell(block, label):
    return next((c for c in block.rows if c.label == label), None)


# --------------------------------------------------------------------------- _cagr helper
def test_cagr_normal():
    # 60k -> 80k over a 4-year span (2020..2024).
    series = {2020: 60_000, 2024: 80_000}
    assert _cagr(series, 5) == pytest.approx((80_000 / 60_000) ** (1 / 4) - 1)


def test_cagr_window_truncates():
    series = {2019: 10, 2021: 20, 2024: 40}  # 3y window → 2021..2024 span 3
    got = _cagr(series, 3)
    assert got == pytest.approx((40 / 20) ** (1 / 3) - 1)


def test_cagr_edge_cases():
    assert _cagr({}, 5) is None                       # empty
    assert _cagr({2024: 100}, 5) is None              # single point
    assert _cagr({2024: 100, 2024: 100}, 5) is None   # one distinct year
    assert _cagr({2020: -5, 2024: 80}, 5) is None     # non-positive start
    assert _cagr({2020: 5, 2024: 0}, 5) is None       # non-positive end


# --------------------------------------------------------------------------- profitability
def test_profitability_margins_and_dupont():
    fund, pack = _fund(), _pack()
    blk = block_profitability(None, fund, pack)
    gm = _cell(blk, "FY2024: Gross margin")
    assert gm.value == pytest.approx(180_000 / 800_000 * 100)  # uses the FY2024 annual figures
    # DuPont components multiply back to net_income/equity.
    nm = _cell(blk, "DuPont — net margin").value
    at = _cell(blk, "DuPont — asset turnover").value
    em = _cell(blk, "DuPont — equity multiplier").value
    implied = _cell(blk, "DuPont — implied ROE").value
    assert at == pytest.approx(800_000 / 500_000)
    assert em == pytest.approx(500_000 / 250_000)
    assert implied == pytest.approx(nm / 100 * at * em * 100)
    assert implied == pytest.approx(40_000 / 250_000 * 100)  # ties to ROE
    assert _cell(blk, "EBIT margin (LTM)").value == pytest.approx(60_000 / 800_000 * 100)


def test_profitability_drops_implausible_assets():
    fund, pack = _fund(), _pack()
    fund.ltm["total_assets"] = 100.0          # far below equity → dropped
    blk = block_profitability(None, fund, pack)
    assert _cell(blk, "DuPont — asset turnover") is None
    assert _cell(blk, "DuPont — implied ROE") is None


# --------------------------------------------------------------------------- fcf_liquidity
def test_fcf_liquidity_ratios():
    fund, pack = _fund(), _pack()
    blk = block_fcf_liquidity(None, fund, pack)
    assert _cell(blk, "Current ratio").value == pytest.approx(120_000 / 150_000)
    assert _cell(blk, "Quick ratio").value == pytest.approx((120_000 - 70_000) / 150_000)
    assert _cell(blk, "Working capital").value == pytest.approx(120_000 - 150_000)
    cogs = 800_000 - 180_000
    assert _cell(blk, "Inventory turns").value == pytest.approx(cogs / 70_000)
    dio = 70_000 / cogs * 365
    dso = 20_000 / 800_000 * 365
    dpo = 90_000 / cogs * 365
    assert _cell(blk, "Cash conversion cycle (days)").value == pytest.approx(dio + dso - dpo)
    # FCF trend series present.
    assert _cell(blk, "FY2024: FCF").value == pytest.approx(60_000 - 20_000)


def test_fcf_liquidity_drops_scale_artifact():
    fund, pack = _fund(), _pack()
    fund.ltm["current_assets"] = 5.0          # ~6e-6 of revenue → implausible → blank
    blk = block_fcf_liquidity(None, fund, pack)
    assert _cell(blk, "Current ratio") is None
    assert _cell(blk, "Working capital") is None


# --------------------------------------------------------------------------- temporal
def test_temporal_ebit_series_and_growth():
    fund, pack = _fund(), _pack()
    blk = block_temporal(None, fund, pack)
    assert _cell(blk, "FY2024: EBIT").value == pytest.approx(60_000)
    assert _cell(blk, "FY2024: EBITDA").value == pytest.approx(80_000)
    ebit_cy, ebit_py = 45_000 + 4 * 3_750, 45_000 + 3 * 3_750
    assert _cell(blk, "EBIT YoY (latest)").value == pytest.approx((ebit_cy / ebit_py - 1) * 100)
    c3 = _cell(blk, "EBITDA CAGR 3y").value
    assert c3 == pytest.approx(((80_000 / (60_000 + 1 * 5_000)) ** (1 / 3) - 1) * 100)


# --------------------------------------------------------------------------- growth
def test_growth_yoy_cagr_stability():
    fund, pack = _fund(), _pack()
    blk = block_growth(None, fund, pack)
    # per-FY revenue YoY starts at the 2nd year.
    assert _cell(blk, "FY2020: Revenue YoY") is None
    yoy21 = (687_500 / 650_000 - 1) * 100
    assert _cell(blk, "FY2021: Revenue YoY").value == pytest.approx(yoy21)
    yoy_series = []
    ry = sorted(fund.annual)
    for i in range(1, len(ry)):
        yoy_series.append((fund.annual[ry[i]]["revenue"] / fund.annual[ry[i - 1]]["revenue"] - 1) * 100)
    assert _cell(blk, "Revenue growth stability (σ)").value == pytest.approx(statistics.pstdev(yoy_series))
    rc5 = _cell(blk, "Revenue CAGR 5y").value
    assert rc5 == pytest.approx(((800_000 / 650_000) ** (1 / 4) - 1) * 100)
    # Revenue CAGR 1y == the latest FY revenue YoY (FY2024 vs FY2023).
    rc1 = _cell(blk, "Revenue CAGR 1y").value
    assert rc1 == pytest.approx((800_000 / 762_500 - 1) * 100)
    # Net income CAGR 1y == latest FY net-income YoY (FY2024 40,000 vs FY2023 37,500), prev > 0.
    ni1 = _cell(blk, "Net income CAGR 1y").value
    assert ni1 == pytest.approx((40_000 / 37_500 - 1) * 100)


# --------------------------------------------------------------------------- percentages are whole
def test_percentages_are_whole_numbers():
    """Margins/CAGRs are stored as whole numbers (22.5), not fractions (0.225)."""
    fund, pack = _fund(), _pack()
    blk = block_profitability(None, fund, pack)
    assert _cell(blk, "FY2024: Gross margin").value == pytest.approx(180_000 / 800_000 * 100)
    assert _cell(blk, "DuPont — net margin").value == pytest.approx(40_000 / 800_000 * 100)
    assert _cell(block_growth(None, fund, pack), "Revenue CAGR 5y").value > 1.0  # ~5%, not 0.05


# --------------------------------------------------------------------------- graceful degradation
def test_blocks_blank_gracefully_with_thin_data():
    fund = Fundamentals(slug="thin", frame=None, periods=["2024-4T"],
                        current_period="2024-4T",
                        ltm={"revenue": 100_000}, annual={2024: {"revenue": 100_000}})
    pack = BloombergPack(slug="thin", subject={})
    # No crash; blocks return with whatever is computable (mostly empty).
    for builder in (block_profitability, block_fcf_liquidity, block_temporal, block_growth):
        blk = builder(None, fund, pack)
        assert all(math.isfinite(c.value) for c in blk.rows if c.value is not None)


# --------------------------------------------------------------------------- timeseries fallback
def test_timeseries_gap_fills_missing_year():
    fund = Fundamentals(slug="acme", frame=None, periods=["2024-4T"], current_period="2024-4T",
                        ltm={"revenue": 800_000},
                        annual={2023: {"revenue": 700_000}, 2024: {"revenue": 800_000}})
    pack = BloombergPack(slug="acme", subject={},
                         timeseries={2022: {"revenue": 600_000}})
    blk = block_growth(None, fund, pack)
    # 2022 comes only from pack.timeseries → the 2023 YoY row exists off it.
    assert _cell(blk, "FY2023: Revenue YoY").value == pytest.approx((700_000 / 600_000 - 1) * 100)


# --------------------------------------------------------------------------- spec wiring
def test_industrial_spec_auto_gets_analysis_blocks(tmp_path):
    md = tmp_path / "acme.md"
    md.write_text("# Acme\n\n## Settings\n- units: millions\n", encoding="utf-8")
    spec = parse_spec(md)
    for b in ("profitability", "fcf_liquidity", "temporal_ebit", "growth"):
        assert b in spec.blocks


def test_explicit_blocks_still_get_analysis_blocks(tmp_path):
    md = tmp_path / "acme.md"
    md.write_text("# Acme\n\n## Blocks\n- snapshot_multiples\n- financial_analysis\n", encoding="utf-8")
    spec = parse_spec(md)
    assert "snapshot_multiples" in spec.blocks
    for b in ("profitability", "fcf_liquidity", "temporal_ebit", "growth"):
        assert b in spec.blocks


def test_opt_out_suppresses_analysis_blocks(tmp_path):
    md = tmp_path / "acme.md"
    md.write_text("# Acme\n\n## Settings\n- analysis_blocks: off\n", encoding="utf-8")
    spec = parse_spec(md)
    for b in ("profitability", "fcf_liquidity", "temporal_ebit", "growth"):
        assert b not in spec.blocks


def test_bank_template_gets_analysis_blocks(tmp_path):
    # Banks now carry the shared analytical stack too, so a bank row in the cross-company matrix has
    # net margin, DuPont, universal P/BV, and CAGRs (EBITDA/liquidity cells stay blank — N/A).
    md = tmp_path / "bank.md"
    md.write_text("# Bank\n\n## Settings\n- template: financials\n", encoding="utf-8")
    spec = parse_spec(md)
    assert "financial_analysis" in spec.blocks
    for b in ("profitability", "fcf_liquidity", "temporal_ebit", "growth"):
        assert b in spec.blocks


def test_build_model_includes_analysis_blocks():
    spec = CoverageSpec(slug="acme", name="Acme",
                        blocks=["financial_analysis", "profitability", "fcf_liquidity",
                                "temporal_ebit", "growth"])
    model = build_model(spec, _fund(), _pack())
    ids = [b.id for b in model.blocks]
    assert ids == ["financial_analysis", "profitability", "fcf_liquidity", "temporal_ebit", "growth"]
    assert not model.warnings
