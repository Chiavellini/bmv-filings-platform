"""Analog dictionary — gated, weighted domain-vocabulary query expansion.

Covers three things the sharpened feature promises:
  1. every one of the 36 analog groups still expands from a representative alias (recall);
  2. the documented polysemy traps do NOT over-expand into the other sense (precision);
  3. the plumbing — per-alias weight bands, split keyword search, and the diversity cap —
     that keeps synonym hits from crowding out literal answers.
"""
from __future__ import annotations

import logging

import pytest

from src.index.keyword_index import (
    KeywordIndex,
    _analog_meta,
    _dictionary,
    concept_phrase_bands,
    fts_match_query,
    synonym_phrases,
    _STRONG_MIN,
)
from src.search.fusion import cap_analog_floods


def _reset():
    # analogs.yaml / metric_search.yaml may have been imported before this process cached them.
    _dictionary.cache_clear()
    _analog_meta.cache_clear()


def _phrases(query: str) -> list[str]:
    _reset()
    return [p.lower() for p in synonym_phrases(query)]


def _analog_concepts() -> dict[str, tuple]:
    _reset()
    d = _dictionary()
    return {name[len("analog::"):]: aliases
            for name, aliases in d.concept_aliases.items()
            if name.startswith("analog::")}


def _firing_alias(concept: str, aliases: tuple) -> str:
    """A query that legitimately fires ``concept``: a strong, non-guarded (unambiguous) alias."""
    meta = _analog_meta().get(concept)
    guarded = meta.gate.guarded if (meta and meta.gate) else frozenset()
    weights = meta.alias_weights if meta else {}
    default = meta.default_weight if meta else 1.0
    strong_unambiguous = [a for a in aliases
                          if a not in guarded and weights.get(a, default) >= _STRONG_MIN]
    # prefer the most specific (multi-word) alias to avoid accidental cross-concept firing
    strong_unambiguous.sort(key=lambda a: -len(a.split()))
    return strong_unambiguous[0] if strong_unambiguous else aliases[0]


# --------------------------------------------------------------------------- coverage: 36 groups

def test_all_38_analog_groups_present():
    # 36 original domain/material groups + 2 geographic-segment groups (europe_eaa_segment,
    # latam_segment) added to bridge Bimbo's opaque "EAA" acronym to Europe/europa queries.
    concepts = _analog_concepts()
    assert len(concepts) == 38, f"expected 38 analog groups, found {len(concepts)}"


@pytest.mark.parametrize("concept", sorted(_analog_concepts()))
def test_every_group_expands_from_its_primary_alias(concept):
    """Each group's representative alias surfaces the rest of the group (Smart-Synonyms recall)."""
    aliases = _analog_concepts()[concept]
    query = _firing_alias(concept, aliases)
    expanded = set(_phrases(query))
    present = [a for a in aliases if a in expanded]
    assert len(present) >= 2, (
        f"{concept!r}: querying {query!r} surfaced only {present} of {list(aliases)}")


# --------------------------------------------------------------------------- precision: traps

# (trap query, phrases that MUST NOT appear because the query is the *other* sense)
POLYSEMY_TRAPS = [
    ("pet food promotions",            ["pet", "resin", "plastic", "pet resin"]),
    ("adopt a pet at the pet store",   ["resin", "plastic", "paraxylene"]),
    ("sugar-free beverages launch",    ["raw material", "raw materials", "sweetener"]),
    ("no sugar added product",         ["raw material", "materias primas"]),
    ("frying pan cookware",            ["bread", "pan de caja", "tortillas", "sliced bread"]),
    ("Panama market entry",            ["bread", "pan de caja", "bollos"]),
    ("water treatment plant capex",    ["bottled water", "agua", "jug water"]),
    ("water scarcity and water stress", ["bottled water", "agua embotellada"]),
    ("related-party transactions note", ["number of transactions", "transacciones",
                                         "total transactions"]),
    ("transaction costs on the deal",  ["number of transactions", "transacciones"]),
    ("magnifying glass and glass ceiling", ["aluminum", "vidrio", "glass bottle"]),
    ("one-way street traffic study",   ["returnable", "retornable", "refillable",
                                        "botella retornable"]),
    ("planta baja retail floor space", ["bakery", "bakeries", "panificadora"]),
    ("SSS is an unrelated acronym here", ["same store sales", "comparable sales"]),
    ("snacks logistics footprint",     ["salty snacks", "botanas", "frituras"]),
    # sugar-free / sugar-tax beverage-policy query must NOT drag in the beverage-category sense
    # (which surfaced a raw-material sugar-COST passage — the documented wrong-sense leak).
    ("bebidas sin azucar y azucar reducida impuesto",
     ["refresco", "bebidas", "sparkling", "soft drink", "sweetener", "azucar"]),
    ("sugar-free zero sugar sweetened beverages tax",
     ["refresco", "sparkling", "soft drink", "sweetener"]),
]


@pytest.mark.parametrize("query,forbidden", POLYSEMY_TRAPS)
def test_polysemy_trap_does_not_over_expand(query, forbidden):
    expanded = set(_phrases(query))
    leaked = [f for f in forbidden if f in expanded]
    assert not leaked, f"{query!r} leaked into the other sense: {leaked}"


# ------------------------------------------------------------------ precision: legit still fires

