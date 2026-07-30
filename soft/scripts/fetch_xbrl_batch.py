#!/usr/bin/env python3
"""Batch-prime the BMV XBRL cache for the coverage universe.

For each company in the classification table, fetch its XBRL filings into
``data/reports/<slug>/xbrl/`` (quarterly for industrials/FIBRAs; annual for financial-sector
issuers — the fetch retries annual automatically). Claves absent from the archive are skipped.

Usage (from soft/, needs network — run with the sandbox override in this environment):
    python3 scripts/fetch_xbrl_batch.py [--only slug1,slug2] [--template reit] [--limit N]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen_universe import UNIVERSE, is_absent, slugify  # noqa: E402
from src.coverage.fundamentals import _xbrl_period_sources  # noqa: E402


def roster():
    for sector, (template, members) in UNIVERSE.items():
        for clave, name in members:
            yield slugify(name), name, clave, template, sector


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default=None, help="comma-separated slugs to fetch")
    ap.add_argument("--template", default=None, help="restrict to one template")
    ap.add_argument("--limit", type=int, default=None, help="max companies to fetch")
    ap.add_argument("--max-filings", type=int, default=40)
    ap.add_argument("--include-absent", action="store_true",
                    help="ALSO attempt claves in the declared-ABSENT set (the 'UNVERIFIED' names) — "
                         "a hit means the name is actually in the archive and stops being Yahoo-only.")
    args = ap.parse_args()

    only = set(args.only.split(",")) if args.only else None
    rows = list(roster())
    if only:
        rows = [r for r in rows if r[0] in only]
    if args.template:
        rows = [r for r in rows if r[3] == args.template]
    if args.limit:
        rows = rows[: args.limit]

    ok = skipped = failed = 0
    print(f"[fetch] {len(rows)} companies")
    for slug, name, clave, template, sector in rows:
        # is_absent() is data-derived: a name that already has cached raw XBRL is never skipped
        # (harmless idempotent re-fetch). --include-absent overrides the skip entirely so the
        # declared-absent 'UNVERIFIED' names can be probed against the archive.
        if is_absent(clave, slug) and not args.include_absent:
            print(f"  SKIP {slug:22s} ({clave}) — declared not-in-archive → Bloomberg-only "
                  f"(use --include-absent to probe)")
            skipped += 1
            continue
        rdir = ROOT / "data" / "reports" / slug
        rdir.mkdir(parents=True, exist_ok=True)
        try:
            docs = _xbrl_period_sources(clave, rdir, args.max_filings)
            n = len(docs)
            q = sum(1 for p in docs if "T" in p)
            print(f"  OK   {slug:22s} ({clave:9s}) {n:2d} periods "
                  f"({'quarterly' if q else 'annual'})")
            ok += 1 if n else 0
            if not n:
                failed += 1
        except Exception as e:
            print(f"  FAIL {slug:22s} ({clave}): {e}")
            failed += 1

    print(f"[fetch] done — ok={ok} skipped={skipped} empty/failed={failed}")


if __name__ == "__main__":
    main()
