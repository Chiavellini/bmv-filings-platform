# Alpha Go Estate portability — second-machine acceptance test

**Paste this entire document into a fresh Claude Code session on the other computer.** That
session has no memory of the work that produced it — everything it needs to act is below. If a
human is running this manually instead of an agent, every command is copy-pasteable as-is.

---

## 1. What this proves

Everything up to now (relocation copy, fresh clone) happened on the **same Mac** that built the
USB Estate — those results are simulated evidence, not proof. This is the first genuinely
independent test: a computer that has never seen this repository, running `git clone` plus the
same physical USB, with **no manual path surgery**.

**Objective:** `git clone <repo>` + plug in the USB Estate produces a working Alpha Go semantic
search system on this machine. If anything below only works because of something specific to the
original Mac (a cached file, an absolute path, an assumption about `/Users/<name>`), that is a bug
to report, not a result to paper over.

## 2. Non-negotiable safety rules

1. Never erase, format, repartition, force-unmount, or rename the USB.
2. Never delete or modify Estate document objects under `blobs/`.
3. Never inject test documents, fixture rows, or fake outbox events into the production Estate —
   this exact class of bug (leaked pytest fixtures reaching the real catalog) was found and fixed
   on 2026-08-07; do not reintroduce it. If you run any test suite, confirm first that Estate
   env vars (`PDFS_DOCUMENT_ESTATE`, `PDFS_ESTATE_BRIDGE`, `PDFS_REPORTS_DIR`,
   `PDFS_ESTATE_MOUNT_ROOT`, `PDFS_ESTATE_ID`) are **not** exported into the test shell, or that
   the suite's autouse fixture clears them.
4. Treat the catalog and existing indexes/checkpoints as read-only unless a step below explicitly
   says to write.
5. Do not run `sync --apply`, `process-estate-outbox run --apply`, or `reconcile-alpha --apply`
   during this test — those are real production writes to the shared Estate and are out of scope
   here. This test only reads.
6. Do not hardcode `/Volumes/<Estate>`, `/Volumes/<Vault>`, `/Users/<username>`, `/private/tmp`,
   or this machine's username into any tracked file. Discover the mount with `diskutil`/`df`.
7. Never commit `.env`, credentials, PDFs, models, indexes, caches, venvs, SQLite runtime DBs, or
   generated corpora.
8. Do not clone the repo under `~/Desktop` or `~/Documents` if this machine syncs those via
   iCloud/OneDrive — file eviction under sync causes long stalls unrelated to the actual test
   (see `docs/DEV_SETUP.md`).
9. Report every result honestly, including failures. A credential- or network-gated feature that
   fails should be reported as "fails, missing X" — never fabricated as passing.

## 3. Setup

Clone `main` (the merged, current state as of this test, at commit `575292ba` or later):

```bash
git clone https://github.com/Chiavellini/bmv-filings-platform.git
cd bmv-filings-platform
git log -1 --oneline    # confirm you're at 575292ba or a later main commit
```

Root environment:
```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
```

Alpha Go environment:
```bash
cd alpha-go
python3.12 -m venv .venv312
.venv312/bin/pip install -r requirements.txt
cd ..
```

Mount the USB Estate, find its actual root, then point `.env` at it:
```bash
diskutil list          # confirm the USB is attached and find its mount point
df -h | grep -i estate  # confirm the mount path
cp .env.example .env
```
Edit `.env` and set (using the real mount path from the commands above, and the estate ID from
`<mount>/bmv-estate-v1/.bmv-estate-volume.json`):
```
PDFS_ESTATE_MOUNT_ROOT=<actual mount path>
PDFS_DOCUMENT_ESTATE=<actual mount path>/bmv-estate-v1
PDFS_ESTATE_ID=<value from .bmv-estate-volume.json>
```
Then for every command below:
```bash
set -a; . ./.env; set +a
```

## 4. Acceptance protocol

Run each step in order. Record the actual output, not just pass/fail.

### 4.1 Estate connection gate
```bash
.venv/bin/python scripts/check_estate_connection.py --require-alpha-index --json
```
**Expect:** `"connected": true`, `"problems": []`, `"alpha_go_index_available": true`.

### 4.2 Alpha Go preflight
```bash
cd alpha-go
../.venv/bin/python scripts/preflight.py --config configs/alpha_go.yaml
cd ..
```
**Expect:** a line like `PREFLIGHT OK: <N> manifest docs, <M> indexed chunks`. As of this writing
the certified baseline is **3,695 manifest docs / 320,726 indexed chunks** — if the number is
higher, that's fine (it means real quarterly filings were added since this doc was written); if
it's lower, or the command fails, that's a real problem to report, not something to explain away.

