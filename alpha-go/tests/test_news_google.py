"""Google News RSS remains metadata-only and carries auditable exact-phrase evidence."""
from __future__ import annotations

import pytest

from src.news.aliases import CompanyAlias
from src.news.google_news import GoogleNewsRssAdapter
from src.news.models import NewsArticle
from src.news.rss import RssNewsAdapter


def test_google_news_records_exact_phrase_query_evidence(monkeypatch):
    alias = CompanyAlias("bimbo", "food", "Grupo Bimbo", "legal_name")
    article = NewsArticle(
        provider="rss:fixture", provider_id="original", canonical_url="https://news.test/1",
        title="New plant", publisher="Google News", published_at="2026-07-24T00:00:00+00:00",
        language="es", summary="", content_mode="metadata_link",
    )

    monkeypatch.setattr("src.news.google_news.RssNewsAdapter.discover", lambda _self: iter([article]))
    found = list(GoogleNewsRssAdapter(
        alias, language="es", hl="es-419", gl="MX", ceid="MX:es-419",
    ).discover())

    assert len(found) == 1
    assert found[0].provider == "google_news_rss"
    assert found[0].content_mode == "metadata_link"
    assert found[0].body == ""
    assert found[0].metadata["provider_matched_aliases"] == ["Grupo Bimbo"]
    assert found[0].metadata["provider_match_type"] == "search_rss_exact_phrase"
    assert "Grupo+Bimbo" in GoogleNewsRssAdapter(
        alias, language="es", hl="es-419", gl="MX", ceid="MX:es-419",
    ).url


def test_google_adapter_rejects_nonpositive_timeout():
    alias = CompanyAlias("bimbo", "food", "Grupo Bimbo", "legal_name")
    with pytest.raises(ValueError, match="timeout"):
        GoogleNewsRssAdapter(alias, language="es", hl="es-419", gl="MX", ceid="MX:es-419",
                             timeout=0)


def test_rss_falls_back_when_host_expat_extension_is_unavailable(monkeypatch):
    class Response:
        def read(self):
            return (b"<rss><channel><item><title>Grupo Bimbo expands</title>"
                    b"<link>https://publisher.test/story</link><guid>one</guid>"
                    b"<pubDate>Mon, 20 Jul 2026 12:00:00 GMT</pubDate>"
                    b"<description>Expansion news</description></item></channel></rss>")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("src.news.rss.urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr("src.news.rss.ET.fromstring", lambda _payload: (_ for _ in ()).throw(ImportError()))
    article = next(iter(RssNewsAdapter(
        name="fixture", url="https://feed.test/rss", publisher="Fixture", language="en",
    ).discover()))
    assert article.title == "Grupo Bimbo expands"
    assert article.canonical_url == "https://publisher.test/story"
    assert article.summary == "Expansion news"
