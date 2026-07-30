"""Metadata/link-only GDELT DOC 2.0 discovery adapter.

GDELT's source-side article search verifies a quoted company phrase against its indexed article
text.  We retain only the title, date, publisher domain and canonical link, never the publisher
body.  The original query alias is retained as auditable membership evidence.
"""
from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Iterable

from src.news.aliases import CompanyAlias
from src.news.models import NewsArticle


_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"


class NewsDiscoveryError(RuntimeError):
    """A provider-side failure that should not corrupt or halt an existing news corpus."""


def _published(value: str) -> str:
    """Turn the documented ``YYYYMMDDTHHMMSSZ`` value into an ISO-8601 timestamp."""
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        return parsed.isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return value or "1970-01-01T00:00:00+00:00"


def _language(value: str) -> str:
    value = (value or "unknown").strip().casefold()
    return {"english": "en", "spanish": "es"}.get(value, value or "unknown")


class GdeltNewsAdapter:
    """Discover recent articles matching one controlled company alias.

    The adapter is deliberately per alias.  A multi-company OR query would not identify which
    issuer phrase matched each resulting article and would weaken deterministic membership.
    """

    def __init__(self, alias: CompanyAlias, *, timespan: str = "14d", max_records: int = 25,
                 timeout: int = 30):
        if alias.kind not in {"legal_name", "manual"}:
            raise ValueError("GDELT discovery requires a reviewed legal-name or manual alias")
        if not 1 <= max_records <= 250:
            raise ValueError("max_records must be between 1 and 250")
        self.alias, self.timespan = alias, timespan
        self.max_records, self.timeout = max_records, timeout

    @property
    def query(self) -> str:
        # Quoting preserves a phrase match, avoiding generic semantic/entity classification.
        return f'"{self.alias.alias.replace(chr(34), "")}"'

    @property
    def url(self) -> str:
        params = {
            "query": self.query,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(self.max_records),
            "timespan": self.timespan,
            "sort": "datedesc",
        }
        return f"{_ENDPOINT}?{urllib.parse.urlencode(params)}"

    def discover(self) -> Iterable[NewsArticle]:
        request = urllib.request.Request(self.url, headers={"User-Agent": "alpha-go-news/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310: fixed endpoint
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise NewsDiscoveryError(f"GDELT discovery failed for {self.alias.company}: {exc}") from exc
        seen_urls: set[str] = set()
        for item in payload.get("articles", []) or []:
            url = str(item.get("url") or "").strip()
            title = " ".join(str(item.get("title") or "").split())
            if not url or not title or url in seen_urls:
                continue
            seen_urls.add(url)
            provider_id = str(item.get("url") or hashlib.sha256(url.encode("utf-8")).hexdigest())
            domain = str(item.get("domain") or urllib.parse.urlparse(url).netloc or "GDELT")
            seen_date = str(item.get("seendate") or "")
            yield NewsArticle(
                provider="gdelt_doc_2", provider_id=provider_id, canonical_url=url, title=title,
                publisher=domain, published_at=_published(seen_date),
                language=_language(str(item.get("language") or "unknown")),
                content_mode="metadata_link",
                metadata={
                    "provider_matched_aliases": [self.alias.alias],
                    "provider_match_type": "source_full_text_exact_phrase",
                    "provider_query": self.query,
                    "source_country": item.get("sourcecountry"),
                    "social_image": item.get("socialimage"),
                },
            )
