# Release gate — 2026-09-07

**This is the authoritative test baseline.** Four earlier documents recorded four
different root counts (846, 1,041, 1,043, 433), which made it impossible to tell a
regression from stale prose. Those are superseded; `docs/STRESS_TEST.md` is now
marked historical and points here.

Scope: a source-only Git payload with **no estate attached** and no
`data/reports/` corpus. Run on macOS (Darwin 25.0.0, arm64) at commit `efb291ef`.

## Interpreters

Each module has its own declared interpreter and its own virtual environment.
Substituting the machine's default `python3` is not supported — on this machine
that is 3.14.6, whose Homebrew build ships a broken `pyexpat` that silently
breaks `openpyxl` and therefore every Excel build.

| Module | Declared | Used | Source |
|---|---|---|---|
| Root | 3.13.9 | 3.13.9 | `/opt/homebrew/bin/python3.13` |
| Alpha Go | 3.12.13 | 3.12.13 | `uv python install 3.12.13` — was **absent** before this gate |
| Soft | 3.11.13 | 3.11.13 | `/opt/homebrew/bin/python3.11` |
| Earnings | 3.11.13 | 3.11.13 | `/opt/homebrew/bin/python3.11` |

Note that `python3.11` on this machine's `PATH` resolves to 3.11.16 (a `uv`
shim), not the certified 3.11.13. Both Soft and Earnings were run against the
Homebrew 3.11.13 explicitly. On another machine, check `python3.11 -V` before
assuming.

## Test matrix

All four suites with `-m "not network and not model"`, `.env` absent.

| Module | 2026-09-07 | After the 2026-09-08 functional pass | Fresh-clone baseline 2026-08-04 |
|---|---|---|---|
| Root | 1,093 passed, 197 skipped | **1,106 passed, 197 skipped, 4 deselected** | 1,038 passed, 197 skipped |
| Alpha Go | 489 passed, 3 skipped | **489 passed, 3 skipped, 3 deselected** | 483 passed, 3 skipped |
| Soft | 326 passed, 2 skipped | **328 passed, 2 skipped, 1 deselected** | 326 passed, 2 skipped |
| Earnings | 116 passed, 3 skipped | **116 passed, 3 skipped** | 116 passed, 3 skipped |

The 2026-09-08 increases are the regression tests that shipped with that pass's
fixes (root +13, soft +2). Skip counts are unchanged in all four modules, which
is the signal that matters: no test started skipping to make a failure go away.

Zero failures in any module. Skip counts match the previous fresh-clone gate
exactly in all four; the higher pass counts are tests added since, including the
eight added by this gate.

Compare against the *fresh-clone* column, not the estate-attached one. The
2026-08-04 estate-attached run recorded 117 passed / 2 skipped for Earnings; the
extra pass is a test that needs `earnings/data/**`, which a clone does not carry.

## Why the skips are the real signal

A green run on a machine with no corpus proves less than it appears to: tests
that read filings skip rather than fail, by design. So this gate records *why*
each test skipped. All 197 root skips are data absence — **none** are code
reasons, and there are no unconditional skips or xfails anywhere in the repo:

| Cause | Count |
|---|---:|
| Named report file absent under `data/reports/` | 122 |
| `filing corpus not checked out (data/reports/ is gitignored)` | 28 |
| Company corpus not present (grupo_mexico, others) | 15 |
| Sample PDF not on disk | 6 |
| `data/ground_truth/*-actual.csv` absent | 4 |
| Ground-truth / era-gate fixtures absent | 21 |
| `live estate absent` | 1 |

The contract in `tests/_corpus.py` holds: a skip says "this needs data I do not
have"; a failure would falsely say "this code is broken".

## Payload and runtime gates

| Gate | Result |
|---|---|
| `scripts/audit_git_payload.py` | **PASS** — 1,308 candidate files, 1,260 history blobs. No secret, DB, PDF, or oversized blob. One warning: a developer username in commit `6844cf10` (see below). |
| `scripts/deployment_preflight.py audit` | exit 0 — 19 checks, 2 findings, **0 blockers**. Both findings are expected with no estate attached: incomplete fleet source coverage, and no catalog file. |
| `scripts/check_download_runtime.py` | **exit 1, as designed** — see below. |

