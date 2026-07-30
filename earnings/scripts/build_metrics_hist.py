#!/usr/bin/env python3
"""build_metrics_hist.py — pre-XBRL-era metrics from root CSVs + report markdowns.

Sources, in preference order per (slug, metric, period):
  1. root_csv  — the root pipeline's validated long series
                 (<checkout>/outputs/gruma_metrics.csv and
                  <checkout>/outputs/<Co>/csv/<slug>_metrics.csv)
  2. pdf_md    — fresh tiered extraction (alpha-go extract_metrics_tiered,
                 no-XBRL tiers) over data/reports/<slug>/*.md

Every candidate series is scale-harmonized and splice-checked against the
XBRL-era metrics.parquet on overlapping quarters: median ratio snapped to the
nearest power of 10, then the metric-source series survives only if the
post-rescale median relative difference is <= tolerance. Output keeps ONLY
pre-2021-2T rows (the XBRL era stays authoritative).

Output: outputs/metrics_hist.parquet + a splice-quality report.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import numpy as np
import pandas as pd
import yaml

METRICS = ["revenue", "operating_income", "depreciation", "net_income", "eps", "ebitda"]
XBRL_START = "2021-2T"
SPLICE_TOL = 0.05
POWERS = [0.001, 0.01, 0.1, 1.0, 10.0, 100.0, 1000.0]

# root slug -> soft slug (universe key); identity unless listed
SLUG_MAP = {"sport": "sports_world"}
PDF_COMPANIES = ["walmex", "kof", "bimbo", "liverpool", "chedraui", "kimber",
                 "lab", "lacomer", "sport", "gruma", "ac", "soriana", "herdez",
                 "becle", "grupo_mexico", "orbia"]

_PERIOD_PATS = [
    re.compile(r"^(20\d{2})-([1-4])T"),                # 2015-2T.md
    re.compile(r"^([1-4])Q(\d{2})(?=\D|$)", re.IGNORECASE),  # 1Q14_Results.md
    re.compile(r"_([1-4])Q(\d{2})", re.IGNORECASE),    # Walmex_Earnings_Release_3Q25
    re.compile(r"^([1-4])T(\d{2})\b", re.IGNORECASE),  # 2T15.md
]
_QEND = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}


def period_of(stem: str) -> str | None:
    m = _PERIOD_PATS[0].match(stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}T"
    for pat in _PERIOD_PATS[1:]:
        m = pat.search(stem)
        if m:
            return f"20{m.group(2)}-{m.group(1)}T"
    return None


def load_root_csvs() -> pd.DataFrame:
    rows = []
    paths = list(bs.LEGACY_OUTPUTS_DIR.glob("*/csv/*_metrics.csv"))
    lone = bs.LEGACY_OUTPUTS_DIR / "gruma_metrics.csv"
    if lone.exists():
        paths.append(lone)
    for p in paths:
        slug = p.stem.replace("_metrics", "")
        slug = SLUG_MAP.get(slug, slug)
        df = pd.read_csv(p)
        if "period" not in df.columns:
            continue
        for m in METRICS:
            if m not in df.columns:
                continue
            for _, r in df.iterrows():
                v = r[m]
                if pd.notna(v) and re.match(r"^20\d{2}-[1-4]T$", str(r["period"])):
                    rows.append({"slug": slug, "period": r["period"],
                                 "metric": m, "current": float(v),
                                 "source": "root_csv"})
    return pd.DataFrame(rows)


def extract_from_mds() -> pd.DataFrame:
    from src.excel.segments_sheet import load_metric_defs
    from src.extract.tiered_extract import PeriodSource, extract_metrics_tiered

    rows = []
    for root_slug in PDF_COMPANIES:
        rep_dir = bs.LEGACY_REPORTS_DIR / root_slug
        if not rep_dir.exists():
            continue
        cfg_path = bs.LEGACY_CONFIGS_DIR / f"{root_slug}.yaml"
        cfg = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else None
        defs = load_metric_defs(str(cfg_path) if cfg_path.exists() else None)
        defs = [d for d in defs if d.key in METRICS]
        slug = SLUG_MAP.get(root_slug, root_slug)

        seen = set()
        for md in sorted(rep_dir.glob("*.md")):
            period = period_of(md.stem)
            if period is None or period in seen:
                continue
            seen.add(period)
            pend = f"{period[:4]}-{_QEND[period[5]]}"
            try:
                text = md.read_text(errors="ignore")
            except OSError:
                continue
            src = PeriodSource(period=period, text=text, period_end=pend)
            try:
                found = extract_metrics_tiered(src, defs, cfg)
            except Exception as e:
                print(f"WARN {root_slug} {period}: extraction failed ({e})")
                continue
            for key, row in found.items():
                if key in METRICS and row.current is not None:
                    rows.append({"slug": slug, "period": period, "metric": key,
                                 "current": float(row.current), "source": "pdf_md"})
    return pd.DataFrame(rows)


def harmonize(hist: pd.DataFrame, xbrl: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Scale-snap each (slug, metric, source) series to the XBRL era and keep
    only series that agree on the overlap."""
    xkey = xbrl.set_index(["slug", "period", "metric"])["current"]
    kept, quality = [], []
    for (slug, metric, source), grp in hist.groupby(["slug", "metric", "source"]):
        overlap_ratios = []
        for _, r in grp.iterrows():
            xv = xkey.get((slug, r["period"], metric))
            if xv is not None and np.isfinite(xv) and xv != 0 and r["current"] != 0:
                overlap_ratios.append(xv / r["current"])
        if len(overlap_ratios) < 3:
            quality.append({"slug": slug, "metric": metric, "source": source,
                            "n_overlap": len(overlap_ratios), "scale": None,
                            "med_rel_diff": None, "kept": False,
                            "reason": "insufficient overlap"})
            continue
        med = float(np.median(overlap_ratios))
        scale = min(POWERS, key=lambda p: abs(np.log10(max(med, 1e-12) / p)))
        rel = [abs(rr / scale - 1.0) for rr in overlap_ratios]
        med_rel = float(np.median(rel))
        ok = med_rel <= SPLICE_TOL
        quality.append({"slug": slug, "metric": metric, "source": source,
                        "n_overlap": len(overlap_ratios), "scale": scale,
                        "med_rel_diff": round(med_rel, 4), "kept": ok,
                        "reason": "" if ok else "splice mismatch"})
        if ok:
            g = grp.copy()
            g["current"] = g["current"] * scale
            kept.append(g)
    return (pd.concat(kept, ignore_index=True) if kept else pd.DataFrame(),
            pd.DataFrame(quality))


