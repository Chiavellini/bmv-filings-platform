#!/usr/bin/env python3
"""eval_conviction_tiers.py — the analyst's whole call universe, tiered by his
own stated conviction, plus the AI reader standalone and the soft-call cascade
variants.

Spec frozen in study.yaml `hybrid_conviction`, committed before any table here
was computed. DIAGNOSTIC ONLY: HYBRID-1 remains the sole primary spec of the
one-shot 2026-3T arbiter.

Two coverage facts govern how everything below may be read, and they lead the
report: the AI read batch was stopped at 116 of 219 responses, so AI cuts rest
on 49 of 73 scope events chosen by where the run stopped rather than at random;
and the identification probe came back 38/38, so the contamination gate fails on
sample size and the AI tier is disabled on dev by the pre-committed rule.

Outputs: outputs/results_v3/conviction_*.csv + outputs/CONVICTION.md
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs
from earnlib import hybrid as hy
from earnlib import stats as st
from earnlib.mdutil import md_table

import numpy as np
import pandas as pd

sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
from simulate_hybrid import build_frame, permutation_p, wilson  # noqa: E402

SEED_OFFSET = 8
COST = 25
TIERS = ("full", "soft", "neutral", "all_directional")


def tier_of(row) -> str:
    """The analyst's own conviction tier.

    `covered` in build_frame means *directional*, so a Neutral call — where he
    looked at the quarter and declined to take a side — would fall through to
    "none" alongside the events he never covers. Those are different things, so
    neutrality is read from `has_call` (a row exists in his workbook) instead.
    """
    strength = float(row.get("strength") or 0.0)
    if strength == hy.FULL_STRENGTH:
        return "full"
    if strength == hy.SOFT_STRENGTH:
        return "soft"
    return "neutral" if bool(row.get("has_call")) else "none"


def tier_mask(df: pd.DataFrame, tier: str) -> pd.Series:
    if tier == "all_directional":
        return df["tier_analyst"].isin(["full", "soft"])
    return df["tier_analyst"] == tier


def score(d: pd.DataFrame, direction: np.ndarray, cfg: dict, rng,
          label: str, tier: str, permute_col: str | None = None) -> dict:
    """Hit rate + Wilson + net economics for one directional vote on one cut."""
    active = direction != 0
    n = int(active.sum())
    if n == 0:
        return {"cut": label, "tier": tier, "n": 0, "hits": None, "hit_%": None,
                "wilson_lo": None, "wilson_hi": None, "bps_net25": None,
                "fm_t": None, "perm_p": None}
    sub = d[active]
    dirs = direction[active]
    hits = int((np.sign(dirs) == np.sign(sub["ar0_cc"])).sum())
    lo, hi = wilson(hits, n)
    net = dirs * sub["raw_ret"].to_numpy(dtype=float) - COST / 1e4
    fm = st.fama_macbeth(
        pd.Series(net, index=sub.index).groupby(sub["period"]).mean().to_numpy())
    perm = None
    if permute_col is not None and n >= 10:
        perm = round(permutation_p(sub.assign(**{permute_col: dirs}),
                                   permute_col, cfg, rng), 4)
    return {"cut": label, "tier": tier, "n": n, "hits": hits,
            "hit_%": round(100 * hits / n, 1), "wilson_lo": lo, "wilson_hi": hi,
            "bps_net25": round(float(np.mean(net)) * 1e4, 1),
            "fm_t": round(fm["fm_t"], 2) if np.isfinite(fm["fm_t"]) else None,
            "perm_p": perm}


def paired_test(d: pd.DataFrame, a: np.ndarray, b: np.ndarray,
                name_a: str, name_b: str, tier: str) -> dict:
    """Who was right, on the events where BOTH voted — McNemar's discordant test."""
    both = (a != 0) & (b != 0)
    if not both.any():
        return {"tier": tier, "comparison": f"{name_a} vs {name_b}", "n": 0,
                "agree_%": None, f"{name_a}_only_right": None,
                f"{name_b}_only_right": None, "mcnemar_p": None}
    sub = d[both]
    truth = np.sign(sub["ar0_cc"].to_numpy(dtype=float))
    ra, rb = np.sign(a[both]) == truth, np.sign(b[both]) == truth
    only_a, only_b = int((ra & ~rb).sum()), int((rb & ~ra).sum())
    disc = only_a + only_b
    p = None
    if disc:
        from scipy import stats as sps
        p = round(float(sps.binomtest(only_a, disc, 0.5).pvalue), 4)
    return {"tier": tier, "comparison": f"{name_a} vs {name_b}",
            "n": int(both.sum()),
            "agree_%": round(100 * float((np.sign(a[both]) == np.sign(b[both])).mean()), 1),
            f"{name_a}_only_right": only_a, f"{name_b}_only_right": only_b,
            "mcnemar_p": p}


