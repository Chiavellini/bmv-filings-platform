"""REIT/FIBRA template — multiples math, template selector, block set, workbook render."""
from __future__ import annotations

import pytest

from src.bloomberg.schema import BloombergPack, required_rows
from src.coverage.fundamentals import Fundamentals
from src.coverage.peers import CompanyInputs, company_multiples_reit
from src.coverage.spec import REIT_BLOCKS, CoverageSpec, Labelled, Peer, parse_spec
from src.coverage.valuation import build_model


def _spec() -> CoverageSpec:
    return CoverageSpec(
        slug="fibraup", name="Fibra Uno", ticker="FUNO11 MM", template="reit",
        blocks=list(REIT_BLOCKS),
        peers=[Peer("fibrahd", "FIBRAHD", "FIBRAHD"), Peer("storage", "STORAGE", "STORAGE")],
        macro=[Labelled("policy_rate", "Banxico policy rate, %")])


def _fund() -> Fundamentals:
    return Fundamentals(slug="fibraup", frame=None, periods=["2025-4T"], current_period="2025-4T",
                        ltm={"revenue": 30_000, "operating_income": 22_000, "net_income": 18_000,
                             "total_equity": 200_000, "total_assets": 340_000},
                        annual={2023: {"net_income": 16_000, "revenue": 26_000},
                                2024: {"net_income": 17_000, "revenue": 28_000},
                                2025: {"net_income": 18_000, "revenue": 30_000}},
                        flow_keys={"revenue", "operating_income", "net_income"})


def _pack() -> BloombergPack:
    return BloombergPack(
        slug="fibraup",
        subject={"px_last": 28.0, "shares_out": 3_900, "net_debt": 100_000, "minority_interest": 0.0,
                 "ffo": 14_000, "affo": 12_000, "nav_ps": 36.0, "distribution_yield": 7.5,
                 "ebitda_ltm": 24_000, "noi": 24_000, "occupancy": 94.0, "ltv": 33.0,
                 "cap_rate": 7.8, "gla": 12_000},
        peers={"fibrahd": {"px_last": 20.0, "shares_out": 900, "net_debt": 8_000,
                           "ffo": 1_600, "affo": 1_400, "nav_ps": 28.0, "distribution_yield": 9.0,
                           "ebitda_ltm": 2_400},
               "storage": {"px_last": 40.0, "shares_out": 500, "net_debt": 3_000,
                           "ffo": 1_100, "affo": 900, "nav_ps": 45.0, "distribution_yield": 6.0,
                           "ebitda_ltm": 1_500}},
        history={2023: 26.0, 2024: 27.0, 2025: 28.0},
        macro={"policy_rate": 8.0})


def test_reit_multiples_math():
    c = CompanyInputs(name="x", px_last=28, shares_out=3_900, net_debt=100_000,
                      ebitda=24_000, ffo=14_000, affo=12_000, nav_ps=36.0, distribution_yield=7.5)
    m = company_multiples_reit(c)
    mc = 28 * 3_900
    assert m["p_ffo"] == pytest.approx(mc / 14_000)
    assert m["p_affo"] == pytest.approx(mc / 12_000)
    assert m["ev_ebitda"] == pytest.approx((mc + 100_000) / 24_000)
    assert m["dist_yield"] == 7.5


def test_reit_template_selects_reit_blocks(tmp_path):
    md = tmp_path / "fibraup.md"
    md.write_text("# Fibra Uno\nTicker: FUNO11 MM\n\n## Settings\n- template: reit\n\n"
                  "## Peers\n- FIBRAHD {fibrahd}\n", encoding="utf-8")
    spec = parse_spec(md)
    assert spec.template == "reit"
    # REITs keep their FFO/NAV blocks AND now carry the shared analytical stack (so a Fibra also gets
    # the universal valuation/profitability/growth metrics in the cross-company matrix).
    assert set(REIT_BLOCKS) <= set(spec.blocks)
    assert {"financial_analysis", "profitability", "growth"} <= set(spec.blocks)
    subj_fields = {r.field for r in required_rows(spec, base_year=2026) if r.entity_kind == "subject"}
    assert {"ffo", "affo", "distribution_yield", "noi", "occupancy"} <= subj_fields
    # dropped (Bloomberg-only, no native source)
    assert not ({"nav_ps", "ltv", "cap_rate", "gla"} & subj_fields)
    assert "seg_ev_ebitda" not in subj_fields


def test_build_reit_model():
    model = build_model(_spec(), _fund(), _pack())
    assert [b.id for b in model.blocks] == list(REIT_BLOCKS)
    assert not model.warnings
    by_id = {b.id: b for b in model.blocks}

    snap = by_id["reit_snapshot"]
    assert snap.cross_section is not None
    pffo = next(r for r in snap.rows if r.label == "P/FFO")
    assert pffo.value == pytest.approx((28 * 3_900) / 14_000)

    met = by_id["reit_metrics"]
    assert any(r.label == "Occupancy" and r.value == 94.0 for r in met.rows)
    noi_m = next(r for r in met.rows if r.label == "NOI margin")
    assert noi_m.value == pytest.approx(24_000 / 30_000 * 100)

    hist = by_id["reit_historical"]
    assert any(r.label.endswith("EV/Revenue") and r.label.startswith("FY") for r in hist.rows)
    assert any(r.label == "EV/Revenue — historical mean" for r in hist.rows)
