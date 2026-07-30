"""USD-reporter support: XBRL currency filtering, workbook units, URL templates.

GMEXICO/ORBIA file BMV XBRL facts tagged ``ISO4217:USD``; the pipeline must not
mix currencies within a company, and USD companies' workbooks must not claim
``P$mn``.
"""

from pathlib import Path

from src.extract.xbrl_facts import (
    _currency_ok,
    extract_from_xbrl,
    iso_currency_for,
)
from src.model.financial_model import METRICS


def _facts(unit_mxn: float, unit_usd: float) -> dict:
    def entry(value, unit):
        return {
            "value": value,
            "period_start": "2025-04-01",
            "period_end": "2025-06-30",
            "unit": unit,
            "decimals": "-3",
            "dimensions": None,
        }
    return {
        "ifrs-full_Revenue": [
            entry(unit_mxn, "ISO4217:MXN"),
            entry(unit_usd, "ISO4217:USD"),
        ]
    }


def _revenue_def():
    from dataclasses import replace
    base = next(m for m in METRICS if m.key == "revenue")
    return [replace(base, xbrl_concepts=["ifrs-full_Revenue"])]


def test_currency_filter_picks_expected_currency():
    facts = _facts(80_000_000_000, 4_195_519_000)
    usd = extract_from_xbrl(facts, _revenue_def(), "2025-06-30", 1e6,
                            expected_currency="USD")
    mxn = extract_from_xbrl(facts, _revenue_def(), "2025-06-30", 1e6,
                            expected_currency="MXN")
    assert usd["revenue"].current == 4195.519
    assert mxn["revenue"].current == 80000.0


def test_no_expected_currency_is_a_noop():
    facts = _facts(80_000_000_000, 4_195_519_000)
    rows = extract_from_xbrl(facts, _revenue_def(), "2025-06-30", 1e6)
    # unfiltered behavior unchanged: first suitable entry wins
    assert rows["revenue"].current == 80000.0


def test_currency_ok_ignores_non_monetary_units():
    assert _currency_ok({"unit": "xbrli:pure"}, "USD")
    assert _currency_ok({"unit": "xbrli:shares"}, "USD")
    assert _currency_ok({"unit": "ISO4217:USD"}, "USD")
    assert not _currency_ok({"unit": "ISO4217:MXN"}, "USD")
    assert _currency_ok({"unit": "ISO4217:MXN"}, None)


def test_iso_currency_for_reads_company_block():
    assert iso_currency_for({"company": {"currency": "USD"}}) == "USD"
    assert iso_currency_for({"company": {"currency": "mxn"}}) == "MXN"
    assert iso_currency_for({"company": {}}) is None
    assert iso_currency_for(None) is None


def test_workbook_units_default_follows_currency():
    from src.excel.segments_sheet import _default_units
    assert _default_units({"company": {"currency": "USD"}}) == "US$mn"
    assert _default_units({"company": {"currency": "MXN"}}) == "P$mn"
    assert _default_units({}) == "P$mn"


def test_usd_caption_scale_detection():
    from src.parse.parse_pdf import detect_scale
    assert detect_scale("(Millions of US dollars)") == (1.0, "header")
    assert detect_scale("(miles de dólares)") == (0.001, "header")
    assert detect_scale("(millones de pesos)") == (1.0, "header")
    assert detect_scale("random prose") == (1.0, "default")


def test_slugify_url_bakes_in_year_quarter_path():
    # Orbia shelves files under .../{year}/{qN}/ and reuses the same basename
    # every quarter — the period context must ride into the staged filename so
    # infer_period_label can canonicalize.
    from src.download.downloader import _slugify_url
    url = ("https://www.orbia.com/4a55aa/siteassets/5.-investor-relations/"
           "quarterly-earnings/2018/q1/earnings-release-eng.pdf")
    assert _slugify_url(url) == "2018_q1_earnings-release-eng.pdf"
    assert _slugify_url("https://x.com/docs/a/b/report.pdf") == "report.pdf"


def test_url_template_fetch(tmp_path, monkeypatch):
    from src.download import downloader as dl

    fetched: list[str] = []

    class _Resp:
        content = b"%PDF-1.4 fake"
        def raise_for_status(self):
            return None
        def iter_content(self, chunk_size=65536):
            yield self.content

    class _Session:
        headers: dict = {}
        def get(self, url, **kw):
            fetched.append(url)
            return _Resp()

    monkeypatch.setattr(dl, "_make_session", lambda *a, **k: _Session())
    monkeypatch.setattr(
        dl, "_verify_pdf_url",
        lambda session, url, verify_ssl=True: "RF_ES_2024" in url,
    )
    monkeypatch.setattr(dl.time, "sleep", lambda s: None)

    tpl = "https://gmexico.com/GMDocs/ReportesFinancieros/Esp/{year}/RF_ES_{year}_{quarter}T.pdf"
    saved = dl.download_from_url_templates(
        [tpl], tmp_path, {"2024-1T", "2024-2T", "2025-1T"},
    )
    names = sorted(p.name for p in saved)
    assert names == ["2024-1T.pdf", "2024-2T.pdf"]   # 2025 fails verification
    assert all(p.read_bytes().startswith(b"%PDF") for p in saved)


def test_url_template_fetch_rejects_non_pdf(tmp_path, monkeypatch):
    """An HTML error page served at a template URL must not be saved as a PDF."""
    from src.download import downloader as dl

    class _Resp:
        content = b"<html>Maintenance</html>"
        def raise_for_status(self):
            return None
        def iter_content(self, chunk_size=65536):
            yield self.content

    class _Session:
        headers: dict = {}
        def get(self, url, **kw):
            return _Resp()

    monkeypatch.setattr(dl, "_make_session", lambda *a, **k: _Session())
    monkeypatch.setattr(dl, "_verify_pdf_url", lambda session, url, verify_ssl=True: True)
    monkeypatch.setattr(dl.time, "sleep", lambda s: None)

    saved = dl.download_from_url_templates(
        ["https://example.com/{year}/RF_{year}_{quarter}T.pdf"], tmp_path, {"2024-1T"},
    )
    assert saved == []
    assert list(tmp_path.iterdir()) == []   # no PDF, no leftover .tmp


def test_url_template_fetch_supports_two_digit_year(tmp_path, monkeypatch):
    from src.download import downloader as dl

    fetched: list[str] = []

    class _Resp:
        content = b"%PDF-1.4 fake"
        def raise_for_status(self):
            return None
        def iter_content(self, chunk_size=65536):
            yield self.content

    class _Session:
        headers: dict = {}
        def get(self, url, **kw):
            fetched.append(url)
            return _Resp()

    monkeypatch.setattr(dl, "_make_session", lambda *a, **k: _Session())
    monkeypatch.setattr(dl, "_verify_pdf_url", lambda *a, **k: True)
    monkeypatch.setattr(dl.time, "sleep", lambda s: None)

    saved = dl.download_from_url_templates(
        ["https://example.com/{quarter}T{year2}/report-{quarter}Q{year2}.pdf"],
        tmp_path,
        {"2024-1T"},
    )

    assert [path.name for path in saved] == ["2024-1T.pdf"]
    assert fetched == ["https://example.com/1T24/report-1Q24.pdf"]
