"""Tests for the analog-band precision transform: dedupe + boilerplate + topical rerank.

These guard the helpers that raise analog synonym-expansion precision without regressing recall
or wrong-sense: near-duplicate suppression, substanceless-boilerplate dropping (topical-fit
gated so a self-description that genuinely answers the query survives), and topical reranking.
"""
from __future__ import annotations

from src.search.fusion import (
    analyze_analog_candidates,
    build_corpus_boilerplate,
    dedupe_rerank_analog,
    rerank_sort_key,
    topical_fit,
)


def test_topical_fit_counts_distinct_query_terms_with_prefix_stem():
    text = "Grupo Bimbo has 223 bakeries and plants across the world"
    # bakery<->bakeries and plant<->plants align via the prefix-5 stem match.
    assert topical_fit(text, ["bakery", "plant"]) == 2
    # A term absent from the text does not count.
    assert topical_fit(text, ["confiteria"]) == 0
    # Exact match for short (<5-char) terms.
    assert topical_fit("pan dulce reposteria", ["pan", "dulce"]) == 2


def test_near_duplicate_keeps_first_drops_rest():
    # Same leading window (period/OCR variants) → only the best-ranked copy survives.
    items = [
        ("a", "Water = Still bottled water in 5.0, 19.0 and 20.0 liter packaging "
              "presentations includes flavored water transactions detail"),
        ("b", "Water = Still bottled water in 5.0, 19.0 and 20.0 liter packaging "
              "presentations includes flavored water TRANSACTIONS other segment"),
        ("c", "raw material costs mainly PET and sweeteners across our territories"),
    ]
    kept, dropped = dedupe_rerank_analog(items, ["pet", "packaging"])
    assert "a" in kept and "b" in dropped and "c" in kept
    assert kept.count("a") == 1


def test_boilerplate_with_zero_topical_fit_is_dropped():
    items = [
        ("boiler", "About Grupo Bimbo: Grupo Bimbo is the leader and largest baking company"),
        ("topical", "pan dulce and reposteria sales grew this quarter"),
    ]
    kept, dropped = dedupe_rerank_analog(items, ["pan", "dulce", "reposteria"])
    assert dropped == ["boiler"]
    assert kept == ["topical"]


def test_boilerplate_with_topical_substance_is_kept():
    # The SAME self-description is relevant when the query is actually about bakeries/plants —
    # signature match alone must never drop a hit that carries query substance.
    items = [("blurb", "Grupo Bimbo is the leader and largest baking company with 223 bakeries and plants")]
    kept, dropped = dedupe_rerank_analog(items, ["bakery", "plant", "closures"])
    assert kept == ["blurb"]
    assert dropped == []


def test_rerank_orders_by_topical_fit_descending():
    items = [
        ("low", "packaging presentations glossary line"),                 # fit 1 (packaging)
        ("high", "higher raw material costs mainly PET resin and packaging"),  # fit 3
    ]
    kept, _dropped = dedupe_rerank_analog(items, ["pet", "resin", "packaging", "costs"])
    assert kept[0] == "high"           # stronger topical fit rises above coincidental match


def test_boilerplate_demoted_on_topical_tie():
    items = [
        ("boiler", "conference call information: raw material discussion replay"),  # boilerplate
        ("plain", "raw material costs rose"),                                        # not boilerplate
    ]
    meta = analyze_analog_candidates(items, ["raw", "material"])
    # Both share topical fit 2, but the boilerplate flag breaks the tie downward.
    kept, _dropped = dedupe_rerank_analog(items, ["raw", "material"])
    assert meta["boiler"]["boiler"] is True
    assert kept == ["plain", "boiler"]


def test_empty_text_never_deduped_or_dropped():
    items = [("x", ""), ("y", "")]
    kept, dropped = dedupe_rerank_analog(items, ["anything"])
    assert set(kept) == {"x", "y"}
    assert dropped == []