LEGIT_EXPANSIONS = [
    ("PET resin cost inflation",        "plastic"),      # flagship: PET -> plastic
    ("plastic packaging material",      "pet"),          # and back: plastic -> PET
    ("sugar cost pressure on margins",  "sweetener"),    # sugar-as-input-cost sense
    ("sliced bread and buns portfolio", "pan"),          # bakery pan sense
    ("bottled water category volume",   "agua"),         # water-as-beverage sense
    ("number of transactions per store", "transacciones"),
    ("glass bottle recycling",          "aluminum"),     # glass-as-container sense
    ("returnable and one-way presentations", "refillable"),
    ("manufacturing plant closures at the bakery", "planta"),
    ("salty snacks category growth",    "botanas"),
    ("comparable store sales growth",   "sss"),
    ("refresco carbonatado category",   "sparkling"),   # ordinary beverage query still fires
    ("bebida gaseosa y refresco carbonatado", "soft drink"),
]


@pytest.mark.parametrize("query,expected", LEGIT_EXPANSIONS)
def test_legitimate_query_still_expands(query, expected):
    assert expected in _phrases(query), f"{query!r} should still surface {expected!r}"


# --------------------------------------------------------------------------- per-alias weighting

def test_risky_bare_token_is_weak_band_unambiguous_is_strong():
    _reset()
    bands = concept_phrase_bands("PET resin bottle packaging")
    strong, weak = bands["pet_resin_plastic"]
    assert "pet" in weak and "meg" in weak            # bare risky tokens demoted
    assert "pet resin" in strong and "resin" in strong  # unambiguous phrases kept strong


def test_missing_weight_defaults_to_strong():
    _reset()
    # cookies has no alias_weights/gate -> everything defaults to weight 1.0 (strong band)
    strong, weak = concept_phrase_bands("galletas")["cookies"]
    assert set(strong) >= {"cookies", "biscuits"} and weak == []


# --------------------------------------------------------------------------- backward-compat

def test_analog_concepts_are_namespaced_and_metrics_survive():
    _reset()
    d = _dictionary()
    assert any(name.startswith("analog::") for name in d.concept_aliases)
    assert "revenue" in d.concept_aliases            # union with metric dict, not replacement


def test_fts_match_query_includes_gated_expansion_only_when_legit():
    _reset()
    assert '"plastic"' in fts_match_query("PET resin", expand_synonyms=True)
    # opt-out: without expansion only the literal terms are queried
    assert '"plastic"' not in fts_match_query("PET resin", expand_synonyms=False)
    # gated: the pet-animal sense does not inject resin/plastic
    assert '"plastic"' not in fts_match_query("pet food", expand_synonyms=True)


# --------------------------------------------------------------------------- split keyword search

def test_search_split_partitions_literal_and_expansion(built_index):
    store, _ = built_index
    ki = KeywordIndex(store)
    # 'uafida' (ES EBITDA alias) never appears literally; it only reaches the EBITDA chunk via
    # the strong expansion band, tagged with its concept.
    split = ki.search_split("uafida", expand_synonyms=True)
    assert split.literal == []                       # no literal 'uafida' in the corpus
    assert split.strong, "metric synonym should surface via the strong band"
    assert all(cid in split.concept_of for cid in
               {h.chunk_id for h in split.strong})     # every expansion hit carries provenance


def test_search_split_no_expansion_is_literal_only(built_index):
    store, _ = built_index
    split = KeywordIndex(store).search_split("revenues", expand_synonyms=False)
    assert split.literal and split.strong == [] and split.weak == []
    assert split.concept_of == {}


# --------------------------------------------------------------------------- diversity cap

def test_cap_analog_floods_demotes_excess_but_keeps_literals():
    # a literal hit (L), then 4 analog-only hits from one concept "c" for company "X"
    candidates = [
        ("L", "X", set(), False),
        ("a1", "X", {"c"}, True),
        ("a2", "X", {"c"}, True),
        ("a3", "X", {"c"}, True),
        ("a4", "X", {"c"}, True),
    ]
    out = cap_analog_floods(candidates, concept_cap=2, company_cap=99)
    assert out[0] == "L"                              # literal never demoted
    assert out == ["L", "a1", "a2", "a3", "a4"]       # a3,a4 sink to the tail (kept, not dropped)
    # with the cap wide open, order is preserved
    assert cap_analog_floods(candidates, concept_cap=9, company_cap=9) == \
        ["L", "a1", "a2", "a3", "a4"]


def test_cap_analog_floods_company_cap():
    candidates = [("a1", "X", {"c"}, True), ("a2", "X", {"d"}, True),
                  ("b1", "Y", {"c"}, True)]
    out = cap_analog_floods(candidates, concept_cap=99, company_cap=1)
    assert out == ["a1", "b1", "a2"]                  # X's 2nd analog hit demoted below Y's


# --------------------------------------------------------------------------- fail-loud on bad yaml

def test_malformed_analogs_logs_warning(tmp_path, monkeypatch, caplog):
    import src.shared.paths as paths
    bad = tmp_path / "analogs.yaml"
    bad.write_text("concepts: [not, a, mapping]\n")
    monkeypatch.setattr(paths, "CONFIGS_DIR", tmp_path)
    _analog_meta.cache_clear()
    with caplog.at_level(logging.WARNING):
        meta = _analog_meta()
    assert meta == {}                                 # degrades to no gating
    assert any("analogs.yaml" in r.message for r in caplog.records)
    _analog_meta.cache_clear()
