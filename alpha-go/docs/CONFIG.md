# Configuration

alpha-go is driven by one top-level file, `configs/alpha_go.yaml`, plus the vendored
per-company configs it reuses.

## `configs/alpha_go.yaml`

```yaml
sources:                      # what to ingest
  - slug: walmex              # corpus id + links to the vendored configs/<slug>.yaml
    company: "Walmex"
    ir_website:               # SAME shape as the parent's ir_website block
      url: https://www.walmex.mx/en/financial-information/quarterly.html
      pdf_link_pattern: '(?:report|trimest|quarter|result|earning).*\.pdf'
      xbrl_ticker: WALMEX     # optional; enables the XBRL download path
    doc_types: [release, report]
    language: en

index:
  db_path: data/index/alpha_go.db
  strict_runtime: true
  embedding_backend: auto     # auto | hashing | sentence-transformers
  embedding_model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
  chunk:
    target_chars: 1200        # ~ chunk size
    overlap_chars: 150        # overlap between adjacent chunks
  expand_synonyms: true       # reuse configs/metric_search.yaml aliases in keyword search

search:
  default_limit: 20
  keyword_weight: 0.3         # BM25 vs semantic bias at fusion time
  semantic_weight: 0.7

qa:                           # optional; the ONLY LLM-using feature
  enabled: false              # off by default
  model: claude-haiku-4-5-20251001
  top_k: 8                    # snippets fed to the model
```

### Field reference

| Path | Meaning |
|------|---------|
| `sources[].slug` | corpus identifier; also the name of the vendored `configs/<slug>.yaml` used by the financials panel |
| `sources[].ir_website` | download config — identical shape to the parent's `ir_website` (URL, link pattern, optional `xbrl_ticker`, `use_playwright`) |
| `sources[].doc_types` | document types this source yields (faceting + `Document.doc_type`) |
| `index.embedding_backend` | `auto` (default; sentence-transformers if installed, else offline `HashingEmbedder`), `hashing`, or `sentence-transformers` |
| `index.embedding_model` | sentence-transformers model id (used by the ST backend); recorded in the index `meta` table |
| `index.chunk.*` | chunker sizing (`chunk_document` defaults) |
| `index.expand_synonyms` | toggle deterministic synonym expansion (vendored `metric_search.yaml`) |
| `search.{keyword,semantic}_weight` | RRF weighting between the two scorers |
| `qa.enabled` | master switch for the LLM Q&A phase (default `false`) |

## Reused per-company configs

Each `sources[].slug` maps to a vendored `configs/<slug>.yaml` (e.g. `walmex.yaml`) that the
**financials panel** consumes through `src.model.financial_model.load_config/apply_config` —
its `metric_overrides`, `custom_metrics`, `era_gates`, `unavailable`, and `segments_file`
fields work exactly as in the parent. See [REUSE_MAP.md](REUSE_MAP.md). alpha-go adds no new
per-company schema; it only adds the top-level `alpha_go.yaml`.

## Environment

| Var | Needed for |
|-----|-----------|
| `ANTHROPIC_API_KEY` | only the optional Q&A phase (`qa.enabled: true`) |
| `SEC_EDGAR_USER_AGENT` | only opt-in SEC EDGAR network fetches; include a monitored contact |

Indexing and search need no network at query time. Ingest is offline when it
uses attached local mirrors; configured network sources have their own
requirements.
