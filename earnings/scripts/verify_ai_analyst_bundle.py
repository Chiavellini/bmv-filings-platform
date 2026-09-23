#!/usr/bin/env python3
"""Independently verify timing, bars, outcomes, signals, and headline metrics."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


COST = 25 / 10_000
LABEL_TO_POSITION = {"Positive": 1, "Negative": -1, "N to P": 1, "N to N": -1}
OVERRIDES = {
    ("oma", "2025-1T"): {
        "report_date": date(2025, 4, 28),
        "report_session": "after_close",
        "report_timestamp": "2025-04-28 18:17 America/Mexico_City",
        "timing_source": "BMV issuer relevant-events record",
        "report_source_reference": "https://www.bmv.com.mx/es/emisoras/eventosrelevantes/OMA-6707-CGEN_CAPIT",
    },
    ("volaris", "2025-1T"): {
        "report_date": date(2025, 4, 28),
        "report_session": "pre_open",
        "report_timestamp": "2025-04-28 08:03:13 America/New_York (06:03:13 America/Mexico_City)",
        "timing_source": "SEC EDGAR accepted timestamp",
        "report_source_reference": "https://www.sec.gov/Archives/edgar/data/1520504/000129281425001665",
    },
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def close(left: pd.Series, right: pd.Series, label: str, tolerance: float = 1e-12) -> None:
    delta = (left.astype(float) - right.astype(float)).abs()
    require(bool((delta <= tolerance).all()), f"{label}: max difference {delta.max()}")


def provider_metrics(frame: pd.DataFrame, provider: str) -> dict:
    position = frame[f"{provider}_position"].astype("Int64")
    available = frame[f"{provider}_response_available"].fillna(False).astype(bool)
    active = position.notna() & position.ne(0)
    correct = active & position.eq(frame["recomputed_realized_position"])
    gross = position.astype(float) * frame["recomputed_raw_ret"]
    net = gross - COST
    return {
        "events": int(len(frame)),
        "responses": int(available.sum()),
        "directional_calls": int(active.sum()),
        "abstentions": int((available & position.eq(0)).sum()),
        "missing": int((~available).sum()),
        "correct_directional": int(correct.sum()),
        "directional_hit_rate": float(correct.sum() / active.sum()) if active.any() else None,
        "mean_gross_return": float(gross[active].mean()) if active.any() else None,
        "mean_net_return_25bps": float(net[active].mean()) if active.any() else None,
        "mean_net_return_per_responded_event": float(
            np.where(active[available], net[available], 0).mean()
        ) if available.any() else None,
        "gross_trade_win_rate": float((gross[active] > 0).mean()) if active.any() else None,
    }


def paired(frame: pd.DataFrame, left: str, right: str) -> dict:
    active = frame[f"{left}_position"].fillna(0).ne(0) & frame[f"{right}_position"].fillna(0).ne(0)
    x = frame.loc[active]
    realized = x["recomputed_realized_position"]
    lc = x[f"{left}_position"].eq(realized)
    rc = x[f"{right}_position"].eq(realized)
    left_only = int((lc & ~rc).sum())
    right_only = int((~lc & rc).sum())
    discordant = left_only + right_only
    tail = sum(math.comb(discordant, k) for k in range(min(left_only, right_only) + 1))
    return {
        "both_directional_n": int(len(x)),
        "both_correct": int((lc & rc).sum()),
        f"{left}_only_correct": left_only,
        f"{right}_only_correct": right_only,
        "both_wrong": int((~lc & ~rc).sum()),
        "direction_agreement": float(x[f"{left}_position"].eq(x[f"{right}_position"]).mean()),
        "mcnemar_exact_two_sided_p": min(1.0, 2 * tail / (2**discordant)) if discordant else 1.0,
    }


def compare_metric_block(actual: dict, expected: dict, label: str) -> None:
    for provider, metrics in actual.items():
        for key, value in metrics.items():
            stored = expected[provider][key]
            if isinstance(value, float):
                require(math.isclose(value, stored, rel_tol=0, abs_tol=1e-12), f"{label}.{provider}.{key}")
            else:
                require(value == stored, f"{label}.{provider}.{key}")


def build_timing(frame: pd.DataFrame, calendar_path: Path, index: pd.DataFrame) -> pd.DataFrame:
    calendar = pd.read_parquet(calendar_path)[["slug", "period", "cal_date", "session"]]
    calendar = calendar.drop_duplicates(["slug", "period"]).rename(columns={
        "cal_date": "verified_report_date", "session": "verified_report_session",
    })
    calendar["verified_report_date"] = pd.to_datetime(calendar["verified_report_date"]).dt.date
    calendar["verified_timing_source"] = "frozen human analyst calendar"
    calendar["verified_report_timestamp"] = pd.NA
    calendar["verified_report_source_reference"] = (
        "earnings/configs/analyst_calendar.yaml -> data/analyst/analyst_expectation_2026-07-28.xlsx"
    )
    out = frame.merge(calendar, on=["slug", "period"], how="left", validate="one_to_one")
    for (slug, period), values in OVERRIDES.items():
        mask = out["slug"].eq(slug) & out["period"].eq(period)
        require(int(mask.sum()) == 1, f"missing override target {slug} {period}")
        out.loc[mask, "verified_report_date"] = values["report_date"]
        out.loc[mask, "verified_report_session"] = values["report_session"]
        out.loc[mask, "verified_report_timestamp"] = values["report_timestamp"]
        out.loc[mask, "verified_timing_source"] = values["timing_source"]
        out.loc[mask, "verified_report_source_reference"] = values["report_source_reference"]

    require(not out["verified_report_session"].isna().any(), "unresolved report session")
    trading_dates = sorted(pd.to_datetime(index["date"]).dt.date.unique().tolist())
    trading_set = set(trading_dates)

    def expected(row: pd.Series) -> date:
        report_date = row["verified_report_date"]
        if row["verified_report_session"] == "pre_open":
            require(report_date in trading_set, f"pre-open non-trading report date {report_date}")
            return report_date
        require(row["verified_report_session"] == "after_close", "unknown report session")
        return next(value for value in trading_dates if value > report_date)

    out["independent_expected_t0"] = out.apply(expected, axis=1)
    out["packaged_t0_date"] = pd.to_datetime(out["t0"]).dt.date
    out["independent_timing_verified"] = out["independent_expected_t0"].eq(out["packaged_t0_date"])
    require(bool(out["independent_timing_verified"].all()), "one or more packaged t0 dates are wrong")
    require(int(out["verified_report_session"].eq("after_close").sum()) == 48, "after-close count")
    require(int(out["verified_report_session"].eq("pre_open").sum()) == 15, "pre-open count")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--calendar", type=Path, required=True)
    parser.add_argument("--timing-audit", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--analyst-predictions", type=Path, required=True)
    parser.add_argument("--claude-reads", type=Path, required=True)
    parser.add_argument("--luna-responses", type=Path, required=True)
    parser.add_argument("--luna-manifest", type=Path, required=True)
    parser.add_argument("--calendar-config", type=Path, required=True)
    args = parser.parse_args()

    parquet = pd.read_parquet(args.bundle_dir / "trades_63.parquet")
    csv = pd.read_csv(args.bundle_dir / "trades_63.csv")
    require(len(parquet) == 63 and not parquet.duplicated(["slug", "period"]).any(), "63 unique export rows")
    require(set(map(tuple, parquet[["slug", "period"]].to_numpy())) == set(map(tuple, csv[["slug", "period"]].to_numpy())), "CSV/Parquet key parity")
    csv_sorted = csv.sort_values(["slug", "period"]).reset_index(drop=True)
    parquet_sorted = parquet.sort_values(["slug", "period"]).reset_index(drop=True)
    for column in [
        "price_open_t0", "price_close_t0", "volume_t0", "index_open_t0",
        "index_close_t0", "raw_ret", "ar0_intra", "analyst_position",
        "luna_position", "analyst_trade_net_return_25bps",
        "luna_trade_net_return_25bps",
    ]:
        close(csv_sorted[column], parquet_sorted[column], f"CSV/Parquet {column}")
    for column in ["ticker", "period", "t0", "report_session", "expected_t0", "label_raw", "luna_analyst_label"]:
        require(bool(csv_sorted[column].astype(str).eq(parquet_sorted[column].astype(str)).all()), f"CSV/Parquet {column}")

    index = pd.read_parquet(args.index)
    audit = build_timing(parquet, args.calendar, index)

    timing_audit = pd.read_csv(args.timing_audit)
    covered = audit.merge(
        timing_audit[["slug", "period", "session_excel", "cal_date", "excel_t0"]],
        on=["slug", "period"], how="left", indicator=True,
    )
    require(int(covered["_merge"].eq("both").sum()) == 61, "human timing audit coverage must be 61")
    human = covered[covered["_merge"].eq("both")]
    require(bool(human["packaged_t0_date"].astype(str).eq(human["excel_t0"].astype(str)).all()), "human-calendar t0 parity")
    require(bool(human["verified_report_session"].eq(human["session_excel"]).all()), "human-calendar session parity")
    missing_keys = set(map(tuple, covered.loc[covered["_merge"].ne("both"), ["slug", "period"]].to_numpy()))
    require(missing_keys == set(OVERRIDES), f"unexpected timing-audit gaps: {missing_keys}")

    prices = pd.read_parquet(args.prices)[["symbol", "date", "open", "close", "volume"]]
    prices["date"] = pd.to_datetime(prices["date"]).dt.date
    stock = prices.drop_duplicates(["symbol", "date"]).rename(columns={
        "open": "verified_stock_open", "close": "verified_stock_close", "volume": "verified_volume",
    })
    idx = index[["date", "open", "close"]].copy()
    idx["date"] = pd.to_datetime(idx["date"]).dt.date
    idx = idx.drop_duplicates(["date"]).rename(columns={"open": "verified_index_open", "close": "verified_index_close"})
    audit = audit.merge(stock, left_on=["symbol", "packaged_t0_date"], right_on=["symbol", "date"], validate="one_to_one")
    audit = audit.merge(idx, left_on="packaged_t0_date", right_on="date", validate="many_to_one", suffixes=("_stock", "_index"))
    require(len(audit) == 63, "all 63 stock and index bars must exist")
    audit["recomputed_raw_ret"] = audit["verified_stock_close"] / audit["verified_stock_open"] - 1
    audit["recomputed_index_ret"] = audit["verified_index_close"] / audit["verified_index_open"] - 1
    audit["recomputed_ar0_intra"] = audit["recomputed_raw_ret"] - audit["recomputed_index_ret"]
    audit["recomputed_realized_position"] = np.sign(audit["recomputed_ar0_intra"]).astype(int)
    close(audit["price_open_t0"], audit["verified_stock_open"], "stock open")
    close(audit["price_close_t0"], audit["verified_stock_close"], "stock close")
    close(audit["volume_t0"], audit["verified_volume"], "volume")
    close(audit["index_open_t0"], audit["verified_index_open"], "index open")
    close(audit["index_close_t0"], audit["verified_index_close"], "index close")
    close(audit["raw_ret"], audit["recomputed_raw_ret"], "raw return")
    close(audit["ar0_intra"], audit["recomputed_ar0_intra"], "abnormal return")
    close(audit["source_ar0_intra"], audit["recomputed_ar0_intra"], "frozen abnormal return")

    decisions = pd.read_parquet(args.decisions)
    decisions = decisions[decisions["strength"] > 0][["slug", "period", "direction", "strength", "t0"]]
    require(len(decisions) == 63, "canonical decisions selection")
    joined = audit.merge(decisions, on=["slug", "period"], suffixes=("", "_decision"), validate="one_to_one")
    require(bool(joined["analyst_position"].astype(int).eq(joined["direction"].astype(int)).all()), "analyst direction source parity")
    close(joined["analyst_strength"], joined["strength"], "analyst strength")
    require(bool(pd.to_datetime(joined["t0_decision"]).dt.date.eq(joined["packaged_t0_date"]).all()), "decision t0 parity")

    analyst = pd.read_parquet(args.analyst_predictions)[["slug", "period", "label_raw"]].drop_duplicates(["slug", "period"])
    joined = joined.merge(analyst, on=["slug", "period"], suffixes=("", "_source"), validate="one_to_one")
    require(bool(joined["label_raw"].eq(joined["label_raw_source"]).all()), "analyst label source parity")
    require(bool(joined["analyst_position"].astype(int).eq(joined["label_raw"].map(LABEL_TO_POSITION)).all()), "analyst label mapping")

    claude = pd.read_parquet(args.claude_reads)
    claude = claude[["slug", "period", "b_direction", "b_rationale"]]
    joined = joined.merge(claude, on=["slug", "period"], how="left", validate="one_to_one")
    response_rows = joined["b_direction"].notna()
    require(int(response_rows.sum()) == 34, "Claude response count")
    require(bool(joined.loc[response_rows, "claude_direction"].eq(joined.loc[response_rows, "b_direction"]).all()), "Claude direction parity")
    require(bool(joined.loc[response_rows, "claude_rationale"].eq(joined.loc[response_rows, "b_rationale"]).all()), "Claude rationale parity")

    luna_rows = []
    for path in sorted(args.luna_responses.glob("*.json")):
        slug, period = path.stem.split("__", 1)
        payload = json.loads(path.read_text(encoding="utf-8"))
        luna_rows.append({"slug": slug, "period": period, **payload})
    luna = pd.DataFrame(luna_rows)
    require(len(luna) == 63 and not luna.duplicated(["slug", "period"]).any(), "Luna response count")
    joined = joined.merge(luna[["slug", "period", "analyst_label", "direction", "rationale"]], on=["slug", "period"], suffixes=("", "_luna_source"), validate="one_to_one")
    require(bool(joined["luna_analyst_label"].eq(joined["analyst_label"]).all()), "Luna label source parity")
    require(bool(joined["luna_direction"].eq(joined["direction_luna_source"]).all()), "Luna direction source parity")
    require(bool(joined["luna_rationale"].eq(joined["rationale"]).all()), "Luna rationale source parity")
    require(bool(joined["luna_analyst_label"].map(LABEL_TO_POSITION).eq(joined["luna_position"].astype(int)).all()), "Luna forced-label mapping")
    require(bool(joined["luna_position"].ne(0).all()), "Luna has no abstentions")

    for provider in ("analyst", "claude", "luna"):
        expected_correct = joined[f"{provider}_position"].eq(joined["recomputed_realized_position"])
        active = joined[f"{provider}_position"].notna() & joined[f"{provider}_position"].ne(0)
        require(bool(joined.loc[active, f"{provider}_correct"].astype(bool).eq(expected_correct[active]).all()), f"{provider} correctness")
        expected_gross = joined[f"{provider}_position"].astype(float) * joined["recomputed_raw_ret"]
        close(joined.loc[active, f"{provider}_trade_gross_return"], expected_gross[active], f"{provider} gross")
        close(joined.loc[active, f"{provider}_trade_net_return_25bps"], expected_gross[active] - COST, f"{provider} net")

    summary = json.loads((args.bundle_dir / "summary.json").read_text(encoding="utf-8"))
    all_actual = {p: provider_metrics(joined, p) for p in ("analyst", "claude", "luna")}
    compare_metric_block(all_actual, summary["all_63"], "all_63")
    full = joined[joined["analyst_strength_label"].eq("full")]
    soft = joined[joined["analyst_strength_label"].eq("soft")]
    shared = joined[joined["claude_position"].fillna(0).ne(0)]
    require((len(full), len(soft), len(shared)) == (27, 36, 26), "comparison-set sizes")
    compare_metric_block({p: provider_metrics(full, p) for p in ("analyst", "claude", "luna")}, summary["analyst_full_strength_27"], "full")
    compare_metric_block({p: provider_metrics(soft, p) for p in ("analyst", "claude", "luna")}, summary["analyst_soft_strength_36"], "soft")
    compare_metric_block({p: provider_metrics(shared, p) for p in ("analyst", "claude", "luna")}, summary["claude_directional_26"], "claude26")
    for actual, key in [
        (paired(joined, "analyst", "luna"), "paired_analyst_luna_all_63"),
        (paired(shared, "claude", "luna"), "paired_claude_luna_on_26"),
        (paired(shared, "analyst", "claude"), "paired_analyst_claude_on_26"),
    ]:
        for name, value in actual.items():
            stored = summary[key][name]
            require(math.isclose(value, stored, abs_tol=1e-12) if isinstance(value, float) else value == stored, f"{key}.{name}")

    label_summary = summary["luna_analyst_labels"]
    label_counts = {str(k): int(v) for k, v in joined["luna_analyst_label"].value_counts().sort_index().items()}
    require(label_counts == label_summary["counts"], "Luna label counts")
    require(int(joined["luna_analyst_label"].eq(joined["label_raw"]).sum()) == label_summary["exact_label_matches"], "Luna exact label matches")
    require(math.isclose(float(joined["luna_position"].eq(joined["analyst_position"]).mean()), label_summary["direction_agreement_with_analyst"], abs_tol=1e-12), "Luna direction agreement")
    require(math.isclose(float(joined["luna_strength_label"].eq(joined["analyst_strength_label"]).mean()), label_summary["strength_agreement_with_analyst"], abs_tol=1e-12), "Luna strength agreement")

    run_manifest = json.loads(args.luna_manifest.read_text(encoding="utf-8"))
    token_keys = ["input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"]
    usage = {key: sum(int(item.get("usage", {}).get(key, 0) or 0) for item in run_manifest["results"]) for key in token_keys}
    uncached = usage["input_tokens"] - usage["cached_input_tokens"]
    usage["estimated_luna_credits"] = round(
        uncached / 1_000_000 * 5
        + usage["cached_input_tokens"] / 1_000_000 * 0.5
        + usage["output_tokens"] / 1_000_000 * 30,
        2,
    )
    for key, value in usage.items():
        require(value == summary["luna_usage"][key], f"Luna usage {key}")
    require(run_manifest.get("model") == summary["luna_usage"]["model"], "Luna model identity")
    require(run_manifest.get("reasoning") == summary["luna_usage"]["reasoning"], "Luna reasoning identity")
    require(run_manifest.get("prompt_mode") == "analyst-forced", "Luna forced prompt mode")

    jsonl = [json.loads(line) for line in (args.bundle_dir / "ai_responses_63.jsonl").read_text(encoding="utf-8").splitlines() if line]
    require(len(jsonl) == 63, "AI JSONL row count")
    jsonl_map = {(row["event"]["slug"], row["event"]["period"]): row for row in jsonl}
    require(set(jsonl_map) == set(map(tuple, joined[["slug", "period"]].to_numpy())), "AI JSONL key parity")
    for _, row in joined.iterrows():
        record = jsonl_map[(row["slug"], row["period"])]
        require(record["luna"]["direction"] == row["luna_direction"], "AI JSONL Luna direction")
        require(record["luna"]["rationale"] == row["luna_rationale"], "AI JSONL Luna rationale")
        if row["claude_response_available"]:
            require(record["claude"]["direction"] == row["claude_direction"], "AI JSONL Claude direction")
            require(record["claude"]["rationale"] == row["claude_rationale"], "AI JSONL Claude rationale")
    html = (args.bundle_dir / "OPEN_ME_FIRST.html").read_text(encoding="utf-8")
    match = re.search(r'<script id="report-data" type="application/json">(.*?)</script>', html, re.S)
    require(match is not None and len(json.loads(match.group(1))) == 63, "HTML embedded event count")
    for fragment in [
        "Timing and arithmetic audit: PASS · 63/63", "43/63", "68.3% correct",
        "41/63", "65.1% correct", "p = 0.815", "Analyst +173, Claude +176, Luna +172",
        "25/27 (92.6%)", "24/27 (88.9%)", "23/27 (85.2%)", "22/27 (81.5%)",
        "AI self-reported confidence · directional calls only",
    ]:
        require(fragment in html, f"HTML headline fragment: {fragment}")

    bimbo = joined[(joined["slug"].eq("bimbo")) & (joined["period"].eq("2026-1T"))].iloc[0]
    bimbo_volume_multiple = bimbo["price_close_t0"] * bimbo["volume_t0"] / bimbo["median_peso_volume"]

    verification_columns = [
        "ticker", "slug", "symbol", "period", "verified_report_date",
        "verified_report_session", "verified_report_timestamp", "verified_timing_source",
        "verified_report_source_reference", "independent_expected_t0", "t0",
        "independent_timing_verified", "verified_stock_open", "verified_stock_close",
        "verified_volume", "verified_index_open", "verified_index_close",
        "recomputed_raw_ret", "recomputed_index_ret", "recomputed_ar0_intra",
        "recomputed_realized_position", "label_raw", "analyst_position", "analyst_correct",
        "claude_direction", "claude_position", "claude_correct", "luna_analyst_label",
        "luna_position", "luna_correct",
    ]
    joined[verification_columns].sort_values(["t0", "ticker"]).to_csv(args.bundle_dir / "verification_63.csv", index=False)

    verification_summary = {
        "status": "PASS",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "events": 63,
        "timing": {"verified": 63, "human_calendar": 61, "primary_record_overrides": 2, "after_close": 48, "pre_open": 15},
        "bars_and_returns_recomputed": 63,
        "signal_source_parity": {"analyst": 63, "claude_responses": 34, "luna": 63},
        "headline_metrics": {
            "analyst_hits": all_actual["analyst"]["correct_directional"],
            "luna_hits": all_actual["luna"]["correct_directional"],
            "claude_directional_events": int(len(shared)),
            "claude_hits_on_26": provider_metrics(shared, "claude")["correct_directional"],
        },
        "bimbo_2026_1t": {
            "t0": str(bimbo["t0"]), "open": float(bimbo["price_open_t0"]),
            "close": float(bimbo["price_close_t0"]), "raw_return": float(bimbo["recomputed_raw_ret"]),
            "market_adjusted_return": float(bimbo["recomputed_ar0_intra"]),
            "volume_multiple": float(bimbo_volume_multiple),
        },
        "source_sha256": {
            "report_calendar": sha256(args.calendar), "timing_audit": sha256(args.timing_audit),
            "prices": sha256(args.prices), "index": sha256(args.index),
            "decisions": sha256(args.decisions), "analyst_predictions": sha256(args.analyst_predictions),
            "claude_reads": sha256(args.claude_reads), "calendar_config": sha256(args.calendar_config),
            "luna_manifest": sha256(args.luna_manifest),
        },
    }
    (args.bundle_dir / "verification_summary.json").write_text(json.dumps(verification_summary, indent=2), encoding="utf-8")

    metrics = summary["all_63"]
    report = f"""# Timing and numbers verification

