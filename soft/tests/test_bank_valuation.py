"""Bank/financials template — multiples math, template selector, and block set."""
from __future__ import annotations

import pytest

from src.bloomberg.schema import BloombergPack, required_rows
from src.coverage.fundamentals import Fundamentals
from src.coverage.peers import CompanyInputs, build_cross_section, company_multiples_bank, MULTIPLES_BANK
from src.coverage.spec import FINANCIALS_BLOCKS, CoverageSpec, Labelled, Peer
from src.coverage.valuation import build_model


def _bank_spec() -> CoverageSpec:
    return CoverageSpec(
        slug="gfnorte", name="GFNorte", ticker="GFNORTEO", template="financials",
        blocks=list(FINANCIALS_BLOCKS),
        peers=[Peer("bsmx", "Santander Mx", "Santander Mx"), Peer("bbajio", "Banbajío", "Banbajío")],
        macro=[Labelled("policy_rate", "Banxico policy rate, %")])


def _bank_fund() -> Fundamentals:
    # annual XBRL only → no LTM flows, but annual equity/NI present
    return Fundamentals(slug="gfnorte", frame=None, periods=["2024"], current_period="2024",
                        ltm={"total_equity": 280_000, "total_assets": 2_400_000},
                        annual={2023: {"total_equity": 255_000, "net_income": 52_000},
                                2024: {"total_equity": 280_000, "net_income": 57_000}},
                        flow_keys={"net_income"})


def _bank_pack() -> BloombergPack:
    return BloombergPack(
        slug="gfnorte",
        subject={"px_last": 160.0, "shares_out": 2_880, "book_value": 280_000,
                 "tangible_book": 250_000, "net_income_ltm": 57_000, "eps_ntm": 21.0,
                 "dvd_yield": 5.0, "roe": 21.5, "rote": 24.0, "nim": 6.2,
                 "efficiency_ratio": 36.0, "cost_of_risk": 1.4, "cet1": 13.5,
                 "loan_growth": 12.0, "deposit_growth": 9.0},
        peers={"bsmx": {"px_last": 40.0, "shares_out": 6_800, "book_value": 175_000,
                        "tangible_book": 160_000, "net_income_ltm": 28_000, "dvd_yield": 4.0},
               "bbajio": {"px_last": 55.0, "shares_out": 1_190, "book_value": 60_000,
                          "tangible_book": 58_000, "net_income_ltm": 13_000, "dvd_yield": 3.0}},
        history={2023: 145.0, 2024: 155.0})


def test_template_selects_financials_blocks(tmp_path):
    md = tmp_path / "gfnorte.md"
    md.write_text("# GFNorte\nTicker: GFNORTEO\n\n## Settings\n- template: financials\n\n"
                  "## Peers\n- Santander Mx {bsmx}\n", encoding="utf-8")
    from src.coverage.spec import parse_spec
    spec = parse_spec(md)
    assert spec.template == "financials"
    # Banks keep their bank blocks AND now carry the shared analytical stack (net margin, DuPont,
    # universal P/BV, CAGRs) — EBITDA/liquidity cells stay blank (N/A for a bank).
    assert set(FINANCIALS_BLOCKS) <= set(spec.blocks)
    assert {"financial_analysis", "growth"} <= set(spec.blocks)
    # required_rows emits bank fields, not EV/EBITDA industrial fields
    rows = required_rows(spec, base_year=2026)
    subj_fields = {r.field for r in rows if r.entity_kind == "subject"}
    assert {"book_value", "tangible_book", "nim", "cet1", "roe"} <= subj_fields
    assert "loan_growth" not in subj_fields  # dropped (Bloomberg-only, no native source)
    assert "seg_ev_ebitda" not in subj_fields


def test_bank_multiples_math():
    c = CompanyInputs(name="gfnorte", px_last=160, shares_out=2_880, net_income=57_000,
                      equity=280_000, tangible_book=250_000, eps_ntm=21.0, dvd_yield=5.0)
    m = company_multiples_bank(c)
    mc = 160 * 2_880
    assert m["pbv"] == pytest.approx(mc / 280_000)
    assert m["ptbv"] == pytest.approx(mc / 250_000)
    assert m["pe_ltm"] == pytest.approx(mc / 57_000)
    assert "ev_ebitda" not in m  # banks have no EV/EBITDA


def test_annual_period_parsing():
    from src.coverage.fundamentals import _period_year_q
    assert _period_year_q("2024-1T") == (2024, 1)
    assert _period_year_q("2024-4T") == (2024, 4)
    assert _period_year_q("2024") == (2024, 4)       # bare annual
    assert _period_year_q("2025-FY") == (2025, 4)    # BMV annual label
    assert _period_year_q("garbage") is None


def test_build_bank_model():
    model = build_model(_bank_spec(), _bank_fund(), _bank_pack())
    assert [b.id for b in model.blocks] == list(FINANCIALS_BLOCKS)
    assert not model.warnings
    by_id = {b.id: b for b in model.blocks}

    snap = by_id["bank_snapshot"]
    assert snap.cross_section is not None
    pbv = next(r for r in snap.rows if r.label == "P/BV")
    assert pbv.value == pytest.approx((160 * 2_880) / 280_000)
    assert not any(r.label == "EV/EBITDA" for r in snap.rows)

    ret = by_id["bank_returns"]
    # ROE computed from filing NI/equity (57000/280000)
    roe = next(r for r in ret.rows if r.label == "ROE")
    assert roe.value == pytest.approx(57_000 / 280_000 * 100)
    assert any(r.label == "CET1 ratio" and r.value == 13.5 for r in ret.rows)
    assert any(r.label.startswith("Net interest margin") and r.value == 6.2 for r in ret.rows)

    grow = by_id["bank_growth"]
    assert any("Book value growth" in r.label for r in grow.rows)

    hist = by_id["bank_historical"]
    assert any(r.label.startswith("FY") and r.label.endswith("P/BV") for r in hist.rows)
    assert any(r.label == "P/BV — historical mean" for r in hist.rows)
