"""Math-audit tests — the clean synthetic model passes; deliberately corrupted models FAIL.

A gate that cannot fail is worthless, so every corruption below MUST be caught (gating error).
Fixtures mirror ``tests/test_valuation.py`` (no filings, no network).
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from src.bloomberg.schema import BloombergPack
from src.coverage.fundamentals import Fundamentals
from src.coverage.math_audit import audit
from src.coverage.spec import CoverageSpec, Labelled, Peer
from src.coverage.valuation import build_model
from src.sheets.valuation_sheet import build_valuation_workbook


def _fund() -> Fundamentals:
    ltm = {
        "revenue": 800_000, "gross_profit": 180_000, "operating_income": 60_000,
        "ebitda": 80_000, "net_income": 40_000, "total_assets": 500_000,
        "cash": 30_000, "inventory": 70_000, "cfo": 60_000, "capex": 20_000,
        "ebitda_mexico": 70_000, "ebitda_cam": 10_000, "sales_floor_mexico": 6_000_000,
        "total_stores": 3_800,
    }
    annual = {
        2020: {"ebitda": 60_000, "net_income": 30_000, "revenue": 650_000},
        2021: {"ebitda": 66_000, "net_income": 33_000, "revenue": 690_000},
        2022: {"ebitda": 70_000, "net_income": 35_000, "revenue": 720_000},
        2023: {"ebitda": 75_000, "net_income": 37_000, "revenue": 750_000},
        2024: {"ebitda": 80_000, "net_income": 40_000, "revenue": 800_000},
    }
    return Fundamentals(slug="walmex", frame=None, periods=["2024-4T"],
                        current_period="2024-4T", ltm=ltm, annual=annual,
                        flow_keys={"revenue", "ebitda", "net_income", "cfo", "capex",
                                   "operating_income", "gross_profit", "ebitda_mexico",
                                   "ebitda_cam"})


def _pack() -> BloombergPack:
    return BloombergPack(
        slug="walmex",
        subject={"px_last": 60.0, "shares_out": 17_400, "net_debt": 50_000,
                 "minority_interest": 0.0, "eps_ntm": 2.6, "dvd_yield": 1.6,
                 "total_equity": 250_000, "replacement_cost_per_sqm": 12_000},
        peers={
            "chedraui": {"px_last": 120.0, "shares_out": 960, "net_debt": 20_000,
                         "sales_ltm": 260_000, "ebitda_ltm": 22_000, "net_income_ltm": 8_000,
                         "total_equity": 60_000, "fcf_ltm": 6_000},
            "soriana": {"px_last": 28.0, "shares_out": 1_800, "net_debt": 15_000,
                        "sales_ltm": 160_000, "ebitda_ltm": 15_000, "net_income_ltm": 7_000,
                        "total_equity": 90_000, "fcf_ltm": 5_000},
        },
        segment_multiples={"mexico": 9.5, "cam": 7.0},
        history={2020: 45.0, 2021: 70.0, 2022: 65.0, 2023: 72.0, 2024: 62.0},
        macro={"gdp_growth": 1.4, "policy_rate": 8.0},
    )


def _spec() -> CoverageSpec:
    return CoverageSpec(
        slug="walmex", name="Walmex", ticker="WALMEX* MM", currency="MXN", units="millions",
        history_years=5,
        blocks=["snapshot_multiples", "historical_multiples", "sum_of_the_parts",
                "replacement_value", "financial_analysis", "macro_sector"],
        peers=[Peer("chedraui", "CHDRAUI", "CHDRAUI"), Peer("soriana", "SORIANA", "SORIANA")],
        segments=[Labelled("mexico", "Mexico"), Labelled("cam", "Central America")],
        macro=[Labelled("gdp_growth", "Mexico GDP growth, %"),
               Labelled("policy_rate", "Banxico policy rate, %")],
    )


def _model():
    return build_model(_spec(), _fund(), _pack())


def _cell(model, block_id, label):
    b = next(b for b in model.blocks if b.id == block_id)
    return next(c for c in b.rows if c.label == label)


# --- positive: the clean model + workbook pass -----------------------------
def test_clean_model_passes():
    model = _model()
    rep = audit(model, _spec(), _fund(), _pack())
    assert rep.passed, [(c.name, c.detail) for c in rep.errors]
    assert len(rep.checks) > 10  # actually exercised many checks


def test_clean_workbook_passes(tmp_path: Path):
    model = _model()
    xlsx = tmp_path / "Walmex.xlsx"
    build_valuation_workbook(model, _spec(), _fund(), _pack(), xlsx)
    rep = audit(model, _spec(), _fund(), _pack(), xlsx)
    assert rep.passed, [(c.name, c.detail) for c in rep.errors]
    # the industrial snapshot really did emit live formulas that we evaluated
    assert any("formula = recompute" in c.name for c in rep.checks)


# --- adversarial: every corruption must be caught --------------------------
def test_corrupt_snapshot_multiple_fails():
    model = _model()
    _cell(model, "snapshot_multiples", "EV/EBITDA").value = 999.0
    rep = audit(model, _spec(), _fund(), _pack())
    assert not rep.passed
    assert any("EV/EBITDA" in c.name for c in rep.errors)


def test_corrupt_ev_bridge_fails():
    model = _model()
    _cell(model, "snapshot_multiples", "Enterprise value").value = 1.0
    rep = audit(model, _spec(), _fund(), _pack())
    assert not rep.passed
    assert any("EV" in c.name for c in rep.errors)


def test_corrupt_ebitda_margin_fails():
    model = _model()
    _cell(model, "financial_analysis", "EBITDA margin").value = 42.0
    rep = audit(model, _spec(), _fund(), _pack())
    assert not rep.passed
    assert any("EBITDA margin" in c.name for c in rep.errors)


def test_corrupt_roe_fails():
    model = _model()
    _cell(model, "financial_analysis", "ROE").value = 5.0
    rep = audit(model, _spec(), _fund(), _pack())
    assert not rep.passed
    assert any("ROE" in c.name for c in rep.errors)


def test_nan_value_fails():
    model = _model()
    _cell(model, "snapshot_multiples", "Market cap").value = float("nan")
    rep = audit(model, _spec(), _fund(), _pack())
    assert not rep.passed


def test_corrupt_sotp_sum_fails():
    model = _model()
    _cell(model, "sum_of_the_parts", "Sum of segment EV").value = 123.0
    rep = audit(model, _spec(), _fund(), _pack())
    assert not rep.passed
    assert any("segment EV" in c.name for c in rep.errors)


def test_corrupt_historical_mean_fails():
    model = _model()
    _cell(model, "historical_multiples", "EV/EBITDA — historical mean").value = 0.0
    rep = audit(model, _spec(), _fund(), _pack())
    assert not rep.passed


def test_corrupt_peer_median_fails():
    model = _model()
    snap = next(b for b in model.blocks if b.id == "snapshot_multiples")
    snap.cross_section.median["ev_ebitda"] = 0.001
    rep = audit(model, _spec(), _fund(), _pack())
    assert not rep.passed
    assert any("median" in c.name.lower() for c in rep.errors)


def test_corrupt_formula_in_workbook_fails(tmp_path: Path):
    """Rewire a live multiple formula to the wrong denominator → the xlsx audit must catch it."""
    from openpyxl import load_workbook

    model = _model()
    xlsx = tmp_path / "Walmex.xlsx"
    build_valuation_workbook(model, _spec(), _fund(), _pack(), xlsx)

    wb = load_workbook(xlsx)
    ws = wb.active
    changed = False
    for r in range(1, ws.max_row + 1):
        if ws.cell(row=r, column=1).value == "EV/EBITDA":
            b = ws.cell(row=r, column=2).value
            if isinstance(b, str) and b.startswith("="):
                ws.cell(row=r, column=2).value = b + "+1"  # break the arithmetic
                changed = True
    assert changed
    wb.save(xlsx)

    rep = audit(model, _spec(), _fund(), _pack(), xlsx)
    assert not rep.passed
    assert any("xlsx" in c.name and "EV/EBITDA" in c.name for c in rep.errors)


def test_div_by_zero_formula_in_workbook_fails(tmp_path: Path):
    from openpyxl import load_workbook

    model = _model()
    xlsx = tmp_path / "Walmex.xlsx"
    build_valuation_workbook(model, _spec(), _fund(), _pack(), xlsx)
    wb = load_workbook(xlsx)
    ws = wb.active
    # find an empty cell to reference, force a /0
    ws["Z1"] = 0
    ws["Z2"] = "=10/Z1"
    wb.save(xlsx)
    rep = audit(model, _spec(), _fund(), _pack(), xlsx)
    assert not rep.passed
    assert any("formulas evaluate cleanly" in c.name for c in rep.errors)


def test_blank_cells_do_not_fail():
    """A pack with no peers/history still audits clean — blanks are allowed, not errors."""
    pack = BloombergPack(slug="walmex", subject={"px_last": 60, "shares_out": 17_400})
    model = build_model(_spec(), _fund(), pack)
    rep = audit(model, _spec(), _fund(), pack)
    assert rep.passed, [(c.name, c.detail) for c in rep.errors]


# ===========================================================================
# Analysis blocks (profitability / fcf_liquidity / temporal_ebit / growth)
# ===========================================================================
def _afund() -> Fundamentals:
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
            "revenue": 650_000 + i * 37_500, "gross_profit": 150_000 + i * 7_500,
            "operating_income": 45_000 + i * 3_750, "ebitda": 60_000 + i * 5_000,
            "net_income": 30_000 + i * 2_500, "cfo": 45_000 + i * 3_750,
            "capex": 15_000 + i * 1_250, "inventory": 55_000 + i * 3_750,
        }
    return Fundamentals(slug="acme", frame=None, periods=["2024-4T"],
                        current_period="2024-4T", ltm=ltm, annual=annual)


def _aspec() -> CoverageSpec:
    return CoverageSpec(slug="acme", name="Acme", currency="MXN", units="millions",
                        blocks=["financial_analysis", "profitability", "fcf_liquidity",
                                "temporal_ebit", "growth"])


def _apack() -> BloombergPack:
    return BloombergPack(slug="acme",
                         subject={"px_last": 60.0, "shares_out": 17_400, "net_debt": 50_000,
                                  "minority_interest": 0.0, "total_equity": 250_000})


def _amodel():
    return build_model(_aspec(), _afund(), _apack())


def test_analysis_clean_passes():
    model = _amodel()
    rep = audit(model, _aspec(), _afund(), _apack())
    assert rep.passed, [(c.name, c.detail) for c in rep.errors]
    # the new audits actually ran
    assert any("profitability" in c.name for c in rep.checks)
    assert any("fcf_liquidity" in c.name for c in rep.checks)
    assert any("temporal_ebit" in c.name for c in rep.checks)
    assert any("growth" in c.name for c in rep.checks)
    assert any("DuPont ROE reconciles" in c.name for c in rep.checks)


@pytest.mark.parametrize("block_id,label,bad", [
    ("profitability", "FY2024: Gross margin", 99.0),
    ("profitability", "EBIT margin (LTM)", 1.0),
    ("profitability", "DuPont — asset turnover", 9.0),
    ("profitability", "DuPont — equity multiplier", 9.0),
    ("profitability", "DuPont — implied ROE", 99.0),
    ("fcf_liquidity", "Current ratio", 9.0),
    ("fcf_liquidity", "Quick ratio", 9.0),
    ("fcf_liquidity", "Working capital", 1.0),
    ("fcf_liquidity", "Inventory turns", 99.0),
    ("fcf_liquidity", "Cash conversion cycle (days)", 999.0),
    ("fcf_liquidity", "FY2024: FCF", 1.0),
    ("temporal_ebit", "FY2024: EBIT", 1.0),
    ("temporal_ebit", "FY2024: EBITDA", 1.0),
    ("temporal_ebit", "EBIT YoY (latest)", 99.0),
    ("temporal_ebit", "EBITDA CAGR 5y", 99.0),
    ("growth", "FY2021: Revenue YoY", 99.0),
    ("growth", "Revenue CAGR 5y", 99.0),
    ("growth", "Net income CAGR 5y", 99.0),
    ("growth", "Revenue growth stability (σ)", 99.0),
])
def test_corrupt_analysis_metric_fails(block_id, label, bad):
    model = _amodel()
    _cell(model, block_id, label).value = bad
    rep = audit(model, _aspec(), _afund(), _apack())
    assert not rep.passed, f"gate should fail when {block_id}/{label} is corrupted"
    assert any(block_id in c.name for c in rep.errors)


def test_dupont_roe_mismatch_fails():
    """Break the DuPont↔ROE reconciliation (>0.5pp) → the gate must fail."""
    model = _amodel()
    _cell(model, "profitability", "DuPont — implied ROE").value = 25.0  # true ROE is 16%
    rep = audit(model, _aspec(), _afund(), _apack())
    assert not rep.passed
    assert any("DuPont" in c.name for c in rep.errors)


def test_analysis_nan_fails():
    model = _amodel()
    _cell(model, "growth", "Revenue CAGR 5y").value = float("nan")
    rep = audit(model, _aspec(), _afund(), _apack())
    assert not rep.passed
