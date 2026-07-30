#!/usr/bin/env python3
"""Gzip-compress the raw BMV XBRL cache in place (lossless).

Raw filing instance JSONs under ``data/reports/<company>/xbrl/<TICKER>_<PERIOD>.json``
are ~14 MB self-describing XBRL documents that compress ~9x. This rewrites each as
``<...>.json.gz`` — verifying the gzip round-trip *before* removing the plaintext
original — so no data is ever lost. The coverage engine reads either form
(see ``_read_raw_text`` / ``_resolve_raw`` in ``src/download/bmv_xbrl.py``), so the
migration is transparent.

Derived ``*_facts.json`` / ``*_mdna.html`` artifacts are left untouched.

Idempotent and resumable: a filing that already has a ``.json.gz`` sibling is skipped.

    python3 scripts/gzip_xbrl_cache.py --dry-run     # report projected savings only
    python3 scripts/gzip_xbrl_cache.py               # migrate in place
"""
from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

# soft/scripts/gzip_xbrl_cache.py -> parents[1] == soft/
REPORTS_DIR = Path(__file__).resolve().parents[1] / "data" / "reports"


def _raw_jsons(root: Path):
    """Yield raw filing JSONs (excluding the derived *_facts.json artifacts)."""
    for p in sorted(root.glob("*/xbrl/*.json")):
        if p.name.endswith("_facts.json"):
            continue
        yield p


def main() -> None:
    ap = argparse.ArgumentParser(description="Gzip the raw XBRL cache in place (lossless).")
    ap.add_argument("--root", type=Path, default=REPORTS_DIR,
                    help="reports dir to migrate (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    ap.add_argument("--level", type=int, default=6, help="gzip level 1-9 (default: 6)")
    args = ap.parse_args()

    if not args.root.is_dir():
        sys.exit(f"no such reports dir: {args.root}")

    files = list(_raw_jsons(args.root))
    n = len(files)
    before = after = 0
    done = skipped = failed = 0
    mb = 1024 * 1024

    for i, src in enumerate(files, 1):
        dst = src.with_name(src.name + ".gz")
        raw = src.stat().st_size
        before += raw
        if dst.exists():
            skipped += 1
            after += dst.stat().st_size
            continue
        if args.dry_run:
            after += raw // 9  # rough projection (~9x on this data)
            done += 1
            continue
        try:
            data = src.read_bytes()
            tmp = src.with_name(src.name + ".gz.tmp")
            tmp.write_bytes(gzip.compress(data, args.level))
            # Verify the round-trip before deleting the plaintext source.
            if gzip.decompress(tmp.read_bytes()) != data:
                tmp.unlink(missing_ok=True)
                raise ValueError("gzip round-trip mismatch")
            tmp.rename(dst)
            src.unlink()
            after += dst.stat().st_size
            done += 1
        except Exception as exc:  # noqa: BLE001 — never abort the whole run on one file
            failed += 1
            print(f"  FAIL {src}: {exc}", file=sys.stderr)
            continue
        if i % 200 == 0:
            print(f"  [{i}/{n}] {done} done, {skipped} already-gz, {failed} failed",
                  file=sys.stderr)

    verb = "Would compress" if args.dry_run else "Compressed"
    print(f"\n{verb} {done} files ({skipped} already gz, {failed} failed) under {args.root}")
    if before:
        print(f"raw {before / mb:,.0f} MB -> ~{after / mb:,.0f} MB "
              f"({(1 - after / before) * 100:.0f}% smaller)")
    else:
        print("no raw plaintext files found (already migrated?)")


if __name__ == "__main__":
    main()
