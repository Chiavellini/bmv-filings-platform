#!/usr/bin/env python3
"""check_metric.py — fast single-metric oracle vs ground truth (overnight inner loop).

Reuses the comparison harness so the numbers match `compare_extractions`, but prints
just one metric's per-period extracted/GT/error/status plus a coverage+accuracy line.
Default tiers omit the slow pdfplumber 'table' tier (regex patterns tagged source:table
still run under 'prose'), so iteration is seconds.

Usage:
    python3 scripts/check_metric.py lacomer revenue
    python3 scripts/check_metric.py sport ebitda_sin_ifrs --tiers xbrl,table,prose
    python3 scripts/check_metric.py walmex total_stores --period 1Q14A
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.eval.compare_extractions import run_comparison, fmt_err, TOLERANCE


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("metric")
    ap.add_argument("--tiers", default="xbrl,prose",
                    help="comma list: xbrl,bmv,note,search,table,prose,llm "
                         "(default xbrl,prose — fast but UNDER-REPORTS: it skips "
                         "bmv/note/search AND custom extractors, which run under "
                         "'search'; pass the full tuned set for real numbers)")
    ap.add_argument("--period", default=None, help="filter to one period, e.g. 1Q14A")
    args = ap.parse_args()

    tiers = {t.strip() for t in args.tiers.split(",") if t.strip()}
    results = run_comparison(args.company, enabled_tiers=tiers)
    rows = [r for r in results if r["key"] == args.metric]
    if args.period:
        rows = [r for r in rows if r["period"] == args.period]
    if not rows:
        print(f"No results for {args.company}/{args.metric}"
              f"{' '+args.period if args.period else ''}.")
        return

    rows.sort(key=lambda r: r["period"])
    tol = rows[0]["tol_type"]
    print(f"\n{args.metric}  (tol={tol}, threshold={TOLERANCE[tol]}, tiers={','.join(sorted(tiers))})")
    if not {"bmv", "note", "search"} & tiers:
        print("NOTE: bmv/note/search tiers (and custom extractors) are OFF with these "
              "tiers — numbers may under-report vs certification.")
    print(f"{'period':<9}{'extracted':>16}{'actual':>16}{'error':>10}  status  tier")
    print("-" * 70)
    n_pass = n_checked = 0
    for r in rows:
        ext = r["extracted"]
        ext_s = f"{ext:,.2f}" if isinstance(ext, (int, float)) else "—"
        act = r["actual"]
        act_s = f"{act:,.2f}" if isinstance(act, (int, float)) else "—"
        err_s = fmt_err(r["error"], tol)
        if r["status"] in ("PASS", "FAIL"):
            n_checked += 1
            if r["status"] == "PASS":
                n_pass += 1
        print(f"{r['period']:<9}{ext_s:>16}{act_s:>16}{err_s:>10}  "
              f"{r['status']:<6}  {r['tier']}")

    n_total = len(rows)
    cov = f"{n_checked}/{n_total} ({n_checked/n_total*100:.0f}%)" if n_total else "—"
    acc = f"{n_pass}/{n_checked} ({n_pass/n_checked*100:.0f}%)" if n_checked else "—"
    print("-" * 70)
    print(f"coverage {cov}   accuracy {acc}")


if __name__ == "__main__":
    main()
