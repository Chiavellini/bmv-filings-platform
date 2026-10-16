# Known issues

Deliberately deferred items, recorded so they are decisions rather than
surprises. Each entry says what is wrong, why it was not fixed, and what fixing
it would cost. Nothing here blocks day-to-day use; several block *distribution*.

Last reviewed: 2026-09-22.

Re-verified during the pre-delivery sweep: every claim below was checked against
the tree rather than carried forward. Items 4 and 5 had drifted out of date in
one direction and item 3 in the other. This file did not previously mention the
Analyst Console or the Extractor, which were merged after its prior review; see
item 14.

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
`scripts/vendor_sync.py`. Divergence from root, measured 2026-09-07: **alpha-go 49 entries**,
**soft 39 entries** (`src/extract` alone accounts for 26 and 21). The previously
recorded "alpha-go 26 / soft 8" understated soft by roughly five times, so treat
any re-sync as more destructive than the old numbers suggest.

`vendor_sync.py --sync` is `rmtree` + `copytree`. Running it today would discard
those divergences — including Soft's herdez extraction fixes — with no warning.

**Partially addressed**: `--sync` now refuses to run when `--check` reports drift
unless `--force` is passed. That converts silent data loss into an explicit
decision, but it does not reconcile the forks.

**Still open**: deciding, per diverged file, whether the fork should be upstreamed
into root or discarded. `src/extract/xbrl_facts.py` exists in three versions
(root 386 lines, alpha-go 244, earnings/vendor 244); `src/excel/segments_sheet.py`
in three (1603 / 847 / 847).

## 4. Company-specific logic is spread across four layers

Onboarding one company currently touches all of:

1. `configs/<slug>.yaml`
2. `src/extract/<slug>.py` — **13 such modules** as of 2026-09-07 (ac, becle,
   femsa, gmexico, gruma, herdez, kimber, kof, lab, liverpool, orbia, soriana,
   tbbb), registered through `src/extract/custom_registry.py`
