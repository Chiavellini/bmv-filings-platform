"""Tests for the universal IR-downloader layers added for the 6-site fix.

Covers: the dual-signal quarterly filter regex matrix, period-label inference,
extension-less candidate extraction (Liferay/Actinver), PDF content verification,
static year-page enumeration/crawling, layer diagnostics, and end-to-end fixture
runs through download_from_ir with Playwright disabled.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import requests

FIXTURES = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _anchor(url: str, text: str = ""):
    from src.download.downloader import _PdfAnchor

    filename = Path(url.split("?")[0]).name
    return _PdfAnchor(url=url, filename=filename, text=text)


# ---------------------------------------------------------------------------
# Quarterly filter regex matrix
# ---------------------------------------------------------------------------

QUARTERLY_POSITIVES = [
    # Chedraui: hyphen between quarter and 4-digit year
    ("https://inversionistas.grupochedraui.com.mx/wp-content/uploads/2026/04/Reporte-BMV-CHDRAUI-1T-2026.pdf", "Q1"),
    ("https://inversionistas.grupochedraui.com.mx/wp-content/uploads/2026/02/Reporte-BMV-4T-2025-Dictaminado.pdf", "Q4 Dictaminado"),
    # GMexico: year-then-quarter with underscore; report signal from URL path
    ("https://www.gmexico.com/GMDocs/ReportesFinancieros/Esp/2026/RF_ES_2026_1T.pdf", "1T"),
    # Peñoles: quarter+4-digit-year glued, BMV token
    ("https://www.penoles.com.mx/media/IPSAB_BMV_1T2026_USD.pdf", "Reporte BMV 1T 2026"),
    # Alsea: quarter signal only in the anchor text
    ("https://www.alsea.net/uploads/es/documents/reports_quarterly/260428_RESULTADOS_ALSEA_1T.pdf", "Resultados 1T26"),
    ("https://www.alsea.net/uploads/es/documents/reports_quarterly/alsea_resultados_3T25.pdf", "Resultados 3T25"),
    # Actinver: extension-less Liferay slug, 'notas' report signal
    ("https://actinver.com/documents/d/actinver/notas_grupo_financiero_actinver_1t25", "Notas"),
    # Herdez/Soriana archive names observed on live IR pages.
    ("https://grupoherdez.com.mx/api/media/FIRST_QUARTER_2026_Grupo_Herdez_edffa6b129.pdf", "Read PDF"),
    ("https://grupoherdez.com.mx/api/media/1_TD_2026_GH_020426_78df86095a.pdf", "Read PDF"),
    # Liverpool uses XBRL in the filename for its human-readable report PDF.
    ("https://www.elpuertodeliverpool.mx/docs/informes-trimestrales/2026/2TXBRL2026.pdf", "Read PDF"),
    ("https://www.organizacionsoriana.com/pdf/reportes/2023/3Q23InfoDir_ingl%C3%A9sV3%20-%20VF.pdf", ""),
    ("https://www.organizacionsoriana.com/pdf/reportes/2022/Informe%20del%20Director%202Q22_ENG.pdf", ""),
]

QUARTERLY_NEGATIVES = [
    ("https://inversionistas.grupochedraui.com.mx/wp-content/uploads/2026/04/Informe-Anual-2025.pdf", "Informe Anual 2025"),
    ("https://example.com/reportes/1T-2026-webcast.pdf", "Webcast 1T 2026"),
    ("https://example.com/reportes/presentacion_4T25.pdf", "Presentación 4T25"),
    ("https://example.com/reportes/2026.pdf", "2026"),
    ("https://www.penoles.com.mx/media/Pen_IA25-ESP.pdf", "Informe Anual 2025"),
    ("https://www.gmexico.com/GMDocs/ReportesFinancieros/Presentaciones/1Q26_GM_Presentation_Results.pdf", "Presentación 1T26"),
    # Liferay junk: no quarter signal anywhere
    ("https://actinver.com/documents/d/actinver/esf-11", "Estado de Situación Financiera"),
    ("https://actinver.com/documents/d/actinver/terminos-y-condiciones-tdf", "Términos y condiciones Divisa Flex"),
    # Herdez attachments: period-looking but not quarterly result PDFs.
    ("https://grupoherdez.com.mx/api/media/Transcript_GH_Conference_Call_4_Q25_6ea41730f4.pdf", "Read PDF"),
    ("https://grupoherdez.com.mx/api/media/ifrsxbrl_HERDEZ_2022_4_dictamen_d4ae809e69.pdf", "Read PDF"),
    ("https://grupoherdez.com.mx/api/media/2_T14_Results_to_the_MSE_XBR_Spanish_only_0d8fcff3a5.pdf", "Read PDF"),
]


@pytest.mark.parametrize("url,text", QUARTERLY_POSITIVES)
def test_quarterly_filter_accepts(url, text):
    from src.download.downloader import _looks_like_quarterly_report

    assert _looks_like_quarterly_report(_anchor(url, text)), url


@pytest.mark.parametrize("url,text", QUARTERLY_NEGATIVES)
def test_quarterly_filter_rejects(url, text):
    from src.download.downloader import _looks_like_quarterly_report

    assert not _looks_like_quarterly_report(_anchor(url, text)), url


def test_explain_rejection_reasons():
    from src.download.downloader import _explain_rejection

    excluded = _explain_rejection(_anchor("https://x.mx/Informe-Anual-2025.pdf", "Informe Anual"))
    assert excluded.startswith("excluded")

    no_quarter = _explain_rejection(_anchor("https://x.mx/reporte_resultados.pdf", "Resultados"))
    assert "quarter signal" in no_quarter

    passes = _explain_rejection(_anchor("https://x.mx/Reporte-BMV-1T-2026.pdf", "Q1"))
    assert passes == "passes quarterly filter"


def test_period_fast_path_rejects_herdez_non_report_attachments():
    from src.download.downloader import _select_pdf_links

    anchors = [
        _anchor("https://grupoherdez.com.mx/api/media/Transcript_GH_Conference_Call_4_Q25_6ea41730f4.pdf", "Read PDF"),
        _anchor("https://grupoherdez.com.mx/api/media/ifrsxbrl_HERDEZ_2022_4_dictamen_d4ae809e69.pdf", "Read PDF"),
        _anchor("https://grupoherdez.com.mx/api/media/FOURTH_QUARTER_AND_YEAR_2025_RESULTS_GRUPO_HERDEZ.pdf", "Read PDF"),
    ]

    assert _select_pdf_links(anchors, None) == [
        "https://grupoherdez.com.mx/api/media/FOURTH_QUARTER_AND_YEAR_2025_RESULTS_GRUPO_HERDEZ.pdf"
    ]


# ---------------------------------------------------------------------------
# Period-label inference (report_index)
# ---------------------------------------------------------------------------

PERIOD_CASES = [
    ("Reporte-BMV-CHDRAUI-1T-2026", "2026-1T"),
    ("Reporte-BMV-4T-2025-Dictaminado", "2025-4T"),
    ("RF_ES_2026_1T", "2026-1T"),
    ("IPSAB_BMV_1T2026_USD", "2026-1T"),
    ("alsea_resultados_3T25", "2025-3T"),
    ("reporte_trimestral_4T2025_BMV", "2025-4T"),
    ("notas_grupo_financiero_actinver_1t25", "2025-1T"),
    ("Walmex_1Q26_Release", "2026-1T"),
    ("FIRST_QUARTER_2026_Grupo_Herdez_edffa6b129", "2026-1T"),
    ("FOURTH_QUARTER_AND_YEAR_2025_RESULTS_GRUPO_HERDEZ", "2025-4T"),
    ("1_TD_2026_GH_020426_78df86095a", "2026-1T"),
    ("3Q23InfoDir_inglésV3_VF", "2023-3T"),
    ("Informe-Anual-2025", "2025-FY"),   # annual reports now resolve to a YYYY-FY period
    ("Pen_IA25-ESP", None),              # abbreviated "IA25" carries no annual keyword/4-digit year
    ("2026", None),
]


@pytest.mark.parametrize("stem,expected", PERIOD_CASES)
def test_infer_period_label(stem, expected):
    from src.shared.report_index import infer_period_label

    assert infer_period_label(stem) == expected


# ---------------------------------------------------------------------------
# Extension-less candidate extraction (Actinver/Liferay fixture)
# ---------------------------------------------------------------------------

ACTINVER_URL = "https://actinver.com/inversionistas-informacionfinanciera-grupofinanciero-2026"


def test_extensionless_extraction_keeps_only_quarterly_docs():
    from src.download.downloader import _extract_extensionless_candidates

    html = _read_fixture("actinver_liferay.html")
    anchors = _extract_extensionless_candidates(html, ACTINVER_URL)
    urls = {a.url for a in anchors}

    assert (
        "https://webserver-actinver-prd.lfr.cloud/documents/d/actinver/reporte-trimestral-corporacion-actinver-1t26"
        in urls
    )
    assert "https://actinver.com/documents/d/actinver/notas_grupo_financiero_actinver_1t26" in urls
    # CMS junk must never surface: no quarter signal on these
    assert not any("esf-11" in u for u in urls)
    assert not any("terminos" in u for u in urls)
    # .pdf links belong to _extract_pdf_candidates, not this extractor
    assert not any(u.lower().endswith(".pdf") for u in urls)


# ---------------------------------------------------------------------------
# _verify_pdf_url (HEAD content-type, ranged-GET magic fallback)
# ---------------------------------------------------------------------------

class _HeadResp:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _RangedResp:
    def __init__(self, status_code=206, chunk=b""):
        self.status_code = status_code
        self._chunk = chunk

    def iter_content(self, chunk_size=1024):
        return iter([self._chunk])

    def close(self):
        return None


class _VerifySession:
    def __init__(self, head_resp=None, head_exc=None, get_resp=None):
        self.head_resp = head_resp
        self.head_exc = head_exc
        self.get_resp = get_resp
        self.get_called = False

    def head(self, url, timeout=None, verify=None, allow_redirects=True):
        if self.head_exc is not None:
            raise self.head_exc
        return self.head_resp

    def get(self, url, timeout=None, verify=None, headers=None, stream=False):
        self.get_called = True
        if self.get_resp is None:
            raise AssertionError("unexpected GET fallback")
        return self.get_resp


def test_verify_pdf_url_head_pdf():
    from src.download.downloader import _verify_pdf_url

    session = _VerifySession(head_resp=_HeadResp(headers={"Content-Type": "application/pdf"}))
    assert _verify_pdf_url(session, "https://x.mx/documents/d/site/doc") is True
    assert not session.get_called


def test_verify_pdf_url_head_html_rejected_without_fallback():
    from src.download.downloader import _verify_pdf_url

    session = _VerifySession(head_resp=_HeadResp(headers={"Content-Type": "text/html; charset=utf-8"}))
    assert _verify_pdf_url(session, "https://x.mx/documents/d/site/doc") is False
    assert not session.get_called


def test_verify_pdf_url_octet_stream_with_pdf_disposition():
    from src.download.downloader import _verify_pdf_url

    session = _VerifySession(head_resp=_HeadResp(headers={
        "Content-Type": "application/octet-stream",
        "Content-Disposition": 'attachment; filename="reporte_1t26.pdf"',
    }))
    assert _verify_pdf_url(session, "https://x.mx/documents/d/site/doc") is True


def test_verify_pdf_url_falls_back_to_magic_bytes():
    from src.download.downloader import _verify_pdf_url

    session = _VerifySession(
        head_exc=requests.exceptions.ReadTimeout("HEAD not supported"),
        get_resp=_RangedResp(chunk=b"%PDF-1.7 rest-of-file"),
    )
    assert _verify_pdf_url(session, "https://x.mx/documents/d/site/doc") is True
    assert session.get_called


def test_verify_pdf_url_magic_bytes_mismatch():
    from src.download.downloader import _verify_pdf_url

    session = _VerifySession(
        head_resp=_HeadResp(headers={"Content-Type": "application/octet-stream"}),
        get_resp=_RangedResp(chunk=b"<html><body>login</body></html>"),
    )
    assert _verify_pdf_url(session, "https://x.mx/documents/d/site/doc") is False


# ---------------------------------------------------------------------------
# Year-page enumeration + crawl (Actinver URL-valued select, Peñoles plain select)
# ---------------------------------------------------------------------------

def test_enumerate_year_pages_from_url_valued_select():
    from src.download.downloader import _enumerate_year_page_urls

    html = _read_fixture("actinver_liferay.html")
    urls = _enumerate_year_page_urls(html, ACTINVER_URL)
    assert urls == [
        "https://actinver.com/inversionistas-informacionfinanciera-grupofinanciero-2025",
        "https://actinver.com/inversionistas-informacionfinanciera-grupofinanciero-2024",
    ]


def test_enumerate_year_pages_plain_select_has_no_urls():
    from src.download.downloader import _enumerate_year_page_urls

    # Peñoles: option values are bare years (JS loads content), page URL has no year
    # token to swap — nothing to enumerate statically.
    html = _read_fixture("penoles_year_select.html")
    urls = _enumerate_year_page_urls(html, "https://www.penoles.com.mx/inversionistas/reportes-y-presentaciones/")
    assert urls == []


class _FakeResponse:
    def __init__(self, text="", url=""):
        self.text = text
        self.url = url

    def raise_for_status(self):
        return None


class _MapSession:
    """Session serving canned HTML per URL; raises for anything unexpected."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.requested: list = []

    def get(self, url, params=None, timeout=None, stream=False, verify=None, headers=None):
        self.requested.append(url)
        if url in self.pages:
            return _FakeResponse(text=self.pages[url], url=url)
        raise AssertionError(f"Unexpected URL fetched: {url}")


