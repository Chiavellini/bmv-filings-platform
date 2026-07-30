"""GDELT discovery remains link-only and carries auditable exact-phrase evidence."""
from __future__ import annotations

import json
import urllib.error

import pytest

from src.index.embeddings import HashingEmbedder
from src.index.store import IndexStore
from src.news.aliases import CompanyAlias, CompanyAliasResolver
from src.news.catalog import NewsCatalog
from src.news.gdelt import GdeltNewsAdapter, NewsDiscoveryError
from src.news.ingest import ingest_article


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_gdelt_discovery_is_metadata_link_only_and_records_exact_query_evidence(monkeypatch):
    alias = CompanyAlias("bimbo", "food", "Grupo Bimbo", "legal_name")
    observed = {}

    def fake_open(request, timeout):
        observed["url"] = request.full_url
        assert timeout == 30
        return _Response({"articles": [{
            "url": "https://publisher.test/news/1", "title": "New plant announced",
            "domain": "publisher.test", "seendate": "20260721T120000Z", "language": "Spanish",
        }]})

    monkeypatch.setattr("src.news.gdelt.urllib.request.urlopen", fake_open)
    article = next(iter(GdeltNewsAdapter(alias).discover()))
    assert "query=%22Grupo+Bimbo%22" in observed["url"]
    assert article.content_mode == "metadata_link"
    assert article.body == ""
    assert article.content_text == "New plant announced"
    assert article.language == "es"
    assert article.published_at == "2026-07-21T12:00:00+00:00"
    assert article.metadata["provider_matched_aliases"] == ["Grupo Bimbo"]
    assert article.metadata["provider_match_type"] == "source_full_text_exact_phrase"


def test_provider_verified_alias_can_attach_a_title_that_omits_the_company(tmp_path):
    alias = CompanyAlias("bimbo", "food", "Grupo Bimbo", "legal_name")
    monkeypatch_payload = {"articles": [{
        "url": "https://publisher.test/news/1", "title": "New plant announced",
        "domain": "publisher.test", "seendate": "20260721T120000Z", "language": "English",
    }]}

    def fake_open(_request, timeout):
        assert timeout == 30
        return _Response(monkeypatch_payload)

    # Keep the provider response fixture local; ingestion must not depend on body text.
    import src.news.gdelt as gdelt
    original = gdelt.urllib.request.urlopen
    gdelt.urllib.request.urlopen = fake_open
    try:
        article = next(iter(GdeltNewsAdapter(alias).discover()))
    finally:
        gdelt.urllib.request.urlopen = original
    store = IndexStore(tmp_path / "news-index.db")
    store.connect()
    store.migrate()
    with NewsCatalog(tmp_path / "news-catalog.db") as catalog:
        result = ingest_article(
            article, resolver=CompanyAliasResolver([alias]), catalog=catalog,
            corpus_dir=tmp_path / "news-corpus", store=store,
            config={"index": {"chunk": {"target_chars": 160, "overlap_chars": 20}}},
            embedder=HashingEmbedder(dim=32),
        )
        assert result["memberships"] == 1
        assert catalog.stats().articles == 1


def test_gdelt_provider_errors_are_reported_without_a_raw_transport_trace(monkeypatch):
    alias = CompanyAlias("bimbo", "food", "Grupo Bimbo", "legal_name")

    def unavailable(_request, timeout):
        assert timeout == 30
        raise urllib.error.HTTPError("https://example.test", 429, "Too Many Requests", {}, None)

    monkeypatch.setattr("src.news.gdelt.urllib.request.urlopen", unavailable)
    with pytest.raises(NewsDiscoveryError, match="bimbo.*429"):
        list(GdeltNewsAdapter(alias).discover())
