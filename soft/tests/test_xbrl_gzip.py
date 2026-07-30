"""Gzip-at-rest for the raw XBRL cache: helpers + transparent read through the
extract/facts/mdna paths. All offline (no network)."""
from __future__ import annotations

import gzip
import json

import pytest

from src.download.bmv_xbrl import (
    _logical_stem,
    _read_raw_text,
    _resolve_raw,
    extract_artifacts,
    load_mdna_text,
)
from src.extract.pipeline import _load_facts


def _minimal_instance() -> dict:
    """A tiny but structurally-valid BMV XBRL instance: one numeric fact + one MD&A block."""
    return {
        "HechosPorIdConcepto": {
            "ifrs-full_Revenue": ["F1"],
            "DisclosureOfRevenueExplanatory": ["F2"],
        },
        "HechosPorId": {
            "F1": {
                "EsValorNil": False, "EsNumerico": True, "ValorNumerico": 1000.0,
                "Valor": "1000", "IdContexto": "C1", "IdUnidad": "U1", "Decimales": "-3",
            },
            "F2": {
                "EsValorNil": False, "EsNumerico": False,
                "Valor": "Comentarios de la administracion. " + ("x" * 2500),
            },
        },
        "ContextosPorId": {"C1": {"Periodo": {"FechaFin": "2024-12-31"}}},
        "UnidadesPorId": {"U1": {"Medidas": [{"Etiqueta": "MXN"}]}},
    }


def _write_gz(path, doc) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh)


# --- helpers ---------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("AC_2021-2T.json.gz", "AC_2021-2T"),
    ("AC_2021-2T.json", "AC_2021-2T"),
    ("AC_2021-2T_facts.json", "AC_2021-2T"),
    ("GFNORTE_2025-FY.json.gz", "GFNORTE_2025-FY"),
])
def test_logical_stem_strips_full_suffix(tmp_path, name, expected):
    assert _logical_stem(tmp_path / name) == expected


def test_resolve_raw_prefers_gz(tmp_path):
    plain = tmp_path / "AC_2021-2T.json"
    gz = tmp_path / "AC_2021-2T.json.gz"
    plain.write_text("{}", encoding="utf-8")
    _write_gz(gz, {"k": 1})
    # given the logical .json path, resolve to the .gz when it exists
    assert _resolve_raw(plain) == gz
    # given a .gz path directly, return it unchanged
    assert _resolve_raw(gz) == gz


def test_read_raw_text_transparent(tmp_path):
    gz = tmp_path / "AC_2021-2T.json.gz"
    _write_gz(gz, {"hello": "world"})
    assert json.loads(_read_raw_text(gz)) == {"hello": "world"}
    # also via the logical .json path
    assert json.loads(_read_raw_text(tmp_path / "AC_2021-2T.json")) == {"hello": "world"}


# --- transparent extraction from a gzipped raw filing ----------------------

def test_extract_artifacts_reads_gz_and_names_siblings_correctly(tmp_path):
    gz = tmp_path / "AC_2024-4T.json.gz"
    _write_gz(gz, _minimal_instance())

    out = extract_artifacts(gz)

    # Correct sibling names — NOT AC_2024-4T.json_facts.json / .json_mdna.html
    assert out["facts"].name == "AC_2024-4T_facts.json"
    assert out["mdna"].name == "AC_2024-4T_mdna.html"
    assert out["facts"].exists() and out["mdna"].exists()

    facts = json.loads(out["facts"].read_text(encoding="utf-8"))["facts"]
    assert facts["ifrs-full_Revenue"][0]["value"] == 1000.0


def test_load_facts_and_mdna_from_gz(tmp_path):
    gz = tmp_path / "AC_2024-4T.json.gz"
    _write_gz(gz, _minimal_instance())

    facts = _load_facts(gz)
    assert facts and facts["ifrs-full_Revenue"][0]["value"] == 1000.0

    text = load_mdna_text(gz)
    assert text and "Comentarios de la administracion" in text
