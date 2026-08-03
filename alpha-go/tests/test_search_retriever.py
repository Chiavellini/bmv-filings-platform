"""End-to-end tests for HybridRetriever.search over the fixture corpus (Phase 3)."""
from __future__ import annotations

from src.search.filters import SearchFilters
from src.search.retriever import HybridRetriever
from src.index.store import IndexStore


def _retriever(built_index):
    store, embedder = built_index
    return HybridRetriever(store, embedder=embedder)


def test_search_returns_hits_with_snippets(built_index):
    hits = _retriever(built_index).search("pricing pressure from e-commerce")
    assert hits
    top = hits[0]
    assert top.company == "acme"
    assert top.period in {"2024-1T", "2024-2T"}
    assert top.snippet.text
    # The hit carries provenance for the doc viewer.
    assert top.markdown_path and top.char_end > top.char_start


def test_empty_query_returns_empty(built_index):
    assert _retriever(built_index).search("   ") == []


def test_results_ranked_by_score_descending(built_index):
    hits = _retriever(built_index).search("revenues ebitda", limit=10)
    assert hits
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_period_filter_narrows_results(built_index):
    r = _retriever(built_index)
    filtered = r.search("revenues", filters=SearchFilters(period_from="2024-2T"))
    assert filtered, "expected a 2Q hit"
    assert all(h.period == "2024-2T" for h in filtered)


def test_company_filter_excludes_everything_when_unmatched(built_index):
    r = _retriever(built_index)
    assert r.search("revenues", filters=SearchFilters(companies=["nonexistent"])) == []


def test_keyword_only_when_no_embedder(built_index):
    store, _ = built_index
    # No embedder + no config -> semantic half is skipped, keyword search still works.
    kw_only = HybridRetriever(store)
    hits = kw_only.search("revenues")
    assert hits and "revenue" in hits[0].snippet.text.lower()


def test_long_lived_retriever_invalidates_caches_after_external_index_commit(
    built_index,
):
    store, _embedder = built_index
    retriever = HybridRetriever(store)
    stale_vector = (["stale-chunk"], object())
    retriever._vector_cache = stale_vector
    retriever._id_index = {"stale-chunk": 0}
    retriever._boiler_cache = object()

    writer = IndexStore(store.db_path)
    writer.connect()
    writer.set_meta("external_projection_test", "committed")
    writer.commit()
    writer.close()

    assert retriever.search("revenues")
    assert retriever._vector_cache is None
    assert retriever._id_index is None
    assert retriever._boiler_cache is None
