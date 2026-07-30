"""Native price fetcher — free share prices for BMV names (no Bloomberg).

Provider is Yahoo Finance's public chart JSON (no API key). BMV equities resolve as
``<TRADING_SYMBOL>.MX`` where the trading symbol carries a series letter (GMEXICO**B**, GFNORTE**O**)
the bare XBRL clave lacks — so we try the config trading ticker first, then a set of series
candidates, and cache the one that resolves. Returns the last price + each fiscal-year-close price
(for the historical band); unresolved tickers return None → the caller leaves those cells for the
Bloomberg residual.

(Stooq was the first choice but now serves a JS proof-of-work wall on its CSV endpoint; Yahoo is the
working native source. The provider stays pluggable.)
"""
from __future__ import annotations

import json
from pathlib import Path

from src.shared.paths import PROJECT_ROOT

_CACHE = PROJECT_ROOT / ".cache" / "market_data"
YF_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=6y&interval=1d&events=div"


def _ttl_seconds() -> float:
    """Market-data cache lifetime in seconds, from ``SOFT_MARKET_TTL_HOURS`` (default 18h).

    Prices and Yahoo key-stats change daily, so a cached quote is reused only while younger than the
    TTL; older → re-fetched. This is what makes a rebuild pull FRESH prices (the cache had no expiry,
    so every rebuild froze the first-fetched price forever). ``SOFT_MARKET_TTL_HOURS=0`` forces every
    quote to re-fetch (the daily-refresh driver sets this); a large value pins the cache for offline
    reproducibility. Malformed values fall back to the default."""
    import os
    raw = os.environ.get("SOFT_MARKET_TTL_HOURS")
    if raw is None:
        return 18 * 3600.0
    try:
        return max(0.0, float(raw)) * 3600.0
    except (TypeError, ValueError):
        return 18 * 3600.0


def _cache_fresh(cache) -> bool:
    """True when a cache file exists, is non-empty, and is younger than the TTL."""
    import time
    try:
        st = cache.stat()
    except OSError:
        return False
    if st.st_size <= 50:
        return False
    return (time.time() - st.st_mtime) < _ttl_seconds()


def _read_cache_stale(cache) -> dict | None:
    """Read + parse a cache file regardless of TTL freshness — the LAST-GOOD value on disk.

    Fallback for a throttled/failed live fetch: a transient Yahoo 429 must not blank a cell that has
    a perfectly good previous quote cached. Returns the parsed JSON, or None if the file is
    absent/empty/unreadable. (``_cache_fresh`` gates the *fresh* reuse path; this ignores the TTL so
    a stale-but-valid quote still beats a blank.)"""
    try:
        if cache.stat().st_size <= 50:
            return None
    except OSError:
        return None
    try:
        return json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        return None

# Series suffixes tried when resolving a bare clave to a Yahoo symbol (most common on the BMV).
# The trailing numerics are the FIBRA (real-estate trust) CBFI series — FUNO11, DANHOS13, FIBRAPL14,
# FMTY14, FINN13, FSHOP13, TERRA13, FIBRAMQ12 — kept LAST so ordinary equities resolve on an earlier
# candidate first (first EQUITY hit wins).
_SERIES = ["", "B", "O", "A", "CPO", "UBC", "UB", "1", "L", "C", "B1", "N",
           "13", "14", "11", "12", "18", "15", "16", "17"]  # 15/16/17: FIBRAHD15, FVIA16, FNOVA17…

# Explicit clave → Yahoo symbol(s) for names Yahoo lists under a form the suffix scan can't build
# (verified live). Tried FIRST so they resolve directly (fast + no wrong-instrument risk).
_YAHOO_OVERRIDE: dict[str, list[str]] = {
    "SPORT": ["SPORTS.MX"],          # Grupo Sports World — Yahoo pluralises the ticker
    # FIBRAs Yahoo lists under a vintage-year suffix; pinned so they resolve directly (the suffix
    # scan would also find them, but only after ~15 wrong-candidate live fetches).
    "FVIA": ["FVIA16.MX"], "FIBRAHD": ["FIBRAHD15.MX"], "FNOVA": ["FNOVA17.MX"],
    "FPLUS": ["FPLUS16.MX"],
    # Big names the suffix scan misses (verified live). PEOLES: _clean strips the '&' so PE&OLES.MX
    # can't be built from the clave; LIVERPOL/IDEAL/GCARSO/MFRISCO list under a series-suffixed symbol.
    "PEOLES": ["PE&OLES.MX"], "LIVERPOL": ["LIVEPOLC-1.MX"], "IDEAL": ["IDEALB-1.MX"],
    "GCARSO": ["GCARSOA1.MX"], "MFRISCO": ["MFRISCOA-1.MX"],
}


def _clean(clave: str) -> str:
    return clave.strip().upper().replace("&", "").replace("*", "").replace(".", "").replace(" ", "")


