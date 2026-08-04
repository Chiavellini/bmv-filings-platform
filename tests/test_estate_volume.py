from __future__ import annotations

import json
from pathlib import Path

import pytest

from estate_bridge import EstateBridgeError, load_estate_bridge
from estate_volume import EstateVolumeError, inspect_estate_volume, read_estate_id
from src.deployment.airflow_task import AirflowTaskConfigurationError, build_command


ESTATE_ID = "56EC1117-38A4-4F38-A135-408F04AF9A89"


def _sentinel(root: Path, estate_id: str = ESTATE_ID) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".bmv-estate-volume.json").write_text(
        json.dumps({"schema_version": 1, "estate_id": estate_id}),
        encoding="utf-8",
    )


def _bridge(tmp_path: Path, estate: Path) -> Path:
    config = tmp_path / "estate.json"
    config.write_text(
        json.dumps({"bridge_version": 1, "estate_root": str(estate)}),
        encoding="utf-8",
    )
    return config


def test_sentinel_identity_round_trip(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    _sentinel(estate)

    assert read_estate_id(estate) == ESTATE_ID
    status = inspect_estate_volume(
        estate,
        mount_root=tmp_path,
        expected_estate_id=ESTATE_ID.lower(),
        require_mountpoint=False,
    )
    assert status.healthy
    assert status.observed_estate_id == ESTATE_ID


def test_wrong_id_and_unmounted_root_fail_closed(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    _sentinel(estate)

    wrong = inspect_estate_volume(
        estate,
        mount_root=tmp_path,
        expected_estate_id="00000000-0000-0000-0000-000000000000",
        require_mountpoint=False,
    )
    assert not wrong.healthy
    assert "estate ID mismatch" in "; ".join(wrong.problems)

    unmounted = inspect_estate_volume(
        estate,
        mount_root=tmp_path,
        expected_estate_id=ESTATE_ID,
    )
    assert not unmounted.healthy
    assert "not a mounted filesystem" in "; ".join(unmounted.problems)


def test_missing_sentinel_is_an_identity_failure(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    estate.mkdir()
    with pytest.raises(EstateVolumeError, match="sentinel is missing"):
        read_estate_id(estate)


def test_bridge_catalog_connection_enforces_configured_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = tmp_path / "estate"
    _sentinel(estate)
    (estate / "catalog.db").touch()
    bridge = load_estate_bridge(config_path=_bridge(tmp_path, estate))
    monkeypatch.setenv("PDFS_ESTATE_MOUNT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "PDFS_ESTATE_ID", "00000000-0000-0000-0000-000000000000"
    )

    with pytest.raises(EstateBridgeError, match="estate ID mismatch"):
        bridge.connect_catalog(read_only=True)


def test_airflow_rejects_wrong_estate_id(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    _sentinel(estate)
    environment = {
        "PDFS_DOCUMENT_ESTATE": str(estate),
        "PDFS_ESTATE_MOUNT_ROOT": str(tmp_path),
        "PDFS_ESTATE_ID": "00000000-0000-0000-0000-000000000000",
    }

    with pytest.raises(AirflowTaskConfigurationError, match="estate ID mismatch"):
        build_command(
            "audit",
            dag_id="quarterly_estate_audit",
            run_id="manual__wrong-estate",
            environment=environment,
        )


def test_partial_identity_configuration_is_rejected(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    _sentinel(estate)
    status = inspect_estate_volume(
        estate,
        expected_estate_id=ESTATE_ID,
        require_mountpoint=False,
    )
    assert not status.healthy
    assert "must be configured together" in "; ".join(status.problems)
