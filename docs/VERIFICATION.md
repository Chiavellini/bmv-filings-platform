# Verification gate — the normalized manual-verification protocol

Every company's excel must pass the **verification gate** before it is "done". The
gate replaces ad-hoc, reactive fixing (spotting a bad metric after the fact) with a
deterministic loop that, from the *first* extraction, surfaces exactly which cells a
human/subagent must verify and reinforces the sheet with the verified values.

Strength bar (the gate's `is_strong`): **no unexplained suspects** — every SUSPECT
cell is resolved AND every disclosed metric is at its coverage ceiling. Metrics that
are genuinely undisclosed (N/D, pre-era, ungrabbable source) are declared
`EXPECTED_EMPTY` in the config and never chased.

## The loop

```
                ┌──────────────────────────────────────────────┐
                │ 1. python3 scripts/verify_extraction.py <slug>│  re-extract + score
                └───────────────┬──────────────────────────────┘
                                │ prints scorecard + writes <slug>_worklist.json
                  is_strong? ───┤
                       yes →  DONE (excel is strong)
                        no ↓
                ┌──────────────────────────────────────────────┐
                │ 2. spawn ONE subagent per worklist item       │  (the protocol below)
                │    → collect verdicts.json                    │
                └───────────────┬──────────────────────────────┘
                                ↓
                ┌──────────────────────────────────────────────┐
                │ 3. verify_extraction.py <slug> --apply verdicts.json │ → data/verified/<slug>.csv
                └───────────────┬──────────────────────────────┘
                                └── back to (1).  Each pass must shrink the suspect
                                    count; recurring root causes → fix the extractor.
```

## Cell classification (the scorecard)
- **STRONG** — disclosed, well-covered, no suspects.
- **WEAK** — disclosed but coverage below its ceiling → worklist (priority 2: fill).
- **SUSPECT** — has cells that are negative-where-positive-expected, magnitude
  breaks vs their surrounding periods (ratio vs neighbor-window median, both
  directions — catches "hundreds for ten quarters, then single-digit"), out of a
  configured range, identity-failures, or low confidence → worklist (priority 1:
  verify/correct). Detection lives in `src/eval/series_checks.py`, shared with
  `pipeline.run` (which attaches `df.attrs['suspects']`) so the workbook marks and
  the gate scores the SAME cells.
- **EMPTY** — disclosed but zero coverage.
- **EXPECTED_EMPTY** — declared undisclosed; excluded from the bar.
- **Unresolved** (scorecard column) — verification was attempted and the source
  could not settle the cell (`UNRESOLVED` verdict). Doesn't create a Suspect or
  block STRONG (it is an *explained* suspect) but stays counted; the cell ships
  with its red-flag comment.

## Workbook marking (what the analyst sees)
Suspect / low-confidence / flagged cells carry **no fill and no colored font** —
the cell Comment's native red-flag indicator is the only marker; the note names the
check that fired, the source tier and confidence, and (for UNRESOLVED cells) that
verification was attempted. Green fill = `[verified]` pin; red fill = expected but
blank. **Verify-before-flag:** a deliverable cell keeps its flag comment ONLY when
the Stage-5 loop could not resolve it — everything else ends confirmed (green),
corrected, or blanked. The workbook also carries **auto-inserted live Check rows**
(segment-sum per section + trusted accounting identities from
`series_checks.IDENTITIES`), inserted only where the data structure genuinely
reconciles on most periods; disable per company with `auto_checks: false` in the
config.

## The subagent prompt template (one per worklist item)
> Verify a single extracted financial value for **<COMPANY>**. Open
> `data/reports/<slug>/<period>.md`. The metric is **<key>** (<human label>); the
> extractor produced **<value>** (reason flagged: <reason>). Find the correct value
> for **this exact period and metric** in the report. Classify whether the report
> figure is the **quarterly** value (what we want), the **annual/cumulative** value,
> or a **prior-year** column — and return the quarterly one. Figures are in
> <units>. Return JSON: `{"period","key","value": <number or null>, "note": "<source line + classification>"}`.
> Return `value: null` only if the report genuinely does not disclose it
> (→ that cell stays blank/red and the metric should be marked EXPECTED_EMPTY in config).
> Return `value: "UNRESOLVED"` if the source is genuinely unreadable/ambiguous for this
> cell (→ the extracted value ships with a red-flag comment recording the attempt).

Batch the items (≤ ~10 concurrent subagents); a `parallel()` Workflow stage is a
good fit when the worklist is large.

## Reinforcement (`[verified]` override layer)
`--apply` writes confirmed values to `data/verified/<slug>.csv`
(`period,key,value,note`). On the next build `pipeline.run` overlays them at the
highest precedence (tag `[verified]`, confidence 1.0): they **fill missing cells and
correct wrong ones**, and the workbook renders them **green**. Manual work is never
lost across re-extractions. Verified cells are excluded from the *extraction*-accuracy
denominator (they are manual), but count as correct in the final sheet.

## Two outcomes per verdict
1. **Systematic** — if a root cause repeats across cells (e.g. an annual figure
   grabbed as quarterly, a wrong sign), fix the extractor/config so it never recurs;
   the loop converges instead of whack-a-mole.
2. **Override** — genuinely one-off prose values that no pattern can generalize go
   into `data/verified/<slug>.csv`.

## Per-company tuning (`configs/<slug>.yaml: metric_expectations`)
```yaml
metric_expectations:
  capex:        {sign: positive}
  net_new_stores: {sign: any}              # closures make it legitimately negative
  units_mercado:  {window: "2019-1T:"}     # disclosure window (denominator)
  e_commerce_sales: {expected_empty: true} # N/D — never chased
  total_units:    {range: [600, 1000]}     # plausibility band
```
