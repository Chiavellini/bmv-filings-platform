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

## 8. Alpha Go's projected corpus is not portable

`alpha-go/data/corpus/manifest.json` stores thousands of **absolute** paths under
the original checkout's `alpha-go/data/corpus/`. Relocating the estate bundle to
another machine leaves that manifest pointing at paths that do not exist.

The estate bundle itself *is* portable — `estate.json` needs only `estate_root`
changed, and every other key is containment-checked relative to it. This is
specifically the Alpha projection.

**Workaround**: regenerate the projection after relocating, via
`alpha-go/scripts/sync_shared_estate.py`.

## 9. Paused semantic index migration

`data/document_estate/indexes/alpha_go.semantic.db.building` is an incomplete
384-dimension re-embed (~131,584 of 320,726 chunks converted). Batches are
transactional, so resuming is safe.

**Do not point any application at it.** The live dashboard index is
`alpha_go.db` (hashing, 256d), with `alpha_go.db.previous` retained as rollback.
Neither was modified by this sweep. Run `PRAGMA quick_check` only after the
migration completes, not before.
