"""mdna_to_corpus — convert already-downloaded BMV XBRL MD&A HTML into corpus markdown.

The BMV XBRL fetcher (``src/download/bmv_xbrl.py``) writes one ``<TICKER>_<period>_mdna.html``
per filing — the official quarterly MD&A narrative (rich Spanish prose). This script strips
those to plain text and writes ``data/corpus/<slug>/<period>.md`` so the ingest/index pipeline
can pick them up. It is the conversion step that ``load_mdna_text`` could not do for sources
whose MD&A html lives under a ``xbrl/`` subdir with a ``<TICKER>_`` prefix (e.g. FEMSA), which
is why those companies landed in the corpus as empty ``.md`` files.

    python3 scripts/mdna_to_corpus.py --slug femsa \
        --src-dir /Users/.../pdfs/data/reports/femsa/xbrl

Skips thin/boilerplate MD&A (< --min-chars) and prints a per-company coverage line. Idempotent
(overwrites existing markdown). Same text extraction as ``bmv_xbrl.load_mdna_text``
(BeautifulSoup ``get_text("\\n", strip=True)``).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]

# <TICKER>_<period>_mdna.html  or  <period>_mdna.html  ->  capture <period> (e.g. 2021-3T)
_PERIOD_RE = re.compile(r"(?:^|_)(\d{4}-\d[TtAa])_mdna\.html$", re.IGNORECASE)


def period_from_name(name: str) -> str | None:
    m = _PERIOD_RE.search(name)
    return m.group(1).upper() if m else None


def html_to_text(path: Path) -> str:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    return soup.get_text("\n", strip=True)


def convert(slug: str, src_dir: Path, corpus_dir: Path, min_chars: int) -> int:
    out_dir = corpus_dir / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    html_files = sorted(src_dir.glob("*_mdna.html"))
    written = skipped_thin = unparsed = 0
    for hp in html_files:
        period = period_from_name(hp.name)
        if not period:
            print(f"  ?  {hp.name}: cannot parse period — skipped")
            unparsed += 1
            continue
        text = html_to_text(hp)
        if len(text) < min_chars:
            print(f"  ·  {period}: thin MD&A ({len(text)} chars < {min_chars}) — skipped")
            skipped_thin += 1
            continue
        (out_dir / f"{period}.md").write_text(text, encoding="utf-8")
        written += 1
    print(f"[{slug}] {written} written, {skipped_thin} thin-skipped, "
          f"{unparsed} unparsed  (of {len(html_files)} html files) -> {out_dir}")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slug", required=True, help="corpus slug, e.g. femsa")
    ap.add_argument("--src-dir", type=Path, required=True,
                    help="dir containing *_mdna.html files")
    ap.add_argument("--corpus-dir", type=Path, default=ROOT / "data/corpus")
    ap.add_argument("--min-chars", type=int, default=500,
                    help="skip MD&A shorter than this (boilerplate/blank)")
    args = ap.parse_args(argv)
    if not args.src_dir.is_dir():
        print(f"src-dir not found: {args.src_dir}", file=sys.stderr)
        return 2
    n = convert(args.slug, args.src_dir, args.corpus_dir, args.min_chars)
    return 0 if n else 1


if __name__ == "__main__":
    raise SystemExit(main())
