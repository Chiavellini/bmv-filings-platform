from __future__ import annotations

import ast
from datetime import date
import hashlib
from pathlib import Path
import re

import pytest
import yaml

from src.acquisition.models import (
    AcquisitionSource,
    FetchedArtifact,
    SourceRecord,
)
from src.acquisition.registry import IssuerRegistryError, load_issuer_registry


ROOT = Path(__file__).resolve().parents[1]


def test_canonical_registry_covers_actual_project_universes():
    registry = load_issuer_registry()

    assert len(registry.issuers) == 179
    assert len(registry.for_project("alpha_go")) == 51
    assert len(registry.for_project("soft")) == 178
    assert len(registry.for_project("earnings")) == 109
    assert len(registry.enabled_sources(kind="investor_relations")) == 24
    assert len(registry.enabled_sources(kind="bmv_issuer_pdf")) == 1
    primary_pdf_sources = (
        *registry.enabled_sources(kind="investor_relations"),
        *registry.enabled_sources(kind="bmv_issuer_pdf"),
    )
    assert sum(
        source.live_verified_period is not None
        for _issuer, source in primary_pdf_sources
    ) == 8

    tiendas_3b = registry.get("tiendas_3b")
    assert not tiendas_3b.is_member("alpha_go")
    assert not tiendas_3b.is_member("soft")
    assert not tiendas_3b.is_member("earnings")
    assert not tiendas_3b.source("bmv_xbrl").enabled
    assert tiendas_3b.source("ir").enabled

    bafar = registry.get("grupo_bafar")
    assert bafar.source("ir").floor_year == 2024
    assert bafar.source("ir").direct_url_templates == (
        "https://grupobafar.s3.us-east-1.amazonaws.com/CentroReportes/"
        "ReportesTrimestrales/{quarter}T{year2}/GB_PR_{quarter}T{year2}.pdf",
    )
    gnp = registry.get("gnp")
    gnp_source = gnp.source("bmv_issuer_pdf")
    assert gnp_source.coverage_from_period == "2026-2T"
    assert gnp_source.live_verified_period == "2026-2T"
    assert gnp_source.live_verified_on == date(2026, 7, 30)
    assert gnp_source.live_verified_url.endswith(
        "asginfin_1576481_2026-02_2.pdf"
    )
    gfnorte = registry.get("gfnorte")
    assert gfnorte.is_member("alpha_go")
    assert not gfnorte.source("bmv_xbrl").enabled
    assert gfnorte.source("ir").strict_pdf_link_pattern
    assert gfnorte.source("ir").live_verified_period == "2026-2T"
    assert registry.get("femsa").source("ir").live_verified_period == "2026-2T"
    assert registry.get("sports_world").source("ir").live_verified_period == "2026-2T"
    assert registry.get("qualitas").source("ir").live_verified_period == "2026-2T"
    assert registry.get("qualitas").source("ir").strict_pdf_link_pattern
    assert registry.get("tiendas_3b").source("ir").live_verified_period == "2026-1T"
    assert registry.get("walmex").source("ir").live_verified_period == "2026-2T"
    assert registry.get("walmex").source("ir").strict_pdf_link_pattern
    assert registry.get("liverpool").source("ir").live_verified_period == "2026-2T"


def test_project_memberships_match_committed_source_universes_exactly():
    registry = load_issuer_registry()

    alpha_raw = yaml.safe_load(
        (ROOT / "alpha-go" / "configs" / "bmv_corpus.yaml").read_text(encoding="utf-8")
    )
    alpha_tickers = {item["ticker"] for item in alpha_raw["companies"]}
    # The Alpha snapshot retains two legacy claves; the root registry uses the
    # current canonical BMV identities for those same issuers.
    alpha_tickers = {
        {"FIBRATC": "FNOVA", "LIVEPOL": "LIVERPOL"}.get(ticker, ticker)
        for ticker in alpha_tickers
    }
    # Alpha's BMV corpus omits its separately configured Banorte IR source.
    # The root registry canonicalizes that legacy `banorte` slug to GFNORTE.
    assert {issuer.ticker for issuer in registry.for_project("alpha_go")} == (
        alpha_tickers | {"GFNORTE"}
    )

    universe_tree = ast.parse(
        (ROOT / "soft" / "scripts" / "gen_universe.py").read_text(encoding="utf-8")
    )
    universe = None
    for node in universe_tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "UNIVERSE"
            for target in node.targets
        ):
            universe = ast.literal_eval(node.value)
            break
    assert universe is not None
    soft_slugs = {
        re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
        for _template, members in universe.values()
        for _ticker, name in members
    }
    assert {issuer.slug for issuer in registry.for_project("soft")} == soft_slugs

    earnings_raw = yaml.safe_load(
        (ROOT / "earnings" / "configs" / "universe_full.yaml").read_text(encoding="utf-8")
    )
    assert {issuer.slug for issuer in registry.for_project("earnings")} == set(
        earnings_raw["universe"]
    )


