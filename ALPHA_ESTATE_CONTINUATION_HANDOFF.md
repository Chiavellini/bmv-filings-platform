# Alpha Go Estate portability — continuation handoff

**Written:** 2026-08-05 · **Branch:** `claude/alpha-estate-portability` off `main` @ `bfb0d762`
**Supersedes nothing.** Read alongside `ALPHA_ESTATE_AGENT_HANDOFF.md`, which remains authoritative
for safety rules and the required final-report format.

---

## 1. THE OBJECTIVE — read this twice

Make **`git clone <repo>` + plug in the USB Estate** produce a working Alpha Go semantic search
system **on a computer that has never seen this project**, with no manual path surgery.

That is the entire goal. Every task below exists only to serve it.

The user's own words, which override any narrower reading of the original handoff:

> *"We are right now testing the code on this mac; like we are on another computer: The whole point
> of this run is to precisely make any changes to any of the files / the codebase in general for
> another machine's implementation with the usb and the repo is seamless."*

So: **you are role-playing a fresh machine.** When something works only because this is the Mac
that built the Estate, that is a bug to fix, not a result to report.

### What "done" means
1. The three portability defects stay fixed and covered by tests. ✅ *already achieved*
2. A complete semantic index sits at the production path on the Estate. ⏳ *built + certified 10/10; needs copying to the Estate*
3. Real searches return correct, manually verified results across multiple issuers. ⬜
4. The system works from a **different filesystem root** and a **fresh clone**. ⬜
5. Missing mount / wrong Estate ID **fail closed**. ⬜
6. Work is committed locally. **STOP before any push.** ⬜

---

## 2. Non-negotiable safety rules (still in force)

1. Never erase, format, repartition, force-unmount, or rename the USB.
2. Never delete or modify Estate document objects under `blobs/`.
3. Never inject test documents, fixture rows, or fake outbox events into the production Estate.
4. Treat the catalog and existing Alpha indexes/checkpoints as read-only during diagnosis.
5. Before an authorized production index/catalog write, back up the specific SQLite file. **Never
   delete the prior file** — retain it as rollback evidence.
6. Create/replace Alpha artifacts only through documented atomic/checkpointed workflows.
7. Do not touch any directory or repository named `ear`.
8. Do not assume the original Mac's internal safety copy exists.
9. **Do not hardcode** `/Volumes/<Estate>`, `/Volumes/<Vault>`, `/Users/<username>`, `/private/tmp`,
   or this username in tracked files. Discover mounts with `diskutil` / `df`.
10. Keep `estate.json` relative and tracked. Absolute machine paths go only in ignored `.env`.
11. Never commit `.env`, credentials, PDFs, models, indexes, caches, venvs, SQLite runtime DBs,
    generated corpora, or datasets.
12. Root uses `.venv` (Python 3.13.9); Alpha uses `alpha-go/.venv312` (Python 3.12.13). Only declared
    dependencies; never global `pip`.
13. Keep Hugging Face/Transformers offline (`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`). The certified
    model travels on the Estate — never silently download or substitute one.
14. Before any job over ten minutes: print the exact command, expected duration, output/checkpoint
    path, free-space requirement, and the exact resume command. Report progress. Do not restart a
    multi-hour build from zero without diagnosing why.
15. Do not enable unattended Alpha/Airflow writes until the index is certified, two canaries pass,
    and alerting is configured.

Also: **do not** run `git push origin backup/pre-merge-alpha-go-import:alpha-go-import`. Keep local
branch `backup/pre-merge-alpha-go-import` at `8b697899` as read-only recovery evidence.

**`origin` is the shared `Chiavellini/bmv-filings-platform`, not a fork.** Pushing publishes directly.

---

## 3. What is already done (verified, not assumed)

### Three defects found and fixed

| # | Defect | Fix | Proof |
|---|---|---|---|
| 1 | Projection manifest stored **absolute** paths (3,695/3,695 rooted at `/Volumes/<Estate>/bmv-estate-v1`) — the bundle only worked on a mount with that exact name | `alpha-go/src/corpus/manifest.py` stores all four path fields relative to the estate root, resolves against the *current* root on load, and rebinds legacy absolute paths by longest-existing-tail | Live manifest now **0/3,695 absolute**, still **3,695/3,695 resolving**. 5 tests in `alpha-go/tests/test_corpus_manifest_portability.py` |
| 2 | `validate(require_alpha_index=True)` was an **`is_file()` existence check** — a 10-document index against a 3,695-document projection passed the release gate | New `estate_portability.inspect_alpha_index()` — one shared definition of "complete" used by both the root gate and Alpha preflight | Gate went from **exit 0 / `problems: []`** to **exit 1** naming three reasons. 11 tests in `tests/test_alpha_index_completeness.py` |
| 3 | Preflight **skipped** the dimension check when `embedding_dim` metadata was absent — an index with no model provenance was treated as compatible with anything | `alpha-go/scripts/preflight.py` delegates to the same `inspect_alpha_index`; missing metadata is now a failure | Preflight fails on missing `embedding_dim` |

