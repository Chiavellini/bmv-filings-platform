"""Facet values + suggested terms — the click-widget option sources."""
from __future__ import annotations

import src.search.facets as facets_mod
from src.search.facets import facet_values, suggested_terms


def test_facet_values_from_fixture_index(built_index):
    store, _ = built_index
    f = facet_values(store)
    assert f.companies == ["acme"]
    assert f.doc_types == ["quarterly_release"]      # taxonomy-inferred from period filenames
    assert f.periods == ["2024-1T", "2024-2T"]   # sorted = chronological
    assert f.industries == []                    # fixture source has no industry tag
    assert [doc.doc_id for doc in f.documents] == ["acme/2024-2T", "acme/2024-1T"]


def test_facets_carry_cascade_relationships(built_index):
    store, _ = built_index
    f = facet_values(store)
    assert f.company_periods == {"acme": ["2024-1T", "2024-2T"]}
    assert "acme" in f.company_industry          # tag is None in the fixture, key exists
    assert f.company_industry["acme"] is None


def test_companies_in_cascades_by_industry():
    from src.search.facets import Facets
    f = Facets(companies=["bimbo", "femsa", "walmex"],
               company_industry={"bimbo": "food", "femsa": "conglomerate",
                                 "walmex": "retail"})
    assert f.companies_in([]) == ["bimbo", "femsa", "walmex"]
    assert f.companies_in(["food"]) == ["bimbo"]
    assert f.companies_in(["food", "retail"]) == ["bimbo", "walmex"]
    assert f.companies_in(["mining"]) == []


def test_periods_for_unions_and_sorts():
    from src.search.facets import Facets
    f = Facets(periods=["2014-1T", "2015-4T", "2016-1T"],
               company_periods={"bimbo": ["2014-1T", "2016-1T"],
                                "femsa": ["2015-4T", "2016-1T"]})
    assert f.periods_for([]) == ["2014-1T", "2015-4T", "2016-1T"]
    assert f.periods_for(["femsa"]) == ["2015-4T", "2016-1T"]
    assert f.periods_for(["bimbo", "femsa"]) == ["2014-1T", "2015-4T", "2016-1T"]
    assert f.periods_for(["unknown"]) == []


def test_documents_for_cascades_to_an_exact_source(built_index):
    store, _ = built_index
    f = facet_values(store)
    docs = f.documents_for(["acme"], [], "2024-2T", "2024-2T")
    assert [doc.doc_id for doc in docs] == ["acme/2024-2T"]
    assert docs[0].companies == ("acme",)


def test_documents_for_respects_selected_document_types(built_index):
    store, _ = built_index
    f = facet_values(store)
    assert [doc.doc_id for doc in f.documents_for([], [], doc_types=["news_article"])] == []
    assert [doc.doc_id for doc in f.documents_for([], [], doc_types=["quarterly_release"])] == [
        "acme/2024-2T", "acme/2024-1T",
    ]


def test_suggested_terms_humanized_and_capped():
    terms = suggested_terms(max_n=5)
    assert 0 < len(terms) <= 5
    assert all("_" not in t for t in terms)
    assert "revenue" in suggested_terms()


def test_suggested_terms_degrades_to_empty(monkeypatch):
    def _boom():
        raise RuntimeError("dictionary unavailable")

    import src.extract.semantic_search as ss
    monkeypatch.setattr(ss, "load_search_dictionary", _boom)
    assert facets_mod.suggested_terms() == []
