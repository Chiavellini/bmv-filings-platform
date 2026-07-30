from src.index.keyword_index import (
    bilingual_equivalent_phrases,
    bilingual_search_phrases,
    fts_match_query,
    metric_synonym_phrases,
)
from src.search.query_understanding import semantic_query_text


def test_financial_acronym_is_expanded_for_semantic_channel():
    assert semantic_query_text("FX exposure") == (
        "FX exposure foreign exchange currency exchange rates"
    )


def test_non_acronym_query_is_unchanged():
    assert semantic_query_text("pricing pressure in Mexico") == "pricing pressure in Mexico"


def test_spanish_and_english_financial_phrases_get_semantic_bridge_only():
    spanish = semantic_query_text("tipo de cambio")
    english = semantic_query_text("foreign exchange")
    assert spanish.startswith("tipo de cambio ") and "foreign exchange" in spanish
    assert english.startswith("foreign exchange ") and "tipo de cambio" in english


def test_short_revenue_labels_expand_bidirectionally_in_related_wording_lane():
    spanish = metric_synonym_phrases("ingresos")
    english = metric_synonym_phrases("revenue")
    assert "revenue" in spanish
    assert "ingresos" in english
    # The literal term itself remains excluded; it is counted only by the exact lane.
    assert "ingresos" not in [phrase.casefold() for phrase in spanish]
    assert "revenue" not in [phrase.casefold() for phrase in english]


def test_curated_expansions_never_repeat_the_literal_query():
    assert "fx" not in [phrase.casefold() for phrase in metric_synonym_phrases("fx")]


def test_passenger_search_has_bidirectional_direct_spanish_english_equivalence():
    assert "pasajeros" in bilingual_equivalent_phrases("passengers")
    assert "passengers" in bilingual_equivalent_phrases("pasajeros")
    assert "pasajeros" in bilingual_search_phrases("passengers")
    # Direct translations stay enabled even when broad synonym discovery is disabled.
    assert '"pasajeros"' in fts_match_query("passengers", expand_synonyms=False)
