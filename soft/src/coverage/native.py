"""Native pack assembly — build a BloombergPack-shaped object entirely from free/public sources
(XBRL fundamentals + derivations, Yahoo prices, Banxico/INEGI macro), so Bloomberg is only needed
for the residual. Peers are BMV names too → their fundamentals come from the same XBRL pipeline.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from src.bloomberg.schema import BloombergPack, history_years
from src.coverage.fundamentals import load_fundamentals, _selected_canonical_fact_files
from src.download import market_data
from src.download import macro as macro_mod
from src.shared.paths import REPORTS_DIR

ROOT = Path(__file__).resolve().parents[2]

# fund LTM key → pack field name (peers and subject use the same mapping).
_FUND_TO_PACK = {
    "revenue": "sales_ltm",
    "ebitda": "ebitda_ltm",
    "net_income": "net_income_ltm",
    "total_equity": ["total_equity", "book_value"],
    "tangible_book": "tangible_book",
    "fcf": "fcf_ltm",
    "total_assets": "total_assets",
    "shares_out": "shares_out",
    "minority_interest": "minority_interest",
}


def _company_cfg(slug: str) -> dict:
    p = ROOT / "configs" / f"{slug}.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


def _clave_and_ticker(slug: str) -> tuple[str | None, str | None]:
    cfg = _company_cfg(slug)
    clave = (cfg.get("ir_website") or {}).get("xbrl_ticker")
    ticker = (cfg.get("company") or {}).get("ticker")
    return clave, ticker


def _fund_pack_fields(fund) -> dict:
    """Map a company's native fundamentals → pack field names."""
    out: dict = {}
    for fkey, pfield in _FUND_TO_PACK.items():
        v = fund.get(fkey)
        if v is None:
            continue
        for name in ([pfield] if isinstance(pfield, str) else pfield):
            out[name] = v
    return out


def _fund_list(cfg_engine: dict, template: str) -> list[str]:
    key = {"financials": "financials_fundamentals", "reit": "reit_fundamentals"}.get(
        template, "industrial_fundamentals")
    return cfg_engine.get(key, []) or cfg_engine.get("industrial_fundamentals", [])


def _native_dividend_yield(reports_dir, px_last, shares_out):
    """Dividend yield (%) from the XBRL ``CashDividendsDeclaredPerShare`` tag ÷ live price — real,
    not Bloomberg. The tag is PER-SHARE for some issuers (GFNorte 12.64, Regional 3.80) and a raw
    TOTAL peso amount for others (Banco del Bajío 6.6bn); we compute both interpretations and keep
    whichever lands in a plausible 0-15% band. Returns a percentage (e.g. 6.8), or None if neither
    is plausible / the tag is absent (never a guess)."""
    import json
    if px_last is None or px_last <= 0:
        return None
    files = _selected_canonical_fact_files(Path(reports_dir))
    if not files:
        return None
    try:
        facts = json.loads(files[-1].read_text(encoding="utf-8")).get("facts", {})
    except Exception:
        return None
    entries = facts.get("ifrs_mx-cor_20141205_CashDividendsDeclaredPerShare") or []
    dated = [(e.get("instant") or e.get("period_end"), e.get("value")) for e in entries]
    dated = [(d, v) for d, v in dated if d and isinstance(v, (int, float)) and v > 0]
    if not dated:
        return None
    dps = max(dated, key=lambda t: t[0])[1]           # latest reported value
    cands = [dps / px_last]                             # per-share ÷ price
    if shares_out:                                     # total ÷ market cap (shares carried in mn)
        cands.append(dps / (px_last * shares_out * 1e6))
    for y in cands:
        if 0 < y <= 0.15:
            return y * 100.0
    return None


# Debt-side concepts for a lease-inclusive net-debt (Mexican issuers rarely tag a clean "Borrowings"
# total; leases are unambiguous debt post-IFRS-16, and the "other financial liabilities" buckets hold
# their bonds/bank debt). Summed, minus cash, → net debt for the EV bridge.
_DEBT_CONCEPTS = [
    "ifrs-full_CurrentLeaseLiabilities", "ifrs-full_NoncurrentLeaseLiabilities",
    "ifrs-full_OtherCurrentFinancialLiabilities", "ifrs-full_OtherNoncurrentFinancialLiabilities",
    "ifrs-full_Borrowings", "ifrs-full_CurrentBorrowings", "ifrs-full_NoncurrentBorrowings",
    "ifrs-full_ShorttermBorrowings", "ifrs-full_LongtermBorrowings",
]
_CASH_CONCEPT = "ifrs-full_CashAndCashEquivalents"


