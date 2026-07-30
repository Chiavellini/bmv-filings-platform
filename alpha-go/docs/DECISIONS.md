# Decision Log

Append-only record of design decisions, newest last. Format mirrors the parent's
decision-record discipline: decision · rationale · consequences.

## D1 — Reuse the parent infra by *vendoring* (copy), not by import

- **Decision:** Copy the reusable parent packages into `alpha-go/src/` under the same `src.`
  namespace; alpha-go never imports from the parent. Re-sync via `scripts/vendor_sync.py`.
- **Why:** The user requires a self-contained sub-project that does not modify or depend on
  the existing infra. The parent's download/parse/extract layers are the hard, proven part
  of turning PDFs into clean text — rewriting them would be wasteful and risky.
- **Consequences:** Zero import rewrites (parent already uses `import src.*` +
  `pythonpath=["."]`, which alpha-go replicates). `paths.py` re-roots automatically. Vendored
  code is never edited in place, so a re-sync is always safe; new behavior lives in the new
  packages. Drift from the parent is a known, accepted cost, managed by `vendor_sync.py`.

## D2 — Search backend: local hybrid (FTS5 + local embeddings), fused by RRF

- **Decision:** Keyword via SQLite **FTS5** (BM25, stdlib — no dependency); semantic via a
  **local** sentence-transformers model stored in the same SQLite db; combine with
  Reciprocal Rank Fusion.
- **Why:** A local AlphaSense must be fully offline and deterministic, with no API key for
  search. FTS5 is dependency-free and strong for exact/keyword recall; local embeddings give
  semantic recall without a provider. RRF is robust to the two scorers' different scales.
- **Consequences:** One self-contained SQLite artifact; reproducible rankings; the embedding
  model is the only heavy dependency (lazy-loaded so imports/tests stay light). Synonyms
  reuse the vendored `metric_search.yaml` dictionary rather than an embedding-only approach.

## D3 — Dashboard: Streamlit (local-first)

- **Decision:** Single-process **Streamlit** app as the reference UI.
- **Why:** Fastest path to a working local, single-user dashboard; pure Python, no frontend
  build; matches the parent's prior `src/ui` approach. The UI is a thin adapter over the
  service interfaces, so a FastAPI + web frontend remains a later swap if multi-user is ever
  needed.
- **Consequences:** Limited UI control and single-user, accepted for a local tool. Business
  logic stays in `src/`, not the app, to keep the UI replaceable.

## D4 — Q&A is the only LLM use, and it is off by default

- **Decision:** All ingest/index/search is deterministic and offline; an LLM is used **only**
  in the optional `qa` phase, gated by `qa.enabled` + `ANTHROPIC_API_KEY`.
- **Why:** Preserves determinism, privacy, and zero-cost local operation as the default;
  mirrors the parent's "deterministic first, LLM last, opt-in" principle.
- **Consequences:** The product is fully usable with no API key; generative answers are an
  additive layer that must cite retrieved snippets (anti-hallucination).
