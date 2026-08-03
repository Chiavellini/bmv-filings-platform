# Quarterly acquisition engine

`src/acquisition/` is the root-owned quarterly acquisition module. This document
describes the code that exists now and the migration gates around it.

## Boundary

The package is scheduler-independent. It has no permanent loop, daemon, cron
installer, or managed-job definition. An operator or external scheduler invokes
the thin wrapper at `scripts/refresh_quarterly_estate.py`.

The root now also has a separate, bounded outbox worker at
`scripts/process_estate_outbox.py`. It can create a verified Markdown derivative
for stored PDF, HTML/news, or text; create canonical numeric facts from raw BMV
XBRL JSON/JSON.GZ; and optionally invoke Alpha Go's owned projection CLI. The
worker is implemented and fixture-tested, but it has not been deployed as a
continuous service. Soft and Earnings consume the resulting root-owned facts;
they do not own acquisition or fact derivation.

```text
external scheduler
       |
       v
acquisition engine
  local plan
       |
       +-- applied run: discover -> fetch -> validate -> EstateWriter
                                                        |
                                  object + estate-relative compatibility path
                                                        |
                                         estate.document.stored outbox row
                                                        |
                                     per-consumer delivery receipts
                                                        |
                       +--------------------------------+----------------------+
                       |                                                       |
        verified PDF/HTML/text -> Markdown                    raw XBRL -> facts
                       |                                                       |
        estate.document.parsed outbox row              estate.document.facts_extracted
                       |
        optional Alpha Go projection
```

An estate commit and consumer delivery are separate transactions. A stored
document does not become parsed or searchable unless a worker is explicitly run
and its delivery receipt succeeds. No live worker or schedule is currently
installed.

## Root-owned configuration

`configs/issuers.yaml` is the authoritative acquisition universe. Each issuer has a
stable slug, ticker, display name, sector, project memberships, and source bindings.
Detailed IR crawler options may be overlaid from the root `configs/<slug>.yaml`
configuration while that legacy layout is being migrated.

Project membership selects consumers; it does not create another document:

- `alpha_go`: issuer documents may be included in Alpha Go estate releases.
- `soft`: issuer data may feed Soft coverage and valuation.
- `earnings`: issuer documents may enter event-study releases.

Acquisition settings belong at the root. Search weights, valuation templates, and
research-study parameters remain app-specific.

As of 2026-07-30, the registry contains 179 active issuers. Twenty-four have an
enabled `investor_relations` source and one has an enabled
`bmv_issuer_pdf` source. The remaining 154 issuers are not source-configured for
primary PDFs even when BMV XBRL is configured. GNP, GFNorte, FEMSA, Sports
World, and Tiendas 3B currently carry explicit root live-canary evidence. Source
configuration, live
verification, source-window coverage, and existing issuer catalog coverage are
separate states; see
[the primary-PDF acquisition runbook](acquisition/PRIMARY_PDF_ACQUISITION.md).

Issuer lifecycle data is also unverified: no issuer has `listed_from` or
`listed_to`, all issuers use the December fiscal-year-end default, all use the
45-day filing-grace default, and all are marked active. Those defaults make the
local missing-period plan conservative but not authoritative.

## Source policy

Implemented root sources are:

- **BMV XBRL** (`bmv_xbrl`): loads one archive snapshot per service instance,
  filters exact quarterly filings by ticker and desired period, and stores raw
  JSON or gzipped JSON.
- **Issuer IR** (`investor_relations`): calls the existing multi-layer IR
  downloader for quarterly PDFs, skipping known periods except for the trailing
  recheck window.
- **Official BMV issuer page** (`bmv_issuer_pdf`): discovers the exact current
  quarterly PDF from the issuer's BMV financial-information page. It prefers
  management discussion and falls back to basic financial statements. A
  `coverage_from_period` activation boundary prevents a current-only page from
  claiming unsupported historical coverage.
- **Wayback**: runs only when the explicit `backfill` command is applied and only
  for periods still missing from a configured IR source.

`SUPPORTED_SOURCE_KINDS` contains `bmv_xbrl`, `investor_relations`, and
`bmv_issuer_pdf`. There are no root SEC EDGAR or upload adapters. Code under
application or vendored directories does not make those root acquisition
adapters.

## Durable lifecycle

Applied runs persist acquisition runs, leases, source records, attempts, and
immutable source-record versions. The implemented source-record states are:

```text
discovered -> stored
     |          |
     |          +-> same filing family + same hash: no new document/version
     |          +-> changed hash: immutable new version with supersedes link
     |
     +-> retryable
     +-> rejected
```

