"""Fundamentals adapter — run the vendored extraction cascade over cached filings and
reshape the result into the LTM / annual views the valuation engine needs (the ``[filing]``
side of the coverage pack).

This is a thin wrapper: all extraction logic lives in the vendored ``src.extract.pipeline``.
We only classify each metric (flow vs. stock) and roll quarters up to LTM and fiscal-year values.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.model.financial_model import METRICS as _BASE_METRICS, apply_config, load_config

# Metric sections that are FLOWS (summed across quarters → LTM / FY). Everything else
# (balance-sheet stocks, KPI counts) is a point-in-time snapshot (latest / year-end).
_FLOW_SECTIONS = {"income", "cashflow"}

# Upper bound (in millions) on a plausible listed-company share count. The largest BMV issuer,
# América Móvil, has ~64,000mn shares; 100,000mn (100bn) leaves comfortable headroom while rejecting
# the ni/eps blow-ups a near-zero eps produces (Club America: eps≈-3e-5 → 340,621mn "shares").
_MAX_PLAUSIBLE_SHARES_MN = 100_000.0

# How many recent periods a facts_only load reads (peers / fast subject). 14 quarters covers the
# trailing-twelve-month LTM plus ~3 complete fiscal years — enough for FY growth AND a real
# historical valuation band, while still far cheaper than parsing every cached period.
_FACTS_ONLY_PERIODS = 40

# Balance-sheet stocks that cannot legitimately be 0 for a going concern. A 0 here is an
# XBRL extraction artifact — the filing tags the current-period instant as 0 while the real
# value lives in the comparative contexts (observed on ACTINVR 2025-FY: equity 2025-12-31 = 0
# but 2024 = 9.73bn). Left unchecked, that 0 poisons book value, P/BV, P/TBV, ROE, ROTE, and
# produces a nonsense −100% book-value growth. We treat it as MISSING everywhere: the current
# snapshot falls back to the latest real value; a historical year with no real value stays blank
# (never fabricated).
_POSITIVE_STOCKS = {"equity", "total_equity", "total_assets"}


_STUB_CORE = ("revenue", "net_income", "total_equity", "equity", "total_assets")


def _is_prospectus_stub(snapshot: dict) -> bool:
    """True when EVERY core monetary metric is 0-or-absent — an annual-report/prospectus (ar_pros)
    filing that stubs its ifrs-full tags to 0 (GNP) rather than a real operating company (which
    always reports at least one nonzero core figure). Used to reclassify such a subject as un-sourced
    (a corpus gap → INCOMPLETE) instead of an engine bug (→ BROKEN)."""
    return all(snapshot.get(k) in (None, 0) for k in _STUB_CORE)


def _is_artifact_zero(key: str, v) -> bool:
    """True when ``v`` is a 0 for a must-be-positive balance-sheet stock (extraction artifact)."""
    return key in _POSITIVE_STOCKS and isinstance(v, (int, float)) and v == 0


def _period_label(filename: str) -> str:
    """Period label from a filing filename, stripping the FULL extension first. Raw filings are stored
    gzip-compressed (``GBM_2025-FY.json.gz``); a naive ``Path.stem`` removes only ``.gz`` and leaves a
    ``…json`` tail that contaminates the label (``2025-FY.json``) → ``period_end_from_label`` fails →
    DURATION facts (net income / revenue / eps) can't be period-matched and silently drop to None while
    instant balance-sheet facts still resolve. That was the annual-bank / peer blank-flows bug.

    ``GBM_2025-FY.json.gz`` → ``2025-FY`` · ``GMEXICO_2026-1T.json`` → ``2026-1T``.
    """
    name = filename
    for suf in (".json.gz", ".json", ".gz"):
        if name.endswith(suf):
            name = name[: -len(suf)]
            break
    return name.split("_", 1)[-1]


def _period_year_q(period: str) -> tuple[int, int] | None:
    """Parse a period label → (year, quarter). Handles 'YYYY-NT' (quarter), and annual filings
    labelled 'YYYY' or 'YYYY-FY' → (year, 4) treated as fiscal-year-end."""
    p = str(period).strip().upper()
    if len(p) == 4 and p.isdigit():
        return int(p), 4
    if p.endswith("-FY") and p[:4].isdigit():
        return int(p[:4]), 4
    try:
        year = int(p[:4])
        q = int(p[5])
        if 1 <= q <= 4:
            return year, q
    except (ValueError, IndexError):
        return None
    return None


@dataclass
class Fundamentals:
    slug: str
    frame: object                         # wide per-period DataFrame (period + metric cols)
    periods: list[str]                    # sorted period labels present
    current_period: str
    ltm: dict[str, float] = field(default_factory=dict)          # LTM (flows) / latest (stocks)
    annual: dict[int, dict[str, float]] = field(default_factory=dict)  # FY -> {metric: value}
    flow_keys: set[str] = field(default_factory=set)
    confidence: dict = field(default_factory=dict)

    def get(self, key: str) -> float | None:
        v = self.ltm.get(key)
        return None if v is None or (isinstance(v, float) and v != v) else v


def _classify(metric_keys: list[str], cfg: dict, sections: set = _FLOW_SECTIONS) -> set[str]:
    """Return the subset of ``metric_keys`` whose section is in ``sections`` (default: flow sections)."""
    defs = apply_config(_BASE_METRICS, cfg)
    section_by_key = {d.key: d.section for d in defs}
    flows: set[str] = set()
    for k in metric_keys:
        base = k
        # segment-prefixed keys (e.g. ebitda_mexico) inherit the base metric's section
        for d in defs:
            if k == d.key or k.startswith(d.key + "_"):
                base = d.key
                break
        if section_by_key.get(base) in sections:
            flows.add(k)
    return flows


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # drop NaN


def _has_local_reports(reports_dir: Path) -> bool:
    d = Path(reports_dir)
    return d.is_dir() and (any(d.glob("*.md")) or any(d.glob("*.pdf")))


def _xbrl_period_sources(ticker: str, reports_dir: Path, max_reports: int,
                         offline: bool = False, facts_only: bool = False) -> dict:
    """Download BMV XBRL for ``ticker`` → {period: PeriodSource}. Tries quarterly first; if empty
    (financial-sector issuers publish annual-only XBRL), retries with annual filings.

    When ``offline`` is set, never touches the network — it reads only the XBRL JSON already
    cached under ``reports_dir/xbrl/`` (the archive-index fetch otherwise blocks for ~120s per
    company in a no-network environment).

    When ``facts_only`` is set, the MD&A narrative is NOT read (``text=""``). Parsing the MD&A
    means loading each 30 MB raw filing JSON; for a peer we only need the Tier-1 XBRL facts
    (equity/NI/EPS), so skipping it turns a multi-GB parse (peers × filings) into a cheap facts
    read — the difference between a build that hangs and one that finishes in seconds."""
    from src.download.bmv_xbrl import load_mdna_text
    from src.extract.pipeline import _load_facts
    from src.extract.tiered_extract import PeriodSource, period_end_from_label

    xdir = Path(reports_dir)
    paths = []
    if offline:
        # Only the raw filing JSONs (TICKER_PERIOD.json). Exclude the *_facts.json / *_mdna
        # extraction artifacts: passing a _facts.json to _load_facts regenerates a deeper
        # _facts_facts.json (a re-entrant naming bug) which pollutes the cache and, treated as its
        # own period, corrupts the reshape. Also cuts offline reads ~15× (raw files vs. all artifacts).
        xd = xdir / "xbrl"
        # Raw filings are stored gzip-compressed (*.json.gz); accept a legacy
        # plaintext *.json too. Exclude the *_facts.json extraction artifacts.
        paths = sorted(
            p for p in xd.iterdir()
            if "_facts" not in p.name and (p.name.endswith(".json.gz") or p.name.endswith(".json"))
        ) if xd.is_dir() else []
        # facts_only callers (peers, fast subject) only need recent periods for LTM + latest annual;
        # each _facts.json is up to ~1 MB and this sandbox reads them slowly, so cap to the most
        # recent few. Full history (all periods) is kept only for the prose-parsing subject path.
        if facts_only and len(paths) > _FACTS_ONLY_PERIODS:
            paths = paths[-_FACTS_ONLY_PERIODS:]
    else:
        from src.download.bmv_xbrl import download_ticker
        # Pull BOTH quarterly and annual filings and UNION them. The old code broke on the first
        # non-empty result, so a thin-corpus name with a few quarters never fetched its annuals —
        # blanking the point-in-time metrics (ROE/margins/leverage) those annual FYs would compute.
        seen: set = set()
        paths = []
        for include_annual in (False, True):
            try:
                got = download_ticker(ticker, xdir, include_annual=include_annual,
                                      max_filings=max_reports, delay_ms=600)
            except Exception as exc:
                print(f"[fundamentals] XBRL fetch {ticker} (annual={include_annual}) failed: {exc}")
                got = []
            for p in got:
                if p not in seen:
                    seen.add(p)
                    paths.append(p)

    docs: dict = {}
    for path in paths:
        period = _period_label(path.name)  # GBM_2025-FY.json.gz → 2025-FY ; GMEXICO_2026-1T → 2026-1T
        if facts_only:
            text = ""  # skip the 30 MB MD&A parse — peers only need the XBRL facts
        else:
            try:
                text = load_mdna_text(path) or ""
            except Exception:
                text = ""
        facts = _load_facts(path)
        # Annual labels (YYYY-FY / YYYY) have no quarter → derive a year-end period_end so XBRL
        # DURATION facts (income statement) can be selected, not just instant balance-sheet facts.
        pe = period_end_from_label(period)
        if pe is None:
            yq = _period_year_q(period)
            if yq:
                pe = f"{yq[0]}-12-31"
        if text or facts:
            docs[period] = PeriodSource(period=period, text=text, facts=facts, period_end=pe)
    return docs


def _extract_via_xbrl(ticker: str, reports_dir: Path, cfg: dict, metric_keys: list[str],
                      max_reports: int = 40, offline: bool = False, facts_only: bool = False):
    """Fetch BMV XBRL for ``ticker`` and extract ``metric_keys`` per period → wide DataFrame.

    Reuses the vendored download + ``extract_metrics_tiered`` (Tier-1 XBRL facts). Standardized
    IFRS fundamentals with no per-company tuning. ``offline`` reads only cached JSON.
    ``facts_only`` skips the MD&A parse (peers don't need prose) — see ``_xbrl_period_sources``.
    """
    import pandas as pd
    from src.extract.tiered_extract import extract_metrics_tiered
    from src.extract.xbrl_facts import pesos_per_unit_for_facts

    docs = _xbrl_period_sources(ticker, reports_dir, max_reports, offline=offline,
                                facts_only=facts_only)
    if not docs:
        return pd.DataFrame()
    metric_defs = apply_config(_BASE_METRICS, cfg)
    rows: list[dict] = []
    for period, src in sorted(docs.items()):
        found = extract_metrics_tiered(src, metric_defs, cfg)
        row = {"period": period}
        for key, mrow in found.items():
            if key in metric_keys and getattr(mrow, "current", None) is not None:
                row[key] = mrow.current
        # Financial-sector ANNUAL filings are labelled by filing year and their income facts are
        # prior-FY durations that exact period_end matching misses → grab the latest full-year
        # duration fact directly (annual filings only, so quarterly extraction is untouched). Scale is
        # sensed PER FILING (some issuers file one year already in millions — Pena Verde FY2025).
        if _is_annual_label(period) and getattr(src, "facts", None):
            ppu = pesos_per_unit_for_facts(cfg, src.facts)
            for key, val in _latest_duration_facts(src.facts, metric_keys, ppu).items():
                row.setdefault(key, val)
        rows.append(row)
    return pd.DataFrame(rows)


# Currency metrics among the annual-fallback set (scaled by pesos-per-unit); eps is per-share.
_CURRENCY_DURATION = {"net_income", "revenue", "interest_income", "operating_income"}


# Minimal duration-concept map for the annual-filing fallback (soft-local).
_DURATION_CONCEPTS = {
    "net_income": ["ifrs-full_ProfitLoss"],
    "revenue": ["ifrs-full_Revenue", "ifrs-full_RevenueFromContractsWithCustomers"],
    "eps": ["ifrs-full_BasicEarningsLossPerShare"],
    "operating_income": ["ifrs-full_ProfitLossFromOperatingActivities"],
    "interest_income": ["ifrs-full_RevenueFromInterest", "ifrs-full_FinanceIncome"],
}


def _is_annual_label(period: str) -> bool:
    p = str(period).strip().upper()
    return p.endswith("-FY") or (len(p) == 4 and p.isdigit())


def _latest_duration_facts(facts: dict, metric_keys: list[str], ppu: float = 1e6) -> dict:
    """For each requested metric with a duration concept, return the value of the entry with the
    latest period_end (the full-year fact in an annual filing). Currency metrics are scaled by
    pesos-per-unit to match the cascade; per-share (eps) is left raw."""
    out: dict = {}
    for key in metric_keys:
        for concept in _DURATION_CONCEPTS.get(key, []):
            entries = facts.get(concept)
            if not entries:
                continue
            durations = [e for e in entries if e.get("instant") is None and e.get("period_end")]
            if not durations:
                continue
            best = max(durations, key=lambda e: e["period_end"])
            v = _num(best.get("value"))
            if v is not None:
                out[key] = v / ppu if key in _CURRENCY_DURATION else v
                break
    return out


def load_fundamentals(
    slug: str,
    reports_dir: str | Path,
    config_path: str | Path,
    metric_keys: list[str],
    offline: bool = False,
    facts_only: bool = False,
    prefer_xbrl: bool = False,
) -> Fundamentals:
    """Extract ``metric_keys`` and roll up to LTM + fiscal-year views.

    If the company config declares ``ir_website.xbrl_ticker`` and no local .md/.pdf filings are
    cached, fundamentals are sourced from BMV XBRL directly; otherwise the full vendored cascade
    runs over the cached filings.

    ``facts_only`` (used for PEER loads) skips the MD&A narrative parse — a large speedup, since a
    peer only feeds the XBRL-derived cross-section and never needs prose metrics.

    ``prefer_xbrl`` (also for PEER loads) forces the XBRL path even when the peer has local .md/.pdf
    filings cached — otherwise loading a peer that happens to carry a rich report corpus (e.g. WALMEX,
    139 PDFs) would run the full 4-tier cascade and stall the whole build for minutes. Peers only need
    XBRL fundamentals, so this is safe and a massive speedup.
    """
    import pandas as pd
    from src.extract.pipeline import run

    cfg = load_config(config_path)
    # Coverage path: XBRL facts of USD reporters CONVERT to MXN (facts-detected
    # currency × USDMXN, per-filing millions sensing) so peso multiples line up
    # with the MXN price. The vendored engine defaults to the deliverable
    # behavior (native currency + expected-currency filter), so soft opts in
    # here at call time — config-independent. See xbrl_facts.pesos_per_unit_for_facts.
    cfg = {**cfg, "xbrl": {**(cfg.get("xbrl") or {}), "currency_mode": "convert_to_mxn"}}
    reports_dir = Path(reports_dir)
    xbrl_ticker = (cfg.get("ir_website") or {}).get("xbrl_ticker")

    if xbrl_ticker and (prefer_xbrl or not _has_local_reports(reports_dir)):
        df = _extract_via_xbrl(xbrl_ticker, reports_dir, cfg, metric_keys, offline=offline,
                               facts_only=facts_only)
    else:
        df = run(str(reports_dir), config=str(config_path), metrics=metric_keys,
                 output_dir=reports_dir, do_validate=True, verbose=False)
    if df.empty:
        raise ValueError(f"{slug}: no documents extracted from {reports_dir}")

    present = [k for k in metric_keys if k in df.columns]
    flows = _classify(present, cfg)

    df = df.sort_values("period").reset_index(drop=True)
    periods = list(df["period"])
    current = periods[-1]
    # Annual-only data (financial-sector filings) has no quarters → flows are already full-year
    # values; take the latest rather than summing four periods.
    annual_only = not any("T" in str(p) for p in periods)

    # --- LTM / latest snapshot ------------------------------------------------
    ltm: dict[str, float] = {}
    # Trailing-four-QUARTER window for flow LTMs: use only quarterly (`-NT`) rows and DROP any annual
    # (`-FY`) filing that got mixed into the series. Summing a full-year `-FY` value alongside quarters
    # both double-counts and imports the annual filing's own scale (Nutrisa: a stray 2025-FY row, itself
    # ×1000 mis-scaled, inflated LTM revenue to 2.4tn and tripped the P/S sentinel). Quarters carry the
    # trailing period; the annual rollup below still uses the `-FY` rows for the FY views.
    q_df = df[df["period"].astype(str).str.contains("T", regex=False)]
    last4 = (q_df if not q_df.empty else df).tail(4)
    for k in present:
        col = df[k]
        if k in flows and not annual_only:
            vals = [_num(v) for v in last4[k]]
            vals = [v for v in vals if v is not None]
            # Require a full trailing four quarters for an honest LTM; else leave absent.
            if len(vals) == 4:
                ltm[k] = sum(vals)
        else:
            # Latest snapshot: walk newest→oldest, skipping artifact zeros so a broken
            # current-period stock falls back to the most recent real value.
            for v in reversed(list(col)):
                fv = _num(v)
                if fv is not None and not _is_artifact_zero(k, fv):
                    ltm[k] = fv
                    break

    # --- Fiscal-year rollup ---------------------------------------------------
    by_year: dict[int, dict[int, dict[str, float]]] = {}  # year -> quarter -> {metric: val}
    for _, row in df.iterrows():
        yq = _period_year_q(str(row["period"]))
        if not yq:
            continue
        yr, q = yq
        cell = by_year.setdefault(yr, {}).setdefault(q, {})
        for k in present:
            fv = _num(row[k])
            if fv is not None:
                cell[k] = fv

    annual: dict[int, dict[str, float]] = {}
    for yr, quarters in by_year.items():
        out: dict[str, float] = {}
        for k in present:
            if k in flows and not annual_only:
                qvals = [quarters[q][k] for q in (1, 2, 3, 4) if q in quarters and k in quarters[q]]
                if len(qvals) == 4:
                    out[k] = sum(qvals)
            else:
                # year-end snapshot: prefer Q4, else the latest available quarter. Skip artifact
                # zeros for must-be-positive stocks so a broken year stays blank (no fabricated
                # carry-forward) — this is what keeps historical P/BV honest and book-value growth
                # from computing a −100% off a zero base.
                for q in (4, 3, 2, 1):
                    if q in quarters and k in quarters[q] and not _is_artifact_zero(k, quarters[q][k]):
                        out[k] = quarters[q][k]
                        break
        if out:
            annual[yr] = out

    # --- prospectus-stub reclassification -------------------------------------
    # An annual-report/prospectus (ar_pros) filing carries governance narrative but stubs EVERY
    # ifrs-full monetary tag to 0 (GNP): the real insurer financials are filed under a different
    # (seguros) taxonomy that isn't cached. That's a CORPUS gap, not an engine bug — so drop the
    # zero-stub flows to None. The completeness gate then reads the subject as un-sourced → INCOMPLETE
    # (corpus pending), not BROKEN (a blank we own). Fires only when EVERY core metric is 0/absent (a
    # real operating company never reports all-zero), so a legitimately-zero single line is untouched.
    if _is_prospectus_stub(ltm):
        for k in ("revenue", "net_income", "eps"):
            ltm.pop(k, None)

    # --- native derivations (no Bloomberg) ------------------------------------
    currency = str(((cfg.get("company") or {}).get("currency"))
                   or cfg.get("currency") or "MXN").upper()
    _derive_native(ltm, currency=currency)
    for yr in annual:
        _derive_native(annual[yr], currency=currency)

    return Fundamentals(
        slug=slug,
        frame=df,
        periods=periods,
        current_period=current,
        ltm=ltm,
        annual=annual,
        flow_keys=flows,
        confidence=dict(getattr(df, "attrs", {}).get("confidence", {})),
    )


def _derive_native(m: dict, currency: str = "MXN") -> None:
    """Inject natively-derived metrics from XBRL inputs (in place). Bloomberg-free.

    - ebitda        = operating_income + depreciation
    - tangible_book = equity − intangibles − goodwill
    - shares_out    = net_income / eps            (eps is per-share; NI in the same period)
    - fcf           = cfo − capex
    Each is only set when its inputs are present, so gaps fall to the Bloomberg residual.

    ``currency`` is the config reporting currency. Monetary facts (net_income, …) are scaled to
    MXN by the extraction tier, but ``eps`` is per-share and left in the reporting currency — so for
    a USD reporter (Grupo México, Orbia, Gruma) ``net_income(MXN) / eps(USD)`` inflates shares_out
    ×USDMXN and hence market cap / P/E / EV/EBITDA. Scale eps to MXN first to keep it consistent.
    """
    def g(k):
        v = m.get(k)
        return v if isinstance(v, (int, float)) and v == v else None

    def gnz(k):
        """Non-zero numeric, else None — a 0 for a balance-sheet stock is an extraction artifact."""
        v = g(k)
        return None if (v is None or v == 0) else v

    # XBRL tags total equity as `equity` (ifrs-full_Equity); valuation reads `total_equity`.
    # Backfill when total_equity is missing OR an artifact 0, from a non-zero equity.
    if not m.get("total_equity") and gnz("equity") is not None:
        m["total_equity"] = gnz("equity")

    oi, dep = g("operating_income"), g("depreciation")
    derived_ebitda = (oi + abs(dep)) if (oi is not None and dep is not None) else None
    cur_ebitda = g("ebitda")
    if cur_ebitda is None and derived_ebitda is not None:
        m["ebitda"] = derived_ebitda   # D&A may be tagged negative in cash-flow context
    elif cur_ebitda is not None and derived_ebitda not in (None, 0):
        # Scale-artifact guard: some FIBRA/issuer EBITDA facts come out un-scaled (raw pesos vs the
        # millions everything else uses) → off by ~10^3/10^6 from oi+D&A. Prefer the derived value
        # so EBITDA is internally consistent (the math gate's ebitda_derivation check would else
        # block the deliverable). Normal EBITDA (ratio ≈ 1) is left untouched.
        ratio = cur_ebitda / derived_ebitda
        if ratio > 100 or ratio < 0.01:
            m["ebitda"] = derived_ebitda

    eq = gnz("equity")
    if not m.get("tangible_book") and eq is not None:
        m["tangible_book"] = eq - (g("intangibles") or 0.0) - (g("goodwill") or 0.0)

    ni, eps = g("net_income"), g("eps")
    if eps is not None and currency == "USD":
        from src.extract.xbrl_facts import _USDMXN
        eps = eps * _USDMXN  # eps is per-share, unscaled → restate to MXN to match net_income
    if m.get("shares_out") is None and ni is not None and eps not in (None, 0):
        cand = ni / eps
        # A near-zero eps (an extraction artifact, common for loss-makers whose bottom line rounds to
        # ~0 per share) makes ni/eps explode into an absurd count — Club America: eps≈-3e-5 →
        # 340,621mn "shares" (340bn) → market cap / P/E / P/S all garbage. No listed BMV company has
        # more than ~65bn shares (AMX), so reject a super-plausible-magnitude derivation and let shares
        # fall to the direct XBRL count / residual (blank honestly) rather than shipping a wrong number.
        if abs(cand) <= _MAX_PLAUSIBLE_SHARES_MN:
            m["shares_out"] = cand

    cfo, capex = g("cfo"), g("capex")
    if m.get("fcf") is None and cfo is not None and capex is not None:
        m["fcf"] = cfo - abs(capex)
