#!/usr/bin/env python3
"""Create a local, untracked native-Airflow environment for one estate."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import plistlib
import secrets
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "deploy" / "airflow" / "native" / ".env"
DEFAULT_AIRFLOW_HOME = Path.home() / ".local" / "state" / "bmv-filings-airflow"
DEFAULT_AIRFLOW_VENV = PROJECT_ROOT / ".airflow-venv"
LAUNCHD_COMPONENTS = ("api-server", "scheduler", "dag-processor")
SECRET_NAMES = (
    "AIRFLOW__CORE__FERNET_KEY",
    "AIRFLOW__API_AUTH__JWT_SECRET",
)


def _dotenv(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError("dotenv values cannot contain line breaks")
    return json.dumps(value)


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _existing_secrets(path: Path) -> dict[str, str]:
    """Read only generated secrets so --force never rotates them implicitly."""

    values: dict[str, str] = {}
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        name, encoded = raw.split("=", 1)
        if name not in SECRET_NAMES:
            continue
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"cannot preserve malformed secret on dotenv line {line_number}"
            ) from exc
        if not isinstance(value, str) or not value:
            raise ValueError(f"cannot preserve blank secret {name}")
        values[name] = value
    missing = sorted(set(SECRET_NAMES) - values.keys())
    if missing:
        raise ValueError(
            "cannot replace environment without preserving secret(s): "
            + ", ".join(missing)
        )
    return values


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_state_separation(
    estate: Path,
    *,
    airflow_home: Path,
    airflow_venv: Path,
    metadata_url: str | None,
) -> None:
    """Reject layouts that can corrupt or accidentally export local state."""

    for label, path in (
        ("Airflow home", airflow_home),
        ("Airflow virtual environment", airflow_venv),
    ):
        if _inside(path, estate):
            raise ValueError(f"{label} must be outside the document estate")

    if metadata_url is None:
        return
    if metadata_url.startswith("sqlite:///"):
        sqlite_token = metadata_url.removeprefix("sqlite:///")
        sqlite_path = Path(sqlite_token).expanduser()
        if not sqlite_path.is_absolute():
            raise ValueError("SQLite Airflow metadata URL must use an absolute path")
        if _inside(sqlite_path.resolve(), estate):
            raise ValueError("Airflow metadata database must be outside the document estate")
        return
    if not (
        metadata_url.startswith("postgresql://")
        or metadata_url.startswith("postgresql+psycopg2://")
    ):
        raise ValueError(
            "--metadata-url must be an absolute SQLite URL or PostgreSQL URL"
        )


def render_launchd_service(
    component: str,
    *,
    environment_file: Path,
    airflow_home: Path,
) -> bytes:
    """Render one path-resolved macOS launchd service without embedding secrets."""

    if component not in LAUNCHD_COMPONENTS:
        raise ValueError(f"unsupported Airflow component: {component}")
    label = f"com.bmv.airflow.{component}"
    log_dir = airflow_home / "service-logs"
    payload = {
        "Label": label,
        "ProgramArguments": [
            str(PROJECT_ROOT / "scripts" / "run_airflow_native.sh"),
            str(environment_file),
            component,
        ],
        "WorkingDirectory": str(PROJECT_ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / f"{component}.out.log"),
        "StandardErrorPath": str(log_dir / f"{component}.err.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def render_environment(
    estate: Path,
    *,
    airflow_home: Path,
    airflow_venv: Path = DEFAULT_AIRFLOW_VENV,
    metadata_url: str | None = None,
    secrets_override: dict[str, str] | None = None,
) -> str:
    """Render settings for a native Airflow process.

    SQLite is the default metadata backend for a first-computer trial.
    Airflow 3.3 uses LocalExecutor for both the trial and PostgreSQL-backed
    always-on installation.
    """

    estate = estate.expanduser().resolve()
    airflow_home = airflow_home.expanduser().resolve()
    airflow_venv = airflow_venv.expanduser().resolve()
    _validate_state_separation(
        estate,
        airflow_home=airflow_home,
        airflow_venv=airflow_venv,
        metadata_url=metadata_url,
    )

    selected_metadata = metadata_url or (
        "sqlite:///" + str((airflow_home / "airflow.db").resolve())
    )
    retained_secrets = secrets_override or {}
    values = {
        "AIRFLOW_HOME": str(airflow_home),
        "AIRFLOW__CORE__DAGS_FOLDER": str(
            PROJECT_ROOT / "deploy" / "airflow" / "dags"
        ),
        "AIRFLOW__CORE__EXECUTOR": "LocalExecutor",
        "AIRFLOW__CORE__PARALLELISM": "3",
        "AIRFLOW__CORE__LOAD_EXAMPLES": "false",
        "AIRFLOW__CORE__DEFAULT_TIMEZONE": "America/Mexico_City",
        "AIRFLOW__CORE__EXECUTION_API_SERVER_URL": (
            "http://127.0.0.1:8080/execution/"
        ),
        "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_USERS": "admin:admin",
        "AIRFLOW__CORE__SIMPLE_AUTH_MANAGER_PASSWORDS_FILE": str(
            airflow_home / "simple_auth_manager_passwords.json"
        ),
        "AIRFLOW__CORE__FERNET_KEY": retained_secrets.get(
            "AIRFLOW__CORE__FERNET_KEY", _fernet_key()
        ),
        "AIRFLOW__API_AUTH__JWT_SECRET": retained_secrets.get(
            "AIRFLOW__API_AUTH__JWT_SECRET", secrets.token_urlsafe(48)
        ),
        "AIRFLOW__API_AUTH__JWT_ISSUER": "bmv-filings-airflow",
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": selected_metadata,
        "AIRFLOW__SCHEDULER__ENABLE_HEALTH_CHECK": "true",
        "PDFS_PROJECT_ROOT": str(PROJECT_ROOT),
        "PDFS_AIRFLOW_VENV": str(airflow_venv),
        "PDFS_DOCUMENT_ESTATE": str(estate),
        "PDFS_REPORTS_DIR": str(estate / "views" / "reports"),
        "PDFS_AIRFLOW_SYNC_ENABLED": "false",
        "PDFS_AIRFLOW_ALLOW_COVERAGE_GAPS": "false",
        "PDFS_AIRFLOW_RECHECK_LATEST": "2",
        "PDFS_AIRFLOW_ONLY": "",
        "PDFS_AIRFLOW_TASK_RETRIES": "2",
        "PDFS_AIRFLOW_RETRY_DELAY_SECONDS": "300",
        "PDFS_AIRFLOW_ALERT_WEBHOOK": "",
        "PDFS_AIRFLOW_ALPHA_ENABLED": "false",
        "PDFS_AIRFLOW_MAX_DELIVERIES": "500",
        "PDFS_AIRFLOW_PDF_PARSER_VERSION": "1",
        "PDFS_AIRFLOW_XBRL_PROCESSOR_VERSION": "1",
        "PDFS_AIRFLOW_ALPHA_TIMEOUT_SECONDS": "1800",
        # These stay blank intentionally.  In particular, choosing the estate
        # root versus Alpha's legacy corpus can trigger a full reindex.  The
        # operator must select and verify every absolute path before enabling.
        "PDFS_ALPHA_ROOT": "",
        "PDFS_ALPHA_CORPUS": "",
        "PDFS_ALPHA_INDEX": "",
        "PDFS_ALPHA_CONFIG": "",
        "PDFS_ALPHA_PYTHON": "",
        "PDFS_ALPHA_TARGET_ID": "",
        "PDFS_AIRFLOW_AUDIT_SCHEDULE": "0 7 * * 1-5",
        "PDFS_AIRFLOW_SYNC_SCHEDULE": "17 */6 * * *",
        "PDFS_AIRFLOW_CONSUMER_SCHEDULE": "*/15 * * * *",
        "PDFS_AIRFLOW_RECONCILE_SCHEDULE": "43 3 * * *",
    }
    heading = (
        "# Generated locally by scripts/configure_airflow.py.\n"
        "# Contains secrets and must not be committed.\n"
        "# Alpha paths are blank intentionally; choose the certified corpus/index\n"
        "# explicitly before setting PDFS_AIRFLOW_ALPHA_ENABLED=true.\n"
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
    parser.add_argument(
        "--airflow-venv",
        type=Path,
        default=DEFAULT_AIRFLOW_VENV,
        help="absolute native Airflow virtual-environment directory",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--launchd-output-dir",
        type=Path,
        help=(
            "also render api-server, scheduler, and dag-processor plists into "
            "this directory; rendering does not load or start them"
        ),
    )
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
    airflow_venv = args.airflow_venv.expanduser()
    if not airflow_venv.is_absolute():
        raise SystemExit("--airflow-venv must be an absolute path")
    airflow_venv = airflow_venv.resolve()
    if not (airflow_venv / "bin" / "airflow").is_file():
        raise SystemExit(
            "Airflow is not installed in --airflow-venv; run "
            "scripts/install_airflow_native.sh first"
        )
    try:
        _validate_state_separation(
            estate,
            airflow_home=airflow_home,
            airflow_venv=airflow_venv,
            metadata_url=args.metadata_url,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    airflow_home.mkdir(parents=True, exist_ok=True)
    output = args.output.expanduser().resolve()
    if _inside(output, estate):
        raise SystemExit("--output must be outside the document estate")
    if output.exists() and not args.force:
        raise SystemExit(f"refusing to replace existing file: {output}")
    retained_secrets: dict[str, str] = {}
    if output.exists():
        try:
            retained_secrets = _existing_secrets(output)
        except (OSError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
    launchd_dir = (
        args.launchd_output_dir.expanduser().resolve()
        if args.launchd_output_dir is not None
        else None
    )
    launchd_outputs = (
        {
            component: launchd_dir / f"com.bmv.airflow.{component}.plist"
            for component in LAUNCHD_COMPONENTS
        }
        if launchd_dir is not None
        else {}
    )
    existing_plists = [path for path in launchd_outputs.values() if path.exists()]
    if existing_plists and not args.force:
        raise SystemExit(
            "refusing to replace existing launchd file(s): "
            + ", ".join(map(str, existing_plists))
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_environment(
            estate,
            airflow_home=airflow_home,
            airflow_venv=airflow_venv,
            metadata_url=args.metadata_url,
            secrets_override=retained_secrets,
        ),
        encoding="utf-8",
    )
    output.chmod(0o600)
    if launchd_dir is not None:
        launchd_dir.mkdir(parents=True, exist_ok=True)
        (airflow_home / "service-logs").mkdir(parents=True, exist_ok=True)
        for component, path in launchd_outputs.items():
            path.write_bytes(
                render_launchd_service(
                    component,
                    environment_file=output,
                    airflow_home=airflow_home,
                )
            )
            path.chmod(0o644)

    print(f"Wrote {output}")
    print(f"Airflow state: {airflow_home}")
    for path in launchd_outputs.values():
        print(f"Rendered (not loaded): {path}")
    print("Applied sync remains disabled; initialize Airflow and start with audit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
