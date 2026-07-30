#!/usr/bin/env python3
"""build_metrics.py — point-in-time metrics panel from soft's XBRL facts files.

For each universe company and quarterly facts file, runs alpha-go's Tier-1
extractor (extract_from_xbrl) for revenue, operating_income, depreciation,
net_income and eps, and derives ebitda = operating_income + depreciation
(current and prior separately). ``prior`` is the year-ago figure as restated in
the SAME filing — exactly what the market saw at announcement time.

Output: outputs/metrics.parquet (long: ticker, slug, period, period_end,
metric, current, prior, source_concept)
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import pandas as pd
import yaml
from src.extract.xbrl_facts import extract_from_xbrl
from src.model.financial_model import MetricDef

PESOS_PER_UNIT = 1e6   # values in MXN millions; SUE is scale-invariant anyway
METRICS = ["revenue", "operating_income", "depreciation", "net_income", "eps"]
_PERIOD_RE = re.compile(r"^(20\d{2})-([1-4])T$")
_QUARTER_END = {"1": "-03-31", "2": "-06-30", "3": "-09-30", "4": "-12-31"}


def period_end_of(period: str) -> str | None:
    m = _PERIOD_RE.match(period)
    if not m:
        return None
    return m.group(1) + _QUARTER_END[m.group(2)]


def metric_defs() -> list[MetricDef]:
    # frozen study copy (byte-identical to vendor/alpha-go's at freeze time) so
    # the concept list cannot drift with either engine's configs
    concepts = yaml.safe_load(open(bs.EARNINGS_ROOT / "configs" / "xbrl_concepts.yaml"))
    defs = []
    for key in METRICS:
        spec = concepts["metrics"][key]
        defs.append(MetricDef(
            key=key, label=key, label_es=key,
            section="income",
            unit="per_share" if key == "eps" else "currency",
            patterns=[],
            xbrl_concepts=spec.get("xbrl_concepts", []),
        ))
    return defs


def main() -> None:
    cfg = bs.load_config()
    universe = bs.load_universe(cfg, "full")
    defs = metric_defs()
    rows = []
    for slug, v in universe.items():
        ticker = v["ticker"]
        for facts_path in bs.facts_glob(slug, ticker):
            period = bs.facts_period(facts_path, ticker)
            if period is None:
                continue
            pend = period_end_of(period)
            if pend is None:      # skip FY files — quarterly study only
                continue
            facts = json.loads(facts_path.read_text()).get("facts", {})
            found = extract_from_xbrl(facts, defs, period_end=pend,
                                      pesos_per_unit=PESOS_PER_UNIT)
            for key, row in found.items():
                rows.append({
                    "ticker": ticker, "slug": slug, "period": period,
                    "period_end": pend, "metric": key,
                    "current": row.current, "prior": row.prior,
                    "source_concept": row.source_line,
                })
            # ebitda derived from the two components of the SAME filing
            oi, dep = found.get("operating_income"), found.get("depreciation")
            if oi is not None and dep is not None and oi.current is not None and dep.current is not None:
                prior = (
                    oi.prior + dep.prior
                    if oi.prior is not None and dep.prior is not None else None
                )
                rows.append({
                    "ticker": ticker, "slug": slug, "period": period,
                    "period_end": pend, "metric": "ebitda",
                    "current": oi.current + dep.current, "prior": prior,
                    "source_concept": "[calc] operating_income + depreciation",
                })

    df = pd.DataFrame(rows)
    bs.OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(bs.art_path("metrics"), index=False)

    # ---- verify block ----
    wide = df.pivot_table(index=["slug", "period"], columns="metric",
                          values="current", aggfunc="first")
    print(f"rows: {len(df)}  companies: {df['slug'].nunique()}  periods: {df['period'].nunique()}")
    print("\nnon-null coverage by metric (% of company-quarters):")
    print((wide.notna().mean() * 100).round(1).to_string())
    q4 = wide.reset_index()
    q4["q"] = q4["period"].str[-2]
    print("\nnet_income coverage by fiscal quarter (Q4 should match Q1-Q3):")
    print((q4.groupby("q")["net_income"].apply(lambda s: s.notna().mean() * 100)).round(1).to_string())
    w = df[(df["slug"] == "walmex") & (df["period"] == "2025-4T") & (df["metric"] == "revenue")]
    print(f"\nspot-check WALMEX 2025-4T revenue (MXN MM): {w['current'].iloc[0] if len(w) else 'MISSING'}")
    print(f"           prior (2024-4T as restated)     : {w['prior'].iloc[0] if len(w) else 'MISSING'}")


if __name__ == "__main__":
    main()
