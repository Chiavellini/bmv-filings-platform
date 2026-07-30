from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from estate_bridge import EstateBridgeError, load_estate_bridge
from src.shared.document_estate import DocumentEstate


def _write_bridge(path: Path, estate_root: str = "portable-estate") -> None:
    path.write_text(
        json.dumps(
            {
                "bridge_version": 1,
                "estate_root": estate_root,
                "catalog": "catalog.db",
                "objects": "blobs",
                "reports_view": "views/reports",
                "uploads": "user",
                "models": {
                    "root": "models",
                    "alpha_go_embedding": "models/alpha-model",
                },
                "manifest": "manifest.json",
                "indexes": {"alpha_go": "indexes/alpha_go.db"},
            }
        ),
        encoding="utf-8",
    )


def test_relative_bridge_moves_as_one_bundle(tmp_path: Path) -> None:
    config = tmp_path / "estate.json"
    _write_bridge(config)
    bundle = tmp_path / "portable-estate"
    (bundle / "blobs").mkdir(parents=True)
    (bundle / "views" / "reports").mkdir(parents=True)
    (bundle / "indexes").mkdir(parents=True)
    sqlite3.connect(bundle / "catalog.db").close()
    (bundle / "indexes" / "alpha_go.db").touch()

    bridge = load_estate_bridge(config_path=config)

    assert bridge.estate_root == bundle.resolve()
    assert bridge.catalog_path == (bundle / "catalog.db").resolve()
    assert bridge.objects_dir == (bundle / "blobs").resolve()
    assert bridge.reports_view_dir == (bundle / "views" / "reports").resolve()
    assert bridge.models_dir == (bundle / "models").resolve()
    assert bridge.alpha_go_embedding_model_path == (
        bundle / "models" / "alpha-model"
    ).resolve()
    assert bridge.alpha_go_index_path == (
        bundle / "indexes" / "alpha_go.db"
    ).resolve()
    assert bridge.validate(require_alpha_index=True) == []


def test_catalog_connection_is_owned_by_bridge(tmp_path: Path) -> None:
    config = tmp_path / "estate.json"
    _write_bridge(config)
    bundle = tmp_path / "portable-estate"
    bundle.mkdir()
    with sqlite3.connect(bundle / "catalog.db") as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES ('connected')")

    bridge = load_estate_bridge(config_path=config)
    with bridge.connect_catalog(read_only=True) as connection:
        row = connection.execute("SELECT value FROM marker").fetchone()

    assert row["value"] == "connected"


def test_alpha_upload_registration_is_atomic_and_complete(tmp_path: Path) -> None:
    config = tmp_path / "estate.json"
    _write_bridge(config)
    bundle = tmp_path / "portable-estate"
    bundle.mkdir()
    with DocumentEstate(bundle / "catalog.db"):
        pass
    upload_dir = bundle / "user" / "alpha-go" / "acme"
    upload_dir.mkdir(parents=True)
    markdown = upload_dir / "2025-1T.md"
    original = upload_dir / "2025-1T.txt"
    markdown.write_text("Passengers increased while revenue improved.", encoding="utf-8")
    original.write_text("Passengers increased while revenue improved.", encoding="utf-8")

    bridge = load_estate_bridge(config_path=config)
    estate_id = bridge.register_alpha_upload(
        alpha_doc_id="acme/2025-1T",
        company="acme",
        period="2025-1T",
        doc_type="quarterly_release",
        title="Acme first quarter",
        language="en",
        memberships=[
            {"company": "acme", "industry": "transport"},
            {"company": "globex", "industry": "industrial"},
        ],
        markdown_path=markdown,
        original_path=original,
        original_filename="acme_1q25.txt",
    )

    assert estate_id == "alpha-go:acme/2025-1T"
    with bridge.connect_catalog(read_only=True) as connection:
        document = connection.execute(
            "SELECT * FROM documents WHERE document_id=?", (estate_id,)
        ).fetchone()
        assert document["company"] == "acme"
        assert document["period"] == "2025-1T"
        assert json.loads(document["metadata_json"])["uploaded"] is True
        assert {
            (row["company"], row["industry"])
            for row in connection.execute(
                "SELECT company,industry FROM memberships WHERE document_id=?",
                (estate_id,),
            )
        } == {("acme", "transport"), ("globex", "industrial")}
        assert connection.execute(
            """SELECT document_id FROM project_records
               WHERE project='alpha-go' AND project_record_id='acme/2025-1T'"""
        ).fetchone()["document_id"] == estate_id
        artifacts = connection.execute(
            """SELECT role,format,path FROM artifacts
               WHERE document_id=? ORDER BY role""",
            (estate_id,),
        ).fetchall()
        assert {(row["role"], row["format"]) for row in artifacts} == {
            ("original", "txt"),
            ("search_text", "md"),
        }
        assert all(Path(row["path"]).is_file() for row in artifacts)


