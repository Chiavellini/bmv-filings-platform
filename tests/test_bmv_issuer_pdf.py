from __future__ import annotations

from datetime import date

import pytest

from src.acquisition.adapters import BmvXbrlAdapter
from src.acquisition.bmv_issuer import (
    BmvIssuerPdfAdapter,
    _bmv_get,
    parse_quarterly_financial_page,
)
from src.acquisition.ledger import AcquisitionLedger
from src.acquisition.models import AcquisitionSource, IssuerSpec
from src.acquisition.registry import IssuerRegistry
from src.acquisition.service import (
    EmptyCoverage,
    PDF_DOCUMENT_TYPE,
    QuarterlyAcquisitionService,
)
from src.acquisition.writer import EstateWriter


SOURCE = AcquisitionSource(
    key="bmv_issuer_pdf",
    kind="bmv_issuer_pdf",
    url=(
        "https://www.bmv.com.mx/es/emisoras/"
        "informacionfinanciera/GNP-5444-CGEN_CAPIT"
    ),
    floor_year=2026,
)
ISSUER = IssuerSpec(
    slug="gnp",
    ticker="GNP",
    name="GNP",
    sector="insurers",
    template="financials",
    sources=(SOURCE,),
)
HTML = """
<h2>ESTADOS FINANCIEROS BÁSICOS</h2>
<table>
  <tr><td>23-Jul-2026 14:27</td>
      <td>Información Del Trimestre 2 Del año 2026</td>
      <td><a href="/docs-pub/estadfin/estados_2026-02.pdf">Descargar</a></td></tr>
</table>
<h2>COMENTARIOS Y ANÁLISIS DE ADMINISTRACIÓN</h2>
<table>
  <tr><td>23-Jul-2026 14:27</td>
      <td>Información Del Trimestre 2 Del año 2026</td>
      <td><a href="/docs-pub/infinasg/asginfin_2026-02.pdf">Descargar</a></td></tr>
</table>
<h2>REPORTES ANALISTAS INDEPENDIENTES</h2>
<table>
  <tr><td>28-Jul-2026 17:35</td>
      <td>Reporte de información financiera Del Trimestre 2 Del Año 2026</td>
      <td>PROGNOS</td>
      <td><a href="/docs-pub/analyst/not-primary.pdf">Descargar</a></td></tr>
</table>
<h2>CONSTANCIA TRIMESTRAL</h2>
<table>
  <tr><td>23-Jul-2026 14:29</td>
      <td>Constancia Trimestral del Periodo 2-2026</td>
      <td><a href="/docs-pub/constrim/certificate.pdf">Descargar</a></td></tr>
</table>
"""


def test_parser_prefers_management_discussion_and_excludes_non_primary_sections():
    records = parse_quarterly_financial_page(HTML, ISSUER, SOURCE)

    assert len(records) == 1
    record = records[0].record
    assert record.period == "2026-2T"
    assert record.document_type == PDF_DOCUMENT_TYPE
    assert record.rendition == "management_discussion"
    assert record.url == (
        "https://www.bmv.com.mx/docs-pub/infinasg/asginfin_2026-02.pdf"
    )
    assert record.published_at.isoformat(timespec="minutes") == "2026-07-23T14:27"
    assert record.metadata["page_url"] == SOURCE.url


def test_parser_does_not_cross_into_the_next_bmv_section():
    html = """
    <h2>COMENTARIOS Y ANÁLISIS DE ADMINISTRACIÓN</h2>
    <p>Sin información disponible.</p>
    <h2>REPORTES ANALISTAS INDEPENDIENTES</h2>
    <table>
      <tr><td>28-Jul-2026</td>
          <td>Reporte del Trimestre 2 del año 2026</td>
          <td><a href="/docs-pub/analyst/not-primary.pdf">PDF</a></td></tr>
    </table>
    """

    assert parse_quarterly_financial_page(html, ISSUER, SOURCE) == ()


