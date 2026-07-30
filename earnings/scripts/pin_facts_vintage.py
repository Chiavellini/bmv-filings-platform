#!/usr/bin/env python3
"""pin_facts_vintage.py — freeze the dev facts vintage from the FROZEN artifacts.

The soft XBRL tree keeps growing (soft-xbrl-backfill added pre-2022 quarters
for unifin, grupo_lamosa, planigrupo, ... on 2026-07-28, AFTER the certified
metrics build; new 2026-2T filings keep arriving). The dev sample is frozen, so
v3's estate reads must be restricted to the (slug, period) universe the
certified artifacts were built from:

  - every (slug, period) present in the frozen outputs/metrics.parquet
  - plus every (slug, period) with has_facts=True in the frozen events.parquet
    (facts files that existed at freeze time but yielded no metric rows)

Output: configs/facts_vintage.csv (committed; bootstrap filters estate reads
through it under EARNINGS_V3). Regenerate ONLY on a deliberate re-freeze.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import pandas as pd


def main() -> None:
    metrics = pd.read_parquet(bs.OUTPUTS_DIR / "metrics.parquet")
    events = pd.read_parquet(bs.OUTPUTS_DIR / "events.parquet")

    pairs = set(zip(metrics["slug"], metrics["period"]))
    ev = events[events["has_facts"] & events["slug"].notna()]
    pairs |= set(zip(ev["slug"], ev["period"]))

    df = (pd.DataFrame(sorted(pairs), columns=["slug", "period"]))
    out = bs.EARNINGS_ROOT / "configs" / "facts_vintage.csv"
    df.to_csv(out, index=False)
    print(f"vintage pairs: {len(df)} across {df['slug'].nunique()} slugs -> {out}")
    print(f"period range: {df['period'].min()} .. {df['period'].max()}")


if __name__ == "__main__":
    main()
