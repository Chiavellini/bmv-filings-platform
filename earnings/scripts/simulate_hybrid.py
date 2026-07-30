#!/usr/bin/env python3
"""simulate_hybrid.py — evaluate HYBRID-1 (system + analyst + AI) on dev.

Spec: study.yaml `hybrid`, frozen and committed before any AI read executed.
DIAGNOSTIC ONLY — no adoption follows from these numbers. The arbiter is the
one-shot 2026-3T walk-forward, for which HYBRID-1 is the pre-registered PRIMARY
candidate.

The script runs with or without the AI channel. When outputs/ai_reads.parquet is
absent (or present but failing the frozen contamination gate), tier T2 is
disabled and the cascade reduces to analyst + system — which is the outcome the
gate itself pre-committed to, not a degraded fallback invented after the fact.

Outputs: outputs/results_v3/hybrid_*.csv + outputs/HYBRID.md
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import hybrid as hy
from earnlib import stats as st
from earnlib import study
from earnlib.mdutil import md_table

import numpy as np
import pandas as pd

sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
from run_refinements import add_margin_sue          # noqa: E402
from simulate_strategy import trade_returns          # noqa: E402

SEED_OFFSET = 7
COSTS = (0, 25, 50)


# --------------------------------------------------------------------------
# frame assembly
# --------------------------------------------------------------------------

def build_frame(cfg: dict) -> tuple[pd.DataFrame, float]:
    """Scope events with trade returns and the analyst's calls attached.

    Same liquidity-filtered tradable universe the strategy simulations use — an
    information study can skip the filter (eval_analyst_edge does), but a
    decision rule that emits orders cannot.
    """
    panel = study.load_panel(cfg)
    ew = study.assemble_events(cfg, panel)
    ew = add_margin_sue(ew, cfg)
    prices = pd.read_parquet(bs.art_path("prices"))
    dev = study.apply_liquidity(
        study.dev_sample(ew, cfg),
        cfg["liquidity"]["primary_min_median_peso_volume"])
    tr = trade_returns(dev, prices)
    tr = tr[tr["s_ts"].notna() & tr["sigma_pre"].notna()
            & tr["raw_ret"].notna()].copy()

    holdout = set(cfg["holdout"]["quarters"]) | set(
        cfg.get("phase_d", {}).get("historical_holdout_quarters", []))
    assert not set(tr["period"]) & holdout, "holdout quarter leaked into dev"

    pred = pd.read_parquet(bs.OUTPUTS_DIR / "analyst_predictions.parquet")
    pred = pred[pred["slug"].notna()]
    frame = tr.merge(pred[["slug", "period", "direction", "strength"]],
                     on=["slug", "period"], how="left", validate="many_to_one")
    frame["direction"] = frame["direction"].fillna(0).astype(int)
    frame["strength"] = frame["strength"].fillna(0.0)
    frame["covered"] = frame["strength"] > 0

    span = pd.to_datetime(frame["t0"])
    years = max((span.max() - span.min()).days / 365.25, 1e-9)
    return frame.reset_index(drop=True), float(years)


def attach_reads(frame: pd.DataFrame, arm: str) -> tuple[pd.DataFrame, dict]:
    """Join the AI reads for one arm; returns (frame, coverage report)."""
    path = bs.OUTPUTS_DIR / "ai_reads.parquet"
    if not path.exists():
        return frame, {"status": "absent", "n": 0}
    reads = pd.read_parquet(path)
    prefix = arm.lower()
    cols = {f"{prefix}_ai_dir": "ai_dir", f"{prefix}_conviction": "ai_conviction"}
    missing = [c for c in cols if c not in reads.columns]
    if missing:
        return frame, {"status": f"missing columns {missing}", "n": 0}
    keep = ["slug", "period", *cols]
    if "identified" in reads.columns:
        keep.append("identified")
    sub = reads[keep].rename(columns=cols)
    out = frame.merge(sub, on=["slug", "period"], how="left",
                      validate="many_to_one")
    out["ai_dir"] = out["ai_dir"].fillna(0).astype(int)
    n = int((out["ai_dir"] != 0).sum())
    return out, {"status": "joined", "n": n}


# --------------------------------------------------------------------------
# the frozen contamination gate
# --------------------------------------------------------------------------

def contamination_gate(frame: pd.DataFrame, cfg: dict, rng) -> dict:
    """Evaluate the pre-committed rule that decides whether tier T2 runs.

    Arm-B directional hit rate on NON-IDENTIFIED events must be >= 0.65 with a
    within-quarter permutation p < 0.05 on at least 20 reads. Failure disables
    the AI tier — that outcome is a finding, not a defect to iterate on.
    """
    need_n, need_hit, need_p = 20, 0.65, 0.05
    if "ai_dir" not in frame.columns or "identified" not in frame.columns:
        return {"evaluated": False, "reason": "no arm-B reads with probe",
                "passed": False}
    sub = frame[(frame["ai_dir"] != 0) & (frame["identified"] == False)]  # noqa: E712
    n = len(sub)
    if n == 0:
        return {"evaluated": False, "reason": "no non-identified reads",
                "passed": False}
    hits = (np.sign(sub["ai_dir"]) == np.sign(sub["ar0_cc"])).to_numpy()
    hit_rate = float(hits.mean())
    p = permutation_p(sub, "ai_dir", cfg, rng)
    passed = bool(n >= need_n and hit_rate >= need_hit and p < need_p)
    return {"evaluated": True, "n": n, "hit_rate": round(hit_rate, 3),
            "perm_p": round(p, 4), "passed": passed,
            "thresholds": f"n>={need_n}, hit>={need_hit}, p<{need_p}"}


def permutation_p(df: pd.DataFrame, col: str, cfg: dict, rng,
                  n_perm: int | None = None) -> float:
    """Within-quarter permutation of one channel's direction against outcomes."""
    n_perm = n_perm or int(cfg["hybrid"]["validation"]["n_perm"])
    sign_ret = np.sign(df["ar0_cc"].to_numpy(dtype=float))
    real = float((np.sign(df[col].to_numpy(dtype=float)) == sign_ret).mean())
    periods = df["period"].to_numpy()
    values = df[col].to_numpy(dtype=float)
    ge = 0
    for _ in range(n_perm):
        shuffled = values.copy()
        for q in np.unique(periods):
            mask = periods == q
            shuffled[mask] = rng.permutation(values[mask])
        if float((np.sign(shuffled) == sign_ret).mean()) >= real:
            ge += 1
    return (ge + 1) / (n_perm + 1)


