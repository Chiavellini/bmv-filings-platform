#!/usr/bin/env python3
"""Run the root deployment preflight without changing the estate."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.acquisition.registry import DEFAULT_REGISTRY_PATH  # noqa: E402
from src.deployment.preflight import (  # noqa: E402
    PreflightMode,
    first_environment_path,
    format_human,
    run_preflight,
)
from src.shared.paths import DOCUMENT_ESTATE_DIR  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only deployment diagnostics. This command never migrates or "
            "writes the estate."
        )
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=[mode.value for mode in PreflightMode],
        default=PreflightMode.AUDIT.value,
        help="policy target (default: audit)",
    )
    parser.add_argument(
        "--registry",
        default=os.environ.get("PDFS_ISSUER_REGISTRY", str(DEFAULT_REGISTRY_PATH)),
        help="canonical issuer registry YAML",
    )
    parser.add_argument(
        "--estate-root",
        default=str(DOCUMENT_ESTATE_DIR),
        help="shared document-estate root",
    )
    parser.add_argument(
        "--database",
        help="estate SQLite catalog (default: <estate-root>/catalog.db)",
    )
    parser.add_argument(
        "--alpha-index",
        "--alpha-index-path",
        dest="alpha_index",
        default=first_environment_path(
            os.environ,
            (
                "PDFS_ALPHA_INDEX_PATH",
                "ALPHA_GO_INDEX_PATH",
                "ALPHA_GO_INDEX_DB",
            ),
        ),
        help="optional Alpha Go index file",
    )
    parser.add_argument(
        "--alpha-corpus",
        "--alpha-corpus-path",
        dest="alpha_corpus",
        default=first_environment_path(
            os.environ,
            (
                "PDFS_ALPHA_CORPUS_PATH",
                "ALPHA_GO_CORPUS_PATH",
                "ALPHA_GO_CORPUS_DIR",
            ),
        ),
        help="optional Alpha Go corpus directory",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a stable JSON report instead of human output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_preflight(
        mode=args.mode,
        registry_path=args.registry,
        estate_root=args.estate_root,
        database_path=args.database,
        alpha_index_path=args.alpha_index,
        alpha_corpus_path=args.alpha_corpus,
    )
    print(report.to_json() if args.json else format_human(report))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
