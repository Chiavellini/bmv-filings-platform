#!/usr/bin/env python3
"""sync_xbrl_facts.py — Revive Tier 1 by placing XBRL facts beside report .md files.

The cascade looks for a `<md_stem>_facts.json` sibling next to each report markdown
(see compare_extractions._build_source / pipeline._load_facts). BMV names its files
`<TICKER>_<YYYY-NT>_facts.json`, so this script downloads the archive filings and
copies each one to the matching report, joined on the canonical "NQ{YY}A" period
label (so "2021-2T" XBRL lands next to a "2T21bmv.md" / "2Q21-Results.md" report).

Companies are discovered from `configs/*.yaml`: any config with an
`ir_website.xbrl_ticker` participates (BMV archive is a ~5-yr rolling window,
so older companies get only recent quarters). The ~4 MB archive index is
fetched once and shared across all tickers.

Usage:
    python3 scripts/sync_xbrl_facts.py                 # all configured companies
    python3 scripts/sync_xbrl_facts.py walmex lacomer  # subset
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.download.bmv_xbrl import _logical_stem, download_ticker, fetch_archive_index
from src.eval.compare_extractions import parse_period
from src.shared.paths import CONFIGS_DIR, PROJECT_ROOT, REPORTS_DIR

DL = PROJECT_ROOT / "downloads"


def discover_companies() -> dict[str, tuple[str, Path]]:
    """slug -> (BMV ticker, report dir) for every config with an xbrl_ticker."""
    companies: dict[str, tuple[str, Path]] = {}
    for cfg_path in sorted(CONFIGS_DIR.glob("*.yaml")):
        if cfg_path.name.startswith("_"):
            continue
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            print(f"skip {cfg_path.name}: unreadable ({exc})", file=sys.stderr)
            continue
        ticker = ((cfg.get("ir_website") or {}).get("xbrl_ticker") or "").strip()
        if ticker:
            companies[cfg_path.stem] = (ticker, REPORTS_DIR / cfg_path.stem)
    return companies


def sync(company: str, ticker: str, report_dir: Path, *,
         index=None, include_annual: bool = False) -> tuple[int, int]:
    # include_annual also pulls YYYY-FY filings. Placement below matches on parse_period(), which
    # yields quarterly labels only — so annual facts are downloaded but land next to a report just
    # when an FY-named report exists and the matcher is extended; fetch_company_reports.py writes
    # annual facts straight into the report dir, which is the primary annual-facts path.
    json_paths = download_ticker(ticker, DL, index=index, max_filings=None, delay_ms=300,
                                 include_annual=include_annual)

    # canonical period label -> downloaded *_facts.json
    facts_by_period: dict[str, Path] = {}
    for jp in json_paths:
        facts = jp.with_name(_logical_stem(jp) + "_facts.json")
        if not facts.exists():
            continue
        # logical stem is "<TICKER>_<YYYY-NT>" → take the period part for parse_period
        period = parse_period(_logical_stem(jp))
        if period:
            facts_by_period[period] = facts

    placed = matched = 0
    for md in sorted(report_dir.glob("*.md")):
        period = parse_period(md.stem)
        if not period or period not in facts_by_period:
            continue
        matched += 1
        dest = md.with_name(md.stem + "_facts.json")
        shutil.copyfile(facts_by_period[period], dest)
        placed += 1
    print(f"{company}: {len(facts_by_period)} XBRL periods, {placed} facts placed "
          f"({matched} reports matched)")
    return placed, matched


def main() -> None:
    argv = sys.argv[1:]
    include_annual = False
    for flag in ("--annual", "--annual-facts"):
        if flag in argv:
            include_annual = True
            argv = [a for a in argv if a != flag]
    companies = discover_companies()
    targets = argv or sorted(companies)
    # One shared index fetch (~4 MB page) instead of one per ticker.
    index = fetch_archive_index()
    failures = 0
    for co in targets:
        if co not in companies:
            print(f"skip unknown: {co} (no configs/{co}.yaml with ir_website.xbrl_ticker)")
            continue
        ticker, report_dir = companies[co]
        try:
            sync(co, ticker, report_dir, index=index, include_annual=include_annual)
        except Exception as exc:  # noqa: BLE001 — one company must not abort the rest
            failures += 1
            print(f"{co}: sync FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
