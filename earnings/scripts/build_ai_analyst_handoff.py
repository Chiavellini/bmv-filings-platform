#!/usr/bin/env python3
"""Build a dashboard-ready analyst/Claude/Luna comparison bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


DIRECTION_TO_POSITION = {"down": -1, "flat": 0, "up": 1}
COST = 25 / 10_000

# These two selected events are absent from the frozen analyst calendar workbook.
# Their report sessions are resolved from primary public records so the 63-event
# bundle never falls back to an XBRL timestamp or an inferred same-day window.
TIMING_OVERRIDES = {
    ("oma", "2025-1T"): {
        "report_date": date(2025, 4, 28),
        "report_session": "after_close",
        "report_timestamp": "2025-04-28 18:17 America/Mexico_City",
        "timing_source": "BMV issuer relevant-events record",
        "report_source_reference": (
            "https://www.bmv.com.mx/es/emisoras/eventosrelevantes/"
            "OMA-6707-CGEN_CAPIT"
        ),
    },
    ("volaris", "2025-1T"): {
        "report_date": date(2025, 4, 28),
        "report_session": "pre_open",
        "report_timestamp": (
            "2025-04-28 08:03:13 America/New_York "
            "(06:03:13 America/Mexico_City)"
        ),
        "timing_source": "SEC EDGAR accepted timestamp",
        "report_source_reference": (
            "https://www.sec.gov/Archives/edgar/data/1520504/"
            "000129281425001665"
        ),
    },
}


def attach_verified_timing(
    frame: pd.DataFrame, calendar_path: Path, index_path: Path
) -> pd.DataFrame:
    """Attach the canonical release session and independently derive t0."""
    calendar = pd.read_parquet(calendar_path)[
        ["slug", "period", "cal_date", "session"]
    ].drop_duplicates(["slug", "period"])
    calendar = calendar.rename(columns={
        "cal_date": "report_date", "session": "report_session",
    })
    calendar["report_date"] = pd.to_datetime(calendar["report_date"]).dt.date
    calendar["report_timestamp"] = pd.NA
    calendar["timing_source"] = "frozen human analyst calendar"
    calendar["report_source_reference"] = (
        "earnings/configs/analyst_calendar.yaml -> "
        "data/analyst/analyst_expectation_2026-07-28.xlsx"
    )

    out = frame.merge(calendar, on=["slug", "period"], how="left", validate="one_to_one")
    for (slug, period), values in TIMING_OVERRIDES.items():
        mask = out["slug"].eq(slug) & out["period"].eq(period)
        if int(mask.sum()) != 1:
            raise SystemExit(f"timing override key missing or duplicated: {slug} {period}")
        for column, value in values.items():
            out.loc[mask, column] = value

    timing_columns = [
        "report_date", "report_session", "timing_source",
        "report_source_reference",
    ]
    if out[timing_columns].isna().any().any():
        missing = out.loc[
            out[timing_columns].isna().any(axis=1), ["slug", "period"]
        ].to_dict("records")
        raise SystemExit(f"canonical timing missing for selected events: {missing}")
    if not out["report_session"].isin(["after_close", "pre_open"]).all():
        raise SystemExit("canonical timing contains an unsupported report session")

    trading_dates = sorted(
        pd.to_datetime(pd.read_parquet(index_path, columns=["date"])["date"])
        .dt.date.unique().tolist()
    )
    trading_set = set(trading_dates)

    def expected_t0(row: pd.Series) -> date:
        report_date = row["report_date"]
        if row["report_session"] == "pre_open":
            if report_date not in trading_set:
                raise SystemExit(
                    f"pre-open report date is not an IPC trading day: "
                    f"{row['slug']} {row['period']} {report_date}"
                )
            return report_date
        try:
            return next(value for value in trading_dates if value > report_date)
        except StopIteration as exc:
            raise SystemExit(f"no next IPC trading day after {report_date}") from exc

    out["expected_t0"] = out.apply(expected_t0, axis=1)
    packaged_t0 = pd.to_datetime(out["t0"]).dt.date
    out["timing_verified"] = packaged_t0.eq(out["expected_t0"])
    if not out["timing_verified"].all():
        bad = out.loc[
            ~out["timing_verified"],
            ["slug", "period", "report_date", "report_session", "t0", "expected_t0"],
        ]
        raise SystemExit(f"wrong reaction session(s):\n{bad.to_string(index=False)}")
    return out


def pct(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def bps(value: float | None) -> str:
    if value is None or not np.isfinite(value):
        return "n/a"
    return f"{value * 10_000:+.0f}"


def nullable_bool(condition: pd.Series, available: pd.Series) -> pd.Series:
    out = pd.Series(pd.NA, index=condition.index, dtype="boolean")
    out.loc[available] = condition.loc[available]
    return out


def wilson_interval(hits: int, trials: int, z: float = 1.96) -> list[float] | None:
    if not trials:
        return None
    p = hits / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    spread = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials**2)) / denominator
    return [center - spread, center + spread]


def load_luna(response_dir: Path) -> pd.DataFrame:
    rows = []
    for path in sorted(response_dir.glob("*.json")):
        slug, period = path.stem.split("__", 1)
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows.append({"slug": slug, "period": period, **{
            f"luna_{key}": value for key, value in payload.items()
        }})
    return pd.DataFrame(rows)


def provider_metrics(frame: pd.DataFrame, provider: str) -> dict:
    position = frame[f"{provider}_position"]
    responded = frame[f"{provider}_response_available"].fillna(False)
    traded = position.notna() & position.ne(0)
    correct = frame[f"{provider}_correct"].fillna(False)
    gross = frame.loc[traded, f"{provider}_trade_gross_return"]
    net = frame.loc[traded, f"{provider}_trade_net_return_25bps"]
    deployed = frame.loc[responded, f"{provider}_strategy_net_return_25bps"]
    hits = int((correct & traded).sum())
    trials = int(traded.sum())
    return {
        "events": int(len(frame)),
        "responses": int(responded.sum()),
        "directional_calls": trials,
        "abstentions": int((responded & position.eq(0)).sum()),
        "missing": int((~responded).sum()),
        "correct_directional": hits,
        "directional_hit_rate": (
            float(hits / trials) if trials else None
        ),
        "directional_hit_rate_wilson95": wilson_interval(hits, trials),
        "coverage_adjusted_hit_rate": float(hits / len(frame)),
        "mean_gross_return": float(gross.mean()) if len(gross) else None,
        "mean_net_return_25bps": float(net.mean()) if len(net) else None,
        "mean_net_return_per_responded_event": (
            float(deployed.mean()) if len(deployed) else None
        ),
        "gross_trade_win_rate": float((gross > 0).mean()) if len(gross) else None,
    }


def paired_split(frame: pd.DataFrame, left: str, right: str) -> dict:
    active = frame[f"{left}_position"].fillna(0).ne(0) & frame[
        f"{right}_position"
    ].fillna(0).ne(0)
    x = frame.loc[active]
    lc = x[f"{left}_correct"].fillna(False)
    rc = x[f"{right}_correct"].fillna(False)
    left_only = int((lc & ~rc).sum())
    right_only = int((~lc & rc).sum())
    discordant = left_only + right_only
    tail = sum(math.comb(discordant, k) for k in range(min(left_only, right_only) + 1))
    mcnemar_p = min(1.0, 2 * tail / (2**discordant)) if discordant else 1.0
    return {
        "both_directional_n": int(len(x)),
        "both_correct": int((lc & rc).sum()),
        f"{left}_only_correct": left_only,
        f"{right}_only_correct": right_only,
        "both_wrong": int((~lc & ~rc).sum()),
        "direction_agreement": (
            float((x[f"{left}_position"] == x[f"{right}_position"]).mean())
            if len(x) else None
        ),
        "mcnemar_exact_two_sided_p": mcnemar_p,
    }


def token_totals(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    totals = {
        "input_tokens": 0, "cached_input_tokens": 0,
        "output_tokens": 0, "reasoning_output_tokens": 0,
    }
    for result in manifest.get("results", []):
        for key in totals:
            totals[key] += int(result.get("usage", {}).get(key, 0) or 0)
    uncached = totals["input_tokens"] - totals["cached_input_tokens"]
    totals["estimated_luna_credits"] = round(
        uncached / 1_000_000 * 5
        + totals["cached_input_tokens"] / 1_000_000 * 0.5
        + totals["output_tokens"] / 1_000_000 * 30,
        2,
    )
    totals["model"] = manifest.get("model")
    totals["reasoning"] = manifest.get("reasoning")
    totals["prompt_mode"] = manifest.get("prompt_mode", "original")
    return totals


def markdown_metrics(metrics: dict[str, dict]) -> str:
    lines = [
        "| Reader | Responses | Directional | Abstained | Hits | Hit rate | Net bps/trade | Net bps/response |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    labels = {"analyst": "Analyst", "claude": "Claude B", "luna": "Luna B"}
    for name in ("analyst", "claude", "luna"):
        row = metrics[name]
        lines.append(
            f"| {labels[name]} | {row['responses']}/{row['events']} | "
            f"{row['directional_calls']} | {row['abstentions']} | "
            f"{row['correct_directional']} | {pct(row['directional_hit_rate'])} | "
            f"{bps(row['mean_net_return_25bps'])} | "
            f"{bps(row['mean_net_return_per_responded_event'])} |"
        )
    return "\n".join(lines)


def write_report(frame: pd.DataFrame, summary: dict, out: Path) -> None:
    all_metrics = summary["all_63"]
    shared = summary["claude_directional_26"]
    full = summary["analyst_full_strength_27"]
    soft = summary["analyst_soft_strength_36"]
    usage = summary.get("luna_usage", {})
    forced = usage.get("prompt_mode") == "analyst-forced"
    if forced:
        labels = summary["luna_analyst_labels"]
        interpretation = f"""- This is the corrected forced-choice test: analyst and Luna both made a
  directional call on all 63 events. Luna scored
  {all_metrics['luna']['correct_directional']}/63
  ({pct(all_metrics['luna']['directional_hit_rate'])}) versus the analyst's
  {all_metrics['analyst']['correct_directional']}/63
  ({pct(all_metrics['analyst']['directional_hit_rate'])}).
