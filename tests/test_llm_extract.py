"""
test_llm_extract.py — Tier 4 LLM fallback.

All tests inject a fake ``complete_fn`` and a temp cache dir, so they are
deterministic and never touch the network or require the anthropic package.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.model.financial_model import METRICS, attach_concept_map
from src.extract.extract_metrics import MetricRow
from src.extract.llm_extract import llm_extract, _build_schema, _evidence_present, _normalize


def _defs(*keys):
    defs = attach_concept_map(METRICS)
    return [m for m in defs if m.key in keys]


def _fake(payload, counter=None):
    def fn(*, model, system, user, schema):
        if counter is not None:
            counter.append(1)
        return payload
    return fn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_evidence_present_accent_insensitive():
    text = _normalize("El EBITDA sin IFRS 16 fue de $97.3 millones en el trimestre.")
    assert _evidence_present("$97.3 millones", text)
    assert not _evidence_present("$500.0 millones", text)


def test_schema_shape():
    schema = _build_schema(_defs("ebitda_sin_ifrs"))
    assert schema["additionalProperties"] is False
    assert "ebitda_sin_ifrs" in schema["properties"]
    assert schema["required"] == ["ebitda_sin_ifrs"]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_extracts_value_with_valid_evidence(tmp_path):
    text = "El EBITDA sin IFRS 16 alcanzó $97.3 millones durante el periodo."
    payload = {"ebitda_sin_ifrs": {"current": 97300, "prior": None,
                                   "evidence": "$97.3 millones"}}
    out = llm_extract(_defs("ebitda_sin_ifrs"), text, {},
                      cache_dir=tmp_path, complete_fn=_fake(payload))
    assert out["ebitda_sin_ifrs"].current == 97300
    assert out["ebitda_sin_ifrs"].source_line.startswith("[llm]")


def test_hallucinated_evidence_is_dropped(tmp_path):
    text = "El EBITDA sin IFRS 16 alcanzó $97.3 millones."
    payload = {"ebitda_sin_ifrs": {"current": 500000, "prior": None,
                                   "evidence": "$500.0 millones de dolares"}}  # not in text
    out = llm_extract(_defs("ebitda_sin_ifrs"), text, {},
                      cache_dir=tmp_path, complete_fn=_fake(payload))
    assert out == {}


def test_null_current_skipped(tmp_path):
    text = "No relevant figures here."
    payload = {"ebitda_sin_ifrs": {"current": None, "evidence": ""}}
    out = llm_extract(_defs("ebitda_sin_ifrs"), text, {},
                      cache_dir=tmp_path, complete_fn=_fake(payload))
    assert out == {}


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_response_is_cached(tmp_path):
    text = "EBITDA sin IFRS 16: $97.3 millones."
    payload = {"ebitda_sin_ifrs": {"current": 97300, "evidence": "$97.3 millones"}}
    calls: list = []
    fn = _fake(payload, calls)
    defs = _defs("ebitda_sin_ifrs")
    llm_extract(defs, text, {}, cache_dir=tmp_path, complete_fn=fn)
    llm_extract(defs, text, {}, cache_dir=tmp_path, complete_fn=fn)
    assert len(calls) == 1   # second call served from cache


def test_unit_aware_system_prompt():
    """The prompt's unit clause must follow company.unit — a millions company
    prompted for thousands returns values 1000x off."""
    from src.extract.llm_extract import _system_prompt

    thousands = _system_prompt({"company": {"unit": "miles_mxn"}})
    assert "THOUSANDS" in thousands and "MXN" in thousands

    millions = _system_prompt({"company": {"unit": "millions"}})
    assert "MILLIONS" in millions and "THOUSANDS" not in millions

    usd = _system_prompt({"company": {"unit": "millions", "currency": "USD"}})
    assert "MILLIONS" in usd and "USD" in usd

    # No config → conservative default (thousands of MXN, the project default)
    assert "THOUSANDS" in _system_prompt(None)


def test_unit_change_busts_cache(tmp_path):
    """Same text, different company.unit → different cache entry AND prompt."""
    text = "EBITDA sin IFRS 16: $97.3 millones."
    payload = {"ebitda_sin_ifrs": {"current": 97300, "evidence": "$97.3 millones"}}
    systems: list = []

    def fn(*, model, system, user, schema):
        systems.append(system)
        return payload

    defs = _defs("ebitda_sin_ifrs")
    llm_extract(defs, text, {}, cfg={"company": {"unit": "miles_mxn"}},
                cache_dir=tmp_path, complete_fn=fn)
    llm_extract(defs, text, {}, cfg={"company": {"unit": "millions"}},
                cache_dir=tmp_path, complete_fn=fn)
    assert len(systems) == 2                  # second call NOT served from cache
    assert "THOUSANDS" in systems[0] and "MILLIONS" in systems[1]


# ---------------------------------------------------------------------------
# Cross-check validation
# ---------------------------------------------------------------------------

def test_value_failing_identity_is_dropped(tmp_path):
    # gross_profit must ≈ revenue - cogs = 60,000; the LLM's 999,999 breaks it
    already = {
        "revenue": MetricRow("revenue", "Ingresos", 100_000, None, None, "currency", "[xbrl] x"),
        "cogs": MetricRow("cogs", "Costo", 40_000, None, None, "currency", "[xbrl] x"),
    }
    text = "La utilidad bruta fue de 999,999 segun la administracion."
    payload = {"gross_profit": {"current": 999_999, "evidence": "999,999"}}
    out = llm_extract(_defs("gross_profit"), text, already,
                      cache_dir=tmp_path, complete_fn=_fake(payload))
    assert "gross_profit" not in out


def test_value_passing_identity_is_kept(tmp_path):
    already = {
        "revenue": MetricRow("revenue", "Ingresos", 100_000, None, None, "currency", "[xbrl] x"),
        "cogs": MetricRow("cogs", "Costo", 40_000, None, None, "currency", "[xbrl] x"),
    }
    text = "La utilidad bruta fue de 60,000 en el periodo."
    payload = {"gross_profit": {"current": 60_000, "evidence": "60,000"}}
    out = llm_extract(_defs("gross_profit"), text, already,
                      cache_dir=tmp_path, complete_fn=_fake(payload))
    assert out["gross_profit"].current == 60_000


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_skips_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = llm_extract(_defs("ebitda_sin_ifrs"), "some text", {}, cache_dir=tmp_path)
    assert out == {}


def test_skips_when_nothing_missing(tmp_path):
    already = {"ebitda_sin_ifrs": MetricRow("ebitda_sin_ifrs", "x", 1.0, None, None, "currency", "[xbrl]")}
    out = llm_extract(_defs("ebitda_sin_ifrs"), "text", already,
                      cache_dir=tmp_path, complete_fn=_fake({"ebitda_sin_ifrs": {"current": 5}}))
    assert out == {}
