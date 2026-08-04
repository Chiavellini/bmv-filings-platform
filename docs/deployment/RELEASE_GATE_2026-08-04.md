# Portable Estate release gate — 2026-08-04

## Scope

This gate certifies a source-only Git payload and a separately mounted BMV
document Estate. A fresh clone must not contain or recreate the document
corpus, models, indexes, analyst workbooks, caches, PDFs, secrets, or generated
datasets. Those runtime assets remain on the external Estate and are selected
with host-local environment variables.

The release branch is `codex/estate-portable-release`. The tracked
`estate.json` stays checkout-relative for an empty development estate; a host
that consumes the permanent Estate sets:

```text
PDFS_ESTATE_MOUNT_ROOT=/absolute/path/to/external/mount
PDFS_DOCUMENT_ESTATE=/absolute/path/to/external/mount/bmv-estate-v1
PDFS_ESTATE_ID=56EC1117-38A4-4F38-A135-408F04AF9A89
```

No source file assumes a particular macOS volume name. The mount root and
estate directory may differ on Linux or another Mac. The required estate ID is
stable across computers and prevents a same-named, incorrect drive from being
accepted.

## Source payload

`scripts/audit_git_payload.py --allow-dirty --no-history --json` passed for the
staged candidate. Generated/private inputs that had previously been tracked
were removed from Git tracking and added to ignore policy; local copies were
not deleted. The payload contains no estate, model, index, PDF, secret `.env`,
machine-specific temporary path, or dependency on a legacy volume.

The original worktree is recoverable from the retained pre-integration Git
stash. The internal Estate safety copy was not modified or deleted.

## Test matrix

| Scope | Command profile | Result |
|---|---|---|
| Root platform | `pytest -m 'not network and not model'` | 1,041 passed, 193 skipped, 4 deselected |
| Alpha Go | `pytest -m 'not network and not model'` | 486 passed, 1 skipped, 3 deselected |
| Soft | `pytest -m 'not network and not model'` | 326 passed, 2 skipped, 1 deselected |
| Earnings | default test suite | 117 passed, 2 skipped |
| Estate portability focus | portability, volume, bridge, and Airflow tests | 41 passed |

The root suite covers quarterly acquisition and duplicate prevention, company
source onboarding, analyst-controlled workbook metrics, restatement
precedence, formula/check behavior, single-current-deliverable enforcement,
root Estate consumers, Alpha synchronization, DAG schedules/import contracts,
and missing/wrong-ID mount failures. Alpha's disposable synchronization tests
exercise incremental add/replace/delete behavior without mutating the
production Estate.

## Fresh-clone certification

A generated snapshot was cloned into a new temporary directory with no ignored
files. Every module was installed from its documented dependency declaration,
not from the working checkout. The non-quick clean-clone run passed:

| Module | Fresh-clone result |
|---|---|
| Root platform | 1,038 passed, 197 skipped, 4 deselected |
| Alpha Go | 483 passed, 3 skipped, 3 deselected |
| Soft | 326 passed, 2 skipped, 1 deselected |
| Earnings | 116 passed, 3 skipped |

The fresh clone also installed native Airflow 3.3 using the documented
installer. `pip check` found no broken requirements, metadata migration passed,
the one-slot `estate_writer` pool was created, all four DAGs appeared in the
local DAG list, and `dags list-import-errors --local` reported no data. The
generated untracked environment file had mode `0600`.

With the external Estate attached, the same clone rejected a missing mount and
rejected the real drive when supplied a deliberately wrong estate ID. It then
passed the correct-ID preflight and read report content through SQLite
`mode=ro`. Representative source-onboarding and extraction commands wrote only
inside the disposable clone.

## Alpha Go runtime

The certified multilingual embedding model and 3,695-document portable corpus
projection live under the Estate's `models/` and `projections/` directories.
They are not Git payload. The projection expands to 320,726 semantic chunks.

The full semantic index is deliberately deferred from this source release. A
resumable local build reached 28,160 embeddings, but no partial build is
certified or described as Alpha-ready. Before Alpha is enabled, the completed
index must pass SQLite quick check, agree with the projection document/chunk
counts, and return real results from several companies.

The production catalog-to-Alpha reconciliation is idempotent after the full
build. Incremental ingestion is separately tested against disposable fixture
directories, never by injecting fixture rows or files into the permanent
Estate.

## Destination-computer activation

On the second computer:

1. Clone and check out the release branch.
2. Create each documented Python environment and install dependencies.
3. Mount the external drive and set the three Estate variables above using the
   destination computer's actual mount path.
4. Run `scripts/check_estate_connection.py --json` before extraction,
   onboarding, or workbook consumers.
5. When Alpha work resumes, complete the semantic index, then run the same
   command with `--require-alpha-index` and perform real search acceptance.
   Keep Alpha's source root, config, and interpreter in the fresh clone while
   its corpus, model, and final index remain on the mounted Estate.
6. Install/configure native Airflow, run the DAG import check, and leave the
   applied sync/Alpha gates disabled until the documented one-company canary
   succeeds twice and the alert webhook is configured.

The Git release does not activate unattended acquisition on a new host. Host
service installation, credentials, alerting, and the applied canary are
deliberate destination-computer operations.
