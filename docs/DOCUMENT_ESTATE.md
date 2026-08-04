# Shared document estate

The configured estate's `catalog.db` is the monorepo-wide source of truth. The
fresh-clone default is `data/document_estate`, while a portable deployment sets
`PDFS_DOCUMENT_ESTATE` to the transferred bundle. The catalog covers documents
and their physical artifacts across the root extraction project, Alpha Go,
Soft, and Earnings.

The current implementation is SQLite plus the local filesystem. The new root
acquisition writer adds immutable content objects, portable object keys,
source-record versions, acquisition runs/attempts, project pins, and an outbox.
Those additions coexist with the older path catalog; they do not yet replace
all legacy writers.

## Single connection bridge

The repository-root `estate.json` is the only user-facing location
configuration. All four execution contexts—root, Alpha Go, Soft, and
Earnings—load it through `estate_bridge.py`.

Every path except `estate_root` is bundle-relative:

```text
catalog.db
blobs/
indexes/alpha_go.db
views/reports/
manifest.json
user/
```

Relative roots resolve from the bridge file. On a transferred external disk,
leave the tracked bridge unchanged and set `PDFS_DOCUMENT_ESTATE` in an
untracked `.env` or service environment. The bridge owns consistent
read-only/read-write SQLite connection setup and rejects object keys that
escape the estate root.

`PDFS_ESTATE_BRIDGE` and `PDFS_DOCUMENT_ESTATE` remain supported for CI and
worker automation. Native writers additionally require
`PDFS_ESTATE_MOUNT_ROOT` and `PDFS_ESTATE_ID`. The latter pair validates a real
mount point plus `.bmv-estate-volume.json`; a missing USB or mismatched ID fails
before SQLite is opened.

After changing `estate_root`, run the read-only connection check:

```bash
python3 scripts/check_estate_connection.py --json
python3 scripts/check_portable_estate.py \
  --estate-root "$PDFS_DOCUMENT_ESTATE" \
  --mount-root "$PDFS_ESTATE_MOUNT_ROOT" \
  --estate-id "$PDFS_ESTATE_ID"
```

Add `--require-alpha-index` only for an Alpha-enabled host. The base Estate
gate is sufficient for extraction and workbook consumers.

## Storage contract

- `documents`: logical records (`company`, `period`, `doc_type`, language, source provenance).
- `artifacts`: physical PDF, Markdown, HTML, JSON, and archive compatibility paths with SHA-256,
  size, role, and owning project.
- `memberships`: company/industry associations, including multi-company News.
- `project_records`: each project's legacy ID mapped to the shared logical record.
- `artifact_duplicates`: content-hash view exposing byte-identical copies that can be consolidated
  in a later, separately verified migration.
- `views/reports/<company>/`: zero-copy symlink view compatible with `report_index`, the root
  extraction pipeline, and segment-sheet generation. News remains catalogued and searchable but
  is intentionally excluded from this extraction view.

An exported estate stores artifact and object locations as bundle-relative
paths. Every content object has an `object_key` of the form
`blobs/<sha-prefix>/<sha256>`; compatibility artifacts are hard links to those
objects when their content matches. Readers resolve both paths against the
runtime estate root, so moving the complete bundle requires no catalog rewrite.

Set `PDFS_DOCUMENT_ESTATE` to relocate the local estate and
`PDFS_REPORTS_DIR` to explicitly point a project at a reports-compatible view.

## Portable export and certification

The exporter never overwrites a destination and does nothing without the
explicit `--apply` acknowledgement. It takes a consistent SQLite backup,
rewrites paths relative to the new root, hard-links compatible artifacts,
generates the sentinel and manifest, then runs full SHA-256 verification before
atomically publishing the destination:

```bash
python3 scripts/export_portable_estate.py \
  --source <existing-estate> --destination <new-estate> \
  --estate-id <uuid> --apply

python3 scripts/check_portable_estate.py \
  --estate-root <new-estate> --estate-id <uuid> \
  --full-hashes --require-alpha-index
```

