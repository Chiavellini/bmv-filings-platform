"""Tests for the corpus manifest persistence (Phase 1)."""
from __future__ import annotations

from src.corpus.manifest import (
    CorpusManifest,
    Document,
    load_manifest,
    manifest_path,
)


def _doc(doc_id: str, company: str, period: str) -> Document:
    return Document(
        doc_id=doc_id,
        company=company,
        period=period,
        doc_type="report",
        title=f"{company} {period}",
        source_url=None,
        pdf_path=None,
        markdown_path=f"{company}/{period}.md",
        language="en",
    )


def test_save_then_load_round_trips(tmp_path):
    m = CorpusManifest(documents=[
        _doc("acme/2024-2T", "acme", "2024-2T"),
        _doc("acme/2024-1T", "acme", "2024-1T"),
    ])
    path = manifest_path(tmp_path)
    written = path  # expected location
    assert manifest_path(tmp_path) == tmp_path / "manifest.json"

    from src.corpus.manifest import save_manifest
    saved = save_manifest(m, tmp_path)
    assert saved == written and saved.exists()

    loaded = load_manifest(tmp_path)
    assert len(loaded.documents) == 2
    # Persisted sorted by doc_id.
    assert [d.doc_id for d in loaded.documents] == ["acme/2024-1T", "acme/2024-2T"]
    assert loaded.documents[0].period == "2024-1T"
    assert loaded.documents[0].markdown_path.endswith(".md")


def test_load_missing_returns_empty(tmp_path):
    loaded = load_manifest(tmp_path)
    assert isinstance(loaded, CorpusManifest)
    assert loaded.documents == []


def test_by_company_filters():
    m = CorpusManifest(documents=[
        _doc("acme/2024-1T", "acme", "2024-1T"),
        _doc("globex/2024-1T", "globex", "2024-1T"),
    ])
    assert [d.doc_id for d in m.by_company("acme")] == ["acme/2024-1T"]
