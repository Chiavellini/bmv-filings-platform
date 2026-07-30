"""Annual-report selection mode for the IR downloader (`doc_kind="annual"`).

The quarterly path deliberately drops annual/integrated/20-F links; annual mode keeps them while
still excluding non-report assets (webcast/transcript/presentation), and never leaks quarterly PDFs.
"""
from __future__ import annotations

_MIXED_HTML = """
<html><body>
  <a href="/docs/Walmex_Integrated_Annual_Report_2024.pdf">2024 Integrated Annual Report</a>
  <a href="/docs/Walmex_Informe_Anual_2023.pdf">Informe Anual 2023</a>
  <a href="/docs/Walmex_1Q24_Release.pdf">1Q24 Release</a>
  <a href="/docs/Walmex_Annual_2024_Webcast.pdf">Annual 2024 Webcast</a>
</body></html>
"""

_BASE = "https://www.walmex.mx/en/financial-information/annual.html"


def test_annual_mode_selects_annual_and_drops_quarterly_and_webcast():
    from src.download.downloader import _extract_pdf_links

    links = _extract_pdf_links(_MIXED_HTML, _BASE, None, doc_kind="annual")
    names = [l.rsplit("/", 1)[-1] for l in links]

    assert "Walmex_Integrated_Annual_Report_2024.pdf" in names
    assert "Walmex_Informe_Anual_2023.pdf" in names
    assert "Walmex_1Q24_Release.pdf" not in names            # quarterly never leaks into annual
    assert "Walmex_Annual_2024_Webcast.pdf" not in names     # webcast still excluded


def test_quarterly_mode_unchanged_still_drops_annual():
    from src.download.downloader import _extract_pdf_links

    # Default doc_kind="quarterly": the annual links must still be excluded (no regression).
    links = _extract_pdf_links(_MIXED_HTML, _BASE, None)
    names = [l.rsplit("/", 1)[-1] for l in links]

    assert names == ["Walmex_1Q24_Release.pdf"]


def test_annual_pattern_path_keeps_annual_that_quarterly_would_drop():
    from src.download.downloader import _PdfAnchor, _select_pdf_links

    anchors = [
        _PdfAnchor(url="https://x/annual_report_2024.pdf",
                   filename="annual_report_2024.pdf", text="2024 Annual Report"),
    ]
    pattern = r"(?:annual|integrated|informe).*\.pdf"

    # Quarterly mode drops it (annual is an excluded asset); annual mode keeps it.
    assert _select_pdf_links(anchors, pattern, "quarterly") == []
    assert _select_pdf_links(anchors, pattern, "annual") == ["https://x/annual_report_2024.pdf"]
