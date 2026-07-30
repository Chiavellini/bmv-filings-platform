from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

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


def _load_airflow_dag_module():
    pytest.importorskip("airflow")
    spec = importlib.util.spec_from_file_location(
        "quarterly_estate_test",
        DAG_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _terminal_outbox_status() -> dict[str, object]:
    return {
        "initialized": True,
        "unpublished_events": 0,
        "missing_receipts": 0,
        "deliveries": {"succeeded": 2, "skipped": 2},
    }


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
    assert source.count('pool="estate_writer"') == 2
    assert "src.acquisition" not in source
    assert "sys.executable" in source
    assert '"src.deployment.airflow_task"' in source
    assert 'task_id="process_estate_outbox"' in source
    assert '"scripts" / "process_estate_outbox.py"' in source
    assert "sync_fleet() >> process_estate_outbox()" in source
    assert '"PDFS_AIRFLOW_SYNC_ENABLED"' in source
    assert '"PDFS_AIRFLOW_POST_SYNC_ENABLED"' in source
    assert '"PDFS_AIRFLOW_ALPHA_ENABLED"' in source


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
    assert 'PDFS_AIRFLOW_POST_SYNC_ENABLED="true"' in first
    assert 'PDFS_AIRFLOW_PDF_PARSER_VERSION="1"' in first
    assert 'PDFS_AIRFLOW_XBRL_PROCESSOR_VERSION="1"' in first
    assert 'PDFS_AIRFLOW_ALPHA_ENABLED="false"' in first
    assert (
        'PDFS_AIRFLOW_ALPHA_INDEX="'
        + str(tmp_path / "indexes" / "alpha_go.db")
        + '"'
    ) in first
    assert "replace-me" not in first
    assert first != second


def test_post_sync_worker_drains_bounded_batches_without_alpha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_airflow_dag_module()
    estate = tmp_path / "estate"
    estate.mkdir()
    monkeypatch.setenv("PDFS_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("PDFS_DOCUMENT_ESTATE", str(estate))
    monkeypatch.setenv("PDFS_AIRFLOW_SYNC_ENABLED", "true")
    responses = iter(({"claimed": 250}, {"claimed": 0}))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        payload = (
            _terminal_outbox_status()
            if "status" in command
            else next(responses)
        )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.process_estate_outbox.function()

    assert len(calls) == 3
    assert calls[0][0][0] == module.sys.executable
    assert calls[0][0][1] == str(ROOT / "scripts/process_estate_outbox.py")
    assert "--apply" in calls[0][0]
    assert calls[0][0][calls[0][0].index("--parser-version") + 1] == "1"
    assert (
        calls[0][0][calls[0][0].index("--xbrl-processor-version") + 1]
        == "1"
    )
    assert "--enable-alpha-go" not in calls[0][0]
    assert "--disable-alpha-go" in calls[0][0]
    assert "status" in calls[-1][0]
    assert calls[0][1]["cwd"] == ROOT


def test_post_sync_worker_requires_the_applied_sync_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_airflow_dag_module()
    monkeypatch.setenv("PDFS_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("PDFS_DOCUMENT_ESTATE", str(tmp_path))
    monkeypatch.setenv("PDFS_AIRFLOW_SYNC_ENABLED", "false")

    with pytest.raises(RuntimeError, match="SYNC_ENABLED=true"):
        module.process_estate_outbox.function()


def test_post_sync_worker_passes_explicit_alpha_target_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_airflow_dag_module()
    estate = tmp_path / "estate"
    estate.mkdir()
    corpus = estate / "indexes" / "alpha_go" / "corpus"
    index = estate / "indexes" / "alpha_go.db"
    monkeypatch.setenv("PDFS_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("PDFS_DOCUMENT_ESTATE", str(estate))
    monkeypatch.setenv("PDFS_AIRFLOW_SYNC_ENABLED", "true")
    monkeypatch.setenv("PDFS_AIRFLOW_ALPHA_ENABLED", "true")
    monkeypatch.setenv("PDFS_AIRFLOW_ALPHA_ROOT", str(ROOT / "alpha-go"))
    monkeypatch.setenv(
        "PDFS_AIRFLOW_ALPHA_CONFIG",
        str(ROOT / "alpha-go" / "configs" / "alpha_go.yaml"),
    )
    monkeypatch.setenv("PDFS_AIRFLOW_ALPHA_PYTHON", module.sys.executable)
    monkeypatch.setenv("PDFS_AIRFLOW_ALPHA_CORPUS", str(corpus))
    monkeypatch.setenv("PDFS_AIRFLOW_ALPHA_INDEX", str(index))
    monkeypatch.setenv("PDFS_AIRFLOW_ALPHA_TARGET_ID", "canary-v1")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        payload = (
            _terminal_outbox_status()
            if "status" in command
            else {"claimed": 0}
        )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.process_estate_outbox.function()

    command = calls[0][0]
    assert "--enable-alpha-go" in command
    assert "--disable-alpha-go" not in command
    assert command[command.index("--alpha-corpus") + 1] == str(corpus)
    assert command[command.index("--alpha-index") + 1] == str(index)
    assert command[command.index("--alpha-target-id") + 1] == "canary-v1"
    assert (
        command[command.index("--alpha-python") + 1]
        == module.sys.executable
    )


def test_post_sync_worker_rejects_delayed_retry_false_green(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_airflow_dag_module()
    estate = tmp_path / "estate"
    estate.mkdir()
    monkeypatch.setenv("PDFS_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("PDFS_DOCUMENT_ESTATE", str(estate))
    monkeypatch.setenv("PDFS_AIRFLOW_SYNC_ENABLED", "true")

    def fake_run(command, **kwargs):
        if "status" in command:
            payload = {
                **_terminal_outbox_status(),
                "unpublished_events": 1,
                "deliveries": {"retryable": 1},
            }
            return SimpleNamespace(
                returncode=1, stdout=json.dumps(payload), stderr=""
            )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"claimed": 0}), stderr=""
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="not terminal"):
        module.process_estate_outbox.function()


def test_exact_batch_ceiling_succeeds_when_status_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_airflow_dag_module()
    estate = tmp_path / "estate"
    estate.mkdir()
    monkeypatch.setenv("PDFS_PROJECT_ROOT", str(ROOT))
    monkeypatch.setenv("PDFS_DOCUMENT_ESTATE", str(estate))
    monkeypatch.setenv("PDFS_AIRFLOW_SYNC_ENABLED", "true")
    monkeypatch.setenv("PDFS_AIRFLOW_OUTBOX_MAX_BATCHES", "1")

    def fake_run(command, **kwargs):
        payload = (
            _terminal_outbox_status()
            if "status" in command
            else {"claimed": 250}
        )
        return SimpleNamespace(
            returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module.process_estate_outbox.function()


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
