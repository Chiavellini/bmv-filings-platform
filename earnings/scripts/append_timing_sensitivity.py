#!/usr/bin/env python3
"""append_timing_sensitivity.py — v2-vs-v3 sensitivity table for the timing
correction, appended to outputs/TIMING_AUDIT.md.

Compares the certified v2 dev tables against the v3 rerun (same engine parity,
timing correction ON). Pre-committed framing (study.yaml audit_v3): if the
correction weakens the result, that IS the finding.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib.mdutil import md_table

import pandas as pd

V2 = bs.OUTPUTS_DIR / "results_v2"
V3 = bs.OUTPUTS_DIR / "results_v3"


def tercile_row(res_dir: Path, tag: str) -> dict:
    t = pd.read_csv(res_dir / f"{tag}_tercile_ar0_cc.csv")
    s = t[t["bucket"] == "T3-T1"].iloc[0]
    return {"spread_bps": round(s["mean"] * 1e4), "fm_bps": round(s["fm_mean"] * 1e4),
            "fm_t": round(s["fm_t"], 2), "fm_p": round(s["fm_p"], 4),
            "n": int(s["n"]), "n_quarters": int(s["n_quarters"])}


def strategy_rows(res_dir: Path) -> pd.DataFrame:
    p = res_dir / "strategy_sim.csv"
    return pd.read_csv(p) if p.exists() else pd.DataFrame()


def append_shuffle_section() -> None:
    """Append the persisted gate-3 permutation results (--shuffle-only).

    Completes the audit_v3 sensitivity scope ("shuffle p"): gate 3 used to be
    print-only; run_validation.py now persists it per era."""
    lines = [
        "",
        "### Within-quarter shuffle / placebo p (gate 3, persisted)",
        "",
        "v2: printed in-session during the certified run, not persisted; v3 "
        "below is the first persisted record (results_v3/"
        "validation_shuffle_placebo_{era}.csv), completing the audit_v3 "
        "sensitivity scope.",
        "",
    ]
    found = False
    for era in ("modern", "all"):
        p = V3 / f"validation_shuffle_placebo_{era}.csv"
        if not p.exists():
            continue
        found = True
        lines += [f"**v3, era={era}**", "", md_table(pd.read_csv(p)), ""]
    if not found:
        raise SystemExit("no validation_shuffle_placebo_*.csv in results_v3 — "
                         "run run_validation.py first")
    with open(bs.OUTPUTS_DIR / "TIMING_AUDIT.md", "a") as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines))


def main() -> None:
    if "--shuffle-only" in sys.argv:
        append_shuffle_section()
        return
    lines = [
        "",
        "## Sensitivity: certified v2 vs timing-corrected v3 (dev only)",
        "",
        "Same engine (parity-gated), same frozen prices/history; the only",
        "difference is the pre-committed timing correction (study.yaml",
        "`audit_v3.timing_rule`). Holdout and walk-forward were NOT rerun.",
        "",
        "### Modern-era tercile T3-T1 (AR0_cc)",
        "",
        "| sample | spread_bps | fm_bps | fm_t | fm_p | n | quarters |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, res_dir in (("v2 (certified)", V2), ("v3 (corrected timing)", V3)):
        r = tercile_row(res_dir, "dev_full_modern")
        lines.append(f"| {name} | {r['spread_bps']} | {r['fm_bps']} | {r['fm_t']} "
                     f"| {r['fm_p']} | {r['n']} | {r['n_quarters']} |")
    lines += ["", "### Full dev sample (all eras)", "",
              "| sample | spread_bps | fm_bps | fm_t | fm_p | n | quarters |",
              "|---|---|---|---|---|---|---|"]
    for name, res_dir in (("v2 (certified)", V2), ("v3 (corrected timing)", V3)):
        r = tercile_row(res_dir, "dev_full")
        lines.append(f"| {name} | {r['spread_bps']} | {r['fm_bps']} | {r['fm_t']} "
                     f"| {r['fm_p']} | {r['n']} | {r['n_quarters']} |")

    lines += ["", "### Strategy headline (liq5M, long s_ts>+1.0, and short leg)", ""]
    for name, res_dir in (("v2", V2), ("v3", V3)):
        s = strategy_rows(res_dir)
        if len(s):
            lines += [f"**{name}**", "", md_table(s), ""]

    lines += [
        "### What changed and why",
        "",
        "- 93 press-release recoveries with a timely XBRL reverted to the XBRL",
        "  timestamp (pre-announcement false matches — WALMEX -5d each quarter,",
        "  CEMEX -4..-7d); 33 stale-XBRL Q4 recoveries kept.",
        "- Inside 3Q24-2Q26, info_dt := min(XBRL filed_dt, calendar-implied",
        "  timestamp); the human calendar recovers Q4 announcements whose",
        "  XBRL-only date was stale, growing the modern dev sample.",
        "- Historical (pdf_header) rows untouched.",
        "",
    ]
    with open(bs.OUTPUTS_DIR / "TIMING_AUDIT.md", "a") as fh:
        fh.write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
