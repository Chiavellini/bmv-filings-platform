"""Reconciliation workflow — the checks that catch faulty calcs the arithmetic gate misses.

Offline tests prove the deterministic layers (structural + golden) flag the exact error classes we
have hit — including the Herdez case (net margin > EBITDA margin) that slipped past the plausibility
band and was only caught by eye. One @network test proves the Yahoo oracle resolves + agrees.
"""
import pytest

from src.coverage.reconcile import (
    CompanyValues, structural_checks, golden_checks, oracle_checks,
)


def _metrics(findings):
    return {f.metric for f in findings}


def test_structural_flags_net_income_exceeding_ebitda():
    """The Herdez-class error: net margin 63.9% vs EBITDA margin 16.5% — no oracle needed."""
    v = CompanyValues(template="industrial", net_margin=63.9, ebitda_margin=16.5, revenue=1000,
                      market_cap=5000, price=10, shares_out=500)
    findings = structural_checks("Herdez", v)
    assert "Net margin" in _metrics(findings)
    nm = next(f for f in findings if f.metric == "Net margin")
    assert nm.root_cause == "net_income scale"


def test_structural_passes_normal_company():
    v = CompanyValues(template="industrial", net_margin=32.8, ebitda_margin=56.1, revenue=342000,
                      price=199.35, shares_out=8898.27, market_cap=1773870.0,
                      net_debt=11936.9, ev=1785806.9)
    assert structural_checks("Grupo Mexico", v) == []


def test_structural_flags_impossible_margin():
    v = CompanyValues(template="industrial", ebitda_margin=226.4, net_margin=198.1, revenue=100,
                      market_cap=50, price=1, shares_out=50)
    metrics = _metrics(structural_checks("CIE", v))
    assert "EBITDA margin" in metrics and "Net margin" in metrics


def test_structural_flags_market_cap_identity_break():
    v = CompanyValues(template="industrial", price=10.0, shares_out=100.0, market_cap=5000.0,
                      revenue=2000)
    assert "Market cap" in _metrics(structural_checks("X", v))


def test_structural_flags_ev_bridge_break():
    v = CompanyValues(template="industrial", price=10, shares_out=100, market_cap=1000,
                      net_debt=200, ev=2500, revenue=800)  # EV should be ~1200
    assert "Enterprise value" in _metrics(structural_checks("X", v))


def test_ev_bridge_ties_with_minority_interest():
    """EV = mktcap + net_debt + minority interest. A real minority stake (the AC/Herdez class) must
    NOT be flagged as an EV-bridge defect when the CSV itemizes minority interest."""
    v = CompanyValues(template="industrial", price=206.84, shares_out=1763.7, market_cap=364803.7,
                      net_debt=32438.9, minority_interest=32517.8, ev=429760.4, revenue=250000)
    assert "Enterprise value" not in _metrics(structural_checks("AC", v))


def test_ev_bridge_minority_residual_expected_when_not_itemized():
    """Older CSV with no minority-interest row: a residual consistent with a plausible minority
    interest is 'expected' (audit trail), not an actionable defect."""
    v = CompanyValues(template="industrial", price=206.84, shares_out=1763.7, market_cap=364803.7,
                      net_debt=32438.9, minority_interest=None, ev=429760.4, revenue=250000)
    findings = [f for f in structural_checks("AC", v) if f.metric == "Enterprise value"]
    assert findings and all(f.kind == "expected" for f in findings)


def test_ev_bridge_oversized_residual_stays_real():
    """A residual too large to be minority interest (GMD: 3,010 vs mktcap 1,944) is a real defect
    even without an itemized minority-interest row."""
    v = CompanyValues(template="industrial", price=10, shares_out=100, market_cap=1944,
                      net_debt=0, minority_interest=None, ev=4954, revenue=3000)
    findings = [f for f in structural_checks("GMD", v) if f.metric == "Enterprise value"]
    assert findings and all(f.kind == "real" for f in findings)


def test_structural_flags_bad_price_to_sales():
    # 3× share count (CPO-vs-ordinary) → market cap 3× → P/S absurd
    v = CompanyValues(template="industrial", price=15, shares_out=43201, market_cap=648015,
                      revenue=200)  # P/S = 3240
    assert "P/Sales" in _metrics(structural_checks("Cemex", v))


def test_structural_flags_pe_on_loss_maker():
    v = CompanyValues(template="industrial", pe=12.0, net_margin=-8.0, revenue=1000,
                      market_cap=500, price=10, shares_out=50)
    assert "P/E" in _metrics(structural_checks("X", v))


def test_golden_flags_out_of_range_shares():
    v = CompanyValues(template="industrial", shares_out=43201.0)
    entry = {"shares_out_mn": [13500, 15500]}
    findings = golden_checks("Cemex", v, entry)
    assert "Shares out (mn)" in _metrics(findings)


