"""onboard_edgar — fetch a SEC issuer's English earnings releases into the corpus.

For each 6-K with an EX-99.1 earnings release, writes ``data/corpus/<slug>/<period>.md`` from the
release text. Built for FEMSA (English 6-K filer) but works for any foreign issuer that files
quarterly results as a 6-K EX-99.1.

    python3 scripts/onboard_edgar.py --slug femsa --cik 1061736 --since-year 2016

Network: SEC EDGAR (works headlessly; no Playwright). Idempotent — overwrites existing periods,
keeps the newest filing per period. Prints a per-company coverage line; never ships empty docs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.download.edgar import (
    annual_period_label,
    fetch_document_html,
    filing_documents,
    html_to_text,
    iter_filings,
    parse_period_label,
    select_annual_document,
    select_earnings_exhibit,
)

_MIN_CHARS = 20000  # a real EX-99.1 results release runs 60k+ chars; this rejects short 6-K
                    # press releases that merely mention a quarter (false period matches)
_MIN_CHARS_ANNUAL = 40000  # a 20-F body is very long; guards against picking a stub document


def onboard(slug: str, cik: str, corpus_dir: Path, since_year: int, max_filings: int | None,
            *, kind: str = "quarterly") -> int:
    out_dir = corpus_dir / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    # Quarterly = 6-K EX-99.1 earnings release; annual = 20-F filing body (YYYY-FY).
    if kind == "annual":
        forms, select_fn, min_chars = ("20-F",), select_annual_document, _MIN_CHARS_ANNUAL
        period_fn = annual_period_label
    else:
        forms, select_fn, min_chars = ("6-K",), select_earnings_exhibit, _MIN_CHARS
        period_fn = lambda text, _fdate: parse_period_label(text)  # noqa: E731
    filings = [f for f in iter_filings(cik, forms=forms)
               if f.filing_date[:4].isdigit() and int(f.filing_date[:4]) >= since_year]
    filings.sort(key=lambda f: f.filing_date, reverse=True)   # newest first → newest wins per period
    print(f"[{slug}] scanning {len(filings)} {'/'.join(forms)} filings since {since_year}…",
          file=sys.stderr)

    written: dict[str, str] = {}   # period -> filing_date (kept)
    scanned = 0
    for f in filings:
        if max_filings and scanned >= max_filings:
            break
        scanned += 1
        try:
            docs = filing_documents(cik, f.accession)
            exhibit = select_fn(docs)
            if not exhibit:
                continue
            html = fetch_document_html(cik, f.accession, exhibit)
            # Keep the original SEC exhibit next to its indexed text so the local reader can
            # always render the source filing, even after moving the corpus offline.
            text = fetch_document_text(cik, f.accession, exhibit) if not html else html_to_text(html)
        except Exception as exc:  # noqa: BLE001 — one bad filing must not abort the run
            print(f"  ! {f.accession}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        period = period_fn(text, f.filing_date)
        if not period or len(text) < min_chars:
            continue
        if period in written:        # already have a (newer) filing for this period
            continue
        (out_dir / f"{period}.md").write_text(text, encoding="utf-8")
        (out_dir / f"{period}.html").write_text(html, encoding="utf-8")
        written[period] = f.filing_date
        print(f"  + {period}  ({len(text):,} chars, filed {f.filing_date}, {exhibit})", file=sys.stderr)

    periods = sorted(written)
    print(f"[{slug}] {len(periods)} periods written -> {out_dir}"
          + (f"  [{periods[0]}..{periods[-1]}]" if periods else ""))
    return len(periods)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--cik", required=True, help="SEC CIK (e.g. 1061736 for FEMSA)")
    ap.add_argument("--corpus-dir", type=Path, default=ROOT / "data/corpus")
    ap.add_argument("--since-year", type=int, default=2016)
    ap.add_argument("--max-filings", type=int, default=None, help="cap filings scanned (testing)")
    ap.add_argument("--annual", action="store_true",
                    help="fetch annual 20-F filings (YYYY-FY) instead of quarterly 6-K releases")
    args = ap.parse_args(argv)
    n = onboard(args.slug, args.cik, args.corpus_dir, args.since_year, args.max_filings,
                kind="annual" if args.annual else "quarterly")
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