### The download-runtime finding is real, not noise

`pip install -e ".[test]"` — the install command the README and
`docs/ANALYST_CONSOLE.md` both give — installs neither the `browser` nor the
`llm` extra. Without `browser`, **nine issuers silently produce degraded
results**: `ac, becle, chedraui, herdez, kimber, kof, liverpool, regional`
(Playwright) and `soriana` (curl_cffi). The downloader degrades rather than
erroring, which is exactly why this script exists.

A machine that only *reads* an existing estate does not need it. A machine that
*acquires* documents does:

```bash
.venv/bin/python -m pip install -e ".[test,browser]"
.venv/bin/python -m playwright install chromium
.venv/bin/python scripts/check_download_runtime.py
```

### Accepted payload warning

Commit `6844cf10` contains a developer username written in flattened form
(`-Users-<name>-…`) inside a handoff document. The working tree is clean; only
history carries it. The leak-detection patterns were slash-anchored and could not
see this form, which is how it passed the gate originally — `flattened_user_path`
now catches it.

Removing it from history would rewrite every commit from `6844cf10` forward.
That cost was judged not worth paying for a developer's own username in their own
repository, so personal-path findings **in committed history** are reported as
warnings. Personal paths in the candidate, and secrets anywhere including
history, remain errors. Covered by three tests in `tests/test_audit_git_payload.py`.

## Clean-clone certification — the delivery proof

`scripts/certify_clean_clone.py --scope all`, certifying committed HEAD
`e59c18d3` from an isolated checkout. This is the gate that actually models the
handover: it clones HEAD into a temporary repository, refuses to proceed if any
external attachment is present (`data/document_estate`, `data/reports`, the
Alpha corpus/index) or if `estate.json` names an absolute `estate_root`, builds
each module from its **declared dependencies only**, and runs every suite in a
hermetic environment (reduced `PATH`, empty `HOME`, `PDFS_MATERIALIZE=0`,
`PDFS_DOCUMENT_ESTATE` and `PDFS_REPORTS_DIR` deliberately absent). A suite that
reports `passed == 0` fails the gate rather than passing vacuously.

```
==> Checking the all-module delivery contract
==> Proving no external document estate or legacy corpus is attached
    root platform  passed=1093 failed=0 skipped=197 errors=0 collection_errors=0
    alpha-go       passed=489  failed=0 skipped=3   errors=0 collection_errors=0
    soft           passed=326  failed=0 skipped=2   errors=0 collection_errors=0
    earnings       passed=116  failed=0 skipped=3   errors=0 collection_errors=0
ALL-MODULE CLEAN-CHECKOUT CERTIFICATION PASSED
```

Interpreters were passed explicitly, because `shutil.which` would not have found
a 3.12 at all on this machine before this gate, and `python3.11` on `PATH`
resolves to 3.11.16 rather than the certified 3.11.13:

```bash
.venv/bin/python scripts/certify_clean_clone.py --scope all \
  --python-root   "$(command -v python3.13)" \
  --python-alpha  "$HOME/.local/bin/python3.12" \
  --python-soft   /opt/homebrew/bin/python3.11 \
  --python-earnings /opt/homebrew/bin/python3.11
```

## Analyst Console — security boundary verified locally

Tested on a scratch port (8791) against a rewritten copy of the published static
assets. The production bridge on **8765 was running throughout and was not
touched**; port 8765 is baked into the published GitHub Pages CSP and cannot be
overridden at runtime, so local testing must rewrite the assets rather than
rebind the bridge (`docs/KNOWN_ISSUES.md` item 11).

