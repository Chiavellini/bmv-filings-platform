from __future__ import annotations

import ast
import json
from pathlib import Path
import plistlib
import sys

import pytest

from scripts.airflow_native_status import inspect as inspect_airflow
from scripts.configure_airflow import (
    main as configure_airflow,
    render_environment,
    render_launchd_service,
)
from src.deployment.airflow_task import (
    AirflowTaskConfigurationError,
    build_command,
)


ROOT = Path(__file__).resolve().parents[1]
ESTATE_ID = "56EC1117-38A4-4F38-A135-408F04AF9A89"
DAG_PATH = ROOT / "deploy" / "airflow" / "dags" / "quarterly_estate.py"
NATIVE_ENV_PATH = ROOT / "deploy" / "airflow" / "native" / ".env.example"
SYSTEMD_PATH = (
    ROOT / "deploy" / "airflow" / "native" / "systemd" / "bmv-airflow@.service"
)
INSTALLER_PATH = ROOT / "scripts" / "install_airflow_native.sh"
RUNNER_PATH = ROOT / "scripts" / "run_airflow_native.sh"
STATUS_PATH = ROOT / "scripts" / "airflow_native_status.py"
RELEASE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release-gate.yml"
AIRFLOW_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "airflow-native.yml"


def _environment(estate: Path, **overrides: str) -> dict[str, str]:
    values = {
        "PDFS_DOCUMENT_ESTATE": str(estate),
        "PDFS_AIRFLOW_SYNC_ENABLED": "false",
    }
    values.update(overrides)
    return values


def _write_sentinel(estate: Path) -> None:
    estate.mkdir(parents=True, exist_ok=True)
    (estate / ".bmv-estate-volume.json").write_text(
        json.dumps({"schema_version": 1, "estate_id": ESTATE_ID}),
        encoding="utf-8",
    )


def _alpha_environment(tmp_path: Path, **overrides: str) -> dict[str, str]:
    estate = tmp_path / "estate"
    alpha = tmp_path / "alpha-go"
    _write_sentinel(estate)
    alpha.mkdir(parents=True)
    index = estate / "alpha.db"
    catalog = estate / "catalog.db"
    corpus = tmp_path / "alpha-corpus"
    config = alpha / "alpha.yaml"
    index.touch()
    catalog.touch()
    corpus.mkdir()
    config.write_text("version: 1\n", encoding="utf-8")
    values = _environment(
        estate,
        PDFS_AIRFLOW_ALPHA_ENABLED="true",
        PDFS_ALPHA_ROOT=str(alpha),
        PDFS_ALPHA_CORPUS=str(corpus),
        PDFS_ALPHA_INDEX=str(index),
        PDFS_ALPHA_CONFIG=str(config),
        PDFS_ALPHA_PYTHON=sys.executable,
        PDFS_ALPHA_TARGET_ID="portable-estate-v1",
    )
    values.update(overrides)
    return values


def test_audit_command_is_read_only_and_scoped(tmp_path: Path) -> None:
    command = build_command(
        "audit",
        dag_id="quarterly_estate_audit",
        run_id="scheduled__2026-07-30T07:00:00+00:00",
        environment=_environment(
            tmp_path,
            PDFS_AIRFLOW_ONLY="femsa,gfnorte",
        ),
    )

    assert command[0:2] == ["refresh-quarterly-estate", "audit"]
    assert "--apply" not in command
    assert command.count("--only") == 2
    assert command[-1] == "--json"


def test_sync_is_double_gated_and_uses_one_fleet_cli(tmp_path: Path) -> None:
    with pytest.raises(AirflowTaskConfigurationError, match="disabled"):
        build_command(
            "sync",
            dag_id="quarterly_estate_sync",
            run_id="manual__canary",
            environment=_environment(tmp_path),
        )

    command = build_command(
        "sync",
        dag_id="quarterly_estate_sync",
        run_id="manual__canary",
        environment=_environment(
            tmp_path,
            PDFS_AIRFLOW_SYNC_ENABLED="true",
        ),
    )

    assert command[0:2] == ["refresh-quarterly-estate", "sync"]
    assert "--apply" in command
    assert "--allow-coverage-gaps" not in command
    assert command[command.index("--trigger") + 1].startswith(
        "airflow:quarterly_estate_sync:"
    )


