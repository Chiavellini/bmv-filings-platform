"""Offline contract tests for the root quarterly-acquisition engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
import sqlite3

from src.acquisition.adapters import (
    BmvXbrlAdapter,
    IRLayerDiagnostic,
    InvestorRelationsAdapter,
    normalize_period,
)
from src.acquisition.models import (
    AcquisitionSource,
    FetchedArtifact,
    IssuerSpec,
    ProjectMembership,
    SourceRecord,
)
from src.acquisition.registry import IssuerRegistry
from src.acquisition.service import (
    CoverageGap,
    EstateCoverage,
    PDF_DOCUMENT_TYPE,
    QuarterlyAcquisitionService,
    RunReport,
    SourceDiagnostic,
    SyncFailure,
    XBRL_DOCUMENT_TYPE,
    expected_quarterly_periods,
)
from src.acquisition import cli as acquisition_cli
from src.acquisition.cli import _print_report, main as acquisition_main
from src.acquisition.ledger import AcquisitionLeaseLost, AcquisitionLedger
from src.acquisition.writer import ArtifactValidationError, EstateWriter


class FakeCoverage:
    def __init__(self, values=None):
        self.values = values or {}

    def known_periods(self, issuer_slug, document_type):
        return set(self.values.get((issuer_slug, document_type), ()))


class FakeLedger:
    def __init__(self):
        self.started = []
        self.discovered = []
        self.retryable = []
        self.rejected = []
        self.finished = []

    def start_run(self, **kwargs):
        self.started.append(kwargs)
        return "run-1"

    def discover(self, record, *, run_id=None):
        self.discovered.append((run_id, record))

    def mark_retryable(self, record, *, run_id, error, attempt_id=None):
        self.retryable.append((run_id, record, error))

    def mark_rejected(self, record, *, run_id, reason, attempt_id=None):
        self.rejected.append((run_id, record, reason))

    def finish_run(self, run_id, *, status="succeeded", error=None):
        self.finished.append((run_id, status, error))

    def renew_lease(self, run_id, **_kwargs):
        return "2099-01-01T00:00:00+00:00"


class FakeWriter:
    def __init__(self, *, created=True):
        self.created = created
        self.calls = []

    def store_fetched(self, artifact, *, run_id=None, memberships=()):
        self.calls.append((artifact, run_id, tuple(memberships)))
        return SimpleNamespace(created=self.created)


class NoIr:
    def fetch_incremental(self, *_args, **_kwargs):
        return ()


class NoWayback:
    def __init__(self):
        self.calls = 0

    def fetch_missing(self, *_args, **_kwargs):
        self.calls += 1
        return ()


def issuer(
    slug: str,
    ticker: str,
    *sources: AcquisitionSource,
    active: bool = True,
    listed_from: date | None = None,
    listed_to: date | None = None,
    grace: int = 0,
    fiscal_year_end_month: int = 12,
) -> IssuerSpec:
    return IssuerSpec(
        slug=slug,
        ticker=ticker,
        name=slug.title(),
        sector="test",
        template="generic",
        sources=tuple(sources),
        active=active,
        listed_from=listed_from,
        listed_to=listed_to,
        filing_grace_days=grace,
        fiscal_year_end_month=fiscal_year_end_month,
    )


def artifact(slug: str, source_key: str, period: str) -> FetchedArtifact:
    year, quarter = period.split("-")
    return FetchedArtifact(
        source=SourceRecord(
            source_key=source_key,
            source_record_id=f"{slug}:{period}",
            issuer_slug=slug,
            document_type=PDF_DOCUMENT_TYPE,
            period_year=int(year),
            period_quarter=int(quarter[0]),
        ),
        content=b"%PDF-1.4\nfixture\n%%EOF",
        filename=f"{period}.pdf",
    )


def test_period_normalization_and_fiscal_expected_periods():
    assert normalize_period("2025-Q2") == "2025-2T"
    assert normalize_period("2025-2T") == "2025-2T"
    assert expected_quarterly_periods(2025, date(2025, 7, 1)) == {
        "2025-1T",
        "2025-2T",
    }
    # June year-end: FY2025 Q4 ends in June 2025, while FY2026 Q1 has not ended.
    assert expected_quarterly_periods(
        2025,
        date(2025, 7, 1),
        fiscal_year_end_month=6,
    ) == {"2025-1T", "2025-2T", "2025-3T", "2025-4T"}


def test_plan_respects_active_listing_and_grace_without_blocking_recheck():
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2024,
    )
    active = issuer(
        "active",
        "ACTIVE",
        source,
        listed_from=date(2024, 7, 1),
        grace=45,
    )
    inactive = issuer("inactive", "INACTIVE", source, active=False)
    registry = IssuerRegistry(version=1, issuers=(active, inactive))
    service = QuarterlyAcquisitionService(
        registry,
        coverage=FakeCoverage(),
        recheck_periods=2,
    )

    plans = service.plan(as_of=date(2025, 7, 15))

    assert [plan.issuer.slug for plan in plans] == ["active"]
    planned = plans[0].source("ir")
    # Q2 ended but is not due until the grace window passes.
    assert "2025-2T" not in planned.missing_periods
    # It is nevertheless checked so an early filing is ingested.
    assert "2025-2T" in planned.recheck_periods
    assert "2024-1T" not in planned.missing_periods


def test_bmv_archive_is_loaded_once_and_each_fetch_is_exact(tmp_path):
    @dataclass(frozen=True)
    class Filing:
        ticker: str
        razon_social: str
        filed_date: str
        period: str
        kind: str
        zip_url: str

    archive_calls = []
    fetch_calls = []
    now = [0.0]
    sleeps = []
    rows = [
        Filing(
            "AAA", "A", "01/05/2025", "2025-1T", "quarterly", "https://bmv/a-old.zip"
        ),
        Filing("AAA", "A", "03/05/2025", "2025-1T", "quarterly", "https://bmv/a.zip"),
        Filing("BBB", "B", "02/05/2025", "2025-1T", "quarterly", "https://bmv/b.zip"),
    ]

    def load_archive():
        archive_calls.append(True)
        return rows

    def fetch_exact(ticker, *, out_dir, filings, **_kwargs):
        assert len(filings) == 1
        fetch_calls.append((ticker, filings[0].zip_url))
        path = Path(out_dir) / "xbrl" / f"{ticker}_2025-1T.json.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"compressed-json-fixture")
        return [path]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    bmv = BmvXbrlAdapter(
        archive_loader=load_archive,
        filing_fetcher=fetch_exact,
        min_interval_ms=1000,
        clock=lambda: now[0],
        sleeper=sleep,
    )
    source_a = AcquisitionSource(
        key="bmv_xbrl", kind="bmv_xbrl", xbrl_ticker="AAA", floor_year=2025
    )
    source_b = AcquisitionSource(
        key="bmv_xbrl", kind="bmv_xbrl", xbrl_ticker="BBB", floor_year=2025
    )
    registry = IssuerRegistry(
        version=1,
        issuers=(issuer("aaa", "AAA", source_a), issuer("bbb", "BBB", source_b)),
    )
    ledger = FakeLedger()
    writer = FakeWriter()
    service = QuarterlyAcquisitionService(
        registry,
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=writer,
        bmv=bmv,
        ir=NoIr(),
        wayback=NoWayback(),
    )

    report = service.sync(as_of=date(2025, 4, 1))

    assert report.ok
    assert len(archive_calls) == 1
    assert fetch_calls == [("AAA", "https://bmv/a.zip"), ("BBB", "https://bmv/b.zip")]
    assert sleeps == [1.0]
    assert [call[0].role for call in writer.calls] == ["raw_xbrl", "raw_xbrl"]
    assert [record.source_record_id for _, record in ledger.discovered] == [
        "AAA:quarterly:2025-1T",
        "BBB:quarterly:2025-1T",
    ]


def test_bmv_discovery_matches_punctuation_variant_but_preserves_native_fetch_ticker(
    tmp_path,
):
    filing = SimpleNamespace(
        ticker="PE&OLES",
        razon_social="Industrias Peñoles",
        filed_date="01/05/2025",
        period="2025-1T",
        kind="quarterly",
        zip_url="https://bmv.test/penoles.zip",
    )
    fetched = []

    def fetch_exact(ticker, *, out_dir, filings, **_kwargs):
        fetched.append((ticker, filings[0].ticker))
        path = Path(out_dir) / "PEOLES_2025-1T.json"
        path.write_text("{}", encoding="utf-8")
        return [path]

    adapter = BmvXbrlAdapter(
        archive_loader=lambda: [filing],
        filing_fetcher=fetch_exact,
        min_interval_ms=0,
    )
    source = AcquisitionSource(
        key="bmv_xbrl",
        kind="bmv_xbrl",
        xbrl_ticker="PEOLES",
        floor_year=2025,
    )
    company = issuer("penoles", "PE&OLES", source)

    discovered = adapter.discover(
        company,
        source,
        wanted_periods={"2025-1T"},
    )

    assert len(discovered) == 1
    assert discovered[0].record.source_record_id == "PEOLES:quarterly:2025-1T"
    assert discovered[0].record.metadata["archive_ticker"] == "PE&OLES"
    adapter.fetch(discovered[0], tmp_path, source=source)
    assert fetched == [("PE&OLES", "PE&OLES")]


def test_ir_sync_excludes_known_periods_but_rechecks_latest(tmp_path):
    captured = {}

    def download(_url, output_dir, **kwargs):
        captured.update(kwargs)
        path = Path(output_dir) / "2025-1T.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\nnew version\n%%EOF")
        kwargs["detail_sink"].append(
            SimpleNamespace(
                url="https://issuer.test/2025-1T.pdf",
                path=path,
                filename=path.name,
                period="2025-1T",
            )
        )
        return [path]

    ir = InvestorRelationsAdapter(
        downloader=download,
        template_downloader=lambda *_args, **_kwargs: [],
    )
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2024,
        delay_ms=0,
    )
    company = issuer("issuer", "ISSUER", source)
    coverage = FakeCoverage(
        {
            ("issuer", PDF_DOCUMENT_TYPE): {
                "2024-1T",
                "2024-2T",
                "2024-3T",
                "2024-4T",
                "2025-1T",
            }
        }
    )
    writer = FakeWriter()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (company,)),
        coverage=coverage,
        ledger=FakeLedger(),
        writer=writer,
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=ir,
        wayback=NoWayback(),
        recheck_periods=1,
    )

    report = service.sync(as_of=date(2025, 4, 1))

    assert report.ok
    assert captured["exclude_periods"] == {
        "2024-1T",
        "2024-2T",
        "2024-3T",
        "2024-4T",
    }
    assert len(writer.calls) == 1
    assert writer.calls[0][0].source.url == "https://issuer.test/2025-1T.pdf"


def test_ir_page_failure_still_attempts_deterministic_period_templates(tmp_path):
    template_calls = []

    def blocked_page(*_args, **_kwargs):
        raise RuntimeError("HTTP 403 Forbidden")

    def templates(configured, output_dir, periods, **kwargs):
        template_calls.append((tuple(configured), set(periods), kwargs))
        paths = []
        for period in sorted(periods):
            path = Path(output_dir) / f"{period}.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"%PDF-1.7 template {period}".encode())
            paths.append(path)
        return paths

    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        direct_url_templates=(
            "https://issuer.test/{year}/results-{quarter}T.pdf",
        ),
        delay_ms=0,
        impersonate="safari",
    )
    adapter = InvestorRelationsAdapter(
        downloader=blocked_page,
        template_downloader=templates,
    )

    report = adapter.fetch_incremental_report(
        issuer("issuer", "ISSUER", source),
        source,
        tmp_path,
        known_periods=set(),
        recheck_periods=set(),
        desired_periods={"2025-1T", "2025-2T"},
    )

    assert template_calls[0][1] == {"2025-1T", "2025-2T"}
    assert template_calls[0][2]["impersonate"] == "safari"
    assert [artifact.source.period for artifact in report.artifacts] == [
        "2025-1T",
        "2025-2T",
    ]
    assert all(
        artifact.source.metadata["adapter"] == "ir_url_template"
        for artifact in report.artifacts
    )
    assert len(report.issues) == 1
    assert report.issues[0].retryable is True
    assert report.issues[0].record.url == source.url
    assert "HTTP 403 Forbidden" in report.issues[0].error
    assert report.layers[-1].layer == "primary_page"
    assert "HTTP 403 Forbidden" in report.layers[-1].note


def test_ir_sync_stores_template_success_while_recording_page_failure(tmp_path):
    def blocked_page(*_args, **_kwargs):
        raise PermissionError("403 from investor-relations index")

    def templates(_configured, output_dir, periods, **_kwargs):
        path = Path(output_dir) / f"{sorted(periods)[-1]}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7 deterministic fallback")
        return [path]

    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
        direct_url_templates=(
            "https://issuer.test/{year}/results-{quarter}T.pdf",
        ),
        delay_ms=0,
    )
    ledger = FakeLedger()
    writer = FakeWriter()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (issuer("issuer", "ISSUER", source),)),
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=writer,
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=InvestorRelationsAdapter(
            downloader=blocked_page,
            template_downloader=templates,
        ),
        wayback=NoWayback(),
        default_floor_year=2025,
        recheck_periods=1,
    )

    report = service.sync(as_of=date(2025, 4, 1))

    assert report.stored == 1
    assert writer.calls[0][0].source.period == "2025-1T"
    assert len(ledger.retryable) == 1
    assert ledger.retryable[0][1].url == source.url
    assert "403 from investor-relations index" in ledger.retryable[0][2]
    assert report.diagnostics[-1].layer == "primary_page"
    assert report.exit_code == 1
    assert ledger.finished[-1][1] == "partial"


def test_template_layer_exception_preserves_primary_and_partial_template_pdfs(
    tmp_path,
):
    def primary(_url, output_dir, **kwargs):
        path = Path(output_dir) / "2025-1T.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7 primary success")
        kwargs["detail_sink"].append(SimpleNamespace(
            url="https://issuer.test/2025-1T.pdf",
            path=path,
            filename=path.name,
            period="2025-1T",
        ))
        return [path]

    def failing_templates(_configured, output_dir, periods, **kwargs):
        assert periods == {"2025-2T"}
        path = Path(output_dir) / "2025-2T.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7 template success before failure")
        kwargs["detail_sink"].append(SimpleNamespace(
            url="https://issuer.test/2025-2T.pdf",
            path=path,
            filename=path.name,
            period="2025-2T",
        ))
        raise RuntimeError("template host failed after first success")

    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        direct_url_templates=(
            "https://issuer.test/{year}/results-{quarter}T.pdf",
        ),
        delay_ms=0,
    )
    adapter = InvestorRelationsAdapter(
        downloader=primary,
        template_downloader=failing_templates,
    )

    report = adapter.fetch_incremental_report(
        issuer("issuer", "ISSUER", source),
        source,
        tmp_path,
        known_periods=set(),
        recheck_periods=set(),
        desired_periods={"2025-1T", "2025-2T"},
    )

    assert [artifact.source.period for artifact in report.artifacts] == [
        "2025-1T",
        "2025-2T",
    ]
    assert len(report.issues) == 1
    assert report.issues[0].retryable is True
    assert report.issues[0].record.source_record_id == "issuer:ir-url-templates"
    assert "template host failed after first success" in report.issues[0].error
    assert report.layers[-1] == IRLayerDiagnostic(
        layer="direct_url_templates",
        candidates=1,
        selected=1,
        note="RuntimeError: template host failed after first success",
    )


def test_ir_adapter_quarantines_periodless_and_governance_pdfs(tmp_path):
    def download(_url, output_dir, **kwargs):
        paths = []
        for filename, url, period in (
            (
                "results_en_Q1_2025.pdf",
                "https://issuer.test/results_en_Q1_2025.pdf",
                "2025-1T",
            ),
            (
                "governance_Q1_2025.pdf",
                "https://issuer.test/governance_Q1_2025.pdf",
                "2025-1T",
            ),
            ("corporate_brochure.pdf", "https://issuer.test/brochure.pdf", None),
        ):
            path = Path(output_dir) / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.7 fixture")
            paths.append(path)
            kwargs["detail_sink"].append(
                SimpleNamespace(
                    url=url,
                    path=path,
                    filename=filename,
                    period=period,
                )
            )
        return paths

    adapter = InvestorRelationsAdapter(
        downloader=download,
        template_downloader=lambda *_args, **_kwargs: [],
    )
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
    )
    company = issuer("issuer", "ISSUER", source)

    artifacts = adapter.fetch_incremental(
        company,
        source,
        tmp_path,
        known_periods=set(),
        recheck_periods=set(),
        desired_periods={"2025-1T"},
    )

    assert [item.filename for item in artifacts] == ["results_en_Q1_2025.pdf"]
    assert artifacts[0].source.period == "2025-1T"
    assert artifacts[0].source.language == "en"
    assert artifacts[0].source.rendition == "release"


def test_bilingual_renditions_are_distinct_but_corrections_version(tmp_path):
    def download(_url, output_dir, **kwargs):
        paths = []
        for language, stem in (("en", "results_en_Q1_2025"), ("es", "resultados_es_Q1_2025")):
            path = Path(output_dir) / f"{stem}.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"%PDF-1.7 {language}".encode())
            paths.append(path)
            kwargs["detail_sink"].append(
                SimpleNamespace(
                    url=f"https://issuer.test/{stem}.pdf",
                    path=path,
                    filename=path.name,
                    period="2025-1T",
                )
            )
        return paths

    adapter = InvestorRelationsAdapter(
        downloader=download,
        template_downloader=lambda *_args, **_kwargs: [],
    )
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
    )
    company = IssuerSpec(
        slug="issuer",
        ticker="ISSUER",
        name="Issuer",
        sector="test",
        template="generic",
        language="bilingual",
        sources=(source,),
    )
    artifacts = adapter.fetch_incremental(
        company,
        source,
        tmp_path / "stage",
        known_periods=set(),
        recheck_periods=set(),
        desired_periods={"2025-1T"},
    )
    by_language = {item.source.language: item for item in artifacts}

    assert set(by_language) == {"en", "es"}
    assert (
        by_language["en"].source.document_family_id
        != by_language["es"].source.document_family_id
    )
    with EstateWriter(tmp_path / "catalog.db", tmp_path / "estate") as writer:
        english = writer.store_fetched(by_language["en"])
        spanish = writer.store_fetched(by_language["es"])
        correction = writer.store_fetched(
            FetchedArtifact(
                source=by_language["en"].source,
                content=b"%PDF-1.7 corrected english",
                filename=by_language["en"].filename,
            )
        )

        assert english.version == spanish.version == 1
        assert correction.version == 2
        assert correction.supersedes_document_id == english.document_id
        assert correction.supersedes_document_id != spanish.document_id


def test_ir_adapter_quarantines_conflicting_document_family(
    tmp_path,
):
    def download(_url, output_dir, **kwargs):
        paths = []
        for stem in ("1T25", "1T25_risk"):
            path = Path(output_dir) / f"{stem}.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"%PDF-1.7 {stem}".encode())
            paths.append(path)
            kwargs["detail_sink"].append(
                SimpleNamespace(
                    url=f"https://issuer.test/{stem}.pdf",
                    path=path,
                    filename=path.name,
                    period="2025-1T",
                )
            )
        return paths

    adapter = InvestorRelationsAdapter(
        downloader=download,
        template_downloader=lambda *_args, **_kwargs: [],
    )
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
    )

    report = adapter.fetch_incremental_report(
        issuer("issuer", "ISSUER", source),
        source,
        tmp_path,
        known_periods=set(),
        recheck_periods=set(),
        desired_periods={"2025-1T"},
    )

    assert report.artifacts == ()
    assert len(report.issues) == 2
    assert all(issue.retryable is False for issue in report.issues)
    assert all(
        "ambiguous quarterly PDFs" in issue.error
        for issue in report.issues
    )


def test_ir_sync_stores_clean_family_while_rejecting_conflicting_family(
    tmp_path,
):
    def download(_url, output_dir, **kwargs):
        paths = []
        for stem in ("1T25_a", "1T25_b", "2T25"):
            path = Path(output_dir) / f"{stem}.pdf"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"%PDF-1.7 {stem}".encode())
            paths.append(path)
            period = "2025-2T" if stem.startswith("2T") else "2025-1T"
            kwargs["detail_sink"].append(
                SimpleNamespace(
                    url=f"https://issuer.test/{stem}.pdf",
                    path=path,
                    filename=path.name,
                    period=period,
                )
            )
        return paths

    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
        delay_ms=0,
    )
    ledger = FakeLedger()
    writer = FakeWriter()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (issuer("issuer", "ISSUER", source),)),
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=writer,
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=InvestorRelationsAdapter(
            downloader=download,
            template_downloader=lambda *_args, **_kwargs: [],
        ),
        wayback=NoWayback(),
        default_floor_year=2025,
    )

    report = service.sync(as_of=date(2025, 7, 1))

    assert report.exit_code == 1
    assert report.stored == 1
    assert [call[0].source.period for call in writer.calls] == ["2025-2T"]
    assert len(ledger.rejected) == 2
    assert ledger.retryable == []
    assert ledger.finished[-1][1] == "partial"


def test_ir_sync_keeps_success_and_records_per_url_failure_and_layers(
    tmp_path,
):
    from src.download.downloader import (
        DiscoveredPdf,
        DiscoveryLayerDiagnostic,
        DownloadFailure,
    )

    def download(_url, output_dir, **kwargs):
        successful_url = "https://issuer.test/2T25.pdf"
        failed_url = "https://issuer.test/1T25.pdf"
        path = Path(output_dir) / "2T25.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.7 clean 2T")
        kwargs["detail_sink"].append(
            SimpleNamespace(
                url=successful_url,
                path=path,
                filename=path.name,
                period="2025-2T",
            )
        )
        kwargs["candidate_sink"].extend(
            (
                DiscoveredPdf(failed_url, "2025-1T", ("static_crawl",)),
                DiscoveredPdf(successful_url, "2025-2T", ("static_crawl",)),
            )
        )
        kwargs["failure_sink"].append(
            DownloadFailure(
                failed_url,
                "2025-1T",
                "TimeoutError: issuer timed out",
            )
        )
        kwargs["layer_sink"].append(
            DiscoveryLayerDiagnostic("static_crawl", candidates=2, selected=2)
        )
        return [path]

    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
        delay_ms=0,
    )
    ledger = FakeLedger()
    writer = FakeWriter()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (issuer("issuer", "ISSUER", source),)),
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=writer,
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=InvestorRelationsAdapter(
            downloader=download,
            template_downloader=lambda *_args, **_kwargs: [],
        ),
        wayback=NoWayback(),
        default_floor_year=2025,
    )

    report = service.sync(as_of=date(2025, 7, 1))

    assert report.exit_code == 1
    assert report.stored == 1
    assert len(ledger.retryable) == 1
    assert ledger.retryable[0][1].url == "https://issuer.test/1T25.pdf"
    assert report.diagnostics == (
        SourceDiagnostic(
            issuer_slug="issuer",
            source_key="ir",
            layer="static_crawl",
            candidates=2,
            selected=2,
        ),
    )
    assert ledger.finished[-1][1] == "partial"


def test_failure_is_isolated_and_applied_failure_returns_nonzero():
    class PartlyFailingIr:
        def fetch_incremental(self, issuer_spec, source, *_args, **_kwargs):
            if issuer_spec.slug == "bad":
                raise RuntimeError("host unavailable")
            return (artifact(issuer_spec.slug, source.key, "2025-1T"),)

    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
    )
    registry = IssuerRegistry(
        1,
        (issuer("bad", "BAD", source), issuer("good", "GOOD", source)),
    )
    ledger = FakeLedger()
    writer = FakeWriter()
    service = QuarterlyAcquisitionService(
        registry,
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=writer,
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=PartlyFailingIr(),
        wayback=NoWayback(),
    )

    report = service.sync(as_of=date(2025, 4, 1))

    assert report.exit_code == 1
    assert report.stored == 1
    assert [call[0].source.issuer_slug for call in writer.calls] == ["good"]
    assert len(ledger.retryable) == 1
    assert ledger.finished[-1][1] == "partial"


def test_permanent_payload_validation_is_rejected_not_retryable():
    class InvalidIr:
        def fetch_incremental(self, issuer_spec, source_spec, *_args, **_kwargs):
            return (
                FetchedArtifact(
                    source=artifact(
                        issuer_spec.slug, source_spec.key, "2025-1T"
                    ).source,
                    content=b"not really a pdf",
                    filename="2025-1T.pdf",
                ),
            )

    class ValidatingWriter(FakeWriter):
        def store_fetched(self, *_args, **_kwargs):
            raise ArtifactValidationError("invalid PDF magic")

    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
    )
    ledger = FakeLedger()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (issuer("issuer", "ISSUER", source),)),
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=ValidatingWriter(),
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=InvalidIr(),
        wayback=NoWayback(),
    )

    report = service.sync(as_of=date(2025, 4, 1))

    assert report.exit_code == 1
    assert len(ledger.rejected) == 1
    assert ledger.retryable == []


def test_due_missing_period_with_empty_source_fails_coverage_gate():
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
    )
    ledger = FakeLedger()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (issuer("issuer", "ISSUER", source),)),
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=FakeWriter(),
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=NoIr(),
        wayback=NoWayback(),
        default_floor_year=2025,
    )

    report = service.sync(as_of=date(2025, 4, 1), strict_coverage=True)

    assert report.exit_code == 1
    assert report.coverage_gaps[0].reason == "due_periods_not_discovered"
    assert report.coverage_gaps[0].periods == ("2025-1T",)
    assert report.failures == ()


def test_known_trailing_recheck_with_no_observation_is_explicit_failure(
    tmp_path,
):
    from src.download.downloader import DiscoveryLayerDiagnostic

    def empty_download(_url, _output_dir, **kwargs):
        kwargs["layer_sink"].append(
            DiscoveryLayerDiagnostic(
                "static_crawl",
                candidates=0,
                selected=0,
                note="issuer page returned no quarterly links",
            )
        )
        return []

    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
    )
    ledger = FakeLedger()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (issuer("issuer", "ISSUER", source),)),
        coverage=FakeCoverage(
            {("issuer", PDF_DOCUMENT_TYPE): {"2025-1T"}}
        ),
        ledger=ledger,
        writer=FakeWriter(),
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=InvestorRelationsAdapter(
            downloader=empty_download,
            template_downloader=lambda *_args, **_kwargs: [],
        ),
        wayback=NoWayback(),
        default_floor_year=2025,
        recheck_periods=1,
    )

    report = service.sync(as_of=date(2025, 4, 1))

    assert report.exit_code == 1
    assert report.coverage_gaps == (
        CoverageGap(
            issuer_slug="issuer",
            source_key="ir",
            document_type=PDF_DOCUMENT_TYPE,
            reason="recheck_periods_not_observed",
            periods=("2025-1T",),
        ),
    )
    assert len(report.failures) == 1
    assert "trailing recheck observed no candidate" in report.failures[0].error
    assert report.diagnostics[0].note == (
        "issuer page returned no quarterly links"
    )
    assert len(ledger.retryable) == 1
    assert ledger.finished[-1][1] == "failed"


def test_wayback_is_never_called_without_explicit_backfill():
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
    )
    company = issuer("issuer", "ISSUER", source)
    wayback = NoWayback()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (company,)),
        coverage=FakeCoverage(),
        ledger=FakeLedger(),
        writer=FakeWriter(),
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=NoIr(),
        wayback=wayback,
    )

    service.sync(as_of=date(2025, 7, 1))
    assert wayback.calls == 0
    service.sync(as_of=date(2025, 7, 1), wayback_backfill=True)
    assert wayback.calls == 1


def test_plan_never_touches_remote_archive():
    source = AcquisitionSource(
        key="bmv_xbrl",
        kind="bmv_xbrl",
        xbrl_ticker="ISSUER",
        floor_year=2025,
    )
    bmv = BmvXbrlAdapter(
        archive_loader=lambda: (_ for _ in ()).throw(AssertionError("network touched"))
    )
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (issuer("issuer", "ISSUER", source),)),
        coverage=FakeCoverage(),
        bmv=bmv,
        ir=NoIr(),
        wayback=NoWayback(),
    )

    plans = service.plan(as_of=date(2025, 4, 1))

    assert plans[0].source("bmv_xbrl").recheck_periods == ("2025-1T",)


def test_unexpected_orchestration_failure_finishes_durable_run():
    class BrokenLedger(FakeLedger):
        def discover(self, record, *, run_id=None):
            raise KeyboardInterrupt("operator stop")

    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
    )

    class OneArtifactIr:
        def fetch_incremental(self, issuer_spec, source_spec, *_args, **_kwargs):
            return (artifact(issuer_spec.slug, source_spec.key, "2025-1T"),)

    ledger = BrokenLedger()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (issuer("issuer", "ISSUER", source),)),
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=FakeWriter(),
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=OneArtifactIr(),
        wayback=NoWayback(),
    )

    try:
        service.sync(as_of=date(2025, 4, 1))
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("KeyboardInterrupt should propagate")

    assert ledger.finished[-1][1] == "failed"


def test_lost_lease_is_never_downgraded_to_retryable_item_failure():
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
    )

    class OneArtifactIr:
        def fetch_incremental(self, issuer_spec, source_spec, *_args, **_kwargs):
            return (artifact(issuer_spec.slug, source_spec.key, "2025-1T"),)

    class FencedWriter(FakeWriter):
        def store_fetched(self, *_args, **_kwargs):
            raise AcquisitionLeaseLost("lease reclaimed")

    ledger = FakeLedger()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (issuer("issuer", "ISSUER", source),)),
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=FencedWriter(),
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=OneArtifactIr(),
        wayback=NoWayback(),
    )

    try:
        service.sync(as_of=date(2025, 4, 1))
    except AcquisitionLeaseLost:
        pass
    else:
        raise AssertionError("lost lease must abort the stale worker")

    assert ledger.retryable == []
    assert ledger.rejected == []


def test_sync_requires_explicit_apply_confirmation():
    try:
        acquisition_main(["sync"])
    except SystemExit as exc:
        assert "--apply" in str(exc)
    else:
        raise AssertionError("mutating sync must require --apply")


def test_applied_report_prints_totals_and_failures(capsys):
    report = RunReport(
        run_id="run-1",
        mode="sync",
        plans=(),
        discovered=3,
        stored=1,
        unchanged=1,
        failures=(
            SyncFailure(
                issuer_slug="issuer",
                source_key="ir",
                source_record_id="record",
                error="RuntimeError: failed",
            ),
        ),
    )

    _print_report(report)
    captured = capsys.readouterr()

    assert "result: 3 discovered · 1 stored · 1 unchanged · 1 failed" in captured.out
    assert "FAILED issuer/ir: RuntimeError: failed" in captured.err


def test_json_mode_redirects_legacy_stdout_noise(monkeypatch, capsys, tmp_path):
    registry = IssuerRegistry(1, ())

    class Resource:
        def close(self):
            return None

    class NoisyService:
        def __init__(self, *_args, **_kwargs):
            pass

        def sync(self, **_kwargs):
            print("legacy downloader noise")
            return RunReport(run_id="run-json", mode="sync", plans=())

    monkeypatch.setattr(acquisition_cli, "load_issuer_registry", lambda _path: registry)
    monkeypatch.setattr(acquisition_cli, "QuarterlyAcquisitionService", NoisyService)
    monkeypatch.setattr(
        "src.acquisition.ledger.AcquisitionLedger", lambda _db: Resource()
    )
    monkeypatch.setattr(
        "src.acquisition.writer.EstateWriter",
        lambda _db, _root: Resource(),
    )

    exit_code = acquisition_main(
        [
            "sync",
            "--apply",
            "--json",
            "--db",
            str(tmp_path / "catalog.db"),
            "--estate-root",
            str(tmp_path / "estate"),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.lstrip().startswith("{")
    assert "legacy downloader noise" not in captured.out
    assert "legacy downloader noise" in captured.err
    assert __import__("json").loads(captured.out)["run_id"] == "run-json"


def test_estate_coverage_requires_primary_artifact_and_accepts_membership(tmp_path):
    db = tmp_path / "estate.db"
    primary_pdf = tmp_path / "primary.pdf"
    primary_pdf.write_bytes(b"%PDF-1.7 fixture")
    raw_xbrl = tmp_path / "ISSUER_2024-4T.json.gz"
    raw_xbrl.write_bytes(b"gzip fixture")
    connection = sqlite3.connect(db)
    connection.executescript(
        """
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY, company TEXT, period TEXT, doc_type TEXT
        );
        CREATE TABLE memberships (
            document_id TEXT, company TEXT, industry TEXT
        );
        CREATE TABLE artifacts (
            document_id TEXT, role TEXT, format TEXT, path TEXT, sha256 TEXT
        );
        CREATE TABLE content_objects (
            sha256 TEXT PRIMARY KEY, blob_path TEXT, object_key TEXT
        );
        INSERT INTO documents VALUES
            ('derived-only', 'alias_name', '2024-1T', 'quarterly_release'),
            ('primary', 'alias_name', '2024-2T', 'quarterly_release'),
            ('facts-only', 'issuer', '2024-3T', 'regulatory_filing'),
            ('raw-xbrl', 'issuer', '2024-4T', 'regulatory_filing');
        INSERT INTO memberships VALUES
            ('derived-only', 'issuer', 'test'),
            ('primary', 'issuer', 'test');
        """
    )
    connection.executemany(
        "INSERT INTO artifacts VALUES(?,?,?,?,?)",
        (
            ("derived-only", "search_text", "md", str(tmp_path / "derived.md"), "d"),
            ("primary", "original", "pdf", str(primary_pdf), "p"),
            (
                "facts-only",
                "derived",
                "json",
                str(tmp_path / "ISSUER_2024-3T_facts.json"),
                "f",
            ),
            ("raw-xbrl", "raw_xbrl", "gz", str(raw_xbrl), "x"),
        ),
    )
    connection.commit()
    connection.close()
    coverage = EstateCoverage(db)

    assert coverage.known_periods("issuer", PDF_DOCUMENT_TYPE) == {"2024-2T"}
    assert coverage.known_periods("issuer", XBRL_DOCUMENT_TYPE) == {"2024-4T"}

    primary_pdf.unlink()
    assert coverage.known_periods("issuer", PDF_DOCUMENT_TYPE) == set()


def test_estate_coverage_resolves_portable_object_keys_after_relocation(
    tmp_path,
):
    estate_root = tmp_path / "relocated-estate"
    object_key = "blobs/ab/" + "ab" * 32
    blob = estate_root / object_key
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"%PDF-1.7 relocated")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-1.7 outside")
    database = estate_root / "catalog.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            company TEXT,
            period TEXT,
            doc_type TEXT
        );
        CREATE TABLE memberships (
            document_id TEXT,
            company TEXT,
            industry TEXT
        );
        CREATE TABLE artifacts (
            document_id TEXT,
            role TEXT,
            format TEXT,
            path TEXT,
            sha256 TEXT
        );
        CREATE TABLE content_objects (
            sha256 TEXT PRIMARY KEY,
            blob_path TEXT,
            object_key TEXT
        );
        INSERT INTO documents VALUES (
            'portable', 'issuer', '2026-1T', 'quarterly_release'
        );
        INSERT INTO documents VALUES (
            'unsafe', 'issuer', '2026-2T', 'quarterly_release'
        );
        """
    )
    connection.execute(
        "INSERT INTO artifacts VALUES (?,?,?,?,?)",
        (
            "portable",
            "original",
            "pdf",
            "/old/computer/views/reports/issuer.pdf",
            "ab" * 32,
        ),
    )
    connection.execute(
        "INSERT INTO content_objects VALUES (?,?,?)",
        (
            "ab" * 32,
            "/old/computer/blobs/ab/object",
            object_key,
        ),
    )
    connection.execute(
        "INSERT INTO artifacts VALUES (?,?,?,?,?)",
        (
            "unsafe",
            "original",
            "pdf",
            "/old/computer/views/reports/unsafe.pdf",
            "cd" * 32,
        ),
    )
    connection.execute(
        "INSERT INTO content_objects VALUES (?,?,?)",
        (
            "cd" * 32,
            "/old/computer/blobs/cd/object",
            "../outside.pdf",
        ),
    )
    connection.commit()
    connection.close()

    coverage = EstateCoverage(database, estate_root=estate_root)

    assert coverage.known_periods("issuer", PDF_DOCUMENT_TYPE) == {
        "2026-1T"
    }
    remote_coverage = EstateCoverage(
        database,
        estate_root=estate_root,
        object_exists=lambda _key: True,
    )
    assert remote_coverage.known_periods(
        "issuer",
        PDF_DOCUMENT_TYPE,
    ) == {"2026-1T"}