def test_root_ir_overlay_preserves_canonical_and_market_tickers():
    registry = load_issuer_registry()
    bimbo = registry.get("bimbo")

    assert bimbo.ticker == "BIMBO"
    assert bimbo.market_ticker == "BIMBOA"
    assert bimbo.name == "Grupo Bimbo"
    assert bimbo.language == "en"
    assert bimbo.source("ir").url == (
        "https://www.grupobimbo.com/en/investors/reports/quarterly-reports"
    )
    assert bimbo.source("bmv_xbrl").xbrl_ticker == "BIMBO"
    assert bimbo.filing_grace_days == 45
    assert registry.find("BIMBOA") is bimbo
    assert registry.find("LIVEPOL").slug == "liverpool"


def test_bimbo_xbrl_binding_matches_offline_bmv_archive_clave():
    from src.download.bmv_xbrl import parse_archive_index

    registry = load_issuer_registry()
    filings = parse_archive_index(
        (ROOT / "tests" / "data" / "bmv_archive_sample.html").read_text(
            encoding="utf-8"
        )
    )

    archive_tickers = {filing.ticker for filing in filings}
    assert registry.get("bimbo").source("bmv_xbrl").xbrl_ticker in archive_tickers
    penoles = registry.get("penoles")
    assert penoles.ticker == "PE&OLES"
    assert penoles.source("bmv_xbrl").xbrl_ticker == "PEOLES"
    assert penoles.source("bmv_xbrl").xbrl_ticker in archive_tickers


@pytest.mark.parametrize("duplicate_field", ["slug", "ticker"])
def test_registry_rejects_duplicate_slug_or_ticker(tmp_path, duplicate_field):
    second_slug = "one" if duplicate_field == "slug" else "two"
    second_ticker = "ONE" if duplicate_field == "ticker" else "TWO"
    path = tmp_path / "issuers.yaml"
    path.write_text(
        f"""
version: 1
defaults: {{}}
memberships: {{}}
groups:
  sample:
    template: industrial
    issuers:
      - {{slug: one, ticker: ONE, name: One}}
      - {{slug: {second_slug}, ticker: {second_ticker}, name: Two}}
overlays: {{}}
""",
        encoding="utf-8",
    )

    with pytest.raises(IssuerRegistryError, match=f"duplicate issuer {duplicate_field}"):
        load_issuer_registry(path)


def test_registry_parses_lifecycle_fields_without_assuming_history(tmp_path):
    path = tmp_path / "issuers.yaml"
    path.write_text(
        """
version: 1
defaults: {}
memberships:
  alpha_go: [new_company]
groups:
  sample:
    template: industrial
    issuers:
      - {slug: new_company, ticker: NEW, name: New Company}
overlays:
  new_company:
    active: false
    listed_from: 2020-02-03
    listed_to: 2024-09-30
    fiscal_year_end_month: 6
    filing_grace_days: 75
""",
        encoding="utf-8",
    )

    issuer = load_issuer_registry(path).get("new_company")
    assert issuer.is_member("alpha_go")
    assert not issuer.is_member("soft")
    assert not issuer.active
    assert issuer.listed_from == date(2020, 2, 3)
    assert issuer.listed_to == date(2024, 9, 30)
    assert issuer.fiscal_year_end_month == 6
    assert issuer.filing_grace_days == 75


def test_source_and_fetched_artifact_use_estate_contract_names():
    source = SourceRecord(
        source_key="issuer_ir",
        source_record_id="https://example.test/report.pdf",
        issuer_slug="gap",
        url="https://example.test/report.pdf",
        period_year=2025,
        period_quarter=2,
    )
    content = b"%PDF-1.7 test"
    artifact = FetchedArtifact(
        source=source,
        content=content,
        role="structured_facts",
        filename="2025-2T.pdf",
    )

    assert source.document_type == "quarterly_release"
    assert source.period == "2025-2T"
    assert source.document_family_id == (
        "gap:quarterly_release:2025-2T:und:default"
    )
    assert artifact.role == "structured_facts"
    assert artifact.size_bytes == len(content)
    assert artifact.sha256 == hashlib.sha256(content).hexdigest()

    with pytest.raises(ValueError, match="artifact role"):
        FetchedArtifact(source=source, content=content, role="not a role")


def test_primary_pdf_source_contract_is_fail_closed():
    with pytest.raises(ValueError, match="requires a URL"):
        AcquisitionSource(key="ir", kind="investor_relations")
    with pytest.raises(ValueError, match="requires period, date, and URL"):
        AcquisitionSource(
            key="ir",
            kind="investor_relations",
            url="https://example.test/reports",
            live_verified_period="2026-2T",
        )
    with pytest.raises(
        ValueError,
        match="strict_pdf_link_pattern requires pdf_link_pattern",
    ):
        AcquisitionSource(
            key="ir",
            kind="investor_relations",
            url="https://example.test/reports",
            strict_pdf_link_pattern=True,
        )
