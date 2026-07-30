"""The market-data cache honours a TTL so a rebuild pulls FRESH prices (the cache had no expiry, which
is what froze the workbook's prices). Offline — exercises the cache-freshness predicate directly, no
network. Also covers the LAST-GOOD fallback: a throttled/failed live fetch reinstates the stale
cached quote instead of blanking the cell."""
from __future__ import annotations

import json
import os
import time

import pytest

from src.download import market_data


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Skip the polite 0.4s throttle sleep in _fetch_yahoo so the suite stays fast."""
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)


class _Resp:
    def __init__(self, status_code=200, text="{}"):
        self.status_code = status_code
        self.text = text


def _seed_chart_cache(cache_dir, symbol="TEST_MX", payload=None, age_seconds=20 * 3600):
    """Write a valid last-good chart-result cache file, aged `age_seconds` in the past."""
    payload = payload if payload is not None else {
        "meta": {"symbol": "TEST.MX"}, "timestamp": [1],
        "indicators": {"quote": [{"close": [123.45]}]}}
    p = cache_dir / f"yf_{symbol}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    st = p.stat()
    os.utime(p, (st.st_atime, time.time() - age_seconds))
    return p, payload


def _write(tmp_path, age_seconds: float, size: int = 200):
    p = tmp_path / "yf_TEST_MX.json"
    p.write_text("x" * size, encoding="utf-8")
    st = p.stat()
    os.utime(p, (st.st_atime, time.time() - age_seconds))
    return p


def test_fresh_within_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "18")
    p = _write(tmp_path, age_seconds=3600)          # 1h old, TTL 18h
    assert market_data._cache_fresh(p)


def test_stale_beyond_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "18")
    p = _write(tmp_path, age_seconds=20 * 3600)     # 20h old, TTL 18h → stale
    assert not market_data._cache_fresh(p)


def test_ttl_zero_always_refetches(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "0")  # daily-refresh driver's setting
    p = _write(tmp_path, age_seconds=1)             # 1s old, but TTL 0 → never fresh
    assert not market_data._cache_fresh(p)


def test_empty_cache_is_not_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "18")
    p = _write(tmp_path, age_seconds=1, size=10)    # <50 bytes → treated as absent
    assert not market_data._cache_fresh(p)


def test_missing_cache_is_not_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "18")
    assert not market_data._cache_fresh(tmp_path / "does_not_exist.json")


def test_default_ttl_when_unset(monkeypatch):
    monkeypatch.delenv("SOFT_MARKET_TTL_HOURS", raising=False)
    assert market_data._ttl_seconds() == 18 * 3600.0


def test_malformed_ttl_falls_back(monkeypatch):
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "not-a-number")
    assert market_data._ttl_seconds() == 18 * 3600.0


# --- last-good stale-cache fallback (the fix that fills the ~135 price-unresolved cells) -----------

def test_read_cache_stale_ignores_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "18")
    p, payload = _seed_chart_cache(tmp_path, age_seconds=100 * 3600)  # far beyond TTL
    assert not market_data._cache_fresh(p)                            # stale by TTL...
    assert market_data._read_cache_stale(p) == payload               # ...but last-good still served


def test_read_cache_stale_missing_or_empty(tmp_path):
    assert market_data._read_cache_stale(tmp_path / "nope.json") is None
    small = tmp_path / "small.json"
    small.write_text("x" * 10, encoding="utf-8")                     # <=50 bytes → treated as absent
    assert market_data._read_cache_stale(small) is None


def test_fetch_yahoo_throttle_serves_last_good(tmp_path, monkeypatch):
    monkeypatch.setattr(market_data, "_CACHE", tmp_path)
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "18")
    _, payload = _seed_chart_cache(tmp_path, age_seconds=20 * 3600)  # stale → live fetch attempted
    monkeypatch.setattr(market_data, "_session_get", lambda url, v: _Resp(status_code=429))
    assert market_data._fetch_yahoo("TEST.MX", verify_ssl=True) == payload   # 429 → last-good, not None


def test_fetch_yahoo_exception_serves_last_good(tmp_path, monkeypatch):
    monkeypatch.setattr(market_data, "_CACHE", tmp_path)
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "18")
    _, payload = _seed_chart_cache(tmp_path, age_seconds=20 * 3600)

    def _boom(url, v):
        raise RuntimeError("network down")
    monkeypatch.setattr(market_data, "_session_get", _boom)
    assert market_data._fetch_yahoo("TEST.MX", verify_ssl=True) == payload


def test_fetch_yahoo_no_cache_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(market_data, "_CACHE", tmp_path)
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "18")
    monkeypatch.setattr(market_data, "_session_get", lambda url, v: _Resp(status_code=429))
    # no cache on disk + failed fetch → genuinely None (nothing to fall back to)
    assert market_data._fetch_yahoo("NOCACHE.MX", verify_ssl=True) is None


def test_candidate_symbols_override_first_and_vintage_suffixes():
    # Verified-direct override symbol is tried FIRST (fast + no wrong-instrument risk).
    c = market_data.candidate_symbols("FVIA")
    assert c[0] == "FVIA16.MX"
    assert market_data.candidate_symbols("SPORT")[0] == "SPORTS.MX"
    # Big names the suffix scan misses; PE&OLES needs the '&' the _clean() would strip.
    assert market_data.candidate_symbols("LIVERPOL")[0] == "LIVEPOLC-1.MX"
    assert market_data.candidate_symbols("IDEAL")[0] == "IDEALB-1.MX"
    assert market_data.candidate_symbols("PE&OLES")[0] == "PE&OLES.MX"
    assert market_data.candidate_symbols("GCARSO")[0] == "GCARSOA1.MX"
    assert market_data.candidate_symbols("MFRISCO")[0] == "MFRISCOA-1.MX"
    # Vintage-year suffixes 15/16/17 are in the scan so other FIBRAs (e.g. FSHOP13/…) can resolve.
    tail = market_data.candidate_symbols("SOMEFIBRA")
    assert {"SOMEFIBRA15.MX", "SOMEFIBRA16.MX", "SOMEFIBRA17.MX"} <= set(tail)


def test_candidate_symbols_ticker_only_resolves_bmv_symbol():
    # A PDF-sourced name (no xbrl_ticker → empty clave) must still resolve via its trading ticker:
    # candidate_symbols("", ticker="BAFARB MM") yields BAFARB.MX FIRST (the ticker branch). This is
    # what the native.py `(clave or ticker)` guard relies on — an empty clave is tolerated.
    assert market_data.candidate_symbols("", ticker="BAFARB MM")[0] == "BAFARB.MX"
    # The ticker-derived symbol also leads when the ticker carries a series letter the bare clave lacks.
    assert market_data.candidate_symbols("GMEXICO", "GMEXICOB MM")[0] == "GMEXICOB.MX"


# --- exponential-backoff retry on transient throttle (the throttled-price fix) --------------------

def test_transient_status_classification():
    assert market_data._transient_status(429)          # throttle → retry
    assert market_data._transient_status(503)          # 5xx → retry
    assert not market_data._transient_status(404)      # wrong symbol → move on, don't retry
    assert not market_data._transient_status(200)


def test_backoff_retries_transient_then_succeeds(tmp_path, monkeypatch):
    # No cache on disk: WITHOUT retry a first-sweep 429 would blank the cell forever; WITH backoff
    # the third attempt lands the fetch and caches it. This is the core throttled-price recovery.
    monkeypatch.setattr(market_data, "_CACHE", tmp_path)
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "0")
    monkeypatch.setenv("SOFT_MARKET_MAX_RETRIES", "3")
    result0 = {"meta": {"symbol": "OK.MX", "currency": "MXN"}, "timestamp": [1],
               "indicators": {"quote": [{"close": [10.0]}]}}
    chart = {"chart": {"result": [result0]}}
    calls = {"n": 0}

    def flaky(url, v):
        calls["n"] += 1
        return (_Resp(status_code=429) if calls["n"] < 3
                else _Resp(status_code=200, text=json.dumps(chart)))
    monkeypatch.setattr(market_data, "_session_get", flaky)
    assert market_data._fetch_yahoo("OK.MX", verify_ssl=True) == result0
    assert calls["n"] == 3


def test_backoff_does_not_retry_permanent_status(monkeypatch):
    monkeypatch.setenv("SOFT_MARKET_MAX_RETRIES", "3")
    calls = {"n": 0}

    def notfound(url, v):
        calls["n"] += 1
        return _Resp(status_code=404)
    monkeypatch.setattr(market_data, "_session_get", notfound)
    resp = market_data._get_with_backoff("http://x", True)
    assert resp.status_code == 404 and calls["n"] == 1   # 404 permanent → single attempt, no backoff


def test_backoff_exhausts_on_persistent_throttle(monkeypatch):
    monkeypatch.setenv("SOFT_MARKET_MAX_RETRIES", "2")
    calls = {"n": 0}

    def throttled(url, v):
        calls["n"] += 1
        return _Resp(status_code=429)
    monkeypatch.setattr(market_data, "_session_get", throttled)
    resp = market_data._get_with_backoff("http://x", True)
    assert resp.status_code == 429 and calls["n"] == 3   # 1 initial + 2 retries, then give up


# --- itype guard: recover mislabeled-MUTUALFUND equities without reviving the fund look-alike bug ---

def _chart_result(symbol, itype, closes=(10.0, 11.0, 12.0)):
    meta = {"symbol": symbol, "currency": "MXN"}
    if itype is not None:
        meta["instrumentType"] = itype
    return {"meta": meta, "timestamp": list(range(len(closes))),
            "indicators": {"quote": [{"close": list(closes)}]}}


def test_fetch_prices_prefers_equity_over_earlier_fund(monkeypatch):
    # Regional case preserved: a fund look-alike (RB.MX) appears FIRST but the real equity (RA.MX) must
    # still win — the non-equity is only a fallback, never returned when an equity resolves.
    monkeypatch.setattr(market_data, "candidate_symbols", lambda clave, ticker=None: ["RB.MX", "RA.MX"])
    results = {"RB.MX": _chart_result("RB.MX", "MUTUALFUND", (1056.0, 1056.0, 1056.0)),
               "RA.MX": _chart_result("RA.MX", "EQUITY", (100.0, 100.0, 100.0))}
    monkeypatch.setattr(market_data, "_fetch_yahoo", lambda sym, *, verify_ssl: results.get(sym))
    out = market_data.fetch_prices("REGIONAL")
    assert out["symbol"] == "RA.MX" and out["last"] == 100.0    # equity wins; fund fallback unused


def test_fetch_prices_accepts_mislabeled_equity_when_no_equity(monkeypatch):
    # GOMO/ALFA/… : Yahoo mislabels the only resolving symbol as MUTUALFUND though it carries a genuine
    # daily series → with no equity candidate, the fallback recovers it (this is Tier-B, 43 cells).
    monkeypatch.setattr(market_data, "candidate_symbols", lambda clave, ticker=None: ["GOMO.MX"])
    monkeypatch.setattr(market_data, "_fetch_yahoo",
                        lambda sym, *, verify_ssl: _chart_result("GOMO.MX", "MUTUALFUND", (7.0, 8.0, 9.0)))
    out = market_data.fetch_prices("GOMO")
    assert out["symbol"] == "GOMO.MX" and out["last"] == 9.0


def test_fetch_prices_rejects_stub_without_series(monkeypatch):
    # A candidate with neither a timestamp series nor a regularMarketPrice is not a real quote → skip.
    monkeypatch.setattr(market_data, "candidate_symbols", lambda clave, ticker=None: ["X.MX"])
    monkeypatch.setattr(market_data, "_fetch_yahoo", lambda sym, *, verify_ssl: {"meta": {"symbol": "X.MX"}})
    out = market_data.fetch_prices("X")
    assert out["last"] is None and out["symbol"] is None


def test_fetch_yahoo_success_then_throttle_reuses(tmp_path, monkeypatch):
    monkeypatch.setattr(market_data, "_CACHE", tmp_path)
    monkeypatch.setenv("SOFT_MARKET_TTL_HOURS", "0")                 # never fresh → always re-fetch
    result0 = {"meta": {"symbol": "OK.MX", "currency": "MXN"}, "timestamp": [1, 2, 3],
               "indicators": {"quote": [{"close": [10.0, 11.0, 12.0]}]}}
    chart = {"chart": {"result": [result0]}}
    monkeypatch.setattr(market_data, "_session_get",
                        lambda url, v: _Resp(status_code=200, text=json.dumps(chart)))
    first = market_data._fetch_yahoo("OK.MX", verify_ssl=True)
    assert first == result0                                         # fresh success, now cached
    # network starts throttling; TTL=0 still forces a fetch attempt → falls back to the just-cached value
    monkeypatch.setattr(market_data, "_session_get", lambda url, v: _Resp(status_code=503))
    assert market_data._fetch_yahoo("OK.MX", verify_ssl=True) == first   # last-good, not None
