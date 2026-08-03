from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sqlite3

import pytest

from src.acquisition.models import (
    AcquisitionSource,
    FetchedArtifact,
    IssuerSpec,
    SourceRecord,
)
from src.acquisition.readiness import PrimaryPdfReadinessRow
from src.acquisition.registry import IssuerRegistry, load_issuer_registry
from src.acquisition.source_onboarding import (
    BmvDirectorySeedProvider,
    DiscoveryOptions,
    DiscoverySeed,
    HttpPayload,
    PdfValidation,
    SourceOnboardingCompiler,
    SourceProposal,
    apply_registry_proposals,
    catalog_seeds,
    classify_link,
    extract_static_links,
    infer_url_templates,
    load_seed_file,
    parse_bmv_directory,
    parse_bmv_profile_website,
    render_registry_patch,
    select_production_canary_issuers,
    select_readiness_targets,
    verify_configured_production_source,
    verify_issuer_identity,
    verify_period_text,
)


def _issuer(slug: str = "alpha") -> IssuerSpec:
    return IssuerSpec(
        slug=slug,
        ticker="ALPHA",
        market_ticker="ALPHAB",
        name="Alpha Industrias",
        sector="industrial",
        template="industrial",
    )


def _configured_issuer() -> IssuerSpec:
    return IssuerSpec(
        slug="alpha",
        ticker="ALPHA",
        market_ticker="ALPHAB",
        name="Alpha Industrias",
        sector="industrial",
        template="industrial",
        sources=(
            AcquisitionSource(
                key="ir",
                kind="investor_relations",
                url="https://ir.example.test/reports",
            ),
        ),
    )


def _row(slug: str = "alpha", gaps: str = "primary_pdf_source_unconfigured"):
    return PrimaryPdfReadinessRow(
        issuer_slug=slug,
        ticker="ALPHA",
        name="Alpha Industrias",
        sector="industrial",
        active=True,
        listed_from=None,
        listed_to=None,
        source_state="unconfigured_without_coverage",
        source_keys="",
        source_kinds="",
        source_urls="",
        coverage_from_periods="",
        live_verified_periods="",
        live_verified_ons="",
        live_verified_urls="",
        known_period_count=0,
        latest_known_period=None,
        in_source_window_period_count=0,
        latest_in_source_window_period=None,
        readiness_gaps=gaps,
    )


def test_classification_keeps_quarterly_release_and_rejects_adjacent_assets():
    report = classify_link(
        "https://ir.example.test/files/2Q26-results.pdf",
        title="Second-quarter results",
        source_page="https://ir.example.test/reports",
    )
    annual = classify_link(
        "https://ir.example.test/files/annual-report-2026.pdf",
        title="Annual report 2026",
        source_page="https://ir.example.test/reports",
    )
    presentation = classify_link(
        "https://ir.example.test/files/2Q26-presentation.pdf",
        title="Results presentation",
        source_page="https://ir.example.test/reports",
    )
    human_readable_xbrl_pdf = classify_link(
        "https://www.elpuertodeliverpool.mx/docs/2TXBRL2026.pdf",
        title="Second quarter financial statements",
        source_page="https://www.elpuertodeliverpool.mx/trimestral.html",
    )
    mse_notice = classify_link(
        "https://ir.example.test/2_T14_Results_to_the_MSE_XBR.pdf",
        title="Read PDF",
        source_page="https://ir.example.test/reports",
    )

    assert report.period == "2026-2T"
    assert report.rejected_reason is None
    assert annual.rejected_reason == "negative_document_type"
    assert presentation.rejected_reason == "negative_document_type"
    assert human_readable_xbrl_pdf.period == "2026-2T"
    assert human_readable_xbrl_pdf.rejected_reason is None
    assert mse_notice.rejected_reason == "negative_document_type"


