from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tests._corpus import requires_corpus_for  # noqa: E402


def _defs(*keys):
    from src.model.financial_model import METRICS, attach_concept_map

    defs = attach_concept_map(METRICS)
    return [m for m in defs if m.key in keys]


def test_metric_requests_from_labels_classifies_common_financial_outline():
    from src.extract.evidence import metric_defs_from_labels, metric_requests_from_labels

    reqs = metric_requests_from_labels([
        "Net Sales Gruma USA",
        "Volume (thousand tons)",
        "Average Price (Per thousand ton)",
        "USA EBITDA margin",
        "FX Effect",
    ])
    by_label = {r.label: r for r in reqs}

    assert by_label["Net Sales Gruma USA"].concept == "net_sales"
    assert by_label["Net Sales Gruma USA"].segment == "gruma usa"
    assert by_label["Volume (thousand tons)"].concept == "volume"
    assert by_label["Average Price (Per thousand ton)"].derived
    assert by_label["USA EBITDA margin"].concept == "ebitda"
    assert by_label["USA EBITDA margin"].derived
    assert by_label["FX Effect"].concept == "fx_effect"

    defs = metric_defs_from_labels(["Net Sales Gruma USA", "FX Effect", "YoY"])
    assert [m.key for m in defs] == ["net_sales_gruma_usa", "fx_effect"]
    assert defs[0].aliases == ["Net Sales Gruma USA"]


def test_extract_with_evidence_accepts_table_candidate_and_writes_diagnostics(tmp_path):
    from src.extract.evidence import (
        DocumentModel,
        DocumentTable,
        build_document_model,
        extract_with_evidence,
        write_candidates_csv,
        write_diagnostics,
    )
    from src.extract.tiered_extract import PeriodSource

    src = PeriodSource(period="2026-1T", text="No regex match here.")

    def fake_model(_src, cache_dir=None):
        return DocumentModel(
            period="2026-1T",
            text=_src.text,
            tables=[
                DocumentTable(page=1, rows=[
                    ("Total de ingresos", ["589,053", "517,708"]),
                    ("Utilidad bruta", ["220,000", "200,000"]),
                ])
            ],
        )

    import src.extract.evidence as evidence
    original = evidence.build_document_model
    evidence.build_document_model = fake_model
    try:
        result = extract_with_evidence(
            src,
            _defs("revenue", "gross_profit", "ebitda"),
            {},
            metric_keys=["revenue", "gross_profit", "ebitda"],
        )
    finally:
        evidence.build_document_model = original

    assert result.accepted["revenue"].current == 589_053
    assert result.accepted["gross_profit"].current == 220_000
    assert "ebitda" not in result.accepted
    assert any(d["metric"] == "ebitda" and d["status"] == "missing" for d in result.diagnostics)

    diag_path = tmp_path / "diag.json"
    cand_path = tmp_path / "candidates.csv"
    write_diagnostics(diag_path, [result])
    write_candidates_csv(cand_path, [result])
    assert json.loads(diag_path.read_text())["periods"][0]["period"] == "2026-1T"
    candidates = pd.read_csv(cand_path)
    assert set(candidates["metric"]) >= {"revenue", "gross_profit"}


def test_extract_with_evidence_uses_semantic_dictionary_for_table_candidates():
    from src.extract.evidence import DocumentModel, DocumentTable, extract_with_evidence
    from src.extract.tiered_extract import PeriodSource

    src = PeriodSource(period="2026-1T", text="No regex match here.")

    def fake_model(_src, cache_dir=None):
        return DocumentModel(
            period="2026-1T",
            text=_src.text,
            tables=[
                DocumentTable(page=1, rows=[
                    ("Resultado operativo", ["107,900", "95,000"]),
                ])
            ],
        )

    import src.extract.evidence as evidence
    original = evidence.build_document_model
    evidence.build_document_model = fake_model
    try:
        result = extract_with_evidence(
            src,
            _defs("operating_income"),
            {},
            metric_keys=["operating_income"],
        )
    finally:
        evidence.build_document_model = original

    assert result.accepted["operating_income"].current == 107_900
    candidate = next(c for c in result.candidates if c.metric == "operating_income")
    assert "semantic_score=" in candidate.reason
    assert "alias=resultado operativo" in candidate.reason


def test_extract_with_evidence_blanks_low_confidence_value():
    from src.extract.evidence import CandidateValue, _resolve_candidates

    accepted, diagnostics = _resolve_candidates(
        "2026-1T",
        _defs("revenue"),
        [CandidateValue(
            metric="revenue",
            value=10,
            prior=None,
            var_pct=None,
            unit="currency",
            source="prose",
            evidence="weak prose percentage",
            confidence=0.55,
        )],
        confidence_threshold=0.75,
    )

    assert accepted == {}
    assert diagnostics[0]["status"] == "low_confidence"


@requires_corpus_for("sport")
def test_pipeline_evidence_mode_outputs_candidates_and_diagnostics(tmp_path):
    from src.extract.pipeline import run

    diag = tmp_path / "diagnostics.json"
    cand = tmp_path / "candidates.csv"
    df = run(
        ROOT / "data" / "reports" / "sport" / "2026-1T.md",
        config=ROOT / "configs" / "sport.yaml",
        metrics=["revenue", "ebitda"],
        do_validate=False,
        use_evidence=True,
        diagnostics_path=diag,
        candidates_path=cand,
        verbose=False,
    )

    assert "period" in df.columns
    assert diag.exists()
    assert cand.exists()
    assert "revenue" in pd.read_csv(cand)["metric"].values
