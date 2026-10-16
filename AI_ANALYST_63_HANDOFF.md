# Analyst vs Luna: 63-event post-report day-trading handoff

## Start here

Use the peer-facing package at:

- `earnings/outputs/codex_luna_forced_63/handoff/OPEN_ME_FIRST.html`
- `earnings/outputs/codex_luna_forced_63/analyst_luna_63_peer_bundle.zip`
- `outputs/ai_analyst_63_clear_20260824/Analyst_Luna_Post_Report_63.xlsx`

The deliverable covers exactly 63 earnings events and compares only the original
analyst call with the forced-choice Luna call. Luna could not abstain and could
only select from the directional labels available to the analyst.

## Reaction-session rule

- After-close report: trade the next date in the frozen IPC trading calendar.
- Pre-open report: trade the report date itself, which must be an IPC trading day.
- All 63 events pass the timing audit: 48 after-close and 15 pre-open.
- Directional correctness uses stock open-to-close minus IPC open-to-close.
- Trading return uses direction-aligned raw stock open-to-close, less 25 bps.

## Verified headline results

| Reader | Relative hits | Relative hit rate | Net-profitable trades | Net win rate | Mean net return |
|---|---:|---:|---:|---:|---:|
| Analyst | 43 / 63 | 68.3% | 42 / 63 | 66.7% | +2.0% |
| Luna | 41 / 63 | 65.1% | 36 / 63 | 57.1% | +0.4% |

Paired outcomes: same direction 45; both correct 33; analyst only correct 10;
Luna only correct 8; both wrong 12.

## Analyst conviction reconciliation

The remembered high-conviction analyst result is correct under the original
research definition, but that is not the literal day-trading window:

| Full-conviction analyst calls | Result |
|---|---:|
| Original adjusted close-to-close directional hit | 25 / 27 = 92.6% |
| Strict market-relative reaction-session open-to-close hit | 24 / 27 = 88.9% |
| Gross profitable reaction-session trade | 23 / 27 = 85.2% |
| Net profitable after 25 bps | 22 / 27 = 81.5% |

## Integration guidance

The Excel workbook is the clearest handoff for a dashboard developer:

- `Summary`: audited aggregate metrics and conviction breakouts.
- `Event Results`: one row per verified event with timing, prices, outcomes,
  analyst call, Luna call, and both return definitions.
- `Luna Responses`: all 63 saved calls and rationales.
- `Methodology`: timing rules, metric definitions, and source records.

Use `slug + period` as the stable join key when adding this view to the existing
analyst trading dashboard.

## Rebuild

Run:

```bash
python3 earnings/scripts/build_analyst_luna_peer_bundle.py
```

The script rebuilds the sendable HTML, CSV, Parquet, JSONL, documentation,
manifest, and ZIP from the audited 63-event source and copies the verified Excel
workbook into the package.
