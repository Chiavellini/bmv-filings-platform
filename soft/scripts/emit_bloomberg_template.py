#!/usr/bin/env python3
"""Emit the blank Bloomberg fill-in template for a coverage spec.

Usage (from the soft/ root):

    python3 scripts/emit_bloomberg_template.py inputs/walmex.md [--out inputs/walmex.bloomberg.csv]

Fill the ``value`` column from a Bloomberg terminal and save it to
``data/bloomberg/<slug>.csv`` — that is what ``scripts/build_coverage.py`` ingests.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bloomberg.template import build_rows, write_template  # noqa: E402
from src.coverage.spec import parse_spec  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", help="path to inputs/<slug>.md")
    ap.add_argument("--out", default=None, help="output CSV path (default inputs/<slug>.bloomberg.csv)")
    ap.add_argument("--base-year", type=int, default=None, help="override the base year for history rows")
    args = ap.parse_args()

    spec = parse_spec(args.spec)
    out = Path(args.out) if args.out else (ROOT / "inputs" / f"{spec.slug}.bloomberg.csv")
    write_template(spec, out, base_year=args.base_year)
    n = len(build_rows(spec, base_year=args.base_year))
    print(f"[emit] {spec.name} ({spec.slug}): wrote {n} template cells -> {out}")
    print(f"[emit] blocks: {', '.join(spec.blocks)}")
    print(f"[emit] peers: {', '.join(p.slug for p in spec.peers) or '(none)'}")
    print(f"[emit] fill the 'value' column and save to data/bloomberg/{spec.slug}.csv")


if __name__ == "__main__":
    main()