def test_static_link_extraction_supports_html_sitemap_and_embedded_urls():
    html_payload = HttpPayload(
        requested_url="https://ir.example.test/reports",
        final_url="https://ir.example.test/reports",
        status_code=200,
        content_type="text/html",
        content=b"""
        <a href='https://[malformed'>Ignore invalid IPv6-like link</a>
        <a href='/docs/1Q26-results.pdf'>First quarter results</a>
        <script>{"url":"https:\\/\\/cdn.example.test\\/2Q26-results.pdf"}</script>
        """,
    )
    assert extract_static_links(html_payload) == (
        ("https://cdn.example.test/2Q26-results.pdf", "embedded static URL"),
        ("https://ir.example.test/docs/1Q26-results.pdf", "First quarter results"),
    )

    sitemap = HttpPayload(
        requested_url="https://ir.example.test/sitemap.xml",
        final_url="https://ir.example.test/sitemap.xml",
        status_code=200,
        content_type="application/xml",
        content=b"<urlset><url><loc>https://ir.example.test/investors/results</loc></url></urlset>",
    )
    assert extract_static_links(sitemap) == (
        ("https://ir.example.test/investors/results", "sitemap"),
    )


def test_bmv_directory_and_profile_parsers_require_exact_ticker_identity():
    directory = parse_bmv_directory(
        b'''for(;;);({"response":{"resultado":[
          {"claveEmisora":"ALPHA","idEmisora":5057,"razonSocial":"ALPHA SA"},
          {"claveEmisora":"","idEmisora":1},
          {"claveEmisora":"BROKEN","idEmisora":"not-an-int"}
        ]}})'''
    )
    assert directory == (
        {"ticker": "ALPHA", "issuer_id": 5057, "legal_name": "ALPHA SA"},
    )

    profile = b"""
      <table>
        <tr><td>Clave:</td><td>ALPHA</td></tr>
        <tr><td>Web:</td><td><a href='http://www.alpha.test'>www.alpha.test</a></td></tr>
      </table>
    """
    assert parse_bmv_profile_website(profile, expected_tickers=("ALPHA",)) == (
        "ALPHA",
        "http://www.alpha.test/",
    )
    with pytest.raises(ValueError, match="ticker mismatch"):
        parse_bmv_profile_website(profile, expected_tickers=("OTHER",))


class FakeBmvClient:
    def __init__(self):
        self.directory_calls = 0
        self.profile_calls = 0

    def get_with_headers(self, url, *, max_bytes, headers):
        self.directory_calls += 1
        assert headers["X-Requested-With"] == "XMLHttpRequest"
        return HttpPayload(
            url,
            url,
            200,
            "application/json",
            b'''for(;;);({"response":{"resultado":[
              {"claveEmisora":"ALPHA","idEmisora":5057,"razonSocial":"ALPHA SA"}
            ]}})''',
        )

    def get(self, url, *, max_bytes):
        self.profile_calls += 1
        assert url.endswith("/ALPHA-5057")
        return HttpPayload(
            url,
            url,
            200,
            "text/html",
            b"""
            <table>
              <tr><td>Clave:</td><td>ALPHA</td></tr>
              <tr><td>Web:</td><td><a href='https://www.alpha.test'>Alpha</a></td></tr>
            </table>
            """,
        )


def test_bmv_seed_provider_fetches_one_directory_and_caches_profile(tmp_path):
    client = FakeBmvClient()
    provider = BmvDirectorySeedProvider(
        client=client,
        cache_dir=tmp_path / "cache",
        clock=lambda: 1000,
    )

    first = provider.resolve(_issuer())
    second = provider.resolve(_issuer())

    assert first.status == "ready"
    assert first.website_url == "https://www.alpha.test/"
    assert first.profile_url.endswith("/ALPHA-5057")
    assert first.seed == DiscoverySeed(
        "https://www.alpha.test/",
        f"bmv_profile:{first.profile_url}",
    )
    assert not first.cache_hit
    assert second.cache_hit
    assert client.directory_calls == 1
    assert client.profile_calls == 1


