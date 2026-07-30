#!/usr/bin/env python3
"""Fast, page-aligned PDF-to-Markdown conversion for document-search ingestion.

This intentionally uses Poppler rather than the slower character-level financial-table parser.
It is appropriate when the deliverable is faithful Ctrl+F-style search text and page snippets;
the original PDF remains the source of truth.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.parse.parse_pdf import tidy
from src.shared.report_index import infer_period_label, period_sort_key


def parse_for_search(pdf: Path) -> str:
    completed = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    pages = completed.stdout.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    parts = [f"# {pdf.name}\n"]
    for number, raw in enumerate(pages, 1):
        parts.append(f"\n\n===== Página {number} =====\n\n")
        text = tidy(raw)
        if text:
            parts.append(text + "\n")
        else:
            parts.append(
                f"<!-- página {number} sin texto: posible escaneo/imagen (requiere OCR) -->\n"
            )
    return "".join(parts)


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_dir", type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    pdfs = sorted(
        (
            path for path in args.report_dir.glob("*.pdf")
            if infer_period_label(path.stem)
        ),
        key=lambda path: period_sort_key(infer_period_label(path.stem) or path.stem),
    )
    written = skipped = failed = 0
    for pdf in pdfs:
        destination = pdf.with_suffix(".md")
        if destination.exists() and not args.force:
            skipped += 1
            continue
        try:
            _atomic_write(destination, parse_for_search(pdf))
            written += 1
            print(f"[{written + skipped}/{len(pdfs)}] {destination.name}", flush=True)
        except Exception as exc:
            failed += 1
            print(f"FAILED {pdf.name}: {type(exc).__name__}: {exc}", flush=True)
    print(f"written={written} skipped={skipped} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
