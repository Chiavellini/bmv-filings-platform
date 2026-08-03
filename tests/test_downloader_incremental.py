"""Incremental/provenance contract for the shared IR downloader."""
from __future__ import annotations


class _Response:
    def __init__(self, text: str, url: str):
        self.text = text
        self.url = url
        self.status_code = 200
        self.headers = {}

    def raise_for_status(self) -> None:
        return None


class _Session:
    headers: dict = {}
    verify = True

    def __init__(self, html: str):
        self.html = html

    def get(self, url: str, **_kwargs):
        return _Response(self.html, url)


def test_incremental_filter_preserves_source_provenance(monkeypatch, tmp_path):
    from src.download import downloader

    html = """
        <a href="/reports/Reporte-Trimestral-1T-2025.pdf">Resultados 1T 2025</a>
        <a href="/reports/Reporte-Trimestral-2T-2025.pdf">Resultados 2T 2025</a>
    """
    downloaded_urls: list[str] = []

    def fake_download(_session, url, dest, **_kwargs):
        downloaded_urls.append(url)
        dest.write_bytes(b"%PDF-1.4\nincremental fixture\n%%EOF")
        return dest

    monkeypatch.setattr(downloader, "_make_session", lambda **_kwargs: _Session(html))
    monkeypatch.setattr(downloader, "_download_pdf", fake_download)

    details: list[downloader.DownloadedPdf] = []
    paths = downloader.download_from_ir(
        "https://issuer.example/quarterlies",
        tmp_path,
        delay_ms=0,
        use_playwright=False,
        exclude_periods={"2025-1T"},
        detail_sink=details,
    )

    assert [path.name for path in paths] == ["Reporte-Trimestral-2T-2025.pdf"]
    assert downloaded_urls == [
        "https://issuer.example/reports/Reporte-Trimestral-2T-2025.pdf"
    ]
    assert details == [
        downloader.DownloadedPdf(
            url=downloaded_urls[0],
            path=paths[0],
            filename=paths[0].name,
            period="2025-2T",
        )
    ]


def test_target_aware_discovery_unions_stale_api_with_static_page(
    monkeypatch,
    tmp_path,
):
    from src.download import downloader

    q1_url = "https://issuer.example/reports/Reporte-Trimestral-1T-2025.pdf"
    q2_url = "https://issuer.example/reports/Reporte-Trimestral-2T-2025.pdf"
    html = f'<a href="{q2_url}">Resultados 2T 2025</a>'
    downloaded_urls: list[str] = []

    def fake_download(_session, url, dest, **_kwargs):
        downloaded_urls.append(url)
        dest.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
        return dest

    monkeypatch.setattr(downloader, "_make_session", lambda **_kwargs: _Session(html))
    monkeypatch.setattr(
        downloader,
        "_fetch_year_api_links",
        lambda *_args, **_kwargs: [q1_url],
    )
    monkeypatch.setattr(downloader, "_download_pdf", fake_download)

    candidates: list[downloader.DiscoveredPdf] = []
    layers: list[downloader.DiscoveryLayerDiagnostic] = []
    downloader.download_from_ir(
        "https://issuer.example/quarterlies",
        tmp_path,
        delay_ms=0,
        use_playwright=False,
        year_api_urls=["https://issuer.example/api/2025"],
        desired_periods={"2025-2T"},
        candidate_sink=candidates,
        layer_sink=layers,
    )

    assert downloaded_urls == [q2_url, q1_url]
    assert {candidate.period for candidate in candidates} == {
        "2025-1T",
        "2025-2T",
    }
    q1 = next(candidate for candidate in candidates if candidate.period == "2025-1T")
    q2 = next(candidate for candidate in candidates if candidate.period == "2025-2T")
    assert q1.layers == ("year_api",)
    assert q2.layers == ("static_crawl",)
    assert {layer.layer for layer in layers} >= {"year_api", "static_crawl"}


def test_target_aware_cap_keeps_newest_periods_from_oldest_first_page(
    monkeypatch,
    tmp_path,
):
    from src.download import downloader

    urls = [
        f"https://issuer.example/reports/Reporte-Trimestral-{quarter}T-2025.pdf"
        for quarter in range(1, 5)
    ]
    html = "".join(
        f'<a href="{url}">Resultados {quarter}T 2025</a>'
        for quarter, url in enumerate(urls, 1)
    )
    downloaded_urls: list[str] = []

    def fake_download(_session, url, dest, **_kwargs):
        downloaded_urls.append(url)
        dest.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
        return dest

    monkeypatch.setattr(downloader, "_make_session", lambda **_kwargs: _Session(html))
    monkeypatch.setattr(downloader, "_download_pdf", fake_download)

    downloader.download_from_ir(
        "https://issuer.example/quarterlies",
        tmp_path,
        delay_ms=0,
        use_playwright=False,
        desired_periods={"2025-4T"},
        max_reports=2,
    )

    assert downloaded_urls == [urls[3], urls[2]]