The bug was **enshrined in the test suite**: `tests/test_estate_bridge.py` did `.touch()` on a fake
index then asserted the gate passed. That fixture now builds a real index via `tests/_alpha_index.py`.

### Semantic index — BUILT, **certified 10/10**, not yet promoted

```
/private/tmp/claude-501/-Users-bernardodelrio-Code-bmv-filings-platform/\
2449713e-1bfb-4456-a419-aee334f587ad/scratchpad/build/alpha_go_semantic.db
```
1,745,862,656 bytes · finished 2026-08-04 17:42

| Check | Value |
|---|---|
| `PRAGMA quick_check` | `ok` |
| documents | **3,695** (== manifest) |
| chunks / chunks_fts / embeddings | **320,726 / 320,726 / 320,726** |
| distinct embedding dims | **1** (384) |
| `embedding_model` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` |
| `embedding_dim` | `384` |
| `reembed_source_signature` | `cf495d33…5ce0e245` |
| foreign-key violations | **0** |
| distinct issuers | **97** (was 1) |

`scratchpad/promotion_checks.py` — every promotion requirement from the original handoff, as one
read-only gate — returned **10/10 PASS, exit 0**, including "shared release gate reports no
problems". The index is certified; it just has not been copied to the Estate yet.

### Tests currently green
- `tests/test_alpha_index_completeness.py` + `tests/test_estate_bridge.py` → **23 passed**
- `alpha-go/tests/test_corpus_manifest_portability.py` → **5 passed**

Green both **with and without** `.env` exported (see gotcha #4).

### Docs updated
- `docs/KNOWN_ISSUES.md` §8 → RESOLVED; §9 → superseded, plus a new subsection on the I/O bottleneck
- `docs/ALPHA_GO_ESTATE_SYNC.md` → new "Index generations" section: **`portable-estate-v1` →
  `portable-estate-v2`** must be set in `deploy/airflow/native/.env` before enabling Alpha automation

### Uncommitted work
```
 M alpha-go/scripts/preflight.py            |  33 +-
 M alpha-go/scripts/sync_shared_estate.py   |   8 +-
 M alpha-go/src/corpus/manifest.py          | 126 +++-
 M docs/ALPHA_GO_ESTATE_SYNC.md             |  13 +
 M docs/KNOWN_ISSUES.md                     |  72 +-
 M estate.json                              |   3 +
 M estate_bridge.py                         |  39 +-
 M estate_portability.py                    | 108 +++
 M tests/test_estate_bridge.py              |  28 +-
   9 files changed, 385 insertions(+), 45 deletions(-)
