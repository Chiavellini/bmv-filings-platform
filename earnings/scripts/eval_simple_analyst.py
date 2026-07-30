#!/usr/bin/env python3
"""eval_simple_analyst.py — plain-language tables for the analyst-facing
report: hit rates per conviction, model vs analyst vs ensembles, and error
breakdowns by sector / company / liquidity.

DIAGNOSTIC ONLY (study.yaml `analyst.adoption`): nothing here enters the
traded spec. The sector, liquidity, and ensemble cuts are POST-HOC analyses
(logged as researcher degrees of freedom in the dissection report ledger).
Model direction throughout = sign(s_ts); model conviction = |s_ts| > 1.0.

Outputs: results_v3/simple_{conviction,ensemble,sector,traits}.csv
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import study

import numpy as np
import pandas as pd

XLSX = bs.EARNINGS_ROOT / "data/analyst/analyst_expectation_2026-07-28.xlsx"


def sector_map(workbook: Path | None = None) -> dict[str, str]:
    """ticker -> sector from the workbook's row order (col B, rows 2-60):
    mixed-case rows are sector headers, ALL-CAPS rows are tickers.

    ``workbook`` is injectable for clean-clone tests; production callers retain
    the frozen analyst workbook as the default.
    """
    from openpyxl import load_workbook
    workbook = XLSX if workbook is None else Path(workbook)
    ws = load_workbook(workbook, read_only=True, data_only=True)["Hoja1"]
    out: dict[str, str] = {}
    sector = None
    for r in range(2, 61):
        cell = ws.cell(row=r, column=2).value
        if not cell:
            continue
        name = str(cell).strip()
        if name != name.upper():          # mixed-case = sector header
            sector = name
            continue
        if sector is not None:
            out[name] = sector
    assert len(out) == 40 and len(set(out.values())) == 10, \
        f"sector map drifted: {len(out)} tickers / {len(set(out.values()))} sectors"
    return out


def hit_row(name: str, cohort: str, hits: pd.Series) -> dict:
    n = len(hits)
    return {"grupo": name, "cohorte": cohort, "n": n,
            "aciertos": int(hits.sum()),
            "pct": round(100 * hits.mean(), 1) if n else np.nan}


def ensemble_hits(d: pd.DataFrame) -> tuple[dict, dict]:
    """(a) both agree; (b) analyst full-strength overrides, else model.

    ``d`` = matched rows with direction, strength, model_dir (sign of s_ts,
    0 only if s_ts==0), realized_up."""
    dd = d[d["direction"] != 0]
    agree = dd[dd["direction"] == dd["model_dir"]]
    a = hit_row("cuando ambos coinciden", "ensamble",
                (agree["direction"] == 1) == agree["realized_up"])
    a["cobertura_pct"] = round(100 * len(agree) / len(dd), 1) if len(dd) else np.nan
    strong = (d["direction"] != 0) & (d["strength"] == 1.0)
    call = np.where(strong, d["direction"], d["model_dir"])
    b_hits = pd.Series((call == 1) == d["realized_up"].to_numpy())[call != 0]
    b = hit_row("convicción fuerte manda, si no el modelo", "ensamble", b_hits)
    b["cobertura_pct"] = 100.0
    return a, b


def main() -> None:
    assert bs.V3, "set EARNINGS_V3=1"
    cfg = bs.load_config()
    smap = sector_map()

    pred = pd.read_parquet(bs.OUTPUTS_DIR / "analyst_predictions.parquet")
    pred = pred[pred["slug"].notna()]
    ew = pd.read_parquet(bs.art_path("event_windows"))
    dev = study.dev_sample(ew, cfg, which="dev")

    # full 73-row matched join (eval_analyst.py convention)
    j = pred.merge(dev[["slug", "period", "ar0_cc", "s_ts",
                        "median_peso_volume"]],
                   on=["slug", "period"], how="inner")
    assert len(j) == 73, f"matched {len(j)} != 73"
    j["realized_up"] = j["ar0_cc"] > 0
    j["model_dir"] = np.sign(j["s_ts"]).astype(int)
    j["sector"] = j["ticker"].map(smap)
    d = j[j["direction"] != 0].copy()
    assert len(d) == 67
    d["hit"] = (d["direction"] == 1) == d["realized_up"]

    # ---- 1. conviction table (analyst + model, same yardstick)
    rows = [
        hit_row("todas las llamadas direccionales", "analista", d["hit"]),
        hit_row("convicción fuerte (Positive/Negative)", "analista",
                d[d["strength"] == 1.0]["hit"]),
        hit_row("convicción suave (N to P / N to N)", "analista",
                d[d["strength"] == 0.5]["hit"]),
        hit_row("llamadas alcistas", "analista", d[d["direction"] == 1]["hit"]),
        hit_row("llamadas bajistas", "analista", d[d["direction"] == -1]["hit"]),
    ]
    assert rows[1]["aciertos"] == 25 and rows[1]["n"] == 27
    assert rows[0]["pct"] == 68.7 and rows[2]["pct"] == 52.5

    md = study.apply_liquidity(dev, cfg["liquidity"]
                               ["primary_min_median_peso_volume"])
    md = md[md["s_ts"].notna() & (md["s_ts"] != 0)].copy()
    md["model_hit"] = (md["s_ts"] > 0) == (md["ar0_cc"] > 0)
    md["alta"] = md["s_ts"].abs() > 1.0
    assert len(md) == 484 and round(100 * md["model_hit"].mean(), 1) == 58.5
    jm = j[j["model_dir"] != 0].copy()
    jm["model_hit"] = (jm["model_dir"] == 1) == jm["realized_up"]
    rows += [
        hit_row("todas las señales", "modelo", md["model_hit"]),
        hit_row("alta convicción (|señal| > 1)", "modelo",
                md[md["alta"]]["model_hit"]),
        hit_row("baja convicción (|señal| <= 1)", "modelo",
                md[~md["alta"]]["model_hit"]),
        hit_row("señales alcistas", "modelo", md[md["s_ts"] > 0]["model_hit"]),
        hit_row("señales bajistas", "modelo", md[md["s_ts"] < 0]["model_hit"]),
    ]
    # like-for-like: model restricted to the analyst's company-quarters
    jm["alta"] = jm["s_ts"].abs() > 1.0
    rows += [
        hit_row("todas las señales", "modelo_emparejado", jm["model_hit"]),
        hit_row("alta convicción (|señal| > 1)", "modelo_emparejado",
                jm[jm["alta"]]["model_hit"]),
        hit_row("baja convicción (|señal| <= 1)", "modelo_emparejado",
                jm[~jm["alta"]]["model_hit"]),
        hit_row("señales alcistas", "modelo_emparejado",
                jm[jm["model_dir"] == 1]["model_hit"]),
        hit_row("señales bajistas", "modelo_emparejado",
                jm[jm["model_dir"] == -1]["model_hit"]),
    ]
    conv = pd.DataFrame(rows)
    # head-to-head on the identical events: the analyst's directional calls
    # where the model also has a sign — same events, same yardstick
    hh = d[d["model_dir"] != 0].copy()
    hh["model_hit"] = (hh["model_dir"] == 1) == hh["realized_up"]
    n_hh = len(hh)
    h2h = pd.DataFrame([
        hit_row(f"analista (mismos {n_hh} eventos)", "cara_a_cara", hh["hit"]),
        hit_row(f"modelo (mismos {n_hh} eventos)", "cara_a_cara",
                hh["model_hit"]),
    ])
    assert (h2h["n"] == n_hh).all() and n_hh >= 60

    # ---- 2. ensembles
    ens_a, ens_b = ensemble_hits(j)
    assert ens_a["n"] == 44 and ens_a["pct"] == 70.5
    assert ens_b["pct"] == 64.4
    ens = pd.DataFrame([
        hit_row("analista solo", "referencia", d["hit"]),
        {**hit_row("modelo solo (mismas empresas-trimestre)", "referencia",
                   jm["model_hit"]), "cobertura_pct": 100.0},
        ens_a, ens_b])

    # ---- 3. sectors (analyst on matched; model on full dev, mapped slugs)
    slug_sector = {row.slug: smap[row.ticker]
                   for row in pred[["ticker", "slug"]].drop_duplicates()
                   .itertuples() if row.ticker in smap}
    sec_rows = [hit_row(sec, "analista", g["hit"])
                for sec, g in d.groupby("sector")]
    mds = md.assign(sector=md["slug"].map(slug_sector)).dropna(subset=["sector"])
    sec_rows += [hit_row(sec, "modelo", g["model_hit"])
                 for sec, g in mds.groupby("sector")]
    sector = pd.DataFrame(sec_rows).sort_values(
        ["cohorte", "pct"]).reset_index(drop=True)

    # ---- 3b. sector x conviction class (analyst): does sector weakness
    # survive within conviction, or is it soft calls all the way down?
    d["clase"] = np.where(d["strength"] == 1.0, "fuerte", "suave")
    sc = (d.groupby(["sector", "clase"])
          .agg(n=("hit", "size"), aciertos=("hit", "sum")).reset_index())
    sc["pct"] = (100 * sc["aciertos"] / sc["n"]).round(1)
    assert sc[sc["clase"] == "fuerte"]["n"].sum() == 27
    assert sc[sc["clase"] == "fuerte"]["aciertos"].sum() == 25
    sc.to_csv(bs.RESULTS_DIR / "simple_sector_conviction.csv", index=False)

    # ---- 4. traits: liquidity terciles (matched analyst + full-dev model)
    def liq_bucket(s: pd.Series) -> pd.Series:
        return pd.qcut(s.rank(method="first"), 3,
                       labels=["baja", "media", "alta"])

    d["liq"] = liq_bucket(d["median_peso_volume"])
    md["liq"] = liq_bucket(md["median_peso_volume"])
    traits = pd.DataFrame(
        [hit_row(f"liquidez {b}", "analista", d[d["liq"] == b]["hit"])
         for b in ("baja", "media", "alta")] +
        [hit_row(f"liquidez {b}", "modelo", md[md["liq"] == b]["model_hit"])
         for b in ("baja", "media", "alta")])
    a_low = traits[(traits["grupo"] == "liquidez baja")
                   & (traits["cohorte"] == "analista")].iloc[0]
    assert a_low["n"] == 23 and a_low["pct"] == 60.9

    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    h2h.to_csv(bs.RESULTS_DIR / "simple_headtohead.csv", index=False)
    conv.to_csv(bs.RESULTS_DIR / "simple_conviction.csv", index=False)
    ens.to_csv(bs.RESULTS_DIR / "simple_ensemble.csv", index=False)
    sector.to_csv(bs.RESULTS_DIR / "simple_sector.csv", index=False)
    traits.to_csv(bs.RESULTS_DIR / "simple_traits.csv", index=False)
    for name, df in (("conviction", conv), ("ensemble", ens),
                     ("sector", sector), ("traits", traits)):
        print(f"--- simple_{name}.csv")
        print(df.to_string(index=False))


if __name__ == "__main__":
    main()
