"""Blind holdout gate: unseen Spanish content must be retrievable without dictionary edits."""
from __future__ import annotations

import pytest

from src.corpus.manifest import Document
from src.index.build import add_document_to_index
from src.index.embeddings import SentenceTransformerEmbedder
from src.index.store import IndexStore
from src.search.retriever import HybridRetriever

_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def _doc(tmp_path, doc_id: str, company: str, text: str) -> Document:
    path = tmp_path / f"{company}.md"
    path.write_text(text, encoding="utf-8")
    return Document(
        doc_id=doc_id,
        company=company,
        period="2025-1T",
        doc_type="internal",
        title=f"Unseen {company} upload",
        source_url=None,
        pdf_path=None,
        markdown_path=str(path),
        language="es",
        industry="unknown",
        extra={"blind_holdout": True},
    )


@pytest.mark.model
def test_unseen_cross_language_document_retrieves_without_synonym_dictionary(tmp_path):
    """No company/config vocabulary mentions the Spanish phrase used by the relevant document."""
    try:
        embedder = SentenceTransformerEmbedder(_MODEL, batch_size=8)
        _ = embedder.dim
    except Exception as exc:  # pragma: no cover - host/model availability gate
        pytest.skip(f"certified multilingual model unavailable: {exc}")

    store = IndexStore(tmp_path / "blind.db")
    store.connect()
    store.migrate()
    config = {"index": {"chunk": {"target_chars": 400, "overlap_chars": 50}}}

    relevant = (
        "La exposición al tipo de cambio peso dólar afectó los resultados del trimestre. "
        "La valorización de divisas generó un efecto negativo."
    )
    unrelated = (
        "Las ventas mismas tiendas crecieron por mayor tráfico y nuevas aperturas. "
        "El margen comercial se mantuvo estable."
    )
    add_document_to_index(
        store, _doc(tmp_path, "unknown_bank/2025-1T", "unknown_bank", relevant),
        relevant, config, embedder=embedder,
    )
    add_document_to_index(
        store, _doc(tmp_path, "unknown_retail/2025-1T", "unknown_retail", unrelated),
        unrelated, config, embedder=embedder,
    )

    retriever = HybridRetriever(store, embedder=embedder, expand_synonyms=False)
    hits = retriever.search("fx", keyword_weight=0.0, semantic_weight=1.0, limit=2)

    assert retriever.semantic_available
    assert hits and hits[0].doc_id == "unknown_bank/2025-1T"
    assert hits[0].match_kind == "semantic"
    assert hits[0].semantic_score is not None