def test_service_integrates_with_durable_ledger_and_writer_idempotently(tmp_path):
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
    )
    company = issuer("issuer", "ISSUER", source)

    class StableIr:
        def fetch_incremental(self, issuer_spec, source_spec, *_args, **_kwargs):
            return (artifact(issuer_spec.slug, source_spec.key, "2025-1T"),)

    db = tmp_path / "catalog.db"
    estate_root = tmp_path / "estate"
    ledger = AcquisitionLedger(db)
    writer = EstateWriter(db, estate_root)
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (company,)),
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=writer,
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=StableIr(),
        wayback=NoWayback(),
    )
    try:
        first = service.sync(as_of=date(2025, 4, 1))
        second = service.sync(as_of=date(2025, 4, 1))

        assert (first.stored, first.unchanged) == (1, 0)
        assert (second.stored, second.unchanged) == (0, 1)
        assert list((estate_root / "blobs").glob("*/*"))
        assert ledger.conn.execute(
            "SELECT COUNT(*) FROM source_record_versions"
        ).fetchone()[0] == 1
        runs = ledger.conn.execute(
            """SELECT status,stored_count,unchanged_count
               FROM acquisition_runs ORDER BY started_at,rowid"""
        ).fetchall()
        assert [tuple(row) for row in runs] == [
            ("succeeded", 1, 0),
            ("succeeded", 0, 1),
        ]
        assert ledger.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        assert ledger.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
        assert ledger.conn.execute(
            "SELECT COUNT(*) FROM acquisition_attempts"
        ).fetchone()[0] == 2
        assert ledger.conn.execute(
            "SELECT COUNT(*) FROM acquisition_leases"
        ).fetchone()[0] == 0
    finally:
        writer.close()
        ledger.close()


