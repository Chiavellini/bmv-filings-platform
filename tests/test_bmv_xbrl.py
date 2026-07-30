"""Tests for the BMV XBRL archive source (bmv_xbrl.py)."""

from __future__ import annotations

import io
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

TEST_DATA = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# Archive index parsing
# ---------------------------------------------------------------------------

def test_parse_archive_index_rows():
    from src.download.bmv_xbrl import parse_archive_index

    html = (TEST_DATA / "bmv_archive_sample.html").read_text(encoding="utf-8")
    filings = parse_archive_index(html)

    assert len(filings) == 5  # empty-ticker and no-docins rows skipped
    by_ticker = {f.ticker: f for f in filings}

    alsea = by_ticker["ALSEA"]
    assert alsea.period == "2026-1T"
    assert alsea.kind == "quarterly"
    assert alsea.zip_url == "https://www.bmv.com.mx/docs-pub/ifrsxbrl/ifrsxbrl_1552416_2026-01_1.zip"
    assert alsea.razon_social == "ALSEA, S.A.B. DE C.V."
    assert alsea.filed_date == "28/04/2026 15:30"

    sport = by_ticker["SPORT"]
    assert sport.period == "2025-3T"
    assert sport.kind == "quarterly"

    ac = by_ticker["AC"]
    assert ac.period == "2025-FY"
    assert ac.kind == "annual"
    assert ac.zip_url == "https://www.bmv.com.mx/docs-pub/anexon/anexon_1553616_2025_1.zip"

    bimbo = by_ticker["BIMBO"]
    assert bimbo.period == "2026-1T"
    assert bimbo.kind == "quarterly"

    assert by_ticker["PEOLES"].period == "2026-1T"


def test_infer_period_falls_back_to_zip_name():
    from src.download.bmv_xbrl import _infer_period

    assert _infer_period("Descargar", "https://x/ifrsxbrl_1_2024-03_1.zip") == ("2024-3T", "quarterly")
    assert _infer_period("Descargar", "https://x/ifrsxbrl_1_2018-04d_1.zip") == ("2018-4T", "quarterly")
    assert _infer_period("Descargar", "https://x/anexon_1_2023_1.zip") == ("2023-FY", "annual")
    assert _infer_period("Descargar", "https://x/whatever.zip") == (None, "unknown")


# ---------------------------------------------------------------------------
# download_ticker (fake session, in-memory zips)
# ---------------------------------------------------------------------------