def test_crawl_year_variant_pages_combines_years_strictly():
    from src.download.downloader import _crawl_year_variant_pages

    main_html = _read_fixture("actinver_liferay.html")
    session = _MapSession({
        "https://actinver.com/inversionistas-informacionfinanciera-grupofinanciero-2025":
            _read_fixture("actinver_liferay_2025.html"),
        "https://actinver.com/inversionistas-informacionfinanciera-grupofinanciero-2024":
            "<html><body><p>Próximamente</p></body></html>",
    })

    diag: list = []
    links = _crawl_year_variant_pages(
        session, main_html, ACTINVER_URL, verify_ssl=True, delay_ms=0, diag=diag,
    )

    assert set(links) == {
        "https://webserver-actinver-prd.lfr.cloud/documents/d/actinver/reporte-trimestral-corporacion-actinver-1t26",
        "https://actinver.com/documents/d/actinver/notas_grupo_financiero_actinver_1t26",
        "https://actinver.com/documents/d/actinver/notas_grupo_financiero_actinver_1t25",
        "https://actinver.com/documents/d/actinver/notas_grupo_financiero_actinver_2t25",
        "https://actinver.com/documents/d/actinver/notas_grupo_financiero_actinver_3t25",
        "https://actinver.com/documents/d/actinver/notas_grupo_financiero_actinver_4t25",
    }
    assert any("2 year page" in d for d in diag)
    assert len(session.requested) == 2


