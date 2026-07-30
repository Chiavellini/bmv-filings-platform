"""Shared fixtures for the search (Phase 3) tests.

Builds a real index over the offline ACME fixture corpus using the deterministic
``HashingEmbedder`` so the whole hybrid pipeline (keyword + semantic + fusion) is exercised
without a model download or network. The same embedder instance is reused at query time so
the semantic dimensionality matches.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.corpus.ingest import ingest_source
from src.index.build import build_index
from src.index.embeddings import HashingEmbedder
from src.index.store import IndexStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_corpus" / "acme"
_EMBED_DIM = 64


@pytest.fixture
def built_index(tmp_path):
    """Return ``(store, embedder)`` for an index built over the ACME fixture corpus."""
    corpus_dir = tmp_path / "corpus"
    dest = corpus_dir / "acme"
    dest.mkdir(parents=True)
    for md in FIXTURE.glob("*.md"):
        shutil.copy2(md, dest / md.name)
    ingest_source(
        {"slug": "acme", "company": "Acme Corp", "doc_types": ["report"],
         "language": "en", "ir_website": {}},
        corpus_dir,
    )

    db = tmp_path / "index" / "alpha.db"
    config = {"index": {"embedding_backend": "hashing", "hashing_dim": _EMBED_DIM,
                        "chunk": {"target_chars": 400, "overlap_chars": 50}}}
    embedder = HashingEmbedder(dim=_EMBED_DIM)
    build_index(corpus_dir, db, config, embedder=embedder)

    store = IndexStore(db)
    store.connect()
    return store, embedder
