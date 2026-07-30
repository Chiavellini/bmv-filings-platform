"""Tests for SearchFilters.to_sql (Phase 3)."""
from __future__ import annotations

from src.search.filters import SearchFilters


def test_empty_filter_yields_no_clause():
    where, params = SearchFilters().to_sql()
    assert where == ""
    assert params == []
    assert SearchFilters().is_empty()


def test_exact_document_filter():
    where, params = SearchFilters(doc_ids=["acme/2024-1T", "acme/2024-2T"]).to_sql()
    assert "documents.doc_id IN (?, ?)" in where
    assert params == ["acme/2024-1T", "acme/2024-2T"]


def test_company_filter():
    where, params = SearchFilters(companies=["walmex", "lacomer"]).to_sql()
    # Company scope resolves through the many-to-many junction so a doc shared across corpora
    # matches under any of its companies.
    assert "document_companies" in where and "EXISTS" in where
    assert "dc.company IN (?, ?)" in where
    assert params == ["walmex", "lacomer"]


def test_combined_filters_are_anded():
    f = SearchFilters(companies=["walmex"], doc_types=["report"],
                      period_from="2022-1T", period_to="2022-4T", languages=["es"])
    where, params = f.to_sql()
    assert "dc.company IN (?)" in where           # company scope via junction EXISTS
    assert "documents.doc_type IN (?)" in where
    assert "documents.language IN (?)" in where
    assert "documents.period >= ?" in where
    assert "documents.period <= ?" in where
    assert params == ["walmex", "report", "es", "2022-1T", "2022-4T"]


def test_period_only_bounds():
    where, params = SearchFilters(period_from="2023-1T").to_sql()
    assert where == "documents.period >= ?"
    assert params == ["2023-1T"]
