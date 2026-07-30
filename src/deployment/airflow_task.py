"""Airflow-safe process boundary for the quarterly acquisition CLI.

The Airflow DAG calls this wrapper as a subprocess.  This module deliberately
does not import acquisition internals: it validates deployment settings and
then replaces itself with the public ``refresh-quarterly-estate`` executable.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Mapping, Sequence


_ISSUER_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
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


def build_command(
    mode: str,
    *,
    dag_id: str,
    run_id: str,
    environment: Mapping[str, str] | None = None,
    executable: str = "refresh-quarterly-estate",
) -> list[str]:
    """Build the exact public-CLI argument vector for one Airflow task."""

    env = os.environ if environment is None else environment
    if mode not in {"audit", "sync"}:
        raise AirflowTaskConfigurationError("mode must be audit or sync")

    root = Path(env.get("PDFS_DOCUMENT_ESTATE", "/estate")).expanduser()
    if not root.is_absolute():
        raise AirflowTaskConfigurationError(
            "PDFS_DOCUMENT_ESTATE must be an absolute path"
        )
    if not root.is_dir():
        raise AirflowTaskConfigurationError(
            f"estate mount is not a directory: {root}"
        )

    recheck = _bounded_integer(
        env,
        "PDFS_AIRFLOW_RECHECK_LATEST",
        default=2,
        minimum=0,
        maximum=8,
    )
    command = [
        executable,
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
            default=True,
        ):
            command.append("--allow-coverage-gaps")

    command.append("--json")
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="airflow-quarterly-estate",
        description="Validate Airflow settings and invoke the acquisition CLI.",
    )
    parser.add_argument("mode", choices=("audit", "sync"))
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
