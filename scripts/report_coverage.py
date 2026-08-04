#!/usr/bin/env python3
"""report_coverage.py — what can we actually extract for this company, and from what?

Answers the question `/onboard-company` stage 2 has always asked in prose and
never enforced: *do we have the periods this deliverable needs?* Prints one row
per expected quarter with the source backing it, and **exits non-zero** when
coverage falls below the threshold — so a missing quarter stops a build instead
of silently shrinking the workbook.

Sources, strongest first:
  pdf    the issuer's own earnings-release PDF, parsed to Markdown
  mdna   narrative rendered from the BMV XBRL filing's MD&A text blocks
  facts  tagged IFRS concepts only, no narrative
  —      nothing at all

Expected periods run from the floor year (or the issuer's listing quarter, when
the registry knows it) through the last completed quarter.

Usage:
    python3 scripts/report_coverage.py <slug>
    python3 scripts/report_coverage.py <slug> --min-coverage 0.95
    python3 scripts/report_coverage.py <slug> --json coverage.json
    python3 scripts/report_coverage.py --all --min-coverage 0
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.fetch_company_reports import _expected_periods, START_FLOOR_YEAR  # noqa: E402
from src.download.xbrl_corpus import (  # noqa: E402
    SOURCE_FACTS,
    SOURCE_MDNA,
    SOURCE_PDF,
    load_provenance,
)
from src.shared.company_source import (  # noqa: E402
    UnknownCompanyError,
    resolve_company_source,
)
from src.shared.paths import (  # noqa: E402
    REPORTS_DIR,
    SHARED_PARSED_REPORTS_DIR,
    SHARED_REPORTS_DIR,
)
from src.shared.report_index import infer_period_label, period_sort_key  # noqa: E402

MISSING = "—"
#: A report PDF is on disk but was never parsed to Markdown. The build can still
#: parse it, but certification globs ``*.md`` and would score the period zero —
#: which is exactly how `gruma` (41 PDFs, 0 Markdown) looked fully covered while
#: certifying at 0%. Fixed by re-running the fetcher with ``--parse``.
SOURCE_PDF_RAW = "pdf-raw"
#: Sources that carry narrative text the prose/search tiers can read *today*.
_NARRATIVE = {SOURCE_PDF, SOURCE_MDNA}
#: Strongest first.
_RANK = {SOURCE_PDF: 0, SOURCE_MDNA: 1, SOURCE_PDF_RAW: 2, SOURCE_FACTS: 3}


@dataclass
class PeriodCoverage:
    period: str
    source: str

    @property
    def ok(self) -> bool:
        return self.source != MISSING

    @property
    def has_narrative(self) -> bool:
        return self.source in _NARRATIVE


def _corpus_dirs(slug: str) -> list[Path]:
    """The same zero-copy union ``build_segments._report_sources`` reads."""
    candidates = (
        SHARED_REPORTS_DIR / slug,
        REPORTS_DIR / slug,
        SHARED_PARSED_REPORTS_DIR / slug,
    )
    return [path for path in candidates if path.is_dir()]


def _observed_sources(slug: str) -> dict[str, str]:
    """Period → strongest available source across the whole corpus union."""
    found: dict[str, str] = {}

    def offer(period: str | None, source: str) -> None:
        if not period:
            return
        current = found.get(period)
        if current is None or _RANK[source] < _RANK[current]:
            found[period] = source

    for directory in _corpus_dirs(slug):
        markdown_periods = {
            period
            for period in (infer_period_label(p.stem) for p in directory.glob("*.md"))
            if period
        }

        # provenance.json is authoritative on *origin* — only it distinguishes a
        # PDF-derived .md from an MD&A-derived one. It says nothing about
        # whether the Markdown was ever produced, so demote a claimed `pdf`
        # period that has no Markdown on disk.
        for period, entry in load_provenance(directory).items():
            if entry.source == SOURCE_PDF and period not in markdown_periods:
                offer(period, SOURCE_PDF_RAW)
            elif entry.source in _RANK:
                offer(period, entry.source)

        for pdf in directory.glob("*.pdf"):
            period = infer_period_label(pdf.stem)
            offer(period, SOURCE_PDF if period in markdown_periods else SOURCE_PDF_RAW)
        for period in markdown_periods:
            # A .md with no provenance entry: parsed report if a PDF sits beside
            # it, otherwise MD&A-derived.
            if period not in found:
                sibling = directory / f"{period}.pdf"
                offer(period, SOURCE_PDF if sibling.exists() else SOURCE_MDNA)
        for facts in directory.glob("*_facts.json"):
            offer(infer_period_label(facts.stem), SOURCE_FACTS)

    return found


def coverage_for(slug: str, *, floor_year: int | None = None) -> list[PeriodCoverage]:
    source = resolve_company_source(slug)
    effective_floor = floor_year or source.floor_year or START_FLOOR_YEAR
    expected = _expected_periods(effective_floor, source.coverage_from_period)
    observed = _observed_sources(slug)
    return [
        PeriodCoverage(period=period, source=observed.get(period, MISSING))
        for period in sorted(expected, key=period_sort_key)
    ]


def _summarize(rows: list[PeriodCoverage]) -> dict:
    total = len(rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.source] = counts.get(row.source, 0) + 1
    covered = sum(1 for row in rows if row.ok)
    narrative = sum(1 for row in rows if row.has_narrative)
    return {
        "expected": total,
        "covered": covered,
        "narrative": narrative,
        "missing": [row.period for row in rows if not row.ok],
        "coverage": (covered / total) if total else 0.0,
        "narrative_coverage": (narrative / total) if total else 0.0,
        "by_source": counts,
    }


def _print_report(slug: str, rows: list[PeriodCoverage], summary: dict) -> None:
    print(f"\n{slug} — {summary['covered']}/{summary['expected']} periods covered "
          f"({summary['coverage']:.0%}), {summary['narrative']} with narrative text "
          f"({summary['narrative_coverage']:.0%})")
    print(f"{'period':<10}{'source':<8}status")
    print("-" * 32)
    for row in rows:
        status = "ok" if row.ok else "MISSING"
        print(f"{row.period:<10}{row.source:<8}{status}")
    breakdown = ", ".join(
        f"{count} {source}" for source, count in sorted(summary["by_source"].items())
    )
    print(f"\nby source: {breakdown or 'nothing'}")
    unparsed = summary["by_source"].get(SOURCE_PDF_RAW, 0)
    if unparsed:
        print(
            f"note: {unparsed} period(s) have a report PDF but no parsed Markdown. "
            f"Certification globs *.md and would score them zero — run "
            f"`python3 scripts/fetch_company_reports.py {slug} --parse`."
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slugs", nargs="*", help="company slug(s)")
    ap.add_argument("--all", action="store_true", help="every company dir under data/reports/")
    ap.add_argument(
        "--min-coverage",
        type=float,
        default=0.9,
        help="fail below this fraction of expected periods (default 0.9; 0 disables)",
    )
    ap.add_argument(
        "--require-narrative",
        action="store_true",
        help="count only periods with narrative text (pdf/mdna) toward the threshold",
    )
    ap.add_argument("--json", metavar="FILE", help="also write the full report as JSON")
    ap.add_argument("--quiet", action="store_true", help="summary lines only")
    args = ap.parse_args(argv)

    slugs = (
        sorted(p.name for p in REPORTS_DIR.iterdir() if p.is_dir())
        if args.all
        else list(args.slugs)
    )
    if not slugs:
        ap.error("give at least one slug, or --all")

    payload: dict[str, dict] = {}
    failures: list[str] = []
    for slug in slugs:
        try:
            rows = coverage_for(slug)
        except UnknownCompanyError as exc:
            print(f"{slug}: {exc}", file=sys.stderr)
            failures.append(slug)
            continue
        summary = _summarize(rows)
        payload[slug] = {
            "summary": summary,
            "periods": {row.period: row.source for row in rows},
        }
        measured = (
            summary["narrative_coverage"] if args.require_narrative else summary["coverage"]
        )
        passed = measured >= args.min_coverage
        if not passed:
            failures.append(slug)
        if args.quiet or args.all:
            flag = "ok " if passed else "FAIL"
            print(
                f"{flag} {slug:<22} {summary['covered']:>3}/{summary['expected']:<3} "
                f"({summary['coverage']:.0%})  narrative {summary['narrative']:>3} "
                f"({summary['narrative_coverage']:.0%})"
            )
        else:
            _print_report(slug, rows, summary)
            if not passed:
                metric = "narrative coverage" if args.require_narrative else "coverage"
                print(
                    f"\nFAIL {slug}: {metric} {measured:.0%} is below the "
                    f"{args.min_coverage:.0%} threshold; "
                    f"{len(summary['missing'])} period(s) missing.",
                    file=sys.stderr,
                )

    if args.json:
        Path(args.json).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
