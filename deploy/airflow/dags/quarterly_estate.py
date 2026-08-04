"""Portable Airflow 3 DAGs for the quarterly document estate."""

from __future__ import annotations

from datetime import timedelta
import json
import os
import subprocess
import sys
from urllib import request
from urllib.parse import urlsplit

import pendulum
from airflow.sdk import DAG, get_current_context, task


TIMEZONE = "America/Mexico_City"
START_DATE = pendulum.datetime(2026, 1, 1, tz=TIMEZONE)


def _bounded_environment_integer(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


TASK_RETRIES = _bounded_environment_integer(
    "PDFS_AIRFLOW_TASK_RETRIES", 2, minimum=0, maximum=5
)
RETRY_DELAY = timedelta(
    seconds=_bounded_environment_integer(
        "PDFS_AIRFLOW_RETRY_DELAY_SECONDS", 300, minimum=30, maximum=3600
    )
)


def _notify_failure(context: dict[str, object]) -> None:
    """Optionally send one compact final-failure notification.

    Airflow's task state remains the source of truth.  The webhook is an
    operator-selected HTTPS endpoint and is deliberately disabled when the
    generated environment leaves it blank.
    """

    url = os.environ.get("PDFS_AIRFLOW_ALERT_WEBHOOK", "").strip()
    if not url:
        return
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("PDFS_AIRFLOW_ALERT_WEBHOOK must use https://")
    task_instance = context.get("task_instance")
    payload = json.dumps(
        {
            "event": "quarterly_estate_task_failed",
            "dag_id": getattr(task_instance, "dag_id", None),
            "task_id": getattr(task_instance, "task_id", None),
            "run_id": context.get("run_id"),
            "try_number": getattr(task_instance, "try_number", None),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request.urlopen(  # noqa: S310 - endpoint is explicit and HTTPS-only
        request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        ),
        timeout=10,
    ).close()


def _run_boundary(mode: str) -> None:
    context = get_current_context()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "src.deployment.airflow_task",
            mode,
            "--dag-id",
            context["dag"].dag_id,
            "--run-id",
            context["run_id"],
        ],
        check=True,
    )


@task(
    task_id="audit_fleet",
    execution_timeout=timedelta(hours=1),
    retries=TASK_RETRIES,
    retry_delay=RETRY_DELAY,
    retry_exponential_backoff=True,
    max_retry_delay=timedelta(minutes=30),
    on_failure_callback=_notify_failure,
)
def audit_fleet() -> None:
    """Run the downloader's read-only whole-registry audit."""

    _run_boundary("audit")


@task(
    task_id="sync_fleet",
    execution_timeout=timedelta(hours=5),
    retries=TASK_RETRIES,
    retry_delay=RETRY_DELAY,
    retry_exponential_backoff=True,
    max_retry_delay=timedelta(minutes=30),
    on_failure_callback=_notify_failure,
    pool="estate_writer",
    pool_slots=1,
)
def sync_fleet() -> None:
    """Run one idempotent, non-overlapping whole-registry refresh."""

    _run_boundary("sync")


@task(
    task_id="drain_alpha_outbox",
    execution_timeout=timedelta(hours=4),
    retries=TASK_RETRIES,
    retry_delay=RETRY_DELAY,
    retry_exponential_backoff=True,
    max_retry_delay=timedelta(minutes=30),
    on_failure_callback=_notify_failure,
    pool="estate_writer",
    pool_slots=1,
)
def drain_alpha_outbox() -> None:
    """Derive searchable text and project every new estate document."""

    _run_boundary("drain")


@task(
    task_id="reconcile_alpha",
    execution_timeout=timedelta(hours=5),
    retries=TASK_RETRIES,
    retry_delay=RETRY_DELAY,
    retry_exponential_backoff=True,
    max_retry_delay=timedelta(minutes=30),
    on_failure_callback=_notify_failure,
    pool="estate_writer",
    pool_slots=1,
)
def reconcile_alpha() -> None:
    """Reconcile Alpha against the entire searchable estate."""

    _run_boundary("reconcile")


@task(
    task_id="audit_alpha_projection",
    execution_timeout=timedelta(hours=2),
    retries=TASK_RETRIES,
    retry_delay=RETRY_DELAY,
    retry_exponential_backoff=True,
    max_retry_delay=timedelta(minutes=30),
    on_failure_callback=_notify_failure,
    pool="estate_writer",
    pool_slots=1,
)
def audit_alpha_projection() -> None:
    """Hash-audit eligible estate text against Alpha's manifest and index."""

    _run_boundary("alpha-audit")


@task(
    task_id="check_consumer_status",
    execution_timeout=timedelta(minutes=15),
    retries=0,
    on_failure_callback=_notify_failure,
)
def check_consumer_status() -> None:
    """Fail when outbox delivery has backlog, expired work, or dead letters."""

    _run_boundary("consumer-status")


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
    sync_fleet() >> drain_alpha_outbox()


with DAG(
    dag_id="quarterly_estate_consume",
    description="Drain new estate documents into the estate-wide Alpha index.",
    start_date=START_DATE,
    schedule=os.environ.get("PDFS_AIRFLOW_CONSUMER_SCHEDULE", "*/15 * * * *"),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=True,
    tags=["estate", "consumer", "alpha-go", "write"],
) as quarterly_estate_consume:
    drain_alpha_outbox()


with DAG(
    dag_id="quarterly_estate_alpha_reconcile",
    description="Daily whole-estate Alpha reconciliation and delivery health gate.",
    start_date=START_DATE,
    schedule=os.environ.get("PDFS_AIRFLOW_RECONCILE_SCHEDULE", "43 3 * * *"),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=True,
    tags=["estate", "consumer", "alpha-go", "reconcile", "write"],
) as quarterly_estate_alpha_reconcile:
    reconcile_alpha() >> audit_alpha_projection() >> check_consumer_status()
