"""Deterministic discovery of repeatable issuer quarterly-PDF sources.

This module is deliberately separate from the production acquisition service.
It proposes registry configuration; it never writes the document estate and it
does not use an LLM.  A proposal is eligible for registry review only when the
downloaded bytes are a PDF and the first pages independently confirm both the
issuer and the period advertised by the link.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from difflib import unified_diff
from io import BytesIO
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Any, Iterable, Mapping, Protocol, Sequence
import unicodedata
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse
from urllib.parse import urlencode
from xml.etree import ElementTree

from bs4 import BeautifulSoup
import pdfplumber
import requests

from src.acquisition.models import AcquisitionSource, FetchedArtifact, IssuerSpec
from src.acquisition.readiness import PrimaryPdfReadinessRow
from src.acquisition.registry import IssuerRegistry, load_issuer_registry
from src.shared.report_index import infer_period_label, period_sort_key


SCHEMA_VERSION = 1
DISCOVERY_ALGORITHM = "static-ir-source-v1"
DEFAULT_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
BMV_DIRECTORY_URL = (
    "https://www.bmv.com.mx/es/Grupo_BMV/Informacion_de_emisora/"
    "_rid/541/_mto/3/_mod/doSearch"
)
BMV_DIRECTORY_REFERER = (
    "https://www.bmv.com.mx/es/emisoras/informacion-de-emisoras"
)

_PAGE_TERMS = re.compile(
    r"(?i)(?:invest(?:or|ors|ment|mentistas)|relaci[oó]n.?con.?inversionistas|"
    r"informaci[oó]n.?financiera|financial|quarterly|trimestr|resultados|"
    r"earnings|reportes?|informes?)"
)
_REPORT_TERMS = re.compile(
    r"(?i)(?:results?|resultados|earnings?|quarter(?:ly)?|trimestr(?:e|al)|"
    r"report(?:e|es)?|informe|comunicado|press.?release|financial.?statements?|"
    r"estados.?financieros)"
)
_PRIMARY_RELEASE_TERMS = re.compile(
    r"(?i)(?:earnings?.?release|results?.?release|press.?release|"
    r"comunicado.?de.?resultados|reporte.?de.?resultados|"
    r"quarterly.?report|reporte.?trimestral)"
)
_HARD_NEGATIVE_TERMS = re.compile(
    r"(?i)(?:annual|anual|integrated.?report|sustainab|sostenibilidad|esg|"
    r"governance|gobierno.?corporativo|presentaci[oó]n|presentation|webcast|"
    r"transcript|conference.?call|earnings.?call|prospectus|prospecto|"
    r"certificate|constancia|auditor|infographic|infograf|carrusel|"
    r"results?.?to.?the.?mse|informaci[oó]n.?a.?la.?bmv|"
    r"\.zip(?:[?#]|$)|10-k|20-f)"
)
_PDF_HINT = re.compile(r"(?i)(?:\.pdf(?:[?#]|$)|/pdf(?:[/?#]|$)|download)")
_GENERIC_IDENTITY_WORDS = frozenset(
    {
        "and",
        "corporacion",
        "corporation",
        "corp",
        "de",
        "del",
        "el",
        "grupo",
        "group",
        "holding",
        "la",
        "las",
        "los",
        "mexico",
        "mexicana",
        "mexicano",
        "sa",
        "sab",
        "sociedad",
        "the",
    }
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


class DiscoveryHttpClient(Protocol):
    """Small injectable network boundary used by the compiler and its tests."""

    def get(self, url: str, *, max_bytes: int) -> "HttpPayload": ...


@dataclass(frozen=True, slots=True)
class HttpPayload:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    content: bytes
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class DiscoverySeed:
    url: str
    origin: str


@dataclass(frozen=True, slots=True)
class PageAttempt:
    url: str
    layer: str
    status: str
    final_url: str | None = None
    content_type: str | None = None
    links_seen: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LinkCandidate:
    url: str
    title: str
    source_page: str
    period: str | None
    score: int
    reasons: tuple[str, ...] = ()
    rejected_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PdfValidation:
    url: str
    final_url: str | None
    advertised_period: str | None
    valid_pdf: bool
    issuer_verified: bool
    period_verified: bool
    size_bytes: int
    sha256: str | None
    content_type: str | None
    identity_terms: tuple[str, ...] = ()
    local_path: str | None = None
    error: str | None = None

    @property
    def fully_verified(self) -> bool:
        return self.valid_pdf and self.issuer_verified and self.period_verified


@dataclass(frozen=True, slots=True)
class SourceProposal:
    issuer_slug: str
    ticker: str
    issuer_name: str
    readiness_gaps: tuple[str, ...]
    status: str
    confidence: str
    cache_hit: bool
    seeds: tuple[DiscoverySeed, ...] = ()
    pages: tuple[PageAttempt, ...] = ()
    candidates: tuple[LinkCandidate, ...] = ()
    validations: tuple[PdfValidation, ...] = ()
    inferred_templates: tuple[str, ...] = ()
    proposed_ir: Mapping[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def patch_eligible(self) -> bool:
        return self.status == "ready_for_review" and self.confidence == "high"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["patch_eligible"] = self.patch_eligible
        return result

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], *, cache_hit: bool) -> "SourceProposal":
        return cls(
            issuer_slug=str(raw["issuer_slug"]),
            ticker=str(raw["ticker"]),
            issuer_name=str(raw["issuer_name"]),
            readiness_gaps=tuple(raw.get("readiness_gaps", ())),
            status=str(raw["status"]),
            confidence=str(raw["confidence"]),
            cache_hit=cache_hit,
            seeds=tuple(DiscoverySeed(**item) for item in raw.get("seeds", ())),
            pages=tuple(PageAttempt(**item) for item in raw.get("pages", ())),
            candidates=tuple(
                LinkCandidate(
                    **{
                        **item,
                        "reasons": tuple(item.get("reasons", ())),
                    }
                )
                for item in raw.get("candidates", ())
            ),
            validations=tuple(
                PdfValidation(
                    **{
                        **item,
                        "identity_terms": tuple(item.get("identity_terms", ())),
                    }
                )
                for item in raw.get("validations", ())
            ),
            inferred_templates=tuple(raw.get("inferred_templates", ())),
            proposed_ir=dict(raw.get("proposed_ir", {})),
            notes=tuple(raw.get("notes", ())),
        )


@dataclass(frozen=True, slots=True)
class BmvWebsiteSeedEvidence:
    issuer_slug: str
    requested_ticker: str
    status: str
    directory_url: str = BMV_DIRECTORY_URL
    directory_ticker: str | None = None
    issuer_id: int | None = None
    legal_name: str | None = None
    profile_url: str | None = None
    website_url: str | None = None
    cache_hit: bool = False
    error: str | None = None

    @property
    def seed(self) -> DiscoverySeed | None:
        if self.status != "ready" or not self.website_url or not self.profile_url:
            return None
        return DiscoverySeed(
            url=self.website_url,
            origin=f"bmv_profile:{self.profile_url}",
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ProductionCanaryResult:
    """Exact-period, non-estate result from the configured production adapter."""

    issuer_slug: str
    source_key: str
    source_kind: str
    expected_period: str
    status: str
    attempts: int
    selected_period: str | None = None
    final_url: str | None = None
    size_bytes: int = 0
    sha256: str | None = None
    valid_pdf: bool = False
    issuer_verified: bool = False
    period_verified: bool = False
    identity_terms: tuple[str, ...] = ()
    local_path: str | None = None
    candidate_periods: tuple[str, ...] = ()
    layers: tuple[Mapping[str, Any], ...] = ()
    issues: tuple[Mapping[str, Any], ...] = ()
    error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            self.status == "passed"
            and self.selected_period == self.expected_period
            and self.valid_pdf
            and self.issuer_verified
            and self.period_verified
            and self.final_url is not None
            and self.local_path is not None
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["passed"] = self.passed
        return result


@dataclass(frozen=True, slots=True)
class DiscoveryOptions:
    max_pages: int = 12
    max_candidates: int = 24
    sample_pdfs: int = 3
    max_page_bytes: int = 4 * 1024 * 1024
    max_pdf_bytes: int = 30 * 1024 * 1024
    min_pdf_bytes: int = 8_000

    def __post_init__(self) -> None:
        for name in ("max_pages", "max_candidates", "sample_pdfs"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_page_bytes <= 0 or self.max_pdf_bytes <= 0:
            raise ValueError("byte limits must be positive")
        if self.min_pdf_bytes < 5:
            raise ValueError("min_pdf_bytes must allow a PDF signature")


class RequestsDiscoveryClient:
    """Bounded HTTP client with a stable user agent and no browser dependency."""

    def __init__(self, *, timeout_seconds: float = 20.0, delay_ms: int = 200):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if delay_ms < 0:
            raise ValueError("delay_ms cannot be negative")
        self.timeout_seconds = timeout_seconds
        self.delay_ms = delay_ms
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "BMV-Filings-Source-Onboarding/1.0 "
                    "(+deterministic quarterly-source verification)"
                ),
                "Accept": "text/html,application/xhtml+xml,application/pdf,application/xml;q=0.9,*/*;q=0.5",
            }
        )
        self._last_request_at: float | None = None

    def get(self, url: str, *, max_bytes: int) -> HttpPayload:
        if self._last_request_at is not None and self.delay_ms:
            remaining = self.delay_ms / 1000 - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()
        with self.session.get(
            url,
            timeout=self.timeout_seconds,
            allow_redirects=True,
            stream=True,
            headers=None,
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                remaining = max_bytes - size
                if remaining <= 0:
                    truncated = True
                    break
                chunks.append(chunk[:remaining])
                size += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    truncated = True
                    break
            return HttpPayload(
                requested_url=url,
                final_url=response.url,
                status_code=response.status_code,
                content_type=response.headers.get("Content-Type", ""),
                content=b"".join(chunks),
                truncated=truncated,
            )

    def get_with_headers(
        self,
        url: str,
        *,
        max_bytes: int,
        headers: Mapping[str, str],
    ) -> HttpPayload:
        """Fetch through the same bounded session with request-specific headers."""

        if self._last_request_at is not None and self.delay_ms:
            remaining = self.delay_ms / 1000 - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()
        with self.session.get(
            url,
            timeout=self.timeout_seconds,
            allow_redirects=True,
            stream=True,
            headers=dict(headers),
        ) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            size = 0
            truncated = False
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                remaining = max_bytes - size
                if remaining <= 0:
                    truncated = True
                    break
                chunks.append(chunk[:remaining])
                size += min(len(chunk), remaining)
                if len(chunk) > remaining:
                    truncated = True
                    break
            return HttpPayload(
                requested_url=url,
                final_url=response.url,
                status_code=response.status_code,
                content_type=response.headers.get("Content-Type", ""),
                content=b"".join(chunks),
                truncated=truncated,
            )


class BmvDirectorySeedProvider:
    """Resolve canonical tickers to official company websites via BMV.

    The full BMV capital-issuer feed is fetched at most once per provider and
    cached as one compact mapping. Each selected issuer then costs at most one
    profile request. A profile is accepted only when its displayed ``Clave``
    matches the requested ticker and its ``Web:`` value is an external HTTP(S)
    URL.
    """

    def __init__(
        self,
        *,
        client: RequestsDiscoveryClient,
        cache_dir: str | Path | None = None,
        cache_max_age_seconds: float = DEFAULT_CACHE_MAX_AGE_SECONDS,
        clock: callable = time.time,
    ):
        self.client = client
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.cache_max_age_seconds = float(cache_max_age_seconds)
        self.clock = clock
        self._directory: dict[str, tuple[dict[str, Any], ...]] | None = None
        self._directory_cache_hit = False

    def resolve(self, issuer: IssuerSpec, *, refresh: bool = False) -> BmvWebsiteSeedEvidence:
        try:
            directory = self._load_directory(refresh=refresh)
        except Exception as exc:
            return BmvWebsiteSeedEvidence(
                issuer_slug=issuer.slug,
                requested_ticker=issuer.ticker,
                status="directory_failed",
                error=f"{type(exc).__name__}: {exc}",
            )
        ticker_keys = _issuer_bmv_ticker_keys(issuer)
        matched: dict[tuple[str, int], dict[str, Any]] = {}
        for ticker in ticker_keys:
            for row in directory.get(ticker, ()):
                matched[(str(row["ticker"]), int(row["issuer_id"]))] = row
        rows = tuple(matched[key] for key in sorted(matched))
        if not rows:
            return BmvWebsiteSeedEvidence(
                issuer_slug=issuer.slug,
                requested_ticker=issuer.ticker,
                status="ticker_missing",
                cache_hit=self._directory_cache_hit,
            )
        if len(rows) != 1:
            return BmvWebsiteSeedEvidence(
                issuer_slug=issuer.slug,
                requested_ticker=issuer.ticker,
                status="ticker_ambiguous",
                cache_hit=self._directory_cache_hit,
                error="multiple BMV capital profiles matched: "
                + ", ".join(f"{row['ticker']}-{row['issuer_id']}" for row in rows),
            )
        row = rows[0]
        directory_ticker = str(row["ticker"])
        issuer_id = int(row["issuer_id"])
        profile_url = (
            "https://www.bmv.com.mx/es/emisoras/perfil/"
            f"{quote(directory_ticker, safe='')}-{issuer_id}"
        )
        cached = None if refresh else self._load_profile_cache(directory_ticker, issuer_id)
        if cached is not None:
            return BmvWebsiteSeedEvidence(
                issuer_slug=issuer.slug,
                requested_ticker=issuer.ticker,
                directory_ticker=directory_ticker,
                issuer_id=issuer_id,
                legal_name=str(row.get("legal_name") or "") or None,
                profile_url=profile_url,
                cache_hit=True,
                **cached,
            )
        try:
            payload = self.client.get(
                profile_url,
                max_bytes=2 * 1024 * 1024,
            )
            if payload.truncated:
                raise ValueError("BMV profile exceeded the byte limit")
            displayed_ticker, website_url = parse_bmv_profile_website(
                payload.content,
                expected_tickers=ticker_keys,
            )
            status = "ready" if website_url else "website_missing"
            result = {"status": status, "website_url": website_url, "error": None}
        except Exception as exc:
            displayed_ticker = None
            result = {
                "status": "profile_failed",
                "website_url": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        self._store_profile_cache(directory_ticker, issuer_id, result)
        return BmvWebsiteSeedEvidence(
            issuer_slug=issuer.slug,
            requested_ticker=issuer.ticker,
            directory_ticker=displayed_ticker or directory_ticker,
            issuer_id=issuer_id,
            legal_name=str(row.get("legal_name") or "") or None,
            profile_url=profile_url,
            website_url=result["website_url"],
            status=result["status"],
            cache_hit=False,
            error=result["error"],
        )

    def _load_directory(self, *, refresh: bool) -> dict[str, tuple[dict[str, Any], ...]]:
        if self._directory is not None and not refresh:
            return self._directory
        cache_path = self.cache_dir / "bmv-capital-directory-v1.json" if self.cache_dir else None
        if not refresh and cache_path is not None:
            cached = _load_fresh_json_cache(
                cache_path,
                max_age_seconds=self.cache_max_age_seconds,
                now=self.clock(),
            )
            if cached and cached.get("schema_version") == 1:
                self._directory = _directory_rows_by_ticker(cached.get("rows", ()))
                self._directory_cache_hit = True
                return self._directory

        query = urlencode(
            {
                "idTipoMercado": "CGEN_CAPIT",
                "idTipoInstrumento": "",
                "idTipoEmpresa": "",
                "idSector": "",
                "idSubsector": "",
                "idRamo": "",
                "idSubramo": "",
            }
        )
        payload = self.client.get_with_headers(
            f"{BMV_DIRECTORY_URL}?{query}",
            max_bytes=4 * 1024 * 1024,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": BMV_DIRECTORY_REFERER,
            },
        )
        if payload.truncated:
            raise ValueError("BMV directory response exceeded the byte limit")
        rows = parse_bmv_directory(payload.content)
        self._directory = _directory_rows_by_ticker(rows)
        self._directory_cache_hit = False
        if cache_path is not None:
            _atomic_json_write(
                cache_path,
                {"schema_version": 1, "rows": list(rows)},
                mtime=self.clock(),
            )
        return self._directory

    def _profile_cache_path(self, ticker: str, issuer_id: int) -> Path | None:
        if self.cache_dir is None:
            return None
        safe_ticker = re.sub(r"[^A-Z0-9]+", "_", ticker.upper()).strip("_")
        return self.cache_dir / "bmv_profiles" / f"{safe_ticker}-{issuer_id}.json"

    def _load_profile_cache(self, ticker: str, issuer_id: int) -> dict[str, Any] | None:
        path = self._profile_cache_path(ticker, issuer_id)
        if path is None:
            return None
        raw = _load_fresh_json_cache(
            path,
            max_age_seconds=self.cache_max_age_seconds,
            now=self.clock(),
        )
        if not raw or raw.get("schema_version") != 1:
            return None
        return {
            "status": str(raw["status"]),
            "website_url": raw.get("website_url"),
            "error": raw.get("error"),
        }

    def _store_profile_cache(self, ticker: str, issuer_id: int, result: Mapping[str, Any]) -> None:
        path = self._profile_cache_path(ticker, issuer_id)
        if path is None:
            return
        _atomic_json_write(
            path,
            {"schema_version": 1, **dict(result)},
            mtime=self.clock(),
        )


def verify_configured_production_source(
    issuer: IssuerSpec,
    *,
    expected_period: str,
    staging_dir: str | Path,
    source_key: str | None = None,
    attempts: int = 2,
    min_pdf_bytes: int = 8_000,
    ir_adapter: Any | None = None,
    bmv_adapter: Any | None = None,
    sleeper: callable = time.sleep,
) -> ProductionCanaryResult:
    """Run one exact-period canary through the configured production adapter.

    This is intentionally not the generic source-proposal crawler. It invokes
    the same adapter and configuration used by quarterly acquisition, asks for
    exactly ``expected_period``, validates the returned PDF text, and writes no
    catalog/ledger/outbox rows. A valid older PDF is an explicit failure.
    """

    normalized_expected = _canonical_quarter(expected_period)
    if normalized_expected is None:
        raise ValueError("expected_period must be canonical YYYY-NT")
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    sources = tuple(
        source
        for source in issuer.sources
        if source.enabled
        and source.kind in {"investor_relations", "bmv_issuer_pdf"}
        and (source_key is None or source.key == source_key)
    )
    if len(sources) != 1:
        return ProductionCanaryResult(
            issuer_slug=issuer.slug,
            source_key=source_key or "",
            source_kind="",
            expected_period=normalized_expected,
            status="source_binding_error",
            attempts=0,
            error=(
                f"expected exactly one enabled primary-PDF source, found {len(sources)}"
            ),
        )
    source = sources[0]
    if ir_adapter is None and source.kind == "investor_relations":
        from src.acquisition.adapters import InvestorRelationsAdapter

        ir_adapter = InvestorRelationsAdapter()
    if bmv_adapter is None and source.kind == "bmv_issuer_pdf":
        from src.acquisition.bmv_issuer import BmvIssuerPdfAdapter

        bmv_adapter = BmvIssuerPdfAdapter()

    last_result: ProductionCanaryResult | None = None
    for attempt in range(1, attempts + 1):
        try:
            with tempfile.TemporaryDirectory(prefix=f"{issuer.slug}-source-canary-") as temp:
                fetched, candidate_periods, layers, issues = _fetch_expected_from_production_adapter(
                    issuer,
                    source,
                    Path(temp),
                    normalized_expected,
                    ir_adapter=ir_adapter,
                    bmv_adapter=bmv_adapter,
                )
                exact = [
                    artifact
                    for artifact in fetched
                    if _canonical_quarter(artifact.source.period) == normalized_expected
                ]
                observed = sorted(
                    {
                        period
                        for artifact in fetched
                        if (period := _canonical_quarter(artifact.source.period)) is not None
                    },
                    key=period_sort_key,
                )
                if not exact:
                    selected = observed[-1] if observed else None
                    last_result = ProductionCanaryResult(
                        issuer_slug=issuer.slug,
                        source_key=source.key,
                        source_kind=source.kind,
                        expected_period=normalized_expected,
                        status="expected_period_not_found",
                        attempts=attempt,
                        selected_period=selected,
                        candidate_periods=candidate_periods,
                        layers=layers,
                        issues=issues,
                        error=(
                            f"configured adapter did not return {normalized_expected}"
                            + (f"; newest returned period was {selected}" if selected else "")
                        ),
                    )
                else:
                    by_hash = {artifact.sha256: artifact for artifact in exact}
                    if len(by_hash) != 1:
                        return ProductionCanaryResult(
                            issuer_slug=issuer.slug,
                            source_key=source.key,
                            source_kind=source.kind,
                            expected_period=normalized_expected,
                            status="conflicting_exact_period",
                            attempts=attempt,
                            selected_period=normalized_expected,
                            candidate_periods=candidate_periods,
                            layers=layers,
                            issues=issues,
                            error="multiple different PDF payloads were returned for the expected period",
                        )
                    artifact = next(iter(by_hash.values()))
                    last_result = _validate_production_artifact(
                        issuer,
                        source,
                        artifact,
                        expected_period=normalized_expected,
                        staging_dir=Path(staging_dir),
                        attempt=attempt,
                        min_pdf_bytes=min_pdf_bytes,
                        candidate_periods=candidate_periods,
                        layers=layers,
                        issues=issues,
                    )
        except Exception as exc:
            last_result = ProductionCanaryResult(
                issuer_slug=issuer.slug,
                source_key=source.key,
                source_kind=source.kind,
                expected_period=normalized_expected,
                status="adapter_error",
                attempts=attempt,
                error=f"{type(exc).__name__}: {exc}",
            )
        if last_result.passed:
            return last_result
        if attempt < attempts:
            sleeper(min(1.0, 0.25 * attempt))
    assert last_result is not None
    return last_result


def _fetch_expected_from_production_adapter(
    issuer: IssuerSpec,
    source: AcquisitionSource,
    staging_dir: Path,
    expected_period: str,
    *,
    ir_adapter: Any,
    bmv_adapter: Any,
) -> tuple[
    tuple[FetchedArtifact, ...],
    tuple[str, ...],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
]:
    if source.kind == "investor_relations":
        # The adapter's exclusion set is the production mechanism for avoiding
        # old downloads. Populate it fully so a stale-but-valid document cannot
        # satisfy or obscure this exact-period canary.
        all_periods = {
            f"{year}-{quarter}T"
            for year in range(1900, 2201)
            for quarter in range(1, 5)
        }
        effective_source = source
        if (
            _canonical_quarter(source.live_verified_period) == expected_period
            and source.live_verified_url
        ):
            # A registry-certified exact URL is stronger and cheaper than
            # rediscovering a slow/blocked index page. It still flows through
            # the production adapter's deterministic-template fetch and all
            # byte/text checks below.
            effective_source = replace(
                source,
                direct_url_templates=(source.live_verified_url,),
                max_reports=1,
            )
            verified_path = urlparse(source.live_verified_url).path.lower()
            if verified_path.endswith(".pdf"):
                # Some CDNs reject the template downloader's HEAD/range probe
                # even though the same PDF is discoverable from the live page.
                # Keep normal bounded discovery, with the certified URL as its
                # fallback, for ordinary .pdf links.
                effective_adapter = _bounded_exact_ir_adapter(ir_adapter)
            else:
                # Extensionless CMS objects (notably FEMSA) need the certified
                # direct route because their index can be slow or bot-blocked.
                effective_adapter = _configured_exact_url_ir_adapter(ir_adapter)
        else:
            effective_adapter = _bounded_exact_ir_adapter(ir_adapter)
        report = effective_adapter.fetch_incremental_report(
            issuer,
            effective_source,
            staging_dir,
            known_periods=all_periods,
            recheck_periods={expected_period},
            desired_periods={expected_period},
        )
        layers = tuple(
            {
                "layer": item.layer,
                "candidates": item.candidates,
                "selected": item.selected,
                "note": item.note,
            }
            for item in report.layers
        )
        issues = tuple(
            {
                "source_record_id": item.record.source_record_id,
                "period": item.record.period,
                "retryable": item.retryable,
                "error": item.error,
            }
            for item in report.issues
        )
        return report.artifacts, tuple(report.candidate_periods), layers, issues
    if source.kind == "bmv_issuer_pdf":
        discovered = bmv_adapter.discover(
            issuer,
            source,
            wanted_periods={expected_period},
        )
        artifacts = tuple(
            bmv_adapter.fetch(item, staging_dir, source=source)
            for item in discovered
        )
        periods = tuple(
            sorted(
                {
                    period
                    for item in discovered
                    if (period := _canonical_quarter(item.record.period)) is not None
                },
                key=period_sort_key,
            )
        )
        return artifacts, periods, (), ()
    raise ValueError(f"unsupported primary-PDF source kind: {source.kind}")


def _bounded_exact_ir_adapter(ir_adapter: Any) -> Any:
    """Cap a real production IR downloader after exact-period exclusions.

    The generic downloader deliberately retains unclassifiable legacy links as
    a safe fallback. For a canary, those must not trigger dozens of irrelevant
    downloads after every classified non-target period is excluded. Classified
    links sort newest-first, so a one-document cap leaves the requested exact
    period first. Injected test adapters without the production boundaries are
    returned unchanged.
    """

    base_downloader = getattr(ir_adapter, "_downloader", None)
    template_downloader = getattr(ir_adapter, "_template_downloader", None)
    if base_downloader is None or template_downloader is None:
        return ir_adapter
    from src.acquisition.adapters import InvestorRelationsAdapter

    def exact_downloader(url: str, output_dir: Path, **kwargs: Any):
        kwargs["max_reports"] = 1
        return base_downloader(url, output_dir, **kwargs)

    return InvestorRelationsAdapter(
        downloader=exact_downloader,
        template_downloader=template_downloader,
    )


def _configured_exact_url_ir_adapter(ir_adapter: Any) -> Any:
    """Use a registry-certified exact URL through the production fallback."""

    template_downloader = getattr(ir_adapter, "_template_downloader", None)
    if template_downloader is None:
        return ir_adapter
    from src.acquisition.adapters import InvestorRelationsAdapter

    def skip_index_page(_url: str, _output_dir: Path, **_kwargs: Any):
        return []

    return InvestorRelationsAdapter(
        downloader=skip_index_page,
        template_downloader=template_downloader,
    )


def _validate_production_artifact(
    issuer: IssuerSpec,
    source: AcquisitionSource,
    artifact: FetchedArtifact,
    *,
    expected_period: str,
    staging_dir: Path,
    attempt: int,
    min_pdf_bytes: int,
    candidate_periods: tuple[str, ...],
    layers: tuple[Mapping[str, Any], ...],
    issues: tuple[Mapping[str, Any], ...],
) -> ProductionCanaryResult:
    content = artifact.content
    valid_pdf = len(content) >= min_pdf_bytes and content.lstrip()[:5] == b"%PDF-"
    selected_period = _canonical_quarter(artifact.source.period)
    final_url = normalize_http_url(artifact.source.url) if artifact.source.url else None
    issuer_verified = False
    period_verified = False
    identity_terms: tuple[str, ...] = ()
    error: str | None = None
    if not valid_pdf:
        error = "production adapter returned invalid or undersized PDF bytes"
    else:
        try:
            text = extract_pdf_identity_text(content)
            issuer_verified, identity_terms = verify_issuer_identity(issuer, text)
            period_verified = verify_period_text(expected_period, text)
        except Exception as exc:
            error = f"PDF text extraction failed: {type(exc).__name__}: {exc}"
    if selected_period != expected_period:
        error = f"selected period {selected_period!r} does not equal {expected_period}"
    elif final_url is None:
        error = "production adapter returned no exact final source URL"
    elif not issuer_verified or not period_verified:
        error = "PDF sampled text did not verify issuer and exact expected period"

    local_path: str | None = None
    status = "validation_failed"
    if valid_pdf and issuer_verified and period_verified and final_url and selected_period == expected_period:
        target = staging_dir / issuer.slug / f"{expected_period}.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.replace(temporary, target)
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise
        local_path = str(target.resolve())
        status = "passed"
        error = None
    return ProductionCanaryResult(
        issuer_slug=issuer.slug,
        source_key=source.key,
        source_kind=source.kind,
        expected_period=expected_period,
        status=status,
        attempts=attempt,
        selected_period=selected_period,
        final_url=final_url,
        size_bytes=len(content),
        sha256=artifact.sha256,
        valid_pdf=valid_pdf,
        issuer_verified=issuer_verified,
        period_verified=period_verified,
        identity_terms=identity_terms,
        local_path=local_path,
        candidate_periods=candidate_periods,
        layers=layers,
        issues=issues,
        error=error,
    )


def _canonical_quarter(value: str | None) -> str | None:
    if not value:
        return None
    match = re.fullmatch(r"(20\d{2})-(?:Q([1-4])|([1-4])T)", value.strip().upper())
    if match is None:
        return None
    return f"{match.group(1)}-{match.group(2) or match.group(3)}T"


class SourceOnboardingCompiler:
    """Compile source proposals from bounded static discovery and PDF evidence."""

    def __init__(
        self,
        *,
        client: DiscoveryHttpClient,
        options: DiscoveryOptions | None = None,
        cache_dir: str | Path | None = None,
        verification_dir: str | Path | None = None,
        cache_max_age_seconds: float = DEFAULT_CACHE_MAX_AGE_SECONDS,
        clock: callable = time.time,
    ):
        self.client = client
        self.options = options or DiscoveryOptions()
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.verification_dir = (
            Path(verification_dir) if verification_dir is not None else None
        )
        self.cache_max_age_seconds = float(cache_max_age_seconds)
        self.clock = clock
        if self.cache_max_age_seconds < 0:
            raise ValueError("cache_max_age_seconds cannot be negative")

    def compile(
        self,
        issuer: IssuerSpec,
        *,
        readiness: PrimaryPdfReadinessRow | None = None,
        seeds: Sequence[DiscoverySeed] = (),
        refresh: bool = False,
        verified_on: date | None = None,
    ) -> SourceProposal:
        normalized_seeds = _dedupe_seeds(
            [*registry_seeds(issuer), *seeds]
        )
        gaps = tuple(
            value
            for value in (
                readiness.readiness_gaps.split("|") if readiness else ()
            )
            if value
        )
        key = _cache_key(issuer, normalized_seeds, gaps, self.options)
        # A requested verification artifact must be materialized during this
        # invocation, so a cached metadata-only proposal is insufficient.
        if not refresh and self.verification_dir is None:
            cached = self._load_cache(issuer.slug, key)
            if cached is not None:
                return cached

        proposal = self._discover(
            issuer,
            readiness_gaps=gaps,
            seeds=normalized_seeds,
            verified_on=verified_on or datetime.now(timezone.utc).date(),
        )
        self._store_cache(issuer.slug, key, proposal)
        return proposal

    def _discover(
        self,
        issuer: IssuerSpec,
        *,
        readiness_gaps: tuple[str, ...],
        seeds: tuple[DiscoverySeed, ...],
        verified_on: date,
    ) -> SourceProposal:
        if not seeds:
            return SourceProposal(
                issuer_slug=issuer.slug,
                ticker=issuer.ticker,
                issuer_name=issuer.name,
                readiness_gaps=readiness_gaps,
                status="needs_seed",
                confidence="none",
                cache_hit=False,
                notes=(
                    "No authoritative HTTP seed was found in the registry, catalog, or seed file.",
                ),
            )

        queue: list[tuple[str, str]] = []
        candidates_by_url: dict[str, LinkCandidate] = {}
        for seed in seeds:
            if _looks_like_pdf_url(seed.url):
                candidates_by_url[seed.url] = classify_link(
                    seed.url,
                    title="",
                    source_page=seed.url,
                )
            else:
                queue.append((seed.url, seed.origin))
                sitemap = _root_url(seed.url, "/sitemap.xml")
                queue.append((sitemap, "sitemap"))

        pages: list[PageAttempt] = []
        visited: set[str] = set()
        index = 0
        while index < len(queue) and len(visited) < self.options.max_pages:
            requested_url, layer = queue[index]
            index += 1
            normalized = normalize_http_url(requested_url)
            if normalized is None or normalized in visited:
                continue
            visited.add(normalized)
            try:
                payload = self.client.get(
                    normalized,
                    max_bytes=self.options.max_page_bytes,
                )
            except Exception as exc:
                pages.append(
                    PageAttempt(
                        url=normalized,
                        layer=layer,
                        status="failed",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            content_type = payload.content_type.lower()
            if _payload_is_pdf(payload):
                candidate = classify_link(
                    payload.final_url,
                    title="",
                    source_page=normalized,
                )
                candidates_by_url[candidate.url] = candidate
                pages.append(
                    PageAttempt(
                        url=normalized,
                        layer=layer,
                        status="pdf_candidate",
                        final_url=payload.final_url,
                        content_type=payload.content_type,
                    )
                )
                continue

            links = extract_static_links(payload)
            pages.append(
                PageAttempt(
                    url=normalized,
                    layer=layer,
                    status="ok",
                    final_url=payload.final_url,
                    content_type=payload.content_type,
                    links_seen=len(links),
                )
            )
            for link_url, title in links:
                candidate = classify_link(
                    link_url,
                    title=title,
                    source_page=payload.final_url,
                )
                if candidate.period is not None or _looks_like_pdf_url(link_url):
                    previous = candidates_by_url.get(candidate.url)
                    if previous is None or candidate.score > previous.score:
                        candidates_by_url[candidate.url] = candidate
                    continue
                if (
                    layer == "sitemap"
                    and urlparse(link_url).path.lower().endswith(".xml")
                    and _same_site(payload.final_url, link_url)
                    and len(queue) < self.options.max_pages * 4
                ):
                    queue.append((link_url, "sitemap"))
                elif (
                    _PAGE_TERMS.search(f"{title} {link_url}")
                    and not _HARD_NEGATIVE_TERMS.search(f"{title} {link_url}")
                    and _same_site(payload.final_url, link_url)
                    and len(queue) < self.options.max_pages * 4
                ):
                    queue.append((link_url, "linked_report_page"))

        candidates = sorted(
            candidates_by_url.values(),
            key=lambda item: (
                item.rejected_reason is not None,
                -item.score,
                tuple(-value for value in _safe_period_rank(item.period)[:2]),
                item.url,
            ),
        )[: self.options.max_candidates]
        eligible = [
            item
            for item in candidates
            if item.rejected_reason is None and item.period is not None
        ][: self.options.sample_pdfs]
        validations = tuple(
            self._validate_pdf(issuer, item)
            for item in eligible
        )
        verified = [item for item in validations if item.fully_verified]
        templates = infer_url_templates(verified)
        proposed_ir = build_ir_proposal(
            pages=pages,
            candidates=candidates,
            validations=verified,
            templates=templates,
            verified_on=verified_on,
        )

        distinct_periods = {item.advertised_period for item in verified}
        if len(distinct_periods) >= 2 and proposed_ir:
            status, confidence = "ready_for_review", "high"
        elif len(distinct_periods) == 1 and proposed_ir:
            status, confidence = "needs_second_sample", "medium"
        elif validations:
            status, confidence = "validation_failed", "low"
        else:
            status, confidence = "no_quarterly_candidates", "none"

        notes: list[str] = []
        if any(item.valid_pdf and not item.issuer_verified for item in validations):
            notes.append("At least one PDF could not be bound to the issuer from sampled PDF text.")
        if any(item.valid_pdf and not item.period_verified for item in validations):
            notes.append("At least one PDF did not confirm its advertised quarter in sampled PDF text.")
        if confidence == "medium":
            notes.append("One verified period is not enough to infer repeatability safely.")
        if confidence == "high" and not templates:
            notes.append("Multiple periods verified; discovery relies on the static index and strict link pattern.")

        proposal = SourceProposal(
            issuer_slug=issuer.slug,
            ticker=issuer.ticker,
            issuer_name=issuer.name,
            readiness_gaps=readiness_gaps,
            status=status,
            confidence=confidence,
            cache_hit=False,
            seeds=seeds,
            pages=tuple(pages),
            candidates=tuple(candidates),
            validations=validations,
            inferred_templates=templates,
            proposed_ir=proposed_ir,
            notes=tuple(notes),
        )
        return proposal

    def _validate_pdf(
        self,
        issuer: IssuerSpec,
        candidate: LinkCandidate,
    ) -> PdfValidation:
        try:
            payload = self.client.get(
                candidate.url,
                max_bytes=self.options.max_pdf_bytes,
            )
        except Exception as exc:
            return PdfValidation(
                url=candidate.url,
                final_url=None,
                advertised_period=candidate.period,
                valid_pdf=False,
                issuer_verified=False,
                period_verified=False,
                size_bytes=0,
                sha256=None,
                content_type=None,
                error=f"{type(exc).__name__}: {exc}",
            )
        content = payload.content
        valid_pdf = (
            not payload.truncated
            and len(content) >= self.options.min_pdf_bytes
            and content.lstrip()[:5] == b"%PDF-"
        )
        if not valid_pdf:
            reason = "response is truncated" if payload.truncated else "invalid or undersized PDF bytes"
            return PdfValidation(
                url=candidate.url,
                final_url=payload.final_url,
                advertised_period=candidate.period,
                valid_pdf=False,
                issuer_verified=False,
                period_verified=False,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest() if content else None,
                content_type=payload.content_type,
                error=reason,
            )
        try:
            text = extract_pdf_identity_text(content)
        except Exception as exc:
            return PdfValidation(
                url=candidate.url,
                final_url=payload.final_url,
                advertised_period=candidate.period,
                valid_pdf=True,
                issuer_verified=False,
                period_verified=False,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                content_type=payload.content_type,
                error=f"PDF text extraction failed: {type(exc).__name__}: {exc}",
            )
        issuer_verified, matched_terms = verify_issuer_identity(issuer, text)
        period_verified = verify_period_text(candidate.period, text)
        local_path: str | None = None
        if issuer_verified and period_verified and self.verification_dir is not None:
            period = candidate.period or "unknown-period"
            target = self.verification_dir / issuer.slug / f"{period}.pdf"
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                os.replace(temporary, target)
            except Exception:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                raise
            local_path = str(target.resolve())
        return PdfValidation(
            url=candidate.url,
            final_url=payload.final_url,
            advertised_period=candidate.period,
            valid_pdf=True,
            issuer_verified=issuer_verified,
            period_verified=period_verified,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content_type=payload.content_type,
            identity_terms=matched_terms,
            local_path=local_path,
            error=None if issuer_verified and period_verified else "PDF identity/period evidence is incomplete",
        )

    def _cache_path(self, slug: str, key: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{slug}-{key}.json"

    def _load_cache(self, slug: str, key: str) -> SourceProposal | None:
        path = self._cache_path(slug, key)
        if path is None or not path.is_file() or self.cache_max_age_seconds == 0:
            return None
        try:
            age = self.clock() - path.stat().st_mtime
            if age < 0 or age > self.cache_max_age_seconds:
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != SCHEMA_VERSION or raw.get("cache_key") != key:
                return None
            return SourceProposal.from_dict(raw["proposal"], cache_hit=True)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def _store_cache(self, slug: str, key: str, proposal: SourceProposal) -> None:
        path = self._cache_path(slug, key)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "algorithm": DISCOVERY_ALGORITHM,
            "cache_key": key,
            "proposal": proposal.to_dict(),
        }
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, path)
            os.utime(path, (self.clock(), self.clock()))
        except Exception:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


def parse_bmv_directory(content: bytes) -> tuple[dict[str, Any], ...]:
    """Parse BMV's anti-JSON-prefixed capital issuer directory response."""

    text = content.decode("utf-8", errors="replace").lstrip("\ufeff \r\n\t")
    if text.startswith("for(;;);"):
        text = text[len("for(;;);") :].strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    elif text.startswith("(") and text.endswith(");"):
        text = text[1:-2]
    try:
        raw = json.loads(text)
        source_rows = raw["response"]["resultado"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("BMV directory response is malformed") from exc
    if not isinstance(source_rows, list):
        raise ValueError("BMV directory result is not a list")
    rows: list[dict[str, Any]] = []
    for item in source_rows:
        if not isinstance(item, dict):
            continue
        ticker = item.get("claveEmisora")
        issuer_id = item.get("idEmisora")
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        if not isinstance(issuer_id, int) or isinstance(issuer_id, bool):
            continue
        rows.append(
            {
                "ticker": ticker.strip().upper(),
                "issuer_id": issuer_id,
                "legal_name": (
                    str(item.get("razonSocial") or "").strip() or None
                ),
            }
        )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                _normalize_ticker(str(row["ticker"])),
                int(row["issuer_id"]),
            ),
        )
    )


