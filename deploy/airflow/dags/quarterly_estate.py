"""Portable Airflow 3 DAGs for the quarterly document estate."""

from __future__ import annotations

from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys

import pendulum
from airflow.sdk import DAG, get_current_context, task
from airflow.sdk.exceptions import AirflowSkipException


TIMEZONE = "America/Mexico_City"
START_DATE = pendulum.datetime(2026, 1, 1, tz=TIMEZONE)
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def _boolean_setting(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise RuntimeError(
        f"{name} must be one of: {', '.join(sorted(_TRUE | _FALSE))}"
    )


def _integer_setting(
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _absolute_setting(name: str, *, default: Path | None = None) -> Path:
    raw = os.environ.get(name)
    value = Path(raw.strip()).expanduser() if raw and raw.strip() else default
    if value is None:
        raise RuntimeError(f"{name} is required")
    if not value.is_absolute():
        raise RuntimeError(f"{name} must be an absolute path")
    # Normalize ``..`` without dereferencing a configured virtualenv Python
    # symlink into its dependency-free base interpreter.
    return Path(os.path.abspath(value))


def _contract_setting(name: str, *, default: str) -> str:
    value = os.environ.get(name, default).strip()
    if not value or len(value) > 128 or "\n" in value or "\r" in value:
        raise RuntimeError(f"{name} must be a non-empty single line (max 128 chars)")
    return value


@task(
    task_id="audit_fleet",
    execution_timeout=timedelta(hours=1),
    retries=0,
)
def audit_fleet() -> None:
    """Run the downloader's read-only whole-registry audit."""

    context = get_current_context()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "src.deployment.airflow_task",
            "audit",
            "--dag-id",
            context["dag"].dag_id,
            "--run-id",
            context["run_id"],
        ],
        check=True,
    )


@task(
    task_id="sync_fleet",
    execution_timeout=timedelta(hours=5),
    retries=0,
    pool="estate_writer",
    pool_slots=1,
)
def sync_fleet() -> None:
    """Run one idempotent, non-overlapping whole-registry refresh."""

    context = get_current_context()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "src.deployment.airflow_task",
            "sync",
            "--dag-id",
            context["dag"].dag_id,
            "--run-id",
            context["run_id"],
        ],
        check=True,
    )


