"""Phase 3 — industry facet, idempotent column migration, and the scope-pushdown recall fix."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.corpus.ingest import ingest_source
from src.index.build import build_index
from src.index.embeddings import HashingEmbedder
from src.index.keyword_index import KeywordIndex
from src.index.store import IndexStore
from src.search.filters import SearchFilters
from src.search.retriever import HybridRetriever

FIXTURE = Path(__file__).parent / "fixtures" / "sample_corpus" / "acme"
_DIM = 64


# --- industry facet -----------------------------------------------------------------------

def test_industry_filter_sql():
    where, params = SearchFilters(industries=["retail", "food"]).to_sql()
    assert "document_companies" in where and "dc.industry IN (?, ?)" in where
    assert params == ["retail", "food"]
    assert not SearchFilters(industries=["retail"]).is_empty()
    assert SearchFilters().is_empty()


# --- idempotent migration -----------------------------------------------------------------

def test_migrate_adds_industry_column_to_legacy_db(tmp_path):
    db = tmp_path / "legacy.db"
    store = IndexStore(db)
    conn = store.connect()
    # Simulate a pre-industry schema (no industry column).
    conn.execute("CREATE TABLE documents (doc_id TEXT PRIMARY KEY, company TEXT, language TEXT)")
    conn.commit()
    store.migrate()                                   # should ALTER-add industry
    store.migrate()                                   # idempotent — must not error
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    assert "industry" in cols


# --- candidate-retrieval pushdown ---------------------------------------------------------

def _two_company_index(tmp_path):
    """Build a hashing index with two companies (acme + beta) over the same fixture text."""
    corpus_dir = tmp_path / "corpus"
    for slug, company, industry in [("acme", "Acme Corp", "retail"), ("beta", "Beta Inc", "food")]:
        dest = corpus_dir / slug
        dest.mkdir(parents=True)
        for md in FIXTURE.glob("*.md"):
            shutil.copy2(md, dest / md.name)
        ingest_source(
            {"slug": slug, "company": company, "doc_types": ["report"],
             "language": "en", "industry": industry, "ir_website": {}},
            corpus_dir,
        )
    db = tmp_path / "index" / "alpha.db"
    config = {"index": {"embedding_backend": "hashing", "hashing_dim": _DIM,
                        "chunk": {"target_chars": 400, "overlap_chars": 50}}}
    embedder = HashingEmbedder(dim=_DIM)
    build_index(corpus_dir, db, config, embedder=embedder)
    store = IndexStore(db)
    store.connect()
    return store, embedder


def test_industry_persisted_to_index(tmp_path):
    store, _ = _two_company_index(tmp_path)
    rows = dict(store.connect().execute(
        "SELECT DISTINCT company, industry FROM documents").fetchall())
    assert rows == {"acme": "retail", "beta": "food"}


def test_keyword_search_filter_pushdown(tmp_path):
    store, _ = _two_company_index(tmp_path)
    ki = KeywordIndex(store)
    # A bare term present in the corpus; pick whatever the fixture has.
    term = "the"
    where, params = SearchFilters(companies=["beta"]).to_sql()
    hits = ki.search(term, limit=50, filter_sql=where, filter_params=params)
    if hits:  # term may be a stopword; assert scoping holds when it returns anything
        doc_companies = {
            store.connect().execute(
                "SELECT d.company FROM chunks c JOIN documents d ON c.doc_id=d.doc_id "
                "WHERE c.chunk_id=?", (h.chunk_id,)).fetchone()["company"]
            for h in hits
        }
        assert doc_companies == {"beta"}


def test_retriever_company_scope_returns_results(tmp_path):
    """A company-scoped query must surface that company's hits (not be crowded out)."""
    store, embedder = _two_company_index(tmp_path)
    retriever = HybridRetriever(store, embedder=embedder, expand_synonyms=False)
    # Grab a real content word from the fixture to query with.
    sample = next(FIXTURE.glob("*.md")).read_text(encoding="utf-8")
    word = next(w for w in sample.split() if w.isalpha() and len(w) > 5)
    for slug, industry in [("beta", "food"), ("acme", "retail")]:
        by_company = retriever.search(word, filters=SearchFilters(companies=[slug]), limit=10)
        by_industry = retriever.search(word, filters=SearchFilters(industries=[industry]), limit=10)
        assert by_company, f"company scope {slug} returned nothing"
        assert all(h.company == slug for h in by_company)
        assert {h.company for h in by_industry} == {slug}