**Status: PASS — 63/63 event windows and all displayed headline metrics independently reproduced.**

## Reaction-session rule

- `after_close` (48 events): `t0` is the next date present in the frozen IPC trading calendar, strictly after the public report date.
- `pre_open` (15 events): `t0` is the report date itself, which must be present in the IPC trading calendar.
- No event uses an in-session XBRL upload time to choose an entry.

The frozen human analyst calendar covers 61 events, and all 61 packaged `t0` values exactly match its expected reaction date. Two events were absent from that workbook and were resolved from primary records:

- OMA 2025-1T: BMV records the earnings release at 18:17 Mexico City time on 2025-04-28, after the close; verified `t0` is 2025-04-29. [BMV record]({OVERRIDES[("oma", "2025-1T")]["report_source_reference"]})
- Volaris 2025-1T: SEC EDGAR accepted the results filing at 08:03:13 New York time / 06:03:13 Mexico City time on 2025-04-28, before the 08:30 BMV open; verified `t0` is 2025-04-28. [SEC accession]({OVERRIDES[("volaris", "2025-1T")]["report_source_reference"]})

`verification_63.csv` contains the complete event-by-event timing and price reconstruction.

## Independent numeric reconstruction

For every event, this verifier reloaded the frozen stock and IPC bars and recalculated:

