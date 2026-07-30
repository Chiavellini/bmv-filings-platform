#!/usr/bin/env python3
"""census_debt_concepts.py — read-only coverage census of balance-sheet debt
concepts across all XBRL facts files, BEFORE the phase-H debt definition is
frozen. The census (not returns, not SUEs) is the only evidence the
definition choice may use — committed to earnings/audit/ alongside the
study.yaml amendment.

For every soft/data/reports/<slug>/xbrl/<TICKER>_<period>_facts.json:
count, per concept of interest, whether a dimensionless entry exists with
instant == period_end and a finite value. Also records the reporting unit.
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import pandas as pd

QEND = {"1": "-03-31", "2": "-06-30", "3": "-09-30", "4": "-12-31"}
FNAME = re.compile(r"^(?P<ticker>.+)_(?P<period>20\d{2}-[1-4]T)_facts\.json$")

CONCEPTS = [
    "ifrs-full_Borrowings",
    "ifrs-full_CurrentBorrowings",
    "ifrs-full_NoncurrentBorrowings",
    "ifrs-full_ShorttermBorrowings",
    "ifrs-full_LongtermBorrowings",
    "ifrs-full_OtherCurrentFinancialLiabilities",
    "ifrs-full_OtherNoncurrentFinancialLiabilities",
    "ifrs-full_CurrentLeaseLiabilities",
    "ifrs-full_NoncurrentLeaseLiabilities",
    "ifrs-full_CashAndCashEquivalents",
    "ifrs-full_CurrentLiabilities",
    "ifrs-full_NoncurrentLiabilities",
    "ifrs-full_Liabilities",
    "ifrs-full_Equity",
]


def instant_value(entries, period_end: str):
    """Dimensionless entry at instant == period_end, else None."""
    if not isinstance(entries, list):
        return None
    for e in entries:
        if (e.get("instant") == period_end and not e.get("dimensions")
                and e.get("value") is not None):
            return e
    return None


def main() -> None:
    reports = bs.SOFT_ROOT / "data" / "reports"
    per_slug = defaultdict(lambda: defaultdict(int))
    n_files = defaultdict(int)
    units = defaultdict(set)
    extra_borrow = defaultdict(int)   # any other instant concept mentioning Borrowings

    for facts_path in sorted(reports.glob("*/xbrl/*_facts.json")):
        m = FNAME.match(facts_path.name)
        if not m:
            continue
        slug = facts_path.parent.parent.name
        period = m.group("period")
        pend = period[:4] + QEND[period[5]]
        try:
            facts = json.loads(facts_path.read_text()).get("facts", {})
        except (json.JSONDecodeError, OSError):
            continue
        n_files[slug] += 1
        for c in CONCEPTS:
            e = instant_value(facts.get(c), pend)
            if e is not None:
                per_slug[slug][c] += 1
                if e.get("unit"):
                    units[slug].add(e["unit"])
        for k, v in facts.items():
            if "Borrowings" in k and k not in CONCEPTS:
                if instant_value(v, pend) is not None:
                    extra_borrow[k] += 1

    rows = []
    for slug in sorted(n_files):
        row = {"slug": slug, "n_files": n_files[slug],
               "units": "|".join(sorted(units[slug])) or ""}
        for c in CONCEPTS:
            short = c.replace("ifrs-full_", "")
            row[short] = per_slug[slug].get(c, 0)
        rows.append(row)
    df = pd.DataFrame(rows)

    out_dir = bs.EARNINGS_ROOT / "audit"
    out_dir.mkdir(exist_ok=True)
    df.to_csv(out_dir / "debt_census.csv", index=False)

    total = df["n_files"].sum()
    print(f"census: {len(df)} slugs, {total} facts files")
    print("\ncoverage share of files (all slugs pooled):")
    for c in CONCEPTS:
        short = c.replace("ifrs-full_", "")
        print(f"  {short:45s} {df[short].sum() / total:6.1%}")
    if extra_borrow:
        print("\nother instant-dated *Borrowings* concepts found:")
        for k, n in sorted(extra_borrow.items(), key=lambda kv: -kv[1]):
            print(f"  {k}: {n}")
    else:
        print("\nno other instant-dated *Borrowings* concepts exist anywhere")
    usd = df[df["units"].str.contains("USD", na=False)]["slug"].tolist()
    print(f"\nnon-MXN reporters: {usd}")


if __name__ == "__main__":
    main()