def parse_bmv_profile_website(
    content: bytes,
    *,
    expected_tickers: Iterable[str],
) -> tuple[str, str | None]:
    """Return the exact BMV profile ticker and normalized external Web URL."""

    soup = BeautifulSoup(content.decode("utf-8", errors="replace"), "html.parser")
    displayed_ticker: str | None = None
    website_value: str | None = None
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) < 2:
            cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = _fold(cells[0].get_text(" ", strip=True)).rstrip(":")
        value_cell = cells[1]
        if label == "clave" and displayed_ticker is None:
            value = value_cell.get_text(" ", strip=True).split()[0:1]
            displayed_ticker = value[0].upper() if value else None
        elif label == "web" and website_value is None:
            anchor = value_cell.find("a", href=True)
            website_value = (
                str(anchor.get("href")) if anchor is not None else value_cell.get_text(" ", strip=True)
            )
    if displayed_ticker is None:
        raise ValueError("BMV profile did not expose Clave")
    expected = {_normalize_ticker(value) for value in expected_tickers}
    if _normalize_ticker(displayed_ticker) not in expected:
        raise ValueError(
            f"BMV profile ticker mismatch: expected {sorted(expected)}, got {displayed_ticker}"
        )
    if website_value is None or website_value.strip().casefold() in {"", "n/a", "na", "-"}:
        return displayed_ticker, None
    candidate = website_value.strip()
    if not re.match(r"(?i)^https?://", candidate):
        candidate = "https://" + candidate.lstrip("/")
    website = normalize_http_url(candidate)
    if website is None:
        raise ValueError("BMV Web field is not an HTTP(S) URL")
    if (urlparse(website).hostname or "").lower() in {"bmv.com.mx", "www.bmv.com.mx"}:
        raise ValueError("BMV Web field points back to BMV")
    return displayed_ticker, website