The checker is read-only. It verifies the sentinel/ID, manifest and catalog
checksum, SQLite quick/foreign-key checks, catalog/object equality, relative
paths, missing and wrong-size blobs, optional full blob hashes, hard-link
counts, and Alpha index integrity/counts.

## Refresh

```bash
python3 scripts/build_document_estate.py
```

Refreshes are idempotent. File hashes are cached by absolute path, size, and nanosecond mtime.
Existing files are never deleted or overwritten; compatibility links are created only when their
destination is absent.

## Segment sheets

`scripts/build_segments.py` now prefers the shared report view when a company has parsed Markdown
there. It falls back to `data/reports/<company>` only when the shared estate has no usable parsed
reports, or when `--force-download` is explicitly supplied.

**Read-only contract (2026-07-28):** the extraction pipeline treats the view as strictly
read-only — it never `mkdir`s or writes inside `views/reports/`. Fresh downloads and parse
products always land in `data/reports/<company>`; the estate builder picks them up on its next
refresh. (Estate-builder side: expect new files to appear under `data/reports/`, never inside
the view.)

## Alpha Go search

Alpha Go can import current parsed estate documents, including quarterly and
annual reports, press releases, and news. The sync is content-hash deduplicated,
resolves portable artifact paths against the estate root, and never copies or
downloads a report:

```bash
# Audit only
python3 alpha-go/scripts/sync_shared_estate.py

# Update Alpha's manifest and the currently selected index
python3 alpha-go/scripts/sync_shared_estate.py \
  --estate "$PDFS_DOCUMENT_ESTATE/catalog.db" \
  --corpus "$PDFS_DOCUMENT_ESTATE/projections/alpha-go" \
  --apply --db "$PDFS_DOCUMENT_ESTATE/indexes/alpha_go.db"
```

Repeated runs are idempotent. Missing source paths are excluded, and stale estate-managed manifest
records are removed during an applied sync.

This script remains the Alpha-owned projection boundary. The root
`AlphaGoProjectionConsumer` invokes it for a bounded group of parsed receipts.
Target generations have independent receipts, and the Alpha-owned file lock
serializes manifest/index replacement. The `v2` consumer is estate-wide: a
legacy `alpha_go` project pin is not required once a verified parsed-text
artifact exists. A periodic broad reconciliation covers legacy writers and
stale/missing artifacts that have no event. See
[ALPHA_GO_ESTATE_SYNC.md](ALPHA_GO_ESTATE_SYNC.md) for the exact scheduler
contract. No live worker installation is implied by the code.

## Acquisition events

Each newly created acquisition document inserts one
`estate.document.stored` row in `outbox`. The payload includes the document and
family IDs, version, superseded document, issuer, document type, period, hash,
portable object key, project pins, language, media type, artifact role, source
key, and source-record ID.

`src/consumers/outbox.py` implements independent durable receipts in
`outbox_deliveries`, with consumer subscriptions, append-only attempt history,
heartbeat-renewed fenced claims, bounded crash retries, fair consumer
scheduling, and explicit dead-letter replay. The first root consumer verifies
the original hash, parses PDF or derives Markdown from HTML/text, records
processor-version lineage, and emits `estate.document.parsed`. Same-hash
project or membership changes emit a routing revision, so an already-derived
document can receive updated memberships later. The optional Alpha consumer
projects every parsed estate document into the selected target; project pins
remain application metadata and do not limit whole-estate search.

This is an implemented delivery path, not proof of a live deployment. The
current catalog must be migrated and the drain/reconcile/status tasks must be
enabled before indexing can be described as an automatic consequence of an
estate commit. Soft and Earnings have no equivalent consumers, and raw XBRL
text derivation is not implemented.

```bash
# Strictly read-only; does not create or migrate tables.
python3 scripts/process_estate_outbox.py status \
  --database data/document_estate/catalog.db --json
```

See [DEPLOYMENT.md](DEPLOYMENT.md) before any `run --apply`.

## Physical consolidation

