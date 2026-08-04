#!/usr/bin/env python3
"""Export a catalogued estate as an atomically verified portable bundle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from estate_portability import export_portable_estate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--estate-id", required=True)
    parser.add_argument("--no-runtime", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="required acknowledgement that a new destination bundle will be written",
    )
    args = parser.parse_args(argv)
    if not args.apply:
        parser.error("export writes a new bundle; pass --apply to proceed")
    result = export_portable_estate(
        args.source,
        args.destination,
        estate_id=args.estate_id,
        include_runtime=not args.no_runtime,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"Exported and verified: {result['estate_root']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
