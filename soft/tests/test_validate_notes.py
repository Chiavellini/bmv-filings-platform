"""A loss-making subject's non-positive multiples are reported as non-blocking 'not meaningful' notes,
not BROKEN sentinels — so an otherwise-complete deliverable for a company in a loss year still PASSes.
Offline (synthetic fixtures, no network)."""
from __future__ import annotations

from src.bloomberg.schema import BloombergPack
from src.coverage.fundamentals import Fundamentals
from src.coverage.spec import CoverageSpec, Peer
from src.coverage.valuation import build_model
from src.coverage.validate import _yahoo_corroborates, validate_model


class _P:
    def __init__(self, stats, subject=None):
        self.yahoo_stats = dict(stats)
        self.subject = dict(subject or {})


def test_yahoo_corroborates_cheap_pbv():
    # Sheet P/BV 0.38, Yahoo's own price_to_book 0.39 → corroborated (real discount).
    assert _yahoo_corroborates("P/BV", 0.38, _P({"price_to_book": 0.39}))


def test_share_count_match_corroborates_any_cheap_multiple():
    # Yahoo shares 470 == our 470 → the too-low-share-count premise is disproven, so even a P/E for
    # which Yahoo has no figure is corroborated (Vitro's real case).
    pack = _P({"shares_out": 470.0}, subject={"shares_out": 470.0})
    assert _yahoo_corroborates("P/E (LTM)", 0.82, pack)
    assert _yahoo_corroborates("P/BV", 0.16, pack)


def test_share_count_mismatch_does_not_corroborate_on_its_own():
    # Yahoo shares 900 vs our 300 (3× off) → share count is NOT verified; with no same-metric figure
    # either, the cheap multiple stays flagged.
    pack = _P({"shares_out": 900.0}, subject={"shares_out": 300.0})
    assert not _yahoo_corroborates("P/BV", 0.30, pack)


def test_yahoo_disagrees_leaves_cheap_flagged():
    # Yahoo P/BV 1.4 while the sheet says 0.30, and no share-count info → not corroborated → stays a bug.
    assert not _yahoo_corroborates("P/BV", 0.30, _P({"price_to_book": 1.4}))


def test_no_yahoo_stat_is_not_corroborated():
    assert not _yahoo_corroborates("P/BV", 0.30, _P({}))
    assert not _yahoo_corroborates("P/E (LTM)", 3.0, _P({"price_to_book": 0.4}))  # wrong metric


def _loss_fund() -> Fundamentals:
    # Net loss → P/E must come out negative; everything else present and sane.
    ltm = {
        "revenue": 100_000, "operating_income": -5_000, "ebitda": 8_000,
        "net_income": -12_000, "total_equity": 40_000, "total_assets": 120_000,
        "cash": 5_000, "cfo": 3_000, "capex": 2_000,
    }
    annual = {y: {"ebitda": 8_000, "net_income": -12_000, "revenue": 100_000}
              for y in (2020, 2021, 2022, 2023, 2024)}
    return Fundamentals(slug="lossco", frame=None, periods=["2024-4T"], current_period="2024-4T",
                        ltm=ltm, annual=annual,
                        flow_keys={"revenue", "ebitda", "net_income", "cfo", "capex",
                                   "operating_income"})


def _pack() -> BloombergPack:
    return BloombergPack(
        slug="lossco",
        subject={"px_last": 45.0, "shares_out": 1_000, "net_debt": 10_000,
                 "minority_interest": 0.0, "dvd_yield": 2.0, "total_equity": 40_000},
        peers={"peera": {"px_last": 30.0, "shares_out": 900, "net_debt": 8_000,
                         "sales_ltm": 90_000, "ebitda_ltm": 12_000, "net_income_ltm": 6_000,
                         "total_equity": 45_000}},
    )


