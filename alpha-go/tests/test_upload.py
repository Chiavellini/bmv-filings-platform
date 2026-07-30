"""Incremental single-doc indexing + the upload orchestration (offline hashing embedder)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.corpus.manifest import Document, load_manifest
from src.corpus.upload import add_uploaded_document, extract_uploaded_text, slugify, suggest_company
from src.index.build import add_document_to_index
from src.index.embeddings import get_embedder
from src.index.keyword_index import KeywordIndex
from src.index.store import IndexStore

_HASHING = {"index": {"embedding_backend": "hashing", "chunk": {"target_chars": 400,
                                                                "overlap_chars": 50}}}


def _embedder():
    emb, _ = get_embedder(_HASHING)
    return emb


def _doc(doc_id, company, text_marker):
    return Document(
        doc_id=doc_id, company=company, period="2025-1T", doc_type="quarterly_release",
        title=f"{company} doc", source_url=None, pdf_path=None,
        markdown_path=f"/tmp/{doc_id}.md", language="en", industry="food", extra={})


def _store(tmp_path):
    s = IndexStore(tmp_path / "idx.db")
    s.connect()
    s.migrate()
    return s


def test_add_document_is_incremental(tmp_path):
    store = _store(tmp_path)
    emb = _embedder()
    md_a = "Alpha company results.\n\nRevenue grew on strong zebracorn demand this quarter."
    md_b = "Beta company results.\n\nMargins expanded on lower quetzalplast input costs."

    add_document_to_index(store, _doc("co/a", "co", "zebracorn"), md_a, _HASHING, embedder=emb)
    add_document_to_index(store, _doc("co/b", "co", "quetzalplast"), md_b, _HASHING, embedder=emb)

    # Both docs present — the second add did NOT clear the first.
    assert store.count("documents") == 2
    assert store.count("chunks") >= 2
    assert store.count("embeddings") == store.count("chunks")
    # FTS reachable for each distinctive term.
    ki = KeywordIndex(store)
    assert any(h.chunk_id.startswith("co/a") for h in ki.search("zebracorn", limit=10))
    assert any(h.chunk_id.startswith("co/b") for h in ki.search("quetzalplast", limit=10))


def test_readd_replaces_cleanly(tmp_path):
    store = _store(tmp_path)
    emb = _embedder()
    add_document_to_index(store, _doc("co/a", "co", "x"),
                          "First version mentions zebracorn.", _HASHING, embedder=emb)
    n1 = store.count("chunks")
    add_document_to_index(store, _doc("co/a", "co", "x"),
                          "Rewritten version mentions quetzalplast only.", _HASHING, embedder=emb)
    assert store.count("documents") == 1                 # still one doc
    ki = KeywordIndex(store)
    assert not ki.search("zebracorn", limit=10)          # old chunk/FTS gone
    assert ki.search("quetzalplast", limit=10)           # new content present
    assert store.count("embeddings") == store.count("chunks")


def test_add_uploaded_document_txt(tmp_path):
    store = _store(tmp_path)
    corpus = tmp_path / "corpus"
    data = b"Internal strategy memo. Our zebracorn packaging initiative targets rPET adoption."
    stats = add_uploaded_document(
        store, _HASHING, slug="acme", company="Acme Corp", industry="materials",
        doc_type_key="internal", title="Strategy memo", label="2024-strategy",
        filename="memo.txt", data=data, embedder=_embedder(), corpus_dir=corpus)

    assert stats["chunks"] >= 1 and stats["company"] == "acme"
    # Corpus markdown + manifest written.
    assert (corpus / "acme" / "2024-strategy.md").exists()
    manifest = load_manifest(corpus)
    uploaded_doc = next(d for d in manifest.documents if d.doc_id == "acme/2024-strategy")
    assert uploaded_doc.doc_type == "internal"
    assert uploaded_doc.original_format == "txt"
    assert (corpus / uploaded_doc.original_path).read_bytes() == data
    # Searchable immediately.
    assert KeywordIndex(store).search("zebracorn", limit=10)


def test_upload_uses_shared_storage_and_registers_through_bridge(tmp_path):
    class _RecordingBridge:
        def __init__(self):
            self.calls = []

        def register_alpha_upload(self, **kwargs):
            self.calls.append(kwargs)
            return f"alpha-go:{kwargs['alpha_doc_id']}"

    store = _store(tmp_path)
    corpus = tmp_path / "corpus"
    storage = tmp_path / "portable-estate" / "user" / "alpha-go"
    bridge = _RecordingBridge()

    stats = add_uploaded_document(
        store, _HASHING, slug="acme", company="Acme", industry="transport",
        doc_type_key="quarterly_release", title="Quarter", label="2025-1T",
        filename="quarter.txt", data=b"Passengers increased.",
        embedder=_embedder(), corpus_dir=corpus, storage_dir=storage,
        estate_bridge=bridge,
    )

    assert stats["estate_registered"] is True
    assert stats["estate_document_id"] == "alpha-go:acme/2025-1T"
    assert stats["estate_error"] is None
    assert Path(stats["markdown_path"]).is_relative_to(storage)
    assert bridge.calls[0]["memberships"] == [
        {"company": "acme", "industry": "transport"}
    ]
    document = next(
        d for d in load_manifest(corpus).documents
        if d.doc_id == "acme/2025-1T"
    )
    assert Path(document.original_path).is_relative_to(storage)


def test_upload_surfaces_estate_registration_failure_without_losing_search(tmp_path):
    class _FailingBridge:
        def register_alpha_upload(self, **_kwargs):
            raise RuntimeError("catalog locked")

    store = _store(tmp_path)
    corpus = tmp_path / "corpus"
    stats = add_uploaded_document(
        store, _HASHING, slug="acme", company="Acme", industry="transport",
        doc_type_key="internal", title="Memo", label="memo",
        filename="memo.txt", data=b"Zebracorn demand increased.",
        embedder=_embedder(), corpus_dir=corpus,
        estate_bridge=_FailingBridge(),
    )

    assert stats["estate_registered"] is False
    assert stats["estate_document_id"] is None
    assert stats["estate_error"] == "RuntimeError: catalog locked"
    assert KeywordIndex(store).search("zebracorn", limit=10)
    assert load_manifest(corpus).documents[0].doc_id == "acme/memo"


def test_upload_period_collision_never_overwrites_an_existing_document(tmp_path):
    store = _store(tmp_path)
    corpus = tmp_path / "corpus"
    first = add_uploaded_document(
        store, _HASHING, slug="acme", company="Acme", industry="food",
        doc_type_key="quarterly_release", title="Official quarter", label="2024-3T",
        filename="official.txt", data=b"Official results mention zebracorn.",
        embedder=_embedder(), corpus_dir=corpus,
    )
    second = add_uploaded_document(
        store, _HASHING, slug="acme", company="Acme", industry="food",
        doc_type_key="internal", title="Sector note", label="2024-3T",
        filename="sector-note.txt", data=b"Independent note mentions quetzalplast.",
        embedder=_embedder(), corpus_dir=corpus,
    )

    assert first["label"] == "2024-3T"
    assert second["renamed_for_collision"]
    assert second["label"].startswith("2024-3T-sector-note")
    assert store.count("documents") == 2
    assert (corpus / "acme" / "2024-3T.md").read_text().startswith("Official results")
    assert (corpus / "acme" / f"{second['label']}.md").read_text().startswith(
        "Independent note"
    )
    assert KeywordIndex(store).search("zebracorn", limit=10)
    assert KeywordIndex(store).search("quetzalplast", limit=10)


def test_failed_incremental_embedding_rolls_back_the_index(tmp_path):
    class _FailingEmbedder:
        def encode(self, texts):
            raise RuntimeError("synthetic embedding failure")

    store = _store(tmp_path)
    corpus = tmp_path / "corpus"
    with pytest.raises(RuntimeError, match="synthetic embedding failure"):
        add_uploaded_document(
            store, _HASHING, slug="acme", company="Acme Corp", industry="materials",
            doc_type_key="internal", title="Failed memo", label="failed",
            filename="memo.txt", data=b"zebracorn", embedder=_FailingEmbedder(),
            corpus_dir=corpus,
        )

    assert store.count("documents") == 0
    assert store.count("chunks") == 0
    assert store.count("embeddings") == 0
    assert load_manifest(corpus).documents == []
    assert not (corpus / "acme" / "failed.md").exists()
    assert not (corpus / "acme" / "failed.txt").exists()


def test_multi_company_upload(tmp_path):
    """A drop-in assigned to several corpora: one physical doc, findable under each company."""
    from src.search.facets import facet_values
    from src.search.filters import SearchFilters
    from src.search.retriever import HybridRetriever

    store = _store(tmp_path)
    corpus = tmp_path / "corpus"
    data = b"In-house note on zebracorn logistics spanning Acme and Globex operations."
    stats = add_uploaded_document(
        store, _HASHING,
        targets=[{"slug": "acme", "company": "Acme Corp", "industry": "materials"},
                 {"slug": "globex", "company": "Globex", "industry": "food"}],
        doc_type_key="internal", title="Shared memo", label="2024-shared",
        filename="memo.txt", data=data, embedder=_embedder(), corpus_dir=corpus)

    # One physical document, primary = the first target; both memberships recorded.
    assert store.count("documents") == 1
    assert store.count("document_companies") == 2
    assert stats["company"] == "acme" and stats["companies"] == ["acme", "globex"]
    assert {m["company"] for m in store.document_companies("acme/2024-shared")} == {"acme", "globex"}
    # File lives once, under the primary company's directory.
    assert (corpus / "acme" / "2024-shared.md").exists()
    assert not (corpus / "globex").exists()

    # Manifest carries both memberships and is findable by_company under each.
    manifest = load_manifest(corpus)
    doc = next(d for d in manifest.documents if d.doc_id == "acme/2024-shared")
    assert doc.company_slugs() == ["acme", "globex"]
    assert [d.doc_id for d in manifest.by_company("globex")] == ["acme/2024-shared"]

    # Company-scoped retrieval matches under EITHER company; a cross-company scope returns it once.
    retr = HybridRetriever(store, embedder=_embedder(), expand_synonyms=False)
    for slug in ("acme", "globex"):
        hits = retr.search("zebracorn", filters=SearchFilters(companies=[slug]), limit=10)
        assert hits, f"scope {slug} returned nothing"
        assert all(slug in h.companies for h in hits)
    both = retr.search("zebracorn", filters=SearchFilters(companies=["acme", "globex"]), limit=10)
    assert len({h.doc_id for h in both}) == 1
    assert set(both[0].companies) == {"acme", "globex"}

    # Facets offer the doc under both corpora, each with its own industry.
    facets = facet_values(store)
    assert {"acme", "globex"} <= set(facets.companies)
    assert facets.company_industry == {"acme": "materials", "globex": "food"}
    assert {"materials", "food"} <= set(facets.industries)


def test_slugify():
    assert slugify("Acme Corp!") == "acme_corp"
    assert slugify("  Bodega Aurrerá  ") == "bodega_aurrer"   # non-ascii dropped
    assert slugify("") == "doc"


def test_suggest_company():
    companies = ["bimbo", "walmex", "ac", "ac_bebidas"]
    # Whole-token match on the filename stem.
    assert suggest_company("bimbo_annual_report_2024.pdf", companies) == "bimbo"
    assert suggest_company("2025-2T_walmex_release.pdf", companies) == "walmex"
    # Longest match wins so the short "ac" never shadows "ac_bebidas".
    assert suggest_company("ac_bebidas_4T24.pdf", companies) == "ac_bebidas"
    # No known company in the filename → None (caller treats it as a new company).
    assert suggest_company("some_random_memo.pdf", companies) is None


def test_extract_uploaded_text_is_non_mutating_for_plain_text(tmp_path):
    assert extract_uploaded_text("unknown.txt", b"Contenido financiero nuevo.") == (
        "Contenido financiero nuevo."
    )
    assert list(tmp_path.iterdir()) == []


def test_pdf_reader_manifest_cache_can_refresh_after_upload():
    from app.components import pdf_view

    pdf_view._manifest_pdf_paths()
    assert pdf_view._manifest_pdf_paths.cache_info().currsize == 1
    pdf_view.invalidate_manifest_cache()
    assert pdf_view._manifest_pdf_paths.cache_info().currsize == 0
