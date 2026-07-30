from __future__ import annotations

import json


class _FakeResponse:
    def __init__(self, *, text: str = "", json_data=None, url: str = ""):
        self.text = text
        self._json_data = json_data
        self.url = url

    def raise_for_status(self):
        return None

    def json(self):
        if self._json_data is None:
            raise ValueError("No JSON payload")
        return self._json_data


class _FakeSession:
    def __init__(self, html: str, archive_pages: dict[int, dict] | None = None):
        self.html = html
        self.archive_pages = archive_pages or {}
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None, stream=False, verify=None, headers=None):
        self.calls.append({
            "url": url,
            "params": params,
            "stream": stream,
            "headers": headers,
        })
        if "Code/snippets/rest/Informacion" in url:
            page = int((params or {}).get("page", 1))
            return _FakeResponse(json_data=self.archive_pages.get(page, {"response": []}), url=url)
        if params is None:
            return _FakeResponse(text=self.html, url=url)
        raise AssertionError(f"Unexpected URL: {url}")


def test_download_from_ir_prefers_archive_feed_and_paginates(tmp_path, monkeypatch):
    from src.download.downloader import download_from_ir

    html = """
    <html><body>
      <form class="js-filter js-multiple" method="get" action="Code/snippets/rest/Informacion">
        <input name="action" type="hidden" value="getInfoTrimestral">
        <input class="js-page" name="page" type="hidden" value="1">
        <input name="lang" type="hidden" value="en">
      </form>
      <a href="https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release.pdf">Release</a>
      <a href="https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_Webcast_1Q26.pdf">Webcast</a>
    </body></html>
    """

    archive_pages = {
        1: {
            "success": True,
            "response": [
                {
                    "year": "2026",
                    "items": [
                        {
                            "archivos": [
                                {
                                    "archivo": "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release.pdf ",
                                    "tipo": "pdf",
                                    "name": "Release",
                                },
                                {
                                    "archivo": "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_Webcast_1Q26.pdf",
                                    "tipo": "pdf",
                                    "name": "Webcast",
                                },
                            ]
                        },
                        {
                            "archivos": [
                                {
                                    "archivo": "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release_Alt.pdf",
                                    "tipo": "pdf",
                                    "name": "Release",
                                }
                            ]
                        },
                    ],
                }
            ],
            "pagetotal": 3,
        },
        2: {
            "success": True,
            "response": [
                {
                    "year": "2025",
                    "items": [
                        {
                            "archivos": [
                                {
                                    "archivo": "https://files.walmex.mx/upload/files/2025/EN/Quarterly/4Q25/Walmex_Earnings_Release_4Q25.pdf",
                                    "tipo": "pdf",
                                    "name": "Release",
                                }
                            ]
                        },
                        {
                            "archivos": [
                                {
                                    "archivo": "https://files.walmex.mx/upload/files/2025/EN/Quarterly/4Q25/Walmex_Earnings_Release_4Q25.pdf",
                                    "tipo": "pdf",
                                    "name": "Release",
                                }
                            ]
                        },
                    ],
                }
            ],
            "pagetotal": 3,
        },
        3: {"success": True, "response": [], "pagetotal": 3},
    }

    fake_session = _FakeSession(html, archive_pages)
    downloaded_urls: list[str] = []

    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: fake_session)
    monkeypatch.setattr(
        "src.download.downloader._download_pdf",
        lambda session, url, dest, **kw: downloaded_urls.append(url) or dest,
    )

    paths = download_from_ir(
        "https://www.walmex.mx/en/financial-information/quarterly.html",
        tmp_path,
        max_reports=10,
        file_pattern=r"(?:reporte|report|trimest|quarter|result|earning).*\.pdf",
        delay_ms=0,
    )

    assert downloaded_urls == [
        "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release.pdf",
        "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release_Alt.pdf",
        "https://files.walmex.mx/upload/files/2025/EN/Quarterly/4Q25/Walmex_Earnings_Release_4Q25.pdf",
    ]
    assert [p.name for p in paths] == [
        "Walmex_1Q26_Release.pdf",
        "Walmex_1Q26_Release_Alt.pdf",
        "Walmex_Earnings_Release_4Q25.pdf",
    ]
    assert fake_session.calls[1]["url"] == "https://www.walmex.mx/Code/snippets/rest/Informacion"


