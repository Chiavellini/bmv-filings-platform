#!/usr/bin/env python3
"""make_audit.py — outputs/AUDIT.md: the v2 verification sweep report.

Structured on the five-gate audit framework. Reads the ORIGINAL frozen
tables (outputs/results/) and the corrected ones (outputs/results_v2/) and
prints them side by side; never modifies either. Committed via the
gitignore negation (!outputs/AUDIT.md).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import numpy as np
import pandas as pd

R1 = bs.OUTPUTS_DIR / "results"
R2 = bs.OUTPUTS_DIR / "results_v2"


def _read(base: Path, name: str) -> pd.DataFrame | None:
    p = base / name
    return pd.read_csv(p) if p.exists() else None


def md(df: pd.DataFrame, nd: int = 2) -> str:
    """Markdown table without the tabulate dependency."""
    df = df.round(nd)
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        cells = ["" if (isinstance(v, float) and not np.isfinite(v)) or v is None
                 else str(v) for v in r]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def tercile_summary(base: Path, name: str) -> dict | None:
    df = _read(base, name)
    if df is None:
        return None
    row = df[df["bucket"] == "T3-T1"].iloc[0]
    return {"spread_bps": round(row["mean"] * 1e4), "fm_bps": round(row["fm_mean"] * 1e4),
            "fm_t": round(row["fm_t"], 2), "n": int(row["n"])}


ISSUES = """\
| # | issue | gate | severity | status |
|:--|:--|:--|:--|:--|
| A | `margin_sue` / `seasonal_qoq` / walk-forward SUEs computed without the `filed=` availability mask (core `s_cs` was always masked) | 1 lookahead | high (refinement candidates only) | FIXED — shared `availability_array`; corrected candidate table below |
| B | `combine_first` precedence inverted vs its "XBRL wins" comment | 1 data precedence | latent (0 colliding keys) | FIXED — `merge_hist_currents`, overlap printed & asserted by test |
| C | `metrics_hist` corruption: margin-%-as-level (GRUMA), EPS-as-net-income (KIMBER), identity violations (AC, BECLE), unit slips, missing-as-zero | 1 data integrity | high for historical era only | FIXED — 87 rows dropped by 6 pre-committed mechanical rules; full log in `results_v2/hist_cleaning_log.csv` |
| D | prices: 6,567 negative-adjclose rows (MINSAB, URBI); 20 single-bar symbols incl. TLEVISAB silently kept | 1 data integrity | medium | FIXED — dropped + logged in `results_v2/price_quality_dropped.csv` |
| E | liquidity-frontier zero-move share measured on the panel's last 250 days, attributed to all history | 4 descriptive honesty | low | FIXED — per-event pre-window (`zero_move_share_pre`) |
| F | strategy Sharpe annualized by trade-days shown next to calendar-basis B&H Sharpe | 4 metric comparability | low-medium | FIXED — both bases reported, labeled |
| G | modern sacred holdout evaluated twice (equal-weight proxy, then real ^MXX; spec unchanged) | 5 | protocol note | DOCUMENTED — holdout is read-twice evidence and is burned for refinement arbitration |
| H | `outputs/` unversioned; reruns silently overwrite the record | process | medium | FIXED — sha256 `MANIFEST.json`, regenerated each results-bearing commit |
| I | universe resolved from currently-existing facts + resolvable Yahoo symbols; delisted names absent | 1 survivorship | documented | OPEN — long-side levels likely overstated; cross-sectional T3−T1 less exposed; multi-year B&H equity curves inherit it fully |
"""


def main() -> None:
    lines = []
    add = lines.append
    add(f"# AUDIT v2 — BMV earnings event study verification sweep")
    add("")
    add(f"Date: {date.today().isoformat()}   repo HEAD: `{bs.repo_head()[:12]}`")
    add("")
    add("Scope: post-phase-G verification sweep, pre-committed in "
        "`study.yaml audit_v2` (frozen before any corrected number was seen). "
        "Original tables in `outputs/results/` are untouched; corrected "
        "tables live in `outputs/results_v2/`. The sacred holdout and the "
        "2026-2T walk-forward were NOT re-run (frozen decisions).")
    add("")
    add("## Issues and dispositions")
    add("")
    add(ISSUES)

    # ---- Gate 1 core comparison ----
    add("## Gate 1-4 — corrected vs original")
    add("")
    o = tercile_summary(R1, "dev_full_modern_tercile_ar0_cc.csv")
    c = tercile_summary(R2, "dev_full_modern_tercile_ar0_cc.csv")
    if o and c:
        add("### Core effect (modern dev, T3−T1 AR0_cc)")
        add("")
        add(md(pd.DataFrame([{"sample": "original", **o},
                             {"sample": "corrected (v2)", **c}])))
        add("")
        add("The incumbent core result is insensitive to every fix — the "
            "modern-era inputs were barely touched (price-quality drops only).")
        add("")
    oh = tercile_summary(R1, "dev_full_historical_tercile_ar0_cc.csv")
    ch = tercile_summary(R2, "dev_full_historical_tercile_ar0_cc.csv")
    if oh and ch:
        add("### Historical era (T3−T1 AR0_cc) — was and remains inconclusive")
        add("")
        add(md(pd.DataFrame([{"sample": "original", **oh},
                             {"sample": "corrected (v2)", **ch}])))
        add("")

    # ---- robustness ----
    orob, crob = _read(R1, "validation_robustness.csv"), _read(R2, "validation_robustness.csv")
    if orob is not None and crob is not None:
        rob = orob.merge(crob, on="variant", suffixes=("_orig", "_v2"))
        add("### Gate 4 robustness grid (FM spread bps / t)")
        add("")
        add(md(rob))
        add("")
        add("Note the tradable `s_ts` row: the corrected data trims it to "
            "t < 2 — the tradable margin is thinner than the frozen grid "
            "suggested. The single-metric SUEs remain the strongest variants.")
        add("")

    # ---- the headline: refinement candidates masked ----
    oref, cref = _read(R1, "refine_candidates.csv"), _read(R2, "refine_candidates.csv")
    if oref is not None and cref is not None:
        m = oref.merge(cref, on="signal", suffixes=("_orig", "_v2"))
        keep = ["signal"]
        for c_ in ("n", "fm_spread_bps", "fm_t", "ic", "shuffle_p"):
            for s in ("_orig", "_v2"):
                if c_ + s in m.columns:
                    keep.append(c_ + s)
        m = m[keep]
        if "fm_t_orig" in m.columns:
            m["fm_t_delta"] = (m["fm_t_v2"] - m["fm_t_orig"]).round(2)
        add("### Issue-A headline: refinement candidates, unmasked (original) "
            "vs availability-masked (v2)")
        add("")
        add(md(m))
        add("")
        add("**Finding:** closing the mask did NOT weaken the two strongest "
            "candidates — margin-SUE firms up (4.08→4.40) and seasonal-QoQ "
            "strengthens (3.76→4.43). The unmasked sigma windows rarely "
            "differed in practice in the modern era, so the lookahead "
            "exposure, while real in code, was not the driver of their "
            "in-sample strength. Margin-SUE's 2026-2T walk-forward failure "
            "therefore stands as ordinary in-sample overfit (or one bad "
            "draw), not a mechanical artifact. `confirmed_beat`'s drop "
            "(3.59→1.99) comes from the metrics-hist cleaning, not the mask.")
        add("")

    # ---- tradability decomposition ----
    trad = _read(R2, "dev_full_modern_tradability.csv")
    if trad is not None:
        add("## The tradability decomposition (corrected data)")
        add("")
        add("The statistically strongest component of the day-0 spread is the "
            "overnight gap, which cannot be captured for after-hours filings; "
            "the capturable open→close piece is materially weaker. Headline "
            "t-stats are not tradable t-stats.")
        add("")
        add(md(trad[trad["bucket"] == "T3-T1"]))
        add("")

    # ---- gate 5 note + input diff ----
    add("## Gate 5 — sacred holdout status")
    add("")
    s1p, s2p = bs.OUTPUTS_DIR / "surprises.parquet", bs.OUTPUTS_DIR / "surprises_v2.parquet"
    n_changed = None
    if s1p.exists() and s2p.exists():
        cfg = bs.load_config()
        hq = set(cfg["holdout"]["quarters"])
        s1 = pd.read_parquet(s1p)
        s2 = pd.read_parquet(s2p)
        a = s1[s1["period"].isin(hq)].set_index(["slug", "period"])["s_cs"]
        b = s2[s2["period"].isin(hq)].set_index(["slug", "period"])["s_cs"]
        j = pd.concat([a.rename("orig"), b.rename("v2")], axis=1)
        n_changed = int(((j["orig"] - j["v2"]).abs() > 1e-9).sum()
                        + j["orig"].isna().ne(j["v2"].isna()).sum())
    add(f"Evaluated twice pre-audit (proxy index, then real ^MXX; spec "
        f"unchanged) — treated as read-twice evidence. NOT re-run here. "
        f"Modern holdout-quarter `s_cs` scores that would differ under the "
        f"corrected inputs: **{n_changed}** (informational only; the holdout "
        f"verdict is what it was).")
    add("")

    # ---- data quality appendix ----
    add("## Data-quality appendix")
    add("")
    log = _read(R2, "hist_cleaning_log.csv")
    if log is not None:
        add(f"`metrics_hist` cleaning: {len(log)} of 1,346 rows dropped — "
            f"{log['rule'].value_counts().to_dict()}. Rules are frozen in "
            "`study.yaml audit_v2.hist_cleaning`; rescaling forbidden; "
            "known-legit stress case (SPORT 2020-2T COVID quarter) survives.")
        add("")
    pq = _read(R2, "price_quality_dropped.csv")
    if pq is not None:
        add(f"Price quality: {len(pq)} symbol-issues dropped, loudly:")
        add("")
        add(md(pq))
        add("")
    add("## Walk-forward 2026-2T (issue-A exposure, not re-run)")
    add("")
    add("The one-shot 2026-2T walk-forward's REFINED leg (margin-SUE) was "
        "computed with the unmasked sigma and is upward-uncertain; the "
        "incumbent leg used the correctly-masked pipeline and stands. Since "
        "the walk-forward's verdict was 'refined does NOT beat incumbent', "
        "closing the mask can only strengthen that verdict — re-running was "
        "neither needed nor allowed (one-shot discipline).")
    add("")
    add("## Desk-estimate reconstruction (honest scope)")
    add("")
    add("Machine-wide vintage sweep (`audit/model_vintage_inventory.csv`): "
        "earliest snapshot is BECLE pre-1Q26. Point-in-time desk estimates "
        "therefore exist from 2026-1T (one name) / 2026-2T (~13 names) and "
        "accumulate forward. The beat-vs-desk pillar CANNOT be backtested on "
        "the dev sample and enters phase H as a diagnostic only.")
    add("")

    out = bs.OUTPUTS_DIR / "AUDIT.md"
    out.write_text("\n".join(lines))
    print(f"wrote {out} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