def test_analyze_metadata_shape_and_order():
    items = [("a", "alpha topical resin"), ("b", "alpha topical resin")]
    meta = analyze_analog_candidates(items, ["resin"])
    assert meta["a"]["order"] == 0 and meta["b"]["order"] == 1
    assert meta["a"]["drop"] is False and meta["b"]["drop"] is True   # b is the dup
    assert meta["a"]["fit"] == 1
    assert meta["a"]["sim"] is None                                   # no sims supplied


# ---- Semantic rerank (MiniLM cosine augmenting the lexical topical fit) ---------------------

def test_semantic_sim_breaks_ties_among_equal_lexical_fit():
    # Two candidates with the SAME lexical fit (both echo "resin"); the higher semantic cosine to
    # the full query wins the tiebreak. This is the "augment topical_fit with MiniLM" behavior.
    items = [
        ("coincidental", "resin mentioned once in an unrelated finance footnote"),
        ("on_topic", "resin cost commentary"),
    ]
    sims = {"coincidental": 0.10, "on_topic": 0.80}
    kept, _dropped = dedupe_rerank_analog(items, ["resin"], sim_scores=sims)
    assert kept == ["on_topic", "coincidental"]


def test_lexical_fit_still_primary_over_semantic():
    # A stronger lexical fit outranks a higher semantic sim — fit is primary, sim only the tiebreak.
    items = [
        ("high_sim_low_fit", "packaging note"),                    # fit 1, sim 0.9
        ("high_fit_low_sim", "pet resin packaging costs"),         # fit 4, sim 0.2
    ]
    sims = {"high_sim_low_fit": 0.9, "high_fit_low_sim": 0.2}
    kept, _dropped = dedupe_rerank_analog(items, ["pet", "resin", "packaging", "costs"], sim_scores=sims)
    assert kept[0] == "high_fit_low_sim"


def test_rerank_sort_key_degrades_without_sim():
    lexical = rerank_sort_key({"fit": 3, "boiler": False, "order": 0, "sim": None})
    assert lexical == (-3, False, 0)
    semantic = rerank_sort_key({"fit": 3, "boiler": False, "order": 0, "sim": 0.5})
    assert semantic == (-3, -0.5, False, 0)


# ---- Corpus-frequency boilerplate detector -------------------------------------------------

def test_corpus_boilerplate_flags_cross_document_repeats():
    # A disclaimer repeated verbatim across many documents is boilerplate; a unique passage is not.
    disclaimer = ("this document may contain forward looking statements that should be "
                  "considered good faith estimates based upon currently available data")
    docs = [(f"doc{i}", disclaimer) for i in range(12)]
    docs.append(("unique", "our salty snacks category grew double digits led by botanas volume"))
    model = build_corpus_boilerplate(docs, min_docs=5, shingle_n=6)
    assert model.is_boilerplate(disclaimer) is True
    assert model.is_boilerplate(
        "our salty snacks category grew double digits led by botanas volume") is False


def test_corpus_boilerplate_predicate_drops_only_zero_fit():
    # Wired through analyze_analog_candidates: a corpus-boilerplate chunk with zero topical overlap
    # drops; the same signature carrying query substance survives (fit-gated, like the curated path).
    boiler_text = "safe harbor forward looking statements disclaimer paragraph filler text here"
    docs = [(f"d{i}", boiler_text) for i in range(10)]
    model = build_corpus_boilerplate(docs, min_docs=5, shingle_n=6)
    pred = lambda key, text: model.is_boilerplate(text)  # noqa: E731

    items = [("boiler", boiler_text), ("topical", "botanas salty snacks category portfolio")]
    kept, dropped = dedupe_rerank_analog(items, ["botanas", "snacks"], is_boilerplate=pred)
    assert dropped == ["boiler"] and kept == ["topical"]

    # Same boilerplate text, but now the query IS about the disclaimer terms → topical fit > 0 → kept.
    kept2, dropped2 = dedupe_rerank_analog([("boiler", boiler_text)],
                                           ["forward", "looking", "statements"], is_boilerplate=pred)
    assert kept2 == ["boiler"] and dropped2 == []