def test_alpha_upload_registration_rejects_an_artifact_owned_by_another_document(
    tmp_path: Path,
) -> None:
    config = tmp_path / "estate.json"
    _write_bridge(config)
    bundle = tmp_path / "portable-estate"
    bundle.mkdir()
    shared = bundle / "user" / "shared.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("shared", encoding="utf-8")
    with DocumentEstate(bundle / "catalog.db") as estate:
        from src.shared.document_estate import EstateDocument

        estate.upsert_document(
            EstateDocument("root:owner", "owner", None, "internal", "Owner")
        )
        estate.add_project_record("root", "owner", "root:owner")
        estate.add_artifact("root:owner", shared, project="root", role="original")
        estate.commit()

    bridge = load_estate_bridge(config_path=config)
    with pytest.raises(EstateBridgeError, match="already owned"):
        bridge.register_alpha_upload(
            alpha_doc_id="acme/shared",
            company="acme",
            period=None,
            doc_type="internal",
            title="Shared",
            language="en",
            memberships=[],
            markdown_path=shared,
            original_path=shared,
            original_filename="shared.md",
        )

    with bridge.connect_catalog(read_only=True) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM documents WHERE document_id='alpha-go:acme/shared'"
        ).fetchone()[0] == 0


def test_read_only_connection_check_reports_catalog_counts(tmp_path: Path) -> None:
    from scripts.check_estate_connection import inspect_connection

    config = tmp_path / "estate.json"
    _write_bridge(config)
    bundle = tmp_path / "portable-estate"
    bundle.mkdir()
    with sqlite3.connect(bundle / "catalog.db") as connection:
        connection.execute("CREATE TABLE documents(document_id TEXT)")
        connection.execute("CREATE TABLE artifacts(artifact_id TEXT)")
        connection.execute("CREATE TABLE content_objects(sha256 TEXT)")
        connection.execute("INSERT INTO documents VALUES ('one')")

    result, exit_code = inspect_connection(config_path=config)

    assert exit_code == 0
    assert result["connected"] is True
    assert result["sqlite_quick_check"] == "ok"
    assert result["counts"] == {
        "documents": 1,
        "artifacts": 0,
        "content_objects": 0,
    }


def test_environment_root_override_preserves_bundle_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "estate.json"
    _write_bridge(config, "ignored")
    external = tmp_path / "external-disk" / "estate-v2"
    monkeypatch.setenv("PDFS_DOCUMENT_ESTATE", str(external))

    bridge = load_estate_bridge(config_path=config)

    assert bridge.estate_root == external.resolve()
    assert bridge.catalog_path == (external / "catalog.db").resolve()
    assert bridge.alpha_go_index_path == (
        external / "indexes" / "alpha_go.db"
    ).resolve()


def test_bridge_rejects_paths_outside_estate(tmp_path: Path) -> None:
    config = tmp_path / "estate.json"
    config.write_text(
        json.dumps(
            {
                "bridge_version": 1,
                "estate_root": "estate",
                "catalog": "../outside.db",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EstateBridgeError, match="inside estate_root"):
        load_estate_bridge(config_path=config)


def test_object_resolution_rejects_directory_traversal(tmp_path: Path) -> None:
    config = tmp_path / "estate.json"
    _write_bridge(config)
    bridge = load_estate_bridge(config_path=config)

    assert bridge.resolve_object("blobs/ab/hash") == (
        tmp_path / "portable-estate" / "blobs" / "ab" / "hash"
    ).resolve()
    with pytest.raises(EstateBridgeError, match="inside estate_root"):
        bridge.resolve_object("../secret")


@pytest.mark.parametrize(
    ("subproject", "expression"),
    [
        (
            ".",
            "from src.shared.paths import DOCUMENT_ESTATE_DB; print(DOCUMENT_ESTATE_DB)",
        ),
        (
            "alpha-go",
            "from src.shared.paths import DOCUMENT_ESTATE_DB; print(DOCUMENT_ESTATE_DB)",
        ),
        (
            "soft",
            "from src.shared.paths import DOCUMENT_ESTATE_DB; print(DOCUMENT_ESTATE_DB)",
        ),
        (
            "earnings",
            "from earnlib.bootstrap import ESTATE_BRIDGE; print(ESTATE_BRIDGE.catalog_path)",
        ),
    ],
)
def test_every_subproject_reads_the_same_bridge(
    tmp_path: Path, subproject: str, expression: str
) -> None:
    repo = Path(__file__).resolve().parents[1]
    config = tmp_path / "estate.json"
    _write_bridge(config, "shared-estate")
    expected = (tmp_path / "shared-estate" / "catalog.db").resolve()
    environment = os.environ.copy()
    environment["PDFS_ESTATE_BRIDGE"] = str(config)
    environment.pop("PDFS_DOCUMENT_ESTATE", None)

    completed = subprocess.run(
        [sys.executable, "-c", expression],
        cwd=repo / subproject,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == str(expected)
