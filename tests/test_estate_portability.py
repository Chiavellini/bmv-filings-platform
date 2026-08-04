from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from estate_portability import export_portable_estate, verify_portable_estate
from src.shared.document_estate import DocumentEstate, EstateDocument


ESTATE_ID = "56EC1117-38A4-4F38-A135-408F04AF9A89"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_portability_modules_import_outside_source_checkout(tmp_path: Path) -> None:
    """Installed console scripts must not depend on the repository as cwd."""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import estate_bridge, estate_portability, estate_volume",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _source_estate(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "legacy"
    outside.mkdir()
    first = outside / "one.md"
    second = outside / "two.md"
    first.write_text("ventas crecieron", encoding="utf-8")
    second.write_text("ventas crecieron", encoding="utf-8")
    with DocumentEstate(source / "catalog.db") as estate:
        for document_id, path in (("one", first), ("two", second)):
            estate.upsert_document(
                EstateDocument(
                    document_id,
                    "acme",
                    "2026-1T",
                    "quarterly_release",
                    document_id,
                )
            )
            estate.add_artifact(
                document_id, path, project="root", role="search_text"
            )
        estate.commit()
    return source


def test_export_is_portable_hard_linked_and_relocatable(tmp_path: Path) -> None:
    source = _source_estate(tmp_path)
    destination = tmp_path / "portable"

    result = export_portable_estate(
        source,
        destination,
        estate_id=ESTATE_ID,
        include_runtime=False,
    )

    assert result["verified"] is True
    assert result["counts"] == {
        "documents": 2,
        "artifacts": 2,
        "content_objects": 1,
    }
    connection = sqlite3.connect(destination / "catalog.db")
    paths = [row[0] for row in connection.execute("SELECT path FROM artifacts")]
    connection.close()
    assert all(not Path(path).is_absolute() for path in paths)
    stats = [(destination / path).stat() for path in paths]
    assert stats[0].st_ino == stats[1].st_ino

    relocated = tmp_path / "elsewhere" / "estate"
    relocated.parent.mkdir()
    destination.rename(relocated)
    moved = verify_portable_estate(
        relocated,
        expected_estate_id=ESTATE_ID,
        full_hashes=True,
    )
    assert moved["verified"] is True


def test_full_hash_check_detects_corruption(tmp_path: Path) -> None:
    source = _source_estate(tmp_path)
    destination = tmp_path / "portable"
    export_portable_estate(source, destination, estate_id=ESTATE_ID)
    blob = next((destination / "blobs").glob("*/*"))
    blob.write_text("corrupted", encoding="utf-8")

    result = verify_portable_estate(
        destination,
        expected_estate_id=ESTATE_ID,
        full_hashes=True,
    )

    assert result["verified"] is False
    assert result["bad_object_hashes"] == 1


@pytest.mark.parametrize(
    "script",
    (
        "scripts/check_portable_estate.py",
        "scripts/export_portable_estate.py",
        "scripts/configure_airflow.py",
        "scripts/airflow_native_status.py",
    ),
)
def test_portability_cli_runs_directly_from_repository_root(script: str) -> None:
    result = subprocess.run(
        [sys.executable, script, "--help"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
