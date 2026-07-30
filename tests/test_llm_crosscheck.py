"""
test_llm_crosscheck.py — the advisory second-model cross-check (check #2 of the
three-check model). Covers: agreement classification per unit, the evidence
anti-hallucination gate, unit-aware prompting, cache round-trip, provider
resolution, and — most importantly — the ADVISORY GUARANTEE: a total
disagreement never flips a gate verdict.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.extract.extract_metrics import MetricRow
from src.extract.llm_crosscheck import (
    agreement_summary, crosscheck_metrics, values_agree,
)


def _row(key: str, current: float, unit: str = "currency", label: str = "Ventas") -> MetricRow:
    return MetricRow(metric=key, label_es=label, current=current, prior=None,
                     var_pct=None, unit=unit, source_line="[search] x")


# ---------------------------------------------------------------------------
# values_agree
# ---------------------------------------------------------------------------

def test_values_agree_currency_tolerance():
    assert values_agree(1000.0, 1015.0, "currency")        # 1.5% < 2%
    assert not values_agree(1000.0, 1030.0, "currency")    # 3% > 2%
    assert values_agree(0.3, 0.7, "currency")              # within ±0.5 floor
    assert not values_agree(1000.0, 2000.0, "currency")


def test_values_agree_pct_and_count():
    assert values_agree(41.2, 41.5, "pct")                 # 0.3pp < 0.5pp
    assert not values_agree(41.2, 42.0, "pct")
    assert values_agree(365, 365, "count")
    assert not values_agree(365, 366, "count")


# ---------------------------------------------------------------------------
# crosscheck_metrics
# ---------------------------------------------------------------------------

_TEXT = "Las ventas netas alcanzaron $1,000 millones. EBITDA de $250 millones."


def test_crosscheck_agree_and_disagree(tmp_path):
    found = {"revenue": _row("revenue", 1000.0), "ebitda": _row("ebitda", 999.0)}

    def fake(system, user):
        assert "JSON" in system            # json_object contract
        return {
            "revenue": {"current": 1000, "evidence": "$1,000 millones"},
            "ebitda": {"current": 250, "evidence": "$250 millones"},
        }

    out = crosscheck_metrics(found, _TEXT, {}, cache_dir=tmp_path, complete_fn=fake)
    assert out["revenue"]["agree"] is True
    assert out["ebitda"]["agree"] is False          # engine 999 vs model 250
    assert out["ebitda"]["engine_value"] == 999.0


def test_unlocatable_evidence_is_uncheckable_not_disagreement(tmp_path):
    found = {"revenue": _row("revenue", 1000.0)}

    def fake(system, user):
        return {"revenue": {"current": 500, "evidence": "totally fabricated quote"}}

    out = crosscheck_metrics(found, _TEXT, {}, cache_dir=tmp_path, complete_fn=fake)
    assert out["revenue"]["agree"] is None
    assert out["revenue"]["value"] is None


def test_null_answer_is_uncheckable(tmp_path):
    found = {"revenue": _row("revenue", 1000.0)}
    out = crosscheck_metrics(found, _TEXT, {}, cache_dir=tmp_path,
                             complete_fn=lambda system, user: {"revenue": None})
    assert out["revenue"]["agree"] is None


def test_cache_round_trip(tmp_path):
    found = {"revenue": _row("revenue", 1000.0)}
    calls = []

    def fake(system, user):
        calls.append(1)
        return {"revenue": {"current": 1000, "evidence": "$1,000 millones"}}

    crosscheck_metrics(found, _TEXT, {}, cache_dir=tmp_path, complete_fn=fake)
    crosscheck_metrics(found, _TEXT, {}, cache_dir=tmp_path, complete_fn=fake)
    assert len(calls) == 1


def test_unit_aware_prompt_for_millions_company(tmp_path):
    found = {"revenue": _row("revenue", 1000.0)}
    seen = {}

    def fake(system, user):
        seen["system"] = system
        return {}

    crosscheck_metrics(found, _TEXT, {"company": {"unit": "millions"}},
                       cache_dir=tmp_path, complete_fn=fake)
    assert "MILLIONS" in seen["system"]


def test_no_provider_degrades_silently(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    found = {"revenue": _row("revenue", 1000.0)}
    assert crosscheck_metrics(found, _TEXT, {}, cache_dir=tmp_path) == {}


def test_agreement_summary():
    checks = {
        ("2024-1T", "revenue"): {"agree": True},
        ("2024-1T", "ebitda"): {"agree": False},
        ("2024-2T", "revenue"): {"agree": True},
        ("2024-2T", "capex"): {"agree": None},
    }
    s = agreement_summary(checks)
    assert (s["checked"], s["agreed"], s["disagreed"], s["unchecked"]) == (3, 2, 1, 1)
    assert s["rate"] == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# THE ADVISORY GUARANTEE — disagreement never gates
# ---------------------------------------------------------------------------

def test_total_disagreement_never_blocks_strong():
    from src.eval.verification_gate import score_metrics

    df = pd.DataFrame([
        {"period": "2024-1T", "revenue": 100.0},
        {"period": "2024-2T", "revenue": 110.0},
        {"period": "2024-3T", "revenue": 120.0},
    ])
    df.attrs["confidence"] = {
        (p, "revenue"): {
            "confidence": 0.95, "flagged": False, "source": "[bmv] x",
            # fabricated total disagreement from the cross-check
            "crosscheck": {"model": "deepseek-chat", "value": 9e9,
                           "engine_value": v, "agree": False, "evidence": "q"},
        }
        for p, v in (("2024-1T", 100.0), ("2024-2T", 110.0), ("2024-3T", 120.0))
    }
    scores, is_strong, worklist = score_metrics(df, ["revenue"])
    assert is_strong is True                       # advisory: never gates
    assert all(not s.suspects for s in scores)
    assert worklist == []
    assert scores[0].llm_agree == "0/3"            # ...but the tally is visible


def test_scorecard_shows_llm_agree_column_only_when_present():
    from src.eval.verification_gate import MetricScore, scorecard_markdown

    without = scorecard_markdown([MetricScore("revenue", "STRONG", 3, 3, 0.9)], True)
    assert "LLM agree" not in without

    with_cc = scorecard_markdown(
        [MetricScore("revenue", "STRONG", 3, 3, 0.9, llm_agree="3/3")], True)
    assert "LLM agree" in with_cc and "3/3" in with_cc


def test_validation_report_crosscheck_section():
    from src.eval.validation_report import build_validation_report

    df = pd.DataFrame([{"period": "2024-1T", "revenue": 100.0}])
    df.attrs["confidence"] = {
        ("2024-1T", "revenue"): {
            "confidence": 0.95, "flagged": False, "source": "[bmv] x",
            "crosscheck": {"model": "deepseek-chat", "value": 250.0,
                           "engine_value": 100.0, "agree": False, "evidence": "q"},
        },
    }
    report = build_validation_report(df, ["revenue"], name="Test Co", slug="testco")
    assert "LLM cross-check (advisory)" in report
    assert "disagreeing cell" in report
    assert "deepseek-chat" in report