@task(
    task_id="process_estate_outbox",
    execution_timeout=timedelta(hours=4),
    retries=0,
    pool="estate_writer",
    pool_slots=1,
)
def process_estate_outbox() -> None:
    """Drain root derivatives and an optional Alpha Go projection."""

    if not _boolean_setting("PDFS_AIRFLOW_SYNC_ENABLED", default=False):
        raise RuntimeError(
            "post-sync delivery requires PDFS_AIRFLOW_SYNC_ENABLED=true"
        )
    if not _boolean_setting(
        "PDFS_AIRFLOW_POST_SYNC_ENABLED",
        default=True,
    ):
        raise AirflowSkipException(
            "post-sync delivery is disabled by PDFS_AIRFLOW_POST_SYNC_ENABLED"
        )

    project_root = _absolute_setting("PDFS_PROJECT_ROOT")
    estate_root = _absolute_setting("PDFS_DOCUMENT_ESTATE")
    worker = project_root / "scripts" / "process_estate_outbox.py"
    if not worker.is_file():
        raise RuntimeError(f"estate outbox worker not found: {worker}")
    if not estate_root.is_dir():
        raise RuntimeError(f"estate mount is not a directory: {estate_root}")

    batch_size = _integer_setting(
        "PDFS_AIRFLOW_OUTBOX_BATCH_SIZE",
        default=250,
        minimum=1,
        maximum=1000,
    )
    max_batches = _integer_setting(
        "PDFS_AIRFLOW_OUTBOX_MAX_BATCHES",
        default=20,
        minimum=1,
        maximum=100,
    )
    batch_timeout = _integer_setting(
        "PDFS_AIRFLOW_OUTBOX_BATCH_TIMEOUT_SECONDS",
        default=3600,
        minimum=60,
        maximum=14400,
    )
    command = [
        sys.executable,
        str(worker),
        "run",
        "--apply",
        "--estate-root",
        str(estate_root),
        "--database",
        str(estate_root / "catalog.db"),
        "--max-deliveries",
        str(batch_size),
        "--parser-version",
        _contract_setting("PDFS_AIRFLOW_PDF_PARSER_VERSION", default="1"),
        "--xbrl-processor-version",
        _contract_setting("PDFS_AIRFLOW_XBRL_PROCESSOR_VERSION", default="1"),
        "--json",
    ]

    alpha_enabled = _boolean_setting(
        "PDFS_AIRFLOW_ALPHA_ENABLED", default=False
    )
    if alpha_enabled:
        alpha_root = _absolute_setting(
            "PDFS_AIRFLOW_ALPHA_ROOT",
            default=project_root / "alpha-go",
        )
        alpha_config = _absolute_setting(
            "PDFS_AIRFLOW_ALPHA_CONFIG",
            default=alpha_root / "configs" / "alpha_go.yaml",
        )
        alpha_python = _absolute_setting(
            "PDFS_AIRFLOW_ALPHA_PYTHON",
            default=alpha_root / ".venv312" / "bin" / "python",
        )
        if not alpha_root.is_dir():
            raise RuntimeError(f"Alpha Go project root not found: {alpha_root}")
        for path, label in (
            (alpha_config, "Alpha Go configuration"),
            (alpha_python, "Alpha Go Python executable"),
        ):
            if not path.is_file():
                raise RuntimeError(f"{label} not found: {path}")
        target_id = _contract_setting(
            "PDFS_AIRFLOW_ALPHA_TARGET_ID",
            default="shared-estate-v1",
        )
        command.extend(
            (
                "--enable-alpha-go",
                "--alpha-root",
                str(alpha_root),
                "--alpha-corpus",
                str(
                    _absolute_setting(
                        "PDFS_AIRFLOW_ALPHA_CORPUS",
                        default=estate_root / "indexes" / "alpha_go" / "corpus",
                    )
                ),
                "--alpha-index",
                str(
                    _absolute_setting(
                        "PDFS_AIRFLOW_ALPHA_INDEX",
                        default=estate_root / "indexes" / "alpha_go.db",
                    )
                ),
                "--alpha-config",
                str(alpha_config),
                "--alpha-target-id",
                target_id,
                "--alpha-python",
                str(alpha_python),
                "--alpha-timeout-seconds",
                str(
                    _integer_setting(
                        "PDFS_AIRFLOW_ALPHA_TIMEOUT_SECONDS",
                        default=1800,
                        minimum=60,
                        maximum=14400,
                    )
                ),
            )
        )
    else:
        # Omission means "leave externally managed subscriptions alone" at the
        # CLI boundary. Airflow owns this deployment switch, so disabling it
        # must explicitly deactivate any prior Alpha target generation.
        command.append("--disable-alpha-go")

    final_claimed: int | None = None
    batches_run = 0
    for batch_number in range(1, max_batches + 1):
        batches_run = batch_number
        completed = subprocess.run(
            command,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=batch_timeout,
            check=False,
        )
        if completed.stdout:
            print(completed.stdout, end="")
        if completed.stderr:
            print(completed.stderr, end="", file=sys.stderr)
        if completed.returncode:
            raise subprocess.CalledProcessError(
                completed.returncode,
                command,
            )
        try:
            report = json.loads(completed.stdout)
            claimed = int(report["claimed"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "estate outbox worker returned invalid machine output"
            ) from exc
        final_claimed = claimed
        if claimed < batch_size:
            break

    # A short batch is not sufficient proof: retryable receipts may be delayed
    # beyond available_at, and an exactly-full last batch may have drained the
    # queue. The read-only status is the authoritative terminal check.
    status_command = [
        sys.executable,
        str(worker),
        "status",
        "--estate-root",
        str(estate_root),
        "--database",
        str(estate_root / "catalog.db"),
        "--json",
    ]
    status_result = subprocess.run(
        status_command,
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=batch_timeout,
        check=False,
    )
    if status_result.stdout:
        print(status_result.stdout, end="")
    if status_result.stderr:
        print(status_result.stderr, end="", file=sys.stderr)
    try:
        status = json.loads(status_result.stdout)
        deliveries = status.get("deliveries") or {}
        nonterminal = {
            key: int(deliveries.get(key, 0))
            for key in ("pending", "running", "retryable", "dead")
            if int(deliveries.get(key, 0))
        }
        unpublished = int(status.get("unpublished_events", 0))
        missing = int(status.get("missing_receipts", 0))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "estate outbox status returned invalid machine output"
        ) from exc
    if (
        status_result.returncode
        or not status.get("initialized")
        or nonterminal
        or unpublished
        or missing
    ):
        raise RuntimeError(
            "estate outbox is not terminal after bounded processing: "
            + json.dumps(
                {
                    "nonterminal_deliveries": nonterminal,
                    "unpublished_events": unpublished,
                    "missing_receipts": missing,
                    "status_exit_code": status_result.returncode,
                },
                sort_keys=True,
            )
        )
    print(
        "Estate outbox drained "
        f"after {batches_run} batch(es); final batch claimed {final_claimed}."
    )


with DAG(
    dag_id="quarterly_estate_audit",
    description="Read-only downloader and estate coverage audit.",
    start_date=START_DATE,
    schedule=os.environ.get("PDFS_AIRFLOW_AUDIT_SCHEDULE", "0 7 * * 1-5"),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=False,
    tags=["estate", "acquisition", "read-only"],
) as quarterly_estate_audit:
    audit_fleet()


with DAG(
    dag_id="quarterly_estate_sync",
    description="Fetch recent quarterly filings into the mounted estate.",
    start_date=START_DATE,
    schedule=os.environ.get("PDFS_AIRFLOW_SYNC_SCHEDULE", "17 */6 * * *"),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=True,
    tags=["estate", "acquisition", "write"],
) as quarterly_estate_sync:
    sync_fleet() >> process_estate_outbox()