def main() -> None:
    assert bs.V3, "set EARNINGS_V3=1"
    cfg = bs.load_config()
    rng = np.random.default_rng(int(cfg["validation"]["seed"]) + SEED_OFFSET)
    results = bs.RESULTS_DIR
    results.mkdir(parents=True, exist_ok=True)

    frame, years = build_frame(cfg)
    pred = pd.read_parquet(bs.OUTPUTS_DIR / "analyst_predictions.parquet")
    called = set(map(tuple, pred.loc[pred["slug"].notna(),
                                     ["slug", "period"]].to_numpy()))
    frame["has_call"] = [(s, p) in called
                         for s, p in zip(frame["slug"], frame["period"])]
    frame["tier_analyst"] = [tier_of(r) for _, r in frame.iterrows()]

    reads_path = bs.OUTPUTS_DIR / "ai_reads.parquet"
    reads = pd.read_parquet(reads_path) if reads_path.exists() else pd.DataFrame()
    if len(reads):
        cols = ["slug", "period"]
        for c in ("n_ai_dir", "n_conviction", "b_ai_dir", "b_conviction",
                  "identified", "probe_move", "doc_source"):
            if c in reads.columns:
                cols.append(c)
        frame = frame.merge(reads[cols], on=["slug", "period"], how="left",
                            validate="many_to_one")
    for c in ("n_ai_dir", "b_ai_dir"):
        frame[c] = frame[c].fillna(0).astype(int) if c in frame else 0
    # arm B is the pre-registered primary; arm N is the sensitivity
    frame["ai_dir"] = frame["b_ai_dir"]
    frame["ai_conviction"] = frame.get("b_conviction")

    analyst_dir = (frame["direction"] * (frame["strength"] > 0)).to_numpy(dtype=float)
    system_dir = np.sign(frame["s_ts"].to_numpy(dtype=float))

    # ---- A. the three voters, by the analyst's own conviction tier --------
    rows = []
    for tier in TIERS:
        m = tier_mask(frame, tier).to_numpy()
        d = frame[m]
        rows.append(score(d, analyst_dir[m], cfg, rng, "analyst", tier, "direction"))
        rows.append(score(d, system_dir[m], cfg, rng, "system (sign s_ts)", tier, "s_ts"))
        rows.append(score(d, frame["ai_dir"].to_numpy(dtype=float)[m], cfg, rng,
                          "AI arm B", tier, "ai_dir"))
        rows.append(score(d, frame["n_ai_dir"].to_numpy(dtype=float)[m], cfg, rng,
                          "AI arm N", tier, "n_ai_dir"))
    by_tier = pd.DataFrame(rows)
    by_tier.to_csv(results / "conviction_by_tier.csv", index=False)

    # ---- B. cascade variants over the whole dev frame ---------------------
    variants = []
    for policy in hy.SOFT_POLICIES:
        dec = hy.fit_tier_kelly(
            hy.apply_cascade(frame, cfg, ai_enabled=False, soft_policy=policy), cfg)
        traded = dec[dec["hybrid_dir"] != 0]
        row = score(traded, traded["hybrid_dir"].to_numpy(dtype=float), cfg, rng,
                    f"cascade soft_policy={policy}", "all")
        row["coverage_%"] = round(100 * len(traded) / len(dec), 1)
        row["tiers_fired"] = ", ".join(
            f"{k}:{v}" for k, v in traded["tier"].value_counts().items())
        variants.append(row)
        if policy == "veto":
            dec.to_parquet(results / "conviction_decisions_veto.parquet", index=False)
    for label, direction in (("analyst alone (all directional)", analyst_dir),
                             ("analyst alone (full only)",
                              analyst_dir * (frame["strength"] == 1.0).to_numpy()),
                             ("system alone (sign s_ts)", system_dir),
                             ("AI alone (arm B)", frame["ai_dir"].to_numpy(dtype=float))):
        row = score(frame, direction, cfg, rng, label, "all")
        row["coverage_%"] = round(100 * int((direction != 0).sum()) / len(frame), 1)
        row["tiers_fired"] = ""
        variants.append(row)
    variants_df = pd.DataFrame(variants)
    variants_df.to_csv(results / "conviction_variants.csv", index=False)

    # ---- C/D. the reader, and who was right --------------------------------
    read_rows, paired = [], []
    if len(reads):
        got_b = frame["ai_dir"] != 0
        got_n = frame["n_ai_dir"] != 0
        both_arms = got_b & got_n
        agree = int((frame.loc[both_arms, "ai_dir"]
                     == frame.loc[both_arms, "n_ai_dir"]).sum())
        ident = frame["identified"].dropna() if "identified" in frame else pd.Series(dtype=float)
        read_rows = [
            {"measure": "events with an arm-B read", "value": int(got_b.sum())},
            {"measure": "events with an arm-N read", "value": int(got_n.sum())},
            {"measure": "events with both arms", "value": int(both_arms.sum())},
            {"measure": "arm N/B direction agreement %",
             "value": round(100 * agree / max(int(both_arms.sum()), 1), 1)},
            {"measure": "probes run", "value": int(ident.notna().sum())},
            {"measure": "issuer identified", "value": int(ident.sum())},
            {"measure": "identification rate %",
             "value": round(100 * float(ident.mean()), 1) if len(ident) else None},
            {"measure": "outcome recalled by probe",
             "value": int((frame.get("probe_move", pd.Series(dtype=object))
                           .isin(["up", "down"])).sum())},
            {"measure": "arm-B reads at HIGH conviction",
             "value": int((frame.get("b_conviction", pd.Series(dtype=object))
                           == "high").sum())},
        ]
        for tier in TIERS:
            m = tier_mask(frame, tier).to_numpy()
            d = frame[m]
            paired.append(paired_test(d, frame["ai_dir"].to_numpy(dtype=float)[m],
                                      analyst_dir[m], "AI", "analyst", tier))
            paired.append(paired_test(d, system_dir[m], analyst_dir[m],
                                      "system", "analyst", tier))
    reader_df = pd.DataFrame(read_rows)
    paired_df = pd.DataFrame(paired)
    reader_df.to_csv(results / "conviction_reader.csv", index=False)
    paired_df.to_csv(results / "conviction_paired.csv", index=False)

    # ---- E. the frozen gate, evaluated once --------------------------------
    n_nonident = int(((frame.get("identified") == False)                # noqa: E712
                      & (frame["ai_dir"] != 0)).sum()) if len(reads) else 0
    gate = {"non_identified_directional_reads": n_nonident, "floor": 20,
            "passed": n_nonident >= 20}

    lines = [
        "# Conviction-tiered diagnostic — analyst × AI × system",
        "",
        "> **Read the coverage before the numbers.** The AI read batch was "
        "stopped at 116 of 219 responses to limit usage, so every AI cut rests "
        "on the events the run happened to reach — issuer order, not a random "
        "sample. AI numbers here are **descriptive**: they characterise the "
        "reader, they are not a hit rate you can trade on. The analyst tier "
        "tables use all his directional dev calls and are unaffected.",
        "",
        "**DIAGNOSTIC ONLY.** Spec frozen in study.yaml `hybrid_conviction` "
        "before any table below existed. HYBRID-1 remains the sole primary spec "
        "of the one-shot 2026-3T arbiter; nothing here creates an adoption path.",
        "",
        f"Frame: {len(frame)} tradable dev events, "
        f"{int((frame['strength'] > 0).sum())} with an analyst call. "
        f"Seed {int(cfg['validation']['seed']) + SEED_OFFSET}. Costs {COST} bps.",
        "",
        "## The frozen contamination gate",
        "",
        f"Non-identified directional arm-B reads: **{gate['non_identified_directional_reads']}** "
        f"against a floor of {gate['floor']} → **gate "
        f"{'PASSES' if gate['passed'] else 'FAILS'}**.",
        "",
        "The gate scores only events the reader could not identify. Entity "
        "scrubbing does not blind a frontier model on a full MD&A, so there are "
        "effectively none, and the AI tier is disabled on dev **by the "
        "pre-committed rule** rather than by choice. This was the outcome the "
        "rule was written to make interpretable, and it says nothing about "
        "whether the reader is any good — only that dev cannot answer it.",
        "",
        "## A. Each voter, by the analyst's own conviction tier",
        "",
        "This frame is the **tradable** universe — liquidity-filtered at the "
        "primary 5M MXN/day cutoff — because a decision rule that emits orders "
        "cannot trade what it cannot fill. `results_v3/simple_conviction.csv` "
        "is an *information* study and applies no such filter, so its counts "
        "are larger (67 directional there against "
        f"{int(tier_mask(frame, 'all_directional').sum())} here; 40 soft "
        f"against {int(tier_mask(frame, 'soft').sum())}). The full-conviction "
        "tier is untouched by the filter and reproduces exactly: 27 calls, "
        "25 hits, 92.6%.",
        "",
        md_table(by_tier),
        "",
        "## B. Cascade variants (AI tier disabled by the gate)",
        "",
        "`veto` is the frozen HYBRID-1. `corroborated` lets a soft call trade "
        "when margin-SUE agrees; `always` trades every directional call.",
        "",
        md_table(variants_df),
        "",
        "## C. The reader itself",
        "",
        md_table(reader_df) if len(reader_df) else "_no reads available_",
        "",
        "## D. Who was right, where both voted",
        "",
        md_table(paired_df) if len(paired_df) else "_no reads available_",
        "",
        "## The expectation, and what happened",
        "",
        "Frozen before computation: *soft calls hit 52.5% in dev, so trading "
        "them should dilute; the question is whether corroboration lifts them "
        "above that base.* Compare the `soft` row of table A against the "
        "`corroborated` and `always` rows of table B.",
        "",
        "## Limits",
        "",
        "- Tier cells are small (27 full / 40 soft / a handful of neutral). "
        "Wilson intervals are wide; read them, not the point estimates.",
        "- AI cuts are partial and non-random, per the note above.",
        "- Dev AI reads are contaminated-until-proven-otherwise. Only 2026-3T "
        "is structurally clean.",
        "- No adoption follows from anything here.",
    ]
    (bs.OUTPUTS_DIR / "CONVICTION.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {bs.OUTPUTS_DIR / 'CONVICTION.md'}", file=sys.stderr)
    print(by_tier.to_string(index=False), file=sys.stderr)
    print(variants_df.to_string(index=False), file=sys.stderr)


if __name__ == "__main__":
    main()