def test_golden_passes_in_range():
    v = CompanyValues(template="industrial", shares_out=17292.0, pe=17.3, net_margin=4.9, roe=20.2)
    entry = {"shares_out_mn": [16000, 18500], "pe": [15, 34], "net_margin": [3.5, 8], "roe": [15, 35]}
    assert golden_checks("Walmex", v, entry) == []


def test_oracle_flags_divergence_and_attributes_shares_root_cause():
    ours = CompanyValues(template="industrial", shares_out=43201.0, pe=2003.0, market_cap=648015.0)
    yahoo = {"shares_out": 14600.0, "market_cap": 315000.0, "trailing_pe": 35.0}
    findings = oracle_checks("Cemex", ours, yahoo)
    metrics = _metrics(findings)
    assert "P/E" in metrics and "Shares out (mn)" in metrics
    pe = next(f for f in findings if f.metric == "P/E")
    assert "shares_out" in pe.root_cause  # multiple error attributed to the wrong share count


def test_oracle_none_is_graceful():
    v = CompanyValues(template="industrial", pe=15.0)
    assert oracle_checks("X", v, None) == []


def test_oracle_ev_ebitda_yahoo_outlier_is_noise():
    """Our EV/EBITDA is in-band and Yahoo's is absurd → the disagreement is Yahoo's denominator
    artifact, classed 'noise' (not an engine defect)."""
    v = CompanyValues(template="industrial", ev_ebitda=8.4)
    findings = oracle_checks("Grupo Mexico", v, {"ev_ebitda": 144.1})
    ev = next(f for f in findings if f.metric == "EV/EBITDA")
    assert ev.kind == "noise"


def test_oracle_absolute_shares_disagreement_is_noise():
    """Yahoo is unreliable for BMV absolute share counts → a Shares out disagreement is noise; the
    real share defects are caught network-free by structural/golden."""
    v = CompanyValues(template="industrial", shares_out=16807.0)
    findings = oracle_checks("KOF", v, {"shares_out": 525.0})
    sh = next(f for f in findings if f.metric == "Shares out (mn)")
    assert sh.kind == "noise"


def test_oracle_pbv_divergence_stays_real():
    """P/BV is price-based and currency-neutral → a real disagreement (the USD-currency class) must
    stay actionable, never suppressed."""
    v = CompanyValues(template="industrial", pbv=5.5)
    findings = oracle_checks("Nemak", v, {"price_to_book": 0.31})
    pbv = next(f for f in findings if f.metric == "P/BV")
    assert pbv.kind == "real"


def test_oracle_dividend_yield_methodology_is_noise():
    """Our trailing-actual yield vs Yahoo's indicated/forward yield is a systematic methodology gap
    (verified: non-payers this year read 0 while Yahoo shows last year's rate)."""
    v = CompanyValues(template="industrial", div_yield=3.0)
    f = next(x for x in oracle_checks("AC", v, {"dividend_yield": 0.62}) if x.metric == "Dividend yield")
    assert f.kind == "noise"


def test_oracle_bank_net_margin_is_noise():
    """A financial's net margin uses a different revenue base than Yahoo → not comparable."""
    v = CompanyValues(template="financials", net_margin=48.0)
    f = next(x for x in oracle_checks("Regional", v, {"profit_margin": 31.0}) if x.metric == "Net margin")
    assert f.kind == "noise"


def test_oracle_pbv_yahoo_below_band_is_noise():
    """Ours in the plausible P/BV band and Yahoo absurdly below it (wrong instrument) → noise."""
    v = CompanyValues(template="industrial", pbv=1.9)
    f = next(x for x in oracle_checks("Value GF", v, {"price_to_book": 0.12}) if x.metric == "P/BV")
    assert f.kind == "noise"


def test_oracle_roe_opposite_sign_is_noise():
    """Ours a sane positive ROE, Yahoo a large NEGATIVE (different instrument/equity base) → noise."""
    v = CompanyValues(template="industrial", roe=5.6)
    f = next(x for x in oracle_checks("Consorcio ARA", v, {"roe": -47.6}) if x.metric == "ROE")
    assert f.kind == "noise"


def test_structural_pe_on_loss_is_expected_not_real():
    v = CompanyValues(template="industrial", pe=12.0, net_margin=-8.0, revenue=1000,
                      market_cap=500, price=10, shares_out=50)
    pe = next(f for f in structural_checks("X", v) if f.metric == "P/E")
    assert pe.kind == "expected"


@pytest.mark.network
def test_fetch_key_stats_walmex_sane():
    from src.download.market_data import fetch_key_stats
    ks = fetch_key_stats("WALMEX", ticker="WALMEX* MM", verify_ssl=False)
    if ks is None:
        pytest.skip("Yahoo quoteSummary unreachable (crumb/network)")
    assert 15000 < ks["shares_out"] < 20000        # ~17.3bn shares
    assert 10 < ks["trailing_pe"] < 40
    assert 10 < ks["roe"] < 35