def _latest_fact(facts, concept):
    entries = facts.get(concept) or []
    dated = [(e.get("instant") or e.get("period_end"), e.get("value")) for e in entries]
    dated = [(d, v) for d, v in dated if d and isinstance(v, (int, float))]
    return max(dated, key=lambda t: t[0])[1] if dated else None


_CFO_CONCEPT = "ifrs-full_CashFlowsFromUsedInOperatingActivities"
_CAPEX_CONCEPT = "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"


def _latest_fy_flow(files, concept):
    """Latest FULL-FISCAL-YEAR value (raw units) for a duration concept across cached facts files.

    BMV files cash-flow YTD-only (no discrete quarter), so the pipeline's quarter-summing LTM leaves
    cfo/capex blank. Here we read the unambiguous full-year figure directly: a duration fact whose
    span is ~330-380 days (a fiscal year), newest period-end wins. Consolidated only (no dimensions)."""
    import datetime as _dt
    import json
    best = None  # (period_end_str, value)
    for fp in files:
        try:
            facts = json.loads(Path(fp).read_text(encoding="utf-8")).get("facts", {})
        except Exception:
            continue
        for e in (facts.get(concept) or []):
            if e.get("dimensions"):
                continue
            ps, pe, v = e.get("period_start"), e.get("period_end"), e.get("value")
            if not ps or not pe or not isinstance(v, (int, float)):
                continue
            try:
                span = (_dt.date.fromisoformat(pe[:10]) - _dt.date.fromisoformat(ps[:10])).days
            except ValueError:
                continue
            if 330 <= span <= 380 and (best is None or pe > best[0]):
                best = (pe, v)
    return best[1] if best else None


def _native_fcf(reports_dir, rev_millions, currency="MXN"):
    """Latest full-fiscal-year free cash flow (millions) = FY cfo − |FY capex| from raw XBRL. Fills the
    FCF the quarter-summing cascade misses (BMV cash-flow is YTD-only). Returns None if either leg is
    untagged or the magnitude is implausible vs revenue (blank-not-wrong). USD filers restated to MXN."""
    import json
    files = _selected_canonical_fact_files(Path(reports_dir))
    if not files:
        return None
    cfo = _latest_fy_flow(files, _CFO_CONCEPT)
    capex = _latest_fy_flow(files, _CAPEX_CONCEPT)
    if cfo is None or capex is None:
        return None
    fcf = (cfo - abs(capex)) / 1e6
    try:
        from src.extract.xbrl_facts import _USDMXN, detect_reporting_currency
        facts = json.loads(files[-1].read_text(encoding="utf-8")).get("facts", {})
        cur = detect_reporting_currency(facts) or str(currency).upper()
        if str(cur).upper() == "USD":
            fcf *= _USDMXN
    except Exception:
        pass
    if rev_millions and abs(fcf) > 1.5 * abs(rev_millions):
        return None                      # scale/period artifact → honest blank, never a wrong FCF
    return fcf