def test_pdf_identity_and_period_checks_require_document_evidence():
    issuer = _issuer()
    text = "Alpha Industrias (ALPHAB) Results for the Second Quarter 2026"

    verified, terms = verify_issuer_identity(issuer, text)
    assert verified
    assert "alphab" in terms
    assert verify_period_text("2026-2T", text)
    assert not verify_period_text("2026-3T", text)
    assert not verify_issuer_identity(issuer, "Unrelated Holdings Q2 2026")[0]

    walmex = IssuerSpec(
        slug="walmex",
        ticker="WALMEX",
        name="Walmex",
        sector="retail",
        template="retail",
    )
    composite_verified, composite_terms = verify_issuer_identity(
        walmex,
        "Walmart de Mexico y Centroamerica second quarter 2026",
    )
    assert composite_verified
    assert "walmex~wal+mex" in composite_terms


def test_template_inference_requires_two_exact_verified_periods():
    def validation(period: str, url: str) -> PdfValidation:
        return PdfValidation(
            url=url,
            final_url=url,
            advertised_period=period,
            valid_pdf=True,
            issuer_verified=True,
            period_verified=True,
            size_bytes=10_000,
            sha256="a" * 64,
            content_type="application/pdf",
        )

    one = validation("2026-1T", "https://ir.example.test/1Q26-results.pdf")
    two = validation("2026-2T", "https://ir.example.test/2Q26-results.pdf")

    assert infer_url_templates((one,)) == ()
    assert infer_url_templates((one, two)) == (
        "https://ir.example.test/{quarter}Q{year2}-results.pdf",
    )


class FakeClient:
    def __init__(self):
        self.calls: list[str] = []

    def get(self, url: str, *, max_bytes: int) -> HttpPayload:
        self.calls.append(url)
        if url.endswith("sitemap.xml"):
            return HttpPayload(url, url, 200, "application/xml", b"<urlset />")
        if url.endswith("reports"):
            return HttpPayload(
                url,
                url,
                200,
                "text/html",
                b"""
                <a href='/docs/1Q26-results.pdf'>Alpha first quarter results 2026</a>
                <a href='/docs/2Q26-results.pdf'>Alpha second quarter results 2026</a>
                <a href='/docs/2Q26-presentation.pdf'>Alpha Q2 presentation</a>
                """,
            )
        if url.endswith("1Q26-results.pdf") or url.endswith("2Q26-results.pdf"):
            return HttpPayload(
                url,
                url,
                200,
                "application/pdf",
                b"%PDF-" + b"x" * 100,
            )
        raise RuntimeError(f"unexpected URL: {url}")


class LinkedPageClient(FakeClient):
    def get(self, url: str, *, max_bytes: int) -> HttpPayload:
        self.calls.append(url)
        if url.endswith("sitemap.xml"):
            return HttpPayload(
                url,
                url,
                200,
                "application/xml",
                b"<sitemapindex><sitemap><loc>https://ir.example.test/post-sitemap.xml</loc></sitemap></sitemapindex>",
            )
        if url.endswith("post-sitemap.xml"):
            return HttpPayload(
                url,
                url,
                200,
                "application/xml",
                b"<urlset><url><loc>https://ir.example.test/investors/financial-results</loc></url></urlset>",
            )
        if url == "https://ir.example.test/":
            return HttpPayload(
                url,
                url,
                200,
                "text/html",
                b"<a href='/investors/results'>Investor results</a>",
            )
        if url in {
            "https://ir.example.test/investors/results",
            "https://ir.example.test/investors/financial-results",
        }:
            return HttpPayload(
                url,
                url,
                200,
                "text/html",
                b"<a href='/docs/2Q26-results.pdf'>Alpha Q2 2026 results</a>",
            )
        if url.endswith("2Q26-results.pdf"):
            return HttpPayload(url, url, 200, "application/pdf", b"%PDF-" + b"x" * 100)
        raise RuntimeError(f"unexpected URL: {url}")


