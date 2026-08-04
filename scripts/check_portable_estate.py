#!/usr/bin/env python3
"""Read-only integrity and portability check for one estate bundle."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from estate_portability import verify_portable_estate
from estate_volume import inspect_estate_volume


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--estate-root", type=Path, required=True)
    parser.add_argument("--estate-id")
    parser.add_argument("--mount-root", type=Path)
    parser.add_argument("--full-hashes", action="store_true")
    parser.add_argument("--require-alpha-index", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = verify_portable_estate(
        args.estate_root,
        expected_estate_id=args.estate_id,
        full_hashes=args.full_hashes,
        require_alpha_index=args.require_alpha_index,
    )
    if args.mount_root is not None:
        identity = inspect_estate_volume(
            args.estate_root,
            mount_root=args.mount_root,
            expected_estate_id=args.estate_id,
        )
        result["mount_identity"] = {
            "healthy": identity.healthy,
            "mount_root": str(identity.mount_root) if identity.mount_root else None,
            "observed_estate_id": identity.observed_estate_id,
            "problems": list(identity.problems),
        }
        if not identity.healthy:
            result["failures"].extend(identity.problems)
            result["verified"] = False
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("VERIFIED" if result["verified"] else "FAILED")
        for key in ("estate_id", "catalog_sha256", "counts", "unique_object_bytes"):
            print(f"{key}: {result.get(key)}")
        for failure in result["failures"]:
            print(f"- {failure}")
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
