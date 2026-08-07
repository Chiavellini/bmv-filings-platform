# Alpha Go Estate portability handoff

## Initial project state

The portable Estate release is now merged into GitHub `main`:

- repository: `https://github.com/Chiavellini/bmv-filings-platform`
- release merge commit: `bfb0d762791439c5921e048b4e7fe5989fad7408`
- included release head: `184fa13f5ac26891e704a982351f8c914fa70423`
- Estate ID: `56EC1117-38A4-4F38-A135-408F04AF9A89`
- expected catalog SHA-256: `ffa342de86947b1e9d8daf02f98fe13723a7d485d753e983429cff5221abd9ad`

The document Estate itself passed full migration certification: 18,731 documents,
20,869 artifacts, 16,510 unique objects, zero missing objects, SQLite checks, portable paths,
and all 16,510 object hashes. Extraction, onboarding, workbook, Soft, Earnings, clean-clone,
and Airflow-import workflows passed their release tests independently of Alpha.

Alpha Go is the unfinished portion. A new read-only inspection of the connected USB confirmed:

- APFS device `/dev/disk5s1` is mounted at `/Volumes/<Estate>` with 231 GiB total, 40 GiB used, and
  191 GiB free;
- the sentinel reports the expected Estate ID;
- the catalog has 18,731 documents and 20,869 artifacts and passes SQLite `quick_check`;
- the portable-estate check reports zero missing objects, zero bad hashes in its non-full-hash
  mode, zero foreign-key violations, and the expected catalog SHA-256;
- the 650 MiB multilingual MiniLM model is present and loads fully offline at 384 dimensions;
- `projections/alpha-go/manifest.json` contains 3,695 documents, but all 3,695 Markdown paths are
  absolute `/Volumes/<Estate>/bmv-estate-v1/...` paths;
- the selected `indexes/alpha_go.db` is only 9.2 MiB and contains 10 documents, 1,890 chunks,
  1,890 FTS rows, and 1,890 embeddings of dimension 384;
- that index passes SQLite `quick_check` but has no embedding-model/dimension metadata and is not a
  complete production index;
- `.alpha_go.db.building-13983` is an empty 72 KiB staging database with zero documents/chunks;
- there is no useful resumable production checkpoint in the Estate `indexes/` directory;
- the root `check_estate_connection.py --require-alpha-index` incorrectly succeeds because it
  checks only that the file exists;
- Alpha's own read-only preflight correctly fails with
  `index documents=10 manifest=3695` while confirming the runtime model is the intended 384d model.

The root problem is therefore concrete: the Estate has a complete projection and model, but only a
10-document partial semantic index; the manifest is mount-name-dependent; and the root release gate
produces a false positive for that incomplete index.

## Work completed

The portability/runtime implementation, Estate checks, Alpha projection consumer, incremental
outbox flow, native Airflow DAGs, tests, and documentation were merged into `main`. The connected
Estate was then audited read-only to replace conflicting historical checkpoint notes with the live
facts above. The production semantic index build and real-search certification remain unfinished.
No Alpha-generated runtime asset belongs in Git.

## Files changed

No source file was changed while preparing this handoff.

## Files created

- `ALPHA_ESTATE_AGENT_HANDOFF.md`: this continuation prompt and the known Alpha/USB state.

## Files intentionally left untouched

- The disconnected Estate and all of its objects, catalog, projection, models, and indexes.
- `~/estate-staging/bmv-estate-v1`, the internal safety copy on the original Mac.
- Any repository or directory named `ear`.
- Alpha source code: the next agent must reproduce and diagnose the destination-host failure first.

## Sources of truth

Read these before acting:

1. `docs/deployment/RELEASE_GATE_2026-08-04.md`
2. `docs/ALPHA_GO_ESTATE_SYNC.md`
3. `docs/deployment/AIRFLOW_PORTABILITY.md`
4. `docs/KNOWN_ISSUES.md` (treat checkpoint counts as historical, not authoritative)
5. `alpha-go/README.md`
6. `alpha-go/scripts/preflight.py`
7. `alpha-go/scripts/build_index.py`
8. `alpha-go/scripts/reembed_index.py`
9. `alpha-go/scripts/sync_shared_estate.py`
10. `estate.json` and `.env.example`

## Current project state

