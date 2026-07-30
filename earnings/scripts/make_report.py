#!/usr/bin/env python3
"""make_report.py — assemble outputs/REPORT.md from the results CSVs."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import pandas as pd

CAVEATS = """\
## Lookahead-risk register

1. **Restated priors — resolved favorably.** Each SUE's year-ago comparison is
   the figure *as restated in the current filing*: exactly what the market saw
   at announcement time. Point-in-time correct by construction.
2. **Q4 XBRL is usually the audited annual, not the earnings event.** Most
   issuers file Q4 XBRL 72–127 days after quarter end (late Mar–May), months
   after the February press release the market reacted to. Filings with lag
   > 60 days are excluded as events (they still feed SUE histories). Only
   WALMEX and KIMBER file Q4 XBRL with their February results.
3. **SUE history availability mask.** A trailing quarter enters an event's
   sigma only if its filing timestamp strictly predates the event's filing —
   conservative (may exclude data public earlier via press release), never
   lookahead with respect to our own data.
4. **In-session filings (~40%).** For filings during trading hours, t0 is the
   NEXT session, so part of the reaction lands on the filing day itself. The
   filing-day AR is reported separately; the primary AR0 is conservative.
5. **XBRL timestamp >= press-release time.** Even for timely filings the press
   release may precede the XBRL by hours. Apparent "pre-announcement drift"
   concentrated at t-1 is likely this timing artifact, not leakage; smooth
   drift over t-10..t-2 is the leakage signature.
6. **Survivorship.** The price snapshot covers currently-resolvable symbols;
   phase A universe is 17 large, surviving issuers. Results are conditional on
   survival (mild at 1-day horizons). See outputs/universe.csv.
7. **Within-quarter z-scores are not tradable in real time** (they use peer
   filings not yet published at a given firm's event). The sign / fixed-cutoff
   s_ts variants are fully point-in-time and tradable.
8. **Refilings.** 79 (ticker, period) pairs in the full archive have multiple
   filings; the earliest timestamp is used and events are flagged is_refiled.
9. **Data window truncation.** Prices end 2026-07-21: CAR[+1,+20] is null (not
   zero) where incomplete. SUE needs >=4 trailing dX quarters, so scoreable
   events start 2022-2T.
"""


def md_table(df: pd.DataFrame) -> str:
    """Markdown table without the tabulate dependency."""
    num = df.select_dtypes("number").columns
    df = df.copy()
    df[num] = df[num].round(4)
    cells = [
        ["" if pd.isna(v) else str(v) for v in row]
        for row in df.itertuples(index=False)
    ]
    header = list(df.columns)
    lines = ["| " + " | ".join(header) + " |",
             "|" + "|".join(["---"] * len(header)) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in cells]
    return "\n".join(lines) + "\n"


def table(tag: str, name: str) -> str:
    p = bs.RESULTS_DIR / f"{tag}_{name}.csv"
    if not p.exists():
        return f"_missing: {p.name}_\n"
    return md_table(pd.read_csv(p))


DOF_LOG = """\
## Researcher degrees of freedom (changes after the config freeze)

1. **Market index substituted** (2026-07-27): Yahoo 429-throttled the one
   planned ^MXX fetch; the study ran with an equal-weight proxy built from the
   159 frozen local price series instead. Decided before any return had been
   computed. Upgrade path: run build_prices.py --fetch-index, re-run.
2. `min_cs_pool: 5` and the 60-day filing-lag event rule were added to the
   config before the first event-study run (pre-freeze, data-integrity driven).
3. A gate-1 assertion was corrected (pre-open filings legitimately react the
   same calendar day); no analysis parameter changed.
"""


def main() -> None:
    ew = pd.read_parquet(bs.art_path("event_windows"))
    uni = pd.read_csv(bs.art_path("universe", ".csv"))
    real_index = (bs.SNAPSHOT_DIR / "yf_MXX_INDEX.json").exists()
    index_note = ("^MXX (IPC)" if real_index
                  else "equal-weight proxy of the 159 frozen BMV series "
                       "(^MXX throttled; see degrees-of-freedom log)")
    holdout_run = (bs.RESULTS_DIR / "holdout_tercile_ar0_cc.csv").exists()
    status = ("FINAL — sacred holdout evaluated" if holdout_run
              else "INTERIM — sacred holdout NOT yet run")

    parts = [f"""# BMV Earnings Event Study — Phase A Report

**Status: {status}**

*Generated {date.today().isoformat()} · repo HEAD {bs.repo_head()[:12]} ·
17 configured issuers, XBRL quarters 2021-2T..2026-1T, prices 2020-07..2026-07 ·
market adjustment: {index_note}.*

**Hypothesis.** A "good"/"bad" BMV quarterly report (surprise vs the company's
own history) predicts the next trading day's market-adjusted return; and/or
prices drift before the filing (leakage).

**Design.** Composite surprise S = mean of within-quarter z-scores of SUE
(net income, revenue, EBITDA), SUE = YoY change / sigma of trailing YoY changes
(>=4 quarters, availability-masked by filing timestamps). t0 = first session
opening after the filing timestamp (minute-level, from the BMV XBRL archive).
Returns market-adjusted vs the index above. Events require filing lag <= 60 days and the
primary liquidity filter (median daily peso volume >= MXN 5M over [t-70,t-11]).

## Sample

- events with measures: {len(ew)} (all universe filings with facts+prices)
- analyzable development-sample events: see tables (holdout quarters
  {ew.attrs.get('holdout', '2022-4T, 2023-3T, 2024-2T, 2025-4T')} excluded
  until the one-shot evaluation)

## Universe

{md_table(uni)}

## Next-day reaction (AR0_cc, market-adjusted close-to-close) by surprise tercile

{table('dev', 'tercile_ar0_cc')}
Open-gap component:

{table('dev', 'tercile_ar0_gap')}
Intraday component:

{table('dev', 'tercile_ar0_intra')}

## Leakage test — pre-announcement drift by tercile

CAR[-5,-1]:

{table('dev', 'tercile_car_pre5')}
CAR[-10,-1]:

{table('dev', 'tercile_car_pre10')}

## Post-earnings drift (PEAD)

CAR[+1,+5]:

{table('dev', 'tercile_car_post5')}
CAR[+1,+20]:

{table('dev', 'tercile_car_post20')}

## Regressions (quarter fixed effects, quarter-clustered t)

{table('dev', 'regressions')}

## Tradable variant — sign/cutoff buckets on s_ts

{table('dev', 'sign_ar0_cc')}
{table('dev', 'hit_rates')}

## Day-by-day mean AR (bps) around the event

{table('dev', 'profile')}

## Holdout (Gate 5)

{table('holdout', 'tercile_ar0_cc')}

{CAVEATS}

{DOF_LOG}
"""]
    out = bs.OUTPUTS_DIR / "REPORT.md"
    out.write_text("\n".join(parts))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
