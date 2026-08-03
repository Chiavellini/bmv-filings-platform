"""Source adapters for the shared quarterly-acquisition engine.

Adapters stop at the :class:`~src.acquisition.models.FetchedArtifact` boundary:
they may use temporary files because the legacy downloaders are file-oriented,
but they never write to a product corpus or to the document estate.  The
acquisition service hands every fetched artifact to the injected estate writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from src.acquisition.models import (
    AcquisitionSource,
    FetchedArtifact,
    IssuerSpec,
    SourceRecord,
)
from src.shared.report_index import infer_period_label, period_sort_key


_PERIOD_RE = re.compile(r"^(?P<year>\d{4})-(?:Q(?P<q1>[1-4])|(?P<q2>[1-4])T)$")


def normalize_bmv_ticker(value: str) -> str:
    """Normalize punctuation for archive matching without changing fetch identity.

    BMV snapshots have used both ``PE&OLES`` and ``PEOLES`` for the same filing
    clave. Matching on the alphanumeric spelling is stable across those
    snapshots; the exact archive ticker remains attached to the native filing
    and is still passed to the downloader.
    """

    return re.sub(r"[^A-Z0-9]+", "", str(value).upper())


def normalize_period(value: str | None) -> str | None:
    """Return the estate's canonical ``YYYY-NT`` spelling."""

    if value is None:
        return None
    match = _PERIOD_RE.fullmatch(value.strip().upper())
    if not match:
        inferred = infer_period_label(value)
        if inferred is None:
            return None
        match = _PERIOD_RE.fullmatch(inferred.upper())
        if not match:
            return None
    quarter = match.group("q1") or match.group("q2")
    return f"{match.group('year')}-{quarter}T"


def period_parts(value: str | None) -> tuple[int | None, int | None]:
    """Split a canonical period without leaking the legacy spelling to models."""

    normalized = normalize_period(value)
    if normalized is None:
        return None, None
    match = _PERIOD_RE.fullmatch(normalized)
    assert match is not None
    return int(match.group("year")), int(match.group("q2"))


@dataclass(frozen=True, slots=True)
class DiscoveredRecord:
    """A normalized discovery record plus adapter-private fetch identity."""

    record: SourceRecord
    native: Any = None


@dataclass(frozen=True, slots=True)
class IRLayerDiagnostic:
    """Adapter-neutral copy of one downloader discovery-layer result."""

    layer: str
    candidates: int = 0
    selected: int = 0
    note: str = ""


@dataclass(frozen=True, slots=True)
class IRFetchIssue:
    """One quarantined record or retryable transport failure."""

    record: SourceRecord
    error: str
    retryable: bool


@dataclass(frozen=True, slots=True)
class IRFetchReport:
    """Structured incremental result while preserving the legacy tuple API."""

    artifacts: tuple[FetchedArtifact, ...]
    candidate_periods: tuple[str, ...] = ()
    layers: tuple[IRLayerDiagnostic, ...] = ()
    issues: tuple[IRFetchIssue, ...] = ()


