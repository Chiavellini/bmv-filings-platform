# System Design

This is the authoritative design for alpha-go's net-new layers. The vendored layers
(download/parse/extract/model/shared) keep their parent contracts unchanged — see
[REUSE_MAP.md](../REUSE_MAP.md).

## Component & data flow

```text
                 ┌──────────── VENDORED (reused as-is) ────────────┐
 IR sources ──▶  │ download (downloader, bmv_xbrl, wayback)        │
                 │ parse    (parse_pdf -> aligned markdown+DocMeta) │
                 └─────────────────────┬───────────────────────────┘
                                       ▼
        ┌──────────────────────── NEW ───────────────────────────────────────┐
 INGEST  src.corpus.ingest ──▶ data/corpus/<doc>.md + manifest.json
                                       │
 INDEX   src.index.build:  chunker ──▶ chunks
                            store    ──▶ SQLite: documents, chunks
                            keyword_index ──▶ chunks_fts (FTS5/BM25)
                            embeddings    ──▶ embeddings (local vectors)
                                       │
 SEARCH  src.search.HybridRetriever.search(query, filters)
            keyword(BM25) ┐
                          ├─▶ fusion.RRF ─▶ filters ─▶ snippets ─▶ [Hit, Hit, ...]
            semantic(cos) ┘
                                       │
                       ┌───────────────┼────────────────────────┐
                       ▼               ▼                         ▼
 FINANCIALS  reuse extract.       SEARCH UI / doc viewer    QA  src.qa.rag.answer
 (per company) tiered_extract     (highlighted snippets)    (optional, cited, LLM)
                       └───────────────┴────────────────────────┘
                                       ▼
 DASHBOARD  app/streamlit_app.py  (search · document · financials · Q&A tabs)
```

One-way flow: ingest → index → search/financials/qa → UI. Validation/eval are reused as
quality gates, not product phases.

## Storage schema (SQLite — `data/index/alpha_go.db`)

Single database, defined authoritatively in `src/index/store.py::SCHEMA_SQL`:

| Table | Role |
|-------|------|
| `documents` | one row per parsed report: `doc_id`, `company`, `period`, `doc_type`, `title`, `source_url`, `pdf_path`, `markdown_path`, `language` |
| `chunks` | retrievable units: `chunk_id`, `doc_id`, `ordinal`, `text`, `char_start`, `char_end`, `heading` |
| `chunks_fts` | **Standard** FTS5 table (`chunk_id UNINDEXED, text`) — BM25 keyword search (stdlib `sqlite3`, no extra dep). Self-contained rather than external-content, since the index rebuilds wholesale. |
| `embeddings` | `chunk_id`, `dim`, `vector` (float32 BLOB) — cosine ranked in-process with numpy |
| `meta` | `schema_version`, `embedding_model`, `embedding_dim`, `built_at`, `documents`, `chunks` |

**Embedding backend (`get_embedder`, `index.embedding_backend`):** `auto` uses
sentence-transformers when installed, else falls back to a deterministic offline
`HashingEmbedder` (hashed bag-of-words, L2-normalized) — so the whole hybrid pipeline builds
and tests offline with no model download, and upgrades to real semantics transparently after
`pip install sentence-transformers`. `build_index(..., embedder=)` is injectable for tests.

`char_start`/`char_end` on `chunks` (and the per-hit snippet spans) are what let the doc
viewer highlight the exact matched passage in the original markdown.

## Retrieval design (the heart of it)

Hybrid, deterministic, offline:

1. **Keyword** — FTS5 `MATCH` over `chunks_fts` → BM25 ranking. Query terms optionally
   expanded with the vendored deterministic synonym dictionary
   (`src.extract.semantic_search` + `configs/metric_search.yaml`) for "Smart Synonyms"
   behavior.
2. **Semantic** — embed the query with the local model (`src.index.embeddings.Embedder`),
   cosine-rank against the `embeddings` table.
3. **Fusion** — `src.search.fusion.reciprocal_rank_fusion` merges the two ranked id-lists.
   RRF is scale-robust (BM25 magnitudes vs cosine in [-1,1]); a `keyword_weight` /
   `semantic_weight` lets the UI bias either way.
4. **Filter** — `src.search.filters.SearchFilters.to_sql()` constrains by
   company/period/doc-type/language via the `documents` join.
5. **Hydrate** — assemble `Hit` rows and build a highlighted `Snippet` per hit
   (`src.search.snippets.make_snippet`).

`HybridRetriever.search(...) -> list[Hit]` is the **stable interface** the dashboard and the
Q&A layer both depend on; it is defined now (Phase 0) so downstream work can target it.

## Financials panel (reuse, don't rebuild)

The per-company financials tab calls the vendored cascade directly:
`src.model.financial_model.load_config/apply_config` → `src.extract.tiered_extract.extract_metrics_tiered`
→ `src.shared.validator.validate`. No new extraction logic; alpha-go just renders the result
beside the search hits for the same company.

## Q&A (optional, the only LLM use)

`src.qa.rag.answer(question, hits)` numbers the top hits' snippets, prompts an LLM
(`src.qa.prompts.SYSTEM_PROMPT`, anti-hallucination: answer only from snippets, cite every
claim), and returns a `CitedAnswer`. Gated behind `qa.enabled` + `ANTHROPIC_API_KEY`. Off by
default; never on the search path.

## Determinism & quality gates

- Ingest → index → search produce identical results given identical inputs (no network, no
  LLM). Embedding model is pinned in config and recorded in `meta`.
- LLM appears only in `qa` and is off by default.
- Chunk offsets must round-trip: `markdown[chunk.char_start:chunk.char_end] == chunk.text`.
- Vendored modules are not edited in place; changes flow from the parent via
  `scripts/vendor_sync.py`.