Exact-hash duplicates can be consolidated into the content-addressed blob store at
`data/document_estate/blobs/<sha-prefix>/<sha256>`. The original project paths remain in place as
hard links, so existing readers and extraction paths do not change.

```bash
# Read-only inode and reclaimable-space audit
python3 scripts/consolidate_document_estate.py

# Pilot, then full consolidation
python3 scripts/consolidate_document_estate.py --apply --max-replacements 10
python3 scripts/consolidate_document_estate.py --apply

# Rehash every canonical object and validate all linked artifact paths
python3 scripts/consolidate_document_estate.py --verify

# Expand one run's paths back into independent copies
python3 scripts/consolidate_document_estate.py --rollback <run-id>
```

Every replacement is written to `consolidation_runs` / `consolidation_actions` and to a fsynced
JSONL journal under `data/document_estate/journals/` before the atomic path replacement occurs.

Consolidated source artifacts are immutable content objects. Consumers must read them or replace
an artifact path atomically when introducing a new version; they must not open an existing path
for in-place mutation, because hard-linked aliases deliberately share one inode.

The initial production consolidation and its validation results are recorded in
[`DOCUMENT_ESTATE_CONSOLIDATION_2026-07-28.md`](DOCUMENT_ESTATE_CONSOLIDATION_2026-07-28.md).

## Table ownership

Several modules write `catalog.db`. That is workable only while **each table has
exactly one declaring module**. Modules that need a table they do not own must
execute the owner's DDL rather than writing their own `CREATE TABLE`; two
definitions of one table in one database is how a schema silently forks.

| Owning module | Tables |
|---|---|
| `src/shared/document_estate.py` | `documents`, `artifacts`, `project_records`, `memberships`, `hash_cache`, `content_objects`, `consolidation_runs`, `consolidation_actions` |
| `src/acquisition/ledger.py` | `acquisition_schema_migrations`, `acquisition_runs`, `acquisition_leases`, `acquisition_attempts`, `source_records`, `source_record_versions`, `document_projects`, **`outbox`** |
| `src/consumers/outbox.py` | `outbox_consumer_schema_migrations`, `outbox_subscriptions`, `outbox_deliveries`, `outbox_delivery_attempts` |
| `src/consumers/derivatives.py` | `document_derivations` |

`outbox` is owned by the acquisition ledger: acquisition publishes events,
consumers only read them and mark delivery. `src/consumers/derivatives.py` still
has to work standalone, so it executes `ledger.OUTBOX_DDL` — the owner's single
canonical definition — instead of declaring its own.

Repeating a declaration *within* one module is fine and expected: that is the
standard SQLite rename-and-rebuild migration, as `consumers/outbox.py` does when
it adds a column to `outbox_delivery_attempts`.

`tests/test_estate_schema_ownership.py` enforces all of the above. It fails if
any table gains a second declaring module, and pins the mapping in this table.

Historical note: `outbox` was declared by both `acquisition/ledger.py` and
`consumers/derivatives.py`. `src/deployment/preflight.py` still documents
deliberately not importing either writer class "because both own migrations" —
that workaround predates this boundary and can be revisited.

## Migration boundary

Exact duplicates may be hard-link consolidated with the workflow above, but unique source
artifacts remain in their established project paths. Moving all unique artifacts behind the blob
store is a separate migration and should happen only after every writer uses atomic,
content-addressed ingestion.

Alpha Go, Soft, and Earnings still contain transitional writers and private
download paths. The compatibility report view is read-only for the root
extraction pipeline, but that does not establish a repository-wide single-writer
cutover.

Production deployment of the acquisition write path must wait for:

- primary-source and issuer-lifecycle enrichment;
- consumer read/write cutover;
- controlled migration and monitored deployment of the implemented outbox
  dispatcher, plus consumers for every required downstream application;
- a shared backend contract covering local SQLite/filesystem and the intended
  production metadata/object store.

Until then, use `refresh_quarterly_estate.py audit` or `plan` for fleet
inspection and reserve `sync --apply`/`backfill --apply` for controlled,
issuer-scoped development trials.