def test_parser_rejects_external_pdf_hosts():
    html = """
    <h2>COMENTARIOS Y ANÁLISIS DE ADMINISTRACIÓN</h2>
    <table>
      <tr><td>23-Jul-2026</td>
          <td>Información Del Trimestre 2 Del año 2026</td>
          <td><a href="https://evil.invalid/payload.pdf">PDF</a></td></tr>
    </table>
    """

    with pytest.raises(ValueError, match="official BMV host"):
        parse_quarterly_financial_page(html, ISSUER, SOURCE)


def test_adapter_rejects_non_bmv_page_before_loading_it():
    called = False

    def load_page(_url):
        nonlocal called
        called = True
        return HTML

    adapter = BmvIssuerPdfAdapter(page_loader=load_page)
    source = AcquisitionSource(
        key="bmv_pdf",
        kind="bmv_issuer_pdf",
        url="https://evil.invalid/issuer",
    )

    with pytest.raises(ValueError, match="official BMV host"):
        adapter.discover(ISSUER, source)
    assert not called


def test_default_bmv_fetcher_rejects_external_redirect_before_following(
    monkeypatch,
):
    calls: list[str] = []

    class Redirect:
        is_redirect = True
        is_permanent_redirect = False
        headers = {"location": "https://evil.invalid/payload.pdf"}
        url = SOURCE.url

    class Session:
        def get(self, url, **_kwargs):
            calls.append(url)
            return Redirect()

    monkeypatch.setattr(
        "src.download.downloader._make_session",
        lambda: Session(),
    )

    with pytest.raises(ValueError, match="official BMV host"):
        _bmv_get(SOURCE.url, timeout=10)
    assert calls == [SOURCE.url]


def test_adapter_filters_periods_and_fetches_the_exact_discovered_pdf(tmp_path):
    fetched_urls: list[str] = []

    def load_pdf(url):
        fetched_urls.append(url)
        return b"%PDF-1.7 official BMV fixture", 200, {"content-type": "application/pdf"}

    adapter = BmvIssuerPdfAdapter(
        page_loader=lambda _url: HTML,
        pdf_loader=load_pdf,
        min_interval_ms=0,
    )

    assert adapter.discover(ISSUER, SOURCE, wanted_periods={"2025-4T"}) == ()
    discovered = adapter.discover(
        ISSUER,
        SOURCE,
        wanted_periods={"2026-2T"},
    )
    artifact = adapter.fetch(discovered[0], tmp_path, source=SOURCE)

    assert fetched_urls == [discovered[0].record.url]
    assert artifact.content.startswith(b"%PDF-")
    assert artifact.filename == "asginfin_2026-02.pdf"
    assert artifact.response_status == 200


def test_adapter_records_validated_final_pdf_url_and_rejects_external_redirect(
    tmp_path,
):
    final_url = (
        "https://www.bmv.com.mx/docs-pub/infinasg/"
        "asginfin_2026-02-final.pdf"
    )
    adapter = BmvIssuerPdfAdapter(
        page_loader=lambda _url: HTML,
        pdf_loader=lambda _url: (
            b"%PDF-1.7 fixture",
            200,
            {},
            final_url,
        ),
        min_interval_ms=0,
    )
    discovered = adapter.discover(ISSUER, SOURCE)[0]

    artifact = adapter.fetch(discovered, tmp_path, source=SOURCE)

    assert artifact.source.url == final_url
    assert artifact.source.metadata["discovered_url"] == (
        discovered.record.url
    )
    assert artifact.source.metadata["final_url"] == final_url

    rejected = BmvIssuerPdfAdapter(
        page_loader=lambda _url: HTML,
        pdf_loader=lambda _url: (
            b"%PDF-1.7 fixture",
            200,
            {},
            "https://evil.invalid/redirected.pdf",
        ),
        min_interval_ms=0,
    )
    with pytest.raises(ValueError, match="official BMV host"):
        rejected.fetch(discovered, tmp_path, source=SOURCE)


