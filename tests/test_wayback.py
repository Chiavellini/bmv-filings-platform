"""Tests for src/download/wayback.py — CDX parsing, raw-archive URL building,
period mapping/dedup, and the 2016 floor."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.download import wayback  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "wayback_cdx.json"


def test_parse_cdx_json_parses_rows():
    rows = wayback._parse_cdx_json(FIXTURE.read_text())
    assert len(rows) == 5
    assert rows[0] == {
        "original": "http://www.elpuertodeliverpool.mx/docs/1T-2018.pdf",
        "timestamp": "20180515120000",
    }


def test_parse_cdx_json_empty():
    assert wayback._parse_cdx_json("") == []
    assert wayback._parse_cdx_json("[]") == []


def test_wayback_pdf_url_builds_id_suffix():
    url = wayback.wayback_pdf_url("20180515120000", "http://x.com/1T-2018.pdf")
    assert url == "https://web.archive.org/web/20180515120000id_/http://x.com/1T-2018.pdf"


def test_host_prefix():
    assert (
        wayback._host_prefix("https://inversionistas.grupochedraui.com.mx/reportes/")
        == "inversionistas.grupochedraui.com.mx/*"
    )


def test_apex_domain():
    cases = [
        ("www.elpuertodeliverpool.mx",         "elpuertodeliverpool.mx"),
        ("inversionistas.grupochedraui.com.mx", "grupochedraui.com.mx"),
        ("www.sportsworld.com.mx",              "sportsworld.com.mx"),
        ("walmex.mx",                           "walmex.mx"),
        ("lacomerfinanzas.com.mx",              "lacomerfinanzas.com.mx"),
        ("grupobimbo.com",                      "grupobimbo.com"),
    ]
    for netloc, expected in cases:
        got = wayback._apex_domain(netloc)
        assert got == expected, f"_apex_domain({netloc!r}) = {got!r}, expected {expected!r}"


def test_discover_archived_reports_maps_dedupes_and_floors(monkeypatch):
    rows = wayback._parse_cdx_json(FIXTURE.read_text())
    monkeypatch.setattr(wayback, "cdx_snapshots", lambda *a, **k: rows)

    result = wayback.discover_archived_reports("http://www.elpuertodeliverpool.mx/", from_year=2016)
    candidates = wayback.discover_archived_report_candidates(
        "http://www.elpuertodeliverpool.mx/",
        from_year=2016,
    )

    # 2018-1T deduped to the latest snapshot; 2019-2T present; pre-2016 dropped;
    # non-period "aviso-legal" ignored.
    assert set(result) == {"2018-1T", "2019-2T"}
    assert "2015-4T" not in result
    assert result["2018-1T"].startswith("https://web.archive.org/web/20181101090000id_/")
    assert candidates["2018-1T"][0].startswith("https://web.archive.org/web/20181101090000id_/")


def test_download_missing_no_targets_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        wayback, "discover_archived_report_candidates", lambda *a, **k: {"2018-1T": ["http://x"]}
    )
    # Requested missing set does not intersect what the archive has.
    out = wayback.download_missing(
        "http://x/", tmp_path, {"2099-1T"}, session=object()
    )
    assert out == []


def test_download_missing_empty_input_short_circuits(tmp_path):
    assert wayback.download_missing("http://x/", tmp_path, set()) == []


def test_download_missing_tries_alternate_when_pdf_is_truncated(monkeypatch, tmp_path):
    monkeypatch.setattr(
        wayback,
        "discover_archived_report_candidates",
        lambda *a, **k: {"2018-1T": ["http://bad", "http://good"]},
    )

    calls = []

    def fake_download(session, url, dest, **kwargs):
        calls.append(url)
        if url == "http://bad":
            dest.write_bytes(b"%PDF-1.4\ntruncated")
        else:
            dest.write_bytes(b"%PDF-1.4\nbody\n%%EOF\n")
        return dest

    monkeypatch.setattr(wayback, "_download_pdf", fake_download)

    out = wayback.download_missing(
        "http://x/",
        tmp_path,
        {"2018-1T"},
        delay_ms=0,
        session=object(),
    )

    assert calls == ["http://bad", "http://good"]
    assert [p.name for p in out] == ["2018-1T.pdf"]
    assert (tmp_path / "2018-1T.pdf").read_bytes().endswith(b"%%EOF\n")
