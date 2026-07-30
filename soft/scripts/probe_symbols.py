#!/usr/bin/env python3
"""Probe Yahoo symbol resolution for the master matrix's no-price names.

The certifier lumps every blank price cell on a no-price row under **throttled-price (recoverable)** —
but that single label hides three very different states. This probe splits them:

  * RESOLVED       — a Yahoo EQUITY symbol exists and returns a price. If that symbol is one
                     ``market_data.candidate_symbols()`` can't build, it needs a ``_YAHOO_OVERRIDE``
                     entry (the probe flags this as ADD-OVERRIDE and prints the exact symbol).
  * NOT-FOUND      — every candidate + common dashed-series variant returned 404 / empty (no free
                     Yahoo listing). Candidate for ``scripts/build_master._CORE_EXCLUDE``.
  * INCONCLUSIVE   — a 429 was seen and nothing resolved: we were throttled, NOT proof of absence.
                     Left for the off-hours ``refresh_daily.py`` (never labelled dead).

Design: reuses the SAME resolution the build uses (``candidate_symbols`` order, the browser-UA
session, the 0.4s throttle, the EQUITY-only guard). It is cache-first — a non-empty
``.cache/market_data/yf_<sym>.json`` counts as RESOLVED without a network call, so already-priced
names cost nothing and the live load stays bounded to genuinely-unknown symbols.

    python3 scripts/probe_symbols.py                       # all no-price names
    python3 scripts/probe_symbols.py --only grupo_bafar,alfa,grupo_sanborns
    python3 scripts/probe_symbols.py --limit 10 --fresh    # first 10, ignore cache

Read-only w.r.t. the repo (writes only outputs/_reconcile/symbol_probe.md). It DOES hit Yahoo.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.certify_master import certify, _roster_index  # noqa: E402
from src.download import market_data  # noqa: E402

REPORT = ROOT / "outputs" / "_reconcile" / "symbol_probe.md"


def _ticker_for(slug: str) -> str | None:
    p = ROOT / "configs" / f"{slug}.yaml"
    if not p.exists():
        return None
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return (cfg.get("company") or {}).get("ticker")


def _dashed_variants(clave: str | None, ticker: str | None) -> list[str]:
    """Common Yahoo dashed/vintage series forms the plain suffix scan can't build
    (GSANBORB-1.MX, LIVEPOLC-1.MX, MFRISCOA-1.MX, IDEALB-1.MX shapes)."""
    bases: list[str] = []
    if ticker:
        bases.append(market_data._clean(ticker.split()[0]))
    if clave:
        bases.append(market_data._clean(clave))
    seen: set[str] = set()
    out: list[str] = []
    for b in bases:
        if not b:
            continue
        for ser in ("", "A", "B", "C", "O", "L"):
            for tail in (f"{ser}-1", f"{ser}1", f"{ser}-2", f"{ser}2"):
                s = f"{b}{tail}.MX"
                if s not in seen:
                    seen.add(s)
                    out.append(s)
    return out


def _cache_hit(symbol: str) -> bool:
    """A non-empty cached chart JSON → this symbol resolved at least once (RESOLVED w/o network)."""
    cache = market_data._CACHE / f"yf_{symbol.replace('.', '_')}.json"
    try:
        return cache.stat().st_size > 50
    except OSError:
        return False


def _probe_symbol(symbol: str, *, use_cache: bool) -> str:
    """One symbol → 'equity' | 'nonequity' | 'notfound' | 'throttled' | 'error'."""
    if use_cache and _cache_hit(symbol):
        return "equity"
    time.sleep(0.4)  # same polite throttle as _fetch_yahoo
    try:
        resp = market_data._session_get(market_data.YF_URL.format(sym=symbol), False)
    except Exception:
        return "error"
    status = getattr(resp, "status_code", 200)
    if status == 429:
        return "throttled"
    if status != 200:
        return "notfound"
    try:
        data = json.loads(resp.text)
    except Exception:
        return "error"
    result = (data.get("chart") or {}).get("result")
    if not result:
        return "notfound"
    meta = result[0].get("meta") or {}
    itype = meta.get("instrumentType") or meta.get("quoteType")
    if itype and itype != "EQUITY":
        return "nonequity"
    if result[0].get("timestamp") or meta.get("regularMarketPrice"):
        return "equity"
    return "notfound"


def probe_name(clave: str | None, ticker: str | None, *, use_cache: bool) -> tuple[str, str | None, bool]:
    """Return (verdict, symbol, buildable) where verdict is resolved|notfound|inconclusive and
    buildable says whether candidate_symbols() already produces the resolving symbol (no override
    needed). Tries the standard candidates first, then the dashed-series variants."""
    standard = market_data.candidate_symbols(clave or "", ticker)
    std_set = set(standard)
    saw_throttle = False
    for sym in standard:
        v = _probe_symbol(sym, use_cache=use_cache)
        if v == "equity":
            return "resolved", sym, True          # buildable by candidate_symbols → no override needed
        if v == "throttled":
            saw_throttle = True
    for sym in _dashed_variants(clave, ticker):
        if sym in std_set:
            continue
        v = _probe_symbol(sym, use_cache=use_cache)
        if v == "equity":
            return "resolved", sym, False         # needs a _YAHOO_OVERRIDE entry
        if v == "throttled":
            saw_throttle = True
    return ("inconclusive" if saw_throttle else "notfound"), None, False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None, help="comma-separated slugs (default: all no-price names)")
    ap.add_argument("--limit", type=int, default=None, help="probe at most N names")
    ap.add_argument("--fresh", action="store_true", help="ignore the price cache; force live probe")
    args = ap.parse_args()

    _, gaps, _ = certify()
    no_price_names = sorted({g[0] for g in gaps if g[2] == "throttled-price"})
    roster = _roster_index()

    targets: list[tuple[str, str, str | None, str | None]] = []  # (name, slug, clave, ticker)
    only = {s.strip() for s in args.only.split(",")} if args.only else None
    for name in no_price_names:
        meta = roster.get(name)
        if meta is None:
            continue
        slug, clave, _template, _sector = meta
        if only and slug not in only and name not in only:
            continue
        targets.append((name, slug, clave, _ticker_for(slug)))
    if args.limit:
        targets = targets[:args.limit]

    print(f"[probe] {len(targets)} names (cache={'off' if args.fresh else 'on'})")
    resolved_ok: list[tuple[str, str, str]] = []       # (name, slug, symbol) — buildable, transient
    resolved_override: list[tuple[str, str, str]] = []  # (name, slug, symbol) — ADD OVERRIDE
    notfound: list[tuple[str, str]] = []
    inconclusive: list[tuple[str, str]] = []
    for name, slug, clave, ticker in targets:
        verdict, sym, buildable = probe_name(clave, ticker, use_cache=not args.fresh)
        if verdict == "resolved" and buildable:
            resolved_ok.append((name, slug, sym))
            tag = "resolved "
        elif verdict == "resolved":
            resolved_override.append((name, slug, sym))
            tag = "OVERRIDE "
        elif verdict == "inconclusive":
            inconclusive.append((name, slug))
            tag = "throttled"
        else:
            notfound.append((name, slug))
            tag = "NOT-FOUND"
        print(f"  {tag} {slug:26s} {sym or ''}")

    lines = ["# Symbol-resolution probe — splitting the 'throttled-price' bucket", ""]
    lines.append(f"Probed **{len(targets)}** no-price names.")
    lines.append(f"- resolved (buildable, transient throttle — refresh will fill): **{len(resolved_ok)}**")
    lines.append(f"- resolved but needs _YAHOO_OVERRIDE: **{len(resolved_override)}**")
    lines.append(f"- NOT-FOUND (candidate for _CORE_EXCLUDE): **{len(notfound)}**")
    lines.append(f"- inconclusive (throttled during probe — retry off-hours): **{len(inconclusive)}**")
    lines.append("")
    if resolved_override:
        lines.append("## ADD to `_YAHOO_OVERRIDE` (market_data.py)")
        lines.append("")
        lines.append("| Company | slug | resolving symbol |")
        lines.append("|---|---|---|")
        for name, slug, sym in resolved_override:
            lines.append(f"| {name} | {slug} | `{sym}` |")
        lines.append("")
    if notfound:
        lines.append("## NOT-FOUND — candidates for `_CORE_EXCLUDE` (build_master.py)")
        lines.append("")
        for name, slug in notfound:
            lines.append(f"- {slug}  ({name})")
        lines.append("")
    if inconclusive:
        lines.append("## Inconclusive (throttled during probe — NOT dead)")
        lines.append("")
        for name, slug in inconclusive:
            lines.append(f"- {slug}  ({name})")
        lines.append("")
    if resolved_ok:
        lines.append("## Resolved & buildable (transient throttle only)")
        lines.append("")
        for name, slug, sym in resolved_ok:
            lines.append(f"- {slug}  →  `{sym}`")
        lines.append("")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"[probe] report → {REPORT}")
    print(f"[probe] resolved {len(resolved_ok)} · override {len(resolved_override)} · "
          f"not-found {len(notfound)} · inconclusive {len(inconclusive)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