There are no `fetching` or `extraction_queued` source-record states. A newly
stored document inserts an `estate.document.stored` outbox event. The root
dispatcher creates an independent durable receipt for each enabled consumer,
uses heartbeat-renewed fenced leases, bounded exponential and crash retries,
fair scheduling, serialization per consumer, and append-only attempt history.
It marks the event published only when all enabled receipts are terminal. A
dead receipt remains visible as a failure even though it is terminal, so
`published_at` alone is not a success signal.

Both root derivatives verify the source SHA-256 and record immutable processor
lineage. The document derivative converts PDF/HTML/text to content-addressed
Markdown and emits `estate.document.parsed`. The XBRL derivative runs the root
`bmv_xbrl.extract_artifacts` helper, stores a root-owned `xbrl_facts` JSON
artifact under `views/reports/<issuer>/xbrl/`, and emits
`estate.document.facts_extracted`. Same-hash project/facet changes emit a
routing event, so downstream adoption can be delivered. An always-enabled
terminal verifier checks each derived event's portable output, content
object/hash, artifact ownership, and immutable lineage. The optional Alpha
consumer projects eligible parsed receipts in batches of up to 64 through
`alpha-go/scripts/sync_shared_estate.py`; target generations have distinct
receipt identities and Alpha holds a file lock over manifest/index
replacement. Alpha projection is not enabled by default.

New content objects receive portable keys such as `blobs/ab/<sha256>`, and new
acquisition and derivative rows store estate-relative `artifacts.path` values.
The current SQLite/local-filesystem implementation still records an absolute
`content_objects.blob_path` as a local locator; consumers use `object_key` as
the portable content identity and resolve artifact paths against the configured
estate root. Legacy catalog rows may still contain host-absolute artifact paths.
Worker preflight flags those rows, and they must be migrated before that legacy
catalog is connected read-write on another host.

## Operator commands

`audit` and `plan` are currently the same local coverage calculation with a
different report label. Neither command discovers remote records, accesses the
network, opens the acquisition ledger for writing, nor mutates the estate.

```bash
# Local/read-only fleet report.
python3 scripts/refresh_quarterly_estate.py audit

# Local/read-only scoped plan.
python3 scripts/refresh_quarterly_estate.py plan --only gap,kof

# Machine-readable local/read-only report.
python3 scripts/refresh_quarterly_estate.py audit --json

# Complete local/read-only primary-PDF source and catalog matrix.
python3 scripts/audit_primary_pdf_sources.py --json

# Show the complete command contract.
python3 scripts/refresh_quarterly_estate.py --help
```

The applied commands below are implemented and mutate the SQLite estate and
local object store. They are suitable for controlled development trials, not a
production schedule while the deployment gates remain open:

```bash
# Small, reviewed live-source trial.
python3 scripts/refresh_quarterly_estate.py sync \
  --only kof --trigger manual_trial --apply

# Explicit historical recovery; Wayback is enabled only in this mode.
python3 scripts/refresh_quarterly_estate.py backfill \
  --only kof --from-year 2016 --trigger manual_backfill --apply
```

`--apply` is required for `sync` and `backfill`. An applied CLI run returns
nonzero when execution failures remain or when the default strict coverage gate
finds readiness/coverage gaps. Those coverage gaps are reported separately from
source-record attempts; they are not mislabeled as retryable download failures.
`--allow-coverage-gaps` is an explicit diagnostic opt-out from the coverage gate,
not a production operating mode. A whole-universe run is therefore expected to
fail the strict gate until primary-PDF source enrichment is complete.

## Safe external scheduling

Until deployment is approved, schedule only a read-only audit. Resolve and pin
the interpreter path first, create a log directory, and use an absolute working
directory:

```bash
command -v python3
mkdir -p /path/to/document-estate/logs
```

Example crontab entry after replacing `/absolute/path/to/python3` with the
resolved interpreter. This does not install a crontab; add it with the normal
operator review process:

```cron
SHELL=/bin/zsh
PATH=/usr/local/bin:/usr/bin:/bin
17 06 * * 1-5 cd /path/to/checkout && /path/to/checkout/.venv/bin/refresh-quarterly-estate audit --json >> /path/to/document-estate/logs/acquisition-audit.log 2>&1
```

For a managed job, use the repository root as the working directory and the
argument vector below. Set maximum concurrency to one, retain stdout/stderr,
alert on nonzero exit, apply a timeout shorter than the acquisition lease, and
do not configure automatic retries for an applied run until retry semantics have
been operationally tested.

```text
/absolute/path/to/python3
scripts/refresh_quarterly_estate.py
audit
--json
```

