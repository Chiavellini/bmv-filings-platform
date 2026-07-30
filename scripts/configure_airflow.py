#!/usr/bin/env python3
"""Create a local, untracked native-Airflow environment for one estate."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import secrets
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "deploy" / "airflow" / "native" / ".env"
DEFAULT_AIRFLOW_HOME = Path.home() / ".local" / "state" / "bmv-filings-airflow"


def _dotenv(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("dotenv values cannot contain line breaks")
    return json.dumps(value)


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def render_environment(
    estate: Path,
    *,
    airflow_home: Path,
    metadata_url: str | None = None,
) -> str:
    """Render settings for a native Airflow process.

    SQLite is the default metadata backend for a first-computer trial.
    Airflow 3.3 uses LocalExecutor for both the trial and PostgreSQL-backed
    always-on installation.
    """

    selected_metadata = metadata_url or (
        "sqlite:///" + str((airflow_home / "airflow.db").resolve())
    )
    values = {
        "AIRFLOW_HOME": str(airflow_home),
        "AIRFLOW__CORE__DAGS_FOLDER": str(
            PROJECT_ROOT / "deploy" / "airflow" / "dags"
        ),
        "AIRFLOW__CORE__EXECUTOR": "LocalExecutor",
        "AIRFLOW__CORE__LOAD_EXAMPLES": "false",
        "AIRFLOW__CORE__DEFAULT_TIMEZONE": "America/Mexico_City",
        "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": (
            "http://127.0.0.1:8080/execution/"
        ),
        "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS": "admin:admin",
        "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE": str(
            airflow_home / "simple_auth_manager_passwords.json"
        ),
        "AIRFLOW__CORE__FERNET_KEY": _fernet_key(),
        "AIRFLOW__API_AUTH__JWT_SECRET": secrets.token_urlsafe(48),
        "AIRFLOW__API_AUTH__JWT_ISSUER": "bmv-filings-airflow",
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": selected_metadata,
        "AIRFLOW__SCHEDULER__ENABLE_HEALTH_CHECK": "true",
        "PDFS_PROJECT_ROOT": str(PROJECT_ROOT),
        "PDFS_DOCUMENT_ESTATE": str(estate),
        "PDFS_REPORTS_DIR": str(estate / "views" / "reports"),
        "PDFS_AIRFLOW_SYNC_ENABLED": "false",
        "PDFS_AIRFLOW_ALLOW_COVERAGE_GAPS": "true",
        "PDFS_AIRFLOW_RECHECK_LATEST": "2",
        "PDFS_AIRFLOW_ONLY": "",
        "PDFS_AIRFLOW_AUDIT_SCHEDULE": "0 7 * * 1-5",
        "PDFS_AIRFLOW_SYNC_SCHEDULE": "17 */6 * * *",
    }
    heading = (
        "# Generated locally by scripts/configure_airflow.py.\n"
        "# Contains secrets and must not be committed.\n"
    )
    return heading + "".join(
        f"{name}={_dotenv(value)}\n" for name, value in values.items()
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure native Airflow for an external document estate."
    )
    parser.add_argument(
        "--estate",
        type=Path,
        required=True,
        help="existing absolute document-estate directory on the host",
    )
    parser.add_argument(
        "--airflow-home",
        type=Path,
        default=DEFAULT_AIRFLOW_HOME,
        help="local Airflow state directory (not the document estate)",
    )
    parser.add_argument(
        "--metadata-url",
        help=(
            "Airflow metadata SQLAlchemy URL; omit for a local SQLite trial, "
            "use PostgreSQL for an always-on installation"
        ),
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output file",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    estate = args.estate.expanduser()
    if not estate.is_absolute():
        raise SystemExit("--estate must be an absolute path")
    estate = estate.resolve()
    if not estate.is_dir():
        raise SystemExit(f"estate directory does not exist: {estate}")

    airflow_home = args.airflow_home.expanduser().resolve()
    airflow_home.mkdir(parents=True, exist_ok=True)
    output = args.output.expanduser().resolve()
    if output.exists() and not args.force:
        raise SystemExit(f"refusing to replace existing file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_environment(
            estate,
            airflow_home=airflow_home,
            metadata_url=args.metadata_url,
        ),
        encoding="utf-8",
    )
    output.chmod(0o600)

    print(f"Wrote {output}")
    print(f"Airflow state: {airflow_home}")
    print("Applied sync remains disabled; initialize Airflow and start with audit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