def test_compiler_is_cached_deterministic_and_never_writes_estate(
    tmp_path, monkeypatch
):
    client = FakeClient()
    monkeypatch.setattr(
        "src.acquisition.source_onboarding.extract_pdf_identity_text",
        lambda _content: (
            "Alpha Industrias ALPHAB first quarter 2026 second quarter 2026"
        ),
    )
    compiler = SourceOnboardingCompiler(
        client=client,
        options=DiscoveryOptions(min_pdf_bytes=5),
        cache_dir=tmp_path / "cache",
        clock=lambda: 1000,
    )

    first = compiler.compile(
        _issuer(),
        readiness=_row(),
        seeds=(DiscoverySeed("https://ir.example.test/reports", "seed_file"),),
        verified_on=date(2026, 8, 1),
    )
    call_count = len(client.calls)
    second = compiler.compile(
        _issuer(),
        readiness=_row(),
        seeds=(DiscoverySeed("https://ir.example.test/reports", "seed_file"),),
        verified_on=date(2026, 8, 1),
    )

    assert first.status == "ready_for_review"
    assert first.confidence == "high"
    assert first.patch_eligible
    assert len([item for item in first.validations if item.fully_verified]) == 2
    assert first.inferred_templates == (
        "https://ir.example.test/docs/{quarter}Q{year2}-results.pdf",
    )
    assert first.proposed_ir["live_verified_period"] == "2026-2T"
    assert not first.cache_hit
    assert second.cache_hit
    assert len(client.calls) == call_count
    assert not (tmp_path / "estate").exists()


def test_compiler_follows_static_report_pages_and_nested_sitemap(tmp_path, monkeypatch):
    client = LinkedPageClient()
    monkeypatch.setattr(
        "src.acquisition.source_onboarding.extract_pdf_identity_text",
        lambda _content: "Alpha Industrias ALPHAB second quarter 2026",
    )
    compiler = SourceOnboardingCompiler(
        client=client,
        options=DiscoveryOptions(max_pages=8, sample_pdfs=1, min_pdf_bytes=5),
    )

    proposal = compiler.compile(
        _issuer(),
        seeds=(DiscoverySeed("https://ir.example.test/", "bmv_profile"),),
        refresh=True,
    )

    assert proposal.status == "needs_second_sample"
    assert proposal.validations[0].fully_verified
    assert "https://ir.example.test/investors/results" in client.calls
    assert "https://ir.example.test/post-sitemap.xml" in client.calls


def test_verification_dir_receives_only_identity_and_period_verified_pdf(
    tmp_path, monkeypatch
):
    client = FakeClient()
    monkeypatch.setattr(
        "src.acquisition.source_onboarding.extract_pdf_identity_text",
        lambda _content: "Alpha Industrias ALPHAB second quarter 2026",
    )
    compiler = SourceOnboardingCompiler(
        client=client,
        options=DiscoveryOptions(sample_pdfs=1, min_pdf_bytes=5),
        verification_dir=tmp_path / "canary",
    )

    proposal = compiler.compile(
        _issuer(),
        seeds=(DiscoverySeed("https://ir.example.test/reports", "seed_file"),),
        refresh=True,
        verified_on=date(2026, 8, 1),
    )

    assert len(proposal.validations) == 1
    validation = proposal.validations[0]
    assert validation.advertised_period == "2026-2T"
    assert validation.fully_verified
    assert validation.local_path == str(
        (tmp_path / "canary" / "alpha" / "2026-2T.pdf").resolve()
    )
    assert Path(validation.local_path).read_bytes().startswith(b"%PDF-")


def test_irrelevant_bmv_profile_website_cannot_produce_a_registry_proposal(
    monkeypatch,
):
    client = FakeClient()
    monkeypatch.setattr(
        "src.acquisition.source_onboarding.extract_pdf_identity_text",
        lambda _content: "Unrelated Trustee Holdings second quarter 2026",
    )
    compiler = SourceOnboardingCompiler(
        client=client,
        options=DiscoveryOptions(sample_pdfs=1, min_pdf_bytes=5),
    )

    proposal = compiler.compile(
        _issuer(),
        seeds=(
            DiscoverySeed(
                "https://ir.example.test/reports",
                "bmv_profile:https://www.bmv.com.mx/es/emisoras/perfil/ALPHA-1",
            ),
        ),
        refresh=True,
    )

    assert proposal.status == "validation_failed"
    assert not proposal.patch_eligible
    assert not proposal.proposed_ir
    assert proposal.validations[0].valid_pdf
    assert not proposal.validations[0].issuer_verified


