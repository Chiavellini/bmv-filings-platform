#!/usr/bin/env python3
"""Emit ONE consolidated Bloomberg pull-matrix for many companies at once.

Instead of filling per-company ``inputs/<slug>.bloomberg.csv`` by hand, this expands the FULL
field registry for every requested company into a single Excel-ready sheet with a ready
``=BDP(...)`` formula per cell. Paste it into a Bloomberg-connected Excel, let BDP resolve, then
the ``value`` column is what each ``data/bloomberg/<slug>.csv`` needs.

    python3 scripts/emit_bdp_matrix.py                      # all companies with an input spec
    python3 scripts/emit_bdp_matrix.py --only cemex,walmex
    python3 scripts/emit_bdp_matrix.py --template reit

Output: outputs/_bloomberg/pull_matrix.csv  (+ a short README with the load-back recipe).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bloomberg.schema import required_rows  # noqa: E402
from src.coverage.spec import parse_spec  # noqa: E402

# hints that are analyst assumptions / derived, not a single BDP field
_MANUAL = {"analyst assumption / build cost", "peer comp median EV/EBITDA",
           "provisions / avg loans", "loan book YoY", "deposits YoY", "per the spec row",
           "occupancy", "cap rate", "gross leasable area", "LTV"}
_MNEMONIC_RE = re.compile(r"^([A-Z][A-Z0-9_]+)")

# Fields the NATIVE pack already sources for free (Yahoo prices + BMV-XBRL fundamentals + Banxico
# macro) — see src/coverage/native.py _FUND_TO_PACK + build_native_pack. In residual mode these are
# EXCLUDED, so the terminal is only asked for what native genuinely can't produce. (net_debt and
# minority_interest are NOT native — the XBRL lists extract cash but no debt line — so they stay in
# the residual as an authoritative terminal pull.)
_NATIVE_FIELDS = {
    "px_last", "px_fy_close", "shares_out",
    "sales_ltm", "ebitda_ltm", "net_income_ltm", "total_equity", "book_value",
    "tangible_book", "fcf_ltm", "total_assets",
    # now sourced from the filings (P1–P3): minority (XBRL), bank ratios (CNBV annual narrative),
    # ROE/ROTE (derived), occupancy (FIBRA MD&A). Extraction is best-effort per issuer — where the
    # scorecard shows one blank, it remains a manual/terminal pull for that company.
    "minority_interest", "roe", "rote", "nim", "efficiency_ratio", "cost_of_risk", "cet1",
    "occupancy",
}


def _bdp(ticker: str, hint: str, unit: str, period: str, kind: str):
    """Return (bbg_field, formula, note) for a cell."""
    if kind == "macro":
        return "", "", "macro series — Banxico/INEGI or terminal (ECO)"
    if hint in _MANUAL:
        return "", "", f"manual / analyst input ({hint})"
    m = _MNEMONIC_RE.match(hint)
    if not m:
        return "", "", f"manual ({hint})"
    field = m.group(1)
    paren = re.search(r"\(([^)]+)\)", hint)
    qualifier = paren.group(1) if paren else ""
    if kind == "history" and period:
        # FY-close price → BDH on the fiscal year-end date
        formula = f'=BDH("{ticker} Equity","{field}","12/31/{period}","12/31/{period}")'
        return field, formula, "FY-close price"
    formula = f'=BDP("{ticker} Equity","{field}")'
    note = ""
    if qualifier:
        note = f"{qualifier} — may need an override (e.g. LTM/NTM period)"
    return field, formula, note


def _ticker_for(row, spec):
    # history rows are the SUBJECT's FY-close prices → use the subject ticker (not blank!)
    if row.entity_kind in ("subject", "history"):
        return spec.ticker or spec.slug.upper()
    if row.entity_kind == "peer":
        for p in spec.peers:
            if p.slug == row.entity:
                return p.name or p.ticker or p.slug.upper()
    return ""  # segment / macro have no single ticker


# The ONLY fields with no public source (everything else = native/filings). This is the entire
# legitimate Bloomberg ask.
_NECESSARY = {
    "eps_ntm": "BEST_EPS",      # consensus forward EPS → forward P/E
    "nav_ps": "NAV_PER_SHARE",  # REIT NAV/share (MX FIBRAs don't disclose it) → P/NAV
}


def _emit_necessary(specs, only, template) -> None:
    """The minimal, maximally-facilitated Bloomberg sheet: only eps_ntm + nav_ps, de-duplicated to
    one row per unique BBG ticker, as a paste-once grid."""
    eps: dict[str, list[str]] = {}
    nav: dict[str, list[str]] = {}
    for sp in specs:
        try:
            spec = parse_spec(str(sp))
        except Exception:
            continue
        if only and spec.slug not in only:
            continue
        if template and spec.template != template:
            continue
        for r in required_rows(spec):
            if r.field not in _NECESSARY:
                continue
            tk = _ticker_for(r, spec)
            if not tk:
                continue
            (eps if r.field == "eps_ntm" else nav).setdefault(tk, []).append(spec.name)

    tickers = sorted(set(eps) | set(nav))
    out_dir = ROOT / "outputs" / "_bloomberg"
    out_dir.mkdir(parents=True, exist_ok=True)

    grid = out_dir / "necessary_pull.csv"
    with grid.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "BEST_EPS (fwd/NTM)", "NAV_PER_SHARE (REITs only)"])
        for tk in tickers:
            eps_f = f'=BDP("{tk} Equity","BEST_EPS")' if tk in eps else ""
            nav_f = f'=BDP("{tk} Equity","NAV_PER_SHARE")' if tk in nav else ""
            w.writerow([tk, eps_f, nav_f])

    mp = out_dir / "necessary_map.csv"
    with mp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "field", "used_by_companies"])
        for tk in tickers:
            if tk in eps:
                w.writerow([tk, "eps_ntm", "; ".join(sorted(set(eps[tk])))])
            if tk in nav:
                w.writerow([tk, "nav_ps", "; ".join(sorted(set(nav[tk])))])

    (out_dir / "README_necessary.md").write_text(
        "# Bloomberg — the ONLY necessary pull\n\n"
        f"- {len(tickers)} unique tickers × up to 2 fields "
        f"({len(eps)} need forward EPS, {len(nav)} REITs need NAV).\n"
        "- Everything else (prices, fundamentals, net debt, minority, bank/REIT ratios, dividend "
        "yield) is sourced natively from Yahoo + the BMV filings — **do not pull it**.\n\n"
        "## One-paste workflow\n"
        "1. Open `necessary_pull.csv` in a Bloomberg-connected Excel.\n"
        "2. The `=BDP(...)` cells resolve live. Paste-special → values.\n"
        "3. Send the filled grid back; `necessary_map.csv` routes each ticker's values to every "
        "`data/bloomberg/<slug>.csv` that uses it.\n\n"
        "Skip forward P/E and P/NAV → this pull is **zero** (trailing multiples are fully native).\n",
        encoding="utf-8")
    print(f"[bdp] NECESSARY: {len(tickers)} unique tickers "
          f"(eps={len(eps)}, nav={len(nav)}) -> {grid}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default=None, help="comma-separated slugs")
    ap.add_argument("--template", default=None)
    ap.add_argument("--necessary", action="store_true",
                    help="RECOMMENDED: only the two fields with no public source (eps_ntm, nav_ps), "
                         "de-duplicated to a one-paste ticker grid.")
    ap.add_argument("--full", action="store_true",
                    help="emit the FULL template (reference only — over-asks).")
    args = ap.parse_args()

    specs = sorted((ROOT / "inputs").glob("*.md"))
    only = set(args.only.split(",")) if args.only else None
    if args.necessary:
        _emit_necessary(specs, only, args.template)
        return
    residual = not args.full
    rows_out: list[dict] = []
    n_companies = 0
    for sp in specs:
        try:
            spec = parse_spec(str(sp))
        except Exception:
            continue
        if only and spec.slug not in only:
            continue
        if args.template and spec.template != args.template:
            continue
        n_companies += 1
        for r in required_rows(spec):  # full registry; residual filter applied below
            if residual and r.field in _NATIVE_FIELDS:
                continue  # native sources this — don't ask the terminal for it
            if residual and r.entity_kind == "macro":
                continue  # Banxico/INEGI native
            ticker = _ticker_for(r, spec)
            field, formula, note = _bdp(ticker, r.bbg_hint, r.unit, r.period, r.entity_kind)
            rows_out.append({
                "company": spec.name, "slug": spec.slug, "template": spec.template,
                "entity_kind": r.entity_kind, "ticker": ticker, "field": r.field,
                "period": r.period, "label": r.label, "unit": r.unit,
                "bbg_field": field, "bdp_formula": formula, "note": note, "value": "",
            })

    out_dir = ROOT / "outputs" / "_bloomberg"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / ("residual_pull_matrix.csv" if residual else "pull_matrix.csv")
    cols = ["company", "slug", "template", "entity_kind", "ticker", "field", "period",
            "label", "unit", "bbg_field", "bdp_formula", "note", "value"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows_out)

    n_bdp = sum(1 for r in rows_out if r["bdp_formula"])
    n_manual = sum(1 for r in rows_out if not r["bdp_formula"])
    mode = "RESIDUAL (minimize-Bloomberg)" if residual else "FULL"
    fname = out.name
    (out_dir / "README.md").write_text(
        f"# Bloomberg pull-matrix — {mode}\n\n"
        f"- {n_companies} companies, {len(rows_out)} cells "
        f"({n_bdp} BDP-automatable, {n_manual} manual/analyst).\n\n"
        + ("**Residual mode:** prices, subject+peer fundamentals, and macro are sourced natively "
           "(Yahoo + BMV-XBRL + Banxico) and are NOT listed here. This file is only what the "
           "terminal must supply: consensus estimates (EPS NTM, dividend yield), net debt / "
           "minority interest, and the bank/REIT specialty ratios + analyst inputs.\n\n"
           if residual else "")
        + "## Recipe\n"
        f"1. Open `{fname}` in a Bloomberg-connected Excel.\n"
        "2. The `bdp_formula` column resolves live (BDP/BDH). Paste-special → values.\n"
        "3. Copy the resolved number into the `value` column.\n"
        "4. Split back per company into `data/bloomberg/<slug>.csv` (same schema as the "
        "`inputs/<slug>.bloomberg.csv` templates), or hand the filled matrix back and the engine "
        "will split it.\n\n"
        "**Units:** all currency in MXN millions, shares in millions, %-fields as the number "
        "(4.2 not 0.042). Manual rows (segment EV/EBITDA, replacement cost, cost of risk, "
        "loan/deposit growth) are analyst inputs — BDP can't fetch them.\n",
        encoding="utf-8")
    print(f"[bdp] {mode}: {n_companies} companies → {len(rows_out)} cells "
          f"({n_bdp} BDP, {n_manual} manual) -> {out}")


if __name__ == "__main__":
    main()