def test_crawl_year_variant_pages_no_navigation_reports_diag():
    from src.download.downloader import _crawl_year_variant_pages

    html = _read_fixture("alsea_webflow.html")
    session = _MapSession({})
    diag: list = []
    links = _crawl_year_variant_pages(
        session, html, "https://www.alsea.net/inversionistas.html",
        verify_ssl=True, delay_ms=0, diag=diag,
    )
    assert links == []
    assert any("no year-navigation" in d for d in diag)


# ---------------------------------------------------------------------------
# End-to-end fixture runs through download_from_ir (Playwright disabled)
# ---------------------------------------------------------------------------

def _run_download(monkeypatch, tmp_path, html: str, page_url: str, extra_pages: dict | None = None):
    from src.download import downloader

    pages = {page_url: html}
    pages.update(extra_pages or {})
    session = _MapSession(pages)
    downloaded_urls: list = []

    def fake_download(sess, url, dest, verify_ssl=True, referer=None, **kwargs):
        downloaded_urls.append(url)
        dest.write_bytes(b"%PDF-1.4 fake")
        return dest

    monkeypatch.setattr(downloader, "_make_session", lambda **kwargs: session)
    monkeypatch.setattr(downloader, "_download_pdf", fake_download)

    paths = downloader.download_from_ir(
        page_url, tmp_path, delay_ms=0, use_playwright=False,
    )
    return paths, downloaded_urls, session


