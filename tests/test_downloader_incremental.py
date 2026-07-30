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
