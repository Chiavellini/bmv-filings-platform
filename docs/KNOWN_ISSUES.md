# Known issues

Deliberately deferred items, recorded so they are decisions rather than
surprises. Each entry says what is wrong, why it was not fixed, and what fixing
it would cost. Nothing here blocks day-to-day use; several block *distribution*.

Last reviewed: 2026-07-29.

---

## 1. The installed package squats the top-level name `src` — blocks distribution

`pyproject.toml` declares `packages.find include = ["src", "src.*"]`, so the
distribution's top-level import namespace is literally `src`
(`bmv_filings_platform.egg-info/top_level.txt` confirms: `estate_bridge`, `src`).
Installing this wheel into any environment claims `import src` globally and will
collide with any other project that does the same.

**Not fixed because** the repair is renaming `src/` to a distinct package name
(e.g. `bmv_platform/`), which is a directory move. This sweep was explicitly
scoped to wiring and documentation, not moves.

**Cost to fix**: mechanical but wide — the rename touches ~1,000 import sites
across root, plus the vendored copies in `alpha-go/`, `soft/`, and
`earnings/vendor/alpha-go/` that shadow the same name. Best done as one
atomic commit with no other changes in it.

**Until then**: install only into a dedicated virtualenv (`pip install -e .`),
never into a shared or system environment.

## 2. `src` is shadowed process-wide by whichever tree is first on `sys.path`

Four separate trees provide a package named `src`: root, `alpha-go/`, `soft/`,
and `earnings/vendor/alpha-go/`. Which one `import src.extract...` resolves to
depends entirely on `sys.path` order at process start.

`earnings/earnlib/bootstrap.py` handles this deliberately and documents it — the
`EARNINGS_V3` switch selects root's engine versus the vendored one, with the
warning "never mix both engines in one process". That is the good case.

The bad case is fixed but worth remembering: `scripts/expand_soft_quarterlies.py`
used to insert `soft/` at `sys.path[0]`, so `import src.*` inside a *root* script
silently resolved to Soft's extractor.

This is a direct consequence of issue #1 and disappears when the rename happens.

## 3. Vendored forks drift, and re-syncing silently reverts them

`alpha-go/` and `soft/` each carry a copy-fork of seven root packages
(`download, parse, extract, shared, model, excel, eval`), maintained by their own
`scripts/vendor_sync.py`. Current divergence from root: **alpha-go 26 files**,
**soft 8 files**.

`vendor_sync.py --sync` is `rmtree` + `copytree`. Running it today would discard
those divergences — including Soft's herdez extraction fixes — with no warning.

**Partially addressed**: `--sync` now refuses to run when `--check` reports drift
unless `--force` is passed. That converts silent data loss into an explicit
decision, but it does not reconcile the forks.

**Still open**: deciding, per diverged file, whether the fork should be upstreamed
into root or discarded. `src/extract/xbrl_facts.py` exists in three versions
(root 386 lines, alpha-go 244, earnings/vendor 244); `src/excel/segments_sheet.py`
in three (1603 / 847 / 847).

## 4. Company-specific logic is spread across five layers

Onboarding one company currently touches all of:

1. `configs/<slug>.yaml`
2. `src/extract/<slug>.py` (7 such modules: gmexico, soriana, herdez, orbia, lab, liverpool, gruma)
3. `src/model/sport_metrics.py` (one company's registry inside a shared package)
4. a hardcoded 17-company dict in `src/eval/compare_extractions.py:68-393`
5. `scripts/build_excels.py:38-40` (a hardcoded 3-company table)

Layer 4 in particular duplicates what `configs/*.yaml` already declares and must
be edited in lockstep. Consolidating these is a design change, not plumbing.

## 5. Dead and single-use scripts (~2,900 LOC)

Inventoried, not removed — deletion is your call:

| Path | LOC | Status |
|---|---:|---|
| `scripts/orbia_tailored.py` + `_review.py` + `_verify.py` | 1,366 | One-off for a single deliverable; self-described as "not the standard Segments workbook" |
| `scripts/migrate_layout.py` | 152 | "One-time repo reorganization" that already happened; zero references |
| `scripts/build_excels.py` | 80 | Superseded by `build_segments.py`; writes to `excels/`, a directory that no longer exists |
| `scripts/_probe.py` | 178 | Marked "DEV ONLY, not part of the pipeline" |
| `scripts/materialize.py` + `src/shared/materialize.py` | 173 | iCloud file warmer; `docs/DEV_SETUP.md` says it is unnecessary after moving to a non-synced disk, yet it is still an autouse fixture in `tests/conftest.py` |

`scripts/build_excels.py` was removed from the phase manifest's declared commands
because it pointed at a nonexistent output directory. The file itself remains.

## 6. Duplicated corpora across subprojects (~21 GB)

`earnings/data/soft/` (7.4 GB) mirrors `soft/data/reports/` (13 GB), and
`earnings/data/legacy_reports/` (91 MB) mirrors root `data/reports/`. The
duplication was a deliberate 2026-07-28 decision to make `earnings/` reproducible
and self-contained, recorded in `earnlib/bootstrap.py`.

The cost is drift: the mirrors already disagree (142 vs 146 company slugs). Root's
`data/document_estate/blobs/` solves exactly this problem with content-addressed
hard links and reclaimed 5.3 GiB when applied. Extending that to the subproject
mirrors is a data-migration decision, not plumbing.

## 7. `configs/xbrl_concepts.yaml` exists in five forked copies

Root, `alpha-go/configs/`, `soft/configs/`, `earnings/configs/`, and
`earnings/vendor/alpha-go/configs/`. The same is true of `metric_search.yaml`
across three trees. A metric-mapping fix made in one place does not propagate.

Related orphan: `configs/her.yaml` (224 B) has zero references anywhere and is
superseded by `configs/herdez.yaml`.

## 7b. Estate bundles must contain real directories, not symlinks

`estate_bridge` containment-checks every key against `estate_root` after
resolving the path. A bundle assembled with symlinks pointing back at an original
location therefore fails with
`EstateBridgeError: objects must remain inside estate_root`.

This is correct — the check exists to stop path traversal — but it is worth
stating, because assembling a test bundle with `ln -s` is the obvious shortcut
and produces a confusing error. Copy the directories instead.

Verify any relocated bundle with:

```bash
.venv/bin/python scripts/check_estate_connection.py --verify-relocated /path/to/estate-v1
```

## 8. Alpha Go's projected corpus is not portable — RESOLVED 2026-08-04

The projection manifest stored **absolute** paths, so relocating the estate
bundle left it pointing at paths that do not exist. The shipped
`projections/alpha-go/manifest.json` carried 3,695 of 3,695 documents rooted at
the original computer's mount point (e.g. `/Volumes/<MountName>/bmv-estate-v1`),
readable only on a computer whose mount happened to carry that name.

`src/corpus/manifest.py` now stores every path field (`markdown_path`,
`pdf_path`, `source_path`, `original_path`) relative to the estate root and
resolves them against the *current* root on load, so the on-disk manifest is
mount-independent while consumers still receive absolute paths. Legacy absolute
manifests are rebound by matching the longest path tail that exists under the
current root, so a manifest written before this change keeps working. Writers
(`save_manifest`, and `sync_shared_estate.py`, which passes the estate root
explicitly) cannot reintroduce a mount name.

Regression coverage: `alpha-go/tests/test_corpus_manifest_portability.py`.

The estate bundle itself was already portable — `estate.json` needs only
`estate_root` changed, and every other key is containment-checked relative to it.

## 9. Paused semantic index migration — superseded 2026-08-04

The checkpoint this section described,
`data/document_estate/indexes/alpha_go.semantic.db.building` at ~131,584 of
320,726 chunks, **does not exist on the certified estate**. A read-only
inventory of the connected bundle found no such file and no other resumable
checkpoint. Do not spend time looking for it or trying to resume it.

What was actually on the estate: `indexes/alpha_go.db` holding 10 documents and
1,890 chunks — a single issuer (`ac`) against a 3,695-document projection — with
no `embedding_model` or `embedding_dim` metadata, plus an empty 72 KiB staging
database `.alpha_go.db.building-13983` containing zero rows. Both are preserved
under `indexes/diagnostic-evidence-2026-08-04/`.

Because that partial index sat at the production path and the release gate only
checked file existence (see `estate_portability.inspect_alpha_index`), nothing
reported the shortfall. The gate now validates completeness, so this class of
silent partial promotion fails closed.

### Building the index is I/O-bound, not CPU-bound

The certified MiniLM embedder sustains ~132 chunks/s on an idle machine, but the
re-embed runs at ~17 chunks/s when its working database is on the external USB
estate: every batch commit pays USB fsync latency and the process sits in
uninterruptible I/O wait at single-digit CPU. Building on internal storage and
copying the finished index onto the estate is roughly 3x faster. Prefer
`--batch-size 2048` over the 512 default to cut fsync count.

## 10. `check_portable_estate.py` will not stay `verified: true` on a live estate

`manifest.json` is a byte-exact receipt written once by
`scripts/export_portable_estate.py` at export time: it records the exported
`catalog.db`'s SHA-256 and row counts. Any later write to `catalog.db` —
including a legitimate new quarterly filing landing through the normal
acquisition pipeline — permanently invalidates that SHA. `export_portable_estate`
only writes to a brand-new destination directory (it refuses to overwrite an
existing one), so there is no cheap in-place way to refresh the manifest; doing
so means a full re-export of every content object (tens of GB).

Treat `check_portable_estate.py` as a one-time "did this specific export copy
correctly" gate to run right after minting a new bundle, not as an ongoing
health check for a live, growing estate. For day-to-day verification use
`scripts/check_estate_connection.py --require-alpha-index` (connects, but does
not diff against the frozen manifest) and `process-estate-outbox audit-alpha`
(hash-audits Alpha's projection against the live catalog) — both are safe to
run repeatedly against a live estate and both were green throughout this
section's diagnosis.

Confirmed 2026-08-07: the live catalog had also accumulated 2 fake `documents`
rows (`alpha-go:acme/2025-1T`, `alpha-go:acme/shared`) and 3 dependent
`artifacts` rows, leaked by a `test_estate_bridge.py::test_alpha_upload_registration_*`
pytest run that reached the real estate before the autouse env-isolation
fixture (clearing `PDFS_DOCUMENT_ESTATE` and friends) existed. That leak was
real and has been purged (catalog backed up first to
`catalog.db.pre-acme-purge-2026-08-07.backup`); the residual manifest
SHA/count mismatch that remains afterward is the expected behavior described
above, not further leakage.