def candidate_symbols(clave: str, ticker: str | None = None) -> list[str]:
    """Ordered Yahoo symbol candidates for a BMV name."""
    cands: list[str] = []
    for sym in _YAHOO_OVERRIDE.get(_clean(clave).upper(), ()):  # verified direct symbol first
        cands.append(sym)
    if ticker:  # config trading ticker, e.g. "GMEXICOB MM" → GMEXICOB.MX
        base = _clean(ticker.split()[0])
        if base:
            cands.append(f"{base}.MX")
    base = _clean(clave)
    for suf in _SERIES:
        s = f"{base}{suf}.MX"
        if s not in cands:
            cands.append(s)
    return cands


# Yahoo 429s the shared Retry-session (it retries 429 and cascades); a plain browser-UA GET works,
# same as curl. Keep one Session for connection reuse but with NO retry adapter.
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
_SESSION = None


def _session_get(url: str, verify_ssl: bool):
    import requests
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session()
        _SESSION.headers.update(_UA)
    return _SESSION.get(url, timeout=10, verify=verify_ssl)


def _transient_status(status) -> bool:
    """True for HTTP statuses worth retrying: 429 (throttle) and 5xx. A permanent status (e.g. a 404
    wrong-symbol candidate) is NOT transient — the caller should move to the next candidate symbol
    immediately, not burn backoff on a symbol that will never resolve."""
    return status == 429 or (isinstance(status, int) and 500 <= status < 600)


def _retry_cfg() -> tuple[int, float]:
    """(max_retries, backoff_base_seconds) from env, defaulting to (3, 1.0). Kept modest on purpose:
    per-symbol backoff cures a transient blip, while sustained throttling is handled by the caller's
    straggler re-fetch pass (a longer cooldown between whole sweeps beats a long per-symbol wait)."""
    import os
    try:
        n = int(os.environ.get("SOFT_MARKET_MAX_RETRIES", "3"))
    except ValueError:
        n = 3
    try:
        base = float(os.environ.get("SOFT_MARKET_BACKOFF_BASE", "1.0"))
    except ValueError:
        base = 1.0
    return max(0, n), max(0.0, base)


def _get_with_backoff(url: str, verify_ssl: bool):
    """GET with exponential backoff + jitter on a transient throttle (429) / 5xx / network error.
    Returns the final Response (which may itself be a non-200) or None if every attempt raised.
    Only transient failures are retried; a permanent status returns on the first try so the candidate
    scan stays fast. A 429's ``Retry-After`` header (when present) sets a floor on the wait."""
    import time
    import random
    retries, base = _retry_cfg()
    resp = None
    for attempt in range(retries + 1):
        try:
            resp = _session_get(url, verify_ssl)
        except Exception:
            resp = None
        status = getattr(resp, "status_code", 200) if resp is not None else None
        if resp is not None and not _transient_status(status):
            return resp                              # 200 or a permanent error → done, no retry
        if attempt >= retries:
            return resp                              # retries exhausted → hand back last resp / None
        delay = base * (2 ** attempt) + random.uniform(0.0, base)
        if resp is not None and status == 429 and hasattr(resp, "headers"):
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    delay = max(delay, float(ra))
                except (TypeError, ValueError):
                    pass
        time.sleep(delay)
    return resp


def _fetch_yahoo(symbol: str, *, verify_ssl: bool) -> dict | None:
    """Fetch Yahoo chart JSON for one symbol (cached). Returns the result dict or None.

    Retries a transient throttle/5xx/network error with exponential backoff (``_get_with_backoff``)
    before giving up — the fix for the ``throttled-price`` gaps where a first-time fetch of a
    never-cached symbol failed under a full-sweep 429 storm. On a persistent failure, falls back to
    the cached value even when older than the TTL (``_read_cache_stale``) — a transient Yahoo 429
    must not blank a cell that has a good last-good price on disk. Returns None only when the fetch
    fails AND no cache exists (the caller then tries the next candidate symbol)."""
    _CACHE.mkdir(parents=True, exist_ok=True)
    cache = _CACHE / f"yf_{symbol.replace('.', '_')}.json"
    if _cache_fresh(cache):
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass
    import time
    time.sleep(0.4)  # polite throttle — Yahoo 429s rapid bursts
    resp = _get_with_backoff(YF_URL.format(sym=symbol), verify_ssl)
    if resp is None or getattr(resp, "status_code", 200) != 200:
        return _read_cache_stale(cache)              # exhausted retries / error → last-good price
    try:
        data = json.loads(resp.text if hasattr(resp, "text") else str(resp))
    except Exception:
        return _read_cache_stale(cache)              # parse failure → last-good price
    result = (data.get("chart") or {}).get("result")
    if not result:
        return _read_cache_stale(cache)              # empty payload → last-good price
    cache.write_text(json.dumps(result[0]), encoding="utf-8")
    return result[0]