3. `src/model/sport_metrics.py` (one company's registry inside a shared package)
4. a hardcoded company dict in `src/eval/compare_extractions.py`

The former fifth layer, `scripts/build_excels.py`, was deleted (see item 5), so
this is now four layers rather than five — but layer 2 has nearly doubled since
this was written, which makes consolidating layer 4 more valuable, not less.

Layer 4 in particular duplicates what `configs/*.yaml` already declares and must
be edited in lockstep. Consolidating these is a design change, not plumbing.

## 5. Dead and single-use scripts — RESOLVED 2026-09-07

Every path inventoried here has since been deleted. Verified absent:
`scripts/orbia_tailored.py`, `_review.py`, `_verify.py`, `migrate_layout.py`,
`build_excels.py`, `_probe.py`, `scripts/materialize.py`.

`src/shared/materialize.py` intentionally survives as a library — it still backs
the autouse warm-up fixture in `tests/conftest.py` — but its standalone CLI is
gone, so the commands in `docs/DEV_SETUP.md` that invoked it were dead and have
been removed.

The original inventory is kept below for provenance:

| Path | LOC | Status |
|---|---:|---|
| `scripts/orbia_tailored.py` + `_review.py` + `_verify.py` | 1,366 | One-off for a single deliverable; self-described as "not the standard Segments workbook" |
| `scripts/migrate_layout.py` | 152 | "One-time repo reorganization" that already happened; zero references |
| `scripts/build_excels.py` | 80 | Superseded by `build_segments.py`; writes to `excels/`, a directory that no longer exists |
| `scripts/_probe.py` | 178 | Marked "DEV ONLY, not part of the pipeline" |
| `scripts/materialize.py` + `src/shared/materialize.py` | 173 | iCloud file warmer; `docs/DEV_SETUP.md` says it is unnecessary after moving to a non-synced disk, yet it is still an autouse fixture in `tests/conftest.py` |

`scripts/build_excels.py` was first removed from the phase manifest's declared
commands, then deleted outright. `scripts/build_segments.py` supersedes it.

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

## 11. `PDFS_REPORTS_DIR` redirects `REPORTS_DIR` at a view that holds no Markdown

Attaching an estate exports `PDFS_REPORTS_DIR=<estate>/views/reports`, and root, `soft/`,
`alpha-go/`, and `earnings/vendor/alpha-go/` each bind their own `REPORTS_DIR` from it **at module
import time**. That is deliberate, but it silently changes what a "reports directory" contains:

| | `views/reports/<slug>/` | `views/parsed/<slug>/` |
|---|---|---|
| Holds | PDFs, legacy Markdown, XBRL facts JSON | current versioned acquisition derivatives (`.md`) |
| Configurable | yes — `estate.json` `reports_view`, `PDFS_REPORTS_DIR` | no — hardcoded, `src/shared/paths.py` |
| On duplicate periods | loses | wins (`src/extract/pipeline.py`) |
| Constant exists in | root, soft, alpha-go, earnings | **root only** |

A walmex slice of the certified estate is 387 PDFs and zero `.md` in the reports view, with its 49
parsed Markdown files in the sibling parsed view. Any consumer that globs `*.md` in one directory
therefore reports an empty corpus for a corpus that is fully present. Root already solved this
three times — `scripts/build_segments.py::_report_sources`, `scripts/report_coverage.py::_corpus_dirs`,
and `src/eval/compare_extractions.py` — each reading the views as one ordered union. **Follow that
union in any new consumer.**

Found 2026-08-13 by the second-machine acceptance run, which hit it twice:
`tests/test_paths.py::test_dir_constants_are_expected_children` asserted the repo-local child
unconditionally, and `soft/tests/test_smoke.py::test_walmex_config_and_filings_cached` counted
Markdown in the reports view alone. Both failed *because* the operator followed the documented
setup. Both are fixed.

**Still open — the guard in `tests/_corpus.py` disagrees with the constant its callers read.**
It hardcodes `REPORTS_DIR = ROOT/"data"/"reports"` while `test_paths.py`, `test_era_gate.py`, and
`test_ebitda_quarter_patterns.py` go on to read `src.shared.paths.REPORTS_DIR`. Under an estate the
two point at different trees, so `skip_without_corpus()` reports "corpus not checked out" for a
corpus that is attached — a silent false *skip*, the mirror image of the two failures above.

**Not fixed because** the obvious repair makes things worse. Pointing the guard at the overridable
constant is not enough: `has_corpus()` returns true for *any* non-empty directory
(`... or any(target.iterdir())`), so a PDF-only view reads as a present corpus and
`tests/test_no_regression.py` — whose own comment says it "reads every company's parsed filings" —
un-skips and fails all 17 companies. Measured against a synthetic estate-shaped tree: 1 failure
before, 17 after. A correct fix also has to make `has_corpus()` require the Markdown its callers
actually read, and that cannot be validated without the USB attached, since whether
`views/reports/<slug>/` carries legacy Markdown varies by issuer.

**Also still open**: `soft/` has no `conftest.py` at all, so it has no equivalent of the autouse
`_bundle_env_isolation` fixture that `tests/test_estate_bridge.py` and
`tests/test_alpha_index_completeness.py` use to keep an exported `.env` out of hermetic tests. Note
that a fixture cannot fix path-constant tests anyway — those constants bind at import, so such
tests must derive their expectation from the environment instead.

## 12. The certified estate carries no soft-derived XBRL facts

`EstateReader.xbrl_facts_map("walmex", project="soft")` returns `{}` against the connected bundle:
no artifact anywhere in the catalog matches `*_facts.json` for walmex under that project.

The cause is structural rather than walmex-specific. `project="soft"` rows are written by exactly
one place — `scripts/build_document_estate.py`, which calls
`import_tree(estate, "soft", ROOT/"soft"/"data"/"reports")` against **the builder host's local soft
corpus**. A bundle exported from a host where that tree is absent or empty carries no such rows,
and because `import_tree` calls `reset_project()` first, a rebuild on such a host also wipes any
that a previous build had registered.

`tests/test_estate_reader.py::test_live_catalog_walmex_soft_facts` now skips on absent data instead
of failing — its `skipif` marker gated on the catalog *file* existing, never on its contents. The
project-filter contract it covers is exercised hermetically by
`test_project_filter_separates_soft_and_root_facts` against the synthetic catalog, so little is
lost.

**Cost to fix**: re-run soft's XBRL derivation and register the artifacts. That is an estate write,
so it needs the USB attached and belongs with the other deliberate write operations, not in a test
run.

## 13. Two complete dashboard panels are unreachable from the UI

`alpha-go/app/components/panels.py` defines `render_summary` and `render_financials` — both fully
implemented, the latter backed by all of `app/components/financials.py` — and **nothing calls
either**. `app/streamlit_app.py` dispatches `render_search`, `render_trends` and Ask
(`app/components/ask.py`, over `src/qa/ask.py`), and
`alpha-go/tests/test_dashboard_modes.py::test_explore_modes_are_search_trends_ask` locks the mode
list in: `assert _EXPLORE_MODES == ("Search", "Trends", "Ask")`. What *is* reachable besides the
three modes lives inside the Search reader pane: a per-document Smart Summary
(`_render_smart_summary`) and a Document/Company toggle whose Company pane is the company card
(`_render_company_card`). The card's **Load financials** button reaches
`financials.render_financials_table` directly, so only the `panels.render_financials` wrapper is
dead. The older cited-Q&A module `src/qa/rag.py` is still library-only.

That financials table reads `alpha-go/data/corpus/<slug>/*.md` (`app/components/financials.py`), a
repo-local tree that neither a fresh clone nor the Estate provides. With only the USB attached it
therefore always shows "No parsed reports for <slug>". It degrades cleanly, but it is empty on every
machine that has no local corpus. Pointing it at the Estate's parsed view needs the USB to confirm the
file layout.

Four documents once claimed the standalone panels shipped and sent the second-machine tester
hunting for them; they were corrected on 2026-08-13, and corrected again on 2026-09-22 after commit
5f72f20c added Ask and the company card (`ALPHA_ESTATE_SECOND_MACHINE_TEST.md`,
`ALPHA_ESTATE_AGENT_HANDOFF.md`, `alpha-go/README.md`, `alpha-go/docs/ROADMAP.md`). `ROADMAP.md`
also documented `render_doc_viewer`, a function that no longer exists — in-document reading happens
inside `render_search` via `html_view.py`/`pdf_view.py`.

**Still open**: whether to wire the two standalone panels in or delete them. Wiring means extending
`_EXPLORE_MODES` and updating `test_dashboard_modes.py`, and neither panel has any test coverage
(`tests/test_summarize.py` exercises `src/qa/summarize.py` directly, never the panel). Until that is
decided the docs describe what is reachable, not what is implemented.

## 14. The Analyst Console is the least-tested surface, and the only one the analyst sees

Added 2026-09-07. `src/analyst_console/` plus `scripts/install_analyst_console.py`
and `scripts/extract_pdf_observations.py` were merged after every handoff
document and after this file's previous review, so nothing above covers them.

The root README calls the launchpad icon "the only visible entry point" for the
analyst. Behind it sits a loopback HTTP bridge that accepts a 60 MB file upload,
executes allowlisted argv arrays as subprocesses, installs a login-persistent
launchd agent, and exposes estate-mutating operations behind confirmation
phrases and a process-wide writer lock. Its "Actualizar biblioteca completa"
action runs a real fleet refresh, outbox drain, and Alpha index update against
the production estate.

Two structural constraints worth knowing before changing anything:

- **The hosted launchpad is tied to port 8765.** The Content-Security-Policy of
  the published GitHub Pages asset (`src/analyst_console/static/index.html`,
  `connect-src http://127.0.0.1:8765`) means the hosted copy can only ever talk
  to that port, so changing it means rebuilding and republishing the static
  assets. The copy the bridge serves itself is not tied: `app.js` talks back to
  whatever loopback origin served it, so a bridge started with `--port <other>`
  works for local testing. Port 8501 (Streamlit) is still fixed, with no
  fallback if it is taken.
- **`ANALYST_CONSOLE_PAGES_ORIGIN` is the only runtime knob**, and the origin
  `https://chiavellini.github.io` is hardcoded in both the installer and the
  server. Moving the repository to a different GitHub organization breaks the
  CORS allowlist and the Pages URL together.

Coverage today is `tests/test_analyst_console.py` and
`tests/test_analyst_console_extractor.py`. Both now inherit the global estate
env-isolation fixture added to `tests/conftest.py` on 2026-09-07, which they
previously lacked.

On 2026-09-07 the security boundary was additionally exercised against a live
bridge on a scratch port: CORS on reads, the `X-Analyst-Console` CSRF header on
writes, the `%PDF-` magic-byte check, filename sanitization against path
traversal, period inference, upload containment, and honest degradation with no
estate attached all behave as documented. Results are recorded in
[the 2026-09-07 release gate](deployment/RELEASE_GATE_2026-09-07.md).

On 2026-09-08 every control was additionally exercised against a live bridge,
driving the real launchpad in a browser. That pass found and fixed six defects
invisible to the test suite -- the launchpad being welded to port 8765, a
`bootstrap()` that re-parsed the issuer registry on every 1.8 s poll, Alpha Go
reporting `running: True` forever after a crash (unreaped zombie), an empty
coverage matrix reporting success, and two estate-write gaps. Details and the
verified-working list are in
[the 2026-09-07 release gate](deployment/RELEASE_GATE_2026-09-07.md).

**Still unverified:** the `install_analyst_console.py` install/uninstall round
trip, launchd survival across logout, the real GitHub Pages origin reaching the
bridge on 8765, and every estate-backed launchpad action (connect,
disconnect+eject, `segments_model` end to end, and the success paths of the two
destructive operations). Those need the console installed and the estate mounted,
so they belong to the second-machine run.

## 15. The positional table tier can match a label that merely contains the target

Found 2026-09-08 while extracting a real filing through the Analyst Console.

Grupo Bimbo's income statement carries three lines containing "NET INCOME":

```text
NET INCOME BEFORE TAXES                  4,443  3,544
INCOME (LOSS) FROM CONTINUED OPERATIONS  2,799  2,229
NET MAJORITY INCOME                      2,362  1,785
```

The generic `net_income` patterns in `src/model/financial_model.py` require a
number immediately after the label, so they correctly declined all three. The
extraction cascade then fell through to the positional `table` tier, which
matched **the first one** by label similarity and returned pre-tax income as
"Utilidad Neta" — 4,443 against a true 2,362, an **88% overstatement**.

It shipped as `validated=True, flagged=False`. The only signal that anything was
weaker than usual was `confidence: 0.6` versus 0.85 for the metrics resolved by
`regex_table`, and that number is not surfaced to the analyst anywhere in the
launchpad.

`configs/bimbo.yaml` now pins `net_income` to NET MAJORITY INCOME (the figure
Bimbo reports in its own highlights table), and `tests/test_bimbo_net_income.py`
covers it without needing a PDF.

**The general case is still open.** Any metric whose label is a prefix or
substring of a different line in the same statement can be mismatched this way,
for any company without an explicit `metric_overrides` entry — which is most of
the 179-issuer universe. Two things would each have caught it:

1. Rejecting a positional match whose matched label carries qualifying words the
   target does not (`BEFORE TAXES`, `FROM CONTINUED OPERATIONS`, `ADJUSTED`).
2. Surfacing confidence in the extractor output the analyst reads, so a 0.6 among
   0.85s is visible rather than buried in a CSV column.

Neither is done. Until then, treat any extraction for an unconfigured company as
requiring the STRONG verification gate in `docs/VERIFICATION.md` before use, and
prefer companies that have a `metric_overrides` block.
