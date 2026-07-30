"""Official BMV issuer-page adapter for quarterly narrative PDFs.

The BMV issuer page exposes one current quarterly package per issuer.  The
management-discussion PDF is preferred because it contains the closest BMV
equivalent to an issuer earnings release; the basic-financial-statements PDF is
retained as a fallback when management discussion is absent.

This adapter stops at the acquisition boundary.  It discovers exact source URLs
and returns :class:`FetchedArtifact` values, but never writes the estate itself.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Callable, Mapping
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from src.acquisition.adapters import (
    DiscoveredRecord,
    normalize_period,
    period_parts,
)
from src.acquisition.models import (
    AcquisitionSource,
    FetchedArtifact,
    IssuerSpec,
    SourceRecord,
)


BMV_BASE = "https://www.bmv.com.mx"
BMV_HOSTS = frozenset({"bmv.com.mx", "www.bmv.com.mx"})
_DATE_RE = re.compile(
    r"^(\d{2})[-/]([A-Za-z]{3}|\d{2})[-/](20\d{2})"
    r"(?:\s+(\d{2}):(\d{2}))?"
)
_BMV_PERIOD_PATTERNS = (
    re.compile(
        r"\btrimestre\s*([1-4])\s*(?:del\s+)?(?:a[ñn]o\s+)?(20\d{2})\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bperiodo\s*([1-4])\s*[-/]\s*(20\d{2})\b",
        re.IGNORECASE,
    ),
)
_MONTHS = {
    month.lower(): number
    for number, month in enumerate(
        (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        ),
        1,
    )
}
_SECTIONS = (
    ("COMENTARIOS Y ANÁLISIS DE ADMINISTRACIÓN", "management_discussion", 0),
    ("ESTADOS FINANCIEROS BÁSICOS", "financial_statements", 1),
)


@dataclass(frozen=True, slots=True)
class _NativePdf:
    """Source-native identity retained until the exact PDF is fetched."""

    url: str
    filename: str
    section: str
    priority: int


def _published_at(raw: str) -> datetime | None:
    match = _DATE_RE.search(" ".join((raw or "").split()))
    if not match:
        return None
    day, month, year, hour, minute = match.groups()
    month_number = int(month) if month.isdigit() else _MONTHS.get(month.lower())
    if not month_number:
        return None
    return datetime(
        int(year),
        month_number,
        int(day),
        int(hour or 0),
        int(minute or 0),
    )


def _table_after_heading(soup: BeautifulSoup, heading: str):
    expected = heading.casefold()
    for tag in soup.find_all(["h2", "h3", "h4"]):
        if expected in tag.get_text(" ", strip=True).casefold():
            # A missing/empty section must not borrow a table from the next
            # section. BMV pages place unrelated analyst and certificate
            # tables under adjacent headings.
            for candidate in tag.find_all_next(
                ["h2", "h3", "h4", "table"]
            ):
                if candidate.name in {"h2", "h3", "h4"}:
                    break
                if candidate.name == "table":
                    return candidate
            return None
    return None


def _pdf_anchor(row):
    for anchor in row.find_all("a", href=True):
        path = urlparse(anchor["href"]).path.lower()
        if path.endswith(".pdf"):
            return anchor
    return None


def _validated_bmv_url(url: str, *, label: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme.lower() != "https"
        or (parsed.hostname or "").lower() not in BMV_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError(
            f"{label} must be an HTTPS URL on an official BMV host"
        )
    return url


def _quarterly_period(title: str) -> str | None:
    normalized = normalize_period(title)
    if normalized is not None:
        return normalized
    for pattern in _BMV_PERIOD_PATTERNS:
        match = pattern.search(title)
        if match:
            quarter, year = match.groups()
            return f"{year}-{quarter}T"
    return None


def parse_quarterly_financial_page(
    html: str,
    issuer: IssuerSpec,
    source: AcquisitionSource,
) -> tuple[DiscoveredRecord, ...]:
    """Parse exact quarterly PDFs from one official BMV issuer page.

    One preferred document is returned for each period.  If both the management
    discussion and the basic statements are present, management discussion wins.
    Analyst reports, quarterly certificates, auditor notices, annual reports and
    XBRL links are outside this source contract.
    """

    soup = BeautifulSoup(html, "html.parser")
    by_period: dict[str, tuple[int, DiscoveredRecord]] = {}
    for heading, section, priority in _SECTIONS:
        table = _table_after_heading(soup, heading)
        if table is None:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            title = cells[1].get_text(" ", strip=True)
            period = _quarterly_period(title)
            # Some BMV sections append an empty actions column after the PDF
            # cell, so the source-native link is searched within the bounded
            # row rather than assumed to live in the final cell.
            anchor = _pdf_anchor(row)
            if period is None or anchor is None:
                continue
            year, quarter = period_parts(period)
            assert year is not None and quarter is not None
            url = _validated_bmv_url(
                urljoin(BMV_BASE, anchor["href"]),
                label="BMV PDF",
            )
            filename = Path(urlparse(url).path).name or f"{issuer.slug}-{period}.pdf"
            published = _published_at(cells[0].get_text(" ", strip=True))
            record = SourceRecord(
                source_key=source.key,
                source_record_id=filename,
                issuer_slug=issuer.slug,
                url=url,
                document_type="quarterly_release",
                title=f"{issuer.name} {period} BMV quarterly report",
                published_at=published,
                period_year=year,
                period_quarter=quarter,
                language="es",
                rendition=section,
                metadata={
                    "adapter": BmvIssuerPdfAdapter.kind,
                    "page_url": source.url,
                    "section": section,
                    "period": period,
                    "ticker": issuer.ticker,
                },
            )
            candidate = DiscoveredRecord(
                record=record,
                native=_NativePdf(
                    url=url,
                    filename=filename,
                    section=section,
                    priority=priority,
                ),
            )
            previous = by_period.get(period)
            if previous is None or priority < previous[0]:
                by_period[period] = (priority, candidate)
    return tuple(
        candidate
        for _priority, candidate in sorted(
            by_period.values(),
            key=lambda item: (
                item[1].record.period or "",
                item[0],
                item[1].record.source_record_id,
            ),
        )
    )


def _bmv_get(url: str, *, timeout: int):
    """Fetch one BMV URL without following an unvalidated redirect."""

    from src.download.downloader import _make_session

    session = _make_session()
    current = _validated_bmv_url(url, label="BMV request")
    for _redirect in range(6):
        response = session.get(
            current,
            timeout=timeout,
            verify=True,
            allow_redirects=False,
        )
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location")
            if not location:
                raise RuntimeError("BMV redirect did not include a location")
            current = _validated_bmv_url(
                urljoin(current, location),
                label="BMV redirect",
            )
            continue
        response.raise_for_status()
        final_url = _validated_bmv_url(
            str(response.url or current),
            label="BMV response",
        )
        return response, final_url
    raise RuntimeError("BMV request exceeded the redirect limit")


def _default_page_loader(url: str) -> tuple[str, str]:
    response, final_url = _bmv_get(url, timeout=120)
    return response.text, final_url


def _default_pdf_loader(
    url: str,
) -> tuple[bytes, int | None, Mapping[str, str], str]:
    response, final_url = _bmv_get(url, timeout=180)
    return (
        response.content,
        response.status_code,
        dict(response.headers),
        final_url,
    )


class BmvIssuerPdfAdapter:
    """Discover and fetch official quarterly PDFs from BMV issuer pages."""

    kind = "bmv_issuer_pdf"

    def __init__(
        self,
        *,
        page_loader: Callable[
            [str],
            str | tuple[str, str],
        ]
        | None = None,
        pdf_loader: Callable[
            [str],
            bytes
            | tuple[bytes, int | None, Mapping[str, str]]
            | tuple[bytes, int | None, Mapping[str, str], str],
        ]
        | None = None,
        min_interval_ms: int = 1000,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if min_interval_ms < 0:
            raise ValueError("min_interval_ms cannot be negative")
        self._page_loader = page_loader or _default_page_loader
        self._pdf_loader = pdf_loader or _default_pdf_loader
        self._min_interval_ms = min_interval_ms
        self._clock = clock
        self._sleeper = sleeper
        self._last_fetch_started: float | None = None

    def discover(
        self,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        *,
        wanted_periods: set[str] | None = None,
    ) -> tuple[DiscoveredRecord, ...]:
        if not source.url:
            raise ValueError(f"{issuer.slug}/{source.key}: BMV issuer URL is required")
        _validated_bmv_url(source.url, label="BMV issuer page")
        wanted = (
            {
                period
                for value in wanted_periods
                if (period := normalize_period(value))
            }
            if wanted_periods is not None
            else None
        )
        loaded_page = self._page_loader(source.url)
        if isinstance(loaded_page, tuple):
            html, page_url = loaded_page
        else:
            html, page_url = loaded_page, source.url
        _validated_bmv_url(page_url, label="BMV issuer page response")
        rows = parse_quarterly_financial_page(
            html,
            issuer,
            source,
        )
        return tuple(
            row
            for row in rows
            if wanted is None or row.record.period in wanted
        )

    def fetch(
        self,
        discovered: DiscoveredRecord,
        staging_dir: Path,
        *,
        source: AcquisitionSource,
    ) -> FetchedArtifact:
        native = discovered.native
        if not isinstance(native, _NativePdf):
            raise ValueError("BMV issuer fetch requires an exact discovered PDF")
        interval_ms = (
            source.delay_ms
            if source.delay_ms is not None
            else self._min_interval_ms
        )
        if self._last_fetch_started is not None and interval_ms > 0:
            elapsed = self._clock() - self._last_fetch_started
            remaining = interval_ms / 1000 - elapsed
            if remaining > 0:
                self._sleeper(remaining)
        self._last_fetch_started = self._clock()

        loaded = self._pdf_loader(native.url)
        if isinstance(loaded, tuple):
            if len(loaded) == 4:
                content, status, headers, final_url = loaded
            else:
                content, status, headers = loaded
                final_url = native.url
        else:
            content, status, headers = loaded, None, {}
            final_url = native.url
        _validated_bmv_url(final_url, label="BMV PDF response")
        record = replace(
            discovered.record,
            url=final_url,
            metadata={
                **discovered.record.metadata,
                "discovered_url": native.url,
                "final_url": final_url,
            },
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        return FetchedArtifact(
            source=record,
            content=content,
            role="original",
            filename=native.filename,
            media_type="application/pdf",
            response_status=status,
            response_headers=headers,
        )


__all__ = [
    "BMV_BASE",
    "BMV_HOSTS",
    "BmvIssuerPdfAdapter",
    "parse_quarterly_financial_page",
]
