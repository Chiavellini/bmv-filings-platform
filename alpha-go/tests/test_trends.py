"""Mention-trend analytics — FTS-backed per-(company, period) counts + lexicon sentiment."""
from __future__ import annotations

from src.corpus.manifest import Document
from src.search.trends import mention_trend
from src.search.filters import SearchFilters


def test_trend_covers_both_fixture_quarters(built_index):
    store, _ = built_index
    points = mention_trend(store, "revenue")
    periods = {p.period for p in points}
    assert {"2024-1T", "2024-2T"} <= periods
    assert all(p.company == "acme" for p in points)
    assert all(p.mentions >= 1 for p in points)
    assert all(-1.0 <= p.sentiment <= 1.0 for p in points)
    assert all(p.positive + p.neutral + p.negative == p.mentions for p in points)


def test_trend_sorted_by_company_then_period(built_index):
    store, _ = built_index
    points = mention_trend(store, "revenue")
    assert [(p.company, p.period) for p in points] == sorted(
        (p.company, p.period) for p in points)


def test_trend_company_scope(built_index):
    store, _ = built_index
    assert mention_trend(store, "revenue", companies=["acme"])
    assert mention_trend(store, "revenue", companies=["nobody"]) == []


def test_trend_unmatched_term_empty(built_index):
    store, _ = built_index
    assert mention_trend(store, "zzzqqqxxx") == []


def test_trend_stopword_only_term_empty(built_index):
    store, _ = built_index
    assert mention_trend(store, "the of and") == []


def test_trend_deterministic(built_index):
    store, _ = built_index
    assert mention_trend(store, "revenue") == mention_trend(store, "revenue")


def test_trend_uses_metric_synonym_expansion(built_index):
    store, _ = built_index
    # The fixture is English, while "ventas netas" is a Spanish revenue alias. Trends must reuse
    # the concept dictionary rather than requiring the exact query spelling in every document.
    assert mention_trend(store, "ventas netas")


def test_trend_honors_shared_search_filters(built_index):
    store, _ = built_index
    assert mention_trend(store, "revenue", filters=SearchFilters(companies=["acme"]))
    assert mention_trend(store, "revenue", filters=SearchFilters(companies=["nobody"])) == []


def test_trend_attributes_a_shared_document_to_the_selected_membership(built_index):
    from src.index.keyword_index import KeywordIndex

    store, _ = built_index
    row = KeywordIndex(store).matching_documents("revenue")[0]
    store.set_document_companies(
        row["doc_id"],
        [{"company": "acme", "industry": "retail"},
         {"company": "globex", "industry": "materials"}],
    )
    store.commit()

    points = mention_trend(
        store, "revenue", filters=SearchFilters(companies=["globex"]),
    )
    assert points
    assert {point.company for point in points} == {"globex"}


def test_trend_includes_undated_relevant_event_in_doc_id_quarter(built_index):
    store, _ = built_index
    markdown_path = store.db_path.parent / "undated-relevant-event.md"
    markdown_path.write_text("Revenue increased after the announcement.", encoding="utf-8")
    doc = Document(
        doc_id="acme/relevant_event/2024-10-23t08-57/example",
        company="acme",
        period=None,
        doc_type="relevant_event",
        title="Relevant event",
        source_url=None,
        pdf_path=None,
        markdown_path=str(markdown_path),
        language="en",
    )
    store.upsert_document(doc)
    store.set_document_companies(
        doc.doc_id, [{"company": "acme", "industry": "retail"}],
    )
    store.commit()

    points = mention_trend(
        store, "revenue", filters=SearchFilters(companies=["acme"]),
    )
    point = next(point for point in points if point.period == "2024-4T")
    assert point.mentions >= 1


def test_trend_keeps_truly_undated_uploads_visible(built_index):
    store, _ = built_index
    markdown_path = store.db_path.parent / "undated-upload.md"
    markdown_path.write_text("Revenue was disclosed in the upload.", encoding="utf-8")
    doc = Document(
        doc_id="acme/upload/no-date",
        company="acme",
        period=None,
        doc_type="upload",
        title="Undated upload",
        source_url=None,
        pdf_path=None,
        markdown_path=str(markdown_path),
        language="en",
    )
    store.upsert_document(doc)
    store.set_document_companies(
        doc.doc_id, [{"company": "acme", "industry": "retail"}],
    )
    store.commit()

    points = mention_trend(
        store, "revenue", filters=SearchFilters(companies=["acme"]),
    )
    assert any(point.period == "Undated" and point.mentions >= 1 for point in points)