?? ALPHA_ESTATE_AGENT_HANDOFF.md
?? alpha-go/tests/test_corpus_manifest_portability.py
?? tests/_alpha_index.py
?? tests/test_alpha_index_completeness.py
```

---

## 4. Current Estate layout

`/Volumes/<Estate>/bmv-estate-v1/indexes/` — 190 GiB free

| File | Meaning |
|---|---|
| `alpha_go.db` (9,682,944 B, mtime Aug 4 08:40) | **The original 10-document index. NEVER MODIFIED through this entire run.** This is what gets replaced. |
| `alpha_go_source.db` (1.07 GB) | Salvaged chunked source — 3,695 docs, 320,726 chunks, 0 FK violations. Input to the re-embed. |
| `alpha_go_semantic.db.building` (1.08 GB) | **ABANDONED USB attempt (~2,560 embeddings).** Rename it so it is never mistaken for a resume checkpoint. Do not delete. |
| `.alpha_go.db.building-13983` (73,728 B) | Empty staging DB, 0 rows — evidence |
| `.alpha_go.db.projection.lock` | Stale lock — evidence |
| `diagnostic-evidence-2026-08-04/` | Preserved copies of the above |

`projections/alpha-go/`
- `manifest.json` (5,517,450 B, Aug 4 13:25) — **portable**, 0/3,695 absolute
- `manifest.json.pre-portable-2026-08-04` (5,794,890 B) — rollback evidence, keep

---

## 5. YOUR TASKS

### Phase 4 (finish) — promote the index

**The gate has already passed 10/10** (log: `scratchpad/build/promotion.log`). Re-run it if you want
independent confirmation — it is read-only and takes ~3 minutes (`PRAGMA quick_check` +
`foreign_key_check` on a 1.75 GB / 320k-row DB):

```bash
cd ~/Code/bmv-filings-platform
set -a; . ./.env; set +a
S=/private/tmp/claude-501/-Users-bernardodelrio-Code-bmv-filings-platform/2449713e-1bfb-4456-a419-aee334f587ad/scratchpad
./.venv/bin/python "$S/promotion_checks.py" "$S/build/alpha_go_semantic.db" --expected-dim 384
```

Then promote — this is the **first authorized production write of the entire run**:
1. Copy the index to the Estate (~1.75 GB over USB — expect minutes, print the ETA per rule 14).
2. Promote to `indexes/alpha_go.db`, **retaining the current file as `alpha_go.db.previous`**
   (rule 5 — never delete the prior file).
3. Rename the abandoned `alpha_go_semantic.db.building` so it cannot be read as a checkpoint.

### Phase 5–6 — real acceptance

- `alpha-go/scripts/preflight.py` against **production paths**
- Root `--require-alpha-index` gate → must now pass for the right reasons
- `audit-alpha` (read-only health gate)
- **Real searches** via `alpha-go/scripts/search_demo.py` across **≥5 issuers** — available doc
  counts: walmex 70, bimbo 145, herdez 77, soriana 73, femsa 50. Spanish **and** English concept
  queries, exact-keyword queries, company filters. **Manually verify company / period / snippet /
  source for each — not merely that results are non-empty.** This is the step that proves the
  semantic index is real rather than plausible.
- Streamlit dashboard on loopback — verify the feature list
- Idempotent incremental ingestion **on disposable copied fixtures only** — never the production Estate
- Airflow DAG import with zero errors. Requires installing `apache-airflow==3.3.0` from
  `requirements/airflow-native.txt` into a new `.airflow-venv` (**does not exist yet**; network
  confirmed reachable). Applied gates stay **disabled**.

### Phase 7 — relocation and fresh-clone proof

The rigorous part, and the one that actually tests the objective.

- **Relocation:** `mount_apfs -o ro` for a second mountpoint fails with *"Resource busy"*, and
  symlinked roots are rejected by design (`KNOWN_ISSUES` §7b). **The plan is a real-directory copy
  at a different root on the Estate itself** (~2.7 GB, skipping the 32 GB blob store; 190 GiB free).
  Then run full preflight + real searches with `PDFS_DOCUMENT_ESTATE` pointed there, plus
  `check_estate_connection.py --verify-relocated`.
- **Fresh clone** into a temp dir with a fresh venv, `.env` written from `.env.example` only, and
  **no reliance on `~/estate-staging`**.
- **Prove fail-closed:** missing mount, wrong Estate ID.
- **Full suites vs baselines:** root 1,043/193/4, Alpha 486/1/3, Soft 326/2/1, Earnings 117/2.
  Explain every deviation.
- **Git payload audit:** secrets, generated assets, absolute machine paths, `/Volumes/<Vault>`.

### Phase 8 — delivery

Commit locally: source, tests, docs only. **STOP and show the diff before any push.** No merge.

### Final report
The original handoff's **12 numbered sections** are required. Cross-machine claims must be labelled
**SIMULATED, not certified** — see §7 below.

---

## 6. Gotchas — learned the hard way, will cost you hours

1. **Never hold a read connection on the index while a writer runs.** SQLite here is in
   rollback-journal mode, so a reader blocks the writer's commit. A progress probe that kept a read
   connection across a 30-second sleep killed the first re-embed with `database is locked`.
   **Read progress only from process stdout, never from the DB file.**

2. **The harness kills background tasks** (process-group cleanup). It killed a multi-hour build
   twice. **macOS has no `setsid(1)`** — use the shim at `scratchpad/detach.py`
   (`os.fork()` + `os.setsid()` + `os.execv`). After launching, confirm **PPID 1**.
   Also: **no `timeout(1)`** on macOS either.

3. **Building on the USB is I/O-bound, not CPU-bound.** 17 chunks/s on USB vs 132 idle benchmark vs
   51–66 on local SSD with `--batch-size 2048`. The tell is process state `U` (uninterruptible I/O
   wait) at single-digit CPU. **Build locally, copy the finished index to the Estate.**

4. **`PDFS_DOCUMENT_ESTATE` deliberately overrides `estate.json`.** A shell with `.env` exported —
   exactly what the deployment docs instruct — silently redirected every `tmp_path` test bundle at
   the real Estate. Fixed with autouse fixtures clearing **five** vars: `PDFS_DOCUMENT_ESTATE`,
   `PDFS_ESTATE_BRIDGE`, `PDFS_REPORTS_DIR`, `PDFS_ESTATE_MOUNT_ROOT`, `PDFS_ESTATE_ID`. Clearing
   only three makes it *worse*. This is a **pre-existing** leak, confirmed on the base commit.

5. **`Path.exists()` raises on real data.** News documents store their article URL in provenance path
   fields; joining one onto a root exceeds the filesystem limit and raises `ENAMETOOLONG` rather than
   returning False. All probes go through `manifest._exists()`.

6. **`git fetch --prune` before measuring anything about branches.** A stale remote-tracking ref led
   to a wrong "unrelated histories, 134 conflicting files" report. The real default branch is `main`.

7. **Local disk is at 96% — only 9.2 GiB free.** The Estate has 190 GiB. Plan large intermediates on
   the Estate, but *build* SQLite on local SSD (see #3). Clean up scratchpad build artifacts
   (~2.8 GB) when done.

---

## 7. What this run cannot prove

This is the machine that produced the Estate. Relocation and fresh-clone tests are strong evidence
that the **path dependency** is gone, but they do not exercise a different OS build, CPU, Python
install, or filesystem.

**Report Phase 7's cross-machine claim as SIMULATED, not certified.**

Anything credential- or network-gated must be reported as fail-closed-verified with the exact
credential named — **never fabricated as passing**.
