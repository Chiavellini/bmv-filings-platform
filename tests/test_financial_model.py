"""
test_financial_model.py — model phase: config loaders + concept-map attach.

These were previously exercised only indirectly through full extraction runs.
Here they are unit-tested directly so config-merge bugs surface at load time.
"""

from __future__ import annotations

import textwrap

from src.shared.paths import CONFIGS_DIR
from src.model.financial_model import (
    METRICS, METRIC_BY_KEY, MetricDef,
    load_config, apply_config, load_concept_map, attach_concept_map,
)


def test_load_config_walmex_shape():
    cfg = load_config(CONFIGS_DIR / "walmex.yaml")
    assert cfg["company"]["ticker"] == "WALMEX"
    assert "metric_overrides" in cfg and "custom_metrics" in cfg
    assert any(s["name"] == "mexico" for s in cfg["sections"])


def test_apply_config_merges_overrides_and_custom_metrics():
    cfg = load_config(CONFIGS_DIR / "walmex.yaml")
    merged = apply_config(METRICS, cfg)
    by_key = {m.key: m for m in merged}

    # Base metrics are preserved …
    assert len(merged) >= len(METRICS)
    # … custom WALMEX KPIs are appended …
    assert "net_sales" in by_key and "total_stores" in by_key
    # … and the revenue override prepends WALMEX patterns (more than the base).
    assert len(by_key["revenue"].patterns) > len(METRIC_BY_KEY["revenue"].patterns)


def test_apply_config_empty_is_noop():
    merged = apply_config(METRICS, {})
    assert [m.key for m in merged] == [m.key for m in METRICS]


def test_load_concept_map_from_explicit_path(tmp_path):
    p = tmp_path / "concepts.yaml"
    p.write_text(textwrap.dedent("""
        metrics:
          revenue:
            xbrl_concepts: [ifrs-full_Revenue]
            aliases: ["Ventas netas"]
    """))
    mapping = load_concept_map(p)
    assert mapping["revenue"]["xbrl_concepts"] == ["ifrs-full_Revenue"]


def test_attach_concept_map_fills_only_empty_fields():
    mapping = {
        "revenue": {"xbrl_concepts": ["ifrs-full_Revenue"], "aliases": ["Ventas"]},
    }
    # A metric with empty concepts/aliases gets them filled.
    bare = MetricDef(key="revenue", label="Revenue", label_es="Ingresos",
                     section="income", unit="currency", patterns=[])
    out = {m.key: m for m in attach_concept_map([bare], mapping)}
    assert out["revenue"].xbrl_concepts == ["ifrs-full_Revenue"]
    assert out["revenue"].aliases == ["Ventas"]

    # A metric that already declares concepts keeps its own (no overwrite).
    owned = MetricDef(key="revenue", label="Revenue", label_es="Ingresos",
                      section="income", unit="currency", patterns=[],
                      xbrl_concepts=["custom_Concept"], aliases=["Mine"])
    out2 = {m.key: m for m in attach_concept_map([owned], mapping)}
    assert out2["revenue"].xbrl_concepts == ["custom_Concept"]
    assert out2["revenue"].aliases == ["Mine"]


def test_attach_concept_map_noop_when_mapping_empty():
    bare = MetricDef(key="revenue", label="Revenue", label_es="Ingresos",
                     section="income", unit="currency", patterns=[])
    out = attach_concept_map([bare], {})
    assert out[0].xbrl_concepts == []
