from __future__ import annotations

from datetime import date

from src.acquisition.models import AcquisitionSource, IssuerSpec
from src.acquisition.readiness import (
    build_primary_pdf_readiness,
    summarize_primary_pdf_readiness,
)
from src.acquisition.registry import IssuerRegistry


class Coverage:
    def __init__(self, periods):
        self.periods = periods

    def known_periods(self, issuer_slug, _document_type):
        return set(self.periods.get(issuer_slug, ()))


def issuer(slug, *, source=None, active=True):
    return IssuerSpec(
        slug=slug,
        ticker=slug.upper(),
        name=slug.title(),
        sector="test",
        template="generic",
        active=active,
        sources=(source,) if source is not None else (),
    )


def test_readiness_matrix_distinguishes_configuration_from_existing_coverage():
    source = AcquisitionSource(
        key="bmv_pdf",
        kind="bmv_issuer_pdf",
        url="https://example.test/bmv",
        coverage_from_period="2026-2T",
        live_verified_period="2026-2T",
        live_verified_on=date(2026, 7, 30),
        live_verified_url="https://example.test/2026-2T.pdf",
    )
    registry = IssuerRegistry(
        1,
        (
            issuer("ready", source=source),
            issuer("configured", source=source),
            issuer("legacy"),
            issuer("missing"),
            issuer("retired", active=False),
        ),
    )
    rows = build_primary_pdf_readiness(
        registry,
        Coverage(
            {
                "ready": {"2026-1T", "2026-2T"},
                "legacy": {"2025-4T"},
            }
        ),
    )
    by_slug = {row.issuer_slug: row for row in rows}

    assert by_slug["ready"].source_state == (
        "configured_with_in_window_catalog_coverage"
    )
    assert by_slug["ready"].latest_known_period == "2026-2T"
    assert by_slug["ready"].in_source_window_period_count == 1
    assert by_slug["ready"].live_verified_periods == "2026-2T"
    assert by_slug["configured"].source_state == (
        "configured_without_catalog_coverage"
    )
    assert by_slug["configured"].coverage_from_periods == "2026-2T"
    assert by_slug["legacy"].source_state == (
        "catalog_coverage_without_configured_source"
    )
    assert by_slug["missing"].source_state == "unconfigured_without_coverage"
    assert by_slug["retired"].source_state == "inactive"
    summary = summarize_primary_pdf_readiness(rows)
    assert summary["issuers_with_configured_source"] == 2
    assert summary["configured_source_bindings"] == 2
    assert summary["issuers_with_live_verified_source"] == 2
    assert summary["live_verified_source_bindings"] == 2


def test_catalog_coverage_before_activation_is_not_attributed_to_new_source():
    source = AcquisitionSource(
        key="bmv_pdf",
        kind="bmv_issuer_pdf",
        url="https://example.test/bmv",
        coverage_from_period="2026-2T",
    )
    rows = build_primary_pdf_readiness(
        IssuerRegistry(1, (issuer("legacy", source=source),)),
        Coverage({"legacy": {"2026-1T"}}),
    )

    row = rows[0]
    assert row.source_state == (
        "configured_with_only_out_of_window_catalog_coverage"
    )
    assert row.in_source_window_period_count == 0
    assert "catalog_coverage_outside_configured_source_window" in (
        row.readiness_gaps
    )
    assert "primary_pdf_source_live_verification_unrecorded" in (
        row.readiness_gaps
    )