# --------------------------------------------------------------------------
# tables
# --------------------------------------------------------------------------

def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    z, p = 1.96, k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(100 * (centre - half), 1), round(100 * (centre + half), 1))


def tier_table(d: pd.DataFrame, total: int) -> pd.DataFrame:
    rows = []
    for tier in hy.TIERS:
        sub = d[d["tier"] == tier]
        n = len(sub)
        if tier == hy.T4:
            rows.append({"tier": tier, "n": n,
                         "coverage_%": round(100 * n / total, 1),
                         "hits": None, "hit_%": None, "wilson_lo": None,
                         "wilson_hi": None, "bps_per_trade_net25": None,
                         "t": None})
            continue
        traded = sub[sub["hybrid_dir"] != 0]
        hits = int((np.sign(traded["hybrid_dir"]) ==
                    np.sign(traded["ar0_cc"])).sum())
        m = len(traded)
        lo, hi = wilson(hits, m)
        net = (traded["hybrid_dir"] * traded["raw_ret"] - 25 / 1e4).to_numpy(float)
        stat = st.mean_t(net) if m else {"mean": np.nan, "t": np.nan}
        rows.append({
            "tier": tier, "n": n, "coverage_%": round(100 * n / total, 1),
            "hits": hits, "hit_%": round(100 * hits / m, 1) if m else None,
            "wilson_lo": lo, "wilson_hi": hi,
            "bps_per_trade_net25": round(stat["mean"] * 1e4, 1) if m else None,
            "t": round(stat["t"], 2) if m else None})
    return pd.DataFrame(rows)


def like_for_like(d: pd.DataFrame) -> pd.DataFrame:
    """Hybrid vs each source alone, on the events the hybrid actually trades."""
    traded = d[d["hybrid_dir"] != 0]
    sign_ret = np.sign(traded["ar0_cc"].to_numpy(dtype=float))
    rows = []

    def add(name: str, direction: np.ndarray) -> None:
        active = direction != 0
        if not active.any():
            rows.append({"source": name, "n": 0, "hit_%": None})
            return
        hits = int((np.sign(direction[active]) == sign_ret[active]).sum())
        rows.append({"source": name, "n": int(active.sum()),
                     "hit_%": round(100 * hits / int(active.sum()), 1)})

    add("hybrid", traded["hybrid_dir"].to_numpy(dtype=float))
    add("analyst alone (any call)",
        (traded["direction"] * (traded["strength"] > 0)).to_numpy(dtype=float))
    add("system alone (sign s_ts)", np.sign(traded["s_ts"].to_numpy(dtype=float)))
    if "ai_dir" in traded.columns:
        add("AI alone", traded["ai_dir"].to_numpy(dtype=float))
    return pd.DataFrame(rows)