class FakeProductionIrAdapter:
    def __init__(self, period: str):
        self.period = period
        self.calls = []

    def fetch_incremental_report(
        self,
        issuer,
        source,
        staging_dir,
        *,
        known_periods,
        recheck_periods,
        desired_periods,
    ):
        from types import SimpleNamespace

        self.calls.append((known_periods, recheck_periods, desired_periods))
        year, quarter = self.period.split("-")
        record = SourceRecord(
            source_key=source.key,
            source_record_id=f"https://ir.example.test/{self.period}.pdf",
            issuer_slug=issuer.slug,
            url=f"https://ir.example.test/{self.period}.pdf",
            period_year=int(year),
            period_quarter=int(quarter[0]),
        )
        artifact = FetchedArtifact(
            source=record,
            content=b"%PDF-" + b"x" * 100,
            role="original",
            filename=f"{self.period}.pdf",
            media_type="application/pdf",
        )
        return SimpleNamespace(
            artifacts=(artifact,),
            candidate_periods=(self.period,),
            layers=(),
            issues=(),
        )


def test_production_canary_uses_configured_adapter_and_exact_period(
    tmp_path, monkeypatch
):
    adapter = FakeProductionIrAdapter("2026-2T")
    monkeypatch.setattr(
        "src.acquisition.source_onboarding.extract_pdf_identity_text",
        lambda _content: "Alpha Industrias ALPHAB second quarter 2026",
    )

    result = verify_configured_production_source(
        _configured_issuer(),
        expected_period="2026-2T",
        staging_dir=tmp_path / "canary",
        attempts=1,
        min_pdf_bytes=5,
        ir_adapter=adapter,
    )

    assert result.passed
    assert result.selected_period == "2026-2T"
    assert result.final_url == "https://ir.example.test/2026-2T.pdf"
    assert Path(result.local_path).is_file()
    known, recheck, desired = adapter.calls[0]
    assert "2025-4T" in known
    assert recheck == {"2026-2T"}
    assert desired == {"2026-2T"}


def test_production_canary_rejects_stale_but_valid_pdf(tmp_path, monkeypatch):
    adapter = FakeProductionIrAdapter("2025-4T")
    monkeypatch.setattr(
        "src.acquisition.source_onboarding.extract_pdf_identity_text",
        lambda _content: "Alpha Industrias ALPHAB fourth quarter 2025",
    )

    result = verify_configured_production_source(
        _configured_issuer(),
        expected_period="2026-2T",
        staging_dir=tmp_path / "canary",
        attempts=2,
        min_pdf_bytes=5,
        ir_adapter=adapter,
        sleeper=lambda _seconds: None,
    )

    assert not result.passed
    assert result.status == "expected_period_not_found"
    assert result.selected_period == "2025-4T"
    assert result.local_path is None
    assert result.attempts == 2
    assert not (tmp_path / "canary").exists()


def test_production_canary_caps_real_ir_downloader_after_period_exclusions(tmp_path):
    from src.acquisition.adapters import InvestorRelationsAdapter

    observed = {}

    def downloader(_url, _output_dir, **kwargs):
        observed.update(kwargs)
        return []

    adapter = InvestorRelationsAdapter(
        downloader=downloader,
        template_downloader=lambda *_args, **_kwargs: [],
    )
    result = verify_configured_production_source(
        _configured_issuer(),
        expected_period="2026-2T",
        staging_dir=tmp_path / "canary",
        attempts=1,
        ir_adapter=adapter,
    )

    assert not result.passed
    assert result.status == "expected_period_not_found"
    assert observed["max_reports"] == 1
    assert observed["desired_periods"] == {"2026-2T"}


