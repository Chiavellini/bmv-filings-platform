#!/usr/bin/env python3
"""Directed fleet refresh used by the Analyst Launchpad Estate node.

The registry decides which issuers and sources are eligible.  This command does
not crawl arbitrary websites: it refreshes configured quarterly sources,
delivers their derivatives to the Estate/Alpha projection, and then syncs the
rights-aware metadata/link news corpus into the USB-native Estate.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

import yaml

from estate_bridge import load_estate_bridge
from estate_volume import inspect_estate_environment


ROOT = Path(__file__).resolve().parents[1]


def _run(label: str, argv: list[str], *, cwd: Path, dry_run: bool) -> None:
    print(f"\n== {label} ==", flush=True)
    print(" ".join(argv), flush=True)
    if dry_run:
        return
    completed = subprocess.run(argv, cwd=cwd, check=False)
    if completed.returncode:
        raise SystemExit(f"{label} falló con código {completed.returncode}.")


def _require_estate():
    bridge = load_estate_bridge(project_root=ROOT)
    volume = inspect_estate_environment(bridge.estate_root)
    problems = list(bridge.validate()) + list(volume.problems)
    if problems:
        raise SystemExit("El Estate no está listo: " + "; ".join(dict.fromkeys(problems)))
    return bridge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", default=[], help="limit to issuer slug/ticker")
    parser.add_argument("--recheck-latest", type=int, default=2)
    parser.add_argument("--skip-news", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    bridge = _require_estate()
    root_cli = ROOT / ".venv" / "bin"
    alpha_root = ROOT / "alpha-go"
    alpha_python = alpha_root / ".venv312" / "bin" / "python"
    for executable in (root_cli / "refresh-quarterly-estate", root_cli / "process-estate-outbox"):
        if not executable.is_file():
            raise SystemExit(f"Falta instalar el ejecutable requerido: {executable}")
    if not alpha_python.is_file():
        raise SystemExit(f"Falta instalar Alpha Go: {alpha_python}")

    quarterly = [
        str(root_cli / "refresh-quarterly-estate"),
        "sync",
        "--apply",
        "--allow-coverage-gaps",
        "--trigger",
        "analyst_console_mass_refresh",
        "--recheck-latest",
        str(args.recheck_latest),
        "--json",
    ]
    for issuer in args.only:
        quarterly.extend(("--only", issuer))
    _run("Documentos trimestrales dirigidos", quarterly, cwd=ROOT, dry_run=args.dry_run)

    target_id = os.environ.get("PDFS_ALPHA_TARGET_ID", "portable-estate-v1")
    delivery = [
        str(root_cli / "process-estate-outbox"),
        "run",
        "--apply",
        "--estate-root",
        str(bridge.estate_root),
        "--database",
        str(bridge.catalog_path),
        "--max-deliveries",
        "10000",
        "--enable-alpha-go",
        "--alpha-root",
        str(alpha_root),
        "--alpha-corpus",
        str(bridge.alpha_go_corpus_dir),
        "--alpha-index",
        str(bridge.alpha_go_index_path),
        "--alpha-config",
        str(alpha_root / "configs" / "alpha_go.yaml"),
        "--alpha-python",
        str(alpha_python),
        "--alpha-target-id",
        target_id,
        "--json",
    ]
    _run("Derivados e índice Alpha", delivery, cwd=ROOT, dry_run=args.dry_run)

    if not args.skip_news:
        news_root = bridge.estate_root / "news"
        news_catalog = news_root / "catalog.db"
        news_corpus = news_root / "corpus"
        news = [
            str(alpha_python),
            "scripts/sync_news_gdelt.py",
            "--apply",
            "--db",
            str(bridge.alpha_go_index_path),
            "--catalog",
            str(news_catalog),
            "--corpus-dir",
            str(news_corpus),
        ]
        if args.only:
            news.extend(("--companies", ",".join(args.only)))
        _run("Noticias dirigidas (GDELT, metadatos y enlaces)", news, cwd=alpha_root, dry_run=args.dry_run)

        news_cfg = yaml.safe_load((alpha_root / "configs" / "news.yaml").read_text(encoding="utf-8")) or {}
        configured_news = news_cfg.get("news") or {}
        if (configured_news.get("google_news") or {}).get("enabled", False):
            google = [
                str(alpha_python),
                "scripts/sync_news_google.py",
                "--apply",
                "--db",
                str(bridge.alpha_go_index_path),
                "--catalog",
                str(news_catalog),
                "--corpus-dir",
                str(news_corpus),
            ]
            if args.only:
                google.extend(("--companies", ",".join(args.only)))
            _run(
                "Noticias dirigidas (Google News RSS, metadatos y enlaces)",
                google,
                cwd=alpha_root,
                dry_run=args.dry_run,
            )

        approved_feeds = configured_news.get("sources") or []
        if approved_feeds:
            rss = [
                str(alpha_python),
                "scripts/sync_news_rss.py",
                "--apply",
                "--db",
                str(bridge.alpha_go_index_path),
                "--catalog",
                str(news_catalog),
                "--corpus-dir",
                str(news_corpus),
            ]
            _run("Fuentes RSS aprobadas", rss, cwd=alpha_root, dry_run=args.dry_run)

    print("\nActualización dirigida del Estate terminada.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