def _native_net_debt(reports_dir, rev_millions, currency="MXN"):
    """Lease-inclusive net debt (in millions) = Σ(lease + financial-liability debt) − cash, from the
    latest XBRL. Returns None if no debt concept is tagged (can't tell net-cash from mis-tagged) or
    the magnitude is implausible vs revenue. A defensible EV input, not a fabricated one.

    The raw XBRL facts are as-filed, so a USD reporter's debt is in USD; restate to MXN (×USDMXN)
    so the EV bridge stays currency-consistent with the MXN market cap, and the revenue-scaled
    guard (``rev_millions`` is MXN) compares like with like."""
    import json
    files = _selected_canonical_fact_files(Path(reports_dir))
    if not files:
        return None
    try:
        facts = json.loads(files[-1].read_text(encoding="utf-8")).get("facts", {})
    except Exception:
        return None
    debt = [(_latest_fact(facts, c) or 0.0) for c in _DEBT_CONCEPTS if facts.get(c)]
    if not debt:
        return None                      # no debt tagged → can't compute honestly
    cash = _latest_fact(facts, _CASH_CONCEPT) or 0.0
    net = (sum(debt) - cash) / 1e6       # → millions, matching the pack
    from src.extract.xbrl_facts import _USDMXN, detect_reporting_currency
    # Detect from the facts we already loaded (authoritative); config ``currency`` is the fallback.
    cur = detect_reporting_currency(facts) or str(currency).upper()
    if str(cur).upper() == "USD":
        net *= _USDMXN                   # USD-filed debt → MXN, consistent with the MXN market cap
    if rev_millions and abs(net) > 25 * abs(rev_millions):
        return None                      # scale artifact
    return net


# Direct XBRL shares-outstanding count — FAR more reliable than net_income/eps (which breaks when
# the eps basis is per-unit / per-CPO / diluted, giving 2-30× wrong counts). Validated against Yahoo:
# GICSA 1500=1500, Vitro 470=470, Alterna 520=520, Accel 189=189 (ni/eps was 461/1013/1814/285).
_SHARES_CONCEPT = "ifrs_mx-cor_20141205_NumeroDeAccionesEnCirculacion"

# Fallback share source for annual CNBV annual-report/prospectus (``ar_pros``) filings, which do NOT
# carry ``NumeroDeAccionesEnCirculacion`` (present only in the quarterly financial-statement XBRL).
# The ordinary count is instead tagged per share series (``ar_pros_SeriesTypedAxis``: Serie O/A/B/L…),
# so the total is the sum across series at the latest period — GFInbursa 6,667mn, Pena Verde 476.7mn,
# GNP 224.1mn, GBM 1,641mn.
_SERIE_SHARES_CONCEPT = "ar_pros_SerieNumberOfStocks"


def _serie_shares_total(facts):
    """Total ordinary shares (raw units) from ``ar_pros_SerieNumberOfStocks``, summed across every
    share series at the latest reported period. Returns None when the concept is untagged."""
    dated = [(e.get("instant") or e.get("period_end"), e.get("value"))
             for e in (facts.get(_SERIE_SHARES_CONCEPT) or [])]
    dated = [(d, v) for d, v in dated if d and isinstance(v, (int, float))]
    if not dated:
        return None
    latest = max(d for d, _ in dated)
    total = sum(v for d, v in dated if d == latest)
    return total or None


def _native_shares_out(reports_dir, shares_per_unit: float | None = None):
    """Shares outstanding in MILLIONS from the direct XBRL count (latest filing). None if untagged.

    The XBRL count is the ORDINARY-share count. For CPO/unit structures (Cemex, KOF, Televisa) the
    market price is quoted per CPO/unit, so the count must be divided by the CPO ratio to line up with
    the price (else market cap and all per-share multiples inflate by that ratio). ``shares_per_unit``
    is that ratio (from ``company.shares_per_unit`` in the config); when set (>1) the ordinary count is
    divided by it so ``shares_out`` is in traded-unit terms, consistent with the quoted price. Only
    supply it for names whose divided count is corroborated by an independent reference (golden range
    or Yahoo); leave unset otherwise and let the plausibility gate blank the multiples honestly."""
    import json
    files = _selected_canonical_fact_files(Path(reports_dir))
    if not files:
        return None
    try:
        facts = json.loads(files[-1].read_text(encoding="utf-8")).get("facts", {})
    except Exception:
        return None
    v = _latest_fact(facts, _SHARES_CONCEPT)
    if not (v and v > 0):
        v = _serie_shares_total(facts)     # annual ar_pros filers → per-series ordinary count
    if not (v and v > 0):
        return None
    out = v / 1e6
    if shares_per_unit and shares_per_unit > 1:
        out /= shares_per_unit
    return out


# Upper bound (millions) on a plausible listed-company share count — AMX, the largest BMV issuer,
# has ~64,000mn shares; 100,000mn (100bn) rejects the thousands-artifact blow-ups while clearing every
# real name. Kept in sync with fundamentals._MAX_PLAUSIBLE_SHARES_MN.
_MAX_PLAUSIBLE_SHARES_MN = 100_000.0


