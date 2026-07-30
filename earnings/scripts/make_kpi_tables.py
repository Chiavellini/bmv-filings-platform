#!/usr/bin/env python3
"""make_kpi_tables.py — per-company markdown KPI tables from kpi_panel.parquet.

Pure rendering, zero statistics: quarters as rows, the three pillars as
columns, latest quarter annotated with its vs-history and vs-desk-estimate
verdicts. Output: outputs/kpi/<slug>.md + outputs/kpi/index.md
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import numpy as np
import pandas as pd

MIN_QUARTERS = 8


def fnum(v, nd=0, suffix=""):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return ""
    return f"{v:,.{nd}f}{suffix}"


def verdicts(r: pd.Series) -> list[str]:
    out = []
    yoy, base = r.get("yoy_rev_pct"), r.get("yoy_rev_trail8_mean")
    if np.isfinite(yoy) and np.isfinite(base):
        word = "above" if yoy > base else "below"
        out.append(f"Revenue growth {yoy:+.1f}% YoY, {word} its trailing-8Q "
                   f"mean of {base:+.1f}%")
    dm = r.get("ebitda_margin_yoy_pp")
    if np.isfinite(dm):
        word = "expanded" if dm > 0 else "compressed"
        out.append(f"EBITDA margin {word} {abs(dm):.1f}pp YoY "
                   f"to {r.get('ebitda_margin'):.1f}%")
    nd, chg = r.get("nd_to_ebitda_ttm"), r.get("net_debt_yoy_chg")
    if np.isfinite(nd):
        s = f"Net debt / EBITDA-TTM at {nd:.2f}x"
        if np.isfinite(chg):
            word = "up" if chg > 0 else "down"
            s += f", net debt {word} {fnum(abs(chg))} MM YoY"
        out.append(s)
    for met, label in (("revenue", "revenue"), ("ebitda", "EBITDA"),
                       ("net_income", "net income")):
        b = r.get(f"beat_{met}_pct")
        if b is not None and np.isfinite(b):
            word = "BEAT" if b > 0 else "MISS"
            out.append(f"{word} desk {label} estimate by {abs(b):.1f}% "
                       f"(vintage {r.get('est_vintage')})")
    return out


COLS = [
    ("period", "quarter", 0, ""),
    ("revenue", "revenue", 0, ""),
    ("yoy_rev_pct", "YoY%", 1, ""),
    ("yoy_rev_trail8_mean", "trail8 YoY%", 1, ""),
    ("rev_accel_pp", "accel pp", 1, ""),
    ("ebitda_margin", "EBITDA m%", 1, ""),
    ("ebitda_margin_yoy_pp", "Δm pp", 1, ""),
    ("net_margin", "net m%", 1, ""),
    ("net_debt", "net debt", 0, ""),
    ("nd_to_ebitda_ttm", "ND/EBITDA", 2, "x"),
    ("s_ts", "SUE (s_ts)", 2, ""),
    ("beat_revenue_pct", "vs est rev%", 1, ""),
    ("beat_ebitda_pct", "vs est EBITDA%", 1, ""),
]


def render(slug: str, sub: pd.DataFrame) -> str:
    sub = sub.sort_values("period")
    ticker = sub["ticker"].dropna().iloc[-1] if sub["ticker"].notna().any() else slug
    ccy = sub["currency"].dropna()
    ccy = ccy.iloc[-1] if len(ccy) else "MXN"
    lines = [f"# {ticker} ({slug}) — quarterly KPI panel",
             "",
             f"Values in millions of the reporting currency ({ccy}). "
             "Debt columns are XBRL-era only (2021-2T+). "
             "Estimates are the desk's own model vintages (point-in-time, "
             "available 2026-1T onward).",
             ""]
    cols = [(c, h, nd, sfx) for c, h, nd, sfx in COLS if c in sub.columns
            and (c == "period" or sub[c].notna().any())]
    lines.append("| " + " | ".join(h for _, h, _, _ in cols) + " |")
    lines.append("|" + "|".join("---:" if c != "period" else ":---"
                                for c, _, _, _ in cols) + "|")
    for _, r in sub.iterrows():
        cells = []
        for c, _, nd, sfx in cols:
            v = r[c]
            cells.append(str(v) if c == "period" else fnum(v, nd, sfx))
        lines.append("| " + " | ".join(cells) + " |")

    last = sub.iloc[-1]
    vs = verdicts(last)
    if vs:
        lines += ["", f"## Latest quarter ({last['period']})", ""]
        lines += [f"- {v}" for v in vs]
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    df = pd.read_parquet(bs.art_path("kpi_panel"))
    out_dir = bs.OUTPUTS_DIR / "kpi"
    out_dir.mkdir(exist_ok=True)
    index = []
    for slug, sub in df.groupby("slug"):
        if sub["revenue"].notna().sum() < MIN_QUARTERS:
            continue
        (out_dir / f"{slug}.md").write_text(render(slug, sub))
        last = sub.sort_values("period").iloc[-1]
        index.append({"slug": slug,
                      "ticker": last.get("ticker") or slug,
                      "last_quarter": last["period"],
                      "quarters": int(sub["revenue"].notna().sum()),
                      "has_estimates": bool(
                          sub.get("est_revenue", pd.Series(dtype=float))
                          .notna().any())})
    idx = pd.DataFrame(index).sort_values("slug")
    lines = ["# KPI panel index", "",
             "| company | ticker | last quarter | quarters | desk estimates |",
             "|:---|:---|:---|---:|:---|"]
    for _, r in idx.iterrows():
        lines.append(f"| [{r['slug']}]({r['slug']}.md) | {r['ticker']} | "
                     f"{r['last_quarter']} | {r['quarters']} | "
                     f"{'yes' if r['has_estimates'] else ''} |")
    (out_dir / "index.md").write_text("\n".join(lines) + "\n")
    print(f"kpi tables: {len(idx)} companies -> {out_dir}")


if __name__ == "__main__":
    main()