After every deployment gate is closed, the managed job may be changed through a
reviewed deployment from `audit --json` to:

```text
scripts/refresh_quarterly_estate.py sync --trigger managed_job --apply --json
```

The engine's lease prevents a second live refresh from taking ownership while a
lease is valid, but scheduler-side non-overlap is still required for predictable
operations.

## Safety invariants

- Same-family bytes with the same SHA-256 create no new document version.
- Changed bytes create a new immutable version linked with
  `supersedes_document_id`; an acquisition refresh does not delete the old one.
- PDF, JSON, ZIP, and gzip payloads receive format-specific validation before
  storage.
- Downloads are staged in a temporary directory outside application corpora.
- A global acquisition lease fences estate mutations and is renewed during the
  run.
- Planning distinguishes original PDF coverage from raw XBRL coverage.
- Coverage requires a resolvable original: an existing artifact path, an
  existing local blob, or a positive lookup from the configured object-store
  resolver. A catalog row by itself is not coverage.
- Filing families include language and rendition. Spanish and English releases,
  and a release versus a full report, are distinct immutable families rather
  than accidental revisions of one another.

These invariants are acquisition-writer guarantees. They do not imply that
legacy application writers are idempotent or that outbox delivery, extraction,
or indexing has occurred.

## Consumer commands

Delivery inspection is read-only and does not initialize or migrate consumer
tables:

```bash
python3 scripts/process_estate_outbox.py status \
  --database data/document_estate/catalog.db --json
```

The bounded worker is a write operation and refuses to run without `--apply`.
The default consumer creates document-to-Markdown derivatives; Alpha Go is an
explicit opt-in and requires a target index:

```bash
# Controlled root derivative batch.
python3 scripts/process_estate_outbox.py run --apply \
  --estate-root data/document_estate --max-deliveries 100 --json

# Controlled derivative + Alpha Go projection batch.
python3 scripts/process_estate_outbox.py run --apply \
  --estate-root data/document_estate --max-deliveries 100 \
  --enable-alpha-go \
  --alpha-index alpha-go/data/index/alpha_go_expanded_hashing.db \
  --alpha-target-id expanded-multilingual-v1 --json
```

After correcting the underlying fault, a dead receipt can be explicitly
replayed without deleting its prior attempt history:

```bash
python3 scripts/process_estate_outbox.py requeue --apply \
  --database data/document_estate/catalog.db \
  --consumer-id '<generation-qualified id>' --limit 100 --json
```

`run --apply` initializes the acquisition/consumer schema as needed, registers
the enabled subscriptions, backfills matching historical events into delivery
receipts, and may create derivatives or update the selected Alpha Go index.
These examples document the command contract; do not run them against the live
catalog until the production preflight findings and rollout plan have been
reviewed. See [DEPLOYMENT.md](DEPLOYMENT.md).

## Migration rule for consumers

During migration, `data/document_estate/views/reports/<slug>/` remains a read-only
compatibility view. New application code should resolve document and artifact IDs
through `EstateReader`; it should not glob or write that view.

Legacy private download entry points can be removed only after:

1. the root engine has source parity;
2. the consumer reads a pinned estate release;
3. repeated fixture runs prove idempotency;
4. the outbox worker is deployed and makes a committed document available to
   each required downstream consumer;
5. an end-to-end run makes a new quarter visible to all three consumers.

At present Alpha Go, Soft, and Earnings still have transitional writers and
download paths. Only the root document derivative and optional Alpha Go projection
consumers exist. The root module must not be described as the sole production
writer until those paths are cut over or retired.

## Deployment gate

Do not enable a production applied schedule until all of the following are true:

- primary PDF sources are configured and verified beyond the current 25/179;
- issuer listing windows, active/delisted state, fiscal year end, and filing
  grace are issuer-verified;
- Alpha Go, Soft, and Earnings read the estate and no longer act as competing
  durable document writers;
- the implemented outbox dispatcher and required consumers are migrated,
  deployed, monitored, and proven to update root extraction, Alpha Go, Soft,
  and Earnings;
- the portable `object_key` contract and absolute compatibility-path behavior
  are explicit across backends;
- the production coverage service is wired to an object-store existence check
  (HEAD or a verified manifest), so missing remote originals cannot count as
  covered;
- direct-template and Wayback fetches persist the exact successful source URL
  and retrieval evidence instead of the current `provenance_warning` fallback;
- SQLite/local-filesystem and the intended production metadata/object-storage
  backend pass the same ingestion, versioning, coverage, and failure tests;
- a clean-clone integration test acquires, stores, consumes, extracts, and
  searches a fixture quarter twice, with the second pass producing no new
  version or downstream duplicate.
