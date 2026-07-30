from __future__ import annotations

from src.download import bmv_xbrl
from src.download.bmv_xbrl import XbrlFiling


def test_download_ticker_can_select_annual_only(tmp_path, monkeypatch):
    filings = [
        XbrlFiling("AC", "Arca", "x", "2024-FY", "annual", "https://x/annual.zip"),
        XbrlFiling("AC", "Arca", "x", "2025-1T", "quarterly", "https://x/q.zip"),
    ]
    fetched: list[str] = []

    def fake_download(_session, filing, dest, **_kwargs):
        fetched.append(filing.kind)
        dest.write_text("{}")
        return dest

    monkeypatch.setattr(bmv_xbrl, "_download_filing_json", fake_download)
    monkeypatch.setattr(bmv_xbrl, "extract_artifacts", lambda _path: {})
    paths = bmv_xbrl.download_ticker(
        "AC", tmp_path, index=filings, kinds=frozenset({"annual"}), delay_ms=0,
    )
    assert fetched == ["annual"]
    assert [p.name for p in paths] == ["AC_2024-FY.json"]


def test_download_ticker_rejects_unknown_kind(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="Unknown XBRL"):
        bmv_xbrl.download_ticker(
            "AC", tmp_path, index=[], kinds=frozenset({"announcement"}), delay_ms=0,
        )


def test_download_ticker_can_receive_an_exact_resumable_filing_subset(tmp_path, monkeypatch):
    filings = [
        XbrlFiling("AC", "Arca", "x", "2024-1T", "quarterly", "https://x/old.zip"),
        XbrlFiling("AC", "Arca", "x", "2025-1T", "quarterly", "https://x/new.zip"),
    ]
    fetched: list[str] = []

    def fake_download(_session, filing, dest, **_kwargs):
        fetched.append(filing.period)
        dest.write_text("{}")
        return dest

    monkeypatch.setattr(bmv_xbrl, "_download_filing_json", fake_download)
    monkeypatch.setattr(bmv_xbrl, "extract_artifacts", lambda _path: {})
    bmv_xbrl.download_ticker("AC", tmp_path, filings=[filings[0]], delay_ms=0)
    assert fetched == ["2024-1T"]