GitHub `main` is the current released code. The core Estate is certified and mounted. Alpha code and
fixture-level incremental ingestion tests passed, but the live index covers only 10 of 3,695
projected documents. Alpha must not be described as ready until the manifest path contract is
portable, a complete semantic index is built and certified, the false-positive root gate is fixed,
and real searches pass on the destination computer. Applied Alpha/Airflow gates remain disabled.

## Immediate next steps

Use the following prompt in the agent running on the destination computer.

---

## Paste-ready agent prompt

You are completing Alpha Go on a genuinely different Mac using the portable external BMV Estate.
Your objective is not to tailor the project to this Mac. The result must remain reproducible on any
computer with a fresh clone and the same USB Estate mounted at an arbitrary valid path.

### Authoritative release

- Upstream repository: `https://github.com/Chiavellini/bmv-filings-platform`
- Authoritative branch: `main`
- Expected starting commit: `bfb0d762791439c5921e048b4e7fe5989fad7408`
- Estate ID: `56EC1117-38A4-4F38-A135-408F04AF9A89`
- Expected catalog SHA-256: `ffa342de86947b1e9d8daf02f98fe13723a7d485d753e983429cff5221abd9ad`

### Preserve, but do not republish, the old Alpha import branch

Do **not** run:

```bash
git push origin backup/pre-merge-alpha-go-import:alpha-go-import
```

Keep the local branch `backup/pre-merge-alpha-go-import` pointing at `8b697899` as read-only
recovery evidence. The branch reference protects its objects from garbage collection; continuing
work in this clone does not endanger it.

Do not merge that branch into `main` or republish it wholesale. It has 81 non-ancestral commits and
its tree differs from released `main` across 571 files. It contains useful historical source work,
but also 268 CSV/JSONL datasets, an analyst XLSX, a SQLite database, and extensive generated
Earnings outputs deliberately excluded from the portable Git payload. It would also remove 113
files present in current `main`.

After Alpha Estate portability is complete, audit that backup separately. Port only genuinely
missing source, tests, and documentation onto a fresh branch from `main`; never restore its
generated/private payload wholesale. A redundant copy, if desired, should be an offline Git bundle
on Vault or another private backup medium rather than a public GitHub branch.

If this is a fork clone, `origin` is the fork and `upstream` must be the repository above. Update
the fork's `main` from upstream before doing anything else:

```bash
git remote get-url upstream >/dev/null 2>&1 || \
  git remote add upstream https://github.com/Chiavellini/bmv-filings-platform.git
git fetch upstream main
git switch main
git merge --ff-only upstream/main
git push origin main
git rev-parse HEAD
```

Stop if there are unexplained local changes. Preserve them instead of overwriting them. After
confirming the expected commit, create a work branch such as `claude/alpha-estate-portability`.

### Non-negotiable safety and portability rules

1. Never erase, format, repartition, force-unmount, or rename the USB.
2. Never delete or modify Estate document objects under `blobs/`.
3. Never inject test documents, fixture rows, or fake outbox events into the production Estate.
4. Treat the catalog and all existing Alpha indexes/checkpoints as read-only during diagnosis.
5. Before an authorized production index/catalog write, make a recoverable backup of the specific
   SQLite file being changed. Never delete the prior file; retain it as rollback evidence.
6. You may create or replace Alpha-generated projection/index artifacts only through the
   repository's documented atomic/checkpointed workflows, after diagnosis and tests.
7. Do not touch any directory or repository named `ear`.
8. Do not assume the original Mac's internal safety copy exists on this computer.
9. Do not hardcode `/Volumes/<Estate>`, `/Volumes/<Vault>`, `/Users/<username>`, `/private/tmp`, or
   this Mac's username. Discover the actual mount with `diskutil` and `df`.
10. Keep `estate.json` relative and tracked. Put absolute machine paths only in an ignored `.env`.
11. Do not commit `.env`, credentials, PDFs, models, indexes, caches, virtual environments, SQLite
    runtime databases, generated corpora, or datasets.
12. Use the root Python version declared by the repository and Alpha's Python 3.12 environment.
    Install only declared dependencies; do not use global `pip`.
13. Keep Hugging Face/Transformers offline. The certified model should travel on the Estate; do
    not silently download or substitute a model.
14. Before any job expected to run longer than ten minutes, print the exact command, expected
    duration, output/checkpoint path, free-space requirement, and exact resume command. Report
    progress periodically. Do not restart a multi-hour build from zero without diagnosing why.