def test_acquisition_expands_company_alias_memberships_for_consumers():
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://investors.banorte.test/reports",
        floor_year=2026,
    )
    company = IssuerSpec(
        slug="gfnorte",
        ticker="GFNORTE",
        name="Grupo Financiero Banorte",
        sector="banks",
        template="financials",
        memberships=ProjectMembership(alpha_go=True, soft=True),
        sources=(source,),
    )

    class StableIr:
        def fetch_incremental(
            self,
            issuer_spec,
            source_spec,
            *_args,
            **_kwargs,
        ):
            return (
                artifact(
                    issuer_spec.slug,
                    source_spec.key,
                    "2026-1T",
                ),
            )

    writer = FakeWriter()
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (company,)),
        coverage=FakeCoverage(),
        ledger=FakeLedger(),
        writer=writer,
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=StableIr(),
        wayback=NoWayback(),
        default_floor_year=2026,
    )

    service.sync(as_of=date(2026, 4, 1))

    memberships = writer.calls[0][2]
    assert {row["company"] for row in memberships} == {
        "gfnorte",
        "banorte",
    }
    assert all(row["alpha_go"] is True for row in memberships)
    assert all(row["soft"] is True for row in memberships)


def test_partial_real_run_is_finalized_and_releases_singleton_lease(tmp_path):
    source = AcquisitionSource(
        key="ir",
        kind="investor_relations",
        url="https://issuer.test/reports",
        floor_year=2025,
    )

    class PartlyFailingIr:
        def fetch_incremental(self, issuer_spec, source_spec, *_args, **_kwargs):
            if issuer_spec.slug == "bad":
                raise RuntimeError("temporary outage")
            return (artifact(issuer_spec.slug, source_spec.key, "2025-1T"),)

    db = tmp_path / "catalog.db"
    ledger = AcquisitionLedger(db)
    writer = EstateWriter(db, tmp_path / "estate")
    service = QuarterlyAcquisitionService(
        IssuerRegistry(
            1,
            (issuer("bad", "BAD", source), issuer("good", "GOOD", source)),
        ),
        coverage=FakeCoverage(),
        ledger=ledger,
        writer=writer,
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        ir=PartlyFailingIr(),
        wayback=NoWayback(),
    )
    try:
        first = service.sync(as_of=date(2025, 4, 1))
        second = service.sync(as_of=date(2025, 4, 1))

        assert first.exit_code == second.exit_code == 1
        assert first.stored == 1
        assert second.unchanged == 1
        assert [
            row[0]
            for row in ledger.conn.execute(
                "SELECT status FROM acquisition_runs ORDER BY started_at, rowid"
            )
        ] == ["partial", "partial"]
        assert ledger.conn.execute(
            "SELECT COUNT(*) FROM acquisition_leases"
        ).fetchone()[0] == 0
    finally:
        writer.close()
        ledger.close()
