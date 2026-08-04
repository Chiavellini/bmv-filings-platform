#!/usr/bin/env python3
"""onboard_source.py — give a company an IR binding, or prove the one it has still works.

``scripts/discover_ir_sources.py`` already implements both halves of this, but
they are separate modes with mutually exclusive flags and six arguments to
assemble correctly (``--verify-live`` needs ``--staging-dir`` *and* an exact
``--expected-period``, and refuses to run with ``--apply``). Getting that wrong
is the difference between "no source found" and "never actually looked".

This wrapper picks the right mode for the company's current state and computes
the arguments:

  unbound issuer  → discovery: propose an ``ir:`` overlay from BMV directory
                    seeds, print it, and with ``--apply`` write it to the
                    registry, then canary the result.
  bound issuer    → canary: fetch the newest expected quarter through the
                    configured production adapter and report pass/fail.

Read-only by default. ``--apply`` is the only thing that writes.

Usage:
    python3 scripts/onboard_source.py <slug>
    python3 scripts/onboard_source.py <slug> --apply
    python3 scripts/onboard_source.py <slug> --expected-period 2026-1T
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.fetch_company_reports import _expected_periods, START_FLOOR_YEAR  # noqa: E402
from src.shared.company_source import (  # noqa: E402
    UnknownCompanyError,
    resolve_company_source,
)
from src.shared.report_index import period_sort_key  # noqa: E402


def latest_expected_period(coverage_from: str | None = None) -> str:
    """Newest quarter an issuer should already have published."""
    periods = _expected_periods(START_FLOOR_YEAR, coverage_from)
    return max(periods, key=period_sort_key)


def _run_discovery(slug: str, *, apply: bool, refresh: bool) -> int:
    from scripts.discover_ir_sources import main as discover_main

    argv = ["--issuer", slug]
    if refresh:
        argv.append("--refresh")
    if apply:
        argv.append("--apply")
    print(f"→ discovery mode: proposing an IR binding for {slug}"
          f"{' (--apply: will write to the registry)' if apply else ' (dry run)'}\n")
    return discover_main(argv)


def _run_canary(slug: str, expected_period: str, *, refresh: bool) -> int:
    from scripts.discover_ir_sources import main as discover_main

    with tempfile.TemporaryDirectory(prefix=f"canary-{slug}-") as staging:
        argv = [
            "--issuer", slug,
            "--verify-live",
            "--expected-period", expected_period,
            "--staging-dir", staging,
        ]
        if refresh:
            argv.append("--refresh")
        print(f"→ canary mode: fetching {expected_period} for {slug} "
              "through the configured production adapter\n")
        return discover_main(argv)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("slug", help="canonical issuer slug from configs/issuers.yaml")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write a high-confidence proposal into the registry (unbound issuers only)",
    )
    ap.add_argument(
        "--expected-period",
        help="canonical period the canary must retrieve (default: newest expected quarter)",
    )
    ap.add_argument("--refresh", action="store_true", help="bypass the discovery cache")
    ap.add_argument(
        "--no-canary",
        action="store_true",
        help="skip the live fetch; only report binding state / proposals",
    )
    args = ap.parse_args(argv)

    try:
        source = resolve_company_source(args.slug)
    except UnknownCompanyError as exc:
        print(f"{args.slug}: {exc}", file=sys.stderr)
        print(
            "Only slugs in configs/issuers.yaml can be onboarded. "
            "Add the issuer to the roster first.",
            file=sys.stderr,
        )
        return 2

    expected_period = args.expected_period or latest_expected_period(
        source.coverage_from_period
    )
    print(
        f"{args.slug}: ticker={source.ticker or '?'} "
        f"xbrl={'yes' if source.has_xbrl else 'no'} "
        f"ir={'bound' if source.has_ir_binding else 'UNBOUND'} "
        f"(surfaces: {', '.join(source.provenance) or 'none'})"
    )

    if source.has_ir_binding:
        if args.apply:
            print(
                f"{args.slug} already has an IR binding ({source.ir_url}); "
                "nothing to apply. Verifying it instead.",
                file=sys.stderr,
            )
        if args.no_canary:
            return 0
        return _run_canary(args.slug, expected_period, refresh=args.refresh)

    print(
        f"\n{args.slug} has no IR binding. The BMV XBRL archive still covers it from "
        "~2021 onward (facts + MD&A); an IR binding is what buys earlier quarters "
        "and the issuer's own earnings-release PDFs.\n"
    )
    status = _run_discovery(args.slug, apply=args.apply, refresh=args.refresh)
    if status != 0 or not args.apply or args.no_canary:
        return status

    # Re-resolve: --apply may have just written the overlay.
    if resolve_company_source(args.slug).has_ir_binding:
        print("\n→ binding applied; verifying it live\n")
        return _run_canary(args.slug, expected_period, refresh=args.refresh)
    print(
        f"{args.slug}: no high-confidence proposal was applied; "
        "add ir.url / ir.pdf_link_pattern by hand and re-run.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
