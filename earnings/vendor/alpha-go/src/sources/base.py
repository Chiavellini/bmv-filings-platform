"""Common acquisition contract for official feeds, IR sites, SEC and manual uploads."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-") or "record"


@dataclass(frozen=True)
class SourceRecord:
    source: str
    source_record_id: str
    company: str
    doc_type: str
    title: str
    canonical_url: str
    ticker: str | None = None
    published_at: str | None = None
    filed_at: str | None = None
    period: str | None = None
    language: str | None = None
    mime_type: str | None = None
    document_family_id: str | None = None
    version: int = 1
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = (self.source, self.source_record_id, self.company, self.doc_type,
                    self.title, self.canonical_url)
        if not all(str(value).strip() for value in required):
            raise ValueError("source records require source identity, company, type, title and URL")

    @property
    def proposed_doc_id(self) -> str:
        """Stable, collision-resistant ID for new multi-document-per-period records."""
        digest = hashlib.sha256(
            f"{self.source}\0{self.source_record_id}".encode("utf-8")
        ).hexdigest()[:12]
        period = _slug(self.period or self.published_at or "undated")
        return f"{_slug(self.company)}/{_slug(self.doc_type)}/{period}/{digest}"


@runtime_checkable
class SourceAdapter(Protocol):
    name: str

    def discover(self) -> Iterable[SourceRecord]:
        """Return normalized source-native records; do not download or index here."""
        ...