def test_download_from_ir_uses_walmex_archive_fallback_without_form(tmp_path, monkeypatch):
    from src.download.downloader import download_from_ir

    html = """
    <html><body>
      <a href="https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release.pdf">Release</a>
      <a href="https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_Webcast_1Q26.pdf">Webcast</a>
    </body></html>
    """

    archive_pages = {
        1: {
            "success": True,
            "response": [
                {
                    "year": "2026",
                    "items": [
                        {
                            "archivos": [
                                {
                                    "archivo": "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release.pdf ",
                                    "tipo": "pdf",
                                    "name": "Release",
                                },
                                {
                                    "archivo": "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/WALMEX_1Q26_MSE.pdf",
                                    "tipo": "pdf",
                                    "name": "Information to the MSE",
                                },
                            ]
                        }
                    ],
                }
            ],
        },
        2: {"success": False, "response": []},
    }

    fake_session = _FakeSession(html, archive_pages)
    downloaded_urls: list[str] = []

    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: fake_session)
    monkeypatch.setattr(
        "src.download.downloader._download_pdf",
        lambda session, url, dest, **kw: downloaded_urls.append(url) or dest,
    )

    paths = download_from_ir(
        "https://www.walmex.mx/en/financial-information/quarterly.html",
        tmp_path,
        max_reports=10,
        delay_ms=0,
    )

    assert downloaded_urls == [
        "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release.pdf",
    ]
    assert [p.name for p in paths] == ["Walmex_1Q26_Release.pdf"]
    assert fake_session.calls[1]["url"] == "https://www.walmex.mx/Code/snippets/rest/Informacion"


def test_download_from_ir_falls_back_to_html_links(tmp_path, monkeypatch):
    from src.download.downloader import download_from_ir

    html = """
    <html><body>
      <a href="https://files.example.com/report.pdf">Release</a>
      <a href="https://files.example.com/webcast.pdf">Webcast</a>
    </body></html>
    """

    fake_session = _FakeSession(html)
    downloaded_urls: list[str] = []

    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: fake_session)
    monkeypatch.setattr(
        "src.download.downloader._download_pdf",
        lambda session, url, dest, **kw: downloaded_urls.append(url) or dest,
    )

    paths = download_from_ir(
        "https://www.example.com/ir.html",
        tmp_path,
        max_reports=10,
        file_pattern=r"(?:release|report).*\.pdf",
        delay_ms=0,
    )

    assert downloaded_urls == ["https://files.example.com/report.pdf"]
    assert [p.name for p in paths] == ["report.pdf"]


def test_download_from_ir_crawls_paginated_report_pages(tmp_path, monkeypatch):
    from src.download.downloader import download_from_ir

    def make_page(page_num: int, total_pages: int = 4) -> str:
        pdfs = []
        start = (page_num - 1) * 10
        for i in range(10):
            idx = start + i + 1
            pdfs.append(
                f'<a href="/investors/reports/report-{idx}.pdf">Reporte {idx}</a>'
            )
        next_link = ""
        if page_num < total_pages:
            next_link = f'<a rel="next" href="?page={page_num + 1}">Siguiente</a>'
        return f"<html><body>{''.join(pdfs)}{next_link}</body></html>"

    pages = {
        1: make_page(1),
        2: make_page(2),
        3: make_page(3),
        4: make_page(4),
    }

    class _PagedSession:
        headers = {}
        verify = True

        def __init__(self):
            self.calls = []

        def get(self, url, params=None, timeout=None, stream=False, verify=None, headers=None):
            from urllib.parse import parse_qs, urlparse

            self.calls.append({
                "url": url,
                "params": params,
                "stream": stream,
                "headers": headers,
            })
            parsed = urlparse(url)
            query = params or parse_qs(parsed.query)
            page = int((query.get("page") or [1])[0])
            if page not in pages:
                raise AssertionError(f"Unexpected page: {page} ({url})")
            return _FakeResponse(text=pages[page], url=url)

    fake_session = _PagedSession()
    downloaded_urls: list[str] = []

    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: fake_session)
    monkeypatch.setattr(
        "src.download.downloader._download_pdf",
        lambda session, url, dest, **kw: downloaded_urls.append(url) or dest,
    )

    paths = download_from_ir(
        "https://example.com/investors/reportes/",
        tmp_path,
        max_reports=50,
        file_pattern=r".*\.pdf",
        delay_ms=0,
    )

    assert len(downloaded_urls) == 40
    assert downloaded_urls[0].endswith("report-1.pdf")
    assert downloaded_urls[-1].endswith("report-40.pdf")
    assert len(paths) == 40


def test_extract_pagination_links_recognizes_load_more_links():
    from src.download.downloader import _extract_pagination_links

    html = """
    <html><body>
      <nav class="pagination">
        <a class="load-more" href="?page=2">Load more</a>
      </nav>
    </body></html>
    """

    links = _extract_pagination_links(
        html,
        "https://example.com/investors/reports/",
        "https://example.com/investors/reports/",
    )

    assert links == ["https://example.com/investors/reports/?page=2"]


