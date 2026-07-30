#!/usr/bin/env python3
"""Read-only check that every project can reach the estate selected in estate.json."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from estate_bridge import EstateBridgeError, load_estate_bridge  # noqa: E402


def inspect_connection(
    *,
    config_path: str | Path | None = None,
    require_alpha_index: bool = False,
) -> tuple[dict, int]:
    try:
        bridge = load_estate_bridge(project_root=ROOT, config_path=config_path)
        problems = bridge.validate(require_alpha_index=require_alpha_index)
        result: dict = {
            **bridge.as_dict(),
            "connected": False,
            "problems": problems,
        }
        if problems:
            return result, 1
        with bridge.connect_catalog(read_only=True) as connection:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            counts = {}
            for table in ("documents", "artifacts", "content_objects"):
                if table in tables:
                    counts[table] = connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
        result.update(
            {
                "connected": integrity == "ok",
                "sqlite_quick_check": integrity,
                "counts": counts,
                "alpha_go_index_available": bridge.alpha_go_index_path.is_file(),
            }
        )
        return result, 0 if result["connected"] else 1
    except (EstateBridgeError, OSError, sqlite3.Error, ValueError) as exc:
        return {
            "connected": False,
            "problems": [f"{type(exc).__name__}: {exc}"],
        }, 1


def verify_relocated(estate_root: Path, *, require_alpha_index: bool = False) -> tuple[dict, int]:
    """Check a bundle at a location other than the one estate.json names.

    This is the portability claim under test: a bundle should work anywhere once
    ``estate_root`` points at it, because every other key is resolved relative to
    that root. Nothing is written and the checked-in estate.json is not modified —
    the relocated root is supplied through PDFS_DOCUMENT_ESTATE for this process
    only.
    """
    estate_root = Path(estate_root).expanduser().resolve()
    previous = os.environ.get("PDFS_DOCUMENT_ESTATE")
    os.environ["PDFS_DOCUMENT_ESTATE"] = str(estate_root)
    try:
        result, exit_code = inspect_connection(require_alpha_index=require_alpha_index)
    finally:
        if previous is None:
            os.environ.pop("PDFS_DOCUMENT_ESTATE", None)
        else:
            os.environ["PDFS_DOCUMENT_ESTATE"] = previous

    result["relocated_root"] = str(estate_root)
    resolved = Path(result.get("catalog", ""))
    if exit_code == 0 and estate_root not in resolved.parents:
        result.setdefault("problems", []).append(
            f"catalog resolved outside the relocated root: {resolved}"
        )
        exit_code = 1
    return result, exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bridge",
        type=Path,
        help="bridge JSON path (default: repository estate.json)",
    )
    parser.add_argument(
        "--verify-relocated",
        type=Path,
        metavar="ESTATE_ROOT",
        help="verify a bundle at another location (portability check; read-only)",
    )
    parser.add_argument(
        "--require-alpha-index",
        action="store_true",
        help="also fail unless indexes/alpha_go.db exists in the estate",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    if args.verify_relocated is not None:
        result, exit_code = verify_relocated(
            args.verify_relocated,
            require_alpha_index=args.require_alpha_index,
        )
    else:
        result, exit_code = inspect_connection(
            config_path=args.bridge,
            require_alpha_index=args.require_alpha_index,
        )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result.get("connected"):
        print(f"Connected: {result['catalog']}")
        counts = result.get("counts") or {}
        print(
            "Estate: "
            + ", ".join(f"{name}={value}" for name, value in counts.items())
        )
        print(
            "Alpha Go index: "
            + (
                str(result["alpha_go_index"])
                if result.get("alpha_go_index_available")
                else "not present in bundle"
            )
        )
    else:
        print("Estate connection failed:", file=sys.stderr)
        for problem in result.get("problems") or []:
            print(f"- {problem}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
