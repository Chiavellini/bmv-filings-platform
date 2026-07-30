"""Command line entry point for durable shared-estate consumers.

Inspection is deliberately separated from delivery: ``status`` opens the
catalog read-only, while ``run`` refuses to initialize schemas or invoke a
consumer unless the operator supplies ``--apply``.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys

from src.consumers.alpha_go import AlphaGoProjectionConsumer
from src.consumers.derivatives import (
    PdfMarkdownDerivativeConsumer,
    XbrlFactsDerivativeConsumer,
)
from src.consumers.outbox import OutboxDispatcher
from src.consumers.publication import DerivativePublicationVerifier
from src.shared.paths import DOCUMENT_ESTATE_DB, DOCUMENT_ESTATE_DIR, PROJECT_ROOT


DEFAULT_ALPHA_ROOT = PROJECT_ROOT / "alpha-go"
DEFAULT_ALPHA_CORPUS = DEFAULT_ALPHA_ROOT / "data" / "corpus"
DEFAULT_ALPHA_CONFIG = DEFAULT_ALPHA_ROOT / "configs" / "alpha_go.yaml"
_PDF_CONSUMER_PREFIX = "root.pdf-markdown.v1:"
_XBRL_CONSUMER_PREFIX = "root.xbrl-facts.v1:"
_ALPHA_CONSUMER_PREFIX = "alpha-go.search-projection.v1:"


def _add_catalog_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--estate-root",
        type=Path,
        default=DOCUMENT_ESTATE_DIR,
        help="shared document-estate root",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="estate catalog (default: <estate-root>/catalog.db)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or deliver shared-estate outbox events."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    status = commands.add_parser(
        "status",
        help="read delivery health without creating or migrating anything",
    )
    _add_catalog_arguments(status)

    run = commands.add_parser(
        "run",
        help="initialize consumer receipts and deliver a bounded event batch",
    )
    _add_catalog_arguments(run)
    run.add_argument(
        "--apply",
        action="store_true",
        help="required acknowledgement that this command writes the estate",
    )
    run.add_argument(
        "--max-deliveries",
        type=int,
        default=100,
        help="maximum receipts to handle in this invocation (default: 100)",
    )
    run.add_argument("--lease-seconds", type=int, default=900)
    run.add_argument("--max-attempts", type=int, default=5)
    run.add_argument("--retry-base-seconds", type=int, default=30)
    run.add_argument("--retry-max-seconds", type=int, default=3600)
    run.add_argument(
        "--parser-version",
        default="1",
        help="root PDF parser contract version",
    )
    run.add_argument(
        "--xbrl-processor-version",
        default="1",
        help="root BMV XBRL-to-facts processor contract version",
    )
    alpha_mode = run.add_mutually_exclusive_group()
    alpha_mode.add_argument(
        "--enable-alpha-go",
        action="store_true",
        help="also deliver parsed documents into an Alpha Go index",
    )
    alpha_mode.add_argument(
        "--disable-alpha-go",
        action="store_true",
        help=(
            "explicitly disable all previously registered managed Alpha Go "
            "projection generations"
        ),
    )
    run.add_argument(
        "--alpha-root",
        type=Path,
        default=DEFAULT_ALPHA_ROOT,
        help="Alpha Go project root",
    )
    run.add_argument(
        "--alpha-corpus",
        type=Path,
        default=DEFAULT_ALPHA_CORPUS,
        help="Alpha Go corpus projection directory",
    )
    run.add_argument(
        "--alpha-index",
        type=Path,
        help="Alpha Go SQLite index; required with --enable-alpha-go",
    )
    run.add_argument(
        "--alpha-config",
        type=Path,
        default=DEFAULT_ALPHA_CONFIG,
        help="Alpha Go runtime/index configuration",
    )
    run.add_argument(
        "--alpha-target-id",
        help=(
            "stable deployment generation for delivery receipts; change it "
            "when rebuilding a target in place"
        ),
    )
    run.add_argument(
        "--alpha-python",
        default=sys.executable,
        help="Python executable for the isolated Alpha Go projection",
    )
    run.add_argument(
        "--alpha-timeout-seconds",
        type=int,
        default=1800,
    )

    requeue = commands.add_parser(
        "requeue",
        help="explicitly replay dead receipts while retaining attempt history",
    )
    _add_catalog_arguments(requeue)
    requeue.add_argument(
        "--apply",
        action="store_true",
        help="required acknowledgement that this command writes the estate",
    )
    requeue.add_argument("--consumer-id", required=True)
    requeue.add_argument("--event-type")
    requeue.add_argument("--limit", type=int, default=100)
    return parser


def _database(args: argparse.Namespace) -> Path:
    return Path(args.database or (Path(args.estate_root) / "catalog.db"))


def _print(payload: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
        return
    print(payload)


def _status(args: argparse.Namespace) -> int:
    status = OutboxDispatcher.read_status(_database(args))
    _print(status, as_json=args.json)
    if not status.get("initialized"):
        return 1
    deliveries = status.get("deliveries", {})
    return int(
        bool(
            any(
                deliveries.get(delivery_status)
                for delivery_status in (
                    "pending",
                    "running",
                    "retryable",
                    "dead",
                )
            )
            or status.get("missing_receipts")
            or status.get("unpublished_events")
        )
    )


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if not args.apply:
        parser.error("run writes the estate; pass --apply to proceed")
    if args.max_deliveries < 0:
        parser.error("--max-deliveries cannot be negative")
    if args.enable_alpha_go and args.alpha_index is None:
        parser.error("--alpha-index is required with --enable-alpha-go")

    database = _database(args)
    estate_root = Path(args.estate_root)
    with ExitStack() as stack:
        pdf_consumer = stack.enter_context(
            PdfMarkdownDerivativeConsumer(
                database,
                estate_root,
                parser_version=args.parser_version,
            )
        )
        xbrl_consumer = stack.enter_context(
            XbrlFactsDerivativeConsumer(
                database,
                estate_root,
                processor_version=args.xbrl_processor_version,
            )
        )
        publication_verifier = DerivativePublicationVerifier(
            database, estate_root
        )
        consumers: list[object] = [
            pdf_consumer,
            xbrl_consumer,
            publication_verifier,
        ]
        managed_generations: dict[str, tuple[str, ...]] = {
            _PDF_CONSUMER_PREFIX: (pdf_consumer.consumer_id,),
            _XBRL_CONSUMER_PREFIX: (xbrl_consumer.consumer_id,),
        }
        alpha_consumer: AlphaGoProjectionConsumer | None = None
        if args.enable_alpha_go:
            alpha_consumer = AlphaGoProjectionConsumer(
                database,
                project_root=args.alpha_root,
                corpus=args.alpha_corpus,
                index_db=args.alpha_index,
                config=args.alpha_config,
                target_id=args.alpha_target_id,
                python_executable=args.alpha_python,
                timeout_seconds=args.alpha_timeout_seconds,
            )
            consumers.append(alpha_consumer)
            managed_generations[_ALPHA_CONSUMER_PREFIX] = (
                alpha_consumer.consumer_id,
            )
        elif args.disable_alpha_go:
            managed_generations[_ALPHA_CONSUMER_PREFIX] = ()
        dispatcher = stack.enter_context(
            OutboxDispatcher(
                database,
                consumers,
                lease_seconds=args.lease_seconds,
                max_attempts=args.max_attempts,
                retry_base_seconds=args.retry_base_seconds,
                retry_max_seconds=args.retry_max_seconds,
                managed_generations=managed_generations,
            )
        )
        report = dispatcher.run(max_deliveries=args.max_deliveries)
        disabled_consumer_ids = dispatcher.disabled_consumer_ids
    payload = report.as_dict()
    payload["disabled_consumer_ids"] = list(disabled_consumer_ids)
    _print(payload, as_json=args.json)
    return report.exit_code


def _requeue(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    if not args.apply:
        parser.error("requeue writes the estate; pass --apply to proceed")
    if args.limit <= 0:
        parser.error("--limit must be positive")
    with OutboxDispatcher(_database(args), []) as dispatcher:
        event_ids = dispatcher.requeue_dead(
            args.consumer_id,
            event_type=args.event_type,
            limit=args.limit,
        )
    _print(
        {
            "consumer_id": args.consumer_id,
            "event_type": args.event_type,
            "requeued": len(event_ids),
            "event_ids": list(event_ids),
        },
        as_json=args.json,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return _status(args)
    if args.command == "requeue":
        return _requeue(args, parser)
    return _run(args, parser)


if __name__ == "__main__":
    raise SystemExit(main())