def test_extract_pdf_links_keeps_all_lacomer_quarterly_bmv_links():
    from src.download.downloader import _extract_pdf_links

    html = """
    <html><body>
      <a href="/wp-content/uploads/BMV_1T26.pdf"></a>
      <a href="/wp-content/uploads/1T24bmv.pdf"></a>
      <a href="/wp-content/uploads/4_Trimestre_2023.pdf"></a>
      <a href="/wp-content/uploads/La-Comer-2do-Trimestre-2022.pdf"></a>
      <a href="/wp-content/uploads/3t19bmv.pdf"></a>
      <a href="/wp-content/uploads/4t17_bmv.pdf"></a>
      <a href="/wp-content/uploads/1T26-webcast.pdf">Webcast</a>
      <a href="/wp-content/uploads/1T26-transcript.pdf">Transcript</a>
      <a href="/wp-content/uploads/1T26-infografia.pdf">Infografía</a>
    </body></html>
    """

    links = _extract_pdf_links(
        html,
        "https://lacomerfinanzas.com.mx/informacion-financiera/reportes-trimestrales-bmv/",
        None,
    )

    assert links == [
        "https://lacomerfinanzas.com.mx/wp-content/uploads/BMV_1T26.pdf",
        "https://lacomerfinanzas.com.mx/wp-content/uploads/1T24bmv.pdf",
        "https://lacomerfinanzas.com.mx/wp-content/uploads/4_Trimestre_2023.pdf",
        "https://lacomerfinanzas.com.mx/wp-content/uploads/La-Comer-2do-Trimestre-2022.pdf",
        "https://lacomerfinanzas.com.mx/wp-content/uploads/3t19bmv.pdf",
        "https://lacomerfinanzas.com.mx/wp-content/uploads/4t17_bmv.pdf",
    ]


def test_extract_pdf_links_uses_base_href_and_non_anchor_urls():
    from src.download.downloader import _extract_pdf_links

    html = """
    <html>
      <head>
        <base href="https://example.com/investors/releases/">
      </head>
      <body>
        <div data-pdf="q1_2026_release.pdf">Release</div>
        <script>
          window.reportUrl = "/investors/releases/q2_2026_release.pdf";
        </script>
      </body>
    </html>
    """

    links = _extract_pdf_links(html, "https://example.com/investors/", r".*release.*\.pdf")

    assert links == [
        "https://example.com/investors/releases/q1_2026_release.pdf",
        "https://example.com/investors/releases/q2_2026_release.pdf",
    ]


def test_download_from_ir_keeps_partial_archive_results_when_later_page_fails(tmp_path, monkeypatch):
    from src.download.downloader import download_from_ir

    html = """
    <html><body>
      <form class="js-filter js-multiple" method="get" action="Code/snippets/rest/Informacion">
        <input name="action" type="hidden" value="getInfoTrimestral">
        <input class="js-page" name="page" type="hidden" value="1">
        <input name="lang" type="hidden" value="en">
      </form>
      <a href="https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release.pdf">Release</a>
    </body></html>
    """

    archive_pages = {
        1: {
            "success": True,
            "pagetotal": 3,
            "response": [
                {
                    "items": [
                        {
                            "archivos": [
                                {
                                    "archivo": "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release.pdf",
                                    "tipo": "pdf",
                                    "name": "Release",
                                }
                            ]
                        }
                    ]
                }
            ],
        },
        2: None,
    }

    fake_session = _FakeSession(html, archive_pages)
    downloaded_urls: list[str] = []

    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: fake_session)
    monkeypatch.setattr(
        "src.download.downloader._download_pdf",
        lambda session, url, dest, **kw: downloaded_urls.append(url) or dest,
    )

    paths = download_from_ir(
        "https://www.walmex.mx/en/financial-information/quarterly.html",
        tmp_path,
        max_reports=10,
        file_pattern=r"(?:reporte|report|trimest|quarter|result|earning).*\.pdf",
        delay_ms=0,
    )

    assert downloaded_urls == [
        "https://files.walmex.mx/upload/files/2026/EN/Quarterly/1Q26/Walmex_1Q26_Release.pdf",
    ]
    assert [p.name for p in paths] == ["Walmex_1Q26_Release.pdf"]


def test_slugify_url_uses_path_context_for_collisions():
    from src.download.downloader import _slugify_url

    used_names: set[str] = set()
    first = _slugify_url("https://files.example.com/q1/report.pdf", used_names)
    second = _slugify_url("https://files.example.com/q2/report.pdf", used_names)

    assert first == "report.pdf"
    assert second != first
    assert "q2" in second
    assert second.endswith(".pdf")


