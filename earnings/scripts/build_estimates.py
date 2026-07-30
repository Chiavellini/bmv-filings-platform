#!/usr/bin/env python3
"""build_estimates.py — point-in-time desk-estimate series from model vintages.

Reads earnings/audit/model_vintage_inventory.csv (the committed sweep) and,
for every unique model snapshot, extracts the E-suffixed quarter columns for
revenue / ebitda / net_income. A value is a usable point-in-time estimate for
target quarter Q only if the vintage predates Q's report:
  * tag=post, vintage_period < Q   (model updated after an earlier quarter)
  * tag=pre,  vintage_period <= Q  (model frozen just before Q's report)
Per (slug, period, metric) the LATEST such vintage wins. Cells carrying an
'A' suffix in the source are actuals, never estimates — skipped.

Honest constraint (sweep result, 2026-07-28): earliest vintage is BECLE
pre-1Q26; the series therefore starts at 2026-1T (one name) / 2026-2T
(~13 names) and accumulates forward. It cannot support a historical backtest.

Output: outputs/estimates_pit.parquet
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import numpy as np
import pandas as pd

QCOL = re.compile(r"^([1-4])Q(\d{2})([AE])$")

# row-label patterns per metric, first match wins (checked top to bottom)
ROW_PATTERNS = {
    "revenue": [r"^Revenues?$", r"^Total sales$", r"^Total revenues?$",
                r"^Net sales$", r"^Ventas totales$"],
    "ebitda": [r"^EBITDA$", r"^EBITDA \(calculated\)$"],
    "net_income": [r"^Majority Net Profit$", r"^Net income$",
                   r"^Net profit$", r"^Majority net income$",
                   r"^Utilidad neta( mayoritaria)?$"],
}


def _match_metric(label: str) -> str | None:
    for metric, pats in ROW_PATTERNS.items():
        for p in pats:
            if re.match(p, label.strip(), re.IGNORECASE):
                return metric
    return None


def _grid_estimates(grid: list[list], src: str) -> list[dict]:
    """Common extractor over a 2D value grid (xlsx sheet or csv)."""
    header_i, header = None, None
    for i, row in enumerate(grid):
        qhits = [c for c in row if isinstance(c, str) and QCOL.match(c.strip())]
        if len(qhits) >= 8:
            header_i, header = i, row
            break
    if header is None:
        return []
    cols = {}
    for j, c in enumerate(header):
        m = QCOL.match(c.strip()) if isinstance(c, str) else None
        if m:
            cols[j] = (f"20{m.group(2)}-{m.group(1)}T", m.group(3))

    out, seen = [], set()
    for row in grid[header_i + 1:]:
        label = next((c for c in row[:4] if isinstance(c, str) and c.strip()), None)
        if label is None:
            continue
        metric = _match_metric(label)
        if metric is None or metric in seen:
            continue
        got_any = False
        for j, (period, flag) in cols.items():
            if flag != "E" or j >= len(row):
                continue
            v = row[j]
            if isinstance(v, str):
                v = v.replace(",", "").strip()
                if not re.match(r"^-?\d+(\.\d+)?$", v):
                    continue
                v = float(v)
            if isinstance(v, (int, float)) and np.isfinite(v) and v != 0:
                out.append({"period": period, "metric": metric,
                            "est_value": float(v), "row_label": label.strip()})
                got_any = True
        if got_any:
            seen.add(metric)   # first matching row per metric wins
    return out


def extract_xlsx(path: Path) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for name in ("Financials", "Modelo", "Model"):
            if name in wb.sheetnames:
                grid = [list(r) for r in wb[name].iter_rows(values_only=True)]
                rows = _grid_estimates(grid, str(path))
                if rows:
                    return rows
        # fallback: first sheet with a quarter header
        for name in wb.sheetnames:
            grid = [list(r) for r in wb[name].iter_rows(values_only=True)]
            rows = _grid_estimates(grid, str(path))
            if rows:
                return rows
    finally:
        wb.close()
    return []


def extract_csv(path: Path) -> list[dict]:
    df = pd.read_csv(path, header=None, dtype=str, keep_default_na=False)
    return _grid_estimates(df.values.tolist(), str(path))


def slug_of(ticker: str, uni: dict) -> str | None:
    t = ticker.upper()
    for slug, v in uni.items():
        if v["ticker"].upper() == t or slug.upper() == t:
            return slug
    return {"BECLE": "becle", "ALSEA": "alsea", "LIVERPOOL": "liverpool",
            "CHDRAUI": "chedraui", "TBBB": "tbbb"}.get(t)


def main() -> None:
    inv = pd.read_csv(bs.EARNINGS_ROOT / "audit" / "model_vintage_inventory.csv")
    inv = inv.drop_duplicates("sha256")
    uni = bs.load_universe(bs.load_config(), "full")

    rows = []
    for _, r in inv.iterrows():
        path = Path(r["path"])
        if not path.exists():
            continue
        slug = slug_of(r["ticker"], uni)
        if slug is None:
            print(f"WARN unmapped ticker {r['ticker']} ({path.name})")
            continue
        try:
            ests = (extract_xlsx(path) if r["ext"] in (".xlsx", ".xlsm")
                    else extract_csv(path))
        except Exception as e:
            print(f"WARN {path.name}: extraction failed ({e})")
            continue
        for e in ests:
            # point-in-time rule
            vp, tag = r["vintage_period"], r["tag"]
            usable = (vp < e["period"]) if tag == "post" else (vp <= e["period"])
            if not usable:
                continue
            rows.append({"slug": slug, "ticker": r["ticker"].upper(),
                         **e, "vintage": r["vintage"],
                         "vintage_period": vp, "tag": tag,
                         "est_asof": r["mtime"], "source": path.name})

    df = pd.DataFrame(rows)
    if len(df):
        # latest usable vintage wins per (slug, period, metric)
        df = (df.sort_values(["vintage_period", "tag"])   # pre > post same period
                .drop_duplicates(["slug", "period", "metric"], keep="last")
                .sort_values(["slug", "period", "metric"])
                .reset_index(drop=True))
    df.to_parquet(bs.OUTPUTS_DIR / "estimates_pit.parquet", index=False)

    print(f"estimates_pit: {len(df)} rows, "
          f"{df['slug'].nunique() if len(df) else 0} slugs")
    if len(df):
        print("\nper (period, metric) coverage:")
        print(df.groupby(["period", "metric"]).size().unstack(fill_value=0)
              .head(12).to_string())
        near = df[df["period"] <= "2026-4T"]
        print("\n2026 estimates by slug/metric:")
        print(near.pivot_table(index="slug", columns=["period", "metric"],
                               values="est_value", aggfunc="first")
              .round(0).to_string())


if __name__ == "__main__":
    main()