- Net of 25 bps, Luna earned {bps(all_metrics['luna']['mean_net_return_per_responded_event'])}
  bps per event versus the analyst's
  {bps(all_metrics['analyst']['mean_net_return_per_responded_event'])} bps.
- Luna used the same four-label vocabulary: `{json.dumps(labels['counts'], sort_keys=True)}`.
  Its exact four-label agreement with the analyst was
  {labels['exact_label_matches']}/63; direction agreement was
  {pct(labels['direction_agreement_with_analyst'])}.
- On the exact 26-event old-Claude directional set, Luna was
  {shared['luna']['correct_directional']}/26, Claude
  {shared['claude']['correct_directional']}/26, and the analyst
  {shared['analyst']['correct_directional']}/26. All three made a direction on
  every event in this comparison.
- McNemar's exact paired p-value for analyst versus Luna across all 63 events is
  {summary['paired_analyst_luna_all_63']['mcnemar_exact_two_sided_p']:.3f}."""
        shared_note = "Luna is forced to make a direction on every one of these event keys."
        prompt_note = (
            "the entity-scrubbed B arm with the exact analyst forced choices "
            "Positive, Negative, N to P, or N to N; no flat/neutral option"
        )
    else:
        interpretation = f"""- The analyst traded all 63 strict open-to-close windows at
  {bps(all_metrics['analyst']['mean_net_return_per_responded_event'])} net bps
  per event, with {full['analyst']['correct_directional']}/27 correct on the
  full-strength tier.
- Luna is selective in this sensitivity: it made
  {all_metrics['luna']['directional_calls']} directional calls and abstained
  {all_metrics['luna']['abstentions']} times. Its conditional hit rate was
  {pct(all_metrics['luna']['directional_hit_rate'])}.
- On the exact 26-event Claude set, use the table below; it includes idle cash
  for explicit abstentions and the same strict open-to-close window for every reader.
- Pairwise samples are small. McNemar's exact test is {summary['paired_analyst_luna_all_63']['mcnemar_exact_two_sided_p']:.3f}
  for analyst vs Luna on Luna's active calls; this run does not establish a
  statistically reliable accuracy difference."""
        shared_note = "Luna is evaluated on these same event keys, including any Luna abstention."
        prompt_note = "the original entity-scrubbed B arm, one independent call per event"
    report = f"""# Analyst vs Claude vs Luna: post-report day trading

Generated {summary['generated_at']}. This is a descriptive historical benchmark
on the frozen 63-event tradable analyst set.

## Headline: all 63 events

{markdown_metrics(all_metrics)}

The analyst strength split is deliberately kept visible: full-strength calls
were {full['analyst']['correct_directional']}/{full['analyst']['directional_calls']}
({pct(full['analyst']['directional_hit_rate'])}); soft calls were
{soft['analyst']['correct_directional']}/{soft['analyst']['directional_calls']}
({pct(soft['analyst']['directional_hit_rate'])}).

These strict intraday hit rates must not be confused with the original
pre-registered close-to-close research statistic. On the 27 analyst
full-conviction calls, the four relevant results are:

- original market-adjusted close-to-close hit rate: 25/27 (92.6%);
- strict market-relative open-to-close hit rate: 24/27 (88.9%);
- gross profitable open-to-close trades: 23/27 (85.2%);
- profitable open-to-close trades after 25 bps: 22/27 (81.5%).

The remembered 92.6% is therefore valid; the numbers differ because they answer
different questions. AI `high/medium/low` conviction is also a separate,
self-reported field and is broken out explicitly in `OPEN_ME_FIRST.html` and the
peer-ready Excel workbook.

## Timing gate: passed 63/63

The reaction date is not inferred from the XBRL upload time. For all 48
after-close releases, `t0` is the next actual IPC trading date; for all 15
pre-open releases, `t0` is the same trading date. Sixty-one mappings match the
frozen human analyst calendar exactly. Its two missing events, OMA 2025-1T and
Volaris 2025-1T, are resolved from primary BMV and SEC records. See
`TIMING_AND_NUMBERS_VERIFICATION.md` and `verification_63.csv`.

## What the result says

{interpretation}

## Exact old-Claude comparison set (26 events)

These are the 26 events where the old Claude B-arm response made a directional
call. {shared_note}

{markdown_metrics(shared)}

Paired split when both Claude and Luna made a directional call:
`{json.dumps(summary['paired_claude_luna_on_26'], sort_keys=True)}`.

## Run cost and reproducibility

- Model: `{usage.get('model', 'unknown')}`, reasoning: `{usage.get('reasoning', 'unknown')}`
- Input tokens: {usage.get('input_tokens', 0):,} ({usage.get('cached_input_tokens', 0):,} cached)
- Output tokens: {usage.get('output_tokens', 0):,}
- Estimated Luna credits from the documented per-token credit table: {usage.get('estimated_luna_credits', 'n/a')}
- Prompt: {prompt_note}
- Prompt mode recorded in the manifest: `{usage.get('prompt_mode', 'unknown')}`

## Interpretation limits

This is not a clean prospective AI test. The reports are historical, Claude's
identification probes recognized the issuers, and Luna may also possess latent
knowledge despite entity scrubbing. The batch is useful for descriptive model
comparison and dashboard integration, not causal proof of deployable edge.