def main() -> None:
    if bs.V3:
        raise SystemExit(
            "build_metrics_hist under EARNINGS_V3 would re-extract the "
            "historical era through the ROOT tiered engine — a deferred "
            "workstream with its own parity surface. v3 inherits "
            "metrics_hist_v2; run this script under EARNINGS_V2 instead.")
    xbrl = pd.read_parquet(bs.art_path("metrics"))

    csv_rows = load_root_csvs()
    print(f"root_csv rows: {len(csv_rows)} across "
          f"{csv_rows['slug'].nunique() if len(csv_rows) else 0} slugs")
    md_rows = extract_from_mds()
    print(f"pdf_md rows  : {len(md_rows)} across "
          f"{md_rows['slug'].nunique() if len(md_rows) else 0} slugs")

    hist = pd.concat([csv_rows, md_rows], ignore_index=True)
    kept, quality = harmonize(hist, xbrl)
    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    quality.to_csv(bs.RESULTS_DIR / "splice_quality.csv", index=False)

    if len(kept) == 0:
        raise SystemExit("nothing survived the splice check")

    # prefer root_csv over pdf_md per (slug, period, metric); pre-XBRL only
    kept["pref"] = (kept["source"] != "root_csv").astype(int)
    kept = (kept.sort_values("pref")
                .drop_duplicates(["slug", "period", "metric"], keep="first")
                .drop(columns=["pref"]))
    kept = kept[kept["period"] < XBRL_START].copy()

    # audit_v2 issue C: mechanical internal-consistency cleaning (rules and
    # constants pre-committed in study.yaml; drop, never rescale; every drop
    # logged). Applied to the final deduped pre-XBRL frame — exactly the rows
    # that feed SUE histories.
    cfg_full = bs.load_config()
    if "audit_v2" in cfg_full:
        from earnlib.quality import clean_metrics_hist
        kept, clean_log = clean_metrics_hist(
            kept, cfg_full["audit_v2"]["hist_cleaning"])
        clean_log.to_csv(bs.RESULTS_DIR / "hist_cleaning_log.csv", index=False)
        print(f"\nhist cleaning: dropped {len(clean_log)} rows "
              f"({clean_log['rule'].value_counts().to_dict()})")

    kept["period_end"] = kept["period"].str[:4] + "-" + kept["period"].str[5].map(_QEND)

    uni = bs.load_universe(cfg_full, "full")
    kept["ticker"] = kept["slug"].map({s: v["ticker"] for s, v in uni.items()})
    kept.to_parquet(bs.art_path("metrics_hist"), index=False)

    # ---- verify block ----
    print(f"\nkept series: {int(quality['kept'].sum())}/{len(quality)} "
          f"(median overlap agreement of kept: "
          f"{quality.loc[quality['kept'], 'med_rel_diff'].median():.3f})")
    print(f"pre-{XBRL_START} rows kept: {len(kept)} across {kept['slug'].nunique()} slugs")
    wide = kept.pivot_table(index=["slug", "period"], columns="metric",
                            values="current", aggfunc="first")
    per_slug = kept.groupby("slug")["period"].agg(["min", "max", "nunique"])
    print("\ncoverage per slug (pre-XBRL quarters):")
    print(per_slug.to_string())
    w = kept[(kept["slug"] == "walmex") & (kept["period"] == "2015-1T") &
             (kept["metric"] == "revenue")]
    print(f"\nspot-check walmex 2015-1T revenue (MXN MM): "
          f"{w['current'].iloc[0]:,.0f}" if len(w) else "\nspot-check walmex 2015-1T: MISSING")
    print("\nsplice failures:")
    print(quality[~quality['kept']].to_string(index=False))


if __name__ == "__main__":
    main()