def _series_from_result(result: dict) -> list[tuple[str, float]]:
    """Yahoo result → sorted [(YYYY-MM-DD, close)]."""
    import datetime as _dt
    ts = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    out: list[tuple[str, float]] = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = _dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d")
        out.append((d, float(c)))
    return sorted(out)


def trailing_dividends(result: dict) -> float | None:
    """Sum the cash dividends paid in the trailing 12 months from the chart's dividend events (the
    same reliable endpoint that yields prices — no crumb-gated quoteSummary call). None if the symbol
    reports no dividend events. Uses only past ex-dates within the last 365 days.

    De-spikes a lone extreme outlier: a single payment that dwarfs the median of the others (>5×) is a
    special dividend or a mis-coded split (e.g. Herdez's 15.0-peso event among ~0.75 regulars → a 30%
    'yield' that gets blanked). Excluding it leaves the RECURRING yield, the useful comparable figure."""
    import datetime as _dt
    import statistics
    events = ((result.get("events") or {}).get("dividends") or {})
    if not events:
        return None
    now = _dt.datetime.utcnow().timestamp()
    horizon = now - 366 * 86400
    amts = [float(ev["amount"]) for ev in events.values()
            if ev.get("date") is not None and ev.get("amount") is not None
            and horizon <= ev["date"] <= now]
    if not amts:
        return None
    if len(amts) >= 3:                    # enough to judge one payment against its peers
        srt = sorted(amts)
        med_rest = statistics.median(srt[:-1])
        if med_rest > 0 and srt[-1] > 5 * med_rest:
            amts = srt[:-1]               # drop the lone special/artifact; keep the recurring stream
    return sum(amts)


def prices_from_series(series: list[tuple[str, float]], fy_years: list[int]) -> dict:
    """Reduce a daily close series → {last, fy_close{year: last close on/before Dec-31}}."""
    if not series:
        return {"last": None, "fy_close": {}}
    fy_close: dict[int, float] = {}
    for yr in fy_years:
        prior = [c for d, c in series if d <= f"{yr}-12-31"]
        if prior:
            fy_close[yr] = prior[-1]
    return {"last": series[-1][1], "fy_close": fy_close}


# --------------------------------------------------------------------------------------------------
# Independent statistics oracle — Yahoo's OWN computed ratios (P/E, P/BV, ROE, margins, shares) for
# the reconciliation workflow (src/coverage/reconcile.py). quoteSummary is crumb-gated (unlike the
# chart endpoint), so a one-time cookie+crumb bootstrap is needed; everything else reuses the client.
# --------------------------------------------------------------------------------------------------
_CRUMB = None
_QS_URL = ("https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
           "?modules=defaultKeyStatistics,financialData,summaryDetail,price&crumb={crumb}")


def _ensure_crumb(verify_ssl: bool) -> str | None:
    """One-time cookie+crumb handshake for quoteSummary. Cached for the process; None on failure."""
    global _CRUMB, _SESSION
    if _CRUMB is not None:
        return _CRUMB or None
    import time
    try:
        if _SESSION is None:
            _session_get("https://fc.yahoo.com/", verify_ssl)  # inits _SESSION + seeds cookies
        else:
            _SESSION.get("https://fc.yahoo.com/", timeout=10, verify=verify_ssl)
        time.sleep(0.4)
        r = _SESSION.get("https://query2.finance.yahoo.com/v1/test/getcrumb",
                         timeout=10, verify=verify_ssl)
        crumb = (r.text or "").strip() if getattr(r, "status_code", 0) == 200 else ""
        _CRUMB = crumb if (crumb and "<" not in crumb) else ""   # reject HTML error pages
    except Exception:
        _CRUMB = ""
    return _CRUMB or None


def _raw(node: dict, key: str):
    """Pull a numeric ``.raw`` from a Yahoo field (which is {raw,fmt} or occasionally a scalar)."""
    v = (node or {}).get(key)
    if isinstance(v, dict):
        v = v.get("raw")
    return v if isinstance(v, (int, float)) else None


