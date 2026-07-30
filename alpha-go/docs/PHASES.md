# Pipeline Phases

Human map for alpha-go. The machine-readable source of truth is
[architecture/pipeline_phases.yaml](architecture/pipeline_phases.yaml); keep it authoritative
when phase wiring changes.

## Product Phases

1. **Ingest** (`src.corpus`) — download + parse configured IR sources into a document
   corpus + manifest. Wraps the vendored `download` and `parse` phases; adds no new
   download/parse logic. Entrypoints: `ingest_source`, `ingest_all`.

2. **Index** (`src.index`) — chunk the corpus and build the hybrid index: SQLite **FTS5**
   keyword (BM25, stdlib) + **local-embedding** vectors, stored with document/chunk metadata
   in one SQLite db. Entrypoint: `build_index`.

3. **Search** (`src.search`) — hybrid retrieval: BM25 + semantic, fused by reciprocal rank
   (`fusion.reciprocal_rank_fusion`), scoped by `SearchFilters`, returning ranked `Hit`s
   with highlighted `Snippet`s. Deterministic and offline. Stable entrypoint:
   `HybridRetriever.search`.

4. **Financials** (`src.extract`, reused) — per-company metric panel via the vendored 4-tier
   cascade (`extract_metrics_tiered`). No new extraction logic.

5. **Q&A** (`src.qa`, optional) — retrieval-augmented, cited answers (`rag.answer`). The only
   LLM-using feature; **off by default**.

6. **Dashboard** (`app/streamlit_app.py`) — Streamlit UI: search · document · financials ·
   Q&A. A thin adapter over the service interfaces.

Reused quality gates: `src.shared.validator` (confidence/sanity for financials) and
`src.eval.compare_extractions` (accuracy vs ground truth).

## Phase Graph

```text
ingest -> index -> search ---------> dashboard
                     \-> qa --------/
          index -> financials -----/
```

## Directory Ownership

- `src/corpus/`: ingestion orchestration + corpus manifest.
- `src/index/`: chunking, SQLite store/schema, FTS5 keyword index, embeddings, build.
- `src/search/`: hybrid retriever, rank fusion, snippets, filters.
- `src/qa/`: optional RAG synthesis + prompts.
- `app/`: the Streamlit dashboard (thin UI).
- `src/{download,parse,extract,shared,model,excel,eval}/`: **vendored** — do not edit in
  place; re-sync via `scripts/vendor_sync.py`.
- `configs/`: `alpha_go.yaml` (top-level) + vendored company configs and dictionaries.
- `data/corpus/`, `data/index/`: generated artifacts (gitignored).

## Agent Workflow

Start with `architecture/pipeline_phases.yaml` when a task changes phase wiring or
entrypoints. Build in [ROADMAP.md](ROADMAP.md) order. The Phase-0 gate is:

```bash
python3 -m pytest tests/ -q
```

Do not edit vendored packages directly; flow changes from the parent through
`scripts/vendor_sync.py`. Keep search deterministic and offline — the LLM lives only in the
`qa` phase and is off by default.
