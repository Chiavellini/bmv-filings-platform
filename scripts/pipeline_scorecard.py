#!/usr/bin/env python3
"""pipeline_scorecard.py — score the full pipeline (download → parse → extract)
for every ground-truth company and print one phase-by-phase table.

Phases per company:
  1. Download : PDFs on disk / AVAILABLE   (available = historical actual '…A'
                quarters in the GT CSV that have a value for >=1 mapped metric
                and whose quarter-end is on/before today; '…E' estimate columns
                and not-yet-reported quarters are excluded).
  2. Parse    : '.md' produced / PDFs downloaded.
  3. Extract  : correct observations / total observations, over the full
                deterministic cascade (xbrl+bmv+search+table+prose), where an observation is
                a (mapped-metric, period) cell that has a GT value and a parsed
                report on disk.

Extraction reuses compare_extractions.run_comparison so the numbers match the
existing scorer exactly. Offline/deterministic (no LLM).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.eval.compare_extractions import (
    COMPANIES,
    parse_actuales,
    parse_period,
    run_comparison,
)

# Deterministic full cascade (LLM excluded — offline, no cost). "note" is the
# header-aligned [800200] income-note tier (opt-in per config; liverpool/chedraui);
# omitting it here silently measured those companies without their segment tier.
FULL_TIERS = {"xbrl", "bmv", "note", "search", "table", "prose"}

# Latest fully-reported quarter as of the run date (2026-06-21): 1Q26 is out;
# 2Q26 closes 2026-06-30, not yet reported. Periods after this are excluded from
# "available" since no report can exist yet.
LATEST_REPORTED = "1Q26A"


def _period_order(p: str) -> tuple[int, int]:
    """'1Q26A' -> (2026, 1) for chronological comparison."""
    return (2000 + int(p[2:4]), int(p[0]))


_CUTOFF = _period_order(LATEST_REPORTED)


def available_quarters(company: str) -> set[str]:
    """Historical actual quarters with a GT value for >=1 mapped metric, up to the
    latest reported quarter."""
    comp = COMPANIES[company]
    actuales = parse_actuales(comp["actual_file"])
    quarters: set[str] = set()
    for key, (section, label, _tol) in comp["metric_map"].items():
        period_map = actuales.get((section, label), {})
        for period, val in period_map.items():
            if val is not None and _period_order(period) <= _CUTOFF:
                quarters.add(period)
    return quarters


def downloaded_quarters(company: str, available: set[str]) -> tuple[set[str], set[str]]:
    """(periods with a .pdf on disk, periods with a .md on disk), restricted to
    available quarters."""
    src = Path(COMPANIES[company]["source_dir"])
    pdfs, mds = set(), set()
    if src.exists():
        for f in src.iterdir():
            if f.suffix.lower() not in (".pdf", ".md"):
                continue
            period = parse_period(f.stem)
            if period is None or period not in available:
                continue
            (pdfs if f.suffix.lower() == ".pdf" else mds).add(period)
    return pdfs, mds


def score_company(company: str) -> dict:
    available = available_quarters(company)
    pdfs, mds = downloaded_quarters(company, available)

    # Extraction: full deterministic cascade, scored vs GT.
    results = run_comparison(company, enabled_tiers=set(FULL_TIERS), use_llm=False)
    # Extractable-obs basis: documented data-unavailable cells (EXCLUDED) are
    # removed from the denominator. The excluded count is reported alongside so
    # the headline accuracy stays auditable.
    excluded = sum(1 for r in results if r["status"] == "EXCLUDED")
    scored = [r for r in results if r["status"] != "EXCLUDED"]
    total = len(scored)
    correct = sum(1 for r in scored if r["status"] == "PASS")
    miss = sum(1 for r in scored if r["status"] == "MISS")
    fail = sum(1 for r in scored if r["status"] == "FAIL")

    return {
        "company": company,
        "available": len(available),
        "downloaded": len(pdfs),
        "parsed": len(mds),
        "obs_total": total,
        "obs_correct": correct,
        "obs_miss": miss,
        "obs_fail": fail,
        "obs_excluded": excluded,
        "n_metrics": len(COMPANIES[company]["metric_map"]),
    }


def _pct(num: int, den: int) -> str:
    return f"{num/den*100:4.0f}%" if den else "   –"


def print_table(rows: list[dict]) -> None:
    print("\n" + "=" * 96)
    print("FULL-PIPELINE SCORECARD  (download → parse → extract)   tiers=xbrl,bmv,note,search,table,prose")
    print("=" * 96)
    hdr = (f"{'company':<11}{'metrics':>8}{'  DOWNLOAD':>14}{'  PARSE':>14}"
           f"{'  EXTRACT (correct/obs)':>26}{'  miss':>7}{'  fail':>7}{'  excl':>7}")
    print(hdr)
    print("-" * 103)
    tot = {k: 0 for k in ("available", "downloaded", "parsed", "obs_total", "obs_correct", "obs_miss", "obs_fail", "obs_excluded")}
    for r in rows:
        dl = f"{r['downloaded']}/{r['available']} {_pct(r['downloaded'], r['available'])}"
        pa = f"{r['parsed']}/{r['downloaded']} {_pct(r['parsed'], r['downloaded'])}"
        ex = f"{r['obs_correct']}/{r['obs_total']} {_pct(r['obs_correct'], r['obs_total'])}"
        print(f"{r['company']:<11}{r['n_metrics']:>8}{dl:>14}{pa:>14}{ex:>26}"
              f"{r['obs_miss']:>7}{r['obs_fail']:>7}{r['obs_excluded']:>7}")
        for k in tot:
            tot[k] += r[k]
    print("-" * 103)
    dl = f"{tot['downloaded']}/{tot['available']} {_pct(tot['downloaded'], tot['available'])}"
    pa = f"{tot['parsed']}/{tot['downloaded']} {_pct(tot['parsed'], tot['downloaded'])}"
    ex = f"{tot['obs_correct']}/{tot['obs_total']} {_pct(tot['obs_correct'], tot['obs_total'])}"
    print(f"{'TOTAL':<11}{'':>8}{dl:>14}{pa:>14}{ex:>26}{tot['obs_miss']:>7}{tot['obs_fail']:>7}{tot['obs_excluded']:>7}")
    print("=" * 103)
    print("DOWNLOAD = report PDFs on disk / actual quarters with ground truth (≤ 1Q26).")
    print("PARSE    = markdown files produced / PDFs downloaded.")
    print("EXTRACT  = values within tolerance / extractable (mapped-metric × period) cells with GT + a report.")
    print("excl     = documented data-unavailable cells removed from the denominator (see NIGHT_LOG ledger).")


def main() -> None:
    companies = sys.argv[1:] or list(COMPANIES)
    rows = [score_company(c) for c in companies]
    print_table(rows)


if __name__ == "__main__":
    main()
