"""Index layer — chunk the corpus and build the hybrid search index.

Keyword index: SQLite FTS5 (BM25), stdlib only.
Semantic index: local embeddings (sentence-transformers) in a vector store.
Both persist alongside document/chunk metadata in a single SQLite database.
See docs/architecture/system_design.md (Index phase) and docs/ROADMAP.md Phase 2.
"""