def _spec() -> CoverageSpec:
    return CoverageSpec(
        slug="lossco", name="LossCo", ticker="LOSS MM", currency="MXN", units="millions",
        history_years=5, template="industrial",
        blocks=["snapshot_multiples", "financial_analysis"],
        peers=[Peer("peera", "PEERA", "PEERA")])


def test_negative_pe_is_a_note_not_a_sentinel():
    fund, pack, spec = _loss_fund(), _pack(), _spec()
    model = build_model(spec, fund, pack)
    v = validate_model(model, spec, fund, pack)

    # The negative P/E is present (not blank) → not an engine gap, and must NOT be a sentinel.
    pe_note = [n for n in v.notes if "P/E" in n]
    assert pe_note, f"expected a P/E not-meaningful note, got notes={v.notes}"
    assert not any("non-positive" in s for s in v.sentinels)
    # The note names the loss driver from the filings.
    assert "net loss" in pe_note[0]
    # A loss year with everything else present is not BROKEN on account of the negative multiple.
    assert v.status != "BROKEN", f"status={v.status} sentinels={v.sentinels} missing={v.missing_engine}"


# --- F5 blank-not-wrong guardrail: impossible margins/returns are blanked, never shipped ----------


def _impossible_margin_fund() -> Fundamentals:
    # Cultiba's shape: a big one-off gain (net income ≫ residual revenue) → net margin 1218%, and a
    # deeply negative operating result → EBITDA margin −236%. Both are arithmetically impossible as a
    # share of revenue and must be blanked. Book-value/ROE inputs are sane so those stay.
    ltm = {
        "revenue": 108.0, "operating_income": -255.0, "ebitda": -255.0,
        "net_income": 1_318.0, "total_equity": 16_118.0, "total_assets": 16_147.0,
        "cash": 500.0, "cfo": 50.0, "capex": 20.0,
    }
    annual = {y: {"revenue": 108.0, "net_income": 1_318.0, "ebitda": -255.0, "total_equity": 16_118.0}
              for y in (2021, 2022, 2023, 2024, 2025)}
    return Fundamentals(slug="holdco", frame=None, periods=["2025-1T"], current_period="2025-1T",
                        ltm=ltm, annual=annual,
                        flow_keys={"revenue", "ebitda", "net_income", "cfo", "capex",
                                   "operating_income"})


def test_impossible_margin_is_blanked_not_shipped():
    fund, pack, spec = _impossible_margin_fund(), _pack(), _spec()
    model = build_model(spec, fund, pack)
    by_id = {b.id: b for b in model.blocks}
    fin = by_id["financial_analysis"]
    cells = {c.label: c for c in fin.rows}

    # Net margin (1218%) and EBITDA margin (−236%) are impossible → value blanked with a note.
    assert cells["Net margin"].value is None
    assert "blanked" in cells["Net margin"].note and "±100%" in cells["Net margin"].note
    assert cells["EBITDA margin"].value is None
    # The redaction is recorded on model.warnings so the validation report surfaces it.
    assert any("guardrail blanked" in w for w in model.warnings)
    # A blanked margin must never surface as an out-of-range value in the emitted CSV.
    for c in fin.rows:
        if c.unit == "pct" and "margin" in c.label.lower() and c.value is not None:
            assert -100.0 <= c.value <= 100.0


def test_net_margin_exceeding_ebitda_margin_is_blanked():
    # Vitro's shape: net margin 74% while EBITDA margin is 23% — net income can't exceed EBITDA by
    # that gap, so the numerator is mis-scaled. Both are inside ±100%, so only the cross-margin check
    # catches it. (revenue 100, ebitda 23, net income 74 → net 74% > ebitda 23% + 20pp buffer.)
    ltm = {"revenue": 1_000.0, "ebitda": 234.0, "operating_income": 180.0, "net_income": 739.0,
           "total_equity": 5_000.0, "total_assets": 12_000.0, "cash": 100.0, "cfo": 200.0, "capex": 80.0}
    fund = Fundamentals(slug="scaleco", frame=None, periods=["2025-1T"], current_period="2025-1T",
                        ltm=ltm, annual={y: dict(ltm) for y in (2023, 2024, 2025)},
                        flow_keys={"revenue", "ebitda", "net_income", "operating_income", "cfo", "capex"})
    model = build_model(_spec(), fund, _pack())
    fin = {c.label: c for b in model.blocks if b.id == "financial_analysis" for c in b.rows}
    assert fin["EBITDA margin"].value is not None      # EBITDA margin stays (it's the reference)
    assert fin["Net margin"].value is None             # impossible net margin blanked
    assert "exceeds EBITDA margin" in fin["Net margin"].note


