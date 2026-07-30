#!/usr/bin/env python3
"""Emit the complete primary-PDF source/readiness matrix without network access."""

from __future__ import annotations

import argparse
import csv
from dataclasses import fields
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.acquisition.readiness import (  # noqa: E402
    PrimaryPdfReadinessRow,
    build_primary_pdf_readiness,
    summarize_primary_pdf_readiness,
)
from src.acquisition.registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    load_issuer_registry,
)
from src.acquisition.service import EstateCoverage  # noqa: E402
from src.shared.paths import (  # noqa: E402
    DOCUMENT_ESTATE_DB,
    DOCUMENT_ESTATE_DIR,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--database", type=Path, default=DOCUMENT_ESTATE_DB)
    parser.add_argument(
        "--estate-root",
        type=Path,
        default=DOCUMENT_ESTATE_DIR,
        help=(
            "root used to resolve portable artifact, blob, and object-key paths"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a summary and all rows as JSON instead of CSV",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write the report to this path instead of stdout",
    )
    return parser


def _csv_text(rows: tuple[PrimaryPdfReadinessRow, ...]) -> str:
    from io import StringIO

    stream = StringIO()
    names = [field.name for field in fields(PrimaryPdfReadinessRow)]
    writer = csv.DictWriter(stream, fieldnames=names, lineterminator="\n")
    writer.writeheader()
    writer.writerows(row.to_dict() for row in rows)
    return stream.getvalue()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    registry = load_issuer_registry(args.registry)
    rows = build_primary_pdf_readiness(
        registry,
        EstateCoverage(args.database, estate_root=args.estate_root),
    )
    if args.json:
        text = json.dumps(
            {
                "schema_version": 1,
                "summary": summarize_primary_pdf_readiness(rows),
                "rows": [row.to_dict() for row in rows],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        suffix = "\n"
    else:
        text = _csv_text(rows)
        suffix = ""
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + suffix, encoding="utf-8")
    else:
        print(text, end=suffix or "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
