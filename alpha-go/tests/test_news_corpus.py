"""Separate news corpus: deterministic company attachment and entitlement-safe indexing."""
from __future__ import annotations

from src.index.embeddings import HashingEmbedder
from src.index.store import IndexStore
from src.news.aliases import CompanyAliasResolver, aliases_from_companies
from src.news.catalog import NewsCatalog
from src.news.ingest import ingest_article
from src.news.models import NewsArticle


COMPANIES = [
    {"slug": "bimbo", "company": "Grupo Bimbo", "ticker": "BIMBO", "industry": "food"},
    {"slug": "ac", "company": "Arca Continental", "ticker": "AC", "industry": "beverage"},
]


def _article(**overrides) -> NewsArticle:
    data = dict(
        provider="fixture", provider_id="1", canonical_url="https://example.test/article/1",
        title="Grupo Bimbo announces a new plant", publisher="Fixture News",
        published_at="2026-07-21T12:00:00+00:00", language="en",
        summary="The Grupo Bimbo investment will expand capacity.", content_mode="metadata_link",
    )
    data.update(overrides)
    return NewsArticle(**data)


def test_resolver_uses_verified_full_names_and_rejects_short_ambiguous_ticker():
    resolver = CompanyAliasResolver(aliases_from_companies(COMPANIES))
    assert [m.company for m in resolver.resolve("Grupo Bimbo reported earnings.")] == ["bimbo"]
    # AC is deliberately absent until a reviewed alias/provider-entity rule is configured.
    assert resolver.resolve("AC power consumption rose in Mexico.") == []


def test_news_ingest_creates_separate_indexed_article_with_company_membership(tmp_path):
    resolver = CompanyAliasResolver(aliases_from_companies(COMPANIES))
    store = IndexStore(tmp_path / "news-index.db")
    store.connect()
    store.migrate()
    with NewsCatalog(tmp_path / "news-catalog.db") as catalog:
        result = ingest_article(
            _article(), resolver=resolver, catalog=catalog, corpus_dir=tmp_path / "news-corpus",
            store=store, config={"index": {"chunk": {"target_chars": 160, "overlap_chars": 20}}},
            embedder=HashingEmbedder(dim=32),
        )
        assert result["indexed"] and result["memberships"] == 1
        assert catalog.stats().articles == 1
        assert catalog.stats().memberships == 1
    row = store.connect().execute("SELECT * FROM documents").fetchone()
    assert row["doc_type"] == "news_article"
    assert row["source_url"] == "https://example.test/article/1"
    assert store.document_companies(row["doc_id"]) == [{"company": "bimbo", "industry": "food"}]


def test_same_canonical_article_deduplicates_without_losing_catalog_integrity(tmp_path):
    resolver = CompanyAliasResolver(aliases_from_companies(COMPANIES))
    with NewsCatalog(tmp_path / "news-catalog.db") as catalog:
        first = ingest_article(_article(), resolver=resolver, catalog=catalog,
                               corpus_dir=tmp_path / "news-corpus")
        second = ingest_article(
            _article(provider="other", provider_id="different"), resolver=resolver, catalog=catalog,
            corpus_dir=tmp_path / "news-corpus",
        )
        assert first["article_id"] == second["article_id"]
        assert catalog.stats().articles == 1


def test_news_article_without_verified_company_is_not_added_to_corpus(tmp_path):
    resolver = CompanyAliasResolver(aliases_from_companies(COMPANIES))
    with NewsCatalog(tmp_path / "news-catalog.db") as catalog:
        result = ingest_article(
            _article(title="Markets move higher", summary="Rates declined in afternoon trading."),
            resolver=resolver, catalog=catalog, corpus_dir=tmp_path / "news-corpus",
        )
        assert result["reason"] == "no verified company alias"
        assert catalog.stats().articles == 0
