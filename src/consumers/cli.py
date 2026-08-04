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
from src.consumers.derivatives import PdfMarkdownDerivativeConsumer
from src.consumers.outbox import OutboxDispatcher
from src.shared.paths import DOCUMENT_ESTATE_DB, DOCUMENT_ESTATE_DIR, PROJECT_ROOT


DEFAULT_ALPHA_ROOT = PROJECT_ROOT / "alpha-go"
# Preserve the current certified manifest location for interactive/manual runs.
# Deployment must pass an explicit absolute corpus path; a portable estate can
# use its root only after that manifest has been seeded or fully reconciled.
DEFAULT_ALPHA_CORPUS = DEFAULT_ALPHA_ROOT / "data" / "corpus"
DEFAULT_ALPHA_CONFIG = DEFAULT_ALPHA_ROOT / "configs" / "alpha_go.yaml"


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


def _add_alpha_arguments(
    parser: argparse.ArgumentParser,
    *,
    index_required: bool,
) -> None:
    parser.add_argument(
        "--alpha-root",
        type=Path,
        default=DEFAULT_ALPHA_ROOT,
        help="Alpha Go project root",
    )
    parser.add_argument(
        "--alpha-corpus",
        type=Path,
        default=DEFAULT_ALPHA_CORPUS,
        help="Alpha Go corpus projection directory",
    )
    parser.add_argument(
        "--alpha-index",
        type=Path,
        required=index_required,
        help="Alpha Go SQLite index target",
    )
    parser.add_argument(
        "--alpha-config",
        type=Path,
        default=DEFAULT_ALPHA_CONFIG,
        help="Alpha Go runtime/index configuration",
    )
    parser.add_argument(
        "--alpha-target-id",
        help=(
            "stable deployment generation for delivery receipts; change it "
            "when rebuilding a target in place"
        ),
    )
    parser.add_argument(
        "--alpha-python",
        default=sys.executable,
        help="Python executable for the isolated Alpha Go projection",
    )
    parser.add_argument(
        "--alpha-timeout-seconds",
        type=int,
        default=1800,
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
        "--enable-alpha-go",
        action="store_true",
        help="also deliver parsed documents into an Alpha Go index",
    )
    _add_alpha_arguments(run, index_required=False)
    run.add_argument(
        "--require-drained",
        action="store_true",
        help=(
            "exit nonzero if any enabled receipt remains pending, running, "
            "retryable, dead, missing, or unpublished after this bounded run"
        ),
    )

    reconcile = commands.add_parser(
        "reconcile-alpha",
        help=(
            "reconcile every searchable estate document into one Alpha target"
        ),
    )
    _add_catalog_arguments(reconcile)
    reconcile.add_argument(
        "--apply",
        action="store_true",
        help="required acknowledgement that this command writes Alpha's target",
    )
    _add_alpha_arguments(reconcile, index_required=True)

    audit_alpha = commands.add_parser(
        "audit-alpha",
        help=(
            "read-only coverage check for eligible estate families and Alpha"
        ),
    )
    _add_catalog_arguments(audit_alpha)
    _add_alpha_arguments(audit_alpha, index_required=True)

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
            deliveries.get("dead")
            or status.get("ready_receipts")
            or status.get("expired_running_receipts")
            or status.get("missing_receipts")
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
        consumers: list[object] = [
            stack.enter_context(
                PdfMarkdownDerivativeConsumer(
                    database,
                    estate_root,
                    parser_version=args.parser_version,
                )
            )
        ]
        if args.enable_alpha_go:
            consumers.append(
                AlphaGoProjectionConsumer(
                    database,
                    project_root=args.alpha_root,
                    corpus=args.alpha_corpus,
                    index_db=args.alpha_index,
                    config=args.alpha_config,
                    target_id=args.alpha_target_id,
                    python_executable=args.alpha_python,
                    timeout_seconds=args.alpha_timeout_seconds,
                )
            )
        dispatcher = stack.enter_context(
            OutboxDispatcher(
                database,
                consumers,
                lease_seconds=args.lease_seconds,
                max_attempts=args.max_attempts,
                retry_base_seconds=args.retry_base_seconds,
                retry_max_seconds=args.retry_max_seconds,
            )
        )
        report = dispatcher.run(max_deliveries=args.max_deliveries)
    payload = report.as_dict()
    exit_code = report.exit_code
    if args.require_drained:
        status = OutboxDispatcher.read_status(database)
        deliveries = status.get("deliveries", {})
        blocking_states = {
            state: int(deliveries.get(state) or 0)
            for state in ("pending", "running", "retryable", "dead")
        }
        drained = bool(status.get("initialized")) and not any(
            (
                *blocking_states.values(),
                int(status.get("missing_receipts") or 0),
                int(status.get("unpublished_events") or 0),
            )
        )
        payload.update(
            {
                "drained": drained,
                "drain_blockers": blocking_states,
                "delivery_status": status,
            }
        )
        if not drained and exit_code == 0:
            exit_code = 2
    _print(payload, as_json=args.json)
    return exit_code


def _reconcile_alpha(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    if not args.apply:
        parser.error("reconcile-alpha writes Alpha's target; pass --apply to proceed")
    consumer = AlphaGoProjectionConsumer(
        _database(args),
        project_root=args.alpha_root,
        corpus=args.alpha_corpus,
        index_db=args.alpha_index,
        config=args.alpha_config,
        target_id=args.alpha_target_id,
        python_executable=args.alpha_python,
        timeout_seconds=args.alpha_timeout_seconds,
    )
    detail = consumer.reconcile()
    _print(
        {"status": "succeeded", **detail},
        as_json=args.json,
    )
    return 0


def _audit_alpha(args: argparse.Namespace) -> int:
    consumer = AlphaGoProjectionConsumer(
        _database(args),
        project_root=args.alpha_root,
        corpus=args.alpha_corpus,
        index_db=args.alpha_index,
        config=args.alpha_config,
        target_id=args.alpha_target_id,
        python_executable=args.alpha_python,
        timeout_seconds=args.alpha_timeout_seconds,
    )
    detail = consumer.audit()
    _print(
        {"status": "healthy" if detail["healthy"] else "drift", **detail},
        as_json=args.json,
    )
    return 0 if detail["healthy"] else 1


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
    if args.command == "reconcile-alpha":
        return _reconcile_alpha(args, parser)
    if args.command == "audit-alpha":
        return _audit_alpha(args)
    return _run(args, parser)


if __name__ == "__main__":
    raise SystemExit(main())
