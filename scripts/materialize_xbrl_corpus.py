#!/usr/bin/env python3
"""materialize_xbrl_corpus.py — make downloaded BMV XBRL filings extractable.

For each company, links every filing's tagged facts to the canonical
``data/reports/<slug>/<period>_facts.json`` and renders its MD&A narrative to
``<period>.md``, then records each period's real source in ``provenance.json``.

Runs entirely offline against what is already on disk. Periods that already
have a report PDF are left alone — the PDF parse owns their Markdown and is the
better source.

Usage:
    python3 scripts/materialize_xbrl_corpus.py <slug> [<slug> ...]
    python3 scripts/materialize_xbrl_corpus.py --all
    python3 scripts/materialize_xbrl_corpus.py <slug> --overwrite
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.download.xbrl_corpus import materialize_xbrl_corpus  # noqa: E402
from src.shared.paths import REPORTS_DIR  # noqa: E402


def _known_slugs() -> list[str]:
    if not REPORTS_DIR.is_dir():
        return []
    return sorted(p.name for p in REPORTS_DIR.iterdir() if p.is_dir())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slugs", nargs="*", help="company slug(s) under data/reports/")
    ap.add_argument("--all", action="store_true", help="every company dir under data/reports/")
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="re-render Markdown for periods that already have it (never touches PDF-backed periods)",
    )
    ap.add_argument(
        "--fill-gaps",
        action="store_true",
        help=(
            "also extend a corpus that already has reports. Off by default: adding "
            "previously-invisible facts/MD&A adds uncertified observations and moves "
            "pinned regression baselines. Re-certify the company afterwards."
        ),
    )
    args = ap.parse_args(argv)

    slugs = _known_slugs() if args.all else list(args.slugs)
    if not slugs:
        ap.error("give at least one slug, or --all")

    total_md = total_facts = 0
    for slug in slugs:
        report_dir = REPORTS_DIR / slug
        if not report_dir.is_dir():
            print(f"{slug}: no directory at {report_dir}", file=sys.stderr)
            continue
        result = materialize_xbrl_corpus(
            report_dir,
            overwrite=args.overwrite,
            fill_gaps=True if args.fill_gaps else None,
        )
        counts: dict[str, int] = {}
        for entry in result.provenance.values():
            counts[entry.source] = counts.get(entry.source, 0) + 1
        breakdown = ", ".join(f"{n} {src}" for src, n in sorted(counts.items())) or "nothing"
        print(
            f"{slug}: +{len(result.markdown_written)} markdown, "
            f"+{len(result.facts_linked)} facts  ({len(result.provenance)} period(s): {breakdown})"
        )
        total_md += len(result.markdown_written)
        total_facts += len(result.facts_linked)

    if len(slugs) > 1:
        print(f"\ntotal: +{total_md} markdown, +{total_facts} facts across {len(slugs)} company dir(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
