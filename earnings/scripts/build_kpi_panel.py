#!/usr/bin/env python3
"""build_kpi_panel.py — per-company quarterly KPI panel: the analyst's three
pillars (revenue growth, profitability, debt) vs the company's own history
and, where vintages exist, vs desk estimates.

Sources:
  * metrics(.parquet)          — XBRL era income lines, point-in-time priors
  * metrics_hist_v2            — pre-2021 income lines (cleaned; no priors)
  * facts files                — balance-sheet debt/cash at instant==period_end
                                 (concepts frozen in study.yaml phase_h.debt,
                                 coverage documented in audit/debt_census.csv);
                                 debt is XBRL-era-only by construction
  * surprises(_v2)             — SUE scores for context
  * estimates_pit              — desk estimates (2026-1T onward; see sweep)

Units: values stored /1e6 (millions of the REPORTING currency — `currency`
column; ORBIA/GRUMA etc. report USD). Ratios and YoY are within-company and
currency-safe.

Output: outputs/kpi_panel.parquet
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import numpy as np
import pandas as pd

QEND = {"1": "-03-31", "2": "-06-30", "3": "-09-30", "4": "-12-31"}
FNAME = re.compile(r"^(?P<ticker>.+)_(?P<period>20\d{2}-[1-4]T)_facts\.json$")


def _prev_year(p: str) -> str:
    return f"{int(p[:4]) - 1}{p[4:]}"


def _instant(entries, pend):
    if not isinstance(entries, list):
        return None
    for e in entries:
        if (e.get("instant") == pend and not e.get("dimensions")
                and e.get("value") is not None):
            return e
    return None


def _facts_paths():
    """(slug, period, path) for every facts file: the soft store plus the
    frozen walk-forward archive (fresh quarters not yet mirrored into soft).
    Soft wins on duplicates (listed first)."""
    uni = bs.load_universe(bs.load_config(), "full")
    t2s = {v["ticker"]: s for s, v in uni.items()}
    seen = set()
    for path in sorted((bs.SOFT_ROOT / "data" / "reports").glob("*/xbrl/*_facts.json")):
        m = FNAME.match(path.name)
        if not m:
            continue
        slug = path.parent.parent.name
        key = (slug, m.group("period"))
        seen.add(key)
        yield slug, m.group("period"), path
    wf = bs.EARNINGS_ROOT / "data" / "walkforward" / "xbrl"
    for path in sorted(wf.glob("*_facts.json")):
        m = FNAME.match(path.name)
        if not m:
            continue
        slug = t2s.get(m.group("ticker"))
        if slug is None or (slug, m.group("period")) in seen:
            continue
        yield slug, m.group("period"), path


def load_balance() -> pd.DataFrame:
    """Debt/cash instants from every facts file, in reporting-currency MM."""
    cfg = bs.load_config()
    dcfg = cfg["phase_h"]["debt"]
    gross = [f"ifrs-full_{c}" for c in dcfg["gross_debt_concepts"]]
    lease = [f"ifrs-full_{c}" for c in dcfg["lease_concepts"]]
    cash_c = f"ifrs-full_{dcfg['cash_concept']}"
    rows = []
    for slug, period, path in _facts_paths():
        pend = period[:4] + QEND[period[5]]
        try:
            facts = json.loads(path.read_text()).get("facts", {})
        except (json.JSONDecodeError, OSError):
            continue
        vals, unit = {}, None
        for label, concepts in (("gross", gross), ("lease", lease)):
            tot, n = 0.0, 0
            for c in concepts:
                e = _instant(facts.get(c), pend)
                if e is not None:
                    tot += float(e["value"])
                    n += 1
                    unit = unit or e.get("unit")
            vals[label] = tot / 1e6 if n else np.nan
        e = _instant(facts.get(cash_c), pend)
        vals["cash"] = float(e["value"]) / 1e6 if e else np.nan
        if e is not None:
            unit = unit or e.get("unit")
        rows.append({"slug": slug, "period": period,
                     "gross_debt": vals["gross"], "lease_liab": vals["lease"],
                     "cash": vals["cash"],
                     "currency": (unit or "").replace("ISO4217:", "")})
    return pd.DataFrame(rows)


def load_walkforward_metrics() -> pd.DataFrame:
    """Income metrics for fresh quarters that exist only in the frozen
    walk-forward archive (not yet mirrored into the soft store / metrics
    parquet). Same extraction as run_walkforward.wf_metrics."""
    from src.extract.xbrl_facts import extract_from_xbrl
    sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
    from build_metrics import metric_defs

    defs = metric_defs()
    uni = bs.load_universe(bs.load_config(), "full")
    t2s = {v["ticker"]: s for s, v in uni.items()}
    rows = []
    wf = bs.EARNINGS_ROOT / "data" / "walkforward" / "xbrl"
    for path in sorted(wf.glob("*_facts.json")):
        m = FNAME.match(path.name)
        if not m or t2s.get(m.group("ticker")) is None:
            continue
        slug, period = t2s[m.group("ticker")], m.group("period")
        pend = period[:4] + QEND[period[5]]
        facts = json.loads(path.read_text()).get("facts", {})
        found = extract_from_xbrl(facts, defs, period_end=pend,
                                  pesos_per_unit=1e6)
        for key, row in found.items():
            rows.append({"slug": slug, "period": period, "metric": key,
                         "current": row.current, "prior": row.prior})
        oi, dep = found.get("operating_income"), found.get("depreciation")
        if oi and dep and oi.current is not None and dep.current is not None:
            rows.append({"slug": slug, "period": period, "metric": "ebitda",
                         "current": oi.current + dep.current,
                         "prior": (oi.prior + dep.prior
                                   if oi.prior is not None and dep.prior is not None
                                   else None)})
    return pd.DataFrame(rows)


def main() -> None:
    cfg = bs.load_config()
    m = pd.read_parquet(bs.art_path("metrics"))
    wf = load_walkforward_metrics()
    if len(wf):
        key = m.set_index(["slug", "period", "metric"]).index
        wf = wf[~wf.set_index(["slug", "period", "metric"]).index.isin(key)]
        m = pd.concat([m, wf], ignore_index=True)
        print(f"walk-forward metrics merged: {len(wf)} rows "
              f"({wf['slug'].nunique()} slugs, "
              f"{sorted(wf['period'].unique())})")
    cur = m.pivot_table(index=["slug", "period"], columns="metric",
                        values="current", aggfunc="first")
    pri = m.pivot_table(index=["slug", "period"], columns="metric",
                        values="prior", aggfunc="first")
    hp = bs.art_path("metrics_hist")
    if hp.exists():
        h = pd.read_parquet(hp)
        hcur = h.pivot_table(index=["slug", "period"], columns="metric",
                             values="current", aggfunc="first")
        from earnlib.study import merge_hist_currents
        cur = merge_hist_currents(cur, hcur)
        pri = pri.reindex(cur.index)

    df = cur.reset_index().sort_values(["slug", "period"]).reset_index(drop=True)
    prv = pri.reset_index().sort_values(["slug", "period"]).reset_index(drop=True)

    # ---- YoY revenue growth: point-in-time prior where filed, else merged ----
    prev_map = cur.reindex(
        pd.MultiIndex.from_tuples([(s, _prev_year(p)) for s, p in cur.index],
                                  names=cur.index.names))
    rev_prev = pd.Series(prev_map["revenue"].to_numpy(), index=cur.index)
    rev_pit_prior = pri["revenue"].reindex(cur.index)
    base = rev_pit_prior.where(rev_pit_prior.notna(), rev_prev)
    with np.errstate(divide="ignore", invalid="ignore"):
        yoy = (cur["revenue"] - base) / base.abs()
    df["yoy_rev_pct"] = (yoy * 100).to_numpy()
    df["yoy_basis"] = np.where(rev_pit_prior.notna(), "pit_prior",
                               np.where(rev_prev.notna(), "merged_yoy", ""))

    # ---- margins + YoY deltas (pp) ----
    for num, name in (("operating_income", "ebit_margin"),
                      ("ebitda", "ebitda_margin"),
                      ("net_income", "net_margin")):
        if num in cur.columns:
            with np.errstate(divide="ignore", invalid="ignore"):
                marg = (cur[num] / cur["revenue"]) * 100
            df[name] = marg.to_numpy()
    df = df.sort_values(["slug", "period"]).reset_index(drop=True)
    for name in ("ebit_margin", "ebitda_margin", "net_margin"):
        if name in df.columns:
            prev = df.groupby("slug")[name].shift(4)
            df[f"{name}_yoy_pp"] = df[name] - prev

    # ---- vs own history baselines ----
    g = df.groupby("slug")["yoy_rev_pct"]
    df["yoy_rev_trail8_mean"] = g.transform(
        lambda s: s.shift(1).rolling(8, min_periods=4).mean())
    df["rev_accel_pp"] = df["yoy_rev_pct"] - g.transform(
        lambda s: s.shift(1).rolling(4, min_periods=2).mean())
    df["fiscal_q"] = df["period"].str[5]
    df["yoy_rev_sameq_mean"] = (
        df.groupby(["slug", "fiscal_q"])["yoy_rev_pct"]
        .transform(lambda s: s.shift(1).expanding(min_periods=2).mean()))

    # ---- debt (XBRL era only) ----
    bal = load_balance()
    df = df.merge(bal, on=["slug", "period"], how="left")
    df["gross_debt_incl_leases"] = df["gross_debt"] + df["lease_liab"].fillna(0)
    df["net_debt"] = df["gross_debt"] - df["cash"]
    df["net_debt_incl_leases"] = df["gross_debt_incl_leases"] - df["cash"]
    if "ebitda" in df.columns:
        ttm = df.groupby("slug")["ebitda"].transform(
            lambda s: s.rolling(4, min_periods=4).sum())
        df["ebitda_ttm"] = ttm
        with np.errstate(divide="ignore", invalid="ignore"):
            df["nd_to_ebitda_ttm"] = np.where(ttm > 0, df["net_debt"] / ttm, np.nan)
            df["nd_incl_leases_to_ebitda_ttm"] = np.where(
                ttm > 0, df["net_debt_incl_leases"] / ttm, np.nan)
    df["net_debt_yoy_chg"] = df.groupby("slug")["net_debt"].diff(4)
    df["net_debt_qoq_chg"] = df.groupby("slug")["net_debt"].diff(1)

    # ---- SUE context ----
    sp = bs.art_path("surprises")
    if sp.exists():
        s = pd.read_parquet(sp)
        keep = [c for c in ("slug", "period", "sue_revenue", "sue_ebitda",
                            "sue_net_income", "s_cs", "s_ts") if c in s.columns]
        df = df.merge(s[keep], on=["slug", "period"], how="left")

    # ---- desk estimates ----
    ep = bs.OUTPUTS_DIR / "estimates_pit.parquet"
    if ep.exists():
        est = pd.read_parquet(ep)
        wide_est = est.pivot_table(index=["slug", "period"], columns="metric",
                                   values="est_value", aggfunc="first")
        wide_est.columns = [f"est_{c}" for c in wide_est.columns]
        vint = est.groupby(["slug", "period"])["vintage"].first().rename("est_vintage")
        df = df.merge(wide_est.reset_index(), on=["slug", "period"], how="left")
        df = df.merge(vint.reset_index(), on=["slug", "period"], how="left")
        for met, actual in (("revenue", "revenue"), ("ebitda", "ebitda"),
                            ("net_income", "net_income")):
            ec = f"est_{met}"
            if ec in df.columns and actual in df.columns:
                with np.errstate(divide="ignore", invalid="ignore"):
                    df[f"beat_{met}_pct"] = ((df[actual] - df[ec])
                                             / df[ec].abs()) * 100

    tickers = {s: v["ticker"] for s, v in bs.load_universe(cfg, "full").items()}
    df["ticker"] = df["slug"].map(tickers)
    df.to_parquet(bs.art_path("kpi_panel"), index=False)

    # ---- verify block ----
    print(f"kpi_panel: {len(df)} slug-quarters, {df['slug'].nunique()} slugs, "
          f"{df['period'].min()} .. {df['period'].max()}")
    print(f"debt coverage: {df['gross_debt'].notna().sum()} rows; "
          f"estimates: {df.get('est_revenue', pd.Series(dtype=float)).notna().sum()} rows")
    spot = df[(df["slug"] == "ac") & (df["period"] == "2026-2T")]
    if len(spot):
        r = spot.iloc[0]
        print(f"\nspot AC 2026-2T: revenue={r['revenue']:.0f} (expect ~63489) "
              f"ebitda={r.get('ebitda', np.nan):.0f} (expect ~13133) "
              f"nd/ebitda={r.get('nd_to_ebitda_ttm', np.nan):.2f} "
              f"beat_rev={r.get('beat_revenue_pct', np.nan):+.1f}%")
    for slug in ("orbia", "gruma"):
        c = df[df["slug"] == slug]["currency"].dropna()
        print(f"{slug} currency: {sorted(c.unique())}")


if __name__ == "__main__":
    main()
