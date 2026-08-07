"""The Alpha Go release gate must reject an index that merely exists.

``--require-alpha-index`` was an ``is_file()`` check, so a partially built index
satisfied the release gate. The connected estate shipped a 10-document index
covering a single issuer against a 3,695-document projection and the gate still
reported success, which is how an incomplete index reached the production path.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from estate_bridge import load_estate_bridge
from estate_portability import inspect_alpha_index

from tests._alpha_index import build_alpha_index as _build_index


@pytest.fixture(autouse=True)
def _bundle_env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """See tests/test_estate_bridge.py: the shell must not redirect the bundle."""
    for name in (
        "PDFS_DOCUMENT_ESTATE",
        "PDFS_ESTATE_BRIDGE",
        "PDFS_REPORTS_DIR",
        "PDFS_ESTATE_MOUNT_ROOT",
        "PDFS_ESTATE_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def test_complete_semantic_index_is_accepted(tmp_path: Path) -> None:
    index = _build_index(tmp_path / "alpha_go.db")
    assert inspect_alpha_index(index) == []


def test_missing_index_is_reported(tmp_path: Path) -> None:
    problems = inspect_alpha_index(tmp_path / "absent.db")
    assert problems and "does not exist" in problems[0]


def test_empty_file_is_not_a_usable_index(tmp_path: Path) -> None:
    """The exact shape of the defect: a touched file used to satisfy the gate."""
    index = tmp_path / "alpha_go.db"
    index.touch()
    assert inspect_alpha_index(index) != []


def test_empty_staging_database_is_rejected(tmp_path: Path) -> None:
    """``.alpha_go.db.building-13983`` on the estate: full schema, zero rows."""
    index = _build_index(tmp_path / "staging.db", documents=0)
    problems = inspect_alpha_index(index)
    assert any("no documents" in p for p in problems)


def test_index_without_embedding_metadata_is_rejected(tmp_path: Path) -> None:
    """The live 10-document index records neither model nor dimension."""
    index = _build_index(tmp_path / "alpha_go.db", model=None, embedding_dim_meta=None)
    problems = inspect_alpha_index(index)
    assert any("embedding_model" in p for p in problems)
    assert any("embedding_dim" in p for p in problems)


def test_hashing_index_is_not_semantic_ready(tmp_path: Path) -> None:
    index = _build_index(tmp_path / "alpha_go.db", model="hashing", dim=256)
    problems = inspect_alpha_index(index)
    assert any("hashing" in p for p in problems)


def test_row_count_disagreement_is_rejected(tmp_path: Path) -> None:
    index = _build_index(tmp_path / "alpha_go.db", embeddings=False)
    assert any("disagree" in p for p in inspect_alpha_index(index))


def test_embedding_dimension_mismatch_is_rejected(tmp_path: Path) -> None:
    index = _build_index(tmp_path / "alpha_go.db", dim=384, embedding_dim_meta=768)
    assert any("dim" in p for p in inspect_alpha_index(index))


def test_partial_document_coverage_is_rejected(tmp_path: Path) -> None:
    """10 indexed documents against a 3,695-document projection must fail."""
    index = _build_index(tmp_path / "alpha_go.db", documents=10)
    assert inspect_alpha_index(index) == []
    problems = inspect_alpha_index(index, expected_documents=3695)
    assert any("3695" in p and "10" in p for p in problems)


def _write_bridge(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "bridge_version": 1,
                "estate_root": "portable-estate",
                "catalog": "catalog.db",
                "objects": "blobs",
                "reports_view": "views/reports",
                "uploads": "user",
                "models": {"root": "models", "alpha_go_embedding": "models/alpha-model"},
                "manifest": "manifest.json",
                "indexes": {"alpha_go": "indexes/alpha_go.db"},
                "projections": {"alpha_go": "projections/alpha-go"},
            }
        ),
        encoding="utf-8",
    )


def _bundle(tmp_path: Path) -> Path:
    config = tmp_path / "estate.json"
    _write_bridge(config)
    bundle = tmp_path / "portable-estate"
    (bundle / "blobs").mkdir(parents=True)
    (bundle / "views" / "reports").mkdir(parents=True)
    (bundle / "indexes").mkdir(parents=True)
    (bundle / "projections" / "alpha-go").mkdir(parents=True)
    sqlite3.connect(bundle / "catalog.db").close()
    return bundle


def test_bridge_gate_rejects_an_incomplete_index(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    _build_index(bundle / "indexes" / "alpha_go.db", documents=10)
    (bundle / "projections" / "alpha-go" / "manifest.json").write_text(
        json.dumps({"documents": [{"doc_id": str(i)} for i in range(3695)]}),
        encoding="utf-8",
    )

    bridge = load_estate_bridge(config_path=tmp_path / "estate.json")

    assert bridge.validate(require_alpha_index=False) == []
    problems = bridge.validate(require_alpha_index=True)
    assert problems, "a 10-of-3695-document index must not satisfy the release gate"


def test_bridge_gate_accepts_a_complete_index(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    _build_index(bundle / "indexes" / "alpha_go.db", documents=4)
    (bundle / "projections" / "alpha-go" / "manifest.json").write_text(
        json.dumps({"documents": [{"doc_id": str(i)} for i in range(4)]}),
        encoding="utf-8",
    )

    bridge = load_estate_bridge(config_path=tmp_path / "estate.json")

    assert bridge.validate(require_alpha_index=True) == []
