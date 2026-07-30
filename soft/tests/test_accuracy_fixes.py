"""Round-2 accuracy fixes — offline, synthetic (no network).

Covers: FEMSA units config, the FIBRA fair-value annotation, the P/E≈P/BV/(ROE) consistency sentinel
(validate.py), and the Yahoo no-corpus fallback blanking a self-inconsistent P/E (Grupo Bafar-shaped).
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bloomberg.schema import BloombergPack
from src.coverage.fundamentals import Fundamentals
from src.coverage.spec import CoverageSpec
from src.coverage.valuation import Block, Cell, CoverageModel, _poison_impossible_cells
from src.coverage.validate import validate_model


def _model_with(pe, pbv, roe, template="industrial") -> CoverageModel:
    return CoverageModel(
        slug="x", name="X", currency="MXN", units="millions", current_period="2026-1T",
        template=template,
        blocks=[Block("snapshot_multiples", "Snapshot", [Cell("P/E (LTM)", pe, "x", "calc")]),
                Block("financial_analysis", "FA", [Cell("P/BV", pbv, "x", "calc"),
                                                   Cell("ROE", roe, "pct", "calc")])])


def _spec(template="industrial") -> CoverageSpec:
    return CoverageSpec(slug="x", name="X", ticker="X MM", currency="MXN", units="millions",
                        template=template, blocks=[], peers=[])


def _fund() -> Fundamentals:
    return Fundamentals(slug="x", frame=None, periods=["2026-1T"], current_period="2026-1T",
                        ltm={"revenue": 1000.0})


# --- FEMSA units (the 5× market-cap bug) -----------------------------------------------------------

def test_femsa_config_has_shares_per_unit():
    cfg = yaml.safe_load((ROOT / "configs" / "femsa.yaml").read_text(encoding="utf-8"))
    assert cfg["company"]["shares_per_unit"] == 5    # UBD = 5 ord shares → cap ÷5 (was 13.1 P/BV)


# --- FIBRA fair-value annotation (kept, per the user) ----------------------------------------------

def test_reit_earnings_cells_annotated():
    m = _model_with(pe=4.2, pbv=1.0, roe=5.0, template="reit")
    _poison_impossible_cells(m)
    pe = m.blocks[0].rows[0]
    assert pe.value == 4.2 and "fair-value" in pe.note        # kept + caveat


def test_industrial_pe_not_annotated():
    m = _model_with(pe=4.2, pbv=1.0, roe=5.0, template="industrial")
    _poison_impossible_cells(m)
    assert "fair-value" not in (m.blocks[0].rows[0].note or "")


# --- P/E ≈ P/BV/(ROE) consistency sentinel (validate.py, advisory note) -----------------------------

def test_consistency_note_fires_on_mismatch():
    m = _model_with(pe=28.1, pbv=3.43, roe=31.6)             # implied 10.85 → 61% gap (Bafar-shaped)
    v = validate_model(m, _spec(), _fund(), BloombergPack(slug="x"))
    assert any("consistency" in n for n in v.notes)


def test_consistency_note_clean_when_reconciled():
    m = _model_with(pe=11.0, pbv=3.43, roe=31.6)             # implied 10.85 → ~1% gap
    v = validate_model(m, _spec(), _fund(), BloombergPack(slug="x"))
    assert not any("consistency" in n for n in v.notes)


def test_consistency_note_skips_reit():
    m = _model_with(pe=4.2, pbv=1.0, roe=5.0, template="reit")  # huge gap, but reit → identity N/A
    v = validate_model(m, _spec("reit"), _fund(), BloombergPack(slug="x"))
    assert not any("consistency" in n for n in v.notes)


# --- Yahoo no-corpus fallback: blank a self-inconsistent P/E ---------------------------------------

def test_yahoo_fallback_blanks_inconsistent_pe(monkeypatch):
    from scripts import build_master as bm
    from src.download import market_data
    monkeypatch.setattr(market_data, "fetch_key_stats",
                        lambda clave, verify_ssl=True: {"trailing_pe": 28.1, "price_to_book": 3.43,
                                                        "roe": 31.6, "profit_margin": 10.0,
                                                        "ev_ebitda": 9.0, "dividend_yield": 2.0})
    monkeypatch.setattr(market_data, "fetch_prices",
                        lambda clave, years, verify_ssl=True: {"last": 100.0, "dvd_ttm": 2.0})
    vmap = bm._yahoo_fallback_vmap("BAFAR", "industrial")
    assert "P/E (LTM)" not in vmap.get("snapshot_multiples", {})   # inconsistent P/E dropped
    assert vmap["financial_analysis"]["P/BV"] == 3.43             # reconciling cells kept


def test_yahoo_fallback_keeps_consistent_pe(monkeypatch):
    from scripts import build_master as bm
    from src.download import market_data
    monkeypatch.setattr(market_data, "fetch_key_stats",
                        lambda clave, verify_ssl=True: {"trailing_pe": 11.0, "price_to_book": 3.43,
                                                        "roe": 31.6})
    monkeypatch.setattr(market_data, "fetch_prices",
                        lambda clave, years, verify_ssl=True: {"last": 100.0})
    vmap = bm._yahoo_fallback_vmap("SOMECO", "industrial")
    assert vmap["snapshot_multiples"]["P/E (LTM)"] == 11.0        # reconciles → kept


# --- native price guard: a ticker-only name (no xbrl_ticker) still fetches prices -----------------

def _native_spec(ticker):
    from src.coverage.spec import CoverageSpec
    return CoverageSpec(slug="synthetic_bafar", name="X", ticker=ticker, currency="MXN",
                        units="millions", template="industrial", blocks=[], peers=[])


def test_native_price_fetch_fires_for_ticker_only_name(monkeypatch):
    """Grupo Bafar has a trading ticker but no xbrl_ticker → clave is None. The subject price fetch
    must still fire off the ticker; previously `if with_prices and clave:` skipped it entirely, so
    every price-derived cell (P/E, EV/EBITDA, P/BV, dividend/FCF yield) stayed permanently blank."""
    from src.coverage import native
    calls = []

    def _rec_prices(clave, years, ticker=None, verify_ssl=True, **kw):
        calls.append({"clave": clave, "ticker": ticker})
        return {"last": None, "fy_close": {}, "symbol": None, "dvd_ttm": None}

    monkeypatch.setattr(native, "_clave_and_ticker", lambda slug: (None, "BAFARB MM"))
    monkeypatch.setattr(native, "_native_shares_out", lambda *a, **k: None)
    monkeypatch.setattr(native.market_data, "fetch_prices", _rec_prices)
    native.build_native_pack(_native_spec("BAFARB MM"), _fund(), with_prices=True,
                             with_macro=False, with_peers=False, offline=True, verify_ssl=False)
    assert calls, "price fetch must fire for a ticker-only name"
    assert calls[0]["ticker"] == "BAFARB MM"     # the ticker is passed through to resolve BAFARB.MX


def test_native_price_fetch_skipped_when_neither_clave_nor_ticker(monkeypatch):
    """The guard is additive, not unconditional: a name with neither an XBRL clave nor a ticker still
    skips the fetch (no junk `.MX` candidates churned)."""
    from src.coverage import native
    calls = []
    monkeypatch.setattr(native, "_clave_and_ticker", lambda slug: (None, None))
    monkeypatch.setattr(native, "_native_shares_out", lambda *a, **k: None)
    monkeypatch.setattr(native.market_data, "fetch_prices",
                        lambda *a, **k: calls.append(1) or {"last": None})
    native.build_native_pack(_native_spec(None), _fund(), with_prices=True,
                             with_macro=False, with_peers=False, offline=True, verify_ssl=False)
    assert not calls, "no clave and no ticker → price fetch must be skipped"


# --- modeled-name Yahoo key-stats backfill: ROE / Net margin / shares_out -------------------------

def _fund_ltm(**ltm):
    return Fundamentals(slug="x", frame=None, periods=["2026-1T"], current_period="2026-1T", ltm=ltm)


def _fin_rows(fund, yahoo_stats):
    from src.coverage.valuation import block_financial
    pack = BloombergPack(slug="x")
    pack.yahoo_stats = dict(yahoo_stats)
    blk = block_financial(_spec(), fund, pack)
    return {c.label: c for c in blk.rows}, pack


def test_block_financial_yahoo_fills_blank_roe_and_net_margin():
    # No filing net income / equity / revenue → both calcs blank → Yahoo's own stats fill them.
    rows, pack = _fin_rows(_fund_ltm(), {"roe": 18.0, "profit_margin": 12.5})
    assert rows["ROE"].value == 18.0 and "Yahoo" in (rows["ROE"].note or "")
    assert rows["Net margin"].value == 12.5 and "Yahoo" in (rows["Net margin"].note or "")
    assert {"roe", "net_margin"} <= pack.yahoo_filled


def test_block_financial_filing_value_wins_over_yahoo():
    # Filing ni/rev/equity present → computed value used; Yahoo never overrides, no provenance tag.
    rows, pack = _fin_rows(_fund_ltm(revenue=1000.0, net_income=150.0, total_equity=600.0),
                           {"roe": 18.0, "profit_margin": 12.5})
    assert rows["Net margin"].value == 15.0 and "Yahoo" not in (rows["Net margin"].note or "")
    assert rows["ROE"].value == 25.0 and "Yahoo" not in (rows["ROE"].note or "")
    assert "roe" not in pack.yahoo_filled and "net_margin" not in pack.yahoo_filled


def test_block_financial_rejects_out_of_band_yahoo():
    # Implausible Yahoo stats (roe 500%, margin 250%) are rejected → cell stays blank (blank-not-wrong).
    rows, _ = _fin_rows(_fund_ltm(), {"roe": 500.0, "profit_margin": 250.0})
    assert "ROE" not in rows and "Net margin" not in rows


def test_native_shares_out_yahoo_fallback(monkeypatch):
    """A no-shares modeled name (no XBRL count, no config override, no ni/eps) takes Yahoo's
    sharesOutstanding as a last resort so market cap / EV / multiples can compute (the Bafar case)."""
    from src.coverage import native
    monkeypatch.setattr(native, "_clave_and_ticker", lambda slug: (None, "BAFARB MM"))
    monkeypatch.setattr(native, "_native_shares_out", lambda *a, **k: None)
    monkeypatch.setattr(native.market_data, "fetch_prices",
                        lambda *a, **k: {"last": 100.0, "fy_close": {}, "symbol": "BAFARB.MX", "dvd_ttm": None})
    monkeypatch.setattr(native.market_data, "fetch_key_stats",
                        lambda *a, **k: {"shares_out": 316.0, "dividend_yield": None})
    pack = native.build_native_pack(_native_spec("BAFARB MM"), _fund(), with_prices=True,
                                    with_macro=False, with_peers=False, offline=False, verify_ssl=False)
    assert pack.subject.get("shares_out") == 316.0
    assert "shares_out" in pack.yahoo_filled


def test_native_shares_out_not_overridden_when_present(monkeypatch):
    """A real filing-derived share count (ni/eps here) always wins over Yahoo's."""
    from src.coverage import native
    monkeypatch.setattr(native, "_clave_and_ticker", lambda slug: (None, "BAFARB MM"))
    monkeypatch.setattr(native, "_native_shares_out", lambda *a, **k: None)
    monkeypatch.setattr(native.market_data, "fetch_prices",
                        lambda *a, **k: {"last": 100.0, "fy_close": {}, "symbol": "BAFARB.MX", "dvd_ttm": None})
    monkeypatch.setattr(native.market_data, "fetch_key_stats",
                        lambda *a, **k: {"shares_out": 316.0, "dividend_yield": None})
    fund = _fund_ltm(revenue=1000.0, shares_out=500.0)   # ni/eps-derived count already present
    pack = native.build_native_pack(_native_spec("BAFARB MM"), fund, with_prices=True,
                                    with_macro=False, with_peers=False, offline=False, verify_ssl=False)
    assert pack.subject.get("shares_out") == 500.0
    assert "shares_out" not in pack.yahoo_filled