def test_in_band_returns_are_not_blanked():
    # A profitable subject with a normal ROE keeps its return figures.
    fund, pack, spec = _loss_fund(), _pack(), _spec()
    fund.ltm["net_income"] = 4_000  # ROE = 4000/40000 = 10% → in band
    model = build_model(spec, fund, pack)
    roe = next((c for b in model.blocks for c in b.rows if c.label == "ROE"), None)
    assert roe is not None and roe.value is not None and 0 < roe.value < 100


# --- sentinel refinement: a plausible share count means a cheap multiple is a real discount --------


def test_cheap_multiple_with_plausible_shares_is_a_note_not_broken():
    from src.coverage.validate import _share_count_plausible
    # 292mn shares (FINDEP-scale) is a normal listed count → plausible.
    assert _share_count_plausible(_P({}, subject={"shares_out": 292.0}))
    # A NI÷EPS blow-up (0.0005mn) or an absent count is implausible → the flag stands.
    assert not _share_count_plausible(_P({}, subject={"shares_out": 0.000476}))
    assert not _share_count_plausible(_P({}, subject={}))


# --- P/S sanity: a wrong live price (market cap ≪ revenue) is flagged, not silently shipped --------


def test_wrong_price_flagged_via_ps_band():
    # Vasconia's shape: price resolved to a penny look-alike → market cap 31mn on 2.4bn revenue →
    # P/S 0.013, far below the floor. Every price-driven multiple is then wrong, so it must sentinel.
    fund = _loss_fund()
    fund.ltm["revenue"] = 2_391.0
    pack = BloombergPack(slug="lossco",
                         subject={"px_last": 0.32, "shares_out": 96.7, "net_debt": 100.0,
                                  "minority_interest": 0.0, "total_equity": 1_000.0})
    spec = _spec()
    v = validate_model(build_model(spec, fund, pack), spec, fund, pack)
    assert any("P/Sales" in s and "out of" in s for s in v.sentinels), v.sentinels
    assert v.status == "BROKEN"


def test_sane_ps_not_flagged():
    # Normal price → market cap 45,000 on 100,000 revenue → P/S 0.45, in band → no P/S sentinel.
    fund, pack, spec = _loss_fund(), _pack(), _spec()
    v = validate_model(build_model(spec, fund, pack), spec, fund, pack)
    assert not any("P/Sales" in s for s in v.sentinels), v.sentinels


# --- un-sourced subject: blank required cells are a corpus gap (INCOMPLETE), not an engine bug -----


def test_unsourced_subject_is_incomplete_not_broken():
    # Aeromexico's shape: filings not cached → no revenue / net income / equity at all. A price
    # resolves, but every fundamental is blank. This is a corpus gap → missing_DATA, not a BROKEN bug.
    empty = Fundamentals(slug="nofilings", frame=None, periods=[], current_period="",
                         ltm={}, annual={}, flow_keys=set())
    pack = BloombergPack(slug="nofilings",
                         subject={"px_last": 167.0, "shares_out": 1_459.0, "net_debt": 0.0})
    spec = _spec()
    v = validate_model(build_model(spec, empty, pack), spec, empty, pack)
    assert not v.missing_engine, f"un-sourced subject must not report engine gaps: {v.missing_engine}"
    assert v.status != "BROKEN", f"status={v.status} sentinels={v.sentinels}"
