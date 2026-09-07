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

| Module | Result | Fresh-clone baseline 2026-08-04 |
|---|---|---|
| Root | **1,090 passed, 197 skipped, 4 deselected** | 1,038 passed, 197 skipped |
| Alpha Go | **489 passed, 3 skipped, 3 deselected** | 483 passed, 3 skipped |
| Soft | **326 passed, 2 skipped, 1 deselected** | 326 passed, 2 skipped |
| Earnings | **116 passed, 3 skipped** | 116 passed, 3 skipped |

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