def _reconcile_share_scale(direct, ni_eps, price, revenue, lo: float = 0.05, hi: float = 40.0):
    """Correct a power-of-1000 scale artifact in the direct XBRL share count.

    Some issuers tag ``NumeroDeAccionesEnCirculacion`` in thousands (or units) rather than the plain
    share unit the ``/1e6`` in :func:`_native_shares_out` assumes, so the count comes out ×1000 —
    e.g. Simec 497,709mn (raw 497,709,214,000) vs a true ~497mn; Club America 340,621mn vs ~340.6mn.
    A rescale must ALWAYS restore a sane implied P/S (``price×shares/revenue`` in ``[lo,hi]``); on top
    of that it needs one corroborating signal so we never "fix" a P/S problem by corrupting a share
    count that is actually fine (a price-broken name like Vasconia, whose 96.7mn count is plausible):
      * an independent ``net_income/eps`` count agrees with the rescaled magnitude (~10^{±3,±6}), OR
      * the ORIGINAL count is an impossible magnitude (>100bn shares) and the rescale lands a plausible
        one — i.e. the count itself, not the price, is the thing that is broken.
    Returns the corrected count, or the original when no rescale satisfies both requirements. A healthy
    count (P/S already in band) is returned untouched without inspection.
    """
    if not (direct and direct > 0):
        return direct

    def ps(s):
        return (price * s / revenue) if (price and revenue and revenue > 0) else None

    base_ps = ps(direct)
    if base_ps is not None and lo <= base_ps <= hi:
        return direct                                  # already sane — never rescale
    for f in (1e3, 1e6, 1e-3, 1e-6):
        cand = direct / f
        cand_ps = ps(cand)
        if not (cand_ps is not None and lo <= cand_ps <= hi):
            continue                                   # rescale must restore a sane P/S
        corrob = bool(ni_eps and ni_eps > 0 and 0.5 < (cand / ni_eps) < 2.0)
        impossible_magnitude = (direct > _MAX_PLAUSIBLE_SHARES_MN
                                and cand <= _MAX_PLAUSIBLE_SHARES_MN)
        if corrob or impossible_magnitude:
            return cand
    return direct


# A live price below this fraction of filing-derived book value is a scale artifact, not a discount:
# even the cheapest real BMV names trade ~0.4× book, ~8× above this floor.
_MIN_SANE_PB = 0.05


def _price_is_scale_corrupt(px, shares, equity) -> bool:
    """True when a live price is orders of magnitude below what the filing's book value allows — a
    Yahoo adjusted-close artifact that penny-scales the whole series (Vasconia: price 0.32 against a
    12.25 XBRL book value → P/B 0.026). Anchored on the XBRL equity (corruption-free, unlike the
    price), with a floor far below any real market discount so a genuinely cheap stock is never
    dropped. Only fires with a positive book value and a plausible share count."""
    if not (px and px > 0 and shares and shares > 0 and equity and equity > 0):
        return False
    return (px * shares) / equity < _MIN_SANE_PB


def _yahoo_yield_fill(ks: dict | None, template: str) -> tuple[str, float] | None:
    """Resolve the Yahoo key-stats dividend yield into a ``(subject_key, value)`` fill, or ``None``.

    Yahoo's ``dividend_yield`` is already normalized to whole percent by ``fetch_key_stats``. We accept
    it only inside the same ``0 < y ≤ 15%`` plausibility band the native XBRL path uses, so a mis-scaled
    or garbage figure is dropped rather than shipped. REITs surface it as ``distribution_yield``; every
    other template as ``dvd_yield``. Pure (no I/O) so the guard is unit-testable."""
    if not ks:
        return None
    dy = ks.get("dividend_yield")
    if not isinstance(dy, (int, float)) or isinstance(dy, bool) or not (0 < dy <= 15.0):
        return None
    key = "distribution_yield" if template == "reit" else "dvd_yield"
    return key, float(dy)