def strategy_table(d: pd.DataFrame, years: float, flat_cap: float) -> pd.DataFrame:
    traded = d[d["hybrid_dir"] != 0].copy()
    rows = []
    for sizing in ("kelly_tier", "flat"):
        weights = (traded["kelly_f"] if sizing == "kelly_tier"
                   else pd.Series(flat_cap, index=traded.index))
        for cost in COSTS:
            net = traded["hybrid_dir"] * traded["raw_ret"] - cost / 1e4
            per_day = []
            for _, g in traded.assign(net=net, w=weights).groupby("t0"):
                w = g["w"].to_numpy(dtype=float)
                if w.sum() > 1.0:
                    w = w / w.sum()
                per_day.append(float(np.sum(w * g["net"].to_numpy(dtype=float))))
            daily = np.array(per_day, dtype=float)
            mt = st.mean_t(net.to_numpy(dtype=float))
            sh = st.dual_sharpe(daily, years)
            fm = st.fama_macbeth(
                traded.assign(net=net).groupby("period")["net"].mean().to_numpy())
            rows.append({
                "sizing": sizing, "cost_bps": cost, "trades": len(traded),
                "bps_per_trade": round(mt["mean"] * 1e4, 1),
                "t_per_trade": round(mt["t"], 2),
                "fm_t": round(fm["fm_t"], 2),
                "sharpe_calendar": round(sh["sharpe_calendar"], 2)
                if np.isfinite(sh["sharpe_calendar"]) else None})
    return pd.DataFrame(rows)


def channel_permutations(d: pd.DataFrame, cfg: dict, rng) -> pd.DataFrame:
    """Which channel carries the result: permute each separately, in place."""
    rows = []
    traded = d[d["hybrid_dir"] != 0]
    for name, col in (("analyst_direction", "direction"),
                      ("s_ts", "s_ts"), ("ai_direction", "ai_dir")):
        if col not in traded.columns:
            continue
        sub = traded[traded[col].notna() & (traded[col] != 0)]
        if len(sub) < 10:
            rows.append({"channel": name, "n": len(sub), "hit_%": None,
                         "perm_p": None})
            continue
        hits = float((np.sign(sub[col].to_numpy(dtype=float)) ==
                      np.sign(sub["ar0_cc"].to_numpy(dtype=float))).mean())
        rows.append({"channel": name, "n": len(sub),
                     "hit_%": round(100 * hits, 1),
                     "perm_p": round(permutation_p(sub, col, cfg, rng), 4)})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------

