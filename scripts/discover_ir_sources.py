#!/usr/bin/env python3
"""Propose repeatable quarterly-PDF sources using deterministic static evidence.

The default mode is a dry run: it may read the network and populate its
regenerable cache, but it never changes the issuer registry or document estate.
Use ``--patch-output`` for a reviewable unified diff or the explicit ``--apply``
flag to add only high-confidence, still-missing ``ir`` overlays.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.acquisition.readiness import build_primary_pdf_readiness  # noqa: E402
from src.acquisition.registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    load_issuer_registry,
)
from src.acquisition.service import EstateCoverage  # noqa: E402
from src.acquisition.source_onboarding import (  # noqa: E402
    BmvDirectorySeedProvider,
    DEFAULT_CACHE_MAX_AGE_SECONDS,
    DiscoveryOptions,
    RequestsDiscoveryClient,
    SourceOnboardingCompiler,
    apply_registry_proposals,
    catalog_seeds,
    load_seed_file,
    registry_seeds,
    render_registry_patch,
    select_production_canary_issuers,
    select_readiness_targets,
    verify_configured_production_source,
)
from src.shared.paths import DOCUMENT_ESTATE_DB, DOCUMENT_ESTATE_DIR  # noqa: E402


DEFAULT_CACHE_DIR = ROOT / ".cache" / "source_onboarding"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--database", type=Path, default=DOCUMENT_ESTATE_DB)
    parser.add_argument("--estate-root", type=Path, default=DOCUMENT_ESTATE_DIR)
    parser.add_argument(
        "--issuer",
        action="append",
        default=[],
        help="limit discovery to this canonical slug; repeat as needed",
    )
    parser.add_argument(
        "--include-configured-gaps",
        action="store_true",
        help="include configured sources with readiness gaps, not just unconfigured issuers",
    )
    parser.add_argument(
        "--seed-file",
        type=Path,
        help="YAML/JSON mapping of issuer slug to one or more authoritative URLs",
    )
    parser.add_argument("--output", type=Path, help="write structured JSON instead of stdout")
    parser.add_argument(
        "--patch-output",
        type=Path,
        help="write a unified registry diff for high-confidence proposals",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="atomically add high-confidence missing IR overlays after validating the registry",
    )
    parser.add_argument("--refresh", action="store_true", help="bypass proposal cache")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument(
        "--cache-max-age-hours",
        type=float,
        default=DEFAULT_CACHE_MAX_AGE_SECONDS / 3600,
    )
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--delay-ms", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=12)
    parser.add_argument("--max-candidates", type=int, default=24)
    parser.add_argument("--sample-pdfs", type=int, default=3)
    parser.add_argument(
        "--no-bmv-directory-seeds",
        action="store_true",
        help="do not resolve missing official websites through the cached BMV directory",
    )
    parser.add_argument(
        "--max-issuers",
        type=int,
        default=10,
        help="bounded default cohort size when --issuer/--random-sample/--all is absent",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="process every selected readiness gap; use intentionally because profiles are fetched per issuer",
    )
    parser.add_argument(
        "--random-sample",
        type=int,
        help="select this many readiness targets reproducibly before network access",
    )
    parser.add_argument("--sample-seed", type=int, default=20260801)
    parser.add_argument(
        "--verify-live",
        action="store_true",
        help="freshly validate only the newest candidate for each explicitly selected issuer",
    )
    parser.add_argument(
        "--staging-dir",
        type=Path,
        help="non-estate directory in which --verify-live saves fully verified PDFs",
    )
    parser.add_argument(
        "--expected-period",
        help="required by --verify-live; exact canonical period such as 2026-2T",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=2,
        help="bounded whole-adapter attempts per issuer in --verify-live mode",
    )
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.cache_max_age_hours < 0:
        parser.error("--cache-max-age-hours cannot be negative")
    if args.random_sample is not None and args.random_sample <= 0:
        parser.error("--random-sample must be positive")
    if args.max_issuers <= 0:
        parser.error("--max-issuers must be positive")
    if args.all and args.random_sample is not None:
        parser.error("--all and --random-sample are mutually exclusive")
    if args.verify_live:
        if not args.issuer and args.random_sample is None:
            parser.error("--verify-live requires --issuer or --random-sample")
        if args.issuer and args.random_sample is not None:
            parser.error("--verify-live accepts either --issuer or --random-sample, not both")
        if args.staging_dir is None:
            parser.error("--verify-live requires --staging-dir")
        if args.expected_period is None:
            parser.error("--verify-live requires --expected-period")
        if args.apply or args.patch_output:
            parser.error("--verify-live cannot be combined with --apply or --patch-output")
    elif args.staging_dir is not None:
        parser.error("--staging-dir is only valid with --verify-live")
    elif args.expected_period is not None:
        parser.error("--expected-period is only valid with --verify-live")
    if args.attempts <= 0:
        parser.error("--attempts must be positive")
    if args.apply and not args.issuer:
        parser.error("--apply requires explicit --issuer selection")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    registry = load_issuer_registry(args.registry)
    coverage = EstateCoverage(args.database, estate_root=args.estate_root)
    rows = build_primary_pdf_readiness(registry, coverage)
    rows_by_slug = {row.issuer_slug: row for row in rows}
    if args.verify_live:
        try:
            configured = select_production_canary_issuers(
                registry,
                identifiers=args.issuer,
                random_sample=args.random_sample,
                sample_seed=args.sample_seed,
            )
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))
        all_configured = select_production_canary_issuers(
            registry,
            random_sample=len(registry.issuers),
            sample_seed=args.sample_seed,
        )
        available_targets = len(all_configured)
        targets = [(issuer, rows_by_slug[issuer.slug]) for issuer in configured]
    else:
        targets = list(
            select_readiness_targets(
                registry,
                rows,
                issuer_slugs=args.issuer,
                include_configured_gaps=args.include_configured_gaps,
            )
        )
        available_targets = len(targets)
        if args.random_sample is not None and len(targets) > args.random_sample:
            rng = random.Random(args.sample_seed)
            selected = set(rng.sample(range(len(targets)), args.random_sample))
            targets = [item for index, item in enumerate(targets) if index in selected]
        elif not args.issuer and not args.all:
            targets = targets[: args.max_issuers]

    if args.verify_live:
        from src.acquisition.adapters import InvestorRelationsAdapter
        from src.acquisition.bmv_issuer import BmvIssuerPdfAdapter

        ir_adapter = InvestorRelationsAdapter()
        bmv_adapter = BmvIssuerPdfAdapter()
        canaries = [
            verify_configured_production_source(
                issuer,
                expected_period=args.expected_period,
                staging_dir=args.staging_dir,
                attempts=args.attempts,
                ir_adapter=ir_adapter,
                bmv_adapter=bmv_adapter,
            )
            for issuer, _row in targets
        ]
        payload = {
            "schema_version": 1,
            "algorithm": "configured-production-adapter-canary-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "mode": "verify_live",
            "expected_period": args.expected_period,
            "estate_mutated": False,
            "registry_mutated": False,
            "summary": {
                "available_targets": available_targets,
                "selected": len(canaries),
                "passed": sum(item.passed for item in canaries),
                "failed": sum(not item.passed for item in canaries),
            },
            "canaries": [item.to_dict() for item in canaries],
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            _write_text(args.output, rendered)
        else:
            print(rendered, end="")
        return 0 if canaries and all(item.passed for item in canaries) else 1

    supplied_seeds = load_seed_file(args.seed_file) if args.seed_file else {}
    unknown_seed_slugs = sorted(set(supplied_seeds) - set(registry.by_slug))
    if unknown_seed_slugs:
        parser.error(
            "seed file references unknown issuer slug(s): "
            + ", ".join(unknown_seed_slugs)
        )

    options = DiscoveryOptions(
        max_pages=args.max_pages,
        max_candidates=args.max_candidates,
        sample_pdfs=1 if args.verify_live else args.sample_pdfs,
    )
    client = RequestsDiscoveryClient(
        timeout_seconds=args.timeout_seconds,
        delay_ms=args.delay_ms,
    )
    compiler = SourceOnboardingCompiler(
        client=client,
        options=options,
        cache_dir=None if args.no_cache else args.cache_dir,
        verification_dir=None,
        cache_max_age_seconds=args.cache_max_age_hours * 3600,
    )
    bmv_provider = BmvDirectorySeedProvider(
        client=client,
        cache_dir=None if args.no_cache else args.cache_dir / "bmv",
        cache_max_age_seconds=args.cache_max_age_hours * 3600,
    )

    proposals = []
    seed_resolution: list[dict[str, Any]] = []
    for issuer, row in targets:
        seeds = [
            *catalog_seeds(args.database, issuer.slug),
            *supplied_seeds.get(issuer.slug, ()),
        ]
        existing = (*registry_seeds(issuer), *seeds)
        if existing:
            seed_resolution.append(
                {
                    "issuer_slug": issuer.slug,
                    "status": "existing_seed",
                    "seeds": [
                        {"url": seed.url, "origin": seed.origin}
                        for seed in existing
                    ],
                }
            )
        elif args.no_bmv_directory_seeds:
            seed_resolution.append(
                {
                    "issuer_slug": issuer.slug,
                    "status": "bmv_directory_disabled",
                    "seeds": [],
                }
            )
        else:
            evidence = bmv_provider.resolve(issuer, refresh=args.refresh)
            seed_resolution.append(evidence.to_dict())
            if evidence.seed is not None:
                seeds.append(evidence.seed)
        proposals.append(
            compiler.compile(
                issuer,
                readiness=row,
                seeds=tuple(seeds),
                refresh=args.refresh or args.verify_live,
            )
        )

    patch = render_registry_patch(args.registry, proposals)
    if args.patch_output is not None:
        _write_text(args.patch_output, patch)

    applied: tuple[str, ...] = ()
    if args.apply:
        applied = apply_registry_proposals(args.registry, proposals)

    summary: dict[str, Any] = {
        "available_targets": available_targets,
        "selected": len(proposals),
        "ready_for_review": sum(item.status == "ready_for_review" for item in proposals),
        "patch_eligible": sum(item.patch_eligible for item in proposals),
        "needs_seed": sum(item.status == "needs_seed" for item in proposals),
        "fully_verified_pdfs": sum(
            validation.fully_verified
            for item in proposals
            for validation in item.validations
        ),
        "cache_hits": sum(item.cache_hit for item in proposals),
        "bmv_websites_resolved": sum(
            item.get("status") == "ready" for item in seed_resolution
        ),
        "bmv_resolution_failures": sum(
            str(item.get("status", "")).startswith(("directory_", "profile_"))
            or item.get("status") in {"ticker_missing", "ticker_ambiguous", "website_missing"}
            for item in seed_resolution
        ),
        "applied": list(applied),
    }
    payload = {
        "schema_version": 1,
        "algorithm": "static-ir-source-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "verify_live" if args.verify_live else ("apply" if args.apply else "dry_run"),
        "estate_mutated": False,
        "registry_mutated": bool(applied),
        "summary": summary,
        "seed_resolution": seed_resolution,
        "proposals": [item.to_dict() for item in proposals],
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        _write_text(args.output, rendered)
    else:
        print(rendered, end="")

    if args.apply and not applied:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
