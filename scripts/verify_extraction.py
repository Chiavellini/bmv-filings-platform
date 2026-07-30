#!/usr/bin/env python3
"""
verify_extraction.py — drive the verification gate for a company.

Workflow (one loop iteration):
  1. ``verify_extraction.py <slug>``            → re-extract + print the strength
     scorecard and write the worklist (the cells a human/subagent must verify).
  2. spawn one verification subagent per worklist item (see docs/VERIFICATION.md),
     collect their verdicts into a JSON list of {period, key, value, note}.
  3. ``verify_extraction.py <slug> --apply verdicts.json`` → record the verified
     values into data/verified/<slug>.csv (reinforces the excel on next build).
     Verdict outcomes: confirmed = pin the value (green, no flag) · corrected =
     pin the fixed value · not disclosed = "value": null → BLANK · source
     unreadable/ambiguous = "value": "UNRESOLVED" (the extracted value ships
     with a red-flag comment and stops re-entering the worklist).
  4. repeat from (1) until the gate prints STRONG. Only UNRESOLVED cells carry
     a flag comment in the shipped workbook — every other suspect was either
     confirmed, corrected, or blanked.

The subagent fan-out in step 2 is orchestrated outside this script (it needs the
Agent tool); everything deterministic — scoring, worklist, override storage — lives
here so the manual work is normalized and never lost.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.shared.paths import PROJECT_ROOT  # noqa: E402

INPUTS = PROJECT_ROOT / "inputs"
VERIFIED_DIR = PROJECT_ROOT / "data" / "verified"


def _slug_to_input(slug: str) -> Path:
    p = INPUTS / f"{slug}.md"
    if not p.exists():
        sys.exit(f"No input file for slug {slug!r} (expected {p}).")
    return p


def cmd_run(slug: str) -> None:
    """Re-extract through build_segments (which runs the gate) and summarize."""
    from scripts.build_segments import run as build_run
    build_run(_slug_to_input(slug))
    wl = _worklist_path(slug)
    if wl.exists():
        items = json.loads(wl.read_text(encoding="utf-8"))
        if items:
            print(f"\nWorklist — {len(items)} cell(s) to verify (top 20):")
            for w in items[:20]:
                v = "—" if w["value"] is None else f"{w['value']:,.0f}"
                print(f"  [{w['period']:<8} {w['key']:<22}] {v:>14}  — {w['reason']}")
            if len(items) > 20:
                print(f"  … +{len(items) - 20} more in {wl.relative_to(PROJECT_ROOT)}")


def _worklist_path(slug: str):
    # mirrors build_segments dirname_for; for the registered companies slug==stem
    from scripts.build_segments import dirname_for, parse_input
    name = parse_input(_slug_to_input(slug).read_text(encoding="utf-8"))[0]
    return PROJECT_ROOT / "outputs" / dirname_for(name) / "validation" / f"{slug}_worklist.json"


def cmd_apply(slug: str, verdicts_path: str) -> None:
    """Merge subagent verdicts into data/verified/<slug>.csv (dedup by period,key)."""
    verdicts = json.loads(Path(verdicts_path).read_text(encoding="utf-8"))
    VERIFIED_DIR.mkdir(parents=True, exist_ok=True)
    out = VERIFIED_DIR / f"{slug}.csv"
    existing: dict[tuple[str, str], dict] = {}
    if out.exists():
        for r in csv.DictReader(out.open(encoding="utf-8")):
            existing[(r["period"], r["key"])] = r
    counts = {"pinned": 0, "blanked": 0, "unresolved": 0}
    for v in verdicts:
        key = (str(v["period"]).strip(), str(v["key"]).strip())
        val = v.get("value")
        if str(v.get("status", "")).strip().lower() == "unresolved" or \
                (isinstance(val, str) and val.strip().upper() == "UNRESOLVED"):
            # Verification attempted, source unreadable/ambiguous → keep the
            # extracted value; it ships with a red-flag comment.
            val = "UNRESOLVED"
            counts["unresolved"] += 1
        elif val in (None, "", "null"):
            # Not disclosed, but the extractor may hold a wrong value → blank it.
            val = "BLANK"
            counts["blanked"] += 1
        else:
            counts["pinned"] += 1
        existing[key] = {"period": key[0], "key": key[1],
                         "value": val, "note": v.get("note", "")}
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["period", "key", "value", "note"])
        w.writeheader()
        for r in sorted(existing.values(), key=lambda r: (r["key"], r["period"])):
            w.writerow(r)
    applied = sum(counts.values())
    try:
        shown = out.relative_to(PROJECT_ROOT)
    except ValueError:
        shown = out
    print(f"Applied {applied} verdict(s) → {shown} "
          f"({counts['pinned']} confirmed/corrected · {counts['blanked']} blanked · "
          f"{counts['unresolved']} unresolved; {len(existing)} total). "
          f"Re-run `verify_extraction.py {slug}` to rebuild.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Verification gate driver.")
    ap.add_argument("slug", help="company slug (matches inputs/<slug>.md and configs/<slug>.yaml)")
    ap.add_argument("--apply", metavar="VERDICTS_JSON",
                    help="merge subagent verdicts into data/verified/<slug>.csv (no rebuild)")
    args = ap.parse_args()
    if args.apply:
        cmd_apply(args.slug, args.apply)
    else:
        cmd_run(args.slug)


if __name__ == "__main__":
    main()
