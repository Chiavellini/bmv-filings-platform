# Primary-PDF acquisition coverage

This runbook is the source-of-truth workflow for expanding quarterly PDF
coverage before a production scheduler is introduced. It separates three facts
that must not be conflated:

1. an issuer already has one or more usable PDFs in the shared catalog;
2. an issuer has a configured source that can be called again;
3. that configured source has been live-certified for a defined period range.

Catalog coverage alone does not prove that a repeatable source exists. A
configured URL alone does not prove that the adapter can discover or fetch a
valid quarterly document.

## Current baseline

The read-only snapshot generated on 2026-07-30 contains all 179 active issuers:

| State | Issuers | Meaning |
| --- | ---: | --- |
| `configured_with_in_window_catalog_coverage` | 21 | A primary-PDF source is configured and at least one resolvable primary PDF falls within a configured source window. This does not by itself prove which source produced the legacy PDF. |
| `configured_with_only_out_of_window_catalog_coverage` | 0 | Catalog PDFs exist, but all precede every configured source window. |
| `configured_without_catalog_coverage` | 4 | A source is configured, but no resolvable primary PDF is currently catalogued. This includes a newly certified source before its first controlled estate write. |
| `catalog_coverage_without_configured_source` | 0 | PDFs exist, but there is no repeatable source binding for future refreshes. |
| `unconfigured_without_coverage` | 154 | Neither a primary-PDF source nor catalog coverage is present. |

There are 25 configured primary-PDF issuer bindings: 24
`investor_relations` bindings and one `bmv_issuer_pdf` binding. Five bindings,
GNP, GFNorte, FEMSA, Sports World, and Tiendas 3B, contain explicit root
live-canary evidence. The BMV XBRL source is a separate regulatory-filing stream
and does not count as primary-PDF coverage.

The committed snapshot is
[`PRIMARY_PDF_SOURCE_MATRIX_2026-07-30.csv`](PRIMARY_PDF_SOURCE_MATRIX_2026-07-30.csv).
It is evidence for this date, not a live dashboard.

## Reproduce the matrix

These commands load the issuer registry and catalog without network access or
estate mutation:

```bash
# Human-portable CSV to stdout.
.venv/bin/python scripts/audit_primary_pdf_sources.py

# Complete JSON, including summary counts.
.venv/bin/python scripts/audit_primary_pdf_sources.py --json

# Refresh a dated release artifact intentionally.
.venv/bin/python scripts/audit_primary_pdf_sources.py \
  --output docs/acquisition/PRIMARY_PDF_SOURCE_MATRIX_YYYY-MM-DD.csv

# Inspect an exported or mounted catalog explicitly.
.venv/bin/python scripts/audit_primary_pdf_sources.py \
  --database /mounted/estate/catalog.db \
  --estate-root /mounted/estate --json
```

On another computer, the preferred connection is the repository's
`estate.json` bridge or the `PDFS_ESTATE_BRIDGE` environment variable. Every
path inside the exported estate bundle remains relative to its `estate_root`.
The explicit `--database` option is useful for auditing a catalog before the
bridge is switched. See
[`DOCUMENT_ESTATE.md`](../DOCUMENT_ESTATE.md) for transfer and validation.

## Source certification workflow

Use the deterministic
[`SOURCE_ONBOARDING_COMPILER.md`](SOURCE_ONBOARDING_COMPILER.md) workflow to
turn official URL seeds into bounded discovery evidence, verified sample PDFs,
inferred templates, and a reviewable registry patch. It requires no LLM and is
dry-run/non-estate-mutating by default. The manual certification rules below
remain the promotion gate.

Add a source only after completing these steps:

1. **Identify the authoritative page.** Prefer an issuer IR page. Use the
   official BMV issuer page when it is the stable authoritative quarterly
   surface available for that issuer.
2. **Classify the artifact.** Primary-PDF acquisition accepts a quarterly
   earnings release, management discussion, or financial-statements report.
   Analyst reports, certificates, annual reports, and XBRL payloads are not
   substitutes.
3. **Run fixture discovery.** Save representative markup in a test and prove
   period extraction, document selection, exclusion rules, and exact URL
   provenance without network access.
4. **Run a live read-only canary.** Discover the newest expected period and
   fetch it into temporary staging. Verify the HTTP result, `%PDF-` signature,
   non-trivial size, exact final source URL, period, language, and rendition.
   Do not write the shared catalog during certification.
5. **Declare the supported period window.** If the page exposes only current
   filings, set `coverage_from_period: YYYY-NT` to the first live-certified
   period. Do not infer historical support.
6. **Add the registry binding and tests.** The root registry owns the source;
   application-specific modules do not.