def test_sync_coverage_opt_out_is_explicit(tmp_path: Path) -> None:
    command = build_command(
        "sync",
        dag_id="quarterly_estate_sync",
        run_id="manual__canary",
        environment=_environment(
            tmp_path,
            PDFS_AIRFLOW_SYNC_ENABLED="true",
            PDFS_AIRFLOW_ALLOW_COVERAGE_GAPS="true",
        ),
    )

    assert "--apply" in command
    assert "--allow-coverage-gaps" in command


def test_alpha_drain_is_applied_bounded_and_fail_closed(tmp_path: Path) -> None:
    command = build_command(
        "drain",
        dag_id="quarterly_estate_sync",
        run_id="scheduled__alpha",
        environment=_alpha_environment(
            tmp_path,
            PDFS_AIRFLOW_MAX_DELIVERIES="750",
        ),
    )

    assert command[0:2] == ["process-estate-outbox", "run"]
    for flag in ("--apply", "--enable-alpha-go", "--require-drained", "--json"):
        assert flag in command
    assert command[command.index("--max-deliveries") + 1] == "750"


def test_alpha_reconcile_and_status_are_explicit(tmp_path: Path) -> None:
    environment = _alpha_environment(tmp_path)
    reconcile = build_command(
        "reconcile",
        dag_id="quarterly_estate_alpha_reconcile",
        run_id="scheduled__alpha",
        environment=environment,
    )
    status = build_command(
        "consumer-status",
        dag_id="quarterly_estate_alpha_reconcile",
        run_id="scheduled__alpha",
        environment=_environment(Path(environment["PDFS_DOCUMENT_ESTATE"])),
    )
    alpha_audit = build_command(
        "alpha-audit",
        dag_id="quarterly_estate_alpha_reconcile",
        run_id="scheduled__alpha",
        environment=environment,
    )

    assert reconcile[0:2] == ["process-estate-outbox", "reconcile-alpha"]
    assert "--apply" in reconcile
    assert reconcile[reconcile.index("--alpha-target-id") + 1] == (
        "portable-estate-v1"
    )
    assert status[0:2] == ["process-estate-outbox", "status"]
    assert "--apply" not in status
    assert alpha_audit[0:2] == ["process-estate-outbox", "audit-alpha"]
    assert "--apply" not in alpha_audit
    assert "--alpha-index" in alpha_audit


def test_alpha_automation_requires_gate_and_absolute_existing_paths(
    tmp_path: Path,
) -> None:
    estate = tmp_path / "estate"
    _write_sentinel(estate)
    with pytest.raises(AirflowTaskConfigurationError, match="disabled"):
        build_command(
            "drain",
            dag_id="alpha",
            run_id="run",
            environment=_environment(estate),
        )

    environment = _alpha_environment(tmp_path / "configured")
    environment["PDFS_ALPHA_INDEX"] = "relative/index.db"
    with pytest.raises(AirflowTaskConfigurationError, match="absolute"):
        build_command(
            "reconcile",
            dag_id="alpha",
            run_id="run",
            environment=environment,
        )

    environment = _alpha_environment(tmp_path / "unsafe-corpus")
    environment["PDFS_ALPHA_CORPUS"] = environment["PDFS_DOCUMENT_ESTATE"]
    with pytest.raises(AirflowTaskConfigurationError, match="projection directory"):
        build_command(
            "reconcile",
            dag_id="alpha",
            run_id="run",
            environment=environment,
        )


def test_task_configuration_rejects_unsafe_inputs(tmp_path: Path) -> None:
    with pytest.raises(AirflowTaskConfigurationError, match="invalid issuer"):
        build_command(
            "audit",
            dag_id="audit",
            run_id="run",
            environment=_environment(
                tmp_path,
                PDFS_AIRFLOW_ONLY="../../escape",
            ),
        )

    with pytest.raises(AirflowTaskConfigurationError, match="absolute"):
        build_command(
            "audit",
            dag_id="audit",
            run_id="run",
            environment={"PDFS_DOCUMENT_ESTATE": "relative/estate"},
        )


