"""Google News RSS discovery for an entitlement-safe company-news backfill.

The adapter stores neither publisher pages nor full text.  It submits one reviewed exact company
name per feed, keeps the RSS headline/summary and Google News link, and records the query as
membership evidence.  That makes it a useful fallback when the GDELT public API is unavailable
while preserving the same deterministic (not learned) company attachment rule.
"""
from __future__ import annotations

import hashlib
import urllib.parse
from dataclasses import replace
from typing import Iterable

from src.news.aliases import CompanyAlias
from src.news.models import NewsArticle
from src.news.rss import RssNewsAdapter


_ENDPOINT = "https://news.google.com/rss/search"


class GoogleNewsRssAdapter:
    """Discover a bounded exact-phrase result set for one controlled company alias."""

    def __init__(self, alias: CompanyAlias, *, language: str, hl: str, gl: str, ceid: str,
                 max_records: int = 100, timeout: int = 12):
        if alias.kind not in {"legal_name", "manual"}:
            raise ValueError("Google News discovery requires a reviewed legal-name or manual alias")
        if not 1 <= max_records <= 100:
            raise ValueError("max_records must be between 1 and 100")
        if timeout < 1:
            raise ValueError("timeout must be positive")
        self.alias = alias
        self.language, self.hl, self.gl, self.ceid = language, hl, gl, ceid
        self.max_records, self.timeout = max_records, timeout

    @property
    def query(self) -> str:
        return f'"{self.alias.alias.replace(chr(34), "")}"'

    @property
    def url(self) -> str:
        return f"{_ENDPOINT}?{urllib.parse.urlencode({
            'q': self.query, 'hl': self.hl, 'gl': self.gl, 'ceid': self.ceid,
        })}"

    def discover(self) -> Iterable[NewsArticle]:
        feed = RssNewsAdapter(
            name=f"google-news-{self.hl}", url=self.url, publisher="Google News",
            language=self.language, content_mode="metadata_link", timeout=self.timeout,
        )
        for ordinal, article in enumerate(feed.discover(), 1):
            if ordinal > self.max_records:
                break
            # The feed GUID is often stable, but source-link redirects and locale variants are
            # not guaranteed to retain it.  Pair a canonical RSS URL with its exact query alias
            # for a deterministic provider ID; catalog-level canonical URL de-duplication joins
            # results returned by more than one company/locale.
            provider_id = hashlib.sha256(
                f"{article.canonical_url}\0{self.alias.alias}".encode("utf-8")
            ).hexdigest()
            yield replace(
                article, provider="google_news_rss", provider_id=provider_id,
                metadata={
                    "provider_matched_aliases": [self.alias.alias],
                    "provider_match_type": "search_rss_exact_phrase",
                    "provider_query": self.query,
                    "feed_locale": {"hl": self.hl, "gl": self.gl, "ceid": self.ceid},
                },
            )