15. Do not enable unattended Alpha/Airflow writes until the index is certified, two canaries pass,
    and alerting is configured.

### Confirmed live Alpha state

The Estate is currently connected at `/Volumes/<Estate>/bmv-estate-v1` on APFS `/dev/disk5s1`, with
191 GiB free. Treat that as the observed runtime path, not a path to hardcode.

- Estate identity and catalog checks pass: 18,731 documents, 20,869 artifacts, 16,510 objects,
  expected catalog SHA-256, SQLite `quick_check=ok`, and zero missing objects/FK violations.
- `models/paraphrase-multilingual-MiniLM-L12-v2` is 650 MiB and loads offline as the intended 384d
  sentence-transformers model.
- `projections/alpha-go/manifest.json` contains 3,695 documents. All 3,695 `markdown_path` values
  are absolute paths beginning `/Volumes/<Estate>/bmv-estate-v1`; this must be made relocatable or
  safely rebound/regenerated on mount changes.
- `indexes/alpha_go.db` passes SQLite quick check but contains only 10 documents and 1,890 matching
  chunks/FTS/embeddings, all 384d. It lacks model/dimension metadata and is not production-ready.
- `indexes/.alpha_go.db.building-13983` is empty and is not a useful resume checkpoint.
- `scripts/check_estate_connection.py --require-alpha-index` currently returns success for this
  incomplete file. This is a release-gate bug.
- `alpha-go/scripts/preflight.py` loads the correct offline model and fails correctly with
  `index documents=10 manifest=3695`.

Historical 28,160/131,584 checkpoint notes refer to files that are not present in the connected
Estate. Do not waste time searching for or trying to resume them unless a new inventory discovers a
separate explicitly supplied checkpoint.

### Phase 1 — destination and checkout preflight

1. Show `git status`, remotes, branch, and HEAD. Confirm the expected main commit.
2. Read the source-of-truth documents listed above and inspect the current implementations rather
   than copying commands from stale notes.
3. Confirm the observed external disk state with `diskutil` and `df`: currently `/dev/disk5s1`,
   APFS, mounted at `/Volumes/<Estate>`, with approximately 191 GiB free. Device numbers and mount
   names can change after reconnecting, so rediscover rather than trusting the observation blindly.
4. Locate `bmv-estate-v1`, read the sentinel, and verify the Estate ID before touching Alpha files.
5. Create an ignored `.env` from `.env.example` using the actual paths:

```text
PDFS_ESTATE_MOUNT_ROOT=<actual mount root>
PDFS_DOCUMENT_ESTATE=<actual mount root>/bmv-estate-v1
PDFS_ESTATE_ID=56EC1117-38A4-4F38-A135-408F04AF9A89
PDFS_ESTATE_BRIDGE=<absolute path to this clone>/estate.json
PDFS_ALPHA_ROOT=<absolute path to this clone>/alpha-go
PDFS_ALPHA_CORPUS=<actual estate>/projections/alpha-go
PDFS_ALPHA_INDEX=<actual estate>/indexes/alpha_go.db
PDFS_ALPHA_CONFIG=<absolute path to this clone>/alpha-go/configs/alpha_go.yaml
PDFS_ALPHA_PYTHON=<absolute path to this clone>/alpha-go/.venv312/bin/python
```

Choose and document a stable `PDFS_ALPHA_TARGET_ID`. If the production index generation is replaced,
use a new generation ID so historical parsed events can backfill; do not silently reuse an
incompatible old subscription.

6. Confirm `.env` is ignored. Load it without modifying tracked configuration.
7. Install the documented root and Alpha environments. Run dependency checks.
8. Run the base Estate checks read-only, initially without `--require-alpha-index`:

```bash
.venv/bin/python scripts/check_estate_connection.py --json
.venv/bin/python scripts/check_portable_estate.py \
  --estate-root "$PDFS_DOCUMENT_ESTATE" \
  --mount-root "$PDFS_ESTATE_MOUNT_ROOT" \
  --estate-id "$PDFS_ESTATE_ID" --json
```

Do not repeat the full 16,510-object rehash unless these checks reveal a real integrity failure.

### Phase 2 — read-only Alpha diagnosis

Reconfirm, without changing them:

- `$PDFS_ALPHA_CORPUS/manifest.json` and its document/chunk inputs;
- `$PDFS_DOCUMENT_ESTATE/models/paraphrase-multilingual-MiniLM-L12-v2`;
- `$PDFS_ALPHA_INDEX`, `*.previous`, `*.building`, `*.semantic*`, and other candidate indexes;
- free space and physical sizes;
- SQLite `PRAGMA quick_check`, table counts, metadata, embedding dimensions, model name, and source
  signatures for every index/checkpoint that can be opened read-only;
- absolute paths in the manifest, especially paths referring to the original computer;
- catalog searchable-family counts and current projection audit;
- outbox subscriptions/receipts and current Alpha target IDs.

Use SQLite URI `mode=ro` for diagnosis. Do not point the dashboard at an incomplete `.building`
file. Do not call a hashing index semantic-ready.

Start from the confirmed root cause, reproduce it with commands/tests, and report any additional
cause you discover. Answer:

1. Is the portable projection actually portable, complete, and readable on this Mac?
2. Is the certified model present, checksummed/identified, and loadable fully offline?
3. Confirm that `estate.json` selects `indexes/alpha_go.db` and that it still contains only 10
   documents/1,890 384d embeddings.
4. Why was a partial 10-document index promoted or left at the production path without complete
   model metadata?
5. Confirm there is no useful resumable checkpoint beyond the empty staging DB.
6. Is the blocker bad paths, missing runtime dependencies, an incomplete build, insufficient disk,
   incompatible model/runtime, consumer drift, or more than one of these?
7. Which exact recovery path is safest and why?

Treat manifest/current catalog counts as authoritative. The historical 3,695/320,726 figures are
only a baseline; explain any legitimate changes.

### Phase 3 — reproduce and fix portability defects

Before changing source, reproduce every code defect with a focused failing test. At minimum add
regressions for the two proven defects: all-manifest absolute path dependence and the false-positive
`--require-alpha-index` gate. Make the smallest portable correction. Relevant areas include:

- absolute paths in the current 3,695-document projection manifest;
- model resolution through the Estate bridge;
- index completeness/model metadata validation in both root and Alpha preflight;
- preventing or clearly rejecting promotion/use of a partial 10-document index;
- cross-machine checkpoint resume;
- index generation/target-ID transitions;
- dashboard and worker selecting different indexes;
- fail-closed behavior for missing/wrong Estate IDs;
- automatic cache refresh after an external index commit.

Do not paper over failures by changing tracked files to this Mac's paths. Regenerate the Estate
projection through `alpha-go/scripts/sync_shared_estate.py` if the manifest is legacy/nonportable.
Use a disposable Estate/catalog/index fixture for destructive and incremental tests.

### Phase 4 — complete the semantic index safely

No useful resume checkpoint is currently present. Prefer a workflow that first creates a complete,
quickly reproducible keyword/hashing source index from the corrected portable projection, then uses
`scripts/reembed_index.py` to create the semantic destination copy-on-write with its resumable
`.building` checkpoint. If code inspection proves a direct full semantic build is equally safe and
resumable across interruption, document that evidence. Choose exactly one path:

1. **Resume:** Only if a newly discovered checkpoint is structurally valid and its recorded source
   signature matches the complete source index/current chunk set, resume with
   `scripts/reembed_index.py --resume`. The known empty staging DB does not qualify.
2. **Copy-on-write re-embed:** If a complete hashing source index is valid but no compatible
   checkpoint exists, create a new semantic destination and retain the hashing index as rollback.
3. **Full build:** If neither source nor checkpoint is compatible, build from the portable Estate
   projection with `scripts/build_index.py` into its staging path and promote only after its atomic
   checks pass.

Never overwrite the only good source. Preserve the current 10-document index and empty staging DB as
diagnostic evidence until a complete replacement is certified. If the chosen multi-hour workflow is
not safely resumable after interruption on this host, fix and test resumability before spending
hours on the production build.

Use the certified sentence-transformers backend and Estate model, not hashing fallback. Tune worker
and thread counts only after a small benchmark demonstrates that the choice is stable on this Mac;
do not encode host-specific worker counts into tracked portable configuration unless justified.

Promotion requirements:

- SQLite `PRAGMA quick_check` returns `ok`;
- document count equals the current manifest;
- `chunks == chunks_fts == embeddings`;
- every embedding has the intended dimension (expected 384 for the certified model);
- index metadata names the actual certified model;
- projection/index content hashes agree under `audit-alpha`;
- previous live index remains recoverable;
- remaining Estate capacity is reported.

### Phase 5 — real Alpha acceptance