| Behavior | Result |
|---|---|
| Degrades honestly with no estate | **PASS** — `"connected": false`, `"USB Estate desconectado"`, `"alpha_index": false`; reports the problem, attempts no fallback write |
| CORS on reads | **PASS** — allowed origin receives `Access-Control-Allow-Origin`; a hostile origin receives none, so the browser blocks the response |
| CSRF on writes | **PASS** — every POST requires `X-Analyst-Console: 1`, which forces a preflight and cannot be sent by a simple cross-site form. A hostile origin is rejected 403 even when it supplies the header. |
| Upload magic-byte check | **PASS** — a non-PDF body is rejected 400; a real `%PDF-` body is accepted 201 |
| Filename sanitization | **PASS** — `../../../etc/passwd.pdf` is stored as `passwd.pdf`; nothing escaped the upload directory |
| Period inference | **PASS** — `Reporte_2T25.pdf` → `2025-2T`, `Walmex 4T24 Resultados.pdf` → `2024-4T`, no-period filenames → `null` |
| Upload containment | **PASS** — writes land under `data/analyst_console/`, which is gitignored (`.gitignore:43`) |

**Not covered here, and still required on the destination Mac:** the
`install_analyst_console.py` install/uninstall round trip, launchd survival
across logout, the real GitHub Pages origin reaching the bridge on 8765, and
every estate-backed launchpad action (Alpha Go launch, workbook generation,
estate operations, USB eject). Those need the console installed and the estate
mounted, so they belong to the second-machine run.

## Analyst Console — full functional pass, 2026-09-08

The 2026-09-07 entry above recorded only the console's *security boundary*. Every
control has since been exercised against a live bridge on a scratch port, driving
the real launchpad in a browser. The production bridge on 8765 stayed up and was
never contacted. Six defects were found and fixed; five of them are invisible from
the test suite because they only appear when the UI is actually used.

### Extraction accuracy — the one that matters most

Extracting `Grupo Bimbo Reports 1Q26 Results__VFF.pdf` (a real filing) through the
console returned **6 of 6 metrics**, five of them exactly right. `net_income` was
**4,443 against a true 2,362 — an 88% overstatement.**