def test_dag_source_uses_airflow_3_public_interface_and_safety_controls() -> None:
    source = DAG_PATH.read_text(encoding="utf-8")
    ast.parse(source)

    assert "from airflow.sdk import" in source
    assert 'dag_id="quarterly_estate_audit"' in source
    assert 'dag_id="quarterly_estate_sync"' in source
    assert 'dag_id="quarterly_estate_consume"' in source
    assert 'dag_id="quarterly_estate_alpha_reconcile"' in source
    assert source.count("is_paused_upon_creation=True") == 3
    assert source.count("catchup=False") == 4
    assert 'pool="estate_writer"' in source
    assert "retries=TASK_RETRIES" in source
    assert "retry_exponential_backoff=True" in source
    assert "on_failure_callback=_notify_failure" in source
    assert 'PDFS_AIRFLOW_ALERT_WEBHOOK' in source
    assert "src.acquisition" not in source
    assert "sys.executable" in source
    assert '"src.deployment.airflow_task"' in source
    assert 'sync_fleet() >> drain_alpha_outbox()' in source
    assert (
        'reconcile_alpha() >> audit_alpha_projection() >> check_consumer_status()'
        in source
    )


def test_native_template_separates_airflow_metadata_from_estate() -> None:
    source = NATIVE_ENV_PATH.read_text(encoding="utf-8")

    assert "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=" in source
    assert "PDFS_DOCUMENT_ESTATE=" in source
    assert "document-estate/airflow.db" not in source
    assert "Docker" not in source
    assert SYSTEMD_PATH.is_file()
    systemd = SYSTEMD_PATH.read_text(encoding="utf-8")
    assert "${PDFS_PROJECT_ROOT}" in systemd
    assert "${PDFS_AIRFLOW_VENV}/bin/airflow %i" in systemd
    assert "WorkingDirectory=/opt/" not in systemd
    installer = INSTALLER_PATH.read_text(encoding="utf-8")
    assert "apache-airflow==3.3.0" in installer
    assert "constraints-3.3.0/constraints-3.13.txt" in installer
    assert '"${project_root}[browser]"' in installer
    assert "-m pip check" in installer
    assert "sqlite3.sqlite_version" in installer
    assert "docker" not in installer.lower()
    assert RUNNER_PATH.is_file()
    assert STATUS_PATH.is_file()


def test_generated_environment_is_secret_bearing_and_sync_off(
    tmp_path: Path,
) -> None:
    estate = tmp_path / "estate"
    airflow_home = tmp_path / "airflow"
    _write_sentinel(estate)
    first = render_environment(estate, airflow_home=airflow_home)
    second = render_environment(estate, airflow_home=airflow_home)

    assert 'PDFS_DOCUMENT_ESTATE="' + str(estate) + '"' in first
    assert f'PDFS_ESTATE_ID="{ESTATE_ID}"' in first
    assert 'PDFS_ESTATE_MOUNT_ROOT="' in first
    assert 'AIRFLOW_HOME="' + str(airflow_home) + '"' in first
    assert 'AIRFLOW__CORE__EXECUTOR="LocalExecutor"' in first
    assert 'PDFS_AIRFLOW_SYNC_ENABLED="false"' in first
    assert 'PDFS_AIRFLOW_ALLOW_COVERAGE_GAPS="false"' in first
    assert 'PDFS_AIRFLOW_TASK_RETRIES="2"' in first
    assert 'PDFS_AIRFLOW_ALERT_WEBHOOK=""' in first
    assert 'PDFS_AIRFLOW_VENV="' in first
    assert "replace-me" not in first
    assert first != second


