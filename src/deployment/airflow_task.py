"""Airflow-safe process boundary for acquisition and search projection.

The Airflow DAG calls this wrapper as a subprocess.  This module deliberately
does not import acquisition or consumer internals: it validates deployment
settings and then replaces itself with the appropriate public executable.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Mapping, Sequence

from estate_volume import inspect_estate_environment


_ISSUER_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_TARGET_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ACQUISITION_MODES = frozenset({"audit", "sync"})
_CONSUMER_MODES = frozenset(
    {"drain", "reconcile", "alpha-audit", "consumer-status"}
)
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class AirflowTaskConfigurationError(RuntimeError):
    """The task environment is incomplete or unsafe."""


def _boolean(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> bool:
    raw = environment.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise AirflowTaskConfigurationError(
        f"{name} must be one of: {', '.join(sorted(_TRUE | _FALSE))}"
    )


def _bounded_integer(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environment.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise AirflowTaskConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise AirflowTaskConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _issuer_scope(environment: Mapping[str, str]) -> tuple[str, ...]:
    raw = environment.get("PDFS_AIRFLOW_ONLY", "")
    values = tuple(token.strip() for token in raw.split(",") if token.strip())
    invalid = tuple(token for token in values if not _ISSUER_TOKEN.fullmatch(token))
    if invalid:
        raise AirflowTaskConfigurationError(
            "PDFS_AIRFLOW_ONLY contains invalid issuer token(s): "
            + ", ".join(invalid)
        )
    return values


def _trigger_part(value: str, *, field: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.:+@=-]+", "_", value.strip())
    if not normalized:
        raise AirflowTaskConfigurationError(f"{field} cannot be empty")
    return normalized[:160]


def _required_absolute_path(
    environment: Mapping[str, str],
    name: str,
    *,
    kind: str,
) -> Path:
    raw = environment.get(name, "").strip()
    if not raw:
        raise AirflowTaskConfigurationError(f"{name} is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise AirflowTaskConfigurationError(f"{name} must be an absolute path")
    path = path.resolve()
    valid = {
        "directory": path.is_dir(),
        "file": path.is_file(),
        "executable": path.is_file() and os.access(path, os.X_OK),
    }.get(kind)
    if valid is None:
        raise ValueError(f"unsupported path kind: {kind}")
    if not valid:
        raise AirflowTaskConfigurationError(
            f"{name} is not an existing {kind}: {path}"
        )
    return path


def _alpha_settings(
    environment: Mapping[str, str],
    *,
    estate_root: Path,
) -> dict[str, str]:
    if not _boolean(
        environment,
        "PDFS_AIRFLOW_ALPHA_ENABLED",
        default=False,
    ):
        raise AirflowTaskConfigurationError(
            "Alpha estate projection is disabled; set "
            "PDFS_AIRFLOW_ALPHA_ENABLED=true only after every Alpha path is verified"
        )
    target_id = environment.get("PDFS_ALPHA_TARGET_ID", "").strip()
    if not _TARGET_TOKEN.fullmatch(target_id):
        raise AirflowTaskConfigurationError(
            "PDFS_ALPHA_TARGET_ID must be a stable 1-128 character token"
        )
    settings = {
        "root": str(
            _required_absolute_path(
                environment, "PDFS_ALPHA_ROOT", kind="directory"
            )
        ),
        "corpus": str(
            _required_absolute_path(
                environment, "PDFS_ALPHA_CORPUS", kind="directory"
            )
        ),
        "index": str(
            _required_absolute_path(environment, "PDFS_ALPHA_INDEX", kind="file")
        ),
        "config": str(
            _required_absolute_path(environment, "PDFS_ALPHA_CONFIG", kind="file")
        ),
        "python": str(
            _required_absolute_path(
                environment, "PDFS_ALPHA_PYTHON", kind="executable"
            )
        ),
        "target_id": target_id,
    }
    if Path(settings["corpus"]) == estate_root.resolve():
        raise AirflowTaskConfigurationError(
            "PDFS_ALPHA_CORPUS must be an explicit projection directory, not "
            "the document-estate root"
        )
    if Path(settings["index"]) == (estate_root / "catalog.db").resolve():
        raise AirflowTaskConfigurationError(
            "PDFS_ALPHA_INDEX must not point at the estate catalog"
        )
    return settings


def build_command(
    mode: str,
    *,
    dag_id: str,
    run_id: str,
    environment: Mapping[str, str] | None = None,
    executable: str | None = None,
) -> list[str]:
    """Build the exact public-CLI argument vector for one Airflow task."""

    env = os.environ if environment is None else environment
    if mode not in _ACQUISITION_MODES | _CONSUMER_MODES:
        raise AirflowTaskConfigurationError(
            "mode must be audit, sync, drain, reconcile, alpha-audit, or "
            "consumer-status"
        )

    root = Path(env.get("PDFS_DOCUMENT_ESTATE", "/estate")).expanduser()
    if not root.is_absolute():
        raise AirflowTaskConfigurationError(
            "PDFS_DOCUMENT_ESTATE must be an absolute path"
        )
    if not root.is_dir():
        raise AirflowTaskConfigurationError(
            f"estate mount is not a directory: {root}"
        )
    volume = inspect_estate_environment(root, dict(env))
    if not volume.healthy:
        raise AirflowTaskConfigurationError("; ".join(volume.problems))

    if mode in _ACQUISITION_MODES:
        recheck = _bounded_integer(
            env,
            "PDFS_AIRFLOW_RECHECK_LATEST",
            default=2,
            minimum=0,
            maximum=8,
        )
        command = [
            executable or "refresh-quarterly-estate",
            mode,
            "--estate-root",
            str(root),
            "--db",
            str(root / "catalog.db"),
            "--recheck-latest",
            str(recheck),
        ]
        for issuer in _issuer_scope(env):
            command.extend(("--only", issuer))
    else:
        # Generic outbox status is deliberately inspectable even when Alpha is
        # disabled or misconfigured.  Mutating and Alpha-specific modes remain
        # gated and validate the complete target before constructing argv.
        alpha = (
            _alpha_settings(env, estate_root=root)
            if mode != "consumer-status"
            else None
        )
        if not (root / "catalog.db").is_file():
            raise AirflowTaskConfigurationError(
                f"estate catalog is missing: {root / 'catalog.db'}"
            )
        command = [
            executable or "process-estate-outbox",
            (
                "run"
                if mode == "drain"
                else "reconcile-alpha"
                if mode == "reconcile"
                else "audit-alpha"
                if mode == "alpha-audit"
                else "status"
            ),
            "--estate-root",
            str(root),
            "--database",
            str(root / "catalog.db"),
        ]
        if mode in {"drain", "reconcile"}:
            command.extend(("--apply",))
        if mode == "drain":
            maximum = _bounded_integer(
                env,
                "PDFS_AIRFLOW_MAX_DELIVERIES",
                default=500,
                minimum=1,
                maximum=10_000,
            )
            command.extend(("--max-deliveries", str(maximum), "--enable-alpha-go"))
        if mode in {"drain", "reconcile", "alpha-audit"}:
            assert alpha is not None
            command.extend(
                (
                    "--alpha-root",
                    alpha["root"],
                    "--alpha-corpus",
                    alpha["corpus"],
                    "--alpha-index",
                    alpha["index"],
                    "--alpha-config",
                    alpha["config"],
                    "--alpha-target-id",
                    alpha["target_id"],
                    "--alpha-python",
                    alpha["python"],
                )
            )
        if mode == "drain":
            command.append("--require-drained")

    if mode == "sync":
        if not _boolean(
            env,
            "PDFS_AIRFLOW_SYNC_ENABLED",
            default=False,
        ):
            raise AirflowTaskConfigurationError(
                "applied sync is disabled; set PDFS_AIRFLOW_SYNC_ENABLED=true "
                "and unpause the DAG after the read-only audit succeeds"
            )
        command.extend(
            (
                "--trigger",
                "airflow:"
                + _trigger_part(dag_id, field="dag_id")
                + ":"
                + _trigger_part(run_id, field="run_id"),
                "--apply",
            )
        )
        if _boolean(
            env,
            "PDFS_AIRFLOW_ALLOW_COVERAGE_GAPS",
            default=False,
        ):
            command.append("--allow-coverage-gaps")

    command.append("--json")
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airflow-quarterly-estate",
        description=(
            "Validate Airflow settings and invoke acquisition/search CLIs."
        ),
    )
    parser.add_argument(
        "mode",
        choices=(
            "audit",
            "sync",
            "drain",
            "reconcile",
            "alpha-audit",
            "consumer-status",
        ),
    )
    parser.add_argument("--dag-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--print-command",
        action="store_true",
        help="validate and print the argument vector without executing it",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        command = build_command(
            args.mode,
            dag_id=args.dag_id,
            run_id=args.run_id,
        )
    except AirflowTaskConfigurationError as exc:
        raise SystemExit(str(exc)) from exc

    if args.print_command:
        print("\n".join(command))
        return 0

    executable = shutil.which(command[0])
    if executable is None:
        sibling = Path(sys.executable).with_name(command[0])
        executable = str(sibling) if sibling.is_file() else None
    if executable is None:
        raise SystemExit(f"required executable is not installed: {command[0]}")
    os.execv(executable, command)
    return 127  # pragma: no cover - os.execv does not return


__all__ = [
    "AirflowTaskConfigurationError",
    "build_command",
    "build_parser",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - console script is primary path
    raise SystemExit(main())