The primary score and P&L in this bundle are strict next-session day trading:
direction accuracy uses `sign(ar0_intra)` (market-adjusted open-to-close on
`t0`) and P&L uses the stock's raw `open(t0)` to `close(t0)` return, less 25
bps. The older `ar0_cc`, `source_entry_rule`, and `source_raw_ret` fields are
retained only as labeled audit/reference columns; they are not used in the
headline results.
"""
    (out / "REPORT.md").write_text(report, encoding="utf-8")


def write_comparison_26(frame: pd.DataFrame, out: Path) -> None:
    x = frame[frame["claude_position"].fillna(0).ne(0)].copy()
    call = lambda d, c: "-" if pd.isna(d) else f"{d} ({c})"  # noqa: E731
    lines = [
        "# Event-level Claude vs Luna comparison",
        "",
        "Exact 26-event set: old Claude B-arm directional calls.",
        "",
        "| Issuer | Period | Outcome | Analyst | Claude | Luna | Winner(s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, row in x.sort_values(["period", "ticker"]).iterrows():
        winners = []
        for provider, label in (("analyst", "Analyst"), ("claude", "Claude"), ("luna", "Luna")):
            value = row.get(f"{provider}_correct")
            if pd.notna(value) and bool(value):
                winners.append(label)
        luna_label = row.get("luna_analyst_label")
        luna_call = (
            str(luna_label) if pd.notna(luna_label)
            else call(row["luna_direction"], row["luna_conviction"])
        )
        lines.append(
            f"| {row['ticker']} | {row['period']} | {row['realized_direction']} "
            f"({row['ar0_intra'] * 100:+.2f}%) | "
            f"{row['label_raw']} | "
            f"{call(row['claude_direction'], row['claude_conviction'])} | "
            f"{luna_call} | "
            f"{', '.join(winners) if winners else 'none'} |"
        )
    (out / "COMPARISON_26.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_responses(frame: pd.DataFrame, out: Path) -> None:
    lines = ["# Actual AI responses", ""]
    for _, row in frame.sort_values(["period", "ticker"]).iterrows():
        lines.extend([
            f"## {row['ticker']} {row['period']}", "",
            f"Outcome: **{row['realized_direction']}** ({row['ar0_intra'] * 100:+.2f}% "
            f"market-adjusted open-to-close; stock {row['raw_ret'] * 100:+.2f}%). "
            f"Analyst: **{row['label_raw']}** ({row['analyst_direction']}, "
            f"{row['analyst_strength_label']}).", "",
        ])
        if row["claude_response_available"]:
            lines.extend([
                f"Claude B: **{row['claude_direction']} / {row['claude_conviction']}**", "",
                str(row.get("claude_rationale") or ""), "",
            ])
        else:
            lines.extend(["Claude B: no completed response.", ""])
        if row["luna_response_available"]:
            luna_label = row.get("luna_analyst_label")
            luna_prefix = (
                f"{luna_label} / " if pd.notna(luna_label) else ""
            )
            lines.extend([
                f"Luna B: **{luna_prefix}{row['luna_direction']} / "
                f"{row['luna_conviction']}**", "",
                str(row.get("luna_rationale") or ""), "",
            ])
        else:
            lines.extend(["Luna B: no completed response.", ""])
    (out / "ACTUAL_AI_RESPONSES.md").write_text("\n".join(lines), encoding="utf-8")


def json_value(value):
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [json_value(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def write_response_jsonl(frame: pd.DataFrame, out: Path) -> None:
    response_keys = [
        "analyst_label", "direction", "conviction", "reason_code",
        "reason_code_secondary",
        "headline_rev_growth_pct", "organic_constant_fx_growth_pct",
        "one_off_items", "adjusted_vs_reported_ebitda_gap_pct",
        "ex_associate_net_income_note", "guidance_direction", "beat_quality",
        "rationale",
    ]
    with (out / "ai_responses_63.jsonl").open("w", encoding="utf-8") as handle:
        for _, row in frame.sort_values(["period", "ticker"]).iterrows():
            record = {
                "event": {
                    "ticker": row["ticker"], "slug": row["slug"],
                    "period": row["period"], "t0": row["t0"],
                    "realized_direction": row["realized_direction"],
                    "ar0_intra": row["ar0_intra"], "raw_ret": row["raw_ret"],
                    "ar0_cc_reference": row["ar0_cc"],
                    "entry_rule": row["entry_rule"],
                },
                "analyst": {
                    "direction": row["analyst_direction"],
                    "strength": row["analyst_strength_label"],
                    "label_raw": row["label_raw"],
                },
                "claude": {
                    "response_available": row["claude_response_available"],
                    **{key: row.get(f"claude_{key}") for key in response_keys},
                },
                "luna": {
                    "response_available": row["luna_response_available"],
                    **{key: row.get(f"luna_{key}") for key in response_keys},
                },
            }
            handle.write(json.dumps(json_value(record), ensure_ascii=False) + "\n")


def write_readme(out: Path) -> None:
    text = """# Dashboard integration bundle

Start here: extract the archive and open `OPEN_ME_FIRST.html` in any desktop
browser. It is a self-contained report with the headline comparison, filters,
all 63 events, and the actual available AI rationales; it needs no server or
internet connection.

`trades_63.csv` and `trades_63.parquet` have one row per tradable analyst event.
The stable join key is `(slug, period)`; `ticker` and `t0` are also supplied.

Recommended dashboard views:

1. Signal quality: directional calls, abstentions, hit rate against the
   market-adjusted open-to-close `realized_position`.
2. Executable trade performance: mean/compound `*_trade_net_return_25bps` by provider.
3. Analyst conviction: split `analyst_strength_label` into `full` and `soft`.
4. Fair model comparison: filter `comparison_set_claude_directional_26 == true`.
5. Audit drill-down: display `claude_rationale` and `luna_rationale` beside each event.

The corrected Luna run has no flat option and must be directional on all 63.
Claude's original prompt allowed `flat`, so use `*_response_available`,
`*_directional_call`, and `*_correct` separately when working with Claude.

The primary outcome and execution window are both the next session open-to-close:
`ar0_intra` determines directional accuracy and `raw_ret` is the stock's
unhedged open-to-close return. The old close-to-close and timing-derived entry
fields are retained under explicit `source_*`/`close_to_close_*` names only.
`report_session` determines which session is `t0`: after-close releases use the
next actual IPC trading date, while pre-open releases use the same trading date.
All 63 mappings are build-time verified; see `TIMING_AND_NUMBERS_VERIFICATION.md`
and `verification_63.csv` for the independent row-level audit.
For an apples-to-apples deployment view, use `*_strategy_net_return_25bps`:
active calls receive their net trade return and explicit abstentions receive 0.
"""
    (out / "README.md").write_text(text, encoding="utf-8")


def write_data_dictionary(out: Path) -> None:
    text = """# Data dictionary

Grain: one row per analyst-covered tradable earnings event. Stable primary key:
`(slug, period)`. There are exactly 63 rows.

## Event and outcome

| Field | Meaning |
|---|---|
| `ticker`, `slug`, `symbol`, `period` | Issuer identifiers and fiscal quarter. |
| `filed_dt`, `info_dt`, `event_dt`, `t0` | Filing/information timestamps and first reaction session. |
| `report_date`, `report_session` | Canonical public earnings-release date and whether it was before the open or after the close. |
| `expected_t0`, `timing_verified` | Reaction date derived from the report session and IPC trading calendar, plus the equality check against packaged `t0`. |
| `timing_source`, `report_timestamp`, `report_source_reference` | Provenance for the timing classification. Sixty-one events use the frozen human calendar; two documented gaps use primary BMV/SEC records. |
| `entry_rule` | Always `open_t0_to_close_t0` in the corrected strict day-trading benchmark. |
| `price_open_t0`, `price_close_t0`, `volume_t0` | Frozen raw OHLCV inputs for the reaction session. |
| `index_open_t0`, `index_close_t0`, `index_ret_open_to_close` | Frozen IPC bars and independently calculated intraday market return. |
| `ar0_intra` | Market-adjusted open-to-close return on `t0`; its sign is the primary direction outcome. |
| `source_ar0_intra` | Previously stored intraday abnormal return, retained to prove it equals the independent reconstruction. |
| `raw_ret` | Stock's unhedged open-to-close return on `t0`, before direction and costs. |
| `realized_direction`, `realized_position` | Sign of `ar0_intra` as `up/down` and `+1/-1`. |
| `ar0_cc`, `close_to_close_*` | Pre-registered close-to-close abnormal return and its direction, retained for reference only. |
| `source_entry_rule`, `source_raw_ret` | Earlier timing-derived execution fields, retained for audit only; not used in the headline benchmark. |

## Reader fields

Each `analyst_*`, `claude_*`, or `luna_*` prefix identifies the signal source.

| Suffix | Meaning |
|---|---|
| `response_available` | A completed response exists. Always true for analyst/Luna; Claude is 34/63. |
| `direction`, `position` | `up/down/flat` and `+1/-1/0`; analyst and corrected Luna have no flat calls in this set. |
| `directional_call` | Position is nonzero. |
| `correct` | Direction matches `sign(ar0_intra)`; null for flat or missing responses. |
| `conviction` | AI `high/medium/low`; analyst conviction is represented by `analyst_strength_label`. |
| `reason_code*`, `guidance_direction`, `beat_quality`, `rationale` | Structured AI interpretation and its actual one-sentence explanation. |

## Return fields