def fetch_key_stats(clave: str, *, ticker: str | None = None, verify_ssl: bool = True) -> dict | None:
    """Yahoo's own computed statistics for a BMV name, normalized to the engine's conventions
    (absolute MXN → millions; fractions → whole percents). Returns None on any failure so callers
    degrade gracefully. Keys: shares_out, market_cap, ev (millions); trailing_pe, price_to_book,
    ev_ebitda (ratios); roe, profit_margin, dividend_yield (percent); trailing_eps; total_revenue,
    total_debt (millions); currency, symbol."""
    import time
    # A missing crumb blocks the LIVE quoteSummary fetch, but a cached value is still usable — so we
    # don't bail here; we fall through and serve the last-good cache per candidate symbol below.
    crumb = _ensure_crumb(verify_ssl)
    _CACHE.mkdir(parents=True, exist_ok=True)
    for sym in candidate_symbols(clave, ticker):
        cache = _CACHE / f"keystats_{sym.replace('.', '_')}.json"
        result = None
        if _cache_fresh(cache):
            try:
                result = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                result = None
        if result is None and crumb:
            time.sleep(0.4)
            fetched = None
            try:
                resp = _session_get(_QS_URL.format(sym=sym, crumb=crumb), verify_ssl)
                if getattr(resp, "status_code", 200) == 200:
                    data = json.loads(resp.text if hasattr(resp, "text") else str(resp))
                    res = ((data.get("quoteSummary") or {}).get("result")) or []
                    if res:
                        fetched = res[0]
            except Exception:
                fetched = None
            if fetched is not None:
                result = fetched
                cache.write_text(json.dumps(result), encoding="utf-8")
        if result is None:
            # no crumb / throttle / failure / empty → reinstate the last-good cached stats
            result = _read_cache_stale(cache)
        if result is None:
            continue
        ks = result.get("defaultKeyStatistics") or {}
        fd = result.get("financialData") or {}
        sd = result.get("summaryDetail") or {}
        pr = result.get("price") or {}
        # reject non-equity look-alikes (same guard as fetch_prices)
        if (pr.get("quoteType") or "EQUITY") not in ("EQUITY", None):
            continue
        shares = _raw(ks, "sharesOutstanding")
        mcap = _raw(sd, "marketCap") or _raw(pr, "marketCap")
        if shares is None and mcap is None:
            continue
        def mn(x):
            return None if x is None else x / 1e6
        def pct(x):
            return None if x is None else x * 100.0
        return {
            "symbol": sym,
            "currency": pr.get("currency"),
            "shares_out": mn(shares),
            "market_cap": mn(mcap),
            "ev": mn(_raw(ks, "enterpriseValue")),
            "trailing_pe": _raw(sd, "trailingPE") or _raw(pr, "trailingPE"),
            "price_to_book": _raw(ks, "priceToBook"),
            "ev_ebitda": _raw(ks, "enterpriseToEbitda"),
            "trailing_eps": _raw(ks, "trailingEps"),
            "roe": pct(_raw(fd, "returnOnEquity")),
            "profit_margin": pct(_raw(fd, "profitMargins") or _raw(ks, "profitMargins")),
            "dividend_yield": pct(_raw(sd, "dividendYield")),
            "total_revenue": mn(_raw(fd, "totalRevenue")),
            "total_debt": mn(_raw(fd, "totalDebt")),
        }
    return None


def fetch_prices(clave: str, fy_years: list[int] | None = None, *, ticker: str | None = None,
                 symbol: str | None = None, verify_ssl: bool = True, provider: str = "yahoo") -> dict:
    """Fetch {last, fy_close{year:px}, symbol} for a BMV name. Resolves the series suffix by trying
    candidates until Yahoo returns data. Returns last=None / empty fy_close if unresolvable."""
    fy_years = fy_years or []
    if provider != "yahoo":
        raise ValueError(f"unknown price provider {provider!r}")
    cands = [symbol] if symbol else candidate_symbols(clave, ticker)
    fallback = None  # first non-EQUITY candidate with a real price — used ONLY if no equity resolves
    for sym in cands:
        result = _fetch_yahoo(sym, verify_ssl=verify_ssl)
        if not result:
            continue
        meta = result.get("meta") or {}
        if not (result.get("timestamp") or meta.get("regularMarketPrice")):
            continue
        out = prices_from_series(_series_from_result(result), fy_years)
        out["symbol"] = sym
        out["dvd_ttm"] = trailing_dividends(result)  # trailing-12m cash dividend (same fetch)
        if out["last"] is None:
            continue
        # Prefer a real EQUITY (or an untyped quote), returned immediately. A non-equity look-alike is
        # ambiguous: Yahoo sometimes MISLABELS a valid BMV equity as MUTUALFUND (GOMO/ALFA/URBI/… have a
        # genuine multi-year daily series), but a real fund can also share a root (Regional's `RB.MX`
        # quotes MXN 1,056, not the `RA.MX` stock). So a non-equity is kept only as a FALLBACK — used
        # just when NO equity candidate resolves, which recovers the mislabeled names while letting a
        # true equity (Regional's RA.MX) still win. The downstream native._price_is_scale_corrupt guard
        # (P/B ≪ book) rejects a genuinely wrong fund price that slips through.
        itype = meta.get("instrumentType") or meta.get("quoteType")
        if not itype or itype == "EQUITY":
            return out
        if fallback is None:
            fallback = out
    return fallback or {"last": None, "fy_close": {}, "symbol": None, "dvd_ttm": None}