1. stock return = `stock_close(t0) / stock_open(t0) - 1`;
2. IPC return = `IPC_close(t0) / IPC_open(t0) - 1`;
3. scoring return = stock return minus IPC return;
4. correctness = signal position equals the sign of the scoring return;
5. trade P&L = signal position × raw stock return, minus 25 bps for an active trade.

All 63 packaged opens, closes, volumes, raw returns, market-adjusted returns, outcomes, provider correctness flags, and net returns match these independent calculations within `1e-12`.

## Reproduced headline results

| Set | Analyst | Claude | Luna |
|---|---:|---:|---:|
| All 63 hit rate | {metrics['analyst']['correct_directional']}/63 ({metrics['analyst']['directional_hit_rate']:.1%}) | 19/26 directional; 34 responses | {metrics['luna']['correct_directional']}/63 ({metrics['luna']['directional_hit_rate']:.1%}) |
| All 63 net bps/event | {metrics['analyst']['mean_net_return_per_responded_event']*10000:+.0f} | not comparable at full coverage | {metrics['luna']['mean_net_return_per_responded_event']*10000:+.0f} |
| Exact Claude 26 hit rate | 18/26 | 19/26 | 21/26 |
| Exact Claude 26 net bps/event | +173 | +176 | +172 |

The full/soft tier metrics, paired contingency counts, and McNemar exact p-value in `summary.json` were also recomputed and matched exactly.

