"""Phase-3 tests — the valuation engine over synthetic fixtures (no filings, no network)."""
from __future__ import annotations

import math

import pytest

from src.bloomberg.schema import BloombergPack
from src.coverage.fundamentals import Fundamentals
from src.coverage.peers import CompanyInputs, build_cross_section, company_multiples
from src.coverage.spec import CoverageSpec, Labelled, Peer
from src.coverage.valuation import build_model


def _fund() -> Fundamentals:
    ltm = {
        "revenue": 800_000, "gross_profit": 180_000, "operating_income": 60_000,
        "ebitda": 80_000, "net_income": 40_000, "total_assets": 500_000,
        "cash": 30_000, "inventory": 70_000, "cfo": 60_000, "capex": 20_000,
        "ebitda_mexico": 70_000, "ebitda_cam": 10_000, "sales_floor_mexico": 6_000_000,
        "total_stores": 3_800,
        # note: total_equity intentionally absent (filings omit it) → pack fallback
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
                                   "operating_income", "gross_profit", "ebitda_mexico", "ebitda_cam"})


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


def test_company_multiples_math():
    c = CompanyInputs(name="x", px_last=60, shares_out=17_400, net_debt=50_000,
                      minority_interest=0, sales=800_000, ebitda=80_000, net_income=40_000,
                      equity=250_000, fcf=40_000, eps_ntm=2.6, dvd_yield=1.6)
    assert c.market_cap == 60 * 17_400
    assert c.ev == 60 * 17_400 + 50_000
    m = company_multiples(c)
    assert m["pe_ltm"] == pytest.approx(c.market_cap / 40_000)
    assert m["ev_ebitda"] == pytest.approx(c.ev / 80_000)
    assert m["pbv"] == pytest.approx(c.market_cap / 250_000)


def test_cross_section_median():
    subj = CompanyInputs(name="walmex", px_last=60, shares_out=17_400, net_debt=50_000,
                         sales=800_000, ebitda=80_000, net_income=40_000, equity=250_000)
    peers = [
        CompanyInputs(name="a", px_last=100, shares_out=1_000, net_debt=10_000,
                      sales=200_000, ebitda=20_000, net_income=8_000, equity=60_000),
        CompanyInputs(name="b", px_last=50, shares_out=2_000, net_debt=5_000,
                      sales=150_000, ebitda=14_000, net_income=6_000, equity=80_000),
    ]
    xs = build_cross_section(subj, peers)
    assert xs.order == ["walmex", "a", "b"]
    # peer-median EV/EBITDA is the median of the two peers
    import statistics
    peer_ev_ebitda = [xs.multiples["ev_ebitda"]["a"], xs.multiples["ev_ebitda"]["b"]]
    assert xs.median["ev_ebitda"] == pytest.approx(statistics.median(peer_ev_ebitda))
    assert xs.subject_vs_median("ev_ebitda") is not None


def test_build_model_all_blocks():
    model = build_model(_spec(), _fund(), _pack())
    assert [b.id for b in model.blocks] == [
        "snapshot_multiples", "historical_multiples", "sum_of_the_parts",
        "replacement_value", "financial_analysis", "macro_sector",
    ]
    assert not model.warnings

    by_id = {b.id: b for b in model.blocks}

    # Snapshot: EV/EBITDA present and correct; peer cross-section attached.
    snap = by_id["snapshot_multiples"]
    ev_ebitda = next(r for r in snap.rows if r.label == "EV/EBITDA")
    ev = 60 * 17_400 + 50_000
    assert ev_ebitda.value == pytest.approx(ev / 80_000)
    assert snap.cross_section is not None

    # SOTP: Mexico 70k×9.5 + CAM 10k×7.0 = 665k + 70k = 735k EV.
    sotp = by_id["sum_of_the_parts"]
    sum_ev = next(r for r in sotp.rows if r.label == "Sum of segment EV")
    assert sum_ev.value == pytest.approx(70_000 * 9.5 + 10_000 * 7.0)
    implied_px = next(r for r in sotp.rows if r.label == "Implied price / share")
    assert implied_px.value == pytest.approx((735_000 - 50_000) / 17_400)

    # Replacement: 6,000,000 m² × 12,000 / 1e6 (normalised to millions) = 72,000.
    repl = by_id["replacement_value"]
    rv = next(r for r in repl.rows if r.label == "Replacement value of sales floor")
    assert rv.value == pytest.approx(6_000_000 * 12_000 / 1_000_000)

    # Financial: ROE = net_income / equity(pack fallback) = 40k/250k.
    fin = by_id["financial_analysis"]
    roe = next(r for r in fin.rows if r.label == "ROE")
    assert roe.value == pytest.approx(40_000 / 250_000 * 100)
    ebm = next(r for r in fin.rows if r.label == "EBITDA margin")
    assert ebm.value == pytest.approx(80_000 / 800_000 * 100)

    # Historical: mean over 5 FYs, current percentile present.
    hist = by_id["historical_multiples"]
    mean_row = next(r for r in hist.rows if r.label == "EV/EBITDA — historical mean")
    assert mean_row.value is not None and math.isfinite(mean_row.value)
    assert any(r.label == "EV/EBITDA — current percentile" for r in hist.rows)

    # Macro: two rows echoing the pack.
    macro = by_id["macro_sector"]
    assert {r.label for r in macro.rows} == {"Mexico GDP growth, %", "Banxico policy rate, %"}


def test_missing_bbg_degrades_gracefully():
    """A pack with no peers / no history still builds; multiples become None, no crash."""
    pack = BloombergPack(slug="walmex", subject={"px_last": 60, "shares_out": 17_400})
    model = build_model(_spec(), _fund(), pack)
    assert not model.warnings  # blocks catch their own gaps
    snap = {b.id: b for b in model.blocks}["snapshot_multiples"]
    ev_ebitda = next(r for r in snap.rows if r.label == "EV/EBITDA")
    assert ev_ebitda.value is None  # net_debt missing → EV None → multiple None