7. **Run a controlled estate trial.** Only after review, use the root
   `refresh_quarterly_estate.py sync --apply` command for one issuer, then
   inspect the source record, immutable object, document family, outbox event,
   and second-run idempotency.
8. **Promote coverage separately.** Historical backfill needs its own
   evidence and explicit command. A current-quarter source must not erase
   historical gaps.

Production preflight treats live evidence as fresh for at most four quarters.
Re-run and record a canary before that window expires, and immediately after an
issuer changes its site or document naming.

Production callers must invoke the root acquisition service or its CLI. They
must not call an adapter directly, because the service owns leases, validation,
versioning, catalog writes, coverage reporting, and outbox creation.

## Official BMV issuer-page policy

`BmvIssuerPdfAdapter` is intentionally narrow:

- it reads only the configured BMV issuer financial-information page;
- it requires HTTPS and allowlists official `bmv.com.mx` page, PDF, and redirect
  hosts before issuing the next request;
- it selects `COMENTARIOS Y ANÁLISIS DE ADMINISTRACIÓN` first;
- it falls back to `ESTADOS FINANCIEROS BÁSICOS` for the same period;
- it returns at most one candidate per period;
- it excludes analyst reports, quarterly certificates, auditor notices,
  annual reports, and XBRL links;
- it records both the listing page, discovered PDF URL, and validated final PDF
  URL;
- it rate-limits PDF fetch starts and hands bytes to the root estate writer.

GNP is the first official-BMV registry canary. Its source is activated at
`2026-2T` because the certified BMV page is a current-quarter surface; the
setting deliberately does not claim older quarters.

GFNorte is the first fail-closed issuer-IR canary. The official Banorte page was
live-tested for `2026-2T`; its allowlist selected exactly `2T26.pdf`, whose
1,366,990 bytes started with `%PDF-`. The exact URL and verification date are
recorded in the registry. The source rejects its separate risk-management PDF,
and conflicting bytes for the same period/language/rendition stop the source
rather than becoming a false revision. Its `banorte` compatibility alias and
Alpha Go project pin are expanded by the root acquisition service.

The next issuer-IR cohort certified three distinct live-source shapes:

- FEMSA uses extensionless `/static-files/<uuid>` document objects. The
  downloader ignores CMS filename metadata as URLs, assigns the period from the
  anchor text, and selected all ten quarters from `2024-1T` through `2026-2T`.
- Sports World uses deterministic
  `/reports_quarterly/gsw_reporte_NTYY.pdf` filenames. Its fail-closed selector
  fetched all ten quarters from `2024-1T` through `2026-2T`.
- Tiendas 3B uses the official Q4 financial-report JSON feed. Its fail-closed
  selector retained nine earnings releases from `2024-1T` through `2026-1T`
  while rejecting presentation PDFs and webcast media.

A disposable-estate run for the cohort stored 69 source records across the
primary-PDF and enabled BMV XBRL streams. A second run rechecked nine current
records, stored zero new versions, and marked all nine unchanged. The shared
estate was not modified.

## Promotion gates before automation

Do not install Airflow, cron, or a managed applied schedule merely because the
adapter runs. Move to scheduler work only when:

- source bindings cover the intended issuer cohort and live canaries are
  recorded;
- issuer lifecycle and filing-grace settings are reliable enough to distinguish
  late documents from nonexistent obligations;
- a controlled `sync --apply` trial proves exact provenance, validation,
  idempotency, revisions, and failure visibility;
- the transferred estate passes portability and object-integrity checks on a
  second computer;
- the database/object-store writer contract and backup/restore procedure are
  selected;
- downstream consumers are deployed so a stored quarter becomes available to
  Alpha Go, Soft, and Earnings without competing writers.

Airflow should eventually orchestrate the same bounded CLI; it should not own
issuer parsing rules or bypass the estate writer. Database and object-store
locations must be supplied through deployment configuration, with secrets held
by the scheduler rather than committed to this repository.
The GitHub/image, external-estate, and second-computer contract is specified in
[`AIRFLOW_PORTABILITY.md`](../deployment/AIRFLOW_PORTABILITY.md).

## Next coverage order

Work the matrix in evidence-first cohorts:

1. after an estate-owner backup and review, promote the FEMSA, Sports World,
   and Tiendas 3B cohort through controlled writes to the shared estate;
2. live-certify configured IR bindings that already have catalog coverage,
   starting with sources that expose the current quarter deterministically;
3. add official BMV current-quarter bindings for issuers whose pages can be
   resolved deterministically;
4. add and certify issuer IR sources where those offer deeper history;
5. design historical backfill only after ongoing refresh coverage is reliable.

Regenerate the matrix after every reviewed cohort so source configuration,
catalog evidence, and remaining gaps stay independently visible.
