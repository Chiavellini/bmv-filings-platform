from __future__ import annotations

import inspect

import src.extract.semantic_search as semantic_search_module
from src.extract.semantic_search import (
    SemanticMatcher,
    best_metric_match,
    build_metric_profile,
    load_search_dictionary,
    normalize_label,
    score_metric_label,
)
from src.model.financial_model import METRICS, attach_concept_map


def _defs(*keys):
    defs = attach_concept_map(METRICS)
    return [m for m in defs if m.key in keys]


def test_search_dictionary_loads_core_concepts():
    dictionary = load_search_dictionary()

    assert dictionary.version == 1
    assert "revenue" in dictionary.concept_aliases
    assert "resultado operativo" in dictionary.concept_aliases["operating_income"]
    assert "yoy" in dictionary.negative_labels


def test_normalize_label_is_accent_case_and_punctuation_insensitive():
    assert normalize_label(" Utilidad de Operación: ") == "utilidad de operacion"
    assert normalize_label("Ventas-Netas / Consolidadas") == "ventas netas consolidadas"


def test_build_metric_profile_merges_metric_and_dictionary_aliases():
    revenue = _defs("revenue")[0]
    profile = build_metric_profile(revenue)

    assert "revenue" in profile.aliases
    assert "ingresos consolidados" in profile.aliases
    assert "total de ingresos" in profile.aliases


def test_score_metric_label_matches_dictionary_only_alias():
    operating_income = _defs("operating_income")[0]
    result = score_metric_label("Resultado operativo", operating_income)

    assert result.score >= 0.85
    assert result.matched_alias == "resultado operativo"
    assert result.method in {"exact", "prefix", "contains", "token_overlap"}


def test_semantic_matcher_reuses_metric_profiles():
    defs = _defs("revenue", "operating_income")
    matcher = SemanticMatcher(defs)

    assert set(matcher.profiles) == {"revenue", "operating_income"}
    result = matcher.best_match("Resultado operativo")
    assert result is not None
    assert result.metric_key == "operating_income"
    assert result.matched_alias == "resultado operativo"


def test_best_metric_match_rejects_negative_derived_rows():
    defs = _defs("revenue", "gross_profit")

    assert best_metric_match("YoY", defs) is None
    assert best_metric_match("As % of Total", defs) is None
    assert best_metric_match("Margin", defs) is None


def test_best_metric_match_rejects_prose_statement_rows():
    defs = _defs("revenue", "total_stores")

    assert best_metric_match("Los Ingresos Totales finalizaron en $517.7", defs) is None
    assert best_metric_match("El Total de Ingresos sumó $653.9", defs) is None
    assert best_metric_match("tiendas. La Compañía también opera aproximadamente 345", defs) is None


def test_best_metric_match_rejects_subline_context_rows():
    defs = _defs("revenue")

    assert best_metric_match("Ingresos U12M* (mismos clubes)", defs) is None
    assert best_metric_match("Ingresos Trimestrales", defs) is None


def test_semantic_matcher_rejects_long_noisy_rows_before_scoring():
    revenue = _defs("revenue")[0]
    matcher = SemanticMatcher([revenue])
    label = (
        "Total de ingresos consolidados por formato operativo region canal "
        "marca unidad tiendas maduras tiendas nuevas ajustes eliminaciones"
    )

    assert matcher.best_match(label) is None
    result = matcher.score_metric_label(label, revenue)
    assert result.method == "long_label"
    assert result.score == 0.0


def test_best_metric_match_rejects_generic_revenue_sublines():
    defs = _defs("revenue")

    assert best_metric_match("Ingresos por Membresias", defs) is None
    assert best_metric_match("Revenue from Memberships", defs) is None
    assert best_metric_match("Total Otros Ingresos", defs) is None
    assert best_metric_match("Ingresos Totales", defs).metric_key == "revenue"


def test_non_prefix_contains_stays_below_match_threshold():
    operating_income = _defs("operating_income")[0]
    matcher = SemanticMatcher([operating_income])

    result = matcher.score_metric_label(
        "Total utilidad de operación al final del periodo",
        operating_income,
    )
    assert result.method == "contains"
    assert result.score < 0.85
    assert matcher.best_match("Total utilidad de operación al final del periodo") is None


def test_best_metric_match_respects_skip_keys():
    defs = _defs("revenue", "gross_profit")

    result = best_metric_match("Ingresos consolidados", defs, skip_keys=frozenset({"revenue"}))
    assert result is None or result.metric_key != "revenue"


def test_semantic_search_hot_path_does_not_use_sequence_matcher():
    assert "SequenceMatcher" not in inspect.getsource(semantic_search_module)


def test_residual_other_label_does_not_match_unmodified_alias():
    """'Other Accounts Receivable' is a residual line item, not AR (soriana
    2025-1T: 6,285 vs true 1,056). Same for the Spanish 'Otras cuentas...'."""
    defs = _defs("accounts_receivable", "accounts_payable")

    assert best_metric_match("Other Accounts Receivable", defs) is None
    assert best_metric_match("Other Accounts Payable", defs) is None
    assert best_metric_match("Otras cuentas por cobrar", defs) is None
    # The unmodified rows still match.
    assert best_metric_match("Accounts Receivable", defs).metric_key == "accounts_receivable"
    assert best_metric_match("Cuentas por cobrar", defs).metric_key == "accounts_receivable"


def test_scrambled_token_order_does_not_match():
    """The cash-flow row 'Payable and receivable accounts' contains both alias
    tokens but in the wrong order — it must not satisfy accounts_receivable."""
    defs = _defs("accounts_receivable")

    # 0.82 is the production text_search threshold; the scrambled row must stay
    # below it while the in-order variant clears it.
    assert best_metric_match("Payable and receivable accounts", defs, threshold=0.82) is None
    assert best_metric_match("Trade accounts receivable, net", defs, threshold=0.82) is not None
