"""End-to-end index build over the Phase-1 fixture corpus (Phase 2, offline)."""
from __future__ import annotations

import shutil
from pathlib import Path

from src.corpus.ingest import ingest_source
from src.index.build import build_index
from src.index.embeddings import HashingEmbedder
from src.index.store import IndexStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_corpus" / "acme"
CONFIG = {"index": {"embedding_backend": "hashing", "chunk": {"target_chars": 400, "overlap_chars": 50}}}


def _seed_corpus(tmp_path: Path) -> Path:
    corpus_dir = tmp_path / "corpus"
    dest = corpus_dir / "acme"
    dest.mkdir(parents=True)
    for md in FIXTURE.glob("*.md"):
        shutil.copy2(md, dest / md.name)
    ingest_source({"slug": "acme", "company": "Acme Corp", "doc_types": ["report"],
                   "language": "en", "ir_website": {}}, corpus_dir)
    return corpus_dir


def test_build_index_stats_and_tables(tmp_path):
    corpus_dir = _seed_corpus(tmp_path)
    db = tmp_path / "index" / "alpha.db"
    stats = build_index(corpus_dir, db, CONFIG, embedder=HashingEmbedder(dim=64))

    assert stats["documents"] == 2
    assert stats["chunks"] >= 2
    assert stats["embedded"] == stats["chunks"]
    assert stats["embedding_dim"] == 64
    assert db.exists()

    store = IndexStore(db)
    store.connect()
    assert store.count("documents") == 2
    assert store.count("chunks") == stats["chunks"]
    assert store.count("embeddings") == stats["chunks"]
    assert store.get_meta("embedding_dim") == "64"


def test_fts5_match_finds_chunk(tmp_path):
    corpus_dir = _seed_corpus(tmp_path)
    db = tmp_path / "index" / "alpha.db"
    build_index(corpus_dir, db, CONFIG, embedder=HashingEmbedder(dim=64))

    conn = IndexStore(db).connect()
    rows = conn.execute(
        "SELECT chunk_id, bm25(chunks_fts) AS score FROM chunks_fts "
        "WHERE chunks_fts MATCH ? ORDER BY score",
        ["revenues"],
    ).fetchall()
    assert rows, "expected an FTS5 hit for 'revenues'"
    # The matched chunk's text actually contains the term.
    top = rows[0]["chunk_id"]
    text = conn.execute("SELECT text FROM chunks WHERE chunk_id=?", [top]).fetchone()["text"]
    assert "Revenues" in text


def test_rebuild_is_idempotent(tmp_path):
    corpus_dir = _seed_corpus(tmp_path)
    db = tmp_path / "index" / "alpha.db"
    s1 = build_index(corpus_dir, db, CONFIG, embedder=HashingEmbedder(dim=64))
    s2 = build_index(corpus_dir, db, CONFIG, embedder=HashingEmbedder(dim=64))
    assert s1 == s2
    assert IndexStore(db).connect().execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"] == s2["chunks"]


def test_build_index_auto_backend_offline(tmp_path, monkeypatch):
    """With no embedder injected and backend=auto + ST unavailable, falls back to hashing."""
    import sys
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # hold in any environment
    corpus_dir = _seed_corpus(tmp_path)
    db = tmp_path / "index" / "alpha.db"
    cfg = {"index": {"embedding_backend": "auto", "chunk": {"target_chars": 400, "overlap_chars": 50}}}
    stats = build_index(corpus_dir, db, cfg)
    assert stats["embedding_model"] == "hashing"
    assert stats["embedded"] == stats["chunks"]
