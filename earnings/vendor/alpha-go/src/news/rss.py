"""Minimal RSS/Atom metadata adapter for approved publisher feeds.

It deliberately returns metadata/link records.  A feed must explicitly be configured as
``licensed_full_text`` before a provider-supplied full body is accepted by the news corpus.
"""
from __future__ import annotations

import email.utils
import hashlib
import html
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timezone
from typing import Iterable

from src.news.models import NewsArticle


def _text(element, *names: str) -> str:
    for name in names:
        found = element.find(name)
        if found is not None and found.text:
            return " ".join(html.unescape(found.text).split())
    return ""


def _published(value: str) -> str:
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return value or "1970-01-01T00:00:00+00:00"


def _fallback_text(value: str) -> str:
    """Decode a simple RSS/Atom field when the host XML extension is unavailable.

    This is intentionally a bounded fallback, not a general XML parser: the adapter consumes
    public syndication feeds and only needs title/link/description/date/id fields.  It keeps the
    ingestion path working on hosts where Python's optional ``pyexpat`` module is mis-linked.
    """
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value or "")).split())


def _fallback_field(block: str, *names: str) -> str:
    for name in names:
        escaped = re.escape(name)
        match = re.search(
            rf"<(?:[\w-]+:)?{escaped}\b[^>]*>(.*?)</(?:[\w-]+:)?{escaped}\s*>",
            block, flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return _fallback_text(match.group(1))
    return ""


def _fallback_link(block: str) -> str:
    match = re.search(r"<(?:[\w-]+:)?link\b[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>",
                      block, flags=re.IGNORECASE)
    if match:
        return html.unescape(match.group(1)).strip()
    return _fallback_field(block, "link")


def _fallback_items(payload: bytes):
    text = payload.decode("utf-8", errors="replace")
    blocks = re.findall(r"<(?:[\w-]+:)?item\b[^>]*>(.*?)</(?:[\w-]+:)?item\s*>", text,
                        flags=re.IGNORECASE | re.DOTALL)
    if not blocks:
        blocks = re.findall(r"<(?:[\w-]+:)?entry\b[^>]*>(.*?)</(?:[\w-]+:)?entry\s*>", text,
                            flags=re.IGNORECASE | re.DOTALL)
    for block in blocks:
        yield {
            "title": _fallback_field(block, "title"),
            "link": _fallback_link(block),
            "summary": _fallback_field(block, "description", "summary", "content"),
            "provider_id": _fallback_field(block, "guid", "id"),
            "published": _fallback_field(block, "pubDate", "published", "updated"),
        }


class RssNewsAdapter:
    """One approved RSS feed. Network access happens only when a sync command invokes it."""

    def __init__(self, *, name: str, url: str, publisher: str, language: str = "unknown",
                 content_mode: str = "metadata_link", timeout: int = 20):
        self.name, self.url, self.publisher = name, url, publisher
        self.language, self.content_mode, self.timeout = language, content_mode, timeout

    def discover(self) -> Iterable[NewsArticle]:
        request = urllib.request.Request(self.url, headers={"User-Agent": "alpha-go-news/1.0"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310: configured URL
            payload = response.read()
        try:
            root = ET.fromstring(payload)
            items = list(root.findall(".//item"))
            # Atom feeds use a namespace; generic suffix matching keeps the adapter dependency-free.
            if not items:
                items = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "entry"]
            parsed = []
            for item in items:
                link = _text(item, "link")
                if not link:
                    link_node = next((n for n in item if n.tag.rsplit("}", 1)[-1] == "link"), None)
                    link = (link_node.get("href") if link_node is not None else "") or ""
                parsed.append({
                    "title": _text(item, "title"), "link": link,
                    "summary": _text(item, "description", "summary", "content"),
                    "provider_id": _text(item, "guid", "id"),
                    "published": _text(item, "pubDate", "published", "updated"),
                })
        except (ImportError, ET.ParseError):
            parsed = list(_fallback_items(payload))
        for item in parsed:
            title, link = item["title"], item["link"]
            summary = item["summary"]
            provider_id = item["provider_id"] or hashlib.sha256(link.encode("utf-8")).hexdigest()
            published = _published(item["published"])
            if title and link:
                yield NewsArticle(
                    provider=f"rss:{self.name}", provider_id=provider_id, canonical_url=link,
                    title=title, publisher=self.publisher, published_at=published,
                    language=self.language, summary=summary, content_mode=self.content_mode,
                )