def _peer_template(slug: str, fallback: str) -> str:
    """A peer's valuation template — from its own ``inputs/<slug>.md`` if present, else the subject's
    template (a bank's peers are banks), else ``industrial``. This drives which fundamentals list is
    extracted, so a bank peer gets bank fundamentals instead of the industrial default."""
    spec_path = ROOT / "inputs" / f"{slug}.md"
    if spec_path.exists():
        try:
            from src.coverage.spec import parse_spec
            return parse_spec(str(spec_path)).template
        except Exception:
            pass
    return fallback if fallback in ("industrial", "financials", "reit") else "industrial"


def build_native_pack(spec, fund, *, verify_ssl: bool = True, with_prices: bool = True,
                      with_macro: bool = True, offline: bool = False,
                      with_peers: bool = True) -> BloombergPack:
    """Assemble the native pack: subject market fields, peer fundamentals (XBRL) + prices, FY-close
    price history, and macro. Fundamentals for the SUBJECT are read from ``fund`` by the valuation
    layer; here we add the market fields (price, shares) the pack must carry.

    ``with_peers=False`` skips the entire peer loop (each peer parses its full XBRL fact set + a
    price fetch) — the dominant cost of a build. Use for fast subject-only iteration when the peer
    cross-section isn't needed; the subject's own completeness (multiples, returns, growth) is
    unaffected."""
    eng = yaml.safe_load((ROOT / "configs" / "soft.yaml").read_text(encoding="utf-8")) or {}
    fy_years = history_years(spec)
    pack = BloombergPack(slug=spec.slug)

    clave, ticker = _clave_and_ticker(spec.slug)

    # --- subject market fields + history (prices) -----------------------------
    pack.subject.update(_fund_pack_fields(fund))  # shares_out (ni/eps) / tangible_book / etc.
    _scfg = (_company_cfg(spec.slug).get("company") or {})
    _spu = _scfg.get("shares_per_unit")
    _ni_eps_sh = pack.subject.get("shares_out")   # independent ni/eps corroborator (may be None)
    _sh = _native_shares_out(REPORTS_DIR / spec.slug, _spu)  # direct XBRL count
    _chart_dvd_ttm = None  # trailing-12m cash dividend from Yahoo's chart events (reliable endpoint)
    if with_prices and (clave or ticker):
        # A PDF-sourced name (e.g. Grupo Bafar) has no xbrl_ticker → clave is None, but its trading
        # ticker still resolves a Yahoo symbol. candidate_symbols() prioritises the ticker branch and
        # tolerates an empty clave, so gate on EITHER and pass clave or "" through.
        px = market_data.fetch_prices(clave or "", fy_years, ticker=ticker, verify_ssl=verify_ssl)
        if px.get("last") is not None:
            pack.subject["px_last"] = px["last"]
        _chart_dvd_ttm = px.get("dvd_ttm")
        pack.history.update({int(y): v for y, v in px.get("fy_close", {}).items()})
        # Drop a scale-corrupt live price (and its equally-corrupt history) so the cell falls to the
        # residual as an honest "price pending" rather than shipping a penny market cap.
        if _price_is_scale_corrupt(pack.subject.get("px_last"),
                                   _scfg.get("shares_out_mn") or _sh or _ni_eps_sh,
                                   fund.get("equity")):
            pack.subject.pop("px_last", None)
            pack.history.clear()
    # Resolve the share count: an explicit config override wins; else the direct XBRL count with a
    # power-of-1000 scale check (Simec 497,709mn → 497mn); else the ni/eps derivation already in place.
    _ovr = _scfg.get("shares_out_mn")
    if _ovr:
        pack.subject["shares_out"] = float(_ovr)
    elif _sh is not None:
        pack.subject["shares_out"] = _reconcile_share_scale(
            _sh, _ni_eps_sh, pack.subject.get("px_last"), fund.get("revenue"))

    # --- peers: fundamentals from XBRL + price --------------------------------
    for p in (spec.peers if with_peers else []):
        pcfg = _company_cfg(p.slug)
        pclave = (pcfg.get("ir_website") or {}).get("xbrl_ticker")
        pticker = (pcfg.get("company") or {}).get("ticker")
        ptemplate = _peer_template(p.slug, getattr(spec, "template", "industrial"))
        peer_row: dict = {}
        if pclave:
            try:
                pfund = load_fundamentals(p.slug, REPORTS_DIR / p.slug,
                                          ROOT / "configs" / f"{p.slug}.yaml",
                                          _fund_list(eng, ptemplate), offline=offline,
                                          facts_only=True,   # peers need XBRL facts only, not MD&A
                                          prefer_xbrl=True)  # never the slow cascade for a peer
                peer_row.update(_fund_pack_fields(pfund))
                _pcompany = (pcfg.get("company") or {})
                _pspu = _pcompany.get("shares_per_unit")
                _pni_eps = peer_row.get("shares_out")   # ni/eps corroborator
                _psh = _native_shares_out(REPORTS_DIR / p.slug, _pspu)
            except Exception:
                _pcompany, _pni_eps, _psh = {}, None, None
        if with_prices and (pclave or pticker):
            ppx = market_data.fetch_prices(pclave or "", [], ticker=pticker, verify_ssl=verify_ssl)
            if ppx.get("last") is not None:
                peer_row["px_last"] = ppx["last"]
        if pclave:
            _povr = _pcompany.get("shares_out_mn")
            if _povr:
                peer_row["shares_out"] = float(_povr)
            elif _psh is not None:
                peer_row["shares_out"] = _reconcile_share_scale(
                    _psh, _pni_eps, peer_row.get("px_last"), peer_row.get("sales_ltm"))
        if peer_row:
            pack.peers[p.slug] = peer_row

    # --- bank ratios: NIM/efficiency/cost-of-risk strictly from the annual-report narrative -----
    # (CET1 is NOT taken from prose — the narrative is full of regulatory minimums/thresholds that
    # read as plausible values; CNBV's structured table below is the authoritative CET1 source.)
    if getattr(spec, "template", "") == "financials":
        try:
            from src.extract.bank_ratios import extract_bank_ratios, latest_annual_json
            j = latest_annual_json(REPORTS_DIR / spec.slug)
            if j is not None:
                for k, v in extract_bank_ratios(j).items():
                    pack.subject.setdefault(k, v)  # filing value; block reads pack.subject.get(k)
        except Exception:
            pass
        # --- authoritative bank CAPITAL ratios from CNBV's structured ICAP table --------------
        # CET1 (CCF) and total capital (ICAP) per bank — unambiguous, self-updating, free. These
        # OVERRIDE any prose grab (CNBV is ground truth). Only mapped banca-múltiple banks fill.
        try:
            from src.download import cnbv
            cap = cnbv.bank_capital_for_slug(spec.slug)
            if cap:
                pack.subject["cet1"] = cap["cet1"]
                pack.subject["icap"] = cap["icap"]
        except Exception:
            pass

    # --- dividend yield: XBRL dividends-per-share ÷ live price (most precise), then the Yahoo CHART
    # trailing-dividend (reliable — same endpoint as prices, no crumb-gated quoteSummary call) -------
    try:
        dy = _native_dividend_yield(REPORTS_DIR / spec.slug,
                                    pack.subject.get("px_last"), pack.subject.get("shares_out"))
        if dy is not None:
            pack.subject.setdefault("dvd_yield", dy)
    except Exception:
        pass
    _px = pack.subject.get("px_last")
    _yld_key = "distribution_yield" if getattr(spec, "template", "") == "reit" else "dvd_yield"
    if _chart_dvd_ttm and _px and _px > 0:
        y = _chart_dvd_ttm / _px * 100.0
        if 0 < y <= 30:  # same plausibility band as the other yield paths — never a wild value
            if pack.subject.get(_yld_key) is None:  # XBRL per-share value always wins
                pack.subject[_yld_key] = y
                pack.yahoo_filled.add(_yld_key)
    # Non-payer: the chart resolved a price but shows NO trailing dividends → the yield is honestly
    # 0.0% (a real value, not "unknown"). Gated on a RESOLVED PRICE (`_px` set — the chart result, whose
    # dividend-event history is authoritative, was obtained; a cached last-good price counts) rather than
    # on `not offline` — the fundamentals-offline flag isn't about price availability, and gating on it
    # meant the 0% never filled in the (always-offline-fundamentals) master build. The daily refresh
    # re-fetches and self-heals any name whose cached chart predates the dividend-event history.
    if (with_prices and (clave or ticker) and _px and _px > 0
            and _chart_dvd_ttm in (None, 0) and pack.subject.get(_yld_key) is None):
        pack.subject[_yld_key] = 0.0
        pack.yahoo_filled.add(_yld_key)

    # --- lease-inclusive net debt from XBRL → EV bridge (non-bank templates) --------------------
    if getattr(spec, "template", "") != "financials":
        try:
            _cur = str(((_company_cfg(spec.slug).get("company") or {}).get("currency"))
                       or "MXN").upper()
            nd = _native_net_debt(REPORTS_DIR / spec.slug, fund.get("revenue"),
                                  currency=_cur)
            if nd is not None:
                pack.subject.setdefault("net_debt", nd)
        except Exception:
            pass
        # --- FY free cash flow from raw XBRL (fills the FCF the quarter-summing cascade misses) ------
        # Goes into the `fcf_ltm` fallback slot, which valuation uses ONLY when the cascade's own FCF
        # (cfo−capex from fundamentals) is absent — so a company that already has FCF is never touched.
        # INDUSTRIAL ONLY: REITs value on FFO/distribution (not FCF) and injecting an FCF breaks their
        # math-audit cross-checks; banks (financials) are already excluded by the outer guard.
        if getattr(spec, "template", "") == "industrial":
            try:
                _curf = str(((_company_cfg(spec.slug).get("company") or {}).get("currency")) or "MXN").upper()
                fcf = _native_fcf(REPORTS_DIR / spec.slug, fund.get("revenue"), currency=_curf)
                if fcf is not None:
                    pack.subject.setdefault("fcf_ltm", fcf)
            except Exception:
                pass

    # --- Yahoo key-stats fallback: dividend/distribution yield the filing path couldn't supply ----
    # The XBRL ``CashDividendsDeclaredPerShare`` tag is present for only ~1 issuer in 8, so for the
    # bulk of the universe the sole real source of dividend yield is Yahoo's own computed figure
    # (``summaryDetail.dividendYield``, already normalized to whole percent by fetch_key_stats). We
    # only fill a BLANK — the native XBRL path above always wins — and only within the same 0-15%
    # plausibility band, tagging the cell as Yahoo-sourced so the workbook stays honest.
    # Key-stats is MARKET data (like prices), so gate it on with_prices — NOT on `offline`, which is
    # the FUNDAMENTALS-offline flag. The master/refresh build runs offline_fundamentals=True with live
    # prices; gating key-stats on `not offline` there starved the Yahoo backfill (ROE / Net margin /
    # shares_out) even though prices were being fetched in the very same pass.
    if with_prices and (clave or ticker):
        try:
            ks = market_data.fetch_key_stats(clave or "", ticker=ticker, verify_ssl=verify_ssl)
        except Exception:
            ks = None
        if ks:
            pack.yahoo_stats = ks  # independent oracle for the validator's cheap-multiple corroboration
            # Last-resort share count: when the XBRL count + config override + ni/eps derivation all
            # yielded nothing, Yahoo's sharesOutstanding (already millions-normalized by fetch_key_stats,
            # so no _reconcile_share_scale needed) lets market cap / EV / P-E / EV-EBITDA / P/BV compute
            # for a no-shares modeled name — e.g. PDF-sourced Grupo Bafar (no XBRL, no override). Fill
            # a genuine blank only; a real filing-derived count always wins.
            if pack.subject.get("shares_out") is None and ks.get("shares_out"):
                pack.subject["shares_out"] = ks["shares_out"]
                pack.yahoo_filled.add("shares_out")
        fill = _yahoo_yield_fill(ks, getattr(spec, "template", ""))
        if fill and pack.subject.get(fill[0]) is None:  # native/filing value always wins
            pack.subject[fill[0]] = fill[1]
            pack.yahoo_filled.add(fill[0])

    # --- FIBRA KPIs from the quarterly MD&A (native, not Bloomberg) ---------------------------
    # Conservative parser — emits only high-confidence values (occupancy cleanly; FFO/NOI only with
    # an unambiguous annual figure). Ambiguous → omitted (blank worklist), never a guessed value.
    if getattr(spec, "template", "") == "reit":
        try:
            from src.extract.fibra_kpis import extract_fibra_kpis, latest_mdna
            m = latest_mdna(REPORTS_DIR / spec.slug)
            if m is not None:
                for k, v in extract_fibra_kpis(m).items():
                    pack.subject.setdefault(k, v)
        except Exception:
            pass

    # --- macro ----------------------------------------------------------------
    if with_macro and spec.macro:
        keys = [m.key for m in spec.macro]
        vals = macro_mod.fetch_macro(keys, eng.get("macro_sources", {}), verify_ssl=verify_ssl)
        # Fall back to the latest verified published figures when the live Banxico/INEGI feed is
        # unavailable (no API token in this environment). Real, dated data — NOT placeholders.
        for k in keys:
            if vals.get(k) is None and k in _MACRO_FALLBACK:
                vals[k] = _MACRO_FALLBACK[k]
        pack.macro.update(vals)

    return pack