def test_download_from_ir_falls_back_to_curl_on_tls_errors(tmp_path, monkeypatch):
    import requests
    from pathlib import Path
    from src.download.downloader import download_from_ir

    html = """
    <html><body>
      <a href="https://files.example.com/report.pdf">Release</a>
    </body></html>
    """

    class _TlsSession:
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html",
            "Accept-Language": "en-US,en;q=0.9",
        }
        verify = True

        def get(self, *args, **kwargs):
            raise requests.exceptions.SSLError("TLSV1_ALERT_PROTOCOL_VERSION")

    class _Completed:
        def __init__(self, url: str, status: int = 200):
            self.stdout = f"{url}\n{status}"
            self.stderr = ""
            self.returncode = 0

    def fake_run(cmd, check, capture_output, text):
        out_path = Path(cmd[cmd.index("-o") + 1])
        url = cmd[-1]
        if url.endswith(".pdf"):
            out_path.write_bytes(b"%PDF-1.4\n%test\n")
        else:
            out_path.write_text(html, encoding="utf-8")
        return _Completed(url)

    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: _TlsSession())
    monkeypatch.setattr("src.download.downloader.shutil.which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr("src.download.downloader.subprocess.run", fake_run)

    paths = download_from_ir(
        "https://example.com/ir/",
        tmp_path,
        max_reports=1,
        delay_ms=0,
        file_pattern=r".*report.*\.pdf",
    )

    assert [p.name for p in paths] == ["report.pdf"]
    assert paths[0].read_bytes().startswith(b"%PDF-")


def test_download_from_ir_does_not_mask_non_tls_errors(tmp_path, monkeypatch):
    import requests
    import pytest
    from src.download.downloader import download_from_ir

    class _BadSession:
        headers = {}
        verify = True

        def get(self, *args, **kwargs):
            raise requests.exceptions.RequestException("HTTP 500")

    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: _BadSession())
    monkeypatch.setattr("src.download.downloader.shutil.which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(
        "src.download.downloader.subprocess.run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("curl should not run")),
    )

    with pytest.raises(RuntimeError, match="Failed to fetch IR page"):
        download_from_ir("https://example.com/ir/", tmp_path, delay_ms=0)


def test_extract_nextjs_links_finds_pdfs_in_next_data():
    from src.download.downloader import _extract_nextjs_links

    next_data = json.dumps({
        "props": {
            "pageProps": {
                "reports": [
                    {"quarter": "1T26", "file": "/api/media/RESULTADOS_1T26/RESULTADOS_1T26.pdf"},
                    {"quarter": "4T25", "file": "/api/media/RESULTADOS_4T25/RESULTADOS_4T25.pdf"},
                    {"quarter": "3T25", "file": "/api/media/RESULTADOS_3T25/RESULTADOS_3T25.pdf"},
                ]
            }
        },
        "buildId": "test-build-id-123",
    })
    html = f'<html><head><script id="__NEXT_DATA__" type="application/json">{next_data}</script></head></html>'

    anchors = _extract_nextjs_links(html, "https://grupoherdez.com.mx/")
    urls = [a.url for a in anchors]

    assert len(urls) == 3
    assert "https://grupoherdez.com.mx/api/media/RESULTADOS_1T26/RESULTADOS_1T26.pdf" in urls
    assert "https://grupoherdez.com.mx/api/media/RESULTADOS_4T25/RESULTADOS_4T25.pdf" in urls
    assert all(a.filename.endswith(".pdf") for a in anchors)


def test_extract_nextjs_links_returns_empty_when_no_next_data():
    from src.download.downloader import _extract_nextjs_links

    html = "<html><body><a href='/reports/q1.pdf'>Q1</a></body></html>"
    anchors = _extract_nextjs_links(html, "https://example.com/")
    assert anchors == []


def test_extract_nextjs_buildid_reads_from_next_data_json():
    from src.download.downloader import _extract_nextjs_buildid

    next_data = json.dumps({"buildId": "abc123xyz", "props": {}})
    html = f'<script id="__NEXT_DATA__" type="application/json">{next_data}</script>'
    assert _extract_nextjs_buildid(html) == "abc123xyz"


def test_extract_nextjs_buildid_falls_back_to_script_src():
    from src.download.downloader import _extract_nextjs_buildid

    html = '<script src="/_next/static/BUILD456/_buildManifest.js"></script>'
    assert _extract_nextjs_buildid(html) == "BUILD456"


