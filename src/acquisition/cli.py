"""Command-line boundary for the shared quarterly-acquisition engine."""

from __future__ import annotations

import argparse
from contextlib import nullcontext, redirect_stdout
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import sys
from typing import Sequence

from src.acquisition.registry import DEFAULT_REGISTRY_PATH, load_issuer_registry
from src.acquisition.service import (
    EstateCoverage,
    PDF_DOCUMENT_TYPE,
    QuarterlyAcquisitionService,
    RunReport,
    XBRL_DOCUMENT_TYPE,
)
from src.shared.paths import DOCUMENT_ESTATE_DB, DOCUMENT_ESTATE_DIR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="refresh-quarterly-estate",
        description=(
            "Audit or refresh quarterly PDFs and BMV XBRL into the root document estate."
        ),
    )
    parser.add_argument(
        "command",
        choices=("audit", "plan", "sync", "backfill"),
        help="audit/plan are read-only; sync/backfill fetch and persist",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="ISSUER[,ISSUER...]",
        help="limit to canonical slugs or tickers (repeatable; default: all active issuers)",
    )
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY_PATH)
    parser.add_argument("--db", type=Path, default=DOCUMENT_ESTATE_DB)
    parser.add_argument("--estate-root", type=Path, default=DOCUMENT_ESTATE_DIR)
    parser.add_argument(
        "--as-of",
        type=_iso_date,
        default=None,
        help="coverage date in YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--recheck-latest",
        type=int,
        default=2,
        metavar="N",
        help="re-fetch the latest N completed quarters to detect restatements",
    )
    parser.add_argument(
        "--trigger",
        default="manual",
        help="durable run trigger label, e.g. manual, cron, github_actions",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="required confirmation for sync/backfill estate mutations",
    )
    parser.add_argument(
        "--from-year",
        type=int,
        default=None,
        metavar="YEAR",
        help="historical floor for explicit backfill",
    )
    parser.add_argument(
        "--allow-coverage-gaps",
        action="store_true",
        help=(
            "do not fail an applied run solely for readiness/coverage gaps "
            "(execution failures still fail)"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.recheck_latest < 0:
        raise SystemExit("--recheck-latest cannot be negative")
    apply_mode = args.command in {"sync", "backfill"}
    if apply_mode and not args.apply:
        raise SystemExit(f"{args.command} mutates the estate; re-run with --apply")
    if args.apply and not apply_mode:
        raise SystemExit("--apply is only valid with sync/backfill")
    if args.from_year is not None and args.command != "backfill":
        raise SystemExit("--from-year is only valid with backfill")
    if args.from_year is not None and not 1900 <= args.from_year <= date.today().year:
        raise SystemExit("--from-year is out of range")

    registry = load_issuer_registry(args.registry)
    coverage = EstateCoverage(args.db, estate_root=args.estate_root)
    service_kwargs = {
        "coverage": coverage,
        "recheck_periods": args.recheck_latest,
    }
    resources = []
    if apply_mode:
        from src.acquisition.ledger import AcquisitionLedger
        from src.acquisition.writer import EstateWriter

        ledger = AcquisitionLedger(args.db)
        try:
            writer = EstateWriter(args.db, args.estate_root)
        except BaseException:
            ledger.close()
            raise
        resources.extend((writer, ledger))
        service_kwargs.update(
            ledger=ledger,
            writer=writer,
        )
    service = QuarterlyAcquisitionService(registry, **service_kwargs)
    only = _only_values(args.only)

    if not apply_mode:
        plans = service.plan(only=only, as_of=args.as_of)
        report = RunReport(run_id=None, mode=args.command, plans=plans)
    else:
        try:
            output_guard = redirect_stdout(sys.stderr) if args.json else nullcontext()
            with output_guard:
                report = service.sync(
                    only=only,
                    as_of=args.as_of,
                    trigger=args.trigger,
                    wayback_backfill=args.command == "backfill",
                    backfill_from_year=args.from_year,
                    strict_coverage=not args.allow_coverage_gaps,
                )
        finally:
            for resource in resources:
                close = getattr(resource, "close", None)
                if callable(close):
                    close()

    if args.json:
        payload = asdict(report)
        payload["fleet_summary"] = _fleet_summary(report)
        print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))
    else:
        _print_report(report)
    return report.exit_code


def _only_values(values: Sequence[str]) -> tuple[str, ...] | None:
    flattened = tuple(
        token.strip()
        for value in values
        for token in value.split(",")
        if token.strip()
    )
    return flattened or None


def _print_report(report: RunReport) -> None:
    summary = _fleet_summary(report)
    print(
        f"{report.mode}: {len(report.plans)} issuer(s)"
        + (f" · run {report.run_id}" if report.run_id else "")
    )
    print(
        f"fleet: {summary['primary_pdf_sources_configured']} "
        "primary-PDF source(s) configured · "
        f"{summary['xbrl_sources_configured']} XBRL source(s) configured "
        f"({summary['xbrl_sources_verified']} locally verified) · "
        f"{summary['due_missing_pdf_periods']} due PDF period(s) missing · "
        f"{summary['readiness_gaps']} readiness gap(s) · "
        f"{summary['coverage_gate_gaps']} applied coverage gap(s)"
    )
    for issuer_plan in report.plans:
        issuer = issuer_plan.issuer
        if not issuer_plan.sources:
            print(f"  {issuer.slug}: no enabled quarterly source")
            continue
        print(f"  {issuer.slug} ({issuer.ticker})")
        for gap in issuer_plan.gaps:
            print(f"    GAP: {gap}")
        for source in issuer_plan.sources:
            print(
                f"    {source.source_key}: "
                f"{len(source.known_periods)} covered · "
                f"{len(source.missing_periods)} due missing · "
                f"{len(source.recheck_periods)} recheck · {source.readiness}"
            )
    if report.run_id:
        print(
            f"result: {report.discovered} discovered · {report.stored} stored · "
            f"{report.unchanged} unchanged · {len(report.failures)} failed"
        )
        for failure in report.failures:
            print(
                f"  FAILED {failure.issuer_slug}/{failure.source_key}: "
                f"{failure.error}",
                file=sys.stderr,
            )
        for gap in report.coverage_gaps:
            periods = f" [{', '.join(gap.periods)}]" if gap.periods else ""
            print(
                f"  COVERAGE {gap.issuer_slug}/{gap.source_key}: "
                f"{gap.reason}{periods}",
                file=sys.stderr,
            )


def _fleet_summary(report: RunReport) -> dict[str, int]:
    return {
        "active_issuers": len(report.plans),
        "primary_pdf_sources_configured": sum(
            any(source.document_type == PDF_DOCUMENT_TYPE for source in plan.sources)
            for plan in report.plans
        ),
        "xbrl_sources_configured": sum(
            any(source.document_type == XBRL_DOCUMENT_TYPE for source in plan.sources)
            for plan in report.plans
        ),
        "xbrl_sources_verified": sum(
            any(
                source.document_type == XBRL_DOCUMENT_TYPE
                and source.readiness == "verified"
                for source in plan.sources
            )
            for plan in report.plans
        ),
        "due_missing_pdf_periods": sum(
            len(plan.primary_pdf_missing_periods) for plan in report.plans
        ),
        "coverage_gate_gaps": len(report.coverage_gaps),
        "readiness_gaps": sum(len(plan.gaps) for plan in report.plans),
    }
def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


if __name__ == "__main__":  # pragma: no cover - exercised by wrapper/CLI tests
    raise SystemExit(main())
