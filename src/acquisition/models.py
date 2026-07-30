"""Normalized domain models for the shared document-acquisition boundary.

These models deliberately contain no downloader, scheduler, or estate logic.
Adapters discover :class:`SourceRecord` instances, fetch them into
:class:`FetchedArtifact` instances, and hand those values to the acquisition
ledger/writer.  Keeping that boundary small lets Alpha Go, Soft, and Earnings
share one acquisition engine without importing one another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import re
from typing import Any, Mapping


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_PERIOD_RE = re.compile(r"^(\d{4})-([1-4])T$")
_PRIMARY_PDF_SOURCE_KINDS = frozenset(
    {"investor_relations", "bmv_issuer_pdf"}
)


@dataclass(frozen=True, slots=True)
class ProjectMembership:
    """Explicit product membership for an issuer.

    Alpha Go is the broad document-search product, Soft owns the original
    coverage universe, and Earnings is intentionally opt-in.
    """

    alpha_go: bool = False
    soft: bool = False
    earnings: bool = False

    def includes(self, project: str) -> bool:
        """Return whether the issuer belongs to a known project."""

        normalized = project.strip().lower().replace("-", "_")
        if normalized not in {"alpha_go", "soft", "earnings"}:
            raise KeyError(f"unknown project membership: {project!r}")
        return bool(getattr(self, normalized))


@dataclass(frozen=True, slots=True)
class AcquisitionSource:
    """One configured discovery source for an issuer."""

    key: str
    kind: str
    enabled: bool = True
    url: str | None = None
    xbrl_ticker: str | None = None
    pdf_link_pattern: str | None = None
    direct_url_templates: tuple[str, ...] = ()
    year_api_urls: tuple[str, ...] = ()
    use_playwright: bool | None = None
    max_reports: int | None = None
    floor_year: int | None = None
    coverage_from_period: str | None = None
    live_verified_period: str | None = None
    live_verified_on: date | None = None
    live_verified_url: str | None = None
    strict_pdf_link_pattern: bool = False
    delay_ms: int | None = None
    impersonate: str | None = None
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.key, "source key")
        _require_text(self.kind, "source kind")
        if self.url is not None:
            _require_text(self.url, f"{self.key} url")
        if (
            self.enabled
            and self.kind in _PRIMARY_PDF_SOURCE_KINDS
            and self.url is None
        ):
            raise ValueError(
                f"{self.key}: enabled {self.kind} source requires a URL"
            )
        if self.xbrl_ticker is not None:
            _require_text(self.xbrl_ticker, f"{self.key} xbrl_ticker")
        if self.max_reports is not None and self.max_reports <= 0:
            raise ValueError(f"{self.key}: max_reports must be positive")
        if self.floor_year is not None and not 1900 <= self.floor_year <= 2200:
            raise ValueError(f"{self.key}: floor_year is out of range")
        if self.coverage_from_period is not None:
            _validate_period(
                self.coverage_from_period,
                f"{self.key}: coverage_from_period",
            )
        verification = (
            self.live_verified_period,
            self.live_verified_on,
            self.live_verified_url,
        )
        if any(value is not None for value in verification):
            if not all(value is not None for value in verification):
                raise ValueError(
                    f"{self.key}: live verification requires period, date, and URL"
                )
            _validate_period(
                self.live_verified_period,
                f"{self.key}: live_verified_period",
            )
            if not isinstance(self.live_verified_on, date):
                raise ValueError(
                    f"{self.key}: live_verified_on must be an ISO date"
                )
            _require_text(
                self.live_verified_url,
                f"{self.key}: live_verified_url",
            )
            if (
                self.coverage_from_period is not None
                and _period_key(self.live_verified_period)
                < _period_key(self.coverage_from_period)
            ):
                raise ValueError(
                    f"{self.key}: live_verified_period cannot precede "
                    "coverage_from_period"
                )
            if (
                self.floor_year is not None
                and _period_key(self.live_verified_period)
                < (self.floor_year, 1)
            ):
                raise ValueError(
                    f"{self.key}: live_verified_period cannot precede "
                    "floor_year"
                )
        if self.strict_pdf_link_pattern and not self.pdf_link_pattern:
            raise ValueError(
                f"{self.key}: strict_pdf_link_pattern requires pdf_link_pattern"
            )
        if self.delay_ms is not None and self.delay_ms < 0:
            raise ValueError(f"{self.key}: delay_ms cannot be negative")
        object.__setattr__(self, "direct_url_templates", tuple(self.direct_url_templates))
        object.__setattr__(self, "year_api_urls", tuple(self.year_api_urls))
        object.__setattr__(self, "options", dict(self.options))


@dataclass(frozen=True, slots=True)
class IssuerSpec:
    """Canonical identity, classification, membership, and sources for an issuer."""

    slug: str
    ticker: str
    name: str
    sector: str
    template: str
    exchange: str = "BMV"
    language: str | None = None
    currency: str = "MXN"
    unit: str | None = None
    market_ticker: str | None = None
    memberships: ProjectMembership = field(default_factory=ProjectMembership)
    sources: tuple[AcquisitionSource, ...] = ()
    active: bool = True
    listed_from: date | None = None
    listed_to: date | None = None
    fiscal_year_end_month: int = 12
    filing_grace_days: int = 45

    def __post_init__(self) -> None:
        if not _SLUG_RE.fullmatch(self.slug):
            raise ValueError(f"invalid issuer slug: {self.slug!r}")
        for value, label in (
            (self.ticker, "ticker"),
            (self.name, "name"),
            (self.sector, "sector"),
            (self.template, "template"),
            (self.exchange, "exchange"),
            (self.currency, "currency"),
        ):
            _require_text(value, f"{self.slug} {label}")
        if self.language is not None and self.language not in {"en", "es", "bilingual"}:
            raise ValueError(f"{self.slug}: unsupported language {self.language!r}")
        if self.listed_from and self.listed_to and self.listed_from > self.listed_to:
            raise ValueError(f"{self.slug}: listed_from cannot follow listed_to")
        if self.fiscal_year_end_month not in range(1, 13):
            raise ValueError(f"{self.slug}: fiscal_year_end_month must be 1..12")
        if self.filing_grace_days < 0:
            raise ValueError(f"{self.slug}: filing_grace_days cannot be negative")
        sources = tuple(self.sources)
        source_keys = [source.key for source in sources]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError(f"{self.slug}: duplicate acquisition source key")
        object.__setattr__(self, "sources", sources)

    def source(self, key: str) -> AcquisitionSource:
        """Return a configured source by stable key."""

        for source in self.sources:
            if source.key == key:
                return source
        raise KeyError(f"{self.slug}: unknown acquisition source {key!r}")

    def is_member(self, project: str) -> bool:
        return self.memberships.includes(project)


@dataclass(frozen=True, slots=True)
class SourceRecord:
    """A durable discovery result before response bytes are fetched.

    ``source_key`` identifies the configured adapter/binding.  The separate
    ``source_record_id`` is the stable accession, article id, or normalized URL
    exposed by that source.  The acquisition ledger uses the pair for
    idempotency.
    """

    source_key: str
    source_record_id: str
    issuer_slug: str
    url: str | None = None
    document_type: str = "quarterly_release"
    title: str | None = None
    published_at: datetime | None = None
    discovered_at: datetime | None = None
    period_year: int | None = None
    period_quarter: int | None = None
    document_family_id: str | None = None
    language: str | None = None
    rendition: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for value, label in (
            (self.source_key, "source_key"),
            (self.source_record_id, "source_record_id"),
            (self.issuer_slug, "issuer_slug"),
            (self.document_type, "document_type"),
        ):
            _require_text(value, label)
        if not _SLUG_RE.fullmatch(self.issuer_slug):
            raise ValueError(f"invalid issuer slug: {self.issuer_slug!r}")
        if self.url is not None:
            _require_text(self.url, "url")
        if (self.period_year is None) != (self.period_quarter is None):
            raise ValueError("period_year and period_quarter must be set together")
        if self.period_year is not None and not 1900 <= self.period_year <= 2200:
            raise ValueError("period_year is out of range")
        if self.period_quarter is not None and self.period_quarter not in {1, 2, 3, 4}:
            raise ValueError("period_quarter must be 1..4")
        if self.language is not None:
            _require_text(self.language, "language")
        rendition = self.rendition or "default"
        if not _SLUG_RE.fullmatch(rendition):
            raise ValueError(f"invalid rendition: {rendition!r}")
        object.__setattr__(self, "rendition", rendition)
        if self.document_family_id is not None:
            _require_text(self.document_family_id, "document_family_id")
        elif self.period is not None:
            language = self.language.lower() if self.language else "und"
            object.__setattr__(
                self,
                "document_family_id",
                (
                    f"{self.issuer_slug}:{self.document_type}:{self.period}:"
                    f"{language}:{rendition}"
                ),
            )
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def company(self) -> str:
        """Compatibility alias used by the acquisition ledger."""

        return self.issuer_slug

    @property
    def doc_type(self) -> str:
        """Compatibility alias used by the estate writer."""

        return self.document_type

    @property
    def period(self) -> str | None:
        if self.period_year is None or self.period_quarter is None:
            return None
        return f"{self.period_year}-{self.period_quarter}T"


@dataclass(frozen=True, slots=True)
class FetchedArtifact:
    """Fetched response bytes plus transport metadata."""

    source: SourceRecord
    content: bytes
    role: str = "original"
    filename: str | None = None
    media_type: str = "application/pdf"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    response_status: int | None = None
    response_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source, SourceRecord):
            raise TypeError("source must be a SourceRecord")
        if not isinstance(self.content, bytes):
            raise TypeError("content must be bytes")
        if not self.content:
            raise ValueError("content cannot be empty")
        if not _SLUG_RE.fullmatch(self.role):
            raise ValueError(f"invalid artifact role: {self.role!r}")
        _require_text(self.media_type, "media_type")
        if self.response_status is not None and not 100 <= self.response_status <= 599:
            raise ValueError("response_status must be a valid HTTP status")
        object.__setattr__(self, "response_headers", dict(self.response_headers))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")


def _period_key(value: str | None) -> tuple[int, int]:
    if value is None:
        raise ValueError("period cannot be None")
    match = _PERIOD_RE.fullmatch(value)
    if match is None:
        raise ValueError("period must use YYYY-NT")
    return int(match.group(1)), int(match.group(2))


def _validate_period(value: str | None, label: str) -> None:
    try:
        year, _quarter = _period_key(value)
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-NT") from exc
    if not 1900 <= year <= 2200:
        raise ValueError(f"{label} year is out of range")