def test_download_from_ir_uses_nextjs_strategy(tmp_path, monkeypatch):
    from src.download.downloader import download_from_ir

    next_data = json.dumps({
        "props": {
            "pageProps": {
                "trimestral": [
                    {"url": "/api/media/RESULTADOS_PRIMER_TRIMESTRE_2026/RESULTADOS_PRIMER_TRIMESTRE_2026.pdf"},
                    {"url": "/api/media/RESULTADOS_CUARTO_TRIMESTRE_2025/RESULTADOS_CUARTO_TRIMESTRE_2025.pdf"},
                    {"url": "/api/media/RESULTADOS_TERCER_TRIMESTRE_2025/RESULTADOS_TERCER_TRIMESTRE_2025.pdf"},
                    {"url": "/api/media/RESULTADOS_SEGUNDO_TRIMESTRE_2025/RESULTADOS_SEGUNDO_TRIMESTRE_2025.pdf"},
                ]
            }
        },
        "buildId": "herdez-build",
    })
    html = f"""
    <html>
      <head><script id="__NEXT_DATA__" type="application/json">{next_data}</script></head>
      <body>
        <a href="/api/media/RESULTADOS_PRIMER_TRIMESTRE_2026/RESULTADOS_PRIMER_TRIMESTRE_2026.pdf">Ver PDF</a>
      </body>
    </html>
    """

    class _HerdezSession:
        headers = {}
        verify = True

        def get(self, url, **kwargs):
            return _FakeResponse(text=html, url=url)

    downloaded_urls = []
    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: _HerdezSession())
    monkeypatch.setattr(
        "src.download.downloader._download_pdf",
        lambda session, url, dest, **kw: downloaded_urls.append(url) or dest,
    )

    paths = download_from_ir(
        "https://grupoherdez.com.mx/informacion-para-inversionistas",
        tmp_path,
        max_reports=10,
        delay_ms=0,
    )

    assert len(downloaded_urls) == 4
    assert all("RESULTADOS" in u for u in downloaded_urls)
    assert all("TRIMESTRE" in u for u in downloaded_urls)


def test_download_from_ir_uses_year_api_urls(tmp_path, monkeypatch):
    from src.download.downloader import download_from_ir

    html = "<html><body><p>IR Page</p></body></html>"
    api_2026 = {
        "data": [
            {"attributes": {"pdf": {"data": {"attributes": {"url": "/api/media/1T26/1T26.pdf"}}}}}
        ]
    }
    api_2025 = {
        "data": [
            {"attributes": {"pdf": {"data": {"attributes": {"url": "/api/media/4T25/4T25.pdf"}}}}}
        ]
    }

    api_responses = {
        "https://example.com/api/reportes?year=2026": api_2026,
        "https://example.com/api/reportes?year=2025": api_2025,
    }

    class _ApiSession:
        headers = {}
        verify = True

        def get(self, url, **kwargs):
            clean_url = url.split("?")[0] + ("?" + kwargs.get("params", {}) and "") if False else url
            for key, val in api_responses.items():
                if key in url or url == key:
                    return _FakeResponse(json_data=val, url=url)
            return _FakeResponse(text=html, url=url)

    downloaded_urls = []
    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: _ApiSession())
    monkeypatch.setattr(
        "src.download.downloader._download_pdf",
        lambda session, url, dest, **kw: downloaded_urls.append(url) or dest,
    )

    paths = download_from_ir(
        "https://example.com/ir/",
        tmp_path,
        year_api_urls=[
            "https://example.com/api/reportes?year=2026",
            "https://example.com/api/reportes?year=2025",
        ],
        max_reports=10,
        delay_ms=0,
    )

    assert len(downloaded_urls) == 2
    assert any("1T26" in u for u in downloaded_urls)
    assert any("4T25" in u for u in downloaded_urls)


def test_download_from_ir_browser_first_skips_initial_http_fetch(tmp_path, monkeypatch):
    from src.download.downloader import _PdfAnchor, download_from_ir

    class _NoFetchSession:
        headers = {}
        verify = True

        def get(self, *args, **kwargs):
            raise AssertionError("initial HTTP fetch should be skipped")

    downloaded_urls = []
    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: _NoFetchSession())
    monkeypatch.setattr(
        "src.download.downloader._playwright_collect_pdf_links",
        lambda *args, **kwargs: [
            _PdfAnchor(
                url="https://www.organizacionsoriana.com/pdf/reportes/2026/Eng/1T_2026_Ingles_InfDirVfinal.pdf",
                filename="1T_2026_Ingles_InfDirVfinal.pdf",
                text="1Q 2026 Results",
            )
        ],
    )
    monkeypatch.setattr(
        "src.download.downloader._download_pdf",
        lambda session, url, dest, **kw: downloaded_urls.append(url) or dest,
    )

    paths = download_from_ir(
        "https://www.organizacionsoriana.com/principales_reportes_en.html",
        tmp_path,
        browser_first=True,
        use_playwright=True,
        delay_ms=0,
    )

    assert downloaded_urls == [
        "https://www.organizacionsoriana.com/pdf/reportes/2026/Eng/1T_2026_Ingles_InfDirVfinal.pdf"
    ]
    assert [p.name for p in paths] == ["1T_2026_Ingles_InfDirVfinal.pdf"]


