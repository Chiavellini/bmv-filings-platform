#!/usr/bin/env python3
"""Re-parse the existing corpus from the parent repo's cached source PDFs.

The alpha-go corpus was built from pre-existing ``.md`` files, so every manifest entry has
``pdf_path=null`` and the markdown predates the table-reconstruction parser. The source PDFs still
live in the parent cache ``<repo>/data/reports/<slug>/``, mapped 1:1 to each corpus doc by filename
stem (``walmex`` 49/49, ``bimbo`` 41/41). This script re-parses those PDFs with the current
``parse_pdf`` (readable pipe tables), overwrites each ``.md`` in place, and populates the previously
null ``pdf_path`` — without touching the shared download-oriented ingest path.

``femsa`` is skipped: it is sourced from SEC EDGAR (HTML → markdown), has no PDFs, and its parse
path is unrelated to table reconstruction.

Run ``scripts/build_index.py`` afterwards to rebuild chunks / FTS / embeddings from the new markdown.

    python3 scripts/reparse_local.py            # re-parse walmex + bimbo
    python3 scripts/reparse_local.py --dry-run  # report what would change, write nothing
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.corpus.manifest import load_manifest, save_manifest  # noqa: E402
from src.shared.paths import DATA_DIR, PROJECT_ROOT  # noqa: E402

# Source PDFs live in the PARENT repo's report cache, e.g. <repo>/data/reports/bimbo/2022-2T.pdf.
PARENT_REPORTS = PROJECT_ROOT.parent / "data" / "reports"
DEFAULT_COMPANIES = ("walmex", "bimbo")


def _pdf_for(company: str, markdown_path: str) -> Path:
    """The cached source PDF for a corpus doc: same filename stem, under the parent report cache."""
    return PARENT_REPORTS / company / f"{Path(markdown_path).stem}.pdf"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--companies", nargs="+", default=list(DEFAULT_COMPANIES),
                    help="slugs to re-parse (default: walmex bimbo)")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = ap.parse_args()

    from src.parse.parse_pdf import parse_pdf  # lazy (pdfplumber)

    corpus_dir = DATA_DIR / "corpus"
    manifest = load_manifest(corpus_dir)
    targets = [d for d in manifest.documents if d.company in set(args.companies)]
    if not targets:
        print(f"No documents for companies {args.companies} in {corpus_dir}", file=sys.stderr)
        return 1

    reparsed = missing = failed = 0
    for d in targets:
        pdf = _pdf_for(d.company, d.markdown_path)
        if not pdf.exists():
            print(f"  MISSING pdf  {d.doc_id}  (looked for {pdf})")
            missing += 1
            continue
        try:
            markdown, _blocks, meta = parse_pdf(pdf, with_meta=True)
        except Exception as exc:  # noqa: BLE001 — report and keep going
            print(f"  FAILED       {d.doc_id}: {exc}")
            failed += 1
            continue
        if not args.dry_run:
            Path(d.markdown_path).write_text(markdown, encoding="utf-8")
            d.pdf_path = str(pdf)
            d.extra = {"scale": meta.scale, "sections": [name for name, _ in meta.sections]}
        reparsed += 1
        print(f"  {'(dry) ' if args.dry_run else ''}reparsed   {d.doc_id}  "
              f"({len(markdown):,} chars) <- {pdf.name}")

    if not args.dry_run:
        save_manifest(manifest, corpus_dir)

    skipped = [d.company for d in manifest.documents if d.company not in set(args.companies)]
    print(f"\nreparsed={reparsed}  missing={missing}  failed={failed}  "
          f"untouched={len(skipped)} docs ({sorted(set(skipped))})")
    print("Next: python3 scripts/build_index.py --config configs/alpha_go.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