def test_postgres_metadata_selects_local_executor(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    _write_sentinel(estate)
    rendered = render_environment(
        estate,
        airflow_home=tmp_path / "airflow",
        metadata_url="postgresql+psycopg2://airflow:replace-me@localhost/airflow",
    )

    assert 'AIRFLOW__CORE__EXECUTOR="LocalExecutor"' in rendered
    assert (
        'AIRFLOW__DATABASE__SQL_ALCHEMY_CONN='
        '"postgresql+psycopg2://airflow:replace-me@localhost/airflow"'
    ) in rendered


def test_environment_rejects_airflow_state_inside_estate(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    _write_sentinel(estate)

    with pytest.raises(ValueError, match="Airflow home must be outside"):
        render_environment(
            estate,
            airflow_home=estate / "airflow",
        )

    with pytest.raises(ValueError, match="metadata database must be outside"):
        render_environment(
            estate,
            airflow_home=tmp_path / "airflow",
            metadata_url=f"sqlite:///{estate / 'airflow.db'}",
        )


def test_launchd_renderer_uses_absolute_runner_and_secret_file_reference(
    tmp_path: Path,
) -> None:
    payload = plistlib.loads(
        render_launchd_service(
            "scheduler",
            environment_file=tmp_path / "airflow.env",
            airflow_home=tmp_path / "state",
        )
    )

    assert payload["Label"] == "com.bmv.airflow.scheduler"
    assert payload["ProgramArguments"] == [
        str(RUNNER_PATH),
        str(tmp_path / "airflow.env"),
        "scheduler",
    ]
    assert payload["KeepAlive"] is True
    assert "FERNET" not in repr(payload)


def test_configure_can_render_but_does_not_load_launchd(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    airflow_home = tmp_path / "airflow-state"
    airflow_venv = tmp_path / "airflow-venv"
    executable = airflow_venv / "bin" / "airflow"
    _write_sentinel(estate)
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    output = tmp_path / "airflow.env"
    launchd = tmp_path / "LaunchAgents"

    assert configure_airflow(
        [
            "--estate",
            str(estate),
            "--airflow-home",
            str(airflow_home),
            "--airflow-venv",
            str(airflow_venv),
            "--output",
            str(output),
            "--launchd-output-dir",
            str(launchd),
        ]
    ) == 0

    assert output.stat().st_mode & 0o777 == 0o600
    assert len(tuple(launchd.glob("com.bmv.airflow.*.plist"))) == 3
    assert not (launchd / "loaded").exists()
    first = output.read_text(encoding="utf-8")

    assert configure_airflow(
        [
            "--estate",
            str(estate),
            "--airflow-home",
            str(airflow_home),
            "--airflow-venv",
            str(airflow_venv),
            "--output",
            str(output),
            "--launchd-output-dir",
            str(launchd),
            "--force",
        ]
    ) == 0
    second = output.read_text(encoding="utf-8")
    for name in (
        "AIRFLOW__CORE__FERNET_KEY",
        "AIRFLOW__API_AUTH__JWT_SECRET",
    ):
        first_line = next(line for line in first.splitlines() if line.startswith(name))
        second_line = next(line for line in second.splitlines() if line.startswith(name))
        assert first_line == second_line


def test_native_status_fails_closed_when_environment_is_missing(
    tmp_path: Path,
) -> None:
    report = inspect_airflow(tmp_path / "missing.env", runtime=False)

    assert report["ok"] is False
    assert report["checks"]["environment_file"]["ok"] is False


def test_release_workflow_covers_every_declared_environment_without_docker() -> None:
    source = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")

    for job in ("payload:", "root:", "alpha-go:", "soft:", "earnings:"):
        assert f"  {job}" in source
    for version in ("3.13.9", "3.12.13", "3.11.13"):
        assert f'python-version: "{version}"' in source
    assert "fetch-depth: 0" in source
    assert "scripts/audit_git_payload.py" in source
    assert source.count('not network and not model') == 4
    assert "docker" not in source.lower()


def test_airflow_workflow_exercises_the_exported_native_installer() -> None:
    source = AIRFLOW_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert 'python-version: "3.13.9"' in source
    assert '- "scripts/install_airflow_native.sh"' in source
    assert "- run: scripts/install_airflow_native.sh" in source
    assert '"${PDFS_AIRFLOW_VENV}/bin/airflow"' in source
    assert "dags list --local --output plain" in source
