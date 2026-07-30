# Roadmap

Build order for agents picking up alpha-go after the foundation. Each phase fills stubbed
entrypoints and adds tests. Keep `architecture/pipeline_phases.yaml` authoritative; keep
search deterministic/offline; never edit vendored packages in place.

Legend: `[x]` done · `[ ]` to do.

## Phase 0 — Foundation ✅ (this deliverable)

- [x] Scaffold `alpha-go/` tree; vendor parent `src/` + configs (`scripts/vendor_sync.py`).
- [x] Self-contained packaging (`pyproject.toml` pythonpath, `requirements.txt`, `.gitignore`).
- [x] Stub the new layers with typed contracts (corpus/index/search/qa) + Streamlit shell.
- [x] Documentation set (this `docs/` tree).
- [x] Phase-0 smoke tests green; vendored imports re-root into alpha-go.

## Phase 1 — Ingest → corpus ✅

- [x] Implement `src/corpus/ingest.py::ingest_source` / `ingest_all` over the vendored
      `download_from_ir` + `parse_pdf` + `index_report_files` (reuse-if-exists; network
      failures are non-fatal so offline runs work).
- [x] Implement `src/corpus/manifest.py::load_manifest` / `save_manifest` (JSON at
      `data/corpus/manifest.json`); `Document.period` via `infer_period_label`.
- [x] `scripts/ingest.py` wired to `ingest_source` / `ingest_all`.
- [x] Tests: offline fixture corpus (`tests/fixtures/sample_corpus/`) → one `Document` per
      period, manifest round-trips, reuse-if-exists skips download; opt-in `network` test.

Phase-1 note: `index_report_files` picks one primary file per period → one `Document` per
period. Multiple doc-types per period is a later enhancement.

## Phase 2 — Index (FTS5 + embeddings) ✅

- [x] `src/index/store.py`: `connect`/`migrate` (`SCHEMA_SQL`, `meta`) + write/read helpers
      (`upsert_document`, `insert_chunk`, `upsert_embedding`, `clear`, `count`). `chunks_fts`
      is a standard (self-contained) FTS5 table for robust wholesale rebuilds.
- [x] `src/index/chunker.py::chunk_document`: paragraph-packed, heading-aware, overlap;
      guarantees `markdown[char_start:char_end] == text`.
- [x] `src/index/keyword_index.py::rebuild`: repopulate `chunks_fts` from `chunks`.
- [x] `src/index/embeddings.py`: `HashingEmbedder` (offline, deterministic) +
      `SentenceTransformerEmbedder` (lazy) + `get_embedder` (`embedding_backend: auto`).
- [x] `src/index/build.py::build_index`: corpus → chunks → FTS5 + vectors; returns stats;
      `embedder=` injectable for tests.
- [x] `scripts/build_index.py` wired. Tests: schema, offset round-trip, FTS5 `bm25()` match,
      vectors stored, idempotent rebuild, auto-fallback. `@pytest.mark.model` opt-in for the
      real ST model.

## Phase 3 — Hybrid search ✅

- [x] `src/search/fusion.py::reciprocal_rank_fusion` (weighted, deterministic tie-break).
- [x] `src/index/keyword_index.py::search` (FTS5 MATCH + optional synonym expansion via
      `src.extract.semantic_search` + `metric_search.yaml`).
- [x] Semantic search: embed query, cosine over `embeddings` (numpy) —
      `src/index/vector_index.py` (`load_matrix` + `search_matrix`, cacheable for warm search).
- [x] `src/search/filters.py::to_sql`; `src/search/snippets.py::make_snippet` (highlight spans).
- [x] `src/search/retriever.py::HybridRetriever.search`: combine → fuse → filter → hydrate Hits;
      holds a cached vector matrix; degrades to keyword-only when no embedder. `scripts/search_demo.py` CLI.
- [x] Tests: RRF ranking, FTS5 + synonym recall, semantic ranking, filters narrow, snippet spans.

Built on real data: index of 477 reports / 53,997 chunks / 53,997 MiniLM embeddings
(`all-MiniLM-L6-v2`, 384-dim) over 13 companies. Built with a Python 3.11 `.venv`
(sentence-transformers + torch); the offline `HashingEmbedder` remains the default fallback.

## Phase 4 — Dashboard ✅

