"""Portable Airflow 3 DAGs for the quarterly document estate."""

from __future__ import annotations

from datetime import timedelta
import os
import subprocess
import sys

import pendulum
from airflow.sdk import DAG, get_current_context, task


TIMEZONE = "America/Mexico_City"
START_DATE = pendulum.datetime(2026, 1, 1, tz=TIMEZONE)


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
    sync_fleet()
