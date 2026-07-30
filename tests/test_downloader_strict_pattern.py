"""Offline coverage for fail-closed configured PDF selectors."""

from __future__ import annotations

from pathlib import Path


BANORTE_PAGE = "https://investors.banorte.com/financial-information/quarterly-reports"
BANORTE_RISK_PDF = (
    "https://investors.banorte.com/media/reportes/"
    "GFNorte_Reporte_de_Riesgos_1T26.pdf"
)
BANORTE_RESULTS_PATTERN = r"(?:resultados|estados[-_ ]financieros).*\.pdf"


class _Response:
    def __init__(self, *, text: str = "", url: str = "", payload=None):
        self.text = text
        self.url = url
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        if self._payload is None:
            raise ValueError("response has no JSON payload")
        return self._payload


class _PageSession:
    verify = True

    def __init__(self, html: str):
        self.html = html

    def get(self, url, **kwargs):
        if url != BANORTE_PAGE:
            raise AssertionError(f"unexpected request: {url}")
        return _Response(text=self.html, url=url)


def _banorte_risk_anchor():
    from src.download.downloader import _PdfAnchor

    return _PdfAnchor(
        url=BANORTE_RISK_PDF,
        filename=Path(BANORTE_RISK_PDF).name,
        text="Reporte de Riesgos 1T26",
    )


def test_strict_selector_does_not_fall_back_to_banorte_risk_report():
    """A narrow configured selector must not widen itself to any quarterly-looking PDF."""
    from src.download.downloader import _select_pdf_links

    anchors = [_banorte_risk_anchor()]

    # Legacy/default behavior remains permissive.
    assert _select_pdf_links(anchors, BANORTE_RESULTS_PATTERN) == [BANORTE_RISK_PDF]
    # Issuer configurations can opt into fail-closed selection.
    assert _select_pdf_links(
        anchors,
        BANORTE_RESULTS_PATTERN,
        strict_file_pattern=True,
    ) == []


def test_canonical_gfnorte_allowlist_selects_results_and_rejects_risk_pdf():
    from src.acquisition.registry import load_issuer_registry
    from src.download.downloader import _extract_pdf_links

    source = load_issuer_registry().get("gfnorte").source("ir")
    result_url = (
        "https://investors.banorte.com/~/media/Files/B/Banorte-IR/"
        "financial-information/quarterly-results/es/2026/2T26/2T26.pdf"
    )
    risk_url = (
        "https://investors.banorte.com/~/media/Files/B/Banorte-IR/"
        "financial-information/quarterly-results/es/2025/4T25/"
        "4T25%20Reporte%20Administracion%20de%20Riesgos.pdf"
    )
    html = (
        f'<a href="{result_url}">2T26</a>'
        f'<a href="{risk_url}">4T25 Reporte Administracion de Riesgos</a>'
    )

    selected = _extract_pdf_links(
        html,
        source.url,
        source.pdf_link_pattern,
        strict_file_pattern=source.strict_pdf_link_pattern,
    )

    assert selected == [result_url]


def test_canonical_femsa_allowlist_selects_extensionless_result_with_period_hint():
    from src.acquisition.registry import load_issuer_registry
    from src.download.downloader import _extract_pdf_links

    source = load_issuer_registry().get("femsa").source("ir")
    result_url = (
        "https://femsa.gcs-web.com/static-files/"
        "c9f9b971-f7e5-4c9c-b76c-f5a21c2c3f88"
    )
    presentation_url = (
        "https://femsa.gcs-web.com/static-files/"
        "00000000-0000-0000-0000-000000000000"
    )
    html = (
        f'<a href="{result_url}" title="PR 2Q26 vf.pdf">2Q 2026 Results</a>'
        f'<a href="{presentation_url}">2Q 2026 Earnings Presentation</a>'
    )
    hints: dict[str, str] = {}

    selected = _extract_pdf_links(
        html,
        source.url,
        source.pdf_link_pattern,
        strict_file_pattern=source.strict_pdf_link_pattern,
        hint_filenames=hints,
    )

    assert selected == [result_url]
    assert hints == {result_url: "2026-2T.pdf"}
    assert not any(url.endswith("/vf.pdf") for url in selected)


def test_femsa_extensionless_hint_reaches_download_provenance(monkeypatch, tmp_path):
    from src.acquisition.registry import load_issuer_registry
    from src.download import downloader

    source = load_issuer_registry().get("femsa").source("ir")
    result_url = (
        "https://femsa.gcs-web.com/static-files/"
        "c9f9b971-f7e5-4c9c-b76c-f5a21c2c3f88"
    )
    html = f'<a href="{result_url}" title="PR 2Q26 vf.pdf">2Q 2026 Results</a>'
    details = []

    class _FemsaPageSession:
        verify = True

        def get(self, url, **kwargs):
            assert url == source.url
            return _Response(text=html, url=url)

    monkeypatch.setattr(
        downloader,
        "_make_session",
        lambda **kwargs: _FemsaPageSession(),
    )
    monkeypatch.setattr(downloader, "_verify_extensionless_links", lambda *args: args[1])
    monkeypatch.setattr(
        downloader,
        "_download_pdf",
        lambda session, url, dest, **kwargs: dest,
    )

    paths = downloader.download_from_ir(
        source.url,
        tmp_path,
        file_pattern=source.pdf_link_pattern,
        strict_file_pattern=source.strict_pdf_link_pattern,
        use_playwright=False,
        delay_ms=0,
        detail_sink=details,
    )

    assert [path.name for path in paths] == ["2026-2T.pdf"]
    assert len(details) == 1
    assert details[0].url == result_url
    assert details[0].period == "2026-2T"