After promotion, run explicit preflight against the production Estate paths:

```bash
cd alpha-go
.venv312/bin/python scripts/preflight.py \
  --config configs/alpha_go.yaml \
  --corpus-dir "$PDFS_ALPHA_CORPUS" \
  --db "$PDFS_ALPHA_INDEX"
cd ..
.venv/bin/python scripts/check_estate_connection.py --require-alpha-index --json
```

Run the root read-only Alpha audit and consumer status commands from
`docs/ALPHA_GO_ESTATE_SYNC.md`. Resolve all missing, stale, or hash-divergent rows before declaring
success.

Run real semantic and hybrid searches from `alpha-go/scripts/search_demo.py` across at least five
companies and multiple periods/document types. Include Spanish and English concept queries, exact
keyword queries, and company filters. At minimum cover Walmex, Bimbo, Herdez, Soriana, and one other
issuer. For each query, record the top results and manually verify that the returned company,
period, document, snippet, and source path are real and relevant. Nonempty results alone are not
acceptance.

Launch the real Streamlit dashboard on loopback and verify:

- semantic and keyword search;
- company, period, industry, and document-type filtering;
- highlighted snippets and source/PDF navigation;
- mention trends;
- Smart Summaries;
- financials panel;
- offline HTML/CSV export;
- upload behavior using a disposable fixture or clearly isolated user test area;
- cache refresh after a disposable incremental index commit.

If cited Q&A, news synchronization, SEC ingestion, or another optional feature requires credentials
or network access, do not fabricate success. Verify its fail-closed/configuration behavior and list
the exact credential or user authorization required.

### Phase 6 — incremental and scheduler certification

1. Run Alpha's offline test suite and the root Alpha consumer/acquisition tests.
2. Prove idempotent incremental ingestion with a disposable copied/minimal catalog, corpus, and
   index: first event adds the document, repeated event performs no write, corrected version
   replaces the current family, audit returns healthy, and the running retriever sees the update.
3. Never perform this fixture test by injecting data into production Estate.
4. Import all Airflow DAGs and confirm zero import errors.
5. Keep applied acquisition and Alpha gates disabled until a documented real one-company canary
   succeeds twice and alerting is configured. If authorized to run the canary, use official
   acquisition -> drain -> reconcile -> audit -> status order and preserve receipts.

### Phase 7 — non-regression and fresh-clone proof

From a genuinely fresh temporary clone at the resulting commit:

- install only documented dependencies;
- connect to the actual Estate through an untracked `.env`;
- pass base and `--require-alpha-index` preflight;
- read representative reports;
- run representative extraction/onboarding/workbook smoke tests;
- run Soft and Earnings smoke tests;
- launch Alpha and repeat representative real searches;
- import scheduler/DAG configuration;
- prove missing mount and wrong Estate ID fail closed.

Run the full relevant final suites and compare with the release baselines:

- root: 1,043 passed, 193 skipped, 4 deselected;
- Alpha: 486 passed, 1 skipped, 3 deselected;
- Soft: 326 passed, 2 skipped, 1 deselected;
- Earnings: 117 passed, 2 skipped.

Counts can legitimately change when tests are added; explain every failure, xfail, or unexpected
drop. Run the Git payload/history audit and search for secrets, generated assets, absolute
machine paths, `/Volumes/<Vault>`, and temporary integration paths.

### Phase 8 — Git delivery

Keep code changes on the work branch. Commit only intentional source, test, and documentation
files. Push the work branch to the fork and open a PR targeting upstream `main`. Do not merge it
until the complete acceptance evidence above is green and the user authorizes the merge. Runtime
indexes, models, corpora, databases, and `.env` stay on Estate or in ignored disposable storage.

### Required final report

Clearly separate:

1. root cause found;
2. code fixes and files changed;
3. actual Estate projection/model/index inventory;
4. semantic build or resume command, duration, checkpoint behavior, and final counts;
5. SQLite/preflight/audit results;
6. real search queries and verified results;
7. dashboard feature results;
8. incremental-ingestion and Airflow results;
9. fresh-clone and non-Alpha regression results;
10. Git branch, commits, push, and PR;
11. remaining credential-gated or operational blockers;
12. exact steps required to reproduce this on a third computer.

Do not call Alpha or the repository fully portable merely because unit tests pass. Completion means
the destination computer runs the real Estate-backed semantic index and returns verified results,
while a fresh clone remains machine-independent.
