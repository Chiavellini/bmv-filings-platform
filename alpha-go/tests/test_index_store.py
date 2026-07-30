"""Tests for IndexStore schema + write/read helpers (Phase 2)."""
from __future__ import annotations

from src.index.store import TABLES, IndexStore


def _open(tmp_path) -> IndexStore:
    store = IndexStore(tmp_path / "idx.db")
    store.connect()
    store.migrate()
    return store


def test_migrate_creates_all_tables(tmp_path):
    store = _open(tmp_path)
    conn = store.connect()
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
    )}
    for t in TABLES:
        assert t in names
    assert store.get_meta("schema_version") == "1"


def test_migrate_idempotent(tmp_path):
    store = _open(tmp_path)
    store.migrate()  # second time must not raise
    assert store.get_meta("schema_version") == "1"


def test_count_rejects_unknown_table(tmp_path):
    store = _open(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        store.count("; DROP TABLE documents")


def test_clear_removes_rows(tmp_path):
    from src.corpus.manifest import Document
    from src.index.chunker import Chunk

    store = _open(tmp_path)
    doc = Document("d/2024-1T", "d", "2024-1T", "report", "D 2024-1T",
                   None, None, "x.md", "en")
    store.upsert_document(doc)
    store.insert_chunk(Chunk("d/2024-1T#0", "d/2024-1T", 0, "hello", 0, 5, None))
    store.commit()
    assert store.count("documents") == 1 and store.count("chunks") == 1
    store.clear()
    assert store.count("documents") == 0 and store.count("chunks") == 0