def test_canonical_sports_world_allowlist_rejects_other_investor_assets():
    from src.acquisition.registry import load_issuer_registry
    from src.download.downloader import _extract_pdf_links

    source = load_issuer_registry().get("sports_world").source("ir")
    result_url = (
        "https://www.sportsworld.com.mx/uploads/es/documents/"
        "reports_quarterly/gsw_reporte_2T26.pdf"
    )
    annual_url = (
        "https://www.sportsworld.com.mx/uploads/es/documents/"
        "reports_annual/gsw_reporte_anual_2025.pdf"
    )
    html = (
        f'<a href="{result_url}">2T</a>'
        f'<a href="{annual_url}">Informe Anual 2025</a>'
    )

    assert _extract_pdf_links(
        html,
        source.url,
        source.pdf_link_pattern,
        strict_file_pattern=source.strict_pdf_link_pattern,
    ) == [result_url]


def test_canonical_tiendas_3b_feed_selects_release_not_presentation():
    from src.acquisition.registry import load_issuer_registry
    from src.download.downloader import _fetch_year_api_links

    source = load_issuer_registry().get("tiendas_3b").source("ir")
    api_url = source.year_api_urls[0]
    release_url = (
        "https://s203.q4cdn.com/155743495/files/doc_financials/"
        "2026/q1/Earnings-Release-1Q26.pdf"
    )
    presentation_url = (
        "https://s203.q4cdn.com/155743495/files/doc_financials/"
        "2026/q1/Earnings-PPT-1Q26.pdf"
    )

    class _TiendasApiSession:
        def get(self, url, **kwargs):
            assert url == api_url
            return _Response(
                payload={
                    "GetFinancialReportListResult": [
                        {
                            "ReportTitle": "First Quarter 2026",
                            "Documents": [
                                {
                                    "DocumentCategory": "news",
                                    "DocumentTitle": "Press Release",
                                    "DocumentPath": release_url,
                                },
                                {
                                    "DocumentCategory": "presentation",
                                    "DocumentTitle": "Presentation",
                                    "DocumentPath": presentation_url,
                                },
                            ],
                        }
                    ]
                },
                url=url,
            )

    assert _fetch_year_api_links(
        _TiendasApiSession(),
        list(source.year_api_urls),
        source.url,
        source.pdf_link_pattern,
        True,
        strict_file_pattern=source.strict_pdf_link_pattern,
    ) == [release_url]


def test_download_from_ir_applies_strict_pattern_to_static_discovery(
    monkeypatch,
    tmp_path,
):
    from src.download import downloader

    html = (
        "<html><body>"
        f'<a href="{BANORTE_RISK_PDF}">Reporte de Riesgos 1T26</a>'
        "</body></html>"
    )
    downloaded: list[str] = []
    monkeypatch.setattr(downloader, "_make_session", lambda **kwargs: _PageSession(html))
    monkeypatch.setattr(
        downloader,
        "_download_pdf",
        lambda session, url, dest, **kwargs: downloaded.append(url) or dest,
    )

    paths = downloader.download_from_ir(
        BANORTE_PAGE,
        tmp_path,
        file_pattern=BANORTE_RESULTS_PATTERN,
        strict_file_pattern=True,
        use_playwright=False,
        delay_ms=0,
    )

    assert paths == []
    assert downloaded == []


def test_year_api_discovery_respects_strict_pattern():
    from src.download.downloader import _fetch_year_api_links

    api_url = "https://investors.banorte.com/api/reports/2026"

    class _ApiSession:
        def get(self, url, **kwargs):
            assert url == api_url
            return _Response(payload={"documents": [BANORTE_RISK_PDF]}, url=url)

    assert _fetch_year_api_links(
        _ApiSession(),
        [api_url],
        BANORTE_PAGE,
        BANORTE_RESULTS_PATTERN,
        True,
        strict_file_pattern=True,
    ) == []


def test_archive_discovery_respects_strict_pattern():
    from src.download.downloader import _extract_quarterly_archive_links

    archive_url = "https://www.walmex.mx/Code/snippets/rest/Informacion"
    page_url = "https://www.walmex.mx/en/financial-information/quarterly.html"
    release_url = (
        "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/"
        "Walmex_1Q26_Release.pdf"
    )

    class _ArchiveSession:
        verify = True

        def get(self, url, **kwargs):
            assert url == archive_url
            return _Response(
                payload={
                    "response": [
                        {
                            "items": [
                                {
                                    "archivos": [
                                        {
                                            "archivo": release_url,
                                            "tipo": "pdf",
                                            "name": "Release",
                                        }
                                    ]
                                }
                            ]
                        }
                    ],
                    "pagetotal": 1,
                },
                url=url,
            )

    assert _extract_quarterly_archive_links(
        _ArchiveSession(),
        "<html></html>",
        page_url,
        10,
        file_pattern=r"resultados.*\.pdf",
        strict_file_pattern=True,
    ) == []
