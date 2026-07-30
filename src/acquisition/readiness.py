"""Read-only primary-PDF source readiness matrix for the canonical fleet."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol

from src.acquisition.models import AcquisitionSource
from src.acquisition.registry import IssuerRegistry
from src.acquisition.service import PDF_DOCUMENT_TYPE, PDF_SOURCE_KINDS
from src.shared.report_index import period_sort_key


class CoverageProtocol(Protocol):
    def known_periods(self, issuer_slug: str, document_type: str) -> set[str]: ...


@dataclass(frozen=True, slots=True)
class PrimaryPdfReadinessRow:
    issuer_slug: str
    ticker: str
    name: str
    sector: str
    active: bool
    listed_from: str | None
    listed_to: str | None
    source_state: str
    source_keys: str
    source_kinds: str
    source_urls: str
    coverage_from_periods: str
    live_verified_periods: str
    live_verified_ons: str
    live_verified_urls: str
    known_period_count: int
    latest_known_period: str | None
    in_source_window_period_count: int
    latest_in_source_window_period: str | None
    readiness_gaps: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_primary_pdf_readiness(
    registry: IssuerRegistry,
    coverage: CoverageProtocol,
) -> tuple[PrimaryPdfReadinessRow, ...]:
    """Return one deterministic row per issuer without touching the network."""

    rows: list[PrimaryPdfReadinessRow] = []
    for issuer in registry.issuers:
        sources = tuple(
            source
            for source in issuer.sources
            if source.enabled and source.kind in PDF_SOURCE_KINDS
        )
        known = sorted(
            coverage.known_periods(issuer.slug, PDF_DOCUMENT_TYPE),
            key=period_sort_key,
        )
        in_source_window = [
            period
            for period in known
            if any(
                _period_is_in_source_window(period, source)
                for source in sources
            )
        ]
        if not issuer.active:
            state = "inactive"
        elif sources and in_source_window:
            state = "configured_with_in_window_catalog_coverage"
        elif sources and known:
            state = "configured_with_only_out_of_window_catalog_coverage"
        elif sources:
            state = "configured_without_catalog_coverage"
        elif known:
            state = "catalog_coverage_without_configured_source"
        else:
            state = "unconfigured_without_coverage"

        gaps: list[str] = []
        if issuer.active and issuer.listed_from is None:
            gaps.append("listing_start_unverified")
        if issuer.active and not sources:
            gaps.append("primary_pdf_source_unconfigured")
        if sources and not known:
            gaps.append("configured_source_has_no_catalog_coverage")
        if sources and known and not in_source_window:
            gaps.append("catalog_coverage_outside_configured_source_window")
        if sources and any(
            source.live_verified_period is None for source in sources
        ):
            gaps.append("primary_pdf_source_live_verification_unrecorded")
        rows.append(
            PrimaryPdfReadinessRow(
                issuer_slug=issuer.slug,
                ticker=issuer.ticker,
                name=issuer.name,
                sector=issuer.sector,
                active=issuer.active,
                listed_from=(
                    issuer.listed_from.isoformat()
                    if issuer.listed_from is not None
                    else None
                ),
                listed_to=(
                    issuer.listed_to.isoformat()
                    if issuer.listed_to is not None
                    else None
                ),
                source_state=state,
                source_keys="|".join(source.key for source in sources),
                source_kinds="|".join(source.kind for source in sources),
                source_urls="|".join(
                    source.url or "" for source in sources
                ),
                coverage_from_periods="|".join(
                    source.coverage_from_period or ""
                    for source in sources
                ),
                live_verified_periods="|".join(
                    source.live_verified_period or ""
                    for source in sources
                ),
                live_verified_ons="|".join(
                    (
                        source.live_verified_on.isoformat()
                        if source.live_verified_on is not None
                        else ""
                    )
                    for source in sources
                ),
                live_verified_urls="|".join(
                    source.live_verified_url or ""
                    for source in sources
                ),
                known_period_count=len(known),
                latest_known_period=known[-1] if known else None,
                in_source_window_period_count=len(in_source_window),
                latest_in_source_window_period=(
                    in_source_window[-1] if in_source_window else None
                ),
                readiness_gaps="|".join(gaps),
            )
        )
    return tuple(rows)


def summarize_primary_pdf_readiness(
    rows: tuple[PrimaryPdfReadinessRow, ...],
) -> dict[str, object]:
    states: dict[str, int] = {}
    for row in rows:
        states[row.source_state] = states.get(row.source_state, 0) + 1
    return {
        "issuers": len(rows),
        "active_issuers": sum(row.active for row in rows),
        "issuers_with_configured_source": sum(
            bool(row.source_keys) for row in rows
        ),
        "configured_source_bindings": sum(
            len(row.source_keys.split("|")) if row.source_keys else 0
            for row in rows
        ),
        "issuers_with_live_verified_source": sum(
            bool(row.live_verified_periods.replace("|", ""))
            for row in rows
        ),
        "live_verified_source_bindings": sum(
            sum(bool(period) for period in row.live_verified_periods.split("|"))
            if row.source_keys
            else 0
            for row in rows
        ),
        "issuers_with_catalog_coverage": sum(
            row.known_period_count > 0 for row in rows
        ),
        "states": dict(sorted(states.items())),
    }


def _period_is_in_source_window(
    period: str,
    source: AcquisitionSource,
) -> bool:
    lower_bounds: list[tuple[int, int]] = []
    if source.floor_year is not None:
        lower_bounds.append((source.floor_year, 1))
    if source.coverage_from_period is not None:
        lower_bounds.append(period_sort_key(source.coverage_from_period))
    return not lower_bounds or period_sort_key(period) >= max(lower_bounds)


__all__ = [
    "PrimaryPdfReadinessRow",
    "build_primary_pdf_readiness",
    "summarize_primary_pdf_readiness",
]
