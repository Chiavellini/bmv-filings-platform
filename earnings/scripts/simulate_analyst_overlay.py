#!/usr/bin/env python3
"""simulate_analyst_overlay.py — the pre-committed analyst overlay
(study.yaml `analyst`, 2026-07-28): s_ts + w * direction * strength for
w in overlay_weights, run through the SAME walk-forward signal layer as
simulate_signal_layer.py (classification, PIT conviction, quarter-Kelly).

DIAGNOSTIC ONLY (study.yaml `analyst.adoption`): no analyst signal enters the
traded spec from this evaluation. Dev quarters only; holdout predictions are
counted but never joined to returns. w=0.0 must reproduce the certified
results_v3/signal_layer.csv row-for-row before any overlay number is written.

Outputs: results_v3/analyst_overlay_{grid,flips,covered,shuffle}.csv
         + outputs/OVERLAY.md
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib.mdutil import md_table
from earnlib import signal as sg
from earnlib import stats as st
from earnlib import study

import numpy as np
import pandas as pd

sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
from simulate_strategy import trade_returns          # noqa: E402
from simulate_signal_layer import daily_returns, sim  # noqa: E402

ADOPTION_RULE = (
    "DIAGNOSTIC ONLY in this phase: no analyst signal enters the traded spec "
    "from this evaluation. Any future adoption candidate must be re-specified "
    "with dev FM t >= incumbent + 0.5 AND shuffle p < 0.05, arbiter one-shot "
    "2026-3T walk-forward. Sample-size caveat (138 calls, fewer matched) "
    "leads every table.")


def walk_forward(tr: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Identical loop to simulate_signal_layer.py: trailing-8q PIT conviction."""
    sl = cfg["signal_layer"]
    quarters = sorted(tr["period"].unique(), key=lambda p: (p[:4], p[5]))
    cal_q = int(sl["conviction"]["calibration_quarters"])
    decided_rows = []
    for qi, q in enumerate(quarters):
        train_q = quarters[max(0, qi - cal_q):qi]
        train = tr[tr["period"].isin(train_q) & (tr["action"] != "neutral")]
        model = sg.fit_conviction(train, cfg) if len(train) else None
        for _, r in tr[tr["period"] == q].iterrows():
            d = sg.decide(r["s_ts"], model, cfg, n_calibration=len(train))
            decided_rows.append({**r.to_dict(), "label": d.label,
                                 "action": d.action, "p_hat": d.p_hat,
                                 "kelly_f": d.kelly_f})
    return pd.DataFrame(decided_rows)


def grid(dec: pd.DataFrame, cfg: dict, dev_years: float) -> pd.DataFrame:
    sl = cfg["signal_layer"]
    rows = []
    for legs, name in ((("buy",), "long only"), (("short",), "short only"),
                       (("buy", "short"), "combined")):
        sub = dec[dec["action"].isin(legs)]
        for sizing in ("equal", "kelly"):
            for cost in sl["costs_bps"]:
                rows.append({"legs": name, "sizing": sizing, "cost_bps": cost,
                             **sim(sub, cost, sizing, dev_years)})
    return pd.DataFrame(rows)


def combined_kelly_25(dec: pd.DataFrame, dev_years: float) -> dict:
    return sim(dec[dec["action"].isin(("buy", "short"))], 25, "kelly",
               dev_years)


def shuffle_p(dec: pd.DataFrame, w: float, real_stat: float,
              cfg: dict) -> tuple[float, int]:
    """Permute the ANALYST contribution within quarter among covered rows,
    holding s_ts_base and the fitted kelly_f maps fixed (certified convention:
    the null reassigns signals, keeping the calibrated sizing columns)."""
    acf = cfg["analyst"]
    rng = np.random.default_rng(cfg["validation"]["seed"] + acf["seed_offset"])
    n_perm = cfg["validation"]["n_perm_shuffle"]
    covered = dec["covered"].to_numpy()
    base = dec["s_ts_base"].to_numpy(dtype=float)
    a_real = dec["analyst_signal"].to_numpy(dtype=float)
    period_idx = dec.groupby("period").indices
    perm_ge = 0
    for _ in range(n_perm):
        a = a_real.copy()
        for _, idx in period_idx.items():
            cov = idx[covered[idx]]
            if len(cov) > 1:
                a[cov] = rng.permutation(a[cov])
        p = dec.copy()
        p["s_perm"] = base + w * a
        p["action"] = [sg.classify(s, cfg)[1] for s in p["s_perm"]]
        traded = p[p["action"] != "neutral"]
        if not len(traded):
            continue
        daily = traded.groupby("t0").apply(
            lambda g: daily_returns(g, 25, "kelly"))
        if float(daily.mean()) * 1e4 >= real_stat:
            perm_ge += 1
    return (perm_ge + 1) / (n_perm + 1), n_perm


