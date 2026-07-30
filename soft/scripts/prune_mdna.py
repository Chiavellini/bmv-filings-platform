#!/usr/bin/env python3
"""Prune the derived ``*_mdna.html`` MD&A-narrative artifacts that only ``industrial``-template
companies never read.

Each cached filing produces a derived ``<TICKER>_<PERIOD>_mdna.html`` (the MD&A prose, extracted
from the raw ``.json.gz``). It is consumed ONLY by:
  * ``src/extract/fibra_kpis.py::latest_mdna`` — REIT (``reit`` template) FFO/AFFO/NOI/occupancy, and
  * the generic prose cascade via ``bmv_xbrl.load_mdna_text`` — which REGENERATES the HTML from the
    retained raw ``.json.gz`` on demand if it is missing.
``industrial``-template companies have no native path that reads it, and bank ratios
(``src/extract/bank_ratios.py``) read the raw JSON, not this HTML. So for industrials it is pure
dead weight (~2.9 GB across the universe).

Deleting it is NON-destructive: the raw ``.json.gz`` and the distilled ``*_facts.json`` are kept, so
no numeric fact is lost, and ``load_mdna_text`` can rebuild the prose from the raw filing (offline,
no network) if a prose path ever needs it.

Template is read from ``scripts/gen_universe.UNIVERSE`` (authoritative) — the per-company configs
carry no ``template`` field. Slugs orphaned from ``UNIVERSE`` are simply never touched.

DRY-RUN BY DEFAULT (this deletes files) — pass ``--apply`` to actually remove them.

    python3 scripts/prune_mdna.py                    # report projected deletions only (default)
    python3 scripts/prune_mdna.py --apply            # delete industrial-template *_mdna.html
    python3 scripts/prune_mdna.py --templates industrial,financials --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen_universe import UNIVERSE, slugify  # noqa: E402

REPORTS_DIR = ROOT / "data" / "reports"


def roster():
    """Yield (slug, name, clave, template, sector) from UNIVERSE (same shape as build_master)."""
    for sector, (template, members) in UNIVERSE.items():
        for clave, name in members:
            yield slugify(name), name, clave, template, sector


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--templates", default="industrial",
                    help="comma-separated templates whose MD&A HTML to prune (default: industrial). "
                         "NEVER pass 'reit' — fibra_kpis needs it and does not regenerate it.")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete (default is a dry-run that only reports).")
    ap.add_argument("--root", type=Path, default=REPORTS_DIR, help="reports dir (default: %(default)s)")
    args = ap.parse_args()

    prune_templates = {t.strip() for t in args.templates.split(",") if t.strip()}
    if "reit" in prune_templates:
        sys.exit("refusing to prune reit MD&A — fibra_kpis.latest_mdna reads it and it does not "
                 "regenerate. Remove 'reit' from --templates.")
    if not args.root.is_dir():
        sys.exit(f"no such reports dir: {args.root}")

    mb = 1024 * 1024
    total_files = total_bytes = 0
    cos_touched = 0
    for slug, name, clave, template, sector in roster():
        if template not in prune_templates:
            continue
        xdir = args.root / slug / "xbrl"
        if not xdir.is_dir():
            continue
        files = sorted(xdir.glob("*_mdna.html"))
        if not files:
            continue
        cos_touched += 1
        for p in files:
            total_bytes += p.stat().st_size
            total_files += 1
            if args.apply:
                p.unlink()

    verb = "Deleted" if args.apply else "Would delete"
    print(f"{verb} {total_files} *_mdna.html files across {cos_touched} "
          f"{'/'.join(sorted(prune_templates))}-template companies "
          f"— {total_bytes / mb:,.0f} MB freed.")
    if not args.apply:
        print("(dry-run — pass --apply to delete. Raw .json.gz + *_facts.json are always kept; "
              "load_mdna_text regenerates the HTML on demand.)")


if __name__ == "__main__":
    main()