def main() -> None:
    assert bs.V3, "set EARNINGS_V3=1"
    cfg = bs.load_config()
    seed = int(cfg["validation"]["seed"]) + SEED_OFFSET
    rng = np.random.default_rng(seed)
    results = bs.RESULTS_DIR
    results.mkdir(parents=True, exist_ok=True)

    frame, years = build_frame(cfg)
    frame, read_info = attach_reads(frame, arm=cfg["hybrid"]["contamination"]
                                    .get("primary_arm", "B"))
    gate = contamination_gate(frame, cfg, rng)
    ai_enabled = bool(gate.get("passed"))

    decided = hy.apply_cascade(frame, cfg, ai_enabled=ai_enabled)
    decided = hy.fit_tier_kelly(decided, cfg)
    decided.to_parquet(results / "hybrid_decisions.parquet", index=False)

    tiers = tier_table(decided, total=len(decided))
    lfl = like_for_like(decided)
    strat = strategy_table(decided, years,
                           float(cfg["hybrid"]["sizing"]["f_cap"]))
    perms = channel_permutations(decided, cfg, rng)
    conflicts = decided[(decided["tier"] == hy.T1) & decided["conflict"].notna()]
    conflict_rows = []
    for label, sub in (("analyst overruled system", conflicts[
            conflicts["conflict"].str.contains("system", na=False)]),
            ("analyst overruled AI", conflicts[
                conflicts["conflict"].str.contains("ai", na=False)])):
        if len(sub):
            hits = int((np.sign(sub["hybrid_dir"]) ==
                        np.sign(sub["ar0_cc"])).sum())
            conflict_rows.append({"cut": label, "n": len(sub), "hits": hits,
                                  "hit_%": round(100 * hits / len(sub), 1)})
        else:
            conflict_rows.append({"cut": label, "n": 0, "hits": None,
                                  "hit_%": None})
    conflict_tbl = pd.DataFrame(conflict_rows)
    vetoes = int(decided["vetoed"].sum())

    for name, table in (("hybrid_tiers", tiers), ("hybrid_like_for_like", lfl),
                        ("hybrid_strategy", strat),
                        ("hybrid_channel_perm", perms),
                        ("hybrid_conflicts", conflict_tbl)):
        table.to_csv(results / f"{name}.csv", index=False)

    # The title must name what actually ran. HYBRID-1 is the spec identifier and
    # always includes an AI tier; a report produced with that tier disabled is a
    # two-channel cascade and saying otherwise overclaims, however carefully the
    # body is caveated.
    title = ("# HYBRID-1 — system + analyst + AI decision cascade (dev)"
             if ai_enabled else
             "# HYBRID-1 spec, ANALYST + SYSTEM cascade only (dev)")
    lines = [
        title,
        "",
    ]
    if not ai_enabled:
        lines += [
            "> **The AI tier (T2) did not execute.** These results come from the "
            "two-channel cascade — the analyst's calls and the system's "
            "precision ladder. Nothing below is evidence about the AI channel "
            "in either direction.",
            "",
        ]
    lines += [
        "**DIAGNOSTIC ONLY.** Spec frozen in study.yaml `hybrid` and committed "
        "before any AI read executed; nothing here enters a traded spec. The "
        "arbiter is the one-shot 2026-3T walk-forward, where HYBRID-1 is the "
        "pre-registered PRIMARY candidate and phase_h / signal_layer / the "
        "analyst overlay / analyst_direction are pre-specified secondaries.",
        "",
        f"Scope: {len(decided)} tradable dev events "
        f"(liquidity-filtered), {int(decided['covered'].sum())} with an analyst "
        f"call. Seed {seed}. Costs {COSTS} bps.",
        "",
        "## AI channel status",
        "",
    ]
    if read_info["status"] != "joined":
        lines += [
            f"**The AI tier (T2) did not run: {read_info['status']}.** "
            "HYBRID-1 was evaluated with T2 disabled, which is exactly the "
            "reduced cascade the frozen gate pre-commits to when the AI channel "
            "is unavailable — analyst + system only. Every number below is that "
            "reduced spec; none of it is evidence about the AI channel either "
            "way.",
        ]
    else:
        lines += [f"Reads joined: {read_info['n']} directional. Gate: {gate}"]
        lines += ["", f"**AI tier {'ENABLED' if ai_enabled else 'DISABLED'}** "
                  "by the frozen contamination rule."]
    lines += [
        "",
        "## Per-tier coverage and precision",
        "",
        md_table(tiers),
        "",
        f"Soft-call vetoes fired: {vetoes}.",
        "",
        "## Like-for-like — hybrid vs each source alone",
        "",
        "On the events the hybrid actually trades, so the comparison is not "
        "confounded by different coverage.",
        "",
        md_table(lfl),
        "",
        "## Strategy",
        "",
        md_table(strat),
        "",
        "## Which channel carries it (within-quarter permutation)",
        "",
        md_table(perms),
        "",
        "## Conflict cut — T1 events where another source opposed the analyst",
        "",
        md_table(conflict_tbl),
        "",
        "## Caveats that lead every reading of this table",
        "",
        "- Small n. 27 full-strength analyst calls exist in the whole dev "
        "sample; tier cells are smaller still, and Wilson intervals are wide.",
        "- The AI channel is validated only on analyst-covered tickers by "
        "construction (study.yaml `hybrid.scope.coverage_caveat`).",
        "- Dev AI numbers, when they exist, are contaminated-until-proven-"
        "otherwise by model memory; only 2026-3T is structurally clean.",
        "- No adoption follows from anything above.",
    ]
    (bs.OUTPUTS_DIR / "HYBRID.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {bs.OUTPUTS_DIR / 'HYBRID.md'}", file=sys.stderr)
    print(tiers.to_string(index=False), file=sys.stderr)


if __name__ == "__main__":
    main()