| Suffix | Meaning |
|---|---|
| `trade_gross_return` | `position * raw_ret` for active calls; null for abstentions/missing responses. |
| `trade_net_return_25bps` | Gross active-trade return minus 0.25%; null for abstentions/missing. |
| `strategy_gross_return` | Active-trade return, 0 for an explicit flat response, null if response missing. |
| `strategy_net_return_25bps` | Net active-trade return, 0 for an explicit flat response, null if response missing. Use this for deployment comparisons. |

## Comparison and caveat fields

| Field | Meaning |
|---|---|
| `analyst_strength_label` | `full` (27 events) or `soft` (36 events). |
| `luna_analyst_label`, `luna_strength_label` | Corrected Luna's exact four-choice analyst label and its full/soft mapping. |
| `comparison_set_claude_directional_26` | Exact old-Claude directional set requested for the Claude-vs-Luna comparison. |
| `claude_identified`, `claude_probe_confidence` | Historical contamination-probe evidence; identification succeeded broadly, so results are descriptive. |

The forced-label mapping is exact: `Positive = up/full`, `Negative = down/full`,
`N to P = up/soft`, and `N to N = down/soft`. No Luna flat/neutral value is
permitted in the corrected primary bundle.

Percent returns are stored as decimals (`0.01` = 1%). Boolean columns should be
kept as booleans, and null is semantically different from an explicit `flat`.
"""
    (out / "DATA_DICTIONARY.md").write_text(text, encoding="utf-8")


def write_price_window_audit(frame: pd.DataFrame, out: Path) -> None:
    large = frame[frame["ar0_intra"].abs() >= 0.05].copy()
    large["volume_multiple"] = (
        large["price_close_t0"] * large["volume_t0"]
        / large["median_peso_volume"]
    )
    lines = [
        "# Price and reaction-window audit",
        "",
        "## Primary correction",
        "",
        "The sendable benchmark now uses one literal next-session day-trading window for every event:",
        "",
        "- score direction with market-adjusted `open(t0) -> close(t0)` (`ar0_intra`);",
        "- calculate P&L with raw stock `open(t0) -> close(t0)`;",
        "- subtract 25 bps from every active trade.",
        "- define `t0` from release timing: after-close -> next IPC trading day; pre-open -> same IPC trading day.",
        "",
        "The former close-to-close outcome and timing-derived execution rule are retained only in labeled audit columns.",
        "",
        "## BIMBO 2026-1T reconstruction",
        "",
    ]
    bimbo = frame[(frame["slug"] == "bimbo") & (frame["period"] == "2026-1T")].iloc[0]
    volume_multiple = (
        bimbo["price_close_t0"] * bimbo["volume_t0"] / bimbo["median_peso_volume"]
    )
    lines += [
        f"- Frozen reaction session: `{bimbo['t0']}`.",
        f"- Open: MXN {bimbo['price_open_t0']:.2f}; close: MXN {bimbo['price_close_t0']:.2f}.",
        f"- Raw strict day trade: **{bimbo['raw_ret'] * 100:+.2f}%**.",
        f"- IPC-adjusted open-to-close outcome: **{bimbo['ar0_intra'] * 100:+.2f}%**.",
        f"- Reference close-to-close abnormal return: {bimbo['ar0_cc'] * 100:+.2f}% (not the primary day-trade return).",
        f"- Volume: {bimbo['volume_t0']:,.0f} shares, about **{volume_multiple:.1f}x** its pre-event median peso volume.",
        "",
        "The move is unusually large but is not a stale-price artifact. Independent historical listings reproduce the same prices (approximately MXN 54.46 on April 29 and MXN 59.38-59.48 on April 30). Grupo Bimbo's official invitation also states that the earnings release would be distributed after market closure, so the old 14:40 XBRL timestamp should not have been used to justify a prior-close entry.",
        "",
        "Sources: [Grupo Bimbo official 1Q26 event](https://www.grupobimbo.com/index.php/en/investors/events/1q26), [official call invitation](https://d2rwhogv2mrkk6.cloudfront.net/s3fs-public/eventos-2026/Conference%20Call%20Invite%201Q26.pdf), and [independent historical listing](https://eulerpool.com/en/stock/Grupo-Bimbo-SAB-de-CV-Stock-MXP495211262).",
        "",
        f"## Large strict intraday reactions (|market-adjusted open-to-close| >= 5%; {len(large)} events)",
        "",
        "| Issuer | Period | t0 | Stock O→C | Market-adjusted O→C | C→C reference | Event volume / median |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for _, row in large.sort_values("ar0_intra", key=lambda s: s.abs(), ascending=False).iterrows():
        lines.append(
            f"| {row['ticker']} | {row['period']} | {row['t0']} | "
            f"{row['raw_ret'] * 100:+.2f}% | {row['ar0_intra'] * 100:+.2f}% | "
            f"{row['ar0_cc'] * 100:+.2f}% | {row['volume_multiple']:.1f}x |"
        )
    lines += [
        "",
        "Large movements therefore remain in the sample, but they are now shown as the exact raw and market-adjusted intraday windows rather than being described through close-to-close AR0.",
    ]
    (out / "PRICE_WINDOW_AUDIT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_visual_report(frame: pd.DataFrame, summary: dict, out: Path) -> None:
    events = []
    for _, row in frame.sort_values(["t0", "ticker"]).iterrows():
        claude_call = "not run"
        if row["claude_response_available"]:
            claude_call = f"{row['claude_direction']} / {row['claude_conviction']}"
        events.append({
            "ticker": row["ticker"],
            "slug": row["slug"],
            "period": row["period"],
            "report_date": json_value(row["report_date"]),
            "report_session": row["report_session"],
            "timing_source": row["timing_source"],
            "t0": json_value(row["t0"]),
            "outcome": row["realized_direction"],
            "ar0_pct": round(float(row["ar0_intra"]) * 100, 3),
            "stock_oc_pct": round(float(row["raw_ret"]) * 100, 3),
            "close_to_close_pct": round(float(row["ar0_cc"]) * 100, 3),
            "analyst_label": row["label_raw"],
            "analyst_strength": row["analyst_strength_label"],
            "analyst_correct": bool(row["analyst_correct"]),
            "analyst_net_bps": round(float(row["analyst_trade_net_return_25bps"]) * 10_000),
            "claude_call": claude_call,
            "claude_correct": json_value(row["claude_correct"]),
            "claude_rationale": json_value(row.get("claude_rationale")),
            "luna_label": row["luna_analyst_label"],
            "luna_direction": row["luna_direction"],
            "luna_correct": bool(row["luna_correct"]),
            "luna_net_bps": round(float(row["luna_trade_net_return_25bps"]) * 10_000),
            "luna_rationale": row["luna_rationale"],
            "claude_26": bool(row["comparison_set_claude_directional_26"]),
        })
    payload = json.dumps(events, ensure_ascii=False).replace("</", "<\\/")
    all_metrics = summary["all_63"]
    full = summary["analyst_full_strength_27"]
    soft = summary["analyst_soft_strength_36"]
    shared = summary["claude_directional_26"]

    def rate(block: dict, provider: str) -> float:
        return float(block[provider]["directional_hit_rate"] * 100)

    def net(block: dict, provider: str) -> int:
        return round(block[provider]["mean_net_return_per_responded_event"] * 10_000)

    analyst_conviction_rows = []
    for tier, label in (("full", "Full (Positive / Negative)"), ("soft", "Soft (N to P / N to N)")):
        subset = frame[frame["analyst_strength_label"].eq(tier)]
        n = len(subset)
        original_hits = int(subset["analyst_correct_close_to_close"].sum())
        relative_hits = int(subset["analyst_correct"].sum())
        gross_wins = int((subset["analyst_trade_gross_return"] > 0).sum())
        net_wins = int((subset["analyst_trade_net_return_25bps"] > 0).sum())
        analyst_conviction_rows.append(
            f"<tr><td>{label}</td><td>{n}</td>"
            f"<td>{original_hits}/{n} ({original_hits/n:.1%})</td>"
            f"<td>{relative_hits}/{n} ({relative_hits/n:.1%})</td>"
            f"<td>{gross_wins}/{n} ({gross_wins/n:.1%})</td>"
            f"<td>{net_wins}/{n} ({net_wins/n:.1%})</td></tr>"
        )

    ai_confidence_rows = []
    for provider, display in (("claude", "Claude"), ("luna", "Luna")):
        for confidence in ("high", "medium", "low"):
            responses = frame[
                frame[f"{provider}_response_available"].fillna(False)
                & frame[f"{provider}_conviction"].eq(confidence)
            ]
            active = responses[responses[f"{provider}_position"].fillna(0).ne(0)]
            hits = int(active[f"{provider}_correct"].fillna(False).sum())
            net_wins = int((active[f"{provider}_trade_net_return_25bps"] > 0).sum())
            mean_net = active[f"{provider}_trade_net_return_25bps"].mean()
            hit_text = f"{hits}/{len(active)} ({hits/len(active):.1%})" if len(active) else "n/a"
            net_text = f"{net_wins}/{len(active)} ({net_wins/len(active):.1%})" if len(active) else "n/a"
            mean_text = f"{mean_net*100:+.2f}%" if len(active) else "n/a"
            ai_confidence_rows.append(
                f"<tr><td>{display}</td><td>{confidence.title()}</td>"
                f"<td>{len(responses)}</td><td>{len(active)}</td>"
                f"<td>{hit_text}</td><td>{net_text}</td><td>{mean_text}</td></tr>"
            )

    replacements = {
        "__ANALYST_HITS__": str(all_metrics["analyst"]["correct_directional"]),
        "__ANALYST_RATE__": f"{rate(all_metrics, 'analyst'):.1f}",
        "__ANALYST_NET__": str(net(all_metrics, "analyst")),
        "__LUNA_HITS__": str(all_metrics["luna"]["correct_directional"]),
        "__LUNA_RATE__": f"{rate(all_metrics, 'luna'):.1f}",
        "__LUNA_NET__": str(net(all_metrics, "luna")),
        "__FULL_ANALYST_RATE__": f"{rate(full, 'analyst'):.1f}",
        "__FULL_LUNA_RATE__": f"{rate(full, 'luna'):.1f}",
        "__SOFT_ANALYST_RATE__": f"{rate(soft, 'analyst'):.1f}",
        "__SOFT_LUNA_RATE__": f"{rate(soft, 'luna'):.1f}",
        "__C26_ANALYST_RATE__": f"{rate(shared, 'analyst'):.1f}",
        "__C26_CLAUDE_RATE__": f"{rate(shared, 'claude'):.1f}",
        "__C26_LUNA_RATE__": f"{rate(shared, 'luna'):.1f}",
        "__C26_ANALYST_NET__": str(net(shared, "analyst")),
        "__C26_CLAUDE_NET__": str(net(shared, "claude")),
        "__C26_LUNA_NET__": str(net(shared, "luna")),
        "__MCNEMAR__": f"{summary['paired_analyst_luna_all_63']['mcnemar_exact_two_sided_p']:.3f}",
        "__CREDITS__": str(summary["luna_usage"]["estimated_luna_credits"]),
        "__ANALYST_CONVICTION_ROWS__": "".join(analyst_conviction_rows),
        "__AI_CONFIDENCE_ROWS__": "".join(ai_confidence_rows),
        "__EVENT_DATA__": payload,
    }
    document = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Post-report day trading benchmark</title>
<style>
  :root { color-scheme: light dark; --bg:#f4f1e8; --paper:#fffdf7; --ink:#17232c; --muted:#61717a; --line:#d9d5c9; --analyst:#0c6b68; --luna:#b8502e; --claude:#3d628f; --good:#16734a; --bad:#ad3e32; --soft:#ece7d9; }
  @media (prefers-color-scheme:dark) { :root { --bg:#121719; --paper:#1b2326; --ink:#edf1ed; --muted:#aab6b7; --line:#3a4547; --soft:#273034; --good:#5fc58d; --bad:#ef8277; } }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; line-height:1.45; }
  main { max-width:1180px; margin:0 auto; padding:42px 28px 70px; }
  h1,h2 { font-family:Georgia,"Times New Roman",serif; font-weight:500; letter-spacing:-.02em; }
  h1 { font-size:clamp(2.2rem,5vw,4.6rem); line-height:.98; margin:.35rem 0 1rem; max-width:900px; }
  h2 { font-size:clamp(1.45rem,2.5vw,2.2rem); margin:2.7rem 0 1rem; }
  p { max-width:820px; }
  .eyebrow { color:var(--analyst); font-size:.78rem; font-weight:700; letter-spacing:.14em; text-transform:uppercase; }
  .lede { color:var(--muted); font-size:1.08rem; }
  .stamp { display:inline-block; border:1px solid var(--analyst); color:var(--analyst); border-radius:999px; padding:.28rem .65rem; font-size:.78rem; font-weight:700; }
  .cards { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; margin:2rem 0; }
  .card { background:var(--paper); border:1px solid var(--line); border-radius:14px; padding:18px; }
  .card small { color:var(--muted); display:block; }
  .big { font-family:Georgia,"Times New Roman",serif; font-size:2.15rem; line-height:1.1; margin:.35rem 0; }
  .analyst { color:var(--analyst); } .luna { color:var(--luna); } .claude { color:var(--claude); }
  .chart-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:28px; }
  .plot { background:var(--paper); border:1px solid var(--line); border-radius:14px; padding:20px; }
  .plot h3 { margin:0 0 1.1rem; font-size:1rem; }
  .bar-row { display:grid; grid-template-columns:120px 1fr 58px; align-items:center; gap:10px; margin:.8rem 0; }
  .track { height:18px; background:var(--soft); border-radius:4px; overflow:hidden; }
  .fill { height:100%; border-radius:4px; } .fill.analyst-bg { background:var(--analyst); } .fill.luna-bg { background:var(--luna); } .fill.claude-bg { background:var(--claude); }
  .bar-value { text-align:right; font-variant-numeric:tabular-nums; font-weight:700; }
  .note { color:var(--muted); font-size:.87rem; }
  .controls { display:flex; flex-wrap:wrap; gap:12px; align-items:end; margin:1rem 0; }
  label { color:var(--muted); font-size:.8rem; font-weight:700; text-transform:uppercase; letter-spacing:.08em; }
  select { display:block; margin-top:5px; border:1px solid var(--line); border-radius:8px; background:var(--paper); color:var(--ink); padding:.58rem .7rem; font:inherit; }
  .table-wrap { overflow-x:auto; border:1px solid var(--line); border-radius:12px; background:var(--paper); }
  table { width:100%; border-collapse:collapse; min-width:900px; }
  th,td { border-bottom:1px solid var(--line); padding:.65rem .7rem; text-align:left; vertical-align:middle; }
  th { color:var(--muted); font-size:.75rem; text-transform:uppercase; letter-spacing:.06em; }
  td { font-size:.88rem; }
  tr:last-child td { border-bottom:0; }
  .ok { color:var(--good); font-weight:700; } .miss { color:var(--bad); font-weight:700; } .na { color:var(--muted); }
  button { border:1px solid var(--line); background:var(--soft); color:var(--ink); border-radius:7px; padding:.36rem .6rem; cursor:pointer; }
  button:hover { border-color:var(--analyst); }
  .detail { margin-top:14px; background:var(--paper); border:1px solid var(--line); border-left:5px solid var(--luna); border-radius:12px; padding:18px; }
  .detail h3 { margin:.1rem 0 .55rem; }
  .detail-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:22px; }
  .detail p { margin:.25rem 0; color:var(--muted); }
  .downloads { display:flex; flex-wrap:wrap; gap:10px; }
  .downloads a { color:var(--ink); border-bottom:2px solid var(--analyst); text-decoration:none; padding:.2rem 0; }
  footer { border-top:1px solid var(--line); margin-top:3rem; padding-top:1rem; color:var(--muted); font-size:.85rem; }
  @media(max-width:760px) { main{padding:28px 16px 50px}.cards,.chart-grid,.detail-grid{grid-template-columns:1fr}.bar-row{grid-template-columns:95px 1fr 52px} }
  @media print { body{background:white;color:#17232c}.controls,button{display:none}.card,.plot,.table-wrap,.detail{break-inside:avoid;background:white}.table-wrap{overflow:visible}main{max-width:none;padding:10mm} }
</style>
</head>
<body>
<main>
  <span class="stamp">Corrected forced-choice benchmark</span>
  <div class="eyebrow" style="margin-top:1.4rem">Mexican listed issuers · first post-report session</div>
  <h1>Analyst judgment remains stronger across the full 63-event suite.</h1>
  <p class="lede">Luna was forced to use the analyst's exact four choices—Positive, Negative, N to P, or N to N—with no flat or neutral option. Every headline score and return now uses one strict window: buy or short at the first post-report session's open and exit at that session's close.</p>

  <section class="card" style="border-left:5px solid var(--analyst);margin-top:1.4rem"><strong>Timing and arithmetic audit: PASS · 63/63</strong><p class="note">Every after-close release is mapped to the next actual IPC trading date; every pre-open release is mapped to the same trading date. Stock and IPC bars, market-adjusted outcomes, correctness flags, net returns, comparison sets, and headline metrics were independently recomputed. Open the timing + numbers audit below for evidence.</p></section>

  <section class="card" style="border-left:5px solid var(--claude);margin-top:1rem"><strong>Window correction</strong><p class="note">The earlier view displayed market-adjusted close-to-close AR0 and used timing-dependent entries for P&amp;L. Those fields remain in the data only for audit. This page uses market-adjusted open→close for correctness and raw stock open→close for trading returns.</p></section>

  <section class="cards" aria-label="Headline results">
    <div class="card"><small>Analyst · all 63</small><div class="big analyst">__ANALYST_HITS__/63</div><strong>__ANALYST_RATE__% correct</strong><small>+__ANALYST_NET__ net bps/event</small></div>
    <div class="card"><small>Luna · all 63</small><div class="big luna">__LUNA_HITS__/63</div><strong>__LUNA_RATE__% correct</strong><small>+__LUNA_NET__ net bps/event</small></div>
    <div class="card"><small>Paired uncertainty</small><div class="big">p = __MCNEMAR__</div><strong>McNemar exact</strong><small>No reliable accuracy difference</small></div>
  </section>

  <h2>Where performance separates</h2>
  <div class="chart-grid">
    <section class="plot">
      <h3>Relative O→C hits on analyst-defined tiers</h3>
      <div class="bar-row"><span>Full · Analyst</span><div class="track"><div class="fill analyst-bg" style="width:__FULL_ANALYST_RATE__%"></div></div><span class="bar-value">__FULL_ANALYST_RATE__%</span></div>
      <div class="bar-row"><span>Full · Luna</span><div class="track"><div class="fill luna-bg" style="width:__FULL_LUNA_RATE__%"></div></div><span class="bar-value">__FULL_LUNA_RATE__%</span></div>
      <div class="bar-row"><span>Soft · Analyst</span><div class="track"><div class="fill analyst-bg" style="width:__SOFT_ANALYST_RATE__%"></div></div><span class="bar-value">__SOFT_ANALYST_RATE__%</span></div>
      <div class="bar-row"><span>Soft · Luna</span><div class="track"><div class="fill luna-bg" style="width:__SOFT_LUNA_RATE__%"></div></div><span class="bar-value">__SOFT_LUNA_RATE__%</span></div>
      <p class="note">This chart uses strict market-relative open-to-close scoring. It is not the original close-to-close 92.6% statistic.</p>
    </section>
    <section class="plot">
      <h3>Same 26 old-Claude directional events</h3>
      <div class="bar-row"><span>Analyst</span><div class="track"><div class="fill analyst-bg" style="width:__C26_ANALYST_RATE__%"></div></div><span class="bar-value">__C26_ANALYST_RATE__%</span></div>
      <div class="bar-row"><span>Claude</span><div class="track"><div class="fill claude-bg" style="width:__C26_CLAUDE_RATE__%"></div></div><span class="bar-value">__C26_CLAUDE_RATE__%</span></div>
      <div class="bar-row"><span>Luna</span><div class="track"><div class="fill luna-bg" style="width:__C26_LUNA_RATE__%"></div></div><span class="bar-value">__C26_LUNA_RATE__%</span></div>
      <p class="note">Net bps/event: Analyst +__C26_ANALYST_NET__, Claude +__C26_CLAUDE_NET__, Luna +__C26_LUNA_NET__. Claude has only 34 completed responses across the full 63, so this 26-event intersection is the clean comparison.</p>
    </section>
  </div>

  <h2>Conviction — definitions that must not be mixed</h2>
  <p class="lede">The remembered analyst full-conviction rate of 92.6% is correct under the original research outcome. Literal post-report day trading produces two additional win definitions. AI confidence is a separate self-reported field.</p>
  <section class="plot" style="margin-bottom:1rem">
    <h3>Analyst label conviction</h3>
    <div class="table-wrap"><table><thead><tr><th>Analyst tier</th><th>Events</th><th>Original adjusted C→C hit</th><th>Strict relative O→C hit</th><th>Gross trade win</th><th>Net trade win (25 bps)</th></tr></thead><tbody>__ANALYST_CONVICTION_ROWS__</tbody></table></div>
    <p class="note">The 92.6% remains the valid original statistic. For the actual first-session trade, the full tier is 23/27 gross profitable and 22/27 profitable after 25 bps. The metrics answer different questions.</p>
  </section>
  <section class="plot">
    <h3>AI self-reported confidence · directional calls only</h3>
    <div class="table-wrap"><table><thead><tr><th>Reader</th><th>Confidence</th><th>Responses</th><th>Directional</th><th>Relative O→C hits</th><th>Net wins</th><th>Mean net/trade</th></tr></thead><tbody>__AI_CONFIDENCE_ROWS__</tbody></table></div>
    <p class="note">High-confidence samples are tiny: Claude 1 directional call and Luna 4. Their 100% relative hit rates are descriptive, not stable estimates.</p>
  </section>

  <h2>Audit every event</h2>
  <div class="controls">
    <label>Comparison set<select id="set-filter"><option value="all">All 63 events</option><option value="claude26">Exact Claude 26</option><option value="full">Analyst full-strength 27</option><option value="soft">Analyst soft 36</option></select></label>
    <span id="row-count" class="note" aria-live="polite"></span>
  </div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Issuer</th><th>Period</th><th>Outcome</th><th>Analyst</th><th>Claude</th><th>Luna</th><th>O→C abnormal</th><th></th></tr></thead>
      <tbody id="event-rows"></tbody>
    </table>
  </div>
  <section id="event-detail" class="detail" aria-live="polite"></section>

  <h2>Files for integration</h2>
  <div class="downloads"><a href="trades_63.csv">63-row CSV</a><a href="trades_63.parquet">Parquet</a><a href="verification_63.csv">63-row verification</a><a href="TIMING_AND_NUMBERS_VERIFICATION.md">Timing + numbers audit</a><a href="ai_responses_63.jsonl">Full AI JSONL</a><a href="REPORT.md">Methodology report</a><a href="PRICE_WINDOW_AUDIT.md">Price/window audit</a><a href="DATA_DICTIONARY.md">Data dictionary</a></div>

  <footer>Historical descriptive benchmark, not prospective proof of alpha. Claude identification probes recognized historical issuers; Luna may also possess latent historical knowledge. Direction uses sign(ar0_intra); trading returns use raw open(t0)→close(t0) and subtract 25 bps. Corrected Luna run cost: __CREDITS__ credits.</footer>
</main>
<script id="report-data" type="application/json">__EVENT_DATA__</script>
<script>
  const events = JSON.parse(document.getElementById('report-data').textContent);
  const tbody = document.getElementById('event-rows');
  const filter = document.getElementById('set-filter');
  const count = document.getElementById('row-count');
  const detail = document.getElementById('event-detail');
  const mark = value => value === true ? '<span class="ok">✓</span>' : value === false ? '<span class="miss">✕</span>' : '<span class="na">—</span>';
  const safe = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function selectedEvents() {
    if (filter.value === 'claude26') return events.filter(e => e.claude_26);
    if (filter.value === 'full') return events.filter(e => e.analyst_strength === 'full');
    if (filter.value === 'soft') return events.filter(e => e.analyst_strength === 'soft');
    return events;
  }
  function showDetail(event) {
    detail.innerHTML = `<h3>${safe(event.ticker)} · ${safe(event.period)} · reaction session ${safe(event.t0)}</h3><p><strong>Release timing:</strong> ${safe(event.report_date)} · ${safe(event.report_session)}; source: ${safe(event.timing_source)}. <strong>Strict day trade:</strong> ${event.stock_oc_pct > 0 ? '+' : ''}${event.stock_oc_pct.toFixed(2)}% raw stock open→close; <strong>scoring outcome:</strong> ${safe(event.outcome)} (${event.ar0_pct > 0 ? '+' : ''}${event.ar0_pct.toFixed(2)}% market-adjusted open→close). Reference close-to-close abnormal return: ${event.close_to_close_pct > 0 ? '+' : ''}${event.close_to_close_pct.toFixed(2)}%.</p><div class="detail-grid"><div><strong>Claude · ${safe(event.claude_call)}</strong><p>${safe(event.claude_rationale || 'No completed Claude response.')}</p></div><div><strong>Luna · ${safe(event.luna_label)} (${safe(event.luna_direction)})</strong><p>${safe(event.luna_rationale)}</p></div></div>`;
  }
  function render() {
    const rows = selectedEvents();
    count.textContent = `${rows.length} events shown`;
    tbody.innerHTML = rows.map((e, i) => `<tr><td><strong>${safe(e.ticker)}</strong></td><td>${safe(e.period)}</td><td>${safe(e.outcome)}</td><td>${safe(e.analyst_label)} ${mark(e.analyst_correct)}</td><td>${safe(e.claude_call)} ${mark(e.claude_correct)}</td><td>${safe(e.luna_label)} ${mark(e.luna_correct)}</td><td>${e.ar0_pct > 0 ? '+' : ''}${e.ar0_pct.toFixed(2)}%</td><td><button type="button" data-row="${i}">Details</button></td></tr>`).join('');
    tbody.querySelectorAll('button').forEach(button => button.addEventListener('click', () => showDetail(rows[Number(button.dataset.row)])));
    if (rows.length) showDetail(rows[0]);
  }
  filter.addEventListener('change', render);
  render();
</script>
</body>
</html>
"""
    for key, value in replacements.items():
        document = document.replace(key, value)
    (out / "OPEN_ME_FIRST.html").write_text(document, encoding="utf-8")
    (out / "START_HERE.txt").write_text(
        "Extract this ZIP and open OPEN_ME_FIRST.html in any browser.\n"
        "The page is self-contained and links to the included data files.\n",
        encoding="utf-8",
    )


