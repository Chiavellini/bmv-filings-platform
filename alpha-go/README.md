# alpha-go — a local AlphaSense

Offline financial-document **search & intelligence** over a corpus of company reports:
cross-document keyword + semantic search, highlighted snippets, mention trends, extractive
**Smart Summaries**, a per-company financials panel, and optional cited Q&A — driven from a
local Streamlit dashboard.

alpha-go is a subproject with its own search/UI code and a shared root-level data contract.
`../estate.json` is the single bridge to the portable document estate, Alpha Go index,
user-upload area, and offline embedding model. The root, `soft/`, `earnings/`, and Alpha Go
therefore consume one catalog instead of maintaining private document copies.

The machine-readable phase graph is [docs/architecture/pipeline_phases.yaml](docs/architecture/pipeline_phases.yaml);
[docs/PHASES.md](docs/PHASES.md) is the human map. New contributors should read
[docs/VISION.md](docs/VISION.md) then [docs/ROADMAP.md](docs/ROADMAP.md).

> **Runtime contract:** literal mention counts are exhaustive, accent/case-insensitive
> Command-F scans; vetted Spanish/English equivalents are transparent; semantic discovery is a
> separate multilingual lane and never inflates mention counts. The default runtime is offline
> and needs no API key.

## Layout

```
alpha-go/
  src/
    download/ parse/ extract/ shared/ model/ excel/ eval/   # VENDORED from parent (verbatim)
    corpus/   ingest.py, manifest.py            # NEW  download+parse -> document corpus
    index/    chunker, store(SQLite), keyword_index(FTS5), embeddings, build   # NEW
    search/   retriever(hybrid), fusion(RRF), snippets, filters                # NEW
    qa/       rag, prompts, sentiment, summarize  # NEW  cited Q&A + sentiment + Smart Summaries
  app/        streamlit_app.py + components/      # NEW  dashboard
  scripts/    ingest.py, build_index.py, vendor_sync.py
  configs/    alpha_go.yaml + VENDORED <company>.yaml, metric_search.yaml, xbrl_concepts.yaml
  data/corpus/  search projection manifest (source artifacts live in the shared estate)
  tests/      test_smoke.py
  docs/       VISION, PHASES, REUSE_MAP, ROADMAP, CONFIG, DECISIONS, architecture/, handoffs/
```

The runtime index, estate catalog, uploads, and shared reports view resolve
through `src/shared/paths.py` and the root `estate.json`. Move an already-built
portable bundle to another computer by changing only `estate_root` in that
file (or setting `PDFS_DOCUMENT_ESTATE`).

For automatic updates, the root outbox worker projects every newly parsed
estate document into Alpha Go; the legacy per-issuer `alpha_go` pin is not a
search-eligibility filter. Corrected filings replace their current family row,
and an already-running dashboard detects external SQLite commits and refreshes
its search/facet caches on the next interaction. The scheduler must run the
bounded drain plus periodic whole-estate reconciliation described in
[`../docs/ALPHA_GO_ESTATE_SYNC.md`](../docs/ALPHA_GO_ESTATE_SYNC.md); merely
starting the dashboard does not consume the outbox.

The `sources` section in `configs/alpha_go.yaml` is a separate rebuild recipe.
Some source definitions retain legacy root/Soft mirror locations for optional
historical re-ingestion. They are not required to search the certified Estate.
On another computer, point Alpha directly at the portable projection, model,
and (once completed) index stored on the external Estate; do not create these
runtime assets inside the Git checkout.

## Run

From the `alpha-go/` directory:

A destination computer can skip ingestion because the certified projection
and model travel with the Estate. If the semantic index is absent or its
preflight fails, build it on that computer before launching Alpha. Extraction,
onboarding, and workbook workflows do not depend on the Alpha index.

```bash
python3.12 -m venv .venv312
.venv312/bin/pip install -r requirements.txt

# 1) Ingest: download + parse configured sources into the corpus            (Phase 1)
.venv312/bin/python scripts/ingest.py --config configs/alpha_go.yaml

# 2) Build the hybrid search index only when replacing the Estate index.     (Phase 2)
#    CPU-only hosts may opt into workers; accelerator-backed hosts should omit
#    the flag. The portable default is one process.
.venv312/bin/python scripts/build_index.py \
  --config configs/alpha_go.yaml \
  --corpus-dir "$PDFS_ALPHA_CORPUS" \
  --db "$PDFS_ALPHA_INDEX"

# 3) Verify the release artifact (fails on missing issuers, stale indexes, or model mismatch)
.venv312/bin/python scripts/preflight.py --config configs/alpha_go.yaml

# 4) Launch the dashboard
.venv312/bin/python -m streamlit run app/streamlit_app.py

# 5) Score retrieval quality against the labeled query set (Phase 6)
.venv312/bin/python scripts/eval_retrieval.py                  # hybrid
.venv312/bin/python scripts/eval_retrieval.py --keyword-only   # BM25 baseline
```

Dashboard views: Search (Mentions, curated related wording, and multilingual Concepts with
inline highlighted evidence), Trends, and Upload. Search selections can be exported as a
self-contained offline HTML evidence brief or CSV. Run `scripts/preflight.py` before delivery.

News is a first-class **Document type** in the main Search page, beside quarterly releases,
annual reports, and press releases. Its default discovery pilot uses GDELT's exact source-side
company-phrase query and retains only a title, publisher, date and canonical link—never an
unlicensed publisher body. Preview one issuer before writing anything:

```bash
python3 scripts/sync_news_gdelt.py --companies bimbo --timespan 7d --max-records 25
python3 scripts/sync_news_gdelt.py --companies bimbo --timespan 7d --max-records 25 --apply
```

Omit `--companies` to cover the configured BMV catalog. The script reports provider rate/network
issues without altering existing news records; a scheduler can rerun it later. Approved RSS feeds
remain available through `scripts/sync_news_rss.py`. Use `--db` only when the running dashboard
uses a deliberate alternate index.

Opt-in SEC EDGAR fetches also require `SEC_EDGAR_USER_AGENT` to contain a
descriptive application name and a monitored contact address. Keep the real
contact in the untracked local environment, not in Git.

Re-sync the vendored infra from the parent (records provenance, never edits the parent):

```bash
.venv312/bin/python scripts/vendor_sync.py --check     # dry-run
.venv312/bin/python scripts/vendor_sync.py --sync
```

## Tests

```bash
.venv312/bin/python -m pytest tests/ -q -m "not network and not model"
```

`pyproject.toml` puts the project root on `pythonpath`, so `import src.<role>.<module>`
resolves to alpha-go's own vendored copy.