Bimbo's income statement carries three lines containing "NET INCOME": `NET INCOME
BEFORE TAXES` (4,443), `INCOME FROM CONTINUED OPERATIONS` (2,799), and `NET
MAJORITY INCOME` (2,362). The generic patterns require a number immediately after
the label, so they correctly declined all three; the looser positional `table` tier
then matched the first by label similarity. It shipped as `validated=True,
flagged=False` at confidence 0.6.

`configs/bimbo.yaml` now pins the metric to NET MAJORITY INCOME — the figure Bimbo
reports in its own highlights table. Re-run: `2,362 / 1,785` via `regex_table` at
confidence 0.85. All six values then matched the filing exactly.

**The generalisable risk remains:** the positional `table` tier can match a label
that merely *contains* the target, and nothing downstream flags it. Every company
without an explicit `metric_overrides` entry is exposed to this. Confidence 0.6 was
the only signal, and it is not surfaced to the analyst.

### Defects found and fixed

| # | Defect | Consequence before the fix |
|---|---|---|
| 1 | The launchpad was welded to port 8765 | Response header sets `connect-src 'self'`, the document meta pinned `8765`; browsers enforce the **intersection**, so on any other port every fetch was blocked. The console could not run if 8765 was taken, with no fallback. `LOCAL_API` now follows the serving origin. |
| 2 | `bootstrap()` re-parsed the 179-issuer registry on every call | 8.8 s cold, 0.7-1.5 s warm — and the UI polls it **every 1.8 s**. First load appeared to hang. Now mtime-cached and warmed on a startup thread: **0.002-0.036 s**. |
| 3 | Alpha Go reported `running: True` forever after a crash | The child is spawned with Popen and never waited on, so on exit it becomes a zombie — and `os.kill(pid, 0)` succeeds for zombies. `launch()` short-circuited on the stale pid, returned success, and the launchpad navigated the analyst away from itself to a dead `127.0.0.1:8501`. Only a bridge restart cleared it. `_pid()` now reaps. |
| 4 | An empty coverage matrix reported success | `soft_master` on a checkout without `soft/data/reports/` produced **157 companies and 0 of 3,925 cells populated**, exited 0, and the console showed a green "listo para abrir" and opened the blank file. Its own log already said "0 with data · filled 0 (0.0%)". It now fails before writing anything, so no blank artifact exists to open. |
| 5 | `soft_expand_apply` had no preconditions | It downloads filings into the estate but went straight to `jobs.create` with no estate check and no active-job check, unlike `estate_refresh_all`. Its only de-facto protection was an unrelated crash. Now gated by `prepare_estate_write`. |
| 6 | Two "review" buttons wrote to the estate | `soft_coverage_audit` and `soft_expand_plan` defaulted their reports into the estate root while registered `mutates=False` — so they took no writer lock and could write concurrently with `estate_refresh_all`, which holds it believing it is the only writer. Both now default under `outputs/`. |

Plus: `soft_expand_plan` and `soft_coverage_audit` crashed with raw
`FileNotFoundError` / `sqlite3.OperationalError` tracebacks on a clean checkout;
both now fail with an actionable Spanish message. And `jobs.py` listed
`soft_coverage_dense.html` as a `soft_master` artifact, which `build_master.py`
never writes.

### One more, found by the pass itself

Running the console left an **empty** `data/reports/ac/` behind (a failed
`segments_model` run creates the directory before failing). `tests/_corpus.py`
accepted `any(target.iterdir())`, so that single empty directory satisfied the
corpus gate and un-skipped 17 tests, which then failed for want of data they
never had: **17 failed, 1,102 passed, 180 skipped**. Removing the directory
restored **1,102 passed, 197 skipped, 0 failed**.

That inverts the contract the module documents -- a skip means "I do not have
this data", a failure means "this code is broken" -- and
`scripts/certify_clean_clone.py` treats any failure on a data-less clone as a
release defect, so a stray directory could fail a release certification.
`has_corpus()` now requires actual files.

### Verified working

Upload (magic bytes, 60 MB cap, filename sanitisation against traversal, period
inference from the real filename → `2026-1T`); metric catalog loading and company
switching (61 generic → 65 for bimbo); search filtering; selection counter with
correct pluralisation; both client-side submit guards, each sending **zero**
requests; the confirmation dialog rejecting all five near-miss phrase variants
(case, double space, leading/trailing space, truncation) and enabling only on an
exact match; both destructive operations refusing server-side with **zero jobs
created**; clean failure surfacing for every estate-dependent operation; job
polling, completion toasts and artifact auto-open; and both artifact-opening
actions.

### Known behaviour, not defects

- The launchpad's poll is throttled while the tab is in the background and catches
  up when it is fronted. Standard browser behaviour; it self-heals.
- With the estate unmounted **no control is disabled** — every estate button stays
  clickable and fails only via a red toast. Worth a product decision.
- If the browser blocks the popup, "Abrir Alpha" navigates the *launchpad tab
  itself* to Alpha Go, so the analyst loses the launchpad.

### Still unverified

Everything requiring the USB: `estate_check`/`estate_audit` success paths,
`segments_model` end to end, connect / disconnect+eject, and the two destructive
operations' success paths. Plus `install_analyst_console.py`, launchd survival, and
the real GitHub Pages origin — all of which belong to the second-machine run.

## What this gate does *not* prove

Stated plainly, because the previous handoffs were explicit that this claim has
been overstated before:

- **Nothing here is cross-machine evidence.** Everything ran on the Mac that
  built the estate. Per `ALPHA_ESTATE_CONTINUATION_HANDOFF.md` §7, the
  cross-machine claim remains **SIMULATED, not certified**, until
  `ALPHA_ESTATE_SECOND_MACHINE_TEST.md` is executed on the other computer.
- **No estate was attached.** The Alpha Go semantic index, the 3,695-document
  projection, the embedding model, and every estate-backed path are unverified by
  this gate. Alpha Go's own `model`-marked tests, which check the 384-dimension
  embedding contract, were deselected.
- **The Analyst Console is only half-tested.** Its security boundary and
  no-estate degradation are verified above; the installed launchd agent, the
  Pages-to-bridge round trip, and every estate-backed action are not. See
  `docs/KNOWN_ISSUES.md` item 11. It is the only entry point the analyst sees.
- **No `network`-marked test ran**, in any module.
