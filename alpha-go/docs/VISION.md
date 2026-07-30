# Vision — a local AlphaSense

## What it is

[AlphaSense](https://www.alpha-sense.com/) is a market-intelligence platform: you search a
huge corpus of filings, transcripts, and broker research and instantly get the exact
passages that answer your question, with company/period/document facets, smart synonyms,
and AI summaries on top.

**alpha-go is a local, offline, single-user version of that** — scoped to the corpus you
ingest yourself (today: Mexican issuer quarterly reports, but the design is
company-agnostic). It turns the parent repo's pile of parsed reports into something you can
*ask questions of* rather than just extract fixed numbers from.

The parent pipeline answers **"what is WALMEX's 2026-1T EBITDA?"** (a known metric).
alpha-go answers **"where, across every company and quarter, do management discuss pricing
pressure from e-commerce?"** (open-ended language search) — and *then* shows you the
numbers next to the passage.

## Target capabilities

| Capability | AlphaSense analogue | alpha-go approach |
|------------|--------------------|-------------------|
| Cross-document search | Universal search | Hybrid BM25 (FTS5) + local embeddings, fused (RRF) |
| Highlighted snippets | Search snippets | Char-offset spans from chunk → doc viewer |
| Facets / filters | Filters | Company, period range, doc type, language |
| Smart synonyms | Smart Synonyms™ | Reuse the deterministic `metric_search.yaml` alias dictionary |
| Financials at a glance | Company dashboards | Reuse the vendored 4-tier extraction cascade |
| AI summaries / Q&A | Generative Search | Optional RAG with citations (off by default) |
| Local dashboard | Web app | Streamlit |

## Non-goals (explicit scope boundary)

- **Not a hosted SaaS / multi-tenant service.** Local, single-user, runs on one machine.
- **No real-time market data, prices, or alerts.** The corpus is what you ingest.
- **Not a re-implementation of extraction.** The 4-tier metric cascade is *reused as-is*
  from the parent via vendoring — alpha-go adds the *search/retrieval/synthesis* layer the
  parent never had.
- **Search stays deterministic and offline.** The only LLM call is the opt-in Q&A phase;
  everything else (ingest, index, search) needs no API key.
- **Does not modify the parent repo.** Reuse is by copy (`scripts/vendor_sync.py`).

## Why "reuse by vendoring"

The user's constraint is a *self-contained* sub-project that doesn't touch the existing
infra. The parent's extraction/parse/download layers are exactly the hard part of turning a
PDF into clean searchable text — re-writing them would be wasteful and risky. So alpha-go
copies them under its own `src.` namespace and builds the new layers on top. See
[REUSE_MAP.md](REUSE_MAP.md) and [DECISIONS.md](DECISIONS.md).