def test_production_canary_uses_configured_exact_verified_url_without_index(
    tmp_path, monkeypatch
):
    from src.acquisition.adapters import InvestorRelationsAdapter
    from src.download.downloader import DownloadedPdf

    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://slow.example.test/reports",
        live_verified_period="2026-2T",
        live_verified_on=date(2026, 8, 1),
        live_verified_url="https://cdn.example.test/exact-2Q26.pdf",
    )
    issuer = IssuerSpec(
        slug="alpha",
        ticker="ALPHA",
        name="Alpha Industrias",
        sector="test",
        template="generic",
        sources=(source,),
    )

    def forbidden_index(*_args, **_kwargs):
        raise AssertionError("the slow index must not be called")

    def template_downloader(templates, output_dir, periods, **kwargs):
        assert templates == [source.live_verified_url]
        assert periods == {"2026-2T"}
        path = output_dir / "2026-2T.pdf"
        path.write_bytes(b"%PDF-" + b"x" * 100)
        kwargs["detail_sink"].append(
            DownloadedPdf(
                url=source.live_verified_url,
                path=path,
                filename=path.name,
                period="2026-2T",
            )
        )
        return [path]

    monkeypatch.setattr(
        "src.acquisition.source_onboarding.extract_pdf_identity_text",
        lambda _content: "Alpha Industrias second quarter 2026",
    )
    result = verify_configured_production_source(
        issuer,
        expected_period="2026-2T",
        staging_dir=tmp_path / "canary",
        attempts=1,
        min_pdf_bytes=5,
        ir_adapter=InvestorRelationsAdapter(
            downloader=forbidden_index,
            template_downloader=template_downloader,
        ),
    )

    assert result.passed
    assert result.final_url == source.live_verified_url


def test_catalog_seeding_is_read_only_and_excludes_xbrl_payloads(tmp_path):
    database = tmp_path / "catalog.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            company TEXT,
            doc_type TEXT,
            source_url TEXT
        );
        CREATE TABLE memberships (document_id TEXT, company TEXT);
        INSERT INTO documents VALUES
          ('pdf', 'alpha', 'quarterly_release', 'https://ir.example.test/2Q26-results.pdf'),
          ('xbrl', 'alpha', 'quarterly_release', 'https://bmv.test/ifrsxbrl_2Q26.zip');
        """
    )
    connection.commit()
    connection.close()

    before = database.read_bytes()
    assert catalog_seeds(database, "alpha") == (
        DiscoverySeed(
            "https://ir.example.test/2Q26-results.pdf",
            "catalog_primary_pdf",
        ),
    )
    assert database.read_bytes() == before


def _proposal() -> SourceProposal:
    return SourceProposal(
        issuer_slug="alpha",
        ticker="ALPHA",
        issuer_name="Alpha Industrias",
        readiness_gaps=("primary_pdf_source_unconfigured",),
        status="ready_for_review",
        confidence="high",
        cache_hit=False,
        proposed_ir={
            "url": "https://ir.example.test/reports",
            "pdf_link_pattern": r"(?i)results.*\.pdf$",
            "strict_pdf_link_pattern": True,
            "use_playwright": False,
            "coverage_from_period": "2026-1T",
            "live_verified_period": "2026-2T",
            "live_verified_on": "2026-08-01",
            "live_verified_url": "https://ir.example.test/2Q26-results.pdf",
        },
    )


def test_registry_patch_is_reviewable_atomic_and_idempotent(tmp_path):
    registry_path = tmp_path / "issuers.yaml"
    registry_path.write_text(
        """# keep this comment
version: 1
defaults: {}
memberships: {}
groups:
  industrial:
    template: industrial
    issuers:
      - {slug: alpha, ticker: ALPHA, name: Alpha Industrias}
