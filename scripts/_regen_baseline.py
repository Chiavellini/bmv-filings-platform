#!/usr/bin/env python3
"""Robustly regenerate tests/fixtures/scorecard_baseline.json.

Scores each company in an isolated subprocess with retries so a single workspace
I/O stall (TimeoutError reading PDFs) cannot abort the whole run. Merges the
per-company results into the baseline JSON only when every company succeeded.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS = ("obs_total", "obs_correct", "obs_miss", "obs_fail", "obs_excluded")


def score_one(company: str) -> dict | None:
    code = (
        "import json;from scripts.pipeline_scorecard import score_company;"
        f"s=score_company({company!r});"
        f"print('RESULT='+json.dumps({{k:s[k] for k in {KEYS!r}}}))"
    )
    for attempt in (1, 2, 3):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT, capture_output=True, text=True, env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"},
        )
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT="):
                return json.loads(line[len("RESULT="):])
        sys.stderr.write(f"[{company}] attempt {attempt} failed:\n{proc.stderr[-500:]}\n")
    return None


def main() -> None:
    from src.eval.compare_extractions import COMPANIES
    out: dict[str, dict] = {}
    for c in sorted(COMPANIES):
        # Skip companies whose ground truth isn't present (e.g. holdout-pdfs/
        # companies ac/becle are not checked out here). They can't be scored and
        # are excluded from the no-regression floor, matching test_no_regression.
        actual = ROOT / COMPANIES[c]["actual_file"]
        if not actual.exists():
            print(f"{c:12} SKIP (no GT: {COMPANIES[c]['actual_file']})")
            continue
        r = score_one(c)
        if r is None:
            sys.stderr.write(f"ABORT: {c} could not be scored after retries\n")
            sys.exit(1)
        out[c] = r
        print(f"{c:12} {r}")
    dest = ROOT / "tests" / "fixtures" / "scorecard_baseline.json"
    dest.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    print(f"WROTE {dest}")


if __name__ == "__main__":
    main()
