"""Normalized, provider-neutral news records.

Only ``licensed_full_text`` articles may store a full body.  The default ``metadata_link`` mode
stores a headline, permitted summary and canonical publisher link, which is safe for a pilot and
still supports alerts / search over the supplied metadata.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

_CONTENT_MODES = frozenset({"metadata_link", "licensed_full_text"})


@dataclass(frozen=True)
class NewsArticle:
    provider: str
    provider_id: str
    canonical_url: str
    title: str
    publisher: str
    published_at: str
    language: str = "unknown"
    summary: str = ""
    body: str = ""
    content_mode: str = "metadata_link"
    author: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not all(str(v).strip() for v in (
            self.provider, self.provider_id, self.canonical_url, self.title, self.publisher,
            self.published_at,
        )):
            raise ValueError("news articles require provider identity, URL, title, publisher and date")
        if self.content_mode not in _CONTENT_MODES:
            raise ValueError(f"unsupported content_mode: {self.content_mode!r}")
        if self.content_mode != "licensed_full_text" and self.body:
            raise ValueError("full article body requires content_mode='licensed_full_text'")

    @property
    def article_id(self) -> str:
        digest = hashlib.sha256(
            f"{self.provider}\0{self.provider_id}".encode("utf-8")
        ).hexdigest()[:16]
        return f"news/{digest}"

    @property
    def content_text(self) -> str:
        """The locally searchable text, constrained by the source entitlement."""
        parts = [self.title.strip(), self.summary.strip()]
        if self.content_mode == "licensed_full_text":
            parts.append(self.body.strip())
        return "\n\n".join(part for part in parts if part)

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NewsMembership:
    company: str
    industry: str | None
    matched_alias: str
    alias_kind: str