def fm_t3t1(df: pd.DataFrame, col: str) -> dict:
    """Per-quarter tercile T3-T1 of ar0_cc on `col`, FM t over quarters."""
    t = study.add_terciles(df, col=col)
    spreads = []
    for _, g in t.groupby("period"):
        if g["tercile"].notna().sum() == 0:
            continue
        t3 = g[g["tercile"] == 3]["ar0_cc"].mean()
        t1 = g[g["tercile"] == 1]["ar0_cc"].mean()
        if np.isfinite(t3) and np.isfinite(t1):
            spreads.append(t3 - t1)
    fm = st.fama_macbeth(np.array(spreads))
    return {"fm_bps": round(fm["fm_mean"] * 1e4, 1) if np.isfinite(fm["fm_mean"]) else np.nan,
            "fm_t": round(fm["fm_t"], 2) if np.isfinite(fm["fm_t"]) else np.nan,
            "n_quarters": fm["n_quarters"]}


def main() -> None:
    assert bs.V3, "analyst overlay runs on v3 artifacts: set EARNINGS_V3=1"
    cfg = bs.load_config()
    acf = cfg["analyst"]
    holdout_q = set(cfg["holdout"]["quarters"]) | set(
        cfg.get("phase_d", {}).get("historical_holdout_quarters", []))

    pred = pd.read_parquet(bs.OUTPUTS_DIR / "analyst_predictions.parquet")
    pred = pred[pred["slug"].notna()]
    n_holdout_pred = int(pred["period"].isin(holdout_q).sum())
    assert n_holdout_pred == 20, f"holdout predictions {n_holdout_pred} != 20"

    # replicate eval_analyst's dev join as the coverage cross-check (73/67)
    ew_art = pd.read_parquet(bs.art_path("event_windows"))
    dev_ew = study.dev_sample(ew_art, cfg, which="dev")
    j = pred.merge(dev_ew[["slug", "period", "ar0_cc"]],
                   on=["slug", "period"], how="inner")
    assert len(j) == 73, f"dev-matched predictions {len(j)} != 73"
    assert int((j["direction"] != 0).sum()) == 67, "directional != 67"

    # assembly identical to simulate_signal_layer.py
    panel = study.load_panel(cfg)
    ew = study.assemble_events(cfg, panel)
    prices = pd.read_parquet(bs.art_path("prices"))
    base = study.apply_liquidity(
        study.dev_sample(ew, cfg),
        cfg["liquidity"]["primary_min_median_peso_volume"])
    tr = trade_returns(base, prices)
    tr = tr[tr["s_ts"].notna()].copy()
    assert not set(tr["period"]) & holdout_q, "holdout leaked into sim frame"

    t0s = pd.to_datetime(tr["t0"])
    dev_years = max((t0s.max() - t0s.min()).days / 365.25, 1e-9)

    weights = [0.0] + [float(w) for w in acf["overlay_weights"]]
    grids, decs = {}, {}
    for w in weights:
        dfw = sg.analyst_overlay(tr, pred, w)
        dfw["action"] = [sg.classify(s, cfg)[1] for s in dfw["s_ts"]]
        dec = walk_forward(dfw, cfg)
        decs[w] = dec
        g = grid(dec, cfg, dev_years)
        g.insert(0, "w", w)
        grids[w] = g

    # w=0 must reproduce the certified signal layer byte-for-byte (anti-drift)
    cert = pd.read_csv(bs.RESULTS_DIR / "signal_layer.csv")
    w0 = grids[0.0].drop(columns=["w"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(
        w0, cert, check_dtype=False, check_exact=False, rtol=0, atol=1e-9)
    print("w=0 grid reproduces certified signal_layer.csv — environment OK")

    n_covered_sim = int(decs[0.0]["covered"].sum())
    print(f"coverage: dev-matched 73 / directional 67 / holdout excluded 20; "
          f"in traded sim frame (liq-filtered): {n_covered_sim}")

    # flip accounting: base action vs overlay action, covered rows only
    flip_rows = []
    for w in weights[1:]:
        d = decs[w]
        base_action = [sg.classify(s, cfg)[1] for s in d["s_ts_base"]]
        d = d.assign(base_action=base_action)
        unc = d[~d["covered"]]
        assert (unc["base_action"] == unc["action"]).all(), \
            "uncovered row flipped"
        cov = d[d["covered"]]
        ct = (cov.groupby(["base_action", "action"]).size()
              .rename("n").reset_index())
        ct.insert(0, "w", w)
        flip_rows.append(ct)
    flips = pd.concat(flip_rows, ignore_index=True)

    # covered-subsample like-for-like (same events, base vs overlay decisions)
    cov_rows = []
    for w in weights[1:]:
        keys = decs[w][decs[w]["covered"]][["slug", "period"]]
        for name, dsrc in (("base", decs[0.0]), (f"overlay w={w}", decs[w])):
            sub = dsrc.merge(keys, on=["slug", "period"])
            cov_rows.append({"w": w, "decisions": name,
                             **combined_kelly_25(sub, dev_years)})
    covered_cmp = pd.DataFrame(cov_rows)

    # FM tercile diagnostic on covered quarters (context for the adoption bar)
    cov_q = sorted(set(decs[0.0][decs[0.0]["covered"]]["period"]))
    fm_rows = []
    for w in weights[1:]:
        sub = decs[w][decs[w]["period"].isin(cov_q)]
        fm_rows.append({"w": w, "score": "s_ts_base",
                        **fm_t3t1(sub, "s_ts_base")})
        fm_rows.append({"w": w, "score": f"s_ts + {w}*analyst",
                        **fm_t3t1(sub, "s_ts")})
    fm_diag = pd.DataFrame(fm_rows).drop_duplicates(
        subset=["score", "fm_bps", "fm_t"])

    # analyst-component shuffle null per weight (pre-committed seed_offset 4;
    # rng re-seeded identically per w so each weight faces the same stream)
    shuf_rows = []
    for w in weights[1:]:
        real = combined_kelly_25(decs[w], dev_years)
        real_stat = real.get("mean_day_bps", np.nan)
        p, n_perm = shuffle_p(decs[w], w, real_stat, cfg)
        shuf_rows.append({"w": w, "real_mean_day_bps": real_stat,
                          "p": round(p, 4), "n_perm": n_perm,
                          "seed": cfg["validation"]["seed"] + acf["seed_offset"],
                          "stat": "combined/kelly/25 mean_day_bps"})
        print(f"shuffle w={w}: real={real_stat} p={p:.4f}")
    shuffle_df = pd.DataFrame(shuf_rows)

    # POST-HOC sensitivity (not pre-registered): soft calls only — the
    # full-strength cell is the one most exposed to label-timing risk
    posthoc_rows = []
    for w in weights[1:]:
        dfw = sg.analyst_overlay(tr, pred[pred["strength"] < 1.0], w)
        dfw["action"] = [sg.classify(s, cfg)[1] for s in dfw["s_ts"]]
        dec = walk_forward(dfw, cfg)
        posthoc_rows.append({"w": w, "calls": "soft only (N to P / N to N)",
                             **combined_kelly_25(dec, dev_years)})
    posthoc = pd.DataFrame(posthoc_rows)

    all_grids = pd.concat([grids[w] for w in weights], ignore_index=True)
    bs.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    all_grids.to_csv(bs.RESULTS_DIR / "analyst_overlay_grid.csv", index=False)
    flips.to_csv(bs.RESULTS_DIR / "analyst_overlay_flips.csv", index=False)
    covered_cmp.to_csv(bs.RESULTS_DIR / "analyst_overlay_covered.csv",
                       index=False)
    shuffle_df.to_csv(bs.RESULTS_DIR / "analyst_overlay_shuffle.csv",
                      index=False)

    focus = all_grids[(all_grids["legs"] == "combined")
                      & (all_grids["sizing"] == "kelly")
                      & (all_grids["cost_bps"] == 25)]
    lines = [
        "# Analyst overlay — pre-committed analyst-weighted system "
        "(dev walk-forward, v3 artifacts)",
        "",
        "**Sample-size caveat first**: 138 predictions (2Q24-2Q26); 20 in "
        "holdout quarters are excluded from every return-joined number; 73 "
        f"match a dev event, 67 directional; {n_covered_sim} fall inside the "
        "liquidity-filtered traded frame. Calls exist only from 2024-3T "
        "onward — every earlier quarter trades the base signal unchanged.",
        "",
        "**Provenance**: the analyst confirmed (2026-07-28, in-session) that "
        "every label was recorded BEFORE the report's release; results are "
        "presented at face value. Prospectively logged quarters (2026-3T "
        "onward, pre-registered and hash-committed before each release) "
        "remain the independent confirmation.",
        "",
        "Spec pre-committed in study.yaml `analyst` (2026-07-28) before this "
        "run: overlay score = s_ts + w*direction*strength, w in "
        f"{acf['overlay_weights']}, cost focus {acf['overlay_cost_bps']} bps, "
        "seed = validation.seed + 4. w=0.0 reproduced the certified "
        "signal_layer.csv exactly before any overlay number was computed.",
        "",
        "## 1. Walk-forward grid (all weights)",
        "",
        md_table(all_grids),
        "",
        "## 2. Focus cell — combined / kelly / 25 bps",
        "",
        md_table(focus),
        "",
        "Analyst-component shuffle (permutes direction*strength within "
        "quarter among covered events; base signal and fitted Kelly maps "
        "held fixed):",
        "",
        md_table(shuffle_df),
        "",
        "## 3. Classification flips on covered events (base -> overlay)",
        "",
        md_table(flips),
        "",
        "## 4. Covered-subsample like-for-like (same events, "
        "combined/kelly/25)",
        "",
        md_table(covered_cmp),
        "",
        "## 5. FM tercile diagnostic, covered quarters only (context for the "
        "adoption bar: dev FM t >= incumbent + 0.5)",
        "",
        md_table(fm_diag),
        "",
        "## 6. POST-HOC sensitivity (not pre-registered; logged as a "
        "researcher degree of freedom)",
        "",
        "Soft calls only — full-strength calls zeroed. No shuffle p is "
        "computed for this branch.",
        "",
        md_table(posthoc),
        "",
        f"**Verdict: DIAGNOSTIC ONLY — not adopted.** {ADOPTION_RULE}",
    ]
    (bs.OUTPUTS_DIR / "OVERLAY.md").write_text("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
