# GitHub release gate — 2026-07-30

## Decision

The sanitized source candidate is ready for a **private** GitHub repository.
It is an isolated repository with one root commit; no remote has been added and
nothing has been pushed. The original `alpha-go-import` worktree, index,
history, and legacy document estate remain untouched.

Public publication is still blocked because the repository has no `LICENSE`.
Connecting the legacy estate read-write on another computer is also blocked
until that estate is migrated and verified separately. Neither blocker prevents
publishing the private source repository or running a greenfield canary.

No Docker runtime or Docker release artifact is part of this gate.

## Exact candidate evidence

The release candidate contains 1,229 explicitly allowlisted files:

- exactly one root commit and no inherited source-repository history;
- a clean worktree and clean `git fsck --full --strict`;
- a packed repository size of approximately 1.75 MiB; and
- zero payload/history findings from `scripts/audit_git_payload.py`.

The payload contains source, configuration, documentation, tests, synthetic
fixtures, and native-Airflow definitions. It contains no PDF, database, prior
history, estate/corpus, private analyst workbook, non-synthetic Bloomberg
export, generated Alpha evaluation bundle, secret file, personal absolute
path, Docker artifact, or oversized file.

## Clean-checkout certification

`scripts/certify_clean_clone.py --scope all --quick` cloned the candidate into
a second isolated checkout, proved that no estate/corpus was attached, and ran
each module with its declared interpreter:

| Module | Interpreter | Result |
|---|---|---|
| Root platform | Python 3.13.9 | 846 passed, 197 optional-data skips, 4 network/model deselections |
| Alpha Go | Python 3.12.13 | 479 passed, 3 optional-data skips, 3 network/model deselections |
| Soft | Python 3.11.13 | 326 passed, 2 optional-data skips, 1 network/model deselection |
| Earnings | Python 3.11.13 | 116 passed, 3 optional-data skips |

The optional skips now describe genuinely external inputs. Root
company-specific ground truth, Alpha corpus-derived sentiment labels, and the
Earnings analyst workbook are not hidden clean-clone requirements; their
parser/behavior paths retain synthetic unit coverage.

## Native Airflow certification

The candidate installer created a disposable native Python 3.13 environment
and installed Apache Airflow 3.3.0 from the official Python 3.13 constraints.
The subsequent project dependency install explicitly repinned Airflow, as
required by the upstream constraints guidance.

Results:

- `pip check`: no broken requirements;
- editable project import resolved to the isolated candidate checkout;
- Airflow metadata migration on disposable SQLite: pass;
- one-slot `estate_writer` pool creation: pass;
- local DAG list: `quarterly_estate_audit` and `quarterly_estate_sync`;
- local DAG import errors: `[]`;
- generated environment file mode: `0600`;
- disabled sync wrapper: correctly refused an applied command; and
- Airflow read-only task: succeeded and did not create or modify the empty
  document estate.

The empty-estate audit reported 179 active issuers, 177 configured BMV XBRL
bindings, 25 current primary-PDF bindings, 7,339 due PDF periods, and 160
readiness gaps. Its zero locally verified XBRL count is expected for an empty
estate; it is not the source-registry verification count.

## Scoped applied Airflow canary

The applied task was enabled only in the process environment and scoped to
`gnp,femsa,sports_world,tiendas_3b`. The sync DAG remained gated by both its
paused-on-creation policy and `PDFS_AIRFLOW_SYNC_ENABLED`.

The cohort exercised:

- shared BMV XBRL acquisition;
- official-BMV issuer PDF discovery;
- extensionless/static-file investor-relations downloads;
- deterministic quarterly PDF links; and
- a feed-backed quarterly-results adapter.

First run:

- 70 documents discovered and 70 stored;
- 40 raw XBRL gzip artifacts and 30 original PDFs;
- zero execution failures; and
- expected historical coverage gaps retained as findings because the canary
  used `--allow-coverage-gaps`.

Immediate repeat:

- 10 recent documents rediscovered;
- 0 stored;
- 10 unchanged; and
- zero execution failures.

The estate remained 141 files and approximately 126 MiB after the repeat.
SQLite integrity was `ok`.

One bounded outbox delivery then initialized the consumer schema, parsed the
GNP PDF, and succeeded with no retry/dead result. The derived Markdown path is
bundle-relative:

```text
views/parsed/gnp/2026-2T__5c0e06595411__6ccfc5b65c24.md
```

Post-canary checks found 70 documents, 71 artifacts/content objects after the
derivative, zero absolute artifact paths, zero unsafe/missing object keys, zero
missing blobs/artifacts, and no original hash without a content object. A
relocated-root connection check returned `connected: true` with no problems.

## Downloader/source coverage

The current registry boundary is:

- 179 active issuers;
- 177 configured BMV XBRL sources;
- 25 current primary-PDF sources;
- 5 live-verified primary-PDF bindings;
- 20 configured but not yet live-verified primary-PDF bindings; and
- 154 issuers without a primary-PDF source.

These gaps remain visible and block a claim of complete full-fleet production
coverage. They do not justify 179 separate downloaders: Airflow invokes one
fleet engine, while issuer differences remain declarative in
`configs/issuers.yaml` and shared adapter types.

## Estate boundary

### Greenfield/disposable estate

The canary proves the portable layout:

```text
estate/
  catalog.db
  blobs/<content-addressed objects>
  views/reports/<issuer>/<period>.<ext>
  views/parsed/<issuer>/<period>__<ids>.md
```

Airflow metadata is separate from `estate/catalog.db`. New catalog paths and
object keys are bundle-relative, and the SQLite writer is serialized through
one Airflow pool slot.

### Existing legacy estate

The earlier read-only legacy preflight found missing acquisition/outbox/
consumer tables, no portable object-key column, 20,869 absolute artifact
paths, and 699 original hashes without a matching content object. That live
estate was not modified by this gate.

A legacy export must fence writers, take a consistent SQLite snapshot, migrate
all paths/object keys, include every referenced blob, generate a checksum
manifest, and pass a relocated-root verification. Until then, do not attach
that catalog read-write on the other computer.

## Downstream freshness boundary

Airflow currently keeps the root raw-document estate fresh. The root derivative
consumer handles PDF/HTML/text originals and explicitly skips raw XBRL facts.
Soft and Earnings production rebuilds are not yet scheduled consumers.

Therefore:

- the private code repository may be published;
- a second-computer greenfield downloader canary may proceed;
- full-fleet unattended sync must remain disabled; and
- the project must not claim that Airflow refreshes every analytical output.

Alpha Go can consume an exported corpus/index, but the index must match its
configured multilingual MiniLM 384-dimensional runtime. The data bundle and
model/index selection remain separate from the Git source release.

## Remaining destination-host checks

These can only be certified on the other computer:

1. clone the private repository and rerun the payload plus four no-estate
   suites;
2. install the native Airflow environment with Python 3.13;
3. validate the physical external-disk mount and permissions;
4. run the audit task, then a scoped applied canary twice;
5. install Chromium/host libraries only for sources that require Playwright;
6. validate native scheduler/API-server/dag-processor supervision; and
7. use PostgreSQL only for durable Airflow metadata, never for the estate
   catalog.

## Publication sequence

1. Create an empty **private** GitHub repository after its owner/name are
   confirmed.
2. Push only the isolated one-commit candidate, not the original branch or
   repository history.
3. Require the two GitHub Actions workflows to pass.
4. Clone on the destination computer and follow
   `docs/deployment/AIRFLOW_PORTABILITY.md`.
5. Start with a disposable/greenfield external-disk estate.
6. Keep `PDFS_AIRFLOW_SYNC_ENABLED=false` except during the explicit scoped
   canary.
7. Treat public licensing, source-coverage expansion, downstream consumer
   scheduling, and legacy-estate migration as separate follow-on gates.