def test_e2e_chedraui_fixture(monkeypatch, tmp_path):
    from src.shared.report_index import infer_period_label

    html = _read_fixture("chedraui_table.html")
    paths, urls, _ = _run_download(
        monkeypatch, tmp_path, html,
        "https://inversionistas.grupochedraui.com.mx/reportes-trimestrales-bmv/",
    )

    names = {p.name for p in paths}
    assert len(paths) == 7
    assert "Reporte-BMV-CHDRAUI-1T-2026.pdf" in names
    assert "Reporte-BMV-4T-2025-Dictaminado.pdf" in names
    assert not any("Informe-Anual" in n for n in names)
    assert not any("Presentacion" in n for n in names)

    periods = {infer_period_label(p.stem) for p in paths}
    assert periods == {"2026-1T", "2025-1T", "2025-2T", "2025-4T", "2024-1T", "2024-2T", "2024-4T"}


def test_e2e_gmexico_fixture(monkeypatch, tmp_path):
    from src.shared.report_index import infer_period_label

    html = _read_fixture("gmexico_links.html")
    paths, urls, _ = _run_download(
        monkeypatch, tmp_path, html, "https://www.gmexico.com/reportes-financieros/",
    )

    assert len(paths) == 5
    assert all("RF_ES_" in p.name for p in paths)
    assert not any("Presentation" in u for u in urls)

    periods = {infer_period_label(p.stem) for p in paths}
    assert periods == {"2026-1T", "2025-1T", "2025-2T", "2025-3T", "2025-4T"}


