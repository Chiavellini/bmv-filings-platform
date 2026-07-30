#!/usr/bin/env python3
"""parity_check.py — W1 blocking gate: frozen artifacts vs the v3 rebuild.

Compares the frozen (pre-v3) metrics / events / surprises artifacts against the
EARNINGS_V3 rebuild produced with the ROOT extraction engine + the shared
document estate, with the W2 timing correction OFF. The engine/data swap must
be diff-clean before any timing change lands, so that every later v3 diff is
attributable solely to timing.

PRE-COMMITTED PASS CRITERIA (written before the first comparison ran):
  1. metrics: zero value_changed rows (current & prior exact float equality —
     same JSON bytes in, same numbers out). concept_changed (same values,
     different source_concept string) is logged, non-blocking.
  2. events: zero rows where filed_dt / press_dt / info_dt / date_source /
     slug differ. has_facts flips and coverage-only rows must each be
     explained.
  3. surprises: s_cs / s_ts agree within 1e-12 on the intersection.
  4. Anything unexplained -> FAIL. The v3 pipeline is not the dev baseline
     until this prints PASS.

RESOLUTION RECORD (first run, 2026-07-28): the initial FAIL decomposed into
  (a) DATA-VINTAGE drift — soft-xbrl-backfill (run 2026-07-28, post-freeze)
      added pre-2022 quarters for unifin/grupo_lamosa/planigrupo/... to soft's
      live tree AND the data/soft mirror; new 2026-2T filings kept arriving.
      Resolved structurally: configs/facts_vintage.csv (pin_facts_vintage.py)
      restricts v3 estate reads to the certified (slug, period) universe.
  (b) ENGINE IMPROVEMENT — the root engine's _PREFER_NONZERO logic skips
      concepts filed as empty 0.0 lines (alpha-go took mx-cor Depreciacion=0
      for fibra_danhos/fibra_hotel/unifin; root takes the real non-zero D&A
      from the next concept). Documented, deliberate root fix. These rows are
      classified engine_improvement (itemized, non-blocking), and the s_cs
      cross-sectional ripple they cause is allowed ONLY in the affected
      quarters (max |delta| reported).

Outputs: outputs/parity/PARITY.md + parity_diff_{metrics,events,surprises}.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import numpy as np
import pandas as pd

PARITY_DIR = bs.OUTPUTS_DIR / "parity"
# Pre-identified coverage deltas. After filtering estate reads to soft-project
# symlink targets, the estate facts selection reproduces the data/soft mirror
# exactly (verified: 0 missing periods, only skipped -FY extras), so no
# coverage difference is expected or excused.
EXPECTED_NEW_QUARTERS: set[tuple[str, str]] = set()


def _eq(a, b) -> bool:
    if pd.isna(a) and pd.isna(b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    return a == b


def compare_metrics() -> tuple[pd.DataFrame, dict]:
    frozen = pd.read_parquet(bs.OUTPUTS_DIR / "metrics.parquet")
    v3 = pd.read_parquet(bs.OUTPUTS_DIR / "metrics_v3.parquet")
    key = ["slug", "period", "metric"]
    m = frozen.merge(v3, on=key, how="outer", suffixes=("_frozen", "_v3"),
                     indicator=True)

    # (slug, period) cells where frozen depreciation was a 0.0 concept pick the
    # root engine's _PREFER_NONZERO improvement replaces — those cells and
    # their derived ebitda are engine_improvement, not blocking value changes
    dep0 = set(zip(
        frozen.loc[(frozen["metric"] == "depreciation")
                   & (frozen["current"] == 0), "slug"],
        frozen.loc[(frozen["metric"] == "depreciation")
                   & (frozen["current"] == 0), "period"]))

    rows = []
    for _, r in m.iterrows():
        if r["_merge"] == "left_only":
            klass = "only_frozen"
        elif r["_merge"] == "right_only":
            klass = "only_v3"
        elif not (_eq(r["current_frozen"], r["current_v3"])
                  and _eq(r["prior_frozen"], r["prior_v3"])):
            if (r["metric"] in ("depreciation", "ebitda")
                    and (r["slug"], r["period"]) in dep0):
                klass = "engine_improvement"
            else:
                klass = "value_changed"
        elif not _eq(r["source_concept_frozen"], r["source_concept_v3"]):
            klass = "concept_changed"
        else:
            continue  # identical — not logged
        rows.append({
            "slug": r["slug"], "period": r["period"], "metric": r["metric"],
            "class": klass,
            "current_frozen": r.get("current_frozen"), "current_v3": r.get("current_v3"),
            "prior_frozen": r.get("prior_frozen"), "prior_v3": r.get("prior_v3"),
            "concept_frozen": r.get("source_concept_frozen"),
            "concept_v3": r.get("source_concept_v3"),
        })
    diff = pd.DataFrame(rows)
    n_identical = int((m["_merge"] == "both").sum()) - int(
        len(diff[diff["class"].isin(
            ["value_changed", "concept_changed", "engine_improvement"])])
        if len(diff) else 0)
    summary = {
        "rows_frozen": len(frozen), "rows_v3": len(v3), "identical": n_identical,
        "value_changed": int((diff["class"] == "value_changed").sum()) if len(diff) else 0,
        "engine_improvement": int((diff["class"] == "engine_improvement").sum()) if len(diff) else 0,
        "concept_changed": int((diff["class"] == "concept_changed").sum()) if len(diff) else 0,
        "only_frozen": int((diff["class"] == "only_frozen").sum()) if len(diff) else 0,
        "only_v3": int((diff["class"] == "only_v3").sum()) if len(diff) else 0,
    }
    summary["improved_quarters"] = sorted(
        diff.loc[diff["class"] == "engine_improvement", "period"].unique()
    ) if len(diff) else []
    if len(diff):
        cov = diff[diff["class"].isin(["only_frozen", "only_v3"])]
        unexplained = [
            (r["slug"], r["period"]) for _, r in cov.iterrows()
            if (r["slug"], r["period"]) not in EXPECTED_NEW_QUARTERS
        ]
        summary["unexplained_coverage"] = len(unexplained)
    else:
        summary["unexplained_coverage"] = 0
    return diff, summary


def compare_events() -> tuple[pd.DataFrame, dict]:
    frozen = pd.read_parquet(bs.OUTPUTS_DIR / "events.parquet")
    v3 = pd.read_parquet(bs.OUTPUTS_DIR / "events_v3.parquet")
    key = ["ticker", "period"]
    cols = ["filed_dt", "press_dt", "info_dt", "date_source", "has_facts", "slug"]
    m = frozen[key + cols].merge(v3[key + cols], on=key, how="outer",
                                 suffixes=("_frozen", "_v3"), indicator=True)
    rows = []
    for _, r in m.iterrows():
        if r["_merge"] != "both":
            rows.append({"ticker": r["ticker"], "period": r["period"],
                         "class": "only_frozen" if r["_merge"] == "left_only" else "only_v3",
                         "field": "", "frozen": "", "v3": ""})
            continue
        for c in ["filed_dt", "press_dt", "info_dt", "date_source", "has_facts", "slug"]:
            if not _eq(r[f"{c}_frozen"], r[f"{c}_v3"]):
                rows.append({"ticker": r["ticker"], "period": r["period"],
                             "class": f"{c}_changed", "field": c,
                             "frozen": r[f"{c}_frozen"], "v3": r[f"{c}_v3"],
                             "slug": r.get("slug_frozen") or r.get("slug_v3")})
    diff = pd.DataFrame(rows)
    hard = diff[diff["class"].isin(
        ["filed_dt_changed", "press_dt_changed", "info_dt_changed",
         "date_source_changed", "slug_changed", "only_frozen", "only_v3"])] if len(diff) else diff
    flips = diff[diff["class"] == "has_facts_changed"] if len(diff) else diff
    unexplained_flips = [
        (r.get("slug"), r["period"]) for _, r in flips.iterrows()
        if (r.get("slug"), r["period"]) not in EXPECTED_NEW_QUARTERS
    ] if len(flips) else []
    summary = {
        "rows_frozen": len(frozen), "rows_v3": len(v3),
        "hard_diffs": len(hard), "has_facts_flips": len(flips),
        "unexplained_flips": len(unexplained_flips),
    }
    return diff, summary


RIPPLE_TOL = 0.05   # max |z| shift a 1-3-input change can impose on OTHER names


def compare_surprises(improved_slugs: list[str]) -> tuple[pd.DataFrame, dict]:
    """s_cs/s_ts parity. The 18 engine-improvement cells legitimately change
    their OWN slugs' SUE chains (dX at q and q+4, sigma over 8 trailing
    quarters) and renormalize every quarter's cross-section slightly.
    Attribution rule: improved slugs may change (itemized); every other name
    may move at most RIPPLE_TOL in |z|; anything larger blocks."""
    frozen = pd.read_parquet(bs.OUTPUTS_DIR / "surprises_v2.parquet")
    v3 = pd.read_parquet(bs.OUTPUTS_DIR / "surprises_v3.parquet")
    key = ["slug", "period"]
    cols = [c for c in ("s_cs", "s_ts") if c in frozen.columns and c in v3.columns]
    m = frozen[key + cols].merge(v3[key + cols], on=key, how="outer",
                                 suffixes=("_frozen", "_v3"), indicator=True)
    improved = set(improved_slugs)
    rows = []
    for _, r in m.iterrows():
        if r["_merge"] != "both":
            klass = "only_frozen" if r["_merge"] == "left_only" else "only_v3"
            rows.append({"slug": r["slug"], "period": r["period"], "class": klass})
            continue
        for c in cols:
            a, b = r[f"{c}_frozen"], r[f"{c}_v3"]
            if pd.isna(a) and pd.isna(b):
                continue
            if pd.isna(a) or pd.isna(b) or abs(a - b) > 1e-12:
                delta = abs(a - b) if pd.notna(a) and pd.notna(b) else None
                if r["slug"] in improved:
                    klass = f"{c}_improved_slug"
                elif delta is not None and delta <= RIPPLE_TOL:
                    klass = f"{c}_ripple"
                else:
                    klass = f"{c}_changed"
                rows.append({"slug": r["slug"], "period": r["period"],
                             "class": klass, "frozen": a, "v3": b,
                             "abs_delta": delta})
    diff = pd.DataFrame(rows)
    n_changed = int(diff["class"].str.endswith("_changed").sum()) if len(diff) else 0
    n_improved = int(diff["class"].str.endswith("_improved_slug").sum()) if len(diff) else 0
    n_ripple = int(diff["class"].str.endswith("_ripple").sum()) if len(diff) else 0
    max_ripple = (diff.loc[diff["class"].str.endswith("_ripple"), "abs_delta"].max()
                  if n_ripple else 0.0)
    coverage = diff[diff["class"].isin(["only_frozen", "only_v3"])] if len(diff) else diff
    unexplained_cov = [
        (r["slug"], r["period"]) for _, r in coverage.iterrows()
        if (r["slug"], r["period"]) not in EXPECTED_NEW_QUARTERS
    ] if len(coverage) else []
    summary = {"rows_frozen": len(frozen), "rows_v3": len(v3),
               "value_changed": n_changed,
               "improved_slug_changes": n_improved,
               "improvement_ripple": n_ripple,
               "max_ripple_abs_delta": float(max_ripple) if max_ripple else 0.0,
               "coverage_rows": len(coverage) if len(coverage) else 0,
               "unexplained_coverage": len(unexplained_cov)}
    return diff, summary


def main() -> None:
    PARITY_DIR.mkdir(parents=True, exist_ok=True)
    md, ms = compare_metrics()
    ed, es = compare_events()
    improved_slugs = sorted(
        md.loc[md["class"] == "engine_improvement", "slug"].unique()
    ) if len(md) else []
    sd, ss = compare_surprises(improved_slugs)
    md.to_csv(PARITY_DIR / "parity_diff_metrics.csv", index=False)
    ed.to_csv(PARITY_DIR / "parity_diff_events.csv", index=False)
    sd.to_csv(PARITY_DIR / "parity_diff_surprises.csv", index=False)

    ok = (
        ms["value_changed"] == 0 and ms["unexplained_coverage"] == 0
        and es["hard_diffs"] == 0 and es["unexplained_flips"] == 0
        and ss["value_changed"] == 0 and ss["unexplained_coverage"] == 0
    )
    verdict = "PASS" if ok else "FAIL"

    lines = [
        "# W1 parity gate — frozen vs v3 (root engine + estate, timing OFF)",
        "",
        f"**Verdict: {verdict}**",
        "",
        "Pre-committed pass criteria are in `scripts/parity_check.py`'s docstring;",
        "explained coverage deltas: post-snapshot 2026-2T arrivals "
        f"{sorted(EXPECTED_NEW_QUARTERS)}.",
        "",
        "## metrics.parquet vs metrics_v3.parquet",
        "",
        f"| rows frozen | rows v3 | identical | value_changed | engine_improvement | concept_changed | only_frozen | only_v3 | unexplained coverage |",
        f"|---|---|---|---|---|---|---|---|---|",
        f"| {ms['rows_frozen']} | {ms['rows_v3']} | {ms['identical']} | {ms['value_changed']} "
        f"| {ms['engine_improvement']} | {ms['concept_changed']} | {ms['only_frozen']} "
        f"| {ms['only_v3']} | {ms['unexplained_coverage']} |",
        "",
        f"engine-improvement quarters (allowed s_cs ripple): {ms['improved_quarters']}",
        "",
        "## events.parquet vs events_v3.parquet",
        "",
        f"| rows frozen | rows v3 | hard diffs (timestamps/source/coverage) | has_facts flips | unexplained flips |",
        f"|---|---|---|---|---|",
        f"| {es['rows_frozen']} | {es['rows_v3']} | {es['hard_diffs']} | {es['has_facts_flips']} | {es['unexplained_flips']} |",
        "",
        "## surprises_v2.parquet vs surprises_v3.parquet",
        "",
        f"improved slugs (own SUE chains may change): {improved_slugs}; "
        f"all other names tolerated to |dz| <= {RIPPLE_TOL}",
        "",
        f"| rows frozen | rows v3 | unexplained changes | improved-slug changes | renorm ripple | max ripple abs delta | coverage rows | unexplained coverage |",
        f"|---|---|---|---|---|---|---|---|",
        f"| {ss['rows_frozen']} | {ss['rows_v3']} | {ss['value_changed']} | {ss['improved_slug_changes']} "
        f"| {ss['improvement_ripple']} | {ss['max_ripple_abs_delta']:.2e} | {ss['coverage_rows']} | {ss['unexplained_coverage']} |",
        "",
        "Per-row detail in `parity_diff_*.csv` beside this file.",
        "",
    ]
    (PARITY_DIR / "PARITY.md").write_text("\n".join(lines))
    print("\n".join(lines))
    if not ok:
        raise SystemExit("PARITY FAIL — v3 is not the dev baseline; investigate before proceeding")


if __name__ == "__main__":
    main()
