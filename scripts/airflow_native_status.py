#!/usr/bin/env python3
"""Read-only health report for the native Airflow estate deployment."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENVIRONMENT = PROJECT_ROOT / "deploy" / "airflow" / "native" / ".env"
COMPONENTS = ("api-server", "scheduler", "dag-processor")


def _load_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid dotenv line {line_number}")
        name, encoded = line.split("=", 1)
        name = name.strip()
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"dotenv value on line {line_number} is not JSON quoted"
            ) from exc
        if not name or not isinstance(value, str):
            raise ValueError(f"invalid dotenv assignment on line {line_number}")
        values[name] = value
    return values


def _run(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout: int = 30,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **environment},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    detail = (result.stdout or result.stderr).strip()
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "detail": detail[-2000:],
    }


def _service_check(component: str) -> dict[str, Any]:
    system = platform.system()
    if system == "Darwin":
        command = (
            "launchctl",
            "print",
            f"gui/{os.getuid()}/com.bmv.airflow.{component}",
        )
    elif system == "Linux":
        command = (
            "systemctl",
            "is-active",
            f"bmv-airflow@{component}.service",
        )
    else:
        return {"ok": False, "detail": f"unsupported supervisor OS: {system}"}
    result = _run(command, environment={})
    if system == "Darwin" and result.get("ok"):
        result["ok"] = "state = running" in str(result.get("detail"))
        if not result["ok"]:
            result["detail"] = "launchd job is loaded but not running"
    return result


def inspect(environment_file: Path, *, runtime: bool = True) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    environment_file = environment_file.expanduser().resolve()
    if not environment_file.is_file():
        return {
            "ok": False,
            "environment": str(environment_file),
            "checks": {
                "environment_file": {
                    "ok": False,
                    "detail": "generated Airflow environment is missing",
                }
            },
        }
    try:
        values = _load_environment(environment_file)
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "environment": str(environment_file),
            "checks": {
                "environment_file": {
                    "ok": False,
                    "detail": str(exc),
                }
            },
        }

    mode = environment_file.stat().st_mode & 0o777
    checks["environment_file"] = {
        "ok": mode & 0o077 == 0,
        "detail": f"mode {mode:04o}; expected owner-only",
    }

    def path_check(name: str, *, directory: bool = False) -> Path | None:
        raw = values.get(name, "").strip()
        path = Path(raw).expanduser() if raw else None
        ok = bool(
            path
            and path.is_absolute()
            and (path.is_dir() if directory else path.is_file())
        )
        checks[name] = {
            "ok": ok,
            "detail": str(path) if path else "missing",
        }
        return path if ok else None

    project = path_check("PDFS_PROJECT_ROOT", directory=True)
    estate = path_check("PDFS_DOCUMENT_ESTATE", directory=True)
    dags = path_check("AIRFLOW__CORE__DAGS_FOLDER", directory=True)
    airflow_venv = path_check("PDFS_AIRFLOW_VENV", directory=True)
    airflow = airflow_venv / "bin" / "airflow" if airflow_venv else None
    checks["airflow_executable"] = {
        "ok": bool(airflow and airflow.is_file() and os.access(airflow, os.X_OK)),
        "detail": str(airflow) if airflow else "missing",
    }
    catalog = estate / "catalog.db" if estate else None
    checks["estate_catalog"] = {
        "ok": bool(catalog and catalog.is_file()),
        "detail": str(catalog) if catalog else "missing",
    }
    dag_file = dags / "quarterly_estate.py" if dags else None
    checks["quarterly_dag"] = {
        "ok": bool(dag_file and dag_file.is_file()),
        "detail": str(dag_file) if dag_file else "missing",
    }
    checks["strict_coverage"] = {
        "ok": values.get("PDFS_AIRFLOW_ALLOW_COVERAGE_GAPS", "").lower()
        in {"0", "false", "no", "off"},
        "detail": values.get("PDFS_AIRFLOW_ALLOW_COVERAGE_GAPS", "missing"),
    }
    for gate in ("PDFS_AIRFLOW_SYNC_ENABLED", "PDFS_AIRFLOW_ALPHA_ENABLED"):
        checks[gate] = {
            "ok": values.get(gate, "").lower() in {"1", "true", "yes", "on"},
            "detail": values.get(gate, "missing"),
        }
    target_id = values.get("PDFS_ALPHA_TARGET_ID", "").strip()
    checks["PDFS_ALPHA_TARGET_ID"] = {
        "ok": bool(target_id),
        "detail": target_id or "missing",
    }
    webhook = urlsplit(values.get("PDFS_AIRFLOW_ALERT_WEBHOOK", ""))
    checks["failure_alert"] = {
        "ok": webhook.scheme == "https" and bool(webhook.netloc),
        "detail": (
            "configured"
            if values.get("PDFS_AIRFLOW_ALERT_WEBHOOK")
            else "missing"
        ),
    }

    for name, directory in (
        ("PDFS_ALPHA_ROOT", True),
        ("PDFS_ALPHA_CORPUS", True),
        ("PDFS_ALPHA_INDEX", False),
        ("PDFS_ALPHA_CONFIG", False),
        ("PDFS_ALPHA_PYTHON", False),
    ):
        path_check(name, directory=directory)
    alpha_python = Path(values.get("PDFS_ALPHA_PYTHON", ""))
    if checks["PDFS_ALPHA_PYTHON"]["ok"]:
        checks["PDFS_ALPHA_PYTHON"]["ok"] = os.access(alpha_python, os.X_OK)
    if estate and checks["PDFS_ALPHA_CORPUS"]["ok"]:
        alpha_corpus = Path(values["PDFS_ALPHA_CORPUS"]).resolve()
        if alpha_corpus == estate.resolve():
            checks["PDFS_ALPHA_CORPUS"] = {
                "ok": False,
                "detail": "must be a projection directory, not the estate root",
            }
    if catalog and checks["PDFS_ALPHA_INDEX"]["ok"]:
        alpha_index = Path(values["PDFS_ALPHA_INDEX"]).resolve()
        if alpha_index == catalog.resolve():
            checks["PDFS_ALPHA_INDEX"] = {
                "ok": False,
                "detail": "must not point at the estate catalog",
            }

    if runtime:
        if platform.system() == "Darwin":
            legacy = _run(
                (
                    "launchctl",
                    "print",
                    f"gui/{os.getuid()}/com.bmv.watch",
                ),
                environment={},
            )
            checks["legacy_scheduler_conflict"] = {
                "ok": not legacy.get("ok"),
                "detail": (
                    "legacy com.bmv.watch is still loaded"
                    if legacy.get("ok")
                    else "not loaded"
                ),
            }
        for component in COMPONENTS:
            checks[f"service.{component}"] = _service_check(component)
        if airflow and checks["airflow_executable"]["ok"]:
            checks["scheduler_job"] = _run(
                (
                    str(airflow),
                    "jobs",
                    "check",
                    "--job-type",
                    "SchedulerJob",
                    "--local",
                ),
                environment=values,
            )
            dag_imports = _run(
                (
                    str(airflow),
                    "dags",
                    "list-import-errors",
                    "--local",
                    "--output",
                    "json",
                ),
                environment=values,
                timeout=60,
            )
            if dag_imports.get("ok"):
                detail = str(dag_imports.get("detail") or "")
                try:
                    parsed = json.loads(detail) if detail else []
                except json.JSONDecodeError:
                    parsed = [] if "No data found" in detail else [detail]
                if parsed:
                    dag_imports["ok"] = False
                    dag_imports["detail"] = "Airflow reports DAG import errors"
            checks["dag_imports"] = dag_imports
            dag_state = _run(
                (
                    str(airflow),
                    "dags",
                    "list",
                    "--output",
                    "json",
                ),
                environment=values,
                timeout=60,
            )
            expected_dags = {
                "quarterly_estate_audit",
                "quarterly_estate_sync",
                "quarterly_estate_consume",
                "quarterly_estate_alpha_reconcile",
            }
            if dag_state.get("ok"):
                try:
                    rows = json.loads(str(dag_state.get("detail") or ""))
                    state = {
                        str(row["dag_id"]): row.get("is_paused")
                        for row in rows
                        if isinstance(row, dict) and "dag_id" in row
                    }
                except (TypeError, json.JSONDecodeError):
                    state = {}
                missing = sorted(expected_dags - state.keys())
                paused = sorted(
                    dag_id
                    for dag_id in expected_dags & state.keys()
                    if str(state[dag_id]).strip().lower() not in {"false", "0"}
                )
                if missing or paused:
                    dag_state["ok"] = False
                    dag_state["detail"] = json.dumps(
                        {"missing": missing, "paused": paused},
                        separators=(",", ":"),
                    )
            checks["scheduled_dags"] = dag_state
            pool = _run(
                (
                    str(airflow),
                    "pools",
                    "get",
                    "estate_writer",
                    "--output",
                    "json",
                ),
                environment=values,
            )
            if pool.get("ok"):
                try:
                    pool_payload = json.loads(str(pool.get("detail") or ""))
                    pool_row = (
                        pool_payload[0]
                        if isinstance(pool_payload, list) and pool_payload
                        else pool_payload
                    )
                    slots = int(pool_row.get("slots"))
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    slots = -1
                if slots != 1:
                    pool["ok"] = False
                    pool["detail"] = (
                        "estate_writer pool must exist with exactly one slot"
                    )
            checks["estate_writer_pool"] = pool
        consumer = airflow_venv / "bin" / "process-estate-outbox" if airflow_venv else None
        if consumer and consumer.is_file() and estate and catalog:
            checks["consumer_status"] = _run(
                (
                    str(consumer),
                    "status",
                    "--estate-root",
                    str(estate),
                    "--database",
                    str(catalog),
                    "--json",
                ),
                environment=values,
                timeout=60,
            )
        else:
            checks["consumer_status"] = {
                "ok": False,
                "detail": "consumer executable or estate catalog is missing",
            }

    return {
        "ok": all(check.get("ok") is True for check in checks.values()),
        "environment": str(environment_file),
        "project_root": str(project) if project else None,
        "estate_root": str(estate) if estate else None,
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only native Airflow, scheduler, and consumer health check."
    )
    parser.add_argument("--environment", type=Path, default=DEFAULT_ENVIRONMENT)
    parser.add_argument(
        "--static",
        action="store_true",
        help="validate files and gates without querying the running supervisor",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = inspect(args.environment, runtime=not args.static)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("READY" if report["ok"] else "NOT READY")
        for name, check in report["checks"].items():
            marker = "PASS" if check.get("ok") else "FAIL"
            print(f"[{marker}] {name}: {check.get('detail', '')}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