## BIMBO 2026-1T spot check

- Report session: after close on 2026-04-29; reaction `t0`: 2026-04-30.
- Stock open MXN {bimbo['price_open_t0']:.2f}; close MXN {bimbo['price_close_t0']:.2f}; raw open-to-close return {bimbo['recomputed_raw_ret']:+.2%}.
- IPC-adjusted open-to-close return {bimbo['recomputed_ar0_intra']:+.2%}.
- Event peso volume was {bimbo_volume_multiple:.2f}× the stored pre-event median.

## Source and packaging checks

- Analyst directions/strengths match the canonical 63-row decisions source; analyst labels match the frozen analyst predictions source.
- Claude's 34 available directions and rationales match the frozen Claude reads source; 26 are directional and 8 are flat.
- Luna has 63 source JSON responses, all using one of the four analyst labels and none abstaining.
- CSV and Parquet contain the same 63 unique keys; AI JSONL and embedded HTML each contain 63 events.
- `verification_summary.json` records SHA-256 hashes for every frozen source used by this audit.

## Scope limitation

This proves internal arithmetic, source parity, and correct reaction-session mapping under the frozen calendar. The 61 workbook-covered releases were not each re-sourced from issuer websites during this pass; their source of truth is the human-curated calendar with the checksum recorded in `earnings/configs/analyst_calendar.yaml`. The two calendar gaps were independently resolved from the primary BMV and SEC records above.
"""
    (args.bundle_dir / "TIMING_AND_NUMBERS_VERIFICATION.md").write_text(report, encoding="utf-8")
    print(json.dumps(verification_summary, indent=2))


if __name__ == "__main__":
    main()