def test_target_aware_selection_understands_orbia_qn_year_filenames(
    monkeypatch,
    tmp_path,
):
    from src.download import downloader

    q1_url = (
        "https://www.orbia.example/assets/2026/q1/"
        "orbia-q1-2026-earnings-release.pdf"
    )
    q2_url = (
        "https://www.orbia.example/assets/2026/q2/"
        "orbia-q2-2026-earnings-release.pdf"
    )
    html = (
        f'<a href="{q1_url}">English</a>'
        f'<a href="{q2_url}">English</a>'
    )
    downloaded_urls: list[str] = []

    def fake_download(_session, url, dest, **_kwargs):
        downloaded_urls.append(url)
        dest.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
        return dest

    monkeypatch.setattr(downloader, "_make_session", lambda **_kwargs: _Session(html))
    monkeypatch.setattr(downloader, "_download_pdf", fake_download)

    downloader.download_from_ir(
        "https://www.orbia.example/quarterlies",
        tmp_path,
        delay_ms=0,
        use_playwright=False,
        desired_periods={"2026-2T"},
        max_reports=1,
    )

    assert downloaded_urls == [q2_url]


def test_download_failure_sink_preserves_failed_url_and_period(
    monkeypatch,
    tmp_path,
):
    from src.download import downloader

    html = """
        <a href="/reports/Reporte-Trimestral-1T-2025.pdf">Resultados 1T 2025</a>
        <a href="/reports/Reporte-Trimestral-2T-2025.pdf">Resultados 2T 2025</a>
    """

    def fake_download(_session, url, dest, **_kwargs):
        if "1T-2025" in url:
            raise TimeoutError("issuer timed out")
        dest.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
        return dest

    monkeypatch.setattr(downloader, "_make_session", lambda **_kwargs: _Session(html))
    monkeypatch.setattr(downloader, "_download_pdf", fake_download)

    failures: list[downloader.DownloadFailure] = []
    paths = downloader.download_from_ir(
        "https://issuer.example/quarterlies",
        tmp_path,
        delay_ms=0,
        use_playwright=False,
        desired_periods={"2025-1T", "2025-2T"},
        failure_sink=failures,
    )

    assert [path.name for path in paths] == ["Reporte-Trimestral-2T-2025.pdf"]
    assert failures == [
        downloader.DownloadFailure(
            url=(
                "https://issuer.example/reports/"
                "Reporte-Trimestral-1T-2025.pdf"
            ),
            period="2025-1T",
            error="TimeoutError: issuer timed out",
        )
    ]


def test_pdf_candidate_preserves_spaces_in_navigable_href():
    from src.download.downloader import _extract_pdf_candidates

    html = (
        '<a href="/storage/informes/2026/trimestral/2T/mx/'
        'Q - Reporte Trimestral 2T26 VFF2.pdf">Descargar</a>'
    )

    candidates = _extract_pdf_candidates(
        html,
        "https://qinversionistas.qualitas.com.mx/ES/reportes-trimestrales",
    )

    assert [candidate.url for candidate in candidates] == [
        "https://qinversionistas.qualitas.com.mx/storage/informes/2026/"
        "trimestral/2T/mx/Q - Reporte Trimestral 2T26 VFF2.pdf"
    ]


def test_template_downloader_uses_configured_browser_tls_profile(monkeypatch, tmp_path):
    from src.download import downloader

    session = _Session("")

    def fake_download(observed_session, _url, dest, **_kwargs):
        assert observed_session is session
        assert observed_session._impersonate_profile == "safari"
        dest.write_bytes(b"%PDF-1.4\nfixture\n%%EOF")
        return dest

    monkeypatch.setattr(downloader, "_make_session", lambda *_args, **_kwargs: session)
    monkeypatch.setattr(
        downloader,
        "_verify_pdf_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("requests-only probe must be skipped for impersonated fetches")
        ),
    )
    monkeypatch.setattr(downloader, "_download_pdf", fake_download)

    paths = downloader.download_from_url_templates(
        ["https://issuer.example/{year}/{quarter}Q{year2}.pdf"],
        tmp_path,
        {"2026-2T"},
        delay_ms=0,
        impersonate="safari",
    )

    assert [path.name for path in paths] == ["2026-2T.pdf"]


def test_liverpool_xbrl_named_pdf_is_a_quarterly_report():
    from src.download.downloader import _PdfAnchor, _select_pdf_links

    url = (
        "https://www.elpuertodeliverpool.mx/docs/informes-trimestrales/"
        "2026/2TXBRL2026.pdf"
    )
    anchor = _PdfAnchor(
        url=url,
        filename="2TXBRL2026.pdf",
        text="2do. Trimestre",
    )

    assert _select_pdf_links([anchor], pattern=None) == [url]
