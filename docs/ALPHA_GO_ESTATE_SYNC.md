# Alpha Go automatic estate search projection

Alpha Go is the search projection for the complete **searchable** document
estate. It is not limited to issuers carrying the legacy `alpha_go` project
pin. A document is searchable when the catalog has a current, readable
Markdown/search-text artifact for it.

## Automatic path

```text
acquisition commit
  -> estate.document.stored
  -> root PDF/HTML/text derivative
  -> estate.document.parsed
  -> alpha-go.search-projection.v2:<target>
  -> stable family row + FTS chunks + embeddings
```

The outbox receipt is the incremental cursor. Receipts and append-only attempts
live in the estate catalog, so restarting a worker does not lose its position.
The Alpha projection is content-hash idempotent and processes up to 64 parsed
events in one isolated subprocess. A repeated event performs no index write.

The `v2` subscription generation intentionally supersedes the old pin-filtered
consumer. Registering it backfills historical parsed events even if the `v1`
receipt was already recorded as skipped.

## Coverage and lifecycle

- PDF, HTML, Markdown, and plain-text originals receive a verified Markdown
  derivative and are eligible for search.
- News links with a catalogued text/HTML artifact are eligible.
- Raw XBRL JSON/XML/ZIP/GZIP does not yet have a text derivative. Those files
  remain available to structured-facts extraction but are not falsely exposed
  as full-text search documents.
- A corrected filing creates an immutable new estate version. Projecting either
  the old or new event resolves the family to its current version and replaces
  the old document row, FTS chunks, and vectors transactionally.
- The estate is immutable and does not treat an online source disappearing as
  a deletion. Explicit catalog removal or a missing local artifact is cleaned
  from Alpha by the periodic whole-estate reconciliation. A future legal
  withdrawal/tombstone workflow still needs its own estate event contract.

## Scheduler commands

Use absolute paths in deployment. Let `R` be the checkout and `E` the estate
root from `estate.json`.

After every successful acquisition sync, run a bounded fail-closed drain:

```bash
R/.venv/bin/python -m src.consumers.cli run --apply \
  --estate-root E --database E/catalog.db \
  --max-deliveries 10000 --require-drained \
  --enable-alpha-go --alpha-root R/alpha-go \
  --alpha-corpus E/projections/alpha-go \
  --alpha-index E/indexes/alpha_go.db \
  --alpha-config R/alpha-go/configs/alpha_go.yaml \
  --alpha-target-id portable-estate-v1 \
  --alpha-python R/alpha-go/.venv/bin/python --json
```

The command exits `0` only when every enabled receipt is terminal-success or
terminal-skip. It exits `1` after a delivery failure and `2` if the bounded run
leaves pending, running, retryable, dead, missing, or unpublished work.

Then run the whole-estate safety net. It catches searchable documents created
by legacy writers without events, metadata-only changes, missing artifacts,
and stale shared-estate index rows:

```bash
R/.venv/bin/python -m src.consumers.cli reconcile-alpha --apply \
  --estate-root E --database E/catalog.db \
  --alpha-root R/alpha-go \
  --alpha-corpus E/projections/alpha-go \
  --alpha-index E/indexes/alpha_go.db \
  --alpha-config R/alpha-go/configs/alpha_go.yaml \
  --alpha-target-id portable-estate-v1 \
  --alpha-python R/alpha-go/.venv/bin/python --json
```

Finally, use the read-only health gate:

```bash
R/.venv/bin/python -m src.consumers.cli audit-alpha \
  --estate-root E --database E/catalog.db \
  --alpha-root R/alpha-go \
  --alpha-corpus E/projections/alpha-go \
  --alpha-index E/indexes/alpha_go.db \
  --alpha-config R/alpha-go/configs/alpha_go.yaml \
  --alpha-target-id portable-estate-v1 \
  --alpha-python R/alpha-go/.venv/bin/python --json

R/.venv/bin/python -m src.consumers.cli status \
  --database E/catalog.db --json
```

`audit-alpha` is read-only. It hashes current Markdown artifacts and proves
equality between eligible estate families, the shared projection manifest, and
matching-hash index rows. Missing, stale, or hash-divergent rows return nonzero
with counts and bounded ID samples. `status` independently proves outbox
delivery health.

Do not substitute literal `R` or `E`; the deployment wrapper must resolve and
validate absolute paths. The target ID is stable for one index generation and
must change when an index is rebuilt in place so historical receipts backfill.
The existing live target can continue using the explicitly configured
`R/alpha-go/data/corpus`; do not silently redirect it. Before moving to the
portable projection directory, either seed it with the certified Alpha manifest
or accept and verify one full reconciliation/reindex. Keeping the projection at
`E/projections/alpha-go` avoids colliding with any estate export/checksum
manifest at `E/manifest.json`.

## Already-running dashboard

The dashboard and `HybridRetriever` monitor SQLite's cross-process
`data_version`. When the worker commits a new index version, the next search
invalidates its cached vector matrix and boilerplate model. Facet and suggestion
caches use the same revision token, so a newly added company, period, or
document type appears without restarting the dashboard.

## Operational prerequisites

Automatic search requires all of the following; code alone does not turn on a
worker:

1. acquisition/outbox/consumer schemas migrated on a backed-up estate;
2. one writable local estate and one-slot writer scheduling;
3. the exact Alpha index, corpus, config, Python runtime, and stable target ID;
4. the derivative and Alpha dependencies installed in their respective
   environments;
5. acquisition followed by drain, reconcile, and status in the scheduler;
6. alerts and explicit dead-letter replay for any nonzero result.
