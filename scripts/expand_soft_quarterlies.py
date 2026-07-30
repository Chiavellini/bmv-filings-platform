#!/usr/bin/env python3
"""Fill archive-addressable quarterly gaps in Soft's company universe.

The planner compares the shared document estate with Soft's company configs and the cached
BMV XBRL archive page.  It downloads only missing periods for companies below the requested
floor, making the operation resumable and avoiding rewrites of already-consolidated artifacts.

Run from the monorepo root:

    python3 scripts/expand_soft_quarterlies.py
    python3 scripts/expand_soft_quarterlies.py --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SOFT_ROOT = ROOT / "soft"

# Root, soft/, alpha-go/, and earnings/vendor/alpha-go/ each provide a package
# named ``src``; whichever is first on sys.path claims the name process-wide.
#
# This script used to insert SOFT_ROOT at position 0, so ``from src.download...``
# below silently resolved to *Soft's* vendored copy of the BMV XBRL engine even
# though the script lives in root/scripts and writes into the root estate. Soft's
# copy is an older fork: it writes ``<name>.tmp`` non-atomically and gzips with a
# live mtime, so the same filing fetched twice produces different bytes and
# therefore a different content hash. Root's copy writes to a uuid-suffixed temp
# file and pins ``mtime=0`` precisely so content-addressing is stable.
#
# Root is now unambiguously first. Only root paths are used to resolve ``src``;
# SOFT_ROOT is data input (its archive cache and reports), never a code source.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from estate_bridge import load_estate_bridge  # noqa: E402
from src.download.bmv_xbrl import (  # noqa: E402
    XbrlFiling,
    download_ticker,
    extract_artifacts,
    parse_archive_index,
)

ESTATE_BRIDGE = load_estate_bridge(project_root=ROOT)
DEFAULT_ESTATE = ESTATE_BRIDGE.catalog_path
DEFAULT_ARCHIVE = SOFT_ROOT / "data" / "cache" / "bmv_archive.html"
DEFAULT_REPORTS = SOFT_ROOT / "data" / "reports"
DEFAULT_ALPHA_COMPANIES = ROOT / "alpha-go" / "configs" / "bmv_corpus.yaml"
DEFAULT_REPORT = (
    ESTATE_BRIDGE.estate_root / "expansion" / "soft_xbrl_backfill.json"
)


def _company_tickers(config_root: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for path in sorted(config_root.glob("*.yaml")):
        if path.name.startswith(("_", "generic", "new_company")):
            continue
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        ticker = ((config.get("ir_website") or {}).get("xbrl_ticker") or "").strip()
        if ticker:
            rows[path.stem] = ticker
    return rows


def _existing_periods(estate_path: Path) -> dict[str, set[str]]:
    conn = sqlite3.connect(f"file:{estate_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT DISTINCT m.company,d.period
        FROM memberships m JOIN documents d ON d.document_id=m.document_id
        WHERE d.period GLOB '[0-9][0-9][0-9][0-9]-[1-4]T'
    """).fetchall()
    conn.close()
    periods: dict[str, set[str]] = {}
    for row in rows:
        periods.setdefault(row["company"], set()).add(row["period"])
    return periods


def _alpha_ticker_slugs(path: Path) -> dict[str, set[str]]:
    if not path.is_file():
        return {}
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    aliases: dict[str, set[str]] = {}
    for row in config.get("companies", []) or []:
        ticker = str(row.get("ticker") or "").strip().upper()
        slug = str(row.get("slug") or "").strip()
        if ticker and slug:
            aliases.setdefault(ticker, set()).add(slug)
    return aliases


def build_worklist(
    estate_path: Path,
    archive_path: Path,
    config_root: Path,
    *,
    floor: int,
    alpha_companies: Path = DEFAULT_ALPHA_COMPANIES,
    all_gaps: bool = False,
) -> list[dict]:
    tickers = _company_tickers(config_root)
    existing = _existing_periods(estate_path)
    alpha_aliases = _alpha_ticker_slugs(alpha_companies)
    filings = parse_archive_index(archive_path.read_text(encoding="utf-8"))
    available: dict[str, dict[str, XbrlFiling]] = {}
    for filing in filings:
        if filing.kind == "quarterly":
            available.setdefault(filing.ticker.upper(), {})[filing.period] = filing

    worklist: list[dict] = []
    for slug, ticker in sorted(tickers.items()):
        company_slugs = {slug} | alpha_aliases.get(ticker.upper(), set())
        have = set().union(*(existing.get(alias, set()) for alias in company_slugs))
        if len(have) >= floor:
            continue
        missing = [
            filing
            for period, filing in available.get(ticker.upper(), {}).items()
            if period not in have
        ]
        # Add the newest available periods first if the archive contains more than needed.
        missing.sort(key=lambda filing: filing.period, reverse=True)
        needed = max(0, floor - len(have))
        selected = missing if all_gaps else missing[:needed]
        for filing in selected:
            worklist.append({
                "slug": slug,
                "ticker": ticker,
                "existing_quarters": len(have),
                "floor": floor,
                "filing": asdict(filing),
            })
    return worklist


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--estate", type=Path, default=DEFAULT_ESTATE)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--reports", type=Path, default=DEFAULT_REPORTS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--floor", type=int, default=20)
    parser.add_argument(
        "--all-gaps", action="store_true",
        help="for below-floor companies, fetch every archive-addressable missing quarter",
    )
    parser.add_argument("--only", help="comma-separated Soft slugs")
    parser.add_argument("--delay-ms", type=int, default=250)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)

    worklist = build_worklist(
        args.estate.resolve(), args.archive.resolve(), SOFT_ROOT / "configs",
        floor=args.floor,
        all_gaps=args.all_gaps,
    )
    if args.only:
        selected = {value.strip() for value in args.only.split(",") if value.strip()}
        worklist = [row for row in worklist if row["slug"] in selected]

    companies = sorted({row["slug"] for row in worklist})
    print(
        f"Archive-addressable gaps: {len(worklist)} quarters across "
        f"{len(companies)} companies"
    )
    for slug in companies:
        periods = [
            row["filing"]["period"] for row in worklist if row["slug"] == slug
        ]
        print(f"  {slug}: {', '.join(periods)}")

    successes: list[dict] = []
    failures: list[dict] = []
    if args.apply:
        for number, row in enumerate(worklist, 1):
            filing = XbrlFiling(**row["filing"])
            destination = args.reports / row["slug"]
            print(
                f"[{number}/{len(worklist)}] {row['slug']} {filing.period}",
                flush=True,
            )
            try:
                saved = download_ticker(
                    row["ticker"],
                    destination,
                    index=[filing],
                    delay_ms=args.delay_ms,
                    write_artifacts=False,
                )
                if not saved:
                    raise RuntimeError("BMV archive returned no usable filing")
                artifacts = extract_artifacts(saved[0])
                successes.append({
                    **row,
                    "raw_path": str(saved[0].resolve()),
                    "artifacts": {
                        name: str(path.resolve()) for name, path in artifacts.items()
                    },
                })
            except Exception as exc:  # keep the resumable batch moving
                failures.append({
                    **row,
                    "error": f"{type(exc).__name__}: {exc}",
                })

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "applied": args.apply,
        "floor": args.floor,
        "planned_quarters": len(worklist),
        "planned_companies": len(companies),
        "successful_quarters": len(successes),
        "failed_quarters": len(failures),
        "successes": successes,
        "failures": failures,
        "worklist": worklist,
    }
    _write_report(args.report, payload)
    print(
        f"Result: successful={len(successes)} failed={len(failures)} "
        f"report={args.report}"
    )
    return 1 if args.apply and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