def _issuer_bmv_ticker_keys(issuer: IssuerSpec) -> tuple[str, ...]:
    values = {issuer.ticker, issuer.market_ticker}
    values.update(
        source.xbrl_ticker
        for source in issuer.sources
        if source.xbrl_ticker is not None
    )
    return tuple(
        sorted(
            {
                _normalize_ticker(value)
                for value in values
                if value and _normalize_ticker(value)
            }
        )
    )


def _directory_rows_by_ticker(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, tuple[dict[str, Any], ...]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for value in rows:
        try:
            row = {
                "ticker": str(value["ticker"]),
                "issuer_id": int(value["issuer_id"]),
                "legal_name": value.get("legal_name"),
            }
        except (KeyError, TypeError, ValueError):
            continue
        grouped.setdefault(_normalize_ticker(row["ticker"]), []).append(row)
    return {
        ticker: tuple(
            sorted(values, key=lambda row: (int(row["issuer_id"]), str(row["ticker"])))
        )
        for ticker, values in sorted(grouped.items())
    }


def _normalize_ticker(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value).upper())


def _load_fresh_json_cache(
    path: Path,
    *,
    max_age_seconds: float,
    now: float,
) -> dict[str, Any] | None:
    if not path.is_file() or max_age_seconds == 0:
        return None
    try:
        age = now - path.stat().st_mtime
        if age < 0 or age > max_age_seconds:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _atomic_json_write(path: Path, payload: Mapping[str, Any], *, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
        os.utime(path, (mtime, mtime))
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def catalog_seeds(
    db_path: str | Path,
    issuer_slug: str,
    *,
    limit: int = 40,
) -> tuple[DiscoverySeed, ...]:
    """Read likely primary-PDF URLs from an existing catalog, without mutation."""

    database = Path(db_path)
    if not database.is_file():
        return ()
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT DISTINCT d.source_url
            FROM documents d
            WHERE d.source_url IS NOT NULL
              AND d.source_url <> ''
              AND d.doc_type IN ('quarterly_release', 'quarterly_report')
              AND (
                    lower(d.company)=lower(?)
                    OR EXISTS (
                        SELECT 1 FROM memberships m
                        WHERE m.document_id=d.document_id
                          AND lower(m.company)=lower(?)
                    )
                  )
            ORDER BY d.source_url
            LIMIT ?
            """,
            (issuer_slug, issuer_slug, limit),
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        if connection is not None:
            connection.close()
    result: list[DiscoverySeed] = []
    for row in rows:
        url = normalize_http_url(str(row["source_url"]))
        if url is None or _is_regulatory_payload(url):
            continue
        result.append(DiscoverySeed(url=url, origin="catalog_primary_pdf"))
    return _dedupe_seeds(result)


def registry_seeds(issuer: IssuerSpec) -> tuple[DiscoverySeed, ...]:
    result: list[DiscoverySeed] = []
    for source in issuer.sources:
        if source.url:
            result.append(DiscoverySeed(source.url, f"registry:{source.key}:url"))
        if source.live_verified_url:
            result.append(
                DiscoverySeed(source.live_verified_url, f"registry:{source.key}:verified")
            )
        for template in source.direct_url_templates:
            # A template is evidence but cannot be fetched until a period is
            # supplied. Its origin and parent still provide a useful page seed.
            parsed = urlparse(template)
            parent = parsed.path.rsplit("/", 1)[0] + "/"
            result.append(
                DiscoverySeed(
                    urlunparse((parsed.scheme, parsed.netloc, parent, "", "", "")),
                    f"registry:{source.key}:template_parent",
                )
            )
    return _dedupe_seeds(result)


def load_seed_file(path: str | Path) -> dict[str, tuple[DiscoverySeed, ...]]:
    """Load compact YAML/JSON issuer seeds supplied by an analyst.

    Accepted shapes are ``issuers: {slug: [url, ...]}`` or simply
    ``{slug: [url, ...]}``. Values may also be a single URL string.
    """

    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("seed file must be a mapping")
    values = raw.get("issuers", raw)
    if not isinstance(values, dict):
        raise ValueError("seed file 'issuers' must be a mapping")
    result: dict[str, tuple[DiscoverySeed, ...]] = {}
    for slug, urls in values.items():
        if not isinstance(slug, str) or not slug.strip():
            raise ValueError("seed issuer slugs must be non-empty strings")
        items = [urls] if isinstance(urls, str) else urls
        if not isinstance(items, list):
            raise ValueError(f"{slug}: seed URLs must be a string or list")
        parsed: list[DiscoverySeed] = []
        for value in items:
            url = normalize_http_url(value) if isinstance(value, str) else None
            if url is None:
                raise ValueError(f"{slug}: invalid HTTP seed URL {value!r}")
            parsed.append(DiscoverySeed(url=url, origin=f"seed_file:{Path(path).name}"))
        result[slug.strip().lower()] = _dedupe_seeds(parsed)
    return result


def select_readiness_targets(
    registry: IssuerRegistry,
    rows: Sequence[PrimaryPdfReadinessRow],
    *,
    issuer_slugs: Iterable[str] = (),
    include_configured_gaps: bool = False,
) -> tuple[tuple[IssuerSpec, PrimaryPdfReadinessRow], ...]:
    """Select active readiness gaps in stable registry order."""

    requested = {value.strip().lower() for value in issuer_slugs if value.strip()}
    by_slug = {row.issuer_slug: row for row in rows}
    unknown = requested - set(registry.by_slug)
    if unknown:
        raise KeyError(f"unknown issuer slug(s): {', '.join(sorted(unknown))}")
    selected: list[tuple[IssuerSpec, PrimaryPdfReadinessRow]] = []
    for issuer in registry.issuers:
        if requested and issuer.slug not in requested:
            continue
        row = by_slug[issuer.slug]
        gaps = set(row.readiness_gaps.split("|"))
        if requested or "primary_pdf_source_unconfigured" in gaps or (
            include_configured_gaps and gaps
        ):
            selected.append((issuer, row))
    return tuple(selected)


def select_production_canary_issuers(
    registry: IssuerRegistry,
    *,
    identifiers: Iterable[str] = (),
    random_sample: int | None = None,
    sample_seed: int = 20260801,
) -> tuple[IssuerSpec, ...]:
    """Select configured primary-PDF canaries in a reproducible order."""

    requested = tuple(value.strip() for value in identifiers if value.strip())
    if requested and random_sample is not None:
        raise ValueError("explicit identifiers and random_sample are mutually exclusive")
    if random_sample is not None and random_sample <= 0:
        raise ValueError("random_sample must be positive")
    configured = tuple(
        issuer
        for issuer in registry.issuers
        if issuer.active
        and any(
            source.enabled
            and source.kind in {"investor_relations", "bmv_issuer_pdf"}
            for source in issuer.sources
        )
    )
    if requested:
        selected: list[IssuerSpec] = []
        for identifier in requested:
            issuer = registry.find(identifier)
            if issuer not in configured:
                raise ValueError(f"{issuer.slug}: no enabled primary-PDF source")
            if issuer not in selected:
                selected.append(issuer)
        return tuple(selected)
    if random_sample is None:
        raise ValueError("explicit identifiers or random_sample are required")
    import random

    generator = random.Random(sample_seed)
    indexes = sorted(
        generator.sample(range(len(configured)), min(random_sample, len(configured)))
    )
    return tuple(configured[index] for index in indexes)


def classify_link(url: str, *, title: str, source_page: str) -> LinkCandidate:
    normalized = normalize_http_url(url) or url
    decoded = unquote(normalized)
    signal = html.unescape(f"{title} {decoded}")
    period = infer_period_label(signal)
    if period and period.endswith("-FY"):
        period = None
    reasons: list[str] = []
    score = 0
    rejected: str | None = None
    if _HARD_NEGATIVE_TERMS.search(signal):
        rejected = "negative_document_type"
    if period:
        score += 60
        reasons.append("canonical_quarter_detected")
    else:
        reasons.append("no_canonical_quarter")
    if _REPORT_TERMS.search(signal):
        score += 25
        reasons.append("quarterly_report_term")
    if _PRIMARY_RELEASE_TERMS.search(signal):
        score += 30
        reasons.append("primary_release_term")
    if _PDF_HINT.search(signal):
        score += 15
        reasons.append("pdf_or_download_hint")
    if rejected is None and period is None:
        rejected = "period_unresolved"
    if rejected is None and not (_REPORT_TERMS.search(signal) or _PDF_HINT.search(signal)):
        rejected = "report_identity_unresolved"
    return LinkCandidate(
        url=normalized,
        title=" ".join(title.split())[:500],
        source_page=source_page,
        period=period,
        score=score,
        reasons=tuple(reasons),
        rejected_reason=rejected,
    )


def extract_static_links(payload: HttpPayload) -> tuple[tuple[str, str], ...]:
    """Extract HTTP links from static HTML, XML sitemaps, and embedded JSON."""

    text = payload.content.decode("utf-8", errors="replace")
    content_type = payload.content_type.lower()
    links: dict[str, str] = {}
    if "xml" in content_type or text.lstrip().startswith("<?xml"):
        try:
            root = ElementTree.fromstring(text)
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1].lower() == "loc" and node.text:
                    url = normalize_http_url(node.text.strip())
                    if url:
                        links[url] = "sitemap"
        except ElementTree.ParseError:
            pass
        else:
            return tuple(sorted(links.items()))
    soup = BeautifulSoup(text, "html.parser")
    for anchor in soup.find_all("a", href=True):
        try:
            joined = urljoin(payload.final_url, str(anchor.get("href")))
        except ValueError:
            # Corporate/trustee sites occasionally contain malformed IPv6-like
            # hrefs. One bad anchor must not discard the rest of the page.
            continue
        url = normalize_http_url(joined)
        if url:
            links[url] = " ".join(anchor.stripped_strings)
    for element in soup.find_all(["iframe", "embed", "object"], src=True):
        try:
            joined = urljoin(payload.final_url, str(element.get("src")))
        except ValueError:
            continue
        url = normalize_http_url(joined)
        if url:
            links[url] = str(element.get("title") or "embedded document")
    # Many CMS pages serialize direct document URLs into JSON script blocks.
    # Extract only explicit HTTP URLs; no script execution or browser is used.
    unescaped_text = html.unescape(text).replace("\\/", "/")
    for raw_url in _URL_RE.findall(unescaped_text):
        url = normalize_http_url(raw_url)
        if url and (_PDF_HINT.search(url) or _PAGE_TERMS.search(url)):
            links.setdefault(url, "embedded static URL")
    return tuple(sorted(links.items()))


def extract_pdf_identity_text(content: bytes, *, pages: int = 4) -> str:
    chunks: list[str] = []
    with pdfplumber.open(BytesIO(content)) as document:
        for page in document.pages[:pages]:
            value = page.extract_text() or ""
            if value:
                chunks.append(value)
    return "\n".join(chunks)[:100_000]


def verify_issuer_identity(issuer: IssuerSpec, text: str) -> tuple[bool, tuple[str, ...]]:
    normalized_text = _fold(text)
    ticker_tokens = {
        _fold(value)
        for value in (issuer.ticker, issuer.market_ticker)
        if value and len(_fold(value)) >= 3
    }
    name_tokens = [
        token
        for token in _fold(issuer.name).split()
        if len(token) >= 4 and token not in _GENERIC_IDENTITY_WORDS
    ]
    matched_tickers = sorted({token for token in ticker_tokens if _word_in(token, normalized_text)})
    matched_names = sorted(token for token in name_tokens if _word_in(token, normalized_text))
    distinct_name_tokens = sorted(set(name_tokens))
    name_required = 1 if len(distinct_name_tokens) == 1 else 2
    composite_matches = sorted(
        match
        for ticker in ticker_tokens
        if (match := _composite_ticker_match(ticker, normalized_text)) is not None
    )
    verified = (
        bool(matched_tickers)
        or len(matched_names) >= name_required
        or bool(composite_matches)
    )
    return verified, tuple(dict.fromkeys([*matched_tickers, *matched_names, *composite_matches]))


def verify_period_text(period: str | None, text: str) -> bool:
    if period is None:
        return False
    match = re.fullmatch(r"(20\d{2})-([1-4])T", period)
    if match is None:
        return False
    year, quarter = match.groups()
    folded = _fold(text)
    if year not in folded and year[-2:] not in folded:
        return False
    quarter_words = {
        "1": ("first quarter", "primer trimestre", "primero trimestre"),
        "2": ("second quarter", "segundo trimestre"),
        "3": ("third quarter", "tercer trimestre", "tercero trimestre"),
        "4": ("fourth quarter", "cuarto trimestre"),
    }[quarter]
    compact = re.sub(r"\s+", " ", folded)
    token = re.search(
        rf"(?i)(?:^|[^0-9a-z])(?:q{quarter}|{quarter}[tq])\s*[-_/ ]?\s*(?:{year}|{year[-2:]})?(?:[^0-9a-z]|$)",
        compact,
    )
    return bool(token) or any(word in compact for word in quarter_words)


def infer_url_templates(validations: Sequence[PdfValidation]) -> tuple[str, ...]:
    """Infer only templates supported by at least two exact verified URLs."""

    groups: dict[str, list[tuple[str, str]]] = {}
    for item in validations:
        if not item.fully_verified or not item.final_url or not item.advertised_period:
            continue
        template = _single_url_template(item.final_url, item.advertised_period)
        if template:
            groups.setdefault(template, []).append((item.final_url, item.advertised_period))
    accepted: list[str] = []
    for template, samples in sorted(groups.items()):
        periods = {period for _url, period in samples}
        if len(periods) < 2:
            continue
        if all(_render_template(template, period) == url for url, period in samples):
            accepted.append(template)
    return tuple(accepted)


def build_ir_proposal(
    *,
    pages: Sequence[PageAttempt],
    candidates: Sequence[LinkCandidate],
    validations: Sequence[PdfValidation],
    templates: Sequence[str],
    verified_on: date,
) -> dict[str, Any]:
    if not validations:
        return {}
    validated_urls = {item.final_url or item.url for item in validations}
    source_counts: dict[str, int] = {}
    for candidate in candidates:
        if candidate.url in validated_urls:
            source_counts[candidate.source_page] = source_counts.get(candidate.source_page, 0) + 1
    page_url = None
    if source_counts:
        page_url = sorted(source_counts, key=lambda value: (-source_counts[value], value))[0]
    if page_url is None or _looks_like_pdf_url(page_url):
        for page in pages:
            if page.status == "ok" and page.final_url and not _looks_like_pdf_url(page.final_url):
                page_url = page.final_url
                break
    if page_url is None:
        return {}
    latest = max(
        validations,
        key=lambda item: period_sort_key(item.advertised_period or "0000-1T"),
    )
    earliest = min(
        validations,
        key=lambda item: period_sort_key(item.advertised_period or "9999-4T"),
    )
    proposal: dict[str, Any] = {
        "url": page_url,
        "pdf_link_pattern": infer_link_pattern(validations),
        "strict_pdf_link_pattern": True,
        "use_playwright": False,
        "max_reports": 120,
        "coverage_from_period": earliest.advertised_period,
        "live_verified_period": latest.advertised_period,
        "live_verified_on": verified_on.isoformat(),
        "live_verified_url": latest.final_url or latest.url,
    }
    if templates:
        proposal["direct_url_templates"] = list(templates)
    return proposal


def infer_link_pattern(validations: Sequence[PdfValidation]) -> str:
    """Return a conservative period-bearing PDF selector for reviewed samples."""

    extensions = all(
        urlparse(item.final_url or item.url).path.lower().endswith(".pdf")
        for item in validations
    )
    tail = r"\.pdf(?:[?#]|$)" if extensions else r"(?:[?#]|$)"
    return (
        r"(?i)(?:results?|resultados|earnings?|quarter(?:ly)?|trimestr(?:e|al)|"
        r"report(?:e|es)?|informe|comunicado|press[-_ ]?release|"
        r"(?:^|[/_.-])[1-4][tq](?:20)?\d{2}|(?:^|[/_.-])q[1-4](?:20)?\d{2})"
        r"[^?#]*" + tail
    )


def render_registry_patch(
    registry_path: str | Path,
    proposals: Sequence[SourceProposal],
) -> str:
    """Render a comment-preserving unified diff for high-confidence proposals."""

    path = Path(registry_path)
    original = path.read_text(encoding="utf-8")
    updated = original
    for proposal in sorted(proposals, key=lambda item: item.issuer_slug):
        if not proposal.patch_eligible:
            continue
        updated = _insert_ir_overlay(updated, proposal.issuer_slug, proposal.proposed_ir)
    if updated == original:
        return ""
    return "".join(
        unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=str(path),
            tofile=str(path),
        )
    )


def apply_registry_proposals(
    registry_path: str | Path,
    proposals: Sequence[SourceProposal],
) -> tuple[str, ...]:
    """Atomically add high-confidence missing ``ir`` overlays.

    Existing IR mappings are never overwritten. The candidate file is loaded
    through the canonical registry parser before replacement.
    """

    path = Path(registry_path)
    original = path.read_text(encoding="utf-8")
    updated = original
    applied: list[str] = []
    for proposal in sorted(proposals, key=lambda item: item.issuer_slug):
        if not proposal.patch_eligible:
            continue
        candidate = _insert_ir_overlay(updated, proposal.issuer_slug, proposal.proposed_ir)
        if candidate != updated:
            updated = candidate
            applied.append(proposal.issuer_slug)
    if updated == original:
        return ()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(updated)
        load_issuer_registry(temporary)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return tuple(applied)


def normalize_http_url(value: str) -> str | None:
    try:
        parsed = urlparse(value.strip())
    except (AttributeError, ValueError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    if parsed.username or parsed.password:
        return None
    path = quote(unquote(parsed.path or "/"), safe="/:@-._~!$&'()*+,;=")
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            parsed.query,
            "",
        )
    )


def _insert_ir_overlay(text: str, slug: str, ir: Mapping[str, Any]) -> str:
    import yaml

    parsed = yaml.safe_load(text)
    overlays = parsed.get("overlays") if isinstance(parsed, dict) else None
    if not isinstance(overlays, dict):
        raise ValueError("registry has no overlays mapping")
    current = overlays.get(slug)
    if isinstance(current, dict) and "ir" in current:
        return text
    dumped = yaml.safe_dump(
        {"ir": dict(ir)},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=120,
    ).rstrip()
    block = "\n".join(f"    {line}" for line in dumped.splitlines()) + "\n"
    lines = text.splitlines(keepends=True)
    overlay_start = next(
        (
            index
            for index, line in enumerate(lines)
            if re.fullmatch(r"overlays:\s*(?:\{\})?\s*\n?", line)
        ),
        None,
    )
    if overlay_start is None:
        raise ValueError("registry has no overlays section")
    if re.fullmatch(r"overlays:\s*\{\}\s*\n?", lines[overlay_start]):
        lines[overlay_start] = "overlays:\n"
    issuer_line = next(
        (
            index
            for index in range(overlay_start + 1, len(lines))
            if re.fullmatch(rf"  {re.escape(slug)}:\s*\n?", lines[index])
        ),
        None,
    )
    if issuer_line is not None:
        insertion = issuer_line + 1
        while insertion < len(lines) and not re.match(r"^  \S", lines[insertion]):
            insertion += 1
        lines.insert(insertion, block)
        return "".join(lines)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.extend([f"  {slug}:\n", block])
    return "".join(lines)


def _single_url_template(url: str, period: str) -> str | None:
    match = re.fullmatch(r"(20\d{2})-([1-4])T", period)
    if match is None:
        return None
    year, quarter = match.groups()
    year2 = year[-2:]
    replacements = (
        (rf"(?i)(?<![0-9]){quarter}([tq])[-_ ]?{year}(?![0-9])", r"{quarter}\1{year}"),
        (rf"(?i)(?<![0-9]){quarter}([tq])[-_ ]?{year2}(?![0-9])", r"{quarter}\1{year2}"),
        (rf"(?i)(?<![0-9])([tq]){quarter}[-_ ]?{year}(?![0-9])", r"\1{quarter}{year}"),
        (rf"(?i)(?<![0-9])([tq]){quarter}[-_ ]?{year2}(?![0-9])", r"\1{quarter}{year2}"),
        (rf"(?<![0-9]){year}[-_ ]?([tq]){quarter}(?![0-9])", r"{year}\1{quarter}"),
        (rf"(?<![0-9]){year}[-_ ]{quarter}(?![0-9])", r"{year}-{quarter}"),
    )
    for pattern, replacement in replacements:
        candidate, count = re.subn(pattern, replacement, url, count=1)
        if count:
            return candidate
    # Common directory/filename split, e.g. /2026/q2/report-2q26.pdf.
    candidate = re.sub(rf"(?<![0-9]){year}(?![0-9])", "{year}", url)
    candidate = re.sub(
        rf"(?i)(?<![0-9])(?:q{quarter}|{quarter}[tq])(?:{year2})?(?![0-9])",
        "{quarter}T{year2}",
        candidate,
    )
    return candidate if "{year}" in candidate and "{quarter}" in candidate else None


def _render_template(template: str, period: str) -> str:
    year, quarter_token = period.split("-", 1)
    quarter = quarter_token[0]
    return template.format(year=year, year2=year[-2:], quarter=quarter)


def _cache_key(
    issuer: IssuerSpec,
    seeds: Sequence[DiscoverySeed],
    gaps: Sequence[str],
    options: DiscoveryOptions,
) -> str:
    payload = {
        "algorithm": DISCOVERY_ALGORITHM,
        "issuer": {
            "slug": issuer.slug,
            "ticker": issuer.ticker,
            "market_ticker": issuer.market_ticker,
            "name": issuer.name,
        },
        "seeds": [asdict(seed) for seed in seeds],
        "gaps": list(gaps),
        "options": asdict(options),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


def _dedupe_seeds(values: Iterable[DiscoverySeed]) -> tuple[DiscoverySeed, ...]:
    result: dict[str, DiscoverySeed] = {}
    for seed in values:
        url = normalize_http_url(seed.url)
        if url is None:
            continue
        result.setdefault(url, DiscoverySeed(url=url, origin=seed.origin))
    return tuple(result[url] for url in sorted(result))


def _safe_period_rank(period: str | None) -> tuple[int, int, str]:
    return period_sort_key(period) if period else (0, 0, "")


def _same_site(left: str, right: str) -> bool:
    left_host = urlparse(left).hostname or ""
    right_host = urlparse(right).hostname or ""
    left_base = left_host.removeprefix("www.")
    right_base = right_host.removeprefix("www.")
    return bool(left_base and right_base and (left_base == right_base or left_base.endswith("." + right_base) or right_base.endswith("." + left_base)))


def _root_url(url: str, path: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _looks_like_pdf_url(url: str) -> bool:
    return bool(_PDF_HINT.search(url))


def _payload_is_pdf(payload: HttpPayload) -> bool:
    return "application/pdf" in payload.content_type.lower() or payload.content.lstrip()[:5] == b"%PDF-"


def _is_regulatory_payload(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.path.lower().endswith((".zip", ".json", ".gz"))
        or "ifrsxbrl" in parsed.path.lower()
        or "fiduxbrl" in parsed.path.lower()
    )


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


def _word_in(token: str, text: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text))


def _composite_ticker_match(ticker: str, text: str) -> str | None:
    """Match portmanteau tickers such as WALMEX -> Walmart + Mexico.

    This fallback is intentionally narrow: the ticker must split into exactly
    two chunks of at least three characters and both chunks must begin distinct
    words in the PDF. It cannot turn an arbitrary substring into identity.
    """

    if len(ticker) < 6 or not ticker.isalnum():
        return None
    words = set(text.split())
    for split in range(3, len(ticker) - 2):
        left, right = ticker[:split], ticker[split:]
        left_words = {word for word in words if word.startswith(left)}
        right_words = {word for word in words if word.startswith(right)}
        if left_words and right_words and left_words != right_words:
            return f"{ticker}~{left}+{right}"
    return None


__all__ = [
    "BMV_DIRECTORY_REFERER",
    "BMV_DIRECTORY_URL",
    "BmvDirectorySeedProvider",
    "BmvWebsiteSeedEvidence",
    "DEFAULT_CACHE_MAX_AGE_SECONDS",
    "DiscoveryOptions",
    "DiscoverySeed",
    "HttpPayload",
    "LinkCandidate",
    "PageAttempt",
    "PdfValidation",
    "ProductionCanaryResult",
    "RequestsDiscoveryClient",
    "SourceOnboardingCompiler",
    "SourceProposal",
    "apply_registry_proposals",
    "build_ir_proposal",
    "catalog_seeds",
    "classify_link",
    "extract_static_links",
    "infer_link_pattern",
    "infer_url_templates",
    "load_seed_file",
    "normalize_http_url",
    "parse_bmv_directory",
    "parse_bmv_profile_website",
    "registry_seeds",
    "render_registry_patch",
    "select_readiness_targets",
    "select_production_canary_issuers",
    "verify_issuer_identity",
    "verify_configured_production_source",
    "verify_period_text",
]