- [x] `app/components/panels.py::render_search`: lists Hits with highlighted snippets (HTML via
      `app/components/highlight.py`) + "open in Document tab".
- [x] `render_doc_viewer`: renders the parsed markdown with the matched span highlighted.
- [x] `render_financials`: per-company metric table via the vendored cascade
      (`app/components/financials.py`); cached + graceful fallback.
- [x] `streamlit_app.py`: `@st.cache_resource` retriever (model + vectors load once, stay warm),
      `HF_HUB_OFFLINE` for offline model load, `session_state` doc selection.
- [x] Tests: `highlight_html` spans/escaping; vector-matrix cache parity. Manual:
      `.venv/bin/python -m streamlit run app/streamlit_app.py`.

## Phase 5 — Q&A (optional) ✅

- [x] `src/qa/prompts.py::build_user_prompt` + finalized `SYSTEM_PROMPT` (exact `NOT_FOUND`
      sentinel so the "not found" path is machine-detectable).
- [x] `src/qa/rag.py`: `answer` (pure synthesis: number snippets → `_call_llm` → `CitedAnswer`
      with markers mapped to real Hits, out-of-range dropped) + `ask` (retrieve → answer,
      gated by `qa.enabled` + `ANTHROPIC_API_KEY` exactly like `sentiment.refine_with_llm`).
- [x] `render_qa` (answer + per-citation "open in Document tab"). Tests: `tests/test_qa_rag.py`
      — citations map to real Hits; "not found" path; disabled-by-default; key-less no-op.

## Phase 6 — Eval & quality

- [x] Retrieval eval: `eval/queries.yaml` (labeled query→company[/period] set) +
      `src/search/evaluate.py` (recall@k / MRR) + `scripts/eval_retrieval.py`
      (`--keyword-only` to isolate BM25 vs hybrid). Real-index runs happen in the user shell.
- [x] Reuse `src.eval.compare_extractions` for the financials panel: `app/components/financials.py::
      accuracy_for` runs `run_comparison` over `data/corpus/<slug>` and surfaces a per-metric
      PASS/FAIL/MISS indicator + overall pass-rate in the 💵 Financials tab. DORMANT until a
      `data/ground_truth/<slug>-actual.csv` is dropped in (alpha-go doesn't vendor the parent's GT);
      degrades to an honest "accuracy not available" note otherwise.
- [x] Determinism gate: identical inputs → identical index + ranking
      (`tests/test_eval_retrieval.py::test_determinism_gate`).

## Post-roadmap parity features (AlphaSense replica push, 2026-07)

- [x] In-doc hit navigation: all query matches highlighted, active-match styling, match n/N
      Prev/Next, windowed rendering for long docs (`app/components/doc_matches.py`,
      `highlight.spans_to_html(active=)`, reworked `render_doc_viewer`).
- [x] Mention-trend analytics: `src/search/trends.py::mention_trend` (FTS + synonym expansion,
      mean lexicon sentiment per company-period) + the 📈 Trends tab.
- [x] Saved searches + watchlist — shipped 2026-07, removed 2026-07 (sidebar Workspace
      section retired; `src/state/` deleted).
- [x] Linear occurrence view: results grouped per document (chronological, company · period),
      every keyword occurrence shown in document order (`app/components/linear_results.py`).
- [x] Recorded retrieval baseline (2026-07-04, 3-company hashing index, 20 queries):
      keyword-only recall@5 95% / MRR 0.95; hybrid-on-hashing 95% / MRR 0.875 (hashing
      semantic half adds rank noise). Known miss: "Bara discount format" (BM25 tf of
      common terms in walmex table chunks beats the rare "Bara"). `eval_retrieval.py` /
      `search_demo.py` now default to the config's `index.db_path` — earlier runs against
      the hardcoded stale `alpha_go.db` scored a pre-pivot index (60% recall, all-FEMSA
      misses) and were meaningless.
- [x] MiniLM semantic index REBUILT (2026-07-05): 119 docs / 4,467 chunks / 384-dim
      `all-MiniLM-L6-v2`; config flipped to `alpha_go.db` + `embedding_backend: auto`.
      Eval on 27 queries (20 original + 7 new paraphrase "semantic-lift" queries):
      keyword-only recall@5 92.6% / MRR 0.911 (1 miss: Bara); **hybrid recall@5 100% /
      MRR 0.957, zero misses** — the semantic half now rescues rare-term and paraphrase
      queries. (Build must run with a venv outside iCloud-synced paths; see HANDOFF.)
