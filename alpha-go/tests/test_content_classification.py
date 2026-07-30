"""Blind, content-first classification for previously unseen uploads."""
from __future__ import annotations

from src.corpus.classification import classify_document
from src.corpus.doc_types import load_taxonomy


def test_content_overrides_misleading_filename():
    taxonomy = load_taxonomy({
        "default_doc_type": "internal",
        "doc_types": [
            {"key": "quarterly_release", "label": "Quarterly", "keywords": ["trimestre", "2t"]},
            {"key": "annual_report", "label": "Annual", "keywords": ["annual", "informe anual"]},
            {"key": "internal", "label": "Other", "keywords": []},
        ],
    })
    text = """
    Grupo Financiero Banorte, S.A.B. de C.V.
    Resultados del segundo trimestre 2T25
    Banorte reportó crecimiento en sus ingresos y utilidad durante el trimestre.
    """
    result = classify_document(
        "walmex_annual_report_2024.pdf",
        text,
        companies=["walmex", "banorte"],
        taxonomy=taxonomy,
    )

    assert result.company.value == "banorte"
    assert [signal.value for signal in result.companies] == ["banorte"]
    assert "content mention" in " ".join(result.company.evidence)
    assert result.doc_type.value == "quarterly_release"
    assert result.language.value == "es"
    assert result.period.value == "2025-2T"


def test_unknown_company_is_suggested_from_document_header():
    taxonomy = load_taxonomy()
    text = """
    Compañía Industrial Ejemplo, S.A.B. de C.V.
    Información financiera y resultados del 1T25.
    Los ingresos consolidados aumentaron durante el trimestre.
    """
    result = classify_document(
        "document.pdf", text, companies=["banorte", "walmex"], taxonomy=taxonomy
    )
    assert result.company.value is None
    assert result.suggested_company_name
    assert "Compañía Industrial Ejemplo" in result.suggested_company_name
    assert result.language.value == "es"


def test_language_is_unknown_when_content_has_no_evidence():
    result = classify_document(
        "opaque.bin", "1234 XYZ", companies=[], taxonomy=load_taxonomy()
    )
    assert result.language.value == "unknown"
    assert result.language.confidence == 0.0


def test_multi_company_research_report_returns_all_materially_covered_known_names():
    taxonomy = load_taxonomy()
    text = """
    Food & Beverage sector initiation
    We are initiating coverage of Bimbo, KOF and FEMSA.
    Bimbo has strong distribution. KOF has beverage exposure. FEMSA operates OXXO.
    Our Bimbo estimates reflect margins. KOF should benefit from volumes. FEMSA has optionality.
    Bimbo valuation is balanced. KOF is preferred. FEMSA remains attractive.
    Walmart is mentioned once as a retailer comparison.
    """
    result = classify_document(
        "sector_report.pdf", text,
        companies=[
            {"slug": "bimbo", "company": "Grupo Bimbo"},
            {"slug": "kof", "company": "Coca-Cola FEMSA"},
            {"slug": "femsa", "company": "FEMSA"},
            {"slug": "walmex", "company": "Walmart de México"},
        ],
        taxonomy=taxonomy,
    )

    assert set(signal.value for signal in result.companies) == {"bimbo", "kof", "femsa"}
    assert "walmex" not in {signal.value for signal in result.companies}
