"""Exact-kind selection required by resumable fleet acquisition."""
from __future__ import annotations

import pytest

from src.download import bmv_xbrl
from src.download.bmv_xbrl import XbrlFiling


def _filings() -> list[XbrlFiling]:
    return [
        XbrlFiling("AC", "Arca", "x", "2024-FY", "annual", "https://x/annual.zip"),
        XbrlFiling("AC", "Arca", "x", "2025-1T", "quarterly", "https://x/q.zip"),
    ]


def test_download_ticker_can_select_one_kind(tmp_path, monkeypatch):
    fetched: list[str] = []

    def fake_download(_session, filing, dest, **_kwargs):
        fetched.append(filing.kind)
        dest.write_bytes(b"{}")
        return dest

    monkeypatch.setattr(bmv_xbrl, "_download_filing_json", fake_download)
    monkeypatch.setattr(bmv_xbrl, "extract_artifacts", lambda _path: {})

    paths = bmv_xbrl.download_ticker(
        "AC", tmp_path, index=_filings(), kinds=frozenset({"annual"}), delay_ms=0,
    )

    assert fetched == ["annual"]
    assert [path.name for path in paths] == ["AC_2024-FY.json.gz"]


def test_download_ticker_accepts_exact_resumable_subset(tmp_path, monkeypatch):
    fetched: list[str] = []

    def fake_download(_session, filing, dest, **_kwargs):
        fetched.append(filing.period)
        dest.write_bytes(b"{}")
        return dest

    monkeypatch.setattr(bmv_xbrl, "_download_filing_json", fake_download)
    monkeypatch.setattr(bmv_xbrl, "extract_artifacts", lambda _path: {})

    bmv_xbrl.download_ticker(
        "AC", tmp_path, filings=[_filings()[1]], delay_ms=0,
    )

    assert fetched == ["2025-1T"]


def test_download_ticker_rejects_conflicting_or_unknown_selection(tmp_path):
    with pytest.raises(ValueError, match="either index or filings"):
        bmv_xbrl.download_ticker(
            "AC", tmp_path, index=_filings(), filings=_filings(), delay_ms=0,
        )
    with pytest.raises(ValueError, match="Unknown XBRL"):
        bmv_xbrl.download_ticker(
            "AC", tmp_path, index=[], kinds=frozenset({"announcement"}), delay_ms=0,
        )