def _zip_bytes(inner_name: str, payload: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(inner_name, json.dumps(payload))
    return buf.getvalue()


MINI_INSTANCE = {
    "HechosPorIdConcepto": {
        "ifrs-full_Revenue": ["f1"],
        "ifrs-full_ProfitLoss": ["f2"],
        "ifrs-mc_ManagementCommentaryExplanatory": ["f3"],
        "ifrs-full_DisclosureOfLeasesExplanatory": ["f4"],
        "some_ShortNote": ["f5"],
        "ifrs-full_NilFact": ["f6"],
    },
    "HechosPorId": {
        "f1": {"EsNumerico": "True", "EsValorNil": "False", "Valor": "1000000",
               "ValorNumerico": "1000000", "IdContexto": "c1", "IdUnidad": "u1", "Decimales": "-3"},
        "f2": {"EsNumerico": "True", "EsValorNil": "False", "Valor": "-50000",
               "ValorNumerico": "-50000", "IdContexto": "c2", "IdUnidad": "u1", "Decimales": "-3"},
        "f3": {"EsNumerico": "False", "EsValorNil": "False",
               "Valor": "<div>" + "comentario " * 300 + "</div>", "IdContexto": "c1"},
        "f4": {"EsNumerico": "False", "EsValorNil": "False",
               "Valor": "<p>" + "arrendamientos " * 300 + "</p>", "IdContexto": "c1"},
        "f5": {"EsNumerico": "False", "EsValorNil": "False", "Valor": "short", "IdContexto": "c1"},
        "f6": {"EsNumerico": "True", "EsValorNil": "True", "Valor": None, "IdContexto": "c1"},
    },
    "ContextosPorId": {
        "c1": {"Periodo": {"FechaInstante": None,
                           "FechaInicio": "2026-01-01T00:00:00Z",
                           "FechaFin": "2026-03-31T00:00:00Z"},
               "ValoresDimension": None},
        "c2": {"Periodo": {"FechaInstante": "2026-03-31T00:00:00Z",
                           "FechaInicio": None, "FechaFin": None},
               "ValoresDimension": None},
    },
    "UnidadesPorId": {
        "u1": {"Medidas": [{"Nombre": "MXN", "Etiqueta": "ISO4217:MXN"}]},
    },
}


class _ZipResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.text = ""
        self.url = ""

    def raise_for_status(self):
        return None


class _ZipSession:
    def __init__(self, zips: dict[str, bytes]):
        self.zips = zips
        self.requested: list[str] = []

    def get(self, url, params=None, timeout=None, stream=False, verify=None, headers=None):
        self.requested.append(url)
        return _ZipResponse(self.zips[url])


def _mini_index():
    from src.download.bmv_xbrl import XbrlFiling

    return [
        XbrlFiling("SPORT", "GRUPO SPORTS WORLD", "27/04/2026 12:00", "2026-1T", "quarterly",
                   "https://www.bmv.com.mx/docs-pub/ifrsxbrl/ifrsxbrl_1_2026-01_1.zip"),
        XbrlFiling("SPORT", "GRUPO SPORTS WORLD", "27/10/2025 12:00", "2025-3T", "quarterly",
                   "https://www.bmv.com.mx/docs-pub/ifrsxbrl/ifrsxbrl_2_2025-03_1.zip"),
        XbrlFiling("SPORT", "GRUPO SPORTS WORLD", "30/06/2026 12:00", "2025-FY", "annual",
                   "https://www.bmv.com.mx/docs-pub/anexon/anexon_3_2025_1.zip"),
        XbrlFiling("ALSEA", "ALSEA", "28/04/2026 12:00", "2026-1T", "quarterly",
                   "https://www.bmv.com.mx/docs-pub/ifrsxbrl/ifrsxbrl_4_2026-01_1.zip"),
    ]


def test_raw_xbrl_gzip_is_byte_deterministic_across_fetches(tmp_path):
    from src.download.bmv_xbrl import _download_filing_json

    filing = _mini_index()[0]
    payload = _zip_bytes("instance.json", MINI_INSTANCE)
    session = _ZipSession({filing.zip_url: payload})
    first = tmp_path / "first.json.gz"
    second = tmp_path / "second.json.gz"

    _download_filing_json(session, filing, first)
    _download_filing_json(session, filing, second)

    assert first.read_bytes() == second.read_bytes()
    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(
        second.read_bytes()
    ).hexdigest()


def test_download_ticker_quarterly_only_and_idempotent(tmp_path):
    from src.download.bmv_xbrl import download_ticker

    zips = {
        "https://www.bmv.com.mx/docs-pub/ifrsxbrl/ifrsxbrl_1_2026-01_1.zip":
            _zip_bytes("inst_2026.json", MINI_INSTANCE),
        "https://www.bmv.com.mx/docs-pub/ifrsxbrl/ifrsxbrl_2_2025-03_1.zip":
            _zip_bytes("inst_2025.json", MINI_INSTANCE),
    }
    session = _ZipSession(zips)

    paths = download_ticker(
        "sport", tmp_path, index=_mini_index(), session=session,
        delay_ms=0, write_artifacts=True,
    )

    names = [p.name for p in paths]
    # gzip-at-rest raw cache (adopted from the soft fork, 2026-07 reconcile)
    assert names == ["SPORT_2025-3T.json.gz", "SPORT_2026-1T.json.gz"]
    assert all(p.parent.name == "xbrl" for p in paths)
    # annual filing and other tickers untouched
    assert len(session.requested) == 2

    # artifacts written alongside
    assert (tmp_path / "xbrl" / "SPORT_2026-1T_facts.json").exists()
    assert (tmp_path / "xbrl" / "SPORT_2026-1T_mdna.html").exists()

    # second run downloads nothing new
    again = download_ticker(
        "SPORT", tmp_path, index=_mini_index(), session=session,
        delay_ms=0, write_artifacts=False,
    )
    assert [p.name for p in again] == names
    assert len(session.requested) == 2


def test_download_ticker_unknown_raises(tmp_path):
    from src.download.bmv_xbrl import download_ticker

    with pytest.raises(RuntimeError, match="No XBRL filings"):
        download_ticker("NOPE", tmp_path, index=_mini_index(), session=_ZipSession({}), delay_ms=0)


def test_download_ticker_rejects_non_zip(tmp_path, capsys):
    from src.download.bmv_xbrl import download_ticker

    session = _ZipSession({
        "https://www.bmv.com.mx/docs-pub/ifrsxbrl/ifrsxbrl_4_2026-01_1.zip": b"<html>error</html>",
    })
    paths = download_ticker("ALSEA", tmp_path, index=_mini_index(), session=session, delay_ms=0)
    assert paths == []
    assert "FAILED" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Artifact extraction
# ---------------------------------------------------------------------------

def test_extract_artifacts_facts_and_mdna(tmp_path):
    from src.download.bmv_xbrl import extract_artifacts

    src = tmp_path / "SPORT_2026-1T.json"
    src.write_text(json.dumps(MINI_INSTANCE), encoding="utf-8")

    out = extract_artifacts(src)
    facts = json.loads(out["facts"].read_text(encoding="utf-8"))["facts"]

    assert set(facts) == {"ifrs-full_Revenue", "ifrs-full_ProfitLoss"}  # nil fact dropped
    revenue = facts["ifrs-full_Revenue"][0]
    assert revenue["value"] == 1000000.0
    assert revenue["period_start"] == "2026-01-01"
    assert revenue["period_end"] == "2026-03-31"
    assert revenue["instant"] is None
    assert revenue["unit"] == "ISO4217:MXN"

    profit = facts["ifrs-full_ProfitLoss"][0]
    assert profit["value"] == -50000.0
    assert profit["instant"] == "2026-03-31"

    mdna = out["mdna"].read_text(encoding="utf-8")
    assert mdna.count("<section") == 2  # short note excluded
    # management commentary ordered before generic notes
    assert mdna.index("ifrs-mc_ManagementCommentaryExplanatory") < mdna.index(
        "ifrs-full_DisclosureOfLeasesExplanatory"
    )
    assert "comentario" in mdna and "arrendamientos" in mdna


def test_extract_artifacts_explicit_out_dir(tmp_path):
    from src.download.bmv_xbrl import extract_artifacts

    src = tmp_path / "X_2026-1T.json"
    src.write_text(json.dumps(MINI_INSTANCE), encoding="utf-8")
    target = tmp_path / "artifacts" / "nested"
    out = extract_artifacts(src, target)
    assert out["facts"].parent == target
    assert out["facts"].exists() and out["mdna"].exists()


def test_load_mdna_text_regenerates_artifacts(tmp_path):
    from src.download.bmv_xbrl import load_mdna_text

    src = tmp_path / "SPORT_2026-1T.json"
    src.write_text(json.dumps(MINI_INSTANCE), encoding="utf-8")
    text = load_mdna_text(src)  # no _mdna.html yet — must regenerate
    assert text and "comentario" in text and "arrendamientos" in text
    assert "<div>" not in text  # HTML stripped


# ---------------------------------------------------------------------------
# Pipeline hook (_from_bmv_xbrl)
# ---------------------------------------------------------------------------

def _write_filings(tmp_path) -> list[Path]:
    xbrl_dir = tmp_path / "xbrl"
    xbrl_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for period in ("2025-4T", "2026-1T"):
        path = xbrl_dir / f"SPORT_{period}.json"
        path.write_text(json.dumps(MINI_INSTANCE), encoding="utf-8")
        paths.append(path)
    return paths


def test_pipeline_from_bmv_xbrl(monkeypatch, tmp_path):
    from src.download import bmv_xbrl
    from src.extract import pipeline

    paths = _write_filings(tmp_path)
    monkeypatch.setattr(bmv_xbrl, "download_ticker", lambda *args, **kwargs: paths)

    docs = pipeline._from_bmv_xbrl("SPORT", tmp_path, None, 50)
    assert set(docs) == {"2025-4T", "2026-1T"}
    # PeriodSource now carries both MD&A text (Tier 3) and structured facts (Tier 1).
    src = docs["2026-1T"]
    assert "comentario" in src.text
    assert src.facts and "ifrs-full_Revenue" in src.facts

    filtered = pipeline._from_bmv_xbrl("SPORT", tmp_path, "2026", 50)
    assert set(filtered) == {"2026-1T"}


def test_pipeline_from_bmv_xbrl_failure_is_soft(monkeypatch, tmp_path, capsys):
    from src.download import bmv_xbrl
    from src.extract import pipeline

    def boom(*args, **kwargs):
        raise RuntimeError("No XBRL filings for ticker 'ACTINVR'")

    monkeypatch.setattr(bmv_xbrl, "download_ticker", boom)
    docs = pipeline._from_bmv_xbrl("ACTINVR", tmp_path, None, 50)
    assert docs == {}
    assert "using IR page only" in capsys.readouterr().err