# Verified Mexico macro (published, as of Dec-2025) used only when the live feed returns nothing:
#   Banxico target rate 7.00% (18-Dec-2025) · CPI 3.69% YoY (Dec-2025) · GDP +0.8% real (2025,
#   INEGI) · USDMXN FIX ~17.5. Refresh periodically; real figures, not a placeholder set.
_MACRO_FALLBACK = {
    "policy_rate": 7.00,
    "inflation": 3.69,
    "gdp_growth": 0.8,
    "usdmxn": 17.5,
}


# Pack fields where a supplied value is authoritative over the native derivation (which is weak).
_RESIDUAL_OVERRIDES = {"shares_out", "net_debt", "book_value"}


def merge_packs(native: BloombergPack, residual: BloombergPack | None) -> BloombergPack:
    """Merge native (wins) with the residual Bloomberg pack (fills gaps)."""
    if residual is None:
        return native
    # Carry the placeholder verdict onto the merged pack so the gate sees it.
    if getattr(residual, "suspect_placeholder", False):
        native.suspect_placeholder = True
        native.placeholder_reasons = list(residual.placeholder_reasons)
    placeholder = getattr(residual, "suspect_placeholder", False)
    for k, v in residual.subject.items():
        # shares_out/net_debt are only WEAKLY derived natively (shares = net-income ÷ EPS is
        # unreliable — see AC/GBM); a real value supplied in the pack is authoritative and overrides.
        # But a SUSPECTED-PLACEHOLDER pack must never clobber a real native XBRL count with a dummy
        # (Fibra Uptown's 3,900mn scaffold value inflating a true 53mn CBFI count → P/S 367×).
        if k in _RESIDUAL_OVERRIDES and v is not None and not placeholder:
            native.subject[k] = v
        else:
            native.subject.setdefault(k, v)
    for slug, row in residual.peers.items():
        dst = native.peers.setdefault(slug, {})
        for k, v in row.items():
            dst.setdefault(k, v)
    for k, v in residual.segment_multiples.items():
        native.segment_multiples.setdefault(k, v)
    for y, v in residual.history.items():
        native.history.setdefault(y, v)
    for k, v in residual.macro.items():
        native.macro.setdefault(k, v)
    for y, row in getattr(residual, "timeseries", {}).items():
        dst = native.timeseries.setdefault(y, {})
        for k, v in row.items():
            dst.setdefault(k, v)
    return native


def filled_cells(pack: BloombergPack, spec) -> set[str]:
    """The set of template cell ids the native pack already covers → used to prune the Bloomberg
    template so it only asks for the residual. Cell id = '<entity_kind>:<entity>:<field>[@period]'."""
    filled: set[str] = set()
    for f, v in pack.subject.items():
        if v is not None:
            filled.add(f"subject:{spec.slug}:{f}")
    for slug, row in pack.peers.items():
        for f, v in row.items():
            if v is not None:
                filled.add(f"peer:{slug}:{f}")
    for y, v in pack.history.items():
        if v is not None:
            filled.add(f"history:{spec.slug}:px_fy_close@{y}")
    for k, v in pack.macro.items():
        if v is not None:
            filled.add(f"macro:{k}:value")
    return filled