def test_adapter_rate_limits_successive_pdf_fetch_starts(tmp_path):
    now = [10.0]
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    adapter = BmvIssuerPdfAdapter(
        page_loader=lambda _url: HTML,
        pdf_loader=lambda _url: b"%PDF-1.7 fixture",
        min_interval_ms=1000,
        clock=lambda: now[0],
        sleeper=sleep,
    )
    discovered = adapter.discover(ISSUER, SOURCE)[0]

    adapter.fetch(discovered, tmp_path / "first", source=SOURCE)
    adapter.fetch(discovered, tmp_path / "second", source=SOURCE)

    assert sleeps == [1.0]


def test_service_stores_bmv_pdf_idempotently_through_root_estate(tmp_path):
    database = tmp_path / "catalog.db"
    estate_root = tmp_path / "estate"
    ledger = AcquisitionLedger(database)
    writer = EstateWriter(database, estate_root)
    adapter = BmvIssuerPdfAdapter(
        page_loader=lambda _url: HTML,
        pdf_loader=lambda _url: b"%PDF-1.7 official BMV fixture",
        min_interval_ms=0,
    )
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (ISSUER,)),
        coverage=EmptyCoverage(),
        ledger=ledger,
        writer=writer,
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        bmv_issuer=adapter,
        recheck_periods=2,
    )
    try:
        first = service.sync(as_of=date(2026, 7, 30))
        second = service.sync(as_of=date(2026, 7, 30))

        assert (first.stored, first.unchanged, first.failures) == (1, 0, ())
        assert (second.stored, second.unchanged, second.failures) == (0, 1, ())
        assert ledger.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        row = ledger.conn.execute(
            "SELECT source_url,period,doc_type FROM documents"
        ).fetchone()
        assert tuple(row) == (
            "https://www.bmv.com.mx/docs-pub/infinasg/asginfin_2026-02.pdf",
            "2026-2T",
            PDF_DOCUMENT_TYPE,
        )
    finally:
        writer.close()
        ledger.close()


def test_service_rejects_non_pdf_bmv_response(tmp_path):
    database = tmp_path / "catalog.db"
    ledger = AcquisitionLedger(database)
    writer = EstateWriter(database, tmp_path / "estate")
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (ISSUER,)),
        coverage=EmptyCoverage(),
        ledger=ledger,
        writer=writer,
        bmv=BmvXbrlAdapter(archive_loader=lambda: []),
        bmv_issuer=BmvIssuerPdfAdapter(
            page_loader=lambda _url: HTML,
            pdf_loader=lambda _url: b"<html>not a PDF</html>",
            min_interval_ms=0,
        ),
    )
    try:
        report = service.sync(as_of=date(2026, 7, 30))

        assert report.stored == 0
        assert len(report.failures) == 1
        assert "does not start with the PDF magic signature" in (
            report.failures[0].error
        )
        assert ledger.conn.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0] == 0
        assert ledger.conn.execute(
            "SELECT status FROM source_records"
        ).fetchone()[0] == "rejected"
    finally:
        writer.close()
        ledger.close()


def test_source_activation_period_does_not_claim_unsupported_history():
    activated = AcquisitionSource(
        key=SOURCE.key,
        kind=SOURCE.kind,
        url=SOURCE.url,
        coverage_from_period="2026-2T",
    )
    company = IssuerSpec(
        slug=ISSUER.slug,
        ticker=ISSUER.ticker,
        name=ISSUER.name,
        sector=ISSUER.sector,
        template=ISSUER.template,
        sources=(activated,),
    )
    service = QuarterlyAcquisitionService(
        IssuerRegistry(1, (company,)),
        coverage=EmptyCoverage(),
        recheck_periods=2,
    )

    plan = service.plan(as_of=date(2026, 9, 1))[0].source(SOURCE.key)

    assert plan.missing_periods == ("2026-2T",)
    assert plan.recheck_periods == ("2026-2T",)
    assert "2026-1T" not in plan.desired_periods
    issuer_plan = service.plan(as_of=date(2026, 9, 1))[0]
    assert "2026-1T" in issuer_plan.primary_pdf_missing_periods
    assert "primary_pdf_history_outside_source_window" in issuer_plan.gaps