def test_curl_response_raise_for_status_raises_on_4xx():
    from src.download.downloader import _CurlResponse

    ok = _CurlResponse(text="<html/>", url="https://example.com/", status_code=200)
    ok.raise_for_status()  # must not raise

    for code in (400, 403, 404, 500):
        bad = _CurlResponse(text="error", url="https://example.com/", status_code=code)
        try:
            bad.raise_for_status()
            assert False, f"Expected RuntimeError for status {code}"
        except RuntimeError:
            pass


def test_session_get_curl_fallback_raises_on_http_error(monkeypatch):
    """The curl-recovered response must raise on 4xx/5xx like every other branch.

    Previously _session_get returned the error page unraised, so a 403/500 body
    was parsed as the IR page and the run reported "no PDF links found" instead
    of failing.
    """
    import pytest
    import requests
    from src.download.downloader import _session_get

    class _TlsSession:
        headers = {"User-Agent": "Mozilla/5.0"}
        verify = True

        def get(self, *args, **kwargs):
            raise requests.exceptions.SSLError("TLSV1_ALERT_PROTOCOL_VERSION")

    class _Completed:
        stdout = "https://example.com/ir/\n403"
        stderr = ""
        returncode = 0

    def fake_run(cmd, check, capture_output, text):
        from pathlib import Path
        Path(cmd[cmd.index("-o") + 1]).write_text("<html>Forbidden</html>", encoding="utf-8")
        return _Completed()

    monkeypatch.setattr("src.download.downloader.shutil.which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr("src.download.downloader.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="403"):
        _session_get(_TlsSession(), "https://example.com/ir/", timeout=30, verify_ssl=True)


def test_run_curl_unparsable_status_raises(monkeypatch):
    """An unparsable -w status must not silently default to 200."""
    import pytest
    from pathlib import Path
    from src.download.downloader import _run_curl

    class _Completed:
        stdout = ""          # curl produced no -w output at all
        stderr = ""
        returncode = 0

    monkeypatch.setattr("src.download.downloader.shutil.which", lambda name: "/usr/bin/curl")
    monkeypatch.setattr(
        "src.download.downloader.subprocess.run",
        lambda *a, **k: _Completed(),
    )

    with pytest.raises(RuntimeError, match="could not parse HTTP status"):
        _run_curl(
            "https://example.com/x",
            output_path=Path("/dev/null"),
            timeout=10,
            verify_ssl=True,
        )


def test_should_fallback_to_curl_covers_additional_ssl_variants():
    import requests
    from src.download.downloader import _should_fallback_to_curl

    variants = [
        "certificate verify failed",
        "EOF occurred in violation of protocol",
        "DH key too small",
        "unsafe legacy renegotiation disabled",
        "bad handshake",
        "connection reset",
    ]
    for msg in variants:
        exc = requests.exceptions.SSLError(msg)
        assert _should_fallback_to_curl(exc), f"Expected fallback for: {msg}"

    plain_error = requests.exceptions.ConnectionError("Network unreachable")
    assert not _should_fallback_to_curl(plain_error)


def test_extract_pdf_candidates_finds_query_param_pdf_urls():
    from src.download.downloader import _extract_pdf_candidates

    html = """
    <html><body>
      <a href="/download?file=report.pdf&token=abc">Reporte</a>
      <a href="/getdoc?type=pdf&id=42&name=earnings.pdf">Earnings</a>
      <a href="/normal/path/direct.pdf">Direct</a>
    </body></html>
    """
    candidates = _extract_pdf_candidates(html, "https://example.com/ir/")
    urls = [c.url for c in candidates]
    assert any("download" in u and "report.pdf" in u for u in urls), "query-param PDF not found"
    assert any("direct.pdf" in u for u in urls), "direct PDF not found"


