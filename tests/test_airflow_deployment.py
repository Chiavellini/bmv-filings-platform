from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.configure_airflow import render_environment
from src.deployment.airflow_task import (
    AirflowTaskConfigurationError,
    build_command,
)


ROOT = Path(__file__).resolve().parents[1]
DAG_PATH = ROOT / "deploy" / "airflow" / "dags" / "quarterly_estate.py"
NATIVE_ENV_PATH = ROOT / "deploy" / "airflow" / "native" / ".env.example"
SYSTEMD_PATH = (
    ROOT / "deploy" / "airflow" / "native" / "systemd" / "bmv-airflow@.service"
)
INSTALLER_PATH = ROOT / "scripts" / "install_airflow_native.sh"
RELEASE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "release-gate.yml"
AIRFLOW_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "airflow-native.yml"


def _environment(estate: Path, **overrides: str) -> dict[str, str]:
    values = {
        "PDFS_DOCUMENT_ESTATE": str(estate),
        "PDFS_AIRFLOW_SYNC_ENABLED": "false",
    }
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
    assert "--allow-coverage-gaps" in command
    assert command[command.index("--trigger") + 1].startswith(
        "airflow:quarterly_estate_sync:"
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
    assert "is_paused_upon_creation=True" in source
    assert source.count("catchup=False") == 2
    assert 'pool="estate_writer"' in source
    assert "src.acquisition" not in source
    assert "sys.executable" in source
    assert '"src.deployment.airflow_task"' in source


def test_native_template_separates_airflow_metadata_from_estate() -> None:
    source = NATIVE_ENV_PATH.read_text(encoding="utf-8")

    assert "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=" in source
    assert "PDFS_DOCUMENT_ESTATE=" in source
    assert "document-estate/airflow.db" not in source
    assert "Docker" not in source
    assert SYSTEMD_PATH.is_file()
    assert "airflow %i" in SYSTEMD_PATH.read_text(encoding="utf-8")
    installer = INSTALLER_PATH.read_text(encoding="utf-8")
    assert "apache-airflow==3.3.0" in installer
    assert "constraints-3.3.0/constraints-3.13.txt" in installer
    assert '"${project_root}[browser]"' in installer
    assert "-m pip check" in installer
    assert "docker" not in installer.lower()


def test_generated_environment_is_secret_bearing_and_sync_off(
    tmp_path: Path,
) -> None:
    airflow_home = tmp_path / "airflow"
    first = render_environment(tmp_path, airflow_home=airflow_home)
    second = render_environment(tmp_path, airflow_home=airflow_home)

    assert 'PDFS_DOCUMENT_ESTATE="' + str(tmp_path) + '"' in first
    assert 'AIRFLOW_HOME="' + str(airflow_home) + '"' in first
    assert 'AIRFLOW__CORE__EXECUTOR="LocalExecutor"' in first
    assert 'PDFS_AIRFLOW_SYNC_ENABLED="false"' in first
    assert "replace-me" not in first
    assert first != second


def test_postgres_metadata_selects_local_executor(tmp_path: Path) -> None:
    rendered = render_environment(
        tmp_path,
        airflow_home=tmp_path / "airflow",
        metadata_url="postgresql+psycopg2://airflow:replace-me@localhost/airflow",
    )

    assert 'AIRFLOW__CORE__EXECUTOR="LocalExecutor"' in rendered
    assert (
        'AIRFLOW__DATABASE__SQL_ALCHEMY_CONN='
        '"postgresql+psycopg2://airflow:replace-me@localhost/airflow"'
    ) in rendered


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