def test_e2e_alsea_fixture(monkeypatch, tmp_path):
    html = _read_fixture("alsea_webflow.html")
    paths, urls, _ = _run_download(
        monkeypatch, tmp_path, html, "https://www.alsea.net/inversionistas.html",
    )

    names = {p.name for p in paths}
    assert names == {
        "260428_RESULTADOS_ALSEA_1T.pdf",
        "alsea_resultados_3T25.pdf",
        "reporte_trimestral_4T2025_BMV.pdf",
    }
    assert not any("ia26" in n for n in names)


def test_e2e_sparse_results_print_layer_diagnostics(monkeypatch, tmp_path, capsys):
    html = """
    <html><body>
      <a href="https://x.mx/uploads/Reporte-BMV-1T-2026.pdf">Q1 2026</a>
      <a href="https://x.mx/uploads/Reporte-BMV-4T-2025.pdf">Q4 2025</a>
      <a href="https://x.mx/uploads/Presentacion-Corporativa-1T-2026.pdf">Presentación</a>
    </body></html>
    """
    paths, urls, _ = _run_download(monkeypatch, tmp_path, html, "https://x.mx/inversionistas/")

    assert len(paths) == 2  # sparse (<3) triggers diagnostics
    err = capsys.readouterr().err
    assert "Discovery layer summary:" in err
    assert "candidates=" in err
    assert "Presentacion-Corporativa-1T-2026.pdf" in err
    assert "excluded" in err