### 4.3 Alpha projection hash-audit
```bash
.venv/bin/process-estate-outbox audit-alpha \
  --estate-root "$PDFS_DOCUMENT_ESTATE" \
  --alpha-root "$PDFS_ALPHA_ROOT" \
  --alpha-corpus "$PDFS_ALPHA_CORPUS" \
  --alpha-index "$PDFS_ALPHA_INDEX" \
  --alpha-config "$PDFS_ALPHA_CONFIG" \
  --alpha-target-id portable-estate-v2 \
  --alpha-python "$PDFS_ALPHA_PYTHON" \
  --json
```
(If `PDFS_ALPHA_*` are blank in your `.env`, set them explicitly: `PDFS_ALPHA_ROOT=$(pwd)/alpha-go`,
`PDFS_ALPHA_CORPUS=$PDFS_DOCUMENT_ESTATE/projections/alpha-go`,
`PDFS_ALPHA_INDEX=$PDFS_DOCUMENT_ESTATE/indexes/alpha_go.db`,
`PDFS_ALPHA_CONFIG=$(pwd)/alpha-go/configs/alpha_go.yaml`,
`PDFS_ALPHA_PYTHON=$(pwd)/alpha-go/.venv312/bin/python`.)

**Expect:** `"status": "healthy"`, `"healthy": true`, `missing_from_index: 0`,
`missing_from_manifest: 0`, `index_hash_mismatches: 0`.

### 4.4 Real, manually-verified search results

This is the step that actually proves the index is real, not merely non-empty. Run at least these
queries (Spanish + English, concept + exact-keyword, across ≥5 issuers):

```bash
cd alpha-go
../.venv312/bin/python scripts/search_demo.py --config configs/alpha_go.yaml --query "utilidad de operación" --company walmex
../.venv312/bin/python scripts/search_demo.py --config configs/alpha_go.yaml --query "impairment" --company femsa
../.venv312/bin/python scripts/search_demo.py --config configs/alpha_go.yaml --query "same-store sales" --company soriana
../.venv312/bin/python scripts/search_demo.py --config configs/alpha_go.yaml --query "deuda neta" --company bimbo
../.venv312/bin/python scripts/search_demo.py --config configs/alpha_go.yaml --query "operating margin" --company herdez
cd ..
```

For **each** query's top hits, record company/period/snippet, then independently open the actual
source file under `$PDFS_DOCUMENT_ESTATE/views/reports/<company>/<period>` and confirm the snippet
text genuinely appears in that document. A result that is merely non-empty is not sufficient
evidence — it must be traced back to real source text.

### 4.5 Dashboard

```bash
cd alpha-go
../.venv312/bin/python -m streamlit run app/streamlit_app.py --server.address 127.0.0.1
```
Walk the feature list: search, company/period filters, snippet + source-document navigation,
mention trends, summaries, financials, export. For any feature gated on a credential or network
resource that isn't available on this machine, report the exact failure and the exact missing
credential — do not fabricate a pass.

### 4.6 Fail-closed proofs

Missing mount:
```bash
PDFS_DOCUMENT_ESTATE=/tmp/not-a-real-estate-path .venv/bin/python scripts/check_estate_connection.py --json
```
**Expect:** `"connected": false"` with a clear `problems` entry — never a silent pass.

Wrong Estate ID:
```bash
.venv/bin/python scripts/check_portable_estate.py --estate-root "$PDFS_DOCUMENT_ESTATE" --estate-id "00000000-0000-0000-0000-000000000000" --json
```
**Expect:** a clean `estate ID mismatch` failure.

### 4.7 Full test suites (no-estate baseline, then estate-attached)

```bash
.venv/bin/python -m pytest -q -m "not network"
cd alpha-go && ../.venv312/bin/python -m pytest -q -m "not network" && cd ..
cd soft && .venv/bin/python -m pytest -q -m "not network and not model" && cd ..
cd earnings && .venv/bin/python -m pytest -q -m "not network and not model" && cd ..
```
Run once with `.env` **not** sourced (no-estate baseline — reads should skip cleanly, not fail),
then once with it sourced. Explain every count that differs from a clean pass.

## 5. Report back

This machine's Claude Code session has no way to reach the original conversation — write your
findings out explicitly and send them back to the user directly (chat, message, whatever channel
they're reachable on right now). Include, for each step in §4: the actual command run, the actual
output (not a paraphrase), and pass/fail. Do not merge anything or push without the user's
explicit go-ahead — `origin` is the shared repo, not a fork.