class BmvXbrlAdapter:
    """Discover the BMV archive once and fetch individual, exact filings."""

    kind = "bmv_xbrl"

    def __init__(
        self,
        *,
        archive_loader: Callable[..., list[Any]] | None = None,
        filing_fetcher: Callable[..., list[Path]] | None = None,
        min_interval_ms: int = 1000,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if min_interval_ms < 0:
            raise ValueError("min_interval_ms cannot be negative")
        if archive_loader is None:
            from src.download.bmv_xbrl import fetch_archive_index

            archive_loader = fetch_archive_index
        if filing_fetcher is None:
            from src.download.bmv_xbrl import download_ticker

            filing_fetcher = download_ticker
        self._archive_loader = archive_loader
        self._filing_fetcher = filing_fetcher
        self._archive: tuple[Any, ...] | None = None
        self._archive_error: Exception | None = None
        self._min_interval_ms = min_interval_ms
        self._clock = clock
        self._sleeper = sleeper
        self._last_fetch_started: float | None = None

    def archive(self) -> tuple[Any, ...]:
        """Return a per-adapter cached archive snapshot.

        One adapter is created per service run, so a whole-universe refresh makes
        exactly one request for the multi-issuer BMV archive page.
        """

        if self._archive_error is not None:
            raise self._archive_error
        if self._archive is None:
            try:
                self._archive = tuple(self._archive_loader())
            except Exception as exc:
                # Cache the failure as well as success: a whole-universe run
                # must never hammer the shared archive once per issuer.
                self._archive_error = exc
                raise
        return self._archive

    def discover(
        self,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        *,
        wanted_periods: set[str] | None = None,
    ) -> tuple[DiscoveredRecord, ...]:
        ticker = (source.xbrl_ticker or issuer.ticker).upper()
        floor = source.floor_year
        wanted = (
            {period for value in wanted_periods if (period := normalize_period(value))}
            if wanted_periods is not None
            else None
        )
        rows_by_identity: dict[str, DiscoveredRecord] = {}
        for filing in self.archive():
            native_ticker = str(filing.ticker).upper()
            if (
                normalize_bmv_ticker(native_ticker) != normalize_bmv_ticker(ticker)
                or filing.kind != "quarterly"
            ):
                continue
            period = normalize_period(str(filing.period))
            if period is None:
                continue
            year, quarter = period_parts(period)
            assert year is not None and quarter is not None
            if floor is not None and year < floor:
                continue
            if wanted is not None and period not in wanted:
                continue
            published = _parse_bmv_datetime(getattr(filing, "filed_date", None))
            record = SourceRecord(
                source_key=source.key,
                source_record_id=(
                    f"{normalize_bmv_ticker(ticker)}:quarterly:{period}"
                ),
                issuer_slug=issuer.slug,
                url=str(filing.zip_url),
                document_type="regulatory_filing",
                title=f"{issuer.name} {period} BMV XBRL filing",
                published_at=published,
                period_year=year,
                period_quarter=quarter,
                rendition="xbrl",
                metadata={
                    "adapter": self.kind,
                    "ticker": ticker,
                    "archive_ticker": native_ticker,
                    "filed_date": str(getattr(filing, "filed_date", "")),
                    "period": period,
                    "filing_kind": "quarterly",
                },
            )
            candidate = DiscoveredRecord(record=record, native=filing)
            previous = rows_by_identity.get(record.source_record_id)
            if previous is None or _published_rank(candidate) > _published_rank(previous):
                rows_by_identity[record.source_record_id] = candidate
        rows = list(rows_by_identity.values())
        rows.sort(
            key=lambda item: (
                period_sort_key(normalize_period(item.record.period) or "0000-1T"),
                item.record.source_record_id,
            )
        )
        return tuple(rows)

    def fetch(
        self,
        discovered: DiscoveredRecord,
        staging_dir: Path,
        *,
        source: AcquisitionSource,
    ) -> FetchedArtifact:
        """Fetch one exact archive row, never a ticker-wide implicit selection."""

        filing = discovered.native
        if filing is None:
            raise ValueError("BMV fetch requires the exact native filing")
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
        paths = self._filing_fetcher(
            str(filing.ticker),
            out_dir=staging_dir,
            filings=[filing],
            include_annual=False,
            delay_ms=source.delay_ms or 0,
            write_artifacts=False,
        )
        if len(paths) != 1:
            raise RuntimeError(
                "BMV exact filing fetch returned "
                f"{len(paths)} artifacts for {discovered.record.source_record_id}"
            )
        path = Path(paths[0])
        return FetchedArtifact(
            source=discovered.record,
            content=path.read_bytes(),
            role="raw_xbrl",
            filename=path.name,
            media_type="application/gzip" if path.name.endswith(".gz") else "application/json",
        )


class InvestorRelationsAdapter:
    """Incremental PDF acquisition through the existing multi-layer IR engine."""

    kind = "investor_relations"

    def __init__(
        self,
        *,
        downloader: Callable[..., list[Path]] | None = None,
        template_downloader: Callable[..., list[Path]] | None = None,
    ):
        if downloader is None or template_downloader is None:
            from src.download.downloader import (
                download_from_ir,
                download_from_url_templates,
            )

            downloader = downloader or download_from_ir
            template_downloader = template_downloader or download_from_url_templates
        self._downloader = downloader
        self._template_downloader = template_downloader

    def fetch_incremental(
        self,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        staging_dir: Path,
        *,
        known_periods: set[str],
        recheck_periods: set[str],
        desired_periods: set[str],
    ) -> tuple[FetchedArtifact, ...]:
        """Compatibility wrapper returning only successful artifacts."""

        return self.fetch_incremental_report(
            issuer,
            source,
            staging_dir,
            known_periods=known_periods,
            recheck_periods=recheck_periods,
            desired_periods=desired_periods,
        ).artifacts

    def fetch_incremental_report(
        self,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        staging_dir: Path,
        *,
        known_periods: set[str],
        recheck_periods: set[str],
        desired_periods: set[str],
    ) -> IRFetchReport:
        """Return clean artifacts plus discovery and quarantine diagnostics."""

        if not source.url:
            raise ValueError(f"{issuer.slug}/{source.key}: IR source has no URL")

        known = {period for value in known_periods if (period := normalize_period(value))}
        recheck = {period for value in recheck_periods if (period := normalize_period(value))}
        excluded = known - recheck
        details: list[Any] = []
        candidates: list[Any] = []
        download_failures: list[Any] = []
        layer_details: list[Any] = []
        kwargs: dict[str, Any] = {
            "max_reports": source.max_reports or 120,
            "floor_year": source.floor_year,
            "delay_ms": source.delay_ms if source.delay_ms is not None else 500,
            "exclude_periods": excluded,
            "detail_sink": details,
            "desired_periods": set(desired_periods),
            "candidate_sink": candidates,
            "failure_sink": download_failures,
            "layer_sink": layer_details,
        }
        if source.pdf_link_pattern:
            kwargs["file_pattern"] = source.pdf_link_pattern
        if source.strict_pdf_link_pattern:
            kwargs["strict_file_pattern"] = True
        if source.year_api_urls:
            kwargs["year_api_urls"] = list(source.year_api_urls)
        if source.use_playwright is not None:
            kwargs["use_playwright"] = source.use_playwright
        if source.impersonate:
            kwargs["impersonate"] = source.impersonate
        for key in ("browser_first", "verify_ssl"):
            if key in source.options:
                kwargs[key] = source.options[key]

        primary_issues: list[IRFetchIssue] = []
        try:
            returned = self._downloader(source.url, staging_dir, **kwargs)
        except Exception as exc:
            # The index page is only one discovery layer. Configured direct URL
            # templates are deterministic per-period fallbacks and must still be
            # attempted when that page is bot-blocked or otherwise unavailable.
            # Preserve the page failure as durable retryable evidence instead of
            # allowing it to discard successful template PDFs.
            returned = ()
            primary_issues.append(
                _ir_primary_page_issue(issuer, source, exc)
            )
            layer_details.append(
                IRLayerDiagnostic(
                    layer="primary_page",
                    candidates=0,
                    selected=0,
                    note=f"{type(exc).__name__}: {exc}",
                )
            )
        artifacts: list[FetchedArtifact] = []
        detailed_paths: set[Path] = set()
        for detail in details:
            path = Path(detail.path)
            detailed_paths.add(path.resolve())
            period = normalize_period(detail.period) or normalize_period(
                infer_period_label(path.stem)
            )
            if not _safe_quarterly_candidate(
                url=str(detail.url),
                filename=path.name,
                period=period,
            ):
                continue
            artifacts.append(
                _pdf_artifact(
                    issuer,
                    source,
                    path,
                    url=str(detail.url),
                    period=period,
                    adapter=self.kind,
                )
            )

        # Defensive compatibility for a custom downloader that has not adopted
        # ``detail_sink`` yet. Provenance remains explicit about the limitation.
        for returned_path in returned:
            path = Path(returned_path)
            if path.resolve() in detailed_paths:
                continue
            period = normalize_period(infer_period_label(path.stem))
            if not _safe_quarterly_candidate(
                url=None,
                filename=path.name,
                period=period,
            ):
                continue
            artifacts.append(
                _pdf_artifact(
                    issuer,
                    source,
                    path,
                    url=None,
                    period=period,
                    adapter=self.kind,
                    metadata={"provenance_warning": "downloader returned no URL detail"},
                )
            )

        # Deterministic URL templates are an incremental fallback for periods
        # that the live page did not return. They receive only desired, still
        # absent periods (including the bounded restatement recheck window).
        template_issues: list[IRFetchIssue] = []
        if source.direct_url_templates:
            fetched_periods = {
                period
                for artifact in artifacts
                if (period := normalize_period(artifact.source.period)) is not None
            }
            template_targets = {
                period
                for value in desired_periods
                if (period := normalize_period(value))
                and period not in excluded
                and period not in fetched_periods
            }
            template_dir = staging_dir / "templates"
            template_dir.mkdir(parents=True, exist_ok=True)
            template_details: list[Any] = []
            try:
                paths = self._template_downloader(
                    list(source.direct_url_templates),
                    template_dir,
                    template_targets,
                    delay_ms=(
                        source.delay_ms if source.delay_ms is not None else 300
                    ),
                    verify_ssl=bool(source.options.get("verify_ssl", True)),
                    impersonate=source.impersonate,
                    detail_sink=template_details,
                )
            except Exception as exc:
                # This fallback is an independent discovery/fetch layer. Its
                # failure must not erase valid artifacts already accumulated
                # from the primary page. Preserve any template successes that
                # reported detail before the exception, and retain the failure
                # as retryable run evidence.
                partial_paths: list[Path] = []
                for detail in template_details:
                    detail_path = getattr(detail, "path", None)
                    if detail_path is None:
                        continue
                    candidate = Path(detail_path)
                    if candidate.is_file():
                        partial_paths.append(candidate)
                paths = tuple(partial_paths)
                template_issues.append(
                    _ir_template_layer_issue(
                        issuer,
                        source,
                        template_targets,
                        exc,
                    )
                )
                layer_details.append(
                    IRLayerDiagnostic(
                        layer="direct_url_templates",
                        candidates=len(template_targets),
                        selected=len(paths),
                        note=f"{type(exc).__name__}: {exc}",
                    )
                )
            template_url_by_path: dict[Path, str] = {}
            for detail in template_details:
                detail_path = getattr(detail, "path", None)
                detail_url = str(getattr(detail, "url", "") or "").strip()
                if detail_path is not None and detail_url:
                    template_url_by_path[Path(detail_path).resolve()] = detail_url
            for path_value in paths:
                path = Path(path_value)
                period = normalize_period(infer_period_label(path.stem))
                selected_url = template_url_by_path.get(path.resolve())
                if not _safe_quarterly_candidate(
                    url=selected_url,
                    filename=path.name,
                    period=period,
                ):
                    continue
                artifacts.append(
                    _pdf_artifact(
                        issuer,
                        source,
                        path,
                        url=selected_url,
                        period=period,
                        adapter="ir_url_template",
                        metadata={
                            "url_templates": list(source.direct_url_templates),
                            **({} if selected_url else {
                                "provenance_warning": (
                                    "legacy template downloader does not expose the selected URL"
                                ),
                            }),
                        },
                    )
                )

        deduped = _dedupe_artifacts(artifacts)
        selected, conflicts = _partition_artifacts_by_family(deduped)
        issues = list(primary_issues)
        issues.extend(
            _download_failure_issue(issuer, source, failure)
            for failure in download_failures
        )
        issues.extend(template_issues)
        issues.extend(conflicts)
        candidate_periods = {
            period
            for candidate in candidates
            if (period := normalize_period(getattr(candidate, "period", None)))
        }
        candidate_periods.update(
            period
            for artifact in deduped
            if (period := normalize_period(artifact.source.period)) is not None
        )
        layers = tuple(
            IRLayerDiagnostic(
                layer=str(getattr(item, "layer", "unknown")),
                candidates=int(getattr(item, "candidates", 0)),
                selected=int(getattr(item, "selected", 0)),
                note=str(getattr(item, "note", "")),
            )
            for item in layer_details
        )
        return IRFetchReport(
            artifacts=selected,
            candidate_periods=tuple(
                sorted(candidate_periods, key=period_sort_key)
            ),
            layers=layers,
            issues=tuple(issues),
        )


class WaybackBackfillAdapter:
    """Explicit historical recovery adapter; never part of recurring sync."""

    kind = "wayback"

    def __init__(self, *, downloader: Callable[..., list[Path]] | None = None):
        if downloader is None:
            from src.download.wayback import download_missing

            downloader = download_missing
        self._downloader = downloader

    def fetch_missing(
        self,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        staging_dir: Path,
        *,
        missing_periods: set[str],
        from_year: int | None = None,
    ) -> tuple[FetchedArtifact, ...]:
        if not source.url:
            raise ValueError(f"{issuer.slug}/{source.key}: IR source has no URL")
        missing = {
            period for value in missing_periods if (period := normalize_period(value))
        }
        paths = self._downloader(
            source.url,
            staging_dir,
            missing,
            from_year=from_year or source.floor_year or 2016,
            delay_ms=source.delay_ms if source.delay_ms is not None else 500,
        )
        artifacts: list[FetchedArtifact] = []
        for path_value in paths:
            path = Path(path_value)
            period = normalize_period(infer_period_label(path.stem))
            if not _safe_quarterly_candidate(
                url=None,
                filename=path.name,
                period=period,
            ):
                continue
            artifacts.append(
                _pdf_artifact(
                    issuer,
                    source,
                    path,
                    url=None,
                    period=period,
                    adapter=self.kind,
                    source_key=f"{source.key}:wayback",
                    metadata={
                        "ir_url": source.url,
                        "provenance_warning": (
                            "legacy Wayback downloader does not expose the selected snapshot URL"
                        ),
                    },
                )
            )
        return tuple(artifacts)


def _pdf_artifact(
    issuer: IssuerSpec,
    source: AcquisitionSource,
    path: Path,
    *,
    url: str | None,
    period: str | None,
    adapter: str,
    source_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> FetchedArtifact:
    year, quarter = period_parts(period)
    language, rendition = _infer_pdf_rendition(
        issuer,
        source,
        url=url,
        filename=path.name,
    )
    source_id = url or f"{adapter}:{issuer.slug}:{period or path.name}"
    record = SourceRecord(
        source_key=source_key or source.key,
        source_record_id=source_id,
        issuer_slug=issuer.slug,
        url=url,
        document_type="quarterly_release",
        title=f"{issuer.name} {period or 'quarterly'} report",
        period_year=year,
        period_quarter=quarter,
        language=language,
        rendition=rendition,
        metadata={
            "adapter": adapter,
            "period": period,
            "language": language,
            "rendition": rendition,
            **(metadata or {}),
        },
    )
    return FetchedArtifact(
        source=record,
        content=path.read_bytes(),
        role="original",
        filename=path.name,
        media_type="application/pdf",
    )


def _safe_quarterly_candidate(
    *,
    url: str | None,
    filename: str,
    period: str | None,
) -> bool:
    """Require a classified period and reject known non-report asset classes."""

    if period is None:
        return False
    from src.download.downloader import _PdfAnchor, _is_excluded_report_asset

    anchor = _PdfAnchor(url=url or "", filename=filename, text="")
    return not _is_excluded_report_asset(anchor)


def _infer_pdf_rendition(
    issuer: IssuerSpec,
    source: AcquisitionSource,
    *,
    url: str | None,
    filename: str,
) -> tuple[str | None, str]:
    haystack = f"{url or ''} {filename}".lower()
    configured = source.options.get("language")
    language = str(configured).lower() if configured else None
    if language is None:
        if re.search(r"(?:^|[/_.-])(?:en|eng|english)(?:[/_.-]|$)", haystack):
            language = "en"
        elif re.search(
            r"(?:^|[/_.-])(?:es|esp|spanish|espanol|español)(?:[/_.-]|$)",
            haystack,
        ):
            language = "es"
        elif issuer.language in {"en", "es"}:
            language = issuer.language

    if re.search(
        r"(?:^|[/_. -])(?:press[-_ ]?release|release|results?|resultados?)"
        r"(?:[/_. -]|$)",
        haystack,
    ):
        rendition = "release"
    elif re.search(
        r"(?:^|[/_. -])(?:full|complete|completo|informe)(?:[/_. -]|$)",
        haystack,
    ):
        rendition = "report"
    else:
        rendition = "default"
    return language, rendition


def _dedupe_artifacts(
    artifacts: Iterable[FetchedArtifact],
) -> tuple[FetchedArtifact, ...]:
    by_identity: dict[tuple[str, str], FetchedArtifact] = {}
    for artifact in artifacts:
        key = (artifact.source.source_key, artifact.source.source_record_id)
        by_identity[key] = artifact
    return tuple(
        sorted(
            by_identity.values(),
            key=lambda item: (
                period_sort_key(normalize_period(item.source.period) or "0000-1T"),
                item.source.source_record_id,
            ),
        )
    )


def _download_failure_issue(
    issuer: IssuerSpec,
    source: AcquisitionSource,
    failure: Any,
) -> IRFetchIssue:
    """Convert a downloader URL failure into a durable source-record identity."""

    url = str(getattr(failure, "url", "") or source.url or "")
    period = normalize_period(getattr(failure, "period", None))
    year, quarter = period_parts(period)
    filename = Path(urlparse(url).path).name
    language, rendition = _infer_pdf_rendition(
        issuer,
        source,
        url=url,
        filename=filename,
    )
    record = SourceRecord(
        source_key=source.key,
        source_record_id=url or f"{issuer.slug}:download:{period or 'unknown'}",
        issuer_slug=issuer.slug,
        url=url or None,
        document_type="quarterly_release",
        title=f"{issuer.name} {period or 'quarterly'} download attempt",
        period_year=year,
        period_quarter=quarter,
        language=language,
        rendition=rendition,
        metadata={
            "adapter": InvestorRelationsAdapter.kind,
            "period": period,
            "transport_failure": True,
        },
    )
    return IRFetchIssue(
        record=record,
        error=str(getattr(failure, "error", "PDF download failed")),
        retryable=True,
    )


def _ir_primary_page_issue(
    issuer: IssuerSpec,
    source: AcquisitionSource,
    exc: Exception,
) -> IRFetchIssue:
    """Represent an index-page failure without suppressing other IR layers."""

    url = str(source.url or "")
    record = SourceRecord(
        source_key=source.key,
        source_record_id=f"{issuer.slug}:ir-page:{url}",
        issuer_slug=issuer.slug,
        url=url or None,
        document_type="quarterly_release",
        title=f"{issuer.name} investor-relations page discovery",
        language=issuer.language if issuer.language in {"en", "es"} else None,
        rendition="default",
        metadata={
            "adapter": InvestorRelationsAdapter.kind,
            "discovery_layer": "primary_page",
            "transport_failure": True,
            "direct_url_templates_configured": bool(source.direct_url_templates),
        },
    )
    return IRFetchIssue(
        record=record,
        error=f"{type(exc).__name__}: {exc}",
        retryable=True,
    )


def _ir_template_layer_issue(
    issuer: IssuerSpec,
    source: AcquisitionSource,
    target_periods: Iterable[str],
    exc: Exception,
) -> IRFetchIssue:
    """Represent a deterministic-template layer failure without losing PDFs."""

    periods = tuple(sorted(
        {
            period
            for value in target_periods
            if (period := normalize_period(value)) is not None
        },
        key=period_sort_key,
    ))
    record = SourceRecord(
        source_key=source.key,
        source_record_id=f"{issuer.slug}:ir-url-templates",
        issuer_slug=issuer.slug,
        document_type="quarterly_release",
        title=f"{issuer.name} deterministic report URL discovery",
        language=issuer.language if issuer.language in {"en", "es"} else None,
        rendition="default",
        metadata={
            "adapter": "ir_url_template",
            "discovery_layer": "direct_url_templates",
            "transport_failure": True,
            "target_periods": list(periods),
            "url_templates": list(source.direct_url_templates),
        },
    )
    return IRFetchIssue(
        record=record,
        error=f"{type(exc).__name__}: {exc}",
        retryable=True,
    )


def _partition_artifacts_by_family(
    artifacts: Iterable[FetchedArtifact],
) -> tuple[tuple[FetchedArtifact, ...], tuple[IRFetchIssue, ...]]:
    """Quarantine conflicting families while retaining every clean family."""

    grouped: dict[str, list[FetchedArtifact]] = {}
    issues: list[IRFetchIssue] = []
    for artifact in artifacts:
        family = artifact.source.document_family_id
        if family is None:
            issues.append(
                IRFetchIssue(
                    record=artifact.source,
                    error="quarterly PDF artifact is missing a document family",
                    retryable=False,
                )
            )
            continue
        grouped.setdefault(family, []).append(artifact)

    selected: list[FetchedArtifact] = []
    for family, candidates in grouped.items():
        by_hash: dict[str, list[FetchedArtifact]] = {}
        for candidate in candidates:
            by_hash.setdefault(candidate.sha256, []).append(candidate)
        if len(by_hash) > 1:
            identities = sorted(
                candidate.source.source_record_id
                for candidate in candidates
            )
            error = (
                "ambiguous quarterly PDFs for "
                f"{family}: {', '.join(identities)}"
            )
            issues.extend(
                IRFetchIssue(
                    record=candidate.source,
                    error=error,
                    retryable=False,
                )
                for candidate in candidates
            )
            continue
        selected.append(
            min(
                candidates,
                key=lambda item: item.source.source_record_id,
            )
        )
    selected_artifacts = tuple(
        sorted(
            selected,
            key=lambda item: (
                period_sort_key(
                    normalize_period(item.source.period) or "0000-1T"
                ),
                item.source.source_record_id,
            ),
        )
    )
    return selected_artifacts, tuple(issues)


def _one_artifact_per_family(
    artifacts: Iterable[FetchedArtifact],
) -> tuple[FetchedArtifact, ...]:
    """Compatibility helper retaining the historical fail-closed behavior."""

    selected, issues = _partition_artifacts_by_family(artifacts)
    if issues:
        raise ValueError(issues[0].error)
    return selected


def _parse_bmv_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _published_rank(discovered: DiscoveredRecord) -> tuple[str, str]:
    published = discovered.record.published_at
    return (
        published.isoformat() if published is not None else "",
        discovered.record.url or "",
    )


__all__ = [
    "BmvXbrlAdapter",
    "DiscoveredRecord",
    "IRFetchIssue",
    "IRFetchReport",
    "IRLayerDiagnostic",
    "InvestorRelationsAdapter",
    "WaybackBackfillAdapter",
    "normalize_bmv_ticker",
    "normalize_period",
    "period_parts",
]