def write_bundle_manifest(out: Path) -> None:
    names = [
        "OPEN_ME_FIRST.html", "START_HERE.txt", "REPORT.md",
        "COMPARISON_26.md", "ACTUAL_AI_RESPONSES.md", "README.md",
        "DATA_DICTIONARY.md", "PRICE_WINDOW_AUDIT.md",
        "TIMING_AND_NUMBERS_VERIFICATION.md", "verification_63.csv",
        "verification_summary.json", "trades_63.csv", "trades_63.parquet",
        "ai_responses_63.jsonl", "summary.json", "comparison-chart.html",
    ]
    files = []
    for name in names:
        path = out / name
        if not path.exists():
            continue
        files.append({
            "file": name,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    manifest = {
        "primary_entrypoint": "OPEN_ME_FIRST.html",
        "benchmark": "corrected analyst-forced Luna 63-event suite",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    (out / "BUNDLE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--analyst-predictions", type=Path, required=True)
    parser.add_argument("--claude-reads", type=Path, required=True)
    parser.add_argument("--luna-responses", type=Path, required=True)
    parser.add_argument("--luna-manifest", type=Path, required=True)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument("--report-calendar", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    base = pd.read_parquet(args.decisions)
    base = base[base["strength"] > 0].copy()
    if len(base) != 63 or base.duplicated(["slug", "period"]).any():
        raise SystemExit(f"expected 63 unique tradable decisions, found {len(base)}")
    base = base[[
        "ticker", "slug", "symbol", "period", "filed_dt", "info_dt",
        "event_dt", "t0", "entry_rule", "ar0_cc", "ar0_gap", "ar0_intra",
        "i_ret0", "raw_ret", "median_peso_volume",
        "direction", "strength",
    ]].rename(columns={
        "entry_rule": "source_entry_rule",
        "raw_ret": "source_raw_ret",
        "ar0_intra": "source_ar0_intra",
    })
    base = attach_verified_timing(base, args.report_calendar, args.index)

    prices = pd.read_parquet(args.prices)[
        ["symbol", "date", "open", "close", "volume"]
    ].rename(columns={
        "date": "t0", "open": "price_open_t0", "close": "price_close_t0",
        "volume": "volume_t0",
    })
    prices = prices.drop_duplicates(["symbol", "t0"])

    analyst = pd.read_parquet(args.analyst_predictions)[
        ["slug", "period", "label_raw"]
    ].drop_duplicates(["slug", "period"])
    claude_raw = pd.read_parquet(args.claude_reads)
    claude_cols = ["slug", "period"] + [
        col for col in claude_raw.columns if col.startswith("b_")
    ] + ["identified", "probe_confidence", "error_B", "truncated_B"]
    claude_rename = {
        col: col.replace("b_", "claude_", 1)
        for col in claude_cols if col.startswith("b_")
    }
    claude_rename["identified"] = "claude_identified"
    claude_rename["probe_confidence"] = "claude_probe_confidence"
    claude = claude_raw[claude_cols].rename(columns=claude_rename)
    luna = load_luna(args.luna_responses)
    frame = base.merge(analyst, on=["slug", "period"], how="left")
    frame = frame.merge(claude, on=["slug", "period"], how="left")
    frame = frame.merge(luna, on=["slug", "period"], how="left")
    frame = frame.merge(prices, on=["symbol", "t0"], how="left", validate="many_to_one")
    if frame[["price_open_t0", "price_close_t0"]].isna().any().any():
        raise SystemExit("strict day-trading bundle requires open and close bars for all 63 t0 sessions")
    frame["raw_ret"] = frame["price_close_t0"] / frame["price_open_t0"] - 1
    index = pd.read_parquet(args.index)[["date", "open", "close"]].rename(columns={
        "date": "t0", "open": "index_open_t0", "close": "index_close_t0",
    }).drop_duplicates(["t0"])
    frame = frame.merge(index, on="t0", how="left", validate="many_to_one")
    if frame[["index_open_t0", "index_close_t0"]].isna().any().any():
        raise SystemExit("strict bundle requires IPC open and close bars for all 63 t0 sessions")
    frame["index_ret_open_to_close"] = (
        frame["index_close_t0"] / frame["index_open_t0"] - 1
    )
    frame["ar0_intra"] = frame["raw_ret"] - frame["index_ret_open_to_close"]
    if not np.allclose(
        frame["ar0_intra"], frame["source_ar0_intra"], rtol=0, atol=1e-12
    ):
        raise SystemExit("independent intraday abnormal returns differ from frozen source")
    frame["entry_rule"] = "open_t0_to_close_t0"

    frame["close_to_close_realized_position"] = np.sign(frame["ar0_cc"]).astype(int)
    frame["close_to_close_realized_direction"] = frame[
        "close_to_close_realized_position"
    ].map({-1: "down", 0: "flat", 1: "up"})
    frame["realized_position"] = np.sign(frame["ar0_intra"]).astype(int)
    frame["realized_direction"] = frame["realized_position"].map({-1: "down", 0: "flat", 1: "up"})
    frame["analyst_direction"] = frame["direction"].map({-1: "down", 1: "up"})
    frame["analyst_position"] = frame["direction"].astype("Int64")
    frame["analyst_strength"] = frame["strength"]
    frame["analyst_strength_label"] = frame["strength"].map({1.0: "full", 0.5: "soft"})
    frame["analyst_response_available"] = True
    frame["claude_response_available"] = frame["claude_direction"].notna()
    frame["luna_response_available"] = frame["luna_direction"].notna()
    frame["claude_position"] = frame["claude_direction"].map(DIRECTION_TO_POSITION).astype("Int64")
    frame["luna_position"] = frame["luna_direction"].map(DIRECTION_TO_POSITION).astype("Int64")
    forced_luna = "luna_analyst_label" in frame.columns
    if forced_luna:
        label_direction = {
            "Positive": "up", "Negative": "down",
            "N to P": "up", "N to N": "down",
        }
        label_strength = {
            "Positive": "full", "Negative": "full",
            "N to P": "soft", "N to N": "soft",
        }
        if not frame["luna_response_available"].all():
            raise SystemExit("forced Luna bundle requires all 63 responses")
        if not frame["luna_analyst_label"].isin(label_direction).all():
            raise SystemExit("forced Luna response contains an invalid analyst label")
        if not (
            frame["luna_analyst_label"].map(label_direction)
            == frame["luna_direction"]
        ).all():
            raise SystemExit("forced Luna label and direction disagree")
        frame["luna_strength_label"] = frame["luna_analyst_label"].map(label_strength)
        frame["luna_strength"] = frame["luna_strength_label"].map({"full": 1.0, "soft": 0.5})
        frame["luna_label_exact_match"] = frame["luna_analyst_label"].eq(frame["label_raw"])
        frame["luna_direction_agrees_analyst"] = frame["luna_position"].eq(
            frame["analyst_position"]
        )
        frame["luna_strength_agrees_analyst"] = frame["luna_strength_label"].eq(
            frame["analyst_strength_label"]
        )
    else:
        frame["luna_analyst_label"] = pd.NA
        frame["luna_strength_label"] = pd.NA
        frame["luna_strength"] = np.nan
        frame["luna_label_exact_match"] = pd.NA
        frame["luna_direction_agrees_analyst"] = pd.NA
        frame["luna_strength_agrees_analyst"] = pd.NA

    for provider in ("analyst", "claude", "luna"):
        position = frame[f"{provider}_position"]
        active = position.notna() & position.ne(0)
        frame[f"{provider}_directional_call"] = active
        frame[f"{provider}_correct"] = nullable_bool(
            position.eq(frame["realized_position"]), active
        )
        frame[f"{provider}_correct_close_to_close"] = nullable_bool(
            position.eq(frame["close_to_close_realized_position"]), active
        )
        frame[f"{provider}_trade_gross_return"] = np.where(
            active, position.astype(float) * frame["raw_ret"], np.nan
        )
        frame[f"{provider}_trade_net_return_25bps"] = np.where(
            active, frame[f"{provider}_trade_gross_return"] - COST, np.nan
        )
        frame[f"{provider}_strategy_gross_return"] = np.where(
            frame[f"{provider}_response_available"],
            np.where(active, frame[f"{provider}_trade_gross_return"], 0.0),
            np.nan,
        )
        frame[f"{provider}_strategy_net_return_25bps"] = np.where(
            frame[f"{provider}_response_available"],
            np.where(active, frame[f"{provider}_trade_net_return_25bps"], 0.0),
            np.nan,
        )

    frame["comparison_set_claude_directional_26"] = frame[
        "claude_directional_call"
    ]
    if int(frame["comparison_set_claude_directional_26"].sum()) != 26:
        raise SystemExit("old Claude directional comparison set is not 26")

    all_metrics = {p: provider_metrics(frame, p) for p in ("analyst", "claude", "luna")}
    full_frame = frame[frame["analyst_strength_label"] == "full"]
    soft_frame = frame[frame["analyst_strength_label"] == "soft"]
    shared_frame = frame[frame["comparison_set_claude_directional_26"]]
    luna_label_summary = None
    if forced_luna:
        luna_label_summary = {
            "counts": {
                str(k): int(v) for k, v in
                frame["luna_analyst_label"].value_counts().sort_index().items()
            },
            "exact_label_matches": int(frame["luna_label_exact_match"].sum()),
            "direction_agreement_with_analyst": float(
                frame["luna_direction_agrees_analyst"].mean()
            ),
            "strength_agreement_with_analyst": float(
                frame["luna_strength_agrees_analyst"].mean()
            ),
            "predicted_full": provider_metrics(
                frame[frame["luna_strength_label"] == "full"], "luna"
            ),
            "predicted_soft": provider_metrics(
                frame[frame["luna_strength_label"] == "soft"], "luna"
            ),
        }
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "all_63": all_metrics,
        "analyst_full_strength_27": {
            p: provider_metrics(full_frame, p) for p in ("analyst", "claude", "luna")
        },
        "analyst_soft_strength_36": {
            p: provider_metrics(soft_frame, p) for p in ("analyst", "claude", "luna")
        },
        "claude_directional_26": {
            p: provider_metrics(shared_frame, p) for p in ("analyst", "claude", "luna")
        },
        "paired_analyst_luna_all_63": paired_split(frame, "analyst", "luna"),
        "paired_claude_luna_on_26": paired_split(shared_frame, "claude", "luna"),
        "paired_analyst_claude_on_26": paired_split(shared_frame, "analyst", "claude"),
        "luna_usage": token_totals(args.luna_manifest),
        "luna_analyst_labels": luna_label_summary,
        "cost_bps": 25,
        "timing_verification": {
            "events_verified": int(frame["timing_verified"].sum()),
            "after_close_next_trading_day": int(frame["report_session"].eq("after_close").sum()),
            "pre_open_same_trading_day": int(frame["report_session"].eq("pre_open").sum()),
            "human_calendar_events": int(frame["timing_source"].eq("frozen human analyst calendar").sum()),
            "primary_record_overrides": int(frame["timing_source"].ne("frozen human analyst calendar").sum()),
        },
        "primary_window": {
            "direction_outcome": "sign(ar0_intra): market-adjusted open(t0) to close(t0)",
            "trade_return": "raw stock open(t0) to close(t0)",
            "entry_rule": "open_t0_to_close_t0 for every event",
        },
    }

    keep = [
        "ticker", "slug", "symbol", "period", "filed_dt", "info_dt",
        "event_dt", "report_date", "report_session", "report_timestamp",
        "timing_source", "report_source_reference", "expected_t0",
        "timing_verified", "t0", "entry_rule", "price_open_t0", "price_close_t0",
        "volume_t0", "index_open_t0", "index_close_t0", "index_ret_open_to_close",
        "ar0_intra", "source_ar0_intra", "raw_ret", "ar0_cc", "ar0_gap", "i_ret0",
        "source_entry_rule", "source_raw_ret", "median_peso_volume",
        "realized_direction", "realized_position",
        "close_to_close_realized_direction", "close_to_close_realized_position",
        "label_raw",
        "analyst_direction", "analyst_position", "analyst_strength",
        "analyst_strength_label", "analyst_response_available",
        "analyst_directional_call", "analyst_correct", "analyst_correct_close_to_close",
        "analyst_trade_gross_return", "analyst_trade_net_return_25bps",
        "analyst_strategy_gross_return", "analyst_strategy_net_return_25bps",
        "claude_response_available", "claude_direction", "claude_position",
        "claude_conviction", "claude_reason_code", "claude_reason_code_secondary",
        "claude_guidance_direction", "claude_beat_quality", "claude_rationale",
        "claude_directional_call", "claude_correct", "claude_correct_close_to_close",
        "claude_trade_gross_return",
        "claude_trade_net_return_25bps", "claude_identified",
        "claude_strategy_gross_return", "claude_strategy_net_return_25bps",
        "claude_probe_confidence",
        "luna_response_available", "luna_direction", "luna_position",
        "luna_analyst_label", "luna_strength", "luna_strength_label",
        "luna_label_exact_match", "luna_direction_agrees_analyst",
        "luna_strength_agrees_analyst",
        "luna_conviction", "luna_reason_code", "luna_reason_code_secondary",
        "luna_guidance_direction", "luna_beat_quality", "luna_rationale",
        "luna_directional_call", "luna_correct", "luna_correct_close_to_close",
        "luna_trade_gross_return",
        "luna_trade_net_return_25bps", "luna_strategy_gross_return",
        "luna_strategy_net_return_25bps",
        "comparison_set_claude_directional_26",
    ]
    export = frame[keep].sort_values(["t0", "ticker"])
    args.output_dir.mkdir(parents=True, exist_ok=True)
    export.to_csv(args.output_dir / "trades_63.csv", index=False)
    export.to_parquet(args.output_dir / "trades_63.parquet", index=False)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8"
    )
    write_report(frame, summary, args.output_dir)
    write_comparison_26(frame, args.output_dir)
    write_responses(frame, args.output_dir)
    write_response_jsonl(frame, args.output_dir)
    write_readme(args.output_dir)
    write_data_dictionary(args.output_dir)
    write_price_window_audit(frame, args.output_dir)
    write_visual_report(frame, summary, args.output_dir)
    write_bundle_manifest(args.output_dir)
    print(json.dumps({
        "rows": len(export), "luna_responses": int(frame["luna_response_available"].sum()),
        "output_dir": str(args.output_dir),
    }))


if __name__ == "__main__":
    main()