def test_effective_base_url_adds_trailing_slash_for_directory_urls():
    from src.download.downloader import _effective_base_url
    from urllib.parse import urljoin

    html = "<html><body><a href='reports/q1.pdf'>Q1</a></body></html>"

    # Without trailing slash, urljoin would drop "quarterly"
    base_no_slash = _effective_base_url(html, "https://example.com/investors/quarterly")
    resolved = urljoin(base_no_slash, "reports/q1.pdf")
    assert "quarterly" in resolved, f"Expected 'quarterly' in {resolved}"

    # With .html extension, should NOT add slash
    base_with_ext = _effective_base_url(html, "https://example.com/investors/quarterly.html")
    assert not base_with_ext.endswith("//")


def test_playwright_collect_pdf_links_returns_empty_when_not_installed(monkeypatch):
    """When playwright is not installed, function returns [] and prints a message to stderr."""
    import sys
    import io
    from src.download.downloader import _playwright_collect_pdf_links

    # Simulate playwright not being importable
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    result = _playwright_collect_pdf_links("https://example.com/ir/", "https://example.com/")
    assert result == []
    assert "playwright" in buf.getvalue().lower()


def test_playwright_collect_pdf_links_harvests_all_years(monkeypatch):
    """Playwright layer discovers year values via evaluate, clicks each, and collects PDFs."""
    from src.download.downloader import _playwright_collect_pdf_links

    clicked_years = []
    year_pdfs = {
        "2026": [
            ["https://example.com/reports/1T26_report.pdf", "1T26"],
            ["https://example.com/reports/2T26_report.pdf", "2T26"],
        ],
        "2025": [
            ["https://example.com/reports/1T25_report.pdf", "1T25"],
            ["https://example.com/reports/2T25_report.pdf", "2T25"],
        ],
    }
    current_links = [["https://example.com/reports/initial_report.pdf", "Initial"]]

    class _FakeButton:
        def __init__(self, text):
            self._text = text

        def text_content(self, timeout=None):
            return self._text

        def scroll_into_view_if_needed(self):
            pass

        def click(self, timeout=None):
            pass

    class _FakePage2:
        def goto(self, url, wait_until=None, timeout=None):
            pass

        def evaluate(self, script, arg=None):
            if arg is None:
                # harvest mode: return current PDF link pairs
                return list(current_links)
            if isinstance(arg, str):
                # year discovery mode: return unique year values for the given selector
                return ["2026", "2025"]
            if isinstance(arg, list) and len(arg) == 2:
                # year click mode: [selector, year] — simulate button click
                year = arg[1]
                clicked_years.append(year)
                current_links.clear()
                current_links.extend(year_pdfs.get(year, []))
                return True
            return None

        def locator(self, selector):
            class _FakeLocator:
                def count(self_):
                    return 0  # no #historicoreporte section → page-wide fallback

                def all(self_):
                    return [_FakeButton("información trimestral")]
            return _FakeLocator()

        def wait_for_timeout(self, ms):
            pass

    class _FakeContext:
        def new_page(self):
            return _FakePage2()

        def close(self):
            pass

    class _FakeBrowser:
        def new_context(self, **kw):
            return _FakeContext()

        def close(self):
            pass

    class _FakeChromium:
        def launch(self, headless=True):
            return _FakeBrowser()

    class _FakePlaywright:
        chromium = _FakeChromium()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    import types
    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePlaywright()
    fake_module.TimeoutError = Exception
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", fake_module)

    results = _playwright_collect_pdf_links("https://example.com/ir/", "https://example.com/")

    urls = [a.url for a in results]
    assert any("1T25" in u for u in urls), f"Expected 2025 PDF in {urls}"
    assert any("1T26" in u for u in urls), f"Expected 2026 PDF in {urls}"
    assert "2026" in clicked_years
    assert "2025" in clicked_years


def test_playwright_collect_pdf_links_skips_signed_storage_urls(monkeypatch):
    """Signed GCS/S3 URLs are never returned by the playwright harvester."""
    from src.download.downloader import _playwright_collect_pdf_links

    class _FakePage:
        def goto(self, url, wait_until=None, timeout=None):
            pass

        def evaluate(self, script):
            return [
                ["https://storage.googleapis.com/bucket/report.pdf?X-Goog-Signature=abc123", "signed"],
                ["https://example.com/reports/normal_report.pdf", "normal"],
            ]

        def locator(self, selector):
            class _FakeLocator:
                def all(self_):
                    return []  # no buttons on this fake page
            return _FakeLocator()

        def wait_for_timeout(self, ms):
            pass

    class _FakeContext:
        def new_page(self):
            return _FakePage()

        def close(self):
            pass

    class _FakeBrowser:
        def new_context(self, **kw):
            return _FakeContext()

        def close(self):
            pass

    class _FakePlaywright:
        class chromium:
            @staticmethod
            def launch(headless=True):
                return _FakeBrowser()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    import types
    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: _FakePlaywright()
    fake_module.TimeoutError = Exception
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", fake_module)

    results = _playwright_collect_pdf_links("https://example.com/ir/", "https://example.com/")
    urls = [a.url for a in results]
    assert all("X-Goog-Signature" not in u for u in urls), "Signed URL leaked through"
    assert any("normal_report" in u for u in urls)


