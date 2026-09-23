#!/usr/bin/env python3
"""Launchpad entry point for "Generar una hoja de segmentos".

``scripts/build_segments.py`` publishes to ``outputs/latest/`` only with a
strict, successful quarterly-sync receipt for the issuer from the last 24
hours. The launchpad's fleet refresh runs with ``--allow-coverage-gaps``, so
its receipts never satisfy that gate, and an analyst at the launchpad has no
other way to produce one. This wrapper closes that gap without loosening the
publication contract:

1. If the issuer is already fresh, skip straight to the build. A strict sync
   that then failed would supersede the earlier success and *remove* the
   freshness proof.
2. Otherwise run one strict, directed sync for the issuer
   (``refresh-quarterly-estate sync --only <slug> --apply``).
3. Build. When freshness is proven, publication proceeds exactly as before.
   When it is still unproven, and freshness is the *only* publication blocker,
   the workbook is handed back as a clearly labelled review candidate:
   ``outputs/latest/`` and its receipt are untouched, and the build manifest
   records the missing receipt as it always has.

A machine-readable result is written for the launchpad (``--result``). Any
other blocker still fails the job, with a Spanish ``ERROR:`` line the
launchpad shows verbatim.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FRESHNESS_HOURS = 24.0
SYNC_TIMEOUT_SECONDS = 30 * 60
FRESHNESS_BLOCKER_PREFIX = "canonical estate freshness is unproven"


def _freshness(slug: str):
    from src.acquisition.service import check_quarterly_sync_freshness
    from src.shared.paths import DOCUMENT_ESTATE_DB

    return check_quarterly_sync_freshness(DOCUMENT_ESTATE_DB, slug, FRESHNESS_HOURS * 3600)


def _estate_connected() -> bool:
    """Only sync into a mounted, identity-checked Estate, never a bare path."""
    from estate_volume import inspect_estate_environment
    from src.shared.paths import DOCUMENT_ESTATE_DB, DOCUMENT_ESTATE_DIR

    return DOCUMENT_ESTATE_DB.is_file() and inspect_estate_environment(DOCUMENT_ESTATE_DIR).healthy


def _sync_issuer(slug: str) -> bool:
    """One strict directed sync. Returns True when it succeeded."""
    cli = Path(sys.executable).with_name("refresh-quarterly-estate")
    argv = [
        str(cli), "sync", "--apply", "--only", slug,
        "--trigger", "analyst_console_segments", "--json",
    ]
    print(f"[0/5] Sincronizando {slug} con sus fuentes oficiales…", flush=True)
    try:
        completed = subprocess.run(
            argv, cwd=ROOT, check=False, timeout=SYNC_TIMEOUT_SECONDS,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"      sincronización no completada: {type(exc).__name__}: {exc}", flush=True)
        return False
    tail = [line for line in completed.stdout.splitlines() if line.strip()][-5:]
    for line in tail:
        print(f"      {line[:300]}", flush=True)
    print(f"      sincronización terminó con código {completed.returncode}", flush=True)
    return completed.returncode == 0


def _write_result(path: Path | None, payload: dict) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--analyst-metrics", type=Path, required=True)
    parser.add_argument("--analyst-company", default=None)
    parser.add_argument("--result", type=Path, default=None)
    parser.add_argument(
        "--no-sync", action="store_true",
        help="never run the directed sync (offline use and tests)",
    )
    args = parser.parse_args(argv)

    from scripts import build_segments

    before = _freshness(args.slug)
    synced: bool | None = None
    if not before.fresh and not args.no_sync:
        if _estate_connected():
            synced = _sync_issuer(args.slug)
        else:
            print("[0/5] El USB Estate no está conectado; se omite la sincronización.", flush=True)
    fresh = before.fresh or (synced is True and _freshness(args.slug).fresh)
    if not fresh:
        print(
            "      ⚠ frescura del Estate no verificada: la hoja se entregará como "
            "candidato para revisión, sin reemplazar la versión publicada.",
            flush=True,
        )

    try:
        result = build_segments.run(
            args.input,
            estate_only=True,
            require_fresh_estate=fresh,
            freshness_hours=FRESHNESS_HOURS,
            analyst_metrics=args.analyst_metrics,
            analyst_company=args.analyst_company,
        )
    except build_segments.InputError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"ERROR: La solicitud no pasó la validación: {first}", flush=True)
        _write_result(args.result, {"status": "failed", "fresh": fresh, "synced": synced})
        return 2

    blockers = list(result.publish_blockers)
    only_freshness = bool(blockers) and all(
        item.startswith(FRESHNESS_BLOCKER_PREFIX) for item in blockers
    )
    if result.published:
        status, workbook = "published", result.latest_path
    elif only_freshness:
        status, workbook = "candidate_unverified", result.candidate_path
    else:
        status, workbook = "blocked", None

    _write_result(args.result, {
        "status": status,
        "fresh": fresh,
        "synced": synced,
        "workbook": str(workbook) if workbook else None,
        "publish_blockers": blockers,
        "manifest": str(result.manifest_path),
    })
    if status == "blocked":
        print(
            "ERROR: La hoja no pasó las verificaciones de publicación: "
            + (blockers[0] if blockers else "motivo no registrado"),
            flush=True,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