- Earlier additions (2026-06-26): English-only corpus pivot, industry facet + scoping recall
  fix, per-hit lexicon sentiment, SEC-EDGAR 6-K adapter (see HANDOFF.md).
- [x] Multilingual embedding decision VALIDATED (2026-07-09): head-to-head on the 35-query eval,
      same 119-doc/4,819-chunk corpus. `paraphrase-multilingual-MiniLM-L12-v2` (shipped) **recall@5
      100% / MRR 0.950** vs `all-MiniLM-L6-v2` **recall@5 97.1% / MRR 0.942** (both recall@20 100%).
      The multilingual model already in `configs/alpha_go.yaml` is the right default — KEEP; no swap.

### Recorded eval baselines (honest, held-out — never quote the tuned dev sets)

- **Sentiment** (`scripts/eval_sentiment.py`, held-out `eval/sentiment_sample2_labeled.yaml`,
  200 snippets es=106/en=94): offline **lexicon acc 75.5% / macro-F1 0.693** (95% CI
  [69.1%, 80.9%]) — the shipped default. Opt-in DeepSeek `deepseek-chat` LLM tier **acc 0.910 /
  macro-F1 0.915** (2026-07-08 run; non-overlapping CI). The self-authored
  `eval/sentiment_labels.yaml` scores ~97% and is an inflated ceiling — must NOT be quoted.
- **Analog precision** (`scripts/eval_analogs.py --score`, 50 judged queries): held-out
  **precision@5 39.4% raw → 52.3% after the live transform** (gate → corpus-boilerplate →
  MiniLM semantic rerank); overall @5 34.3% → 43.2%. **Wrong-sense false-fires 0/15** under the
  polysemy gate (pre-gate raw pool: 100% on the one judged trap) — the gate is what removes them.

## Phase 7 — Smart Summaries (AlphaSense "Smart Summaries")

- [x] Extractive, offline, deterministic key-takeaways: `src/qa/summarize.py` — offset-preserving
      sentence split, corpus-boilerplate drop (reuses `fusion.CorpusBoilerplate`), salience
      (semantic centrality + figure/financial signal + tonal magnitude + lead prior) + greedy MMR.
      Per-document and thematic (query-scoped over retrieved hits) share one core.
- [x] Opt-in LLM tier (same citation contract as `rag.answer`): numbered passages → bulleted
      takeaways with `[n]` markers mapped back to real points; disk-cached; degrades to extractive.
      Gated on the `summary` config block + a resolvable provider key (off by default).
- [x] Dashboard: 📝 Summary tab (`panels.render_summary`) — company/period picker, sentiment
      badges, "📖 Read in context", "Summarize results" thematic mode, "✨ Sharpen with LLM".
- [x] Tests (`tests/test_summarize.py`, 15) + eval harness (`scripts/eval_summary.py`,
      `eval/summary_gold.yaml`): coverage@N + boilerplate-rate, tuning|heldout split, two-phase
      author-then-score (never tune on held-out). Seed gold (ACME fixture) scores 100% coverage /
      0% boilerplate — a hermetic smoke; add real company targets for discriminating numbers.

## Cross-cutting invariants (all phases)

- Vendored packages are never edited in place — re-sync via `scripts/vendor_sync.py`.
- Ingest/index/search need no API key and no network at query time.
- LLM only in `qa`, off by default.
- New behavior ships with a test; measure retrieval changes before claiming a lift.

## Release hardening — two-week flagship delivery

- [x] Certified six-issuer source configuration (Walmex, Bimbo, FEMSA, Grupo México, Fibra Uno,
      Banorte) with a 2021-current floor and explicit minimum coverage checks.
- [x] Portable manifest provenance (`source_path`, format, content checksum) and idempotent local
      mirror materialization for PDF and MD&A sources.
- [x] Strict offline runtime validation: the configured local embedding model must be available
      and match the index dimension; `scripts/preflight.py` blocks stale or incomplete artifacts.
- [x] Separate Mentions, curated Analogs, and semantic Concepts lanes; indexed-text reader fallback;
      lazy Explore views; deterministic offline evidence brief HTML/CSV export.