def test_download_from_ir_uses_playwright_layer(tmp_path, monkeypatch):
    """With use_playwright=True, Layer 3 runs playwright and downloads its PDFs."""
    from src.download.downloader import download_from_ir

    # IR page has no static PDFs (static layers find nothing)
    html = "<html><body><p>IR page with no static links</p></body></html>"

    class _EmptySession:
        headers = {}
        verify = True

        def get(self, url, **kwargs):
            return _FakeResponse(text=html, url=url)

    # Playwright finds these PDFs
    playwright_urls = [
        "https://example.com/reports/resultados_1T26.pdf",
        "https://example.com/reports/resultados_4T25.pdf",
        "https://example.com/reports/resultados_3T25.pdf",
    ]

    from src.download.downloader import _PdfAnchor
    from pathlib import Path as _Path
    from urllib.parse import urlparse as _urlparse

    playwright_anchors = [
        _PdfAnchor(
            url=u,
            filename=_Path(_urlparse(u).path).name,
            text="",
        )
        for u in playwright_urls
    ]

    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: _EmptySession())
    monkeypatch.setattr(
        "src.download.downloader._playwright_collect_pdf_links",
        lambda *args, **kwargs: playwright_anchors,
    )
    downloaded_urls = []
    monkeypatch.setattr(
        "src.download.downloader._download_pdf",
        lambda session, url, dest, **kw: downloaded_urls.append(url) or dest,
    )

    paths = download_from_ir(
        "https://example.com/ir/",
        tmp_path,
        use_playwright=True,
        max_reports=10,
        delay_ms=0,
    )

    assert len(downloaded_urls) == 3
    assert all("resultados" in u for u in downloaded_urls)


def test_download_from_ir_skips_playwright_when_flag_false(tmp_path, monkeypatch):
    """With use_playwright=False (default), the playwright function is never called."""
    from src.download.downloader import download_from_ir

    html = "<html><body><a href='/reports/report.pdf'>Q1</a></body></html>"

    class _SimpleSession:
        headers = {}
        verify = True

        def get(self, url, **kwargs):
            return _FakeResponse(text=html, url=url)

    playwright_called = []
    monkeypatch.setattr("src.download.downloader._make_session", lambda **kw: _SimpleSession())
    monkeypatch.setattr(
        "src.download.downloader._playwright_collect_pdf_links",
        lambda *args, **kwargs: playwright_called.append(True) or [],
    )
    monkeypatch.setattr(
        "src.download.downloader._download_pdf",
        lambda session, url, dest, **kw: dest,
    )

    download_from_ir(
        "https://example.com/ir/",
        tmp_path,
        use_playwright=False,
        max_reports=10,
        delay_ms=0,
    )

    assert playwright_called == [], "Playwright should not be called when use_playwright=False"


def test_download_pdf_sends_pdf_headers_and_referer(tmp_path):
    from src.download.downloader import _download_pdf

    class _PdfResponse:
        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=65536):
            yield b"%PDF-1.4\n%test\n"

    class _PdfSession:
        def __init__(self):
            self.calls = []

        def get(self, url, stream=False, timeout=None, verify=None, headers=None):
            self.calls.append({
                "url": url,
                "stream": stream,
                "timeout": timeout,
                "verify": verify,
                "headers": headers,
            })
            return _PdfResponse()

    session = _PdfSession()
    dest = tmp_path / "legacy.pdf"
    referer = "https://www.walmex.mx/en/financial-information/quarterly.html"

    path = _download_pdf(
        session,
        "https://files.walmex.mx/assets/files/Informacion%20financiera/Trimestral/Eng/2017/2Q17_Walmex_reports_results.pdf",
        dest,
        referer=referer,
    )

    assert path == dest
    assert dest.read_bytes().startswith(b"%PDF-")
    assert session.calls == [
        {
            "url": "https://files.walmex.mx/assets/files/Informacion%20financiera/Trimestral/Eng/2017/2Q17_Walmex_reports_results.pdf",
            "stream": True,
            "timeout": 60,
            "verify": True,
            "headers": {
                "Accept": "application/pdf,*/*",
                "Referer": referer,
            },
        }
    ]