overlays: {}
""",
        encoding="utf-8",
    )

    patch = render_registry_patch(registry_path, (_proposal(),))
    assert "+  alpha:" in patch
    assert "+    ir:" in patch
    assert apply_registry_proposals(registry_path, (_proposal(),)) == ("alpha",)
    assert apply_registry_proposals(registry_path, (_proposal(),)) == ()
    assert registry_path.read_text(encoding="utf-8").startswith("# keep this comment")
    source = load_issuer_registry(registry_path).get("alpha").source("ir")
    assert source.live_verified_period == "2026-2T"


def test_seed_file_and_readiness_target_selection_are_explicit(tmp_path):
    path = tmp_path / "seeds.yaml"
    path.write_text(
        "issuers:\n  alpha: https://ir.example.test/reports\n",
        encoding="utf-8",
    )
    assert load_seed_file(path)["alpha"][0].url == "https://ir.example.test/reports"

    registry = IssuerRegistry(1, (_issuer("alpha"),))
    assert select_readiness_targets(registry, (_row("alpha"),))[0][0].slug == "alpha"
    with pytest.raises(KeyError, match="unknown issuer"):
        select_readiness_targets(registry, (_row("alpha"),), issuer_slugs=("missing",))


def test_configured_canary_random_sample_is_reproducible_and_excludes_unconfigured():
    def configured(slug: str, ticker: str) -> IssuerSpec:
        return IssuerSpec(
            slug=slug,
            ticker=ticker,
            name=slug.title(),
            sector="test",
            template="generic",
            sources=(
                AcquisitionSource(
                    key="ir",
                    kind="investor_relations",
                    url=f"https://{slug}.example.test/reports",
                ),
            ),
        )

    registry = IssuerRegistry(
        1,
        (
            configured("one", "ONE"),
            _issuer("unconfigured"),
            configured("two", "TWO"),
            configured("three", "THREE"),
        ),
    )

    first = select_production_canary_issuers(
        registry,
        random_sample=2,
        sample_seed=77,
    )
    second = select_production_canary_issuers(
        registry,
        random_sample=2,
        sample_seed=77,
    )
    assert tuple(item.slug for item in first) == tuple(item.slug for item in second)
    assert len(first) == 2
    assert all(item.slug != "unconfigured" for item in first)
    assert select_production_canary_issuers(
        registry,
        identifiers=("TWO",),
    )[0].slug == "two"


def test_recorded_walmex_selector_keeps_release_and_rejects_adjacent_assets():
    from src.acquisition.registry import load_issuer_registry
    from src.download.downloader import _extract_pdf_links

    source = load_issuer_registry().get("walmex").source("ir")
    release = (
        "https://files.walmex.mx/upload/files/2026/EN/Quarterly/2Q26/"
        "Walmex%202Q26%20Earnings%20Release.pdf"
    )
    mse = (
        "https://files.walmex.mx/upload/files/2026/EN/Quarterly/2Q26/"
        "WALMEX%202Q26%20MSE.pdf"
    )
    infographic = (
        "https://files.walmex.mx/upload/files/2026/EN/Quarterly/2Q26/"
        "Walmex%202Q26%20Infographic.pdf"
    )
    html = "".join(
        (
            f'<a href="{release}">Release</a>',
            f'<a href="{mse}">Information to the MSE</a>',
            f'<a href="{infographic}">Infographic</a>',
        )
    )

    assert source.strict_pdf_link_pattern
    assert _extract_pdf_links(
        html,
        source.url,
        source.pdf_link_pattern,
        strict_file_pattern=source.strict_pdf_link_pattern,
    ) == [release]


def test_cached_proposal_round_trip_retains_nested_tuples():
    raw = _proposal().to_dict()
    raw["candidates"] = [
        {
            "url": "https://ir.example.test/2Q26-results.pdf",
            "title": "Q2 results",
            "source_page": "https://ir.example.test/reports",
            "period": "2026-2T",
            "score": 100,
            "reasons": ["canonical_quarter_detected"],
            "rejected_reason": None,
        }
    ]
    decoded = SourceProposal.from_dict(json.loads(json.dumps(raw)), cache_hit=True)

    assert decoded.cache_hit
    assert decoded.candidates[0].reasons == ("canonical_quarter_detected",)
