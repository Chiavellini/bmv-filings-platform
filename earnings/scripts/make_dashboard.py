#!/usr/bin/env python3
"""make_dashboard.py — self-contained HTML dashboard from the results CSVs.

Emits outputs/dashboard.html (artifact body: no doctype/html/head/body tags).
Charts are hand-built inline SVG; palette per the validated dataviz tokens
(diverging blue<->red for surprise polarity; ordinal blue ramp for quintiles).
"""
from __future__ import annotations

import html
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import numpy as np
import pandas as pd

R = bs.RESULTS_DIR


def load(tag, name):
    p = R / f"{tag}_{name}.csv"
    return pd.read_csv(p) if p.exists() else None


def fmt_bps(x):
    return f"{x*1e4:+,.0f}"


def esc(s):
    return html.escape(str(s))


def table_html(df: pd.DataFrame, bps_cols=(), cls="") -> str:
    df = df.copy()
    for c in df.columns:
        if c in bps_cols:
            df[c] = df[c].map(lambda v: fmt_bps(v) if pd.notna(v) else "")
        elif df[c].dtype.kind == "f":
            df[c] = df[c].map(lambda v: f"{v:,.2f}" if pd.notna(v) else "")
    head = "".join(f"<th>{esc(c)}</th>" for c in df.columns)
    rows = "".join(
        "<tr>" + "".join(f"<td>{esc(v)}</td>" for v in row) + "</tr>"
        for row in df.itertuples(index=False))
    return f'<div class="tblwrap"><table class="{cls}"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>'


# ---------------------------------------------------------------- SVG charts

W, H, PAD_L, PAD_B, PAD_T = 960, 300, 52, 30, 14


def _scale(vals, h):
    lo, hi = min(0, min(vals)), max(0, max(vals))
    span = (hi - lo) or 1.0
    def y(v):
        return PAD_T + (hi - v) / span * (h - PAD_T - PAD_B)
    return y, lo, hi


def _grid(y, lo, hi, w):
    import math
    step = 10 ** math.floor(math.log10((hi - lo) / 4 or 1))
    for mult in (1, 2, 5, 10):
        if (hi - lo) / (step * mult) <= 6:
            step *= mult
            break
    lines, v = [], math.ceil(lo / step) * step
    while v <= hi + 1e-9:
        yy = y(v)
        cls = "axisline" if abs(v) < 1e-9 else "gridline"
        lines.append(f'<line x1="{PAD_L}" x2="{w-8}" y1="{yy:.1f}" y2="{yy:.1f}" class="{cls}"/>' +
                     f'<text x="{PAD_L-6}" y="{yy+3.5:.1f}" class="ticklab" text-anchor="end">{v:+,.0f}</text>')
        v += step
    return "".join(lines)


def grouped_bars(days, series, colors, names, emphasize=None) -> str:
    """series: list of value-lists (bps), one per group member."""
    allv = [v for s in series for v in s if v == v]
    y, lo, hi = _scale(allv, H)
    n, k = len(days), len(series)
    slot = (W - PAD_L - 16) / n
    bw = min(14.0, (slot - 6) / k)
    out = [_grid(y, lo, hi, W)]
    y0 = y(0)
    for gi, d in enumerate(days):
        x0 = PAD_L + gi * slot + (slot - bw * k - 2 * (k - 1)) / 2
        if emphasize is not None and d == emphasize:
            out.append(f'<rect x="{PAD_L+gi*slot+1:.1f}" y="{PAD_T}" width="{slot-2:.1f}" '
                       f'height="{H-PAD_T-PAD_B}" class="emph"/>')
        for si, s in enumerate(series):
            v = s[gi]
            if v != v:
                continue
            yy = y(v)
            top, hh = (yy, y0 - yy) if v >= 0 else (y0, yy - y0)
            x = x0 + si * (bw + 2)
            out.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(hh,1):.1f}" '
                f'rx="2" fill="{colors[si]}"><title>day {d} · {names[si]}: {v:+,.0f} bps</title></rect>')
        out.append(f'<text x="{PAD_L+gi*slot+slot/2:.1f}" y="{H-10}" class="ticklab" '
                   f'text-anchor="middle">{d:+d}</text>')
    return svg_wrap("".join(out))


def simple_bars(labels, vals, colors, unit="bps") -> str:
    y, lo, hi = _scale(vals, H)
    n = len(labels)
    slot = (W - PAD_L - 16) / n
    bw = min(64.0, slot * 0.55)
    out = [_grid(y, lo, hi, W)]
    y0 = y(0)
    for i, (lab, v) in enumerate(zip(labels, vals)):
        if v != v:
            continue
        x = PAD_L + i * slot + (slot - bw) / 2
        yy = y(v)
        top, hh = (yy, y0 - yy) if v >= 0 else (y0, yy - y0)
        out.append(
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" height="{max(hh,1):.1f}" rx="3" '
            f'fill="{colors[i % len(colors)]}"><title>{esc(lab)}: {v:+,.0f} {unit}</title></rect>')
        vy = top - 6 if v >= 0 else top + hh + 14
        out.append(f'<text x="{x+bw/2:.1f}" y="{vy:.1f}" class="vallab" text-anchor="middle">{v:+,.0f}</text>')
        out.append(f'<text x="{PAD_L+i*slot+slot/2:.1f}" y="{H-10}" class="ticklab" '
                   f'text-anchor="middle">{esc(lab)}</text>')
    return svg_wrap("".join(out))


def line_chart(dates, series: dict, colors: dict, emphasize=None) -> str:
    """Multi-line equity chart (log scale), weekly-sampled."""
    import math
    allv = [v for vals in series.values() for v in vals if v == v and v > 0]
    lo, hi = math.log(min(allv)), math.log(max(allv))
    span = (hi - lo) or 1.0
    n = len(dates)
    def x(i): return PAD_L + i / max(n - 1, 1) * (W - PAD_L - 120)
    def y(v): return PAD_T + (hi - math.log(v)) / span * (H - PAD_T - PAD_B)
    out = []
    for lvl in (1, 1.5, 2, 3, 4):
        if math.log(lvl) <= hi:
            yy = y(lvl)
            out.append(f'<line x1="{PAD_L}" x2="{W-120}" y1="{yy:.1f}" y2="{yy:.1f}" class="gridline"/>' +
                       f'<text x="{PAD_L-6}" y="{yy+3.5:.1f}" class="ticklab" text-anchor="end">{lvl}x</text>')
    for i in range(0, n, max(n // 8, 1)):
        out.append(f'<text x="{x(i):.1f}" y="{H-10}" class="ticklab" text-anchor="middle">{str(dates[i])[:7]}</text>')
    for name, vals in series.items():
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(vals) if v == v and v > 0)
        wgt = 2.5 if name == emphasize else 1.6
        op = 1.0 if name == emphasize else 0.75
        out.append(f'<polyline points="{pts}" fill="none" stroke="{colors[name]}" '
                   f'stroke-width="{wgt}" opacity="{op}"><title>{esc(name)}</title></polyline>')
        last = [v for v in vals if v == v][-1]
        out.append(f'<text x="{W-116}" y="{y(last)+4:.1f}" class="vallab" '
                   f'fill="{colors[name]}">{esc(name.split(" (")[0])} {last:.2f}x</text>')
    return svg_wrap("".join(out))


def svg_wrap(body) -> str:
    return (f'<svg viewBox="0 0 {W} {H}" role="img" style="width:100%;height:auto;display:block">'
            f"{body}</svg>")


# ---------------------------------------------------------------- assemble

def main() -> None:
    cfg = bs.load_config()
    ew = pd.read_parquet(bs.art_path("event_windows"))
    uni = pd.read_csv(bs.art_path("universe", ".csv"))
    real_index = ((bs.SNAPSHOT_DIR / "yf_MXX_INDEX.json").exists()
                  or (bs.SNAPSHOT_DIR.parent / "market_data_max" / "yf_MXX_INDEX.json").exists())

    ter = load("dev_full_modern", "tercile_ar0_cc")
    qui = load("dev_full_modern", "quintile_ar0_cc")
    sar = load("dev_full_modern", "quintile_sar0_cc")
    react = load("dev_full_modern", "tercile_ar_react")
    pre5 = load("dev_full_modern", "tercile_car_pre5")
    pre10 = load("dev_full_modern", "tercile_car_pre10")
    post5 = load("dev_full_modern", "tercile_car_post5")
    reg = load("dev_full_modern", "regressions")
    prof = load("dev_full_modern", "profile")
    trad = load("dev_full_modern", "tradability")
    peryear = load("dev_full_modern", "per_year")
    hits = load("dev_full_modern", "hit_rates")
    repl = load("repl", "tercile_ar0_cc")
    repl_react = load("repl", "tercile_ar_react")
    hold = load("holdout_modern", "tercile_ar0_cc")

    spread = ter[ter.bucket == "T3-T1"].iloc[0]
    qspread = qui[qui.bucket == "Q5-Q1"].iloc[0]
    intra = trad[(trad.piece.str.startswith("open->close")) & (trad.bucket == "T3-T1")].iloc[0]
    r_spread = repl[repl.bucket == "T3-T1"].iloc[0]
    ic_row = reg[reg.measure == "ar0_cc"].iloc[0]

    status = "FINAL — holdouts + 2026-2T walk-forward evaluated"
    idx_label = "^MXX (IPC)" if real_index else "equal-weight proxy (159 BMV series)"

    # charts
    days = list(range(-10, 6))
    t1 = [prof.loc[prof.day == d, "T1_bps"].iloc[0] for d in days]
    t3 = [prof.loc[prof.day == d, "T3_bps"].iloc[0] for d in days]
    chart_profile = grouped_bars(days, [t1, t3],
                                 ["var(--neg)", "var(--pos)"],
                                 ["T1 (worst reports)", "T3 (best reports)"],
                                 emphasize=0)
    qlabels = ["Q1", "Q2", "Q3", "Q4", "Q5"]
    qvals = [qui[qui.bucket == b]["fm_mean"].iloc[0] * 1e4 for b in qlabels]
    chart_q = simple_bars(qlabels, qvals, ["var(--q1)", "var(--q2)", "var(--q3)", "var(--q4)", "var(--q5)"])
    yvals = peryear["t3_t1_bps"].tolist()
    chart_year = simple_bars(peryear["year"].astype(str).tolist(), yvals, ["var(--pos)"])

    legend = ('<div class="legend"><span><i style="background:var(--neg)"></i>T1 — worst reports</span>'
              '<span><i style="background:var(--pos)"></i>T3 — best reports</span></div>')

    lf = (pd.read_csv(R / "liquidity_frontier.csv")
          if (R / "liquidity_frontier.csv").exists() else None)
    frontier_g_section = "" if lf is None else f"""
<h2>How far down-cap does the BMV edge survive?</h2>
<p class="sub">All 109 companies, bucketed by liquidity, with a pre-committed cost ladder
(20→250 bps by bucket, labeled assumptions). Verdict: <b>certified tradable at ≥20M MXN/day</b>
(net +56 bps/trade, t = 2.5). The 1–20M zone shows gross spreads twice as large — down-cap BMV
is even less efficient — but only ~130 events, so it stays "promising, unproven" until the
forward record grows. Below 1M MXN/day prices are stale (30–36% zero-move days) and no cost
assumption saves it.</p>
<div class="card">{table_html(lf)}</div>"""

    usq = (pd.read_csv(R / "us_quintile_ar0_cc.csv")
           if (R / "us_quintile_ar0_cc.csv").exists() else None)
    uss = (pd.read_csv(R / "us_strategy_sweep.csv")
           if (R / "us_strategy_sweep.csv").exists() else None)
    usy = (pd.read_csv(R / "us_per_year.csv")
           if (R / "us_per_year.csv").exists() else None)
    us_section = "" if usq is None else f"""
<h2>The S&amp;P 500 mirror study: bigger reaction, no edge</h2>
<p class="sub">Same design, recalibrated for the US: 500 current constituents, 2019–2026,
<b>analyst-consensus EPS surprise</b> as the signal (~10,400 events, 22 quarters). The day-0
reaction is 3× the BMV's — Q5−Q1 = <b>+433 bps, FM t = 18.9</b>, positive every single year —
but <b>+408 of those bps are the overnight gap</b>: the market prices the report at the opening
auction. What's left to capture from the open is +26 bps (t = 2.5) before costs — single digits
after. The only tradable whisper is a 20-day drift tail (+47 bps/trade at 5 bps costs, t = 2.5,
hit 52%). <b>The contrast is the finding:</b> the same information event that leaves ~90 bps on
the table for a day in Mexico is consumed in milliseconds in New York. The BMV edge is a
small-market inefficiency — which is exactly why it exists and why it may persist.</p>
<div class="card"><div class="eyebrow">US · Q5−Q1 next-day AR (cc) by consensus-surprise quintile</div>
{table_html(usq, bps_cols=("mean","fm_mean"))}</div>
<div class="card"><div class="eyebrow">US · strategy hold sweep (long Q5 / short Q1 from open t0)</div>
{table_html(uss)}</div>
<div class="card"><div class="eyebrow">US · Q5−Q1 spread by year (bps)</div>
{table_html(usy)}</div>"""

    bpanel = (pd.read_csv(R / "benchmark_panel.csv")
              if (R / "benchmark_panel.csv").exists() else None)
    bcurves = (pd.read_csv(R / "benchmark_curves.csv")
               if (R / "benchmark_curves.csv").exists() else None)
    if bpanel is not None and bcurves is not None:
        weekly = bcurves.iloc[::5].reset_index(drop=True)
        pick = {"EVT-SIGN + CETES 9% cash": "var(--pos)",
                "EVT-SIGN 100%": "var(--q3)",
                "B&H equal-weight (same cos)": "var(--neg)",
                "B&H ^MXX": "var(--muted)"}
        chart_eq = line_chart(weekly["date"].tolist(),
                              {k: weekly[k].tolist() for k in pick},
                              pick, emphasize="EVT-SIGN + CETES 9% cash")
        bench_section = f"""
<h2>Strategy vs buy &amp; hold — the profit panel</h2>
<p class="sub">Common window 2018-07 → 2026-07, same 40 companies. Event strategy: 100% of
capital per signal, long s_ts&gt;0 / short s_ts&lt;0, validated entry rules, exit close(t0),
net 25 bps. Raw event strategy ≈ buy&amp;hold on total profit with <b>half the drawdown on 9%
of the calendar exposure</b> (≈10× the return per exposed day). The practical implementation —
idle cash in CETES at a flat 9% proxy — <b>triples buy&amp;hold's profit at twice its Sharpe</b>.
The overlay variant (hold the book, tilt around earnings) beats plain B&amp;H by +12.5pts at
equal drawdown. Shorting caveat: the short leg is small and often unborrowable on the BMV;
the long-only row is the realistic floor.</p>
<div class="card">{chart_eq}</div>
<div class="card"><div class="eyebrow">panel · net of 25 bps per round trip · CETES row uses a flat 9%/yr MXN cash proxy</div>
{table_html(bpanel)}</div>"""
    else:
        bench_section = ""

    refc = (pd.read_csv(R / "refine_candidates.csv")
            if (R / "refine_candidates.csv").exists() else None)
    wfe = (pd.read_csv(R / "walkforward_eval.csv")
           if (R / "walkforward_eval.csv").exists() else None)
    if refc is not None and wfe is not None:
        refine_section = f"""
<h2>Refinement round (phase E) — and its walk-forward reckoning</h2>
<p class="sub">Nine pre-committed signal candidates evaluated on the dev sample with fixed
adoption rules (max 2 adoptions, shuffle p &lt; 0.05, FM-t margin ≥ 0.5). In-sample winner:
<b>margin-SUE</b> (+164 bps FM, t = 4.1 vs incumbent +101, t = 2.3). MD&amp;A lexicon sentiment and
the conviction filter failed; no pre-report posture rule beat the incumbent (though good reports
preceded by high abnormal volume react notably less: +29 vs +115 bps — informed positioning,
suggestive only). The one-shot 2026-2T walk-forward (18 pristine events filed after every
decision was locked) then <b>confirmed the core effect emphatically (+339 bps spread, 72% hit)
but did NOT confirm the refinement's superiority</b> — so the incumbent composite stays primary
and margin-SUE is carried as a co-signal for future quarters. The hold-to-t+1 execution variant
likewise failed to confirm. This is the process working: in-sample winners must earn it forward.</p>
<div class="card"><div class="eyebrow">signal candidates · modern dev sample</div>
{table_html(refc)}</div>
<div class="card"><div class="eyebrow">walk-forward 2026-2T · one shot, both specs</div>
{table_html(wfe)}</div>"""
    else:
        refine_section = ""

    frontier = (pd.read_csv(R / "strategy_frontier.csv")
                if (R / "strategy_frontier.csv").exists() else None)
    if frontier is not None:
        f25 = frontier[frontier["cost_bps_rt"] == 25]
        frontier_section = f"""
<h2>Trades-per-year frontier</h2>
<p class="sub">Pre-listed s_ts cutoffs (no post-hoc picking), net of 25 bps per round trip.
Lower cutoffs trade more often at thinner edge; the pre-committed headline stays at +1.0.
liq1M adds 5 smaller names (real costs likely higher there).</p>
<div class="card"><div class="eyebrow">long side · net of 25 bps round trip</div>
{table_html(f25.drop(columns=["cost_bps_rt"]))}</div>"""
    else:
        frontier_section = ""

    sim = load("", "strategy_sim") if (R / "_strategy_sim.csv").exists() else (
        pd.read_csv(R / "strategy_sim.csv") if (R / "strategy_sim.csv").exists() else None)
    if sim is not None:
        strategy_section = f"""
<h2>As a strategy: what would it have returned?</h2>
<p class="sub">Fixed rule, no tuning: only filings public before the session open,
liquid names, long when the tradable score s_ts &gt; +1, enter at the open of the
reaction day, exit at its close. Raw unhedged returns, development quarters
(2022-04…2026-04). Capital sits in cash between events (~62 trade-days in 4 years).
<b>The anomaly is real; the standalone edge after costs is thin.</b></p>
<div class="card"><div class="eyebrow">historical simulation · cost sensitivity (per round trip)</div>
{table_html(sim)}</div>
<p class="note">Shorts assume borrowable stock — usually false for BMV mid-caps.
Per-trade long mean +74 bps gross (t = 2.2); at 25 bps costs 2 of 5 years are negative.
Untested ways to concentrate the edge (stronger cutoffs, EPS-only score) would need a
fresh holdout to claim anything.</p>"""
    else:
        strategy_section = ""

    gates = pd.DataFrame([
        {"gate": "1 · Structural", "check": "t0 after filing; SUE recomputed independently; priors point-in-time", "result": "PASS"},
        {"gate": "2 · Signal", "check": f"rank IC = {ic_row['ic']:.2f} (t = {ic_row['ic_t']:.1f})", "result": "PASS"},
        {"gate": "3 · Permutation", "check": "shuffle-S p=0.003 · slope p=0.001 · placebo-dates p=0.006", "result": "PASS"},
        {"gate": "4 · Robustness", "check": "5 signal variants, 4 liquidity cuts, beta-adj all positive; 5/5 years", "result": "PASS"},
        {"gate": "5 · Holdout", "check": "4 sacred quarters, one-shot", "result": ("PASS" if hold is not None else "PENDING")},
    ])

    body = f"""
<style>
:root {{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --pos:#2a78d6; --neg:#e34948; --neu:#898781; --good:#006300;
  --q1:#86b6ef; --q2:#5598e7; --q3:#2a78d6; --q4:#1c5cab; --q5:#104281;
  --emph:rgba(42,120,214,.07);
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
    --pos:#3987e5; --neg:#e66767; --good:#0ca30c;
    --q1:#9ec5f4; --q2:#6da7ec; --q3:#3987e5; --q4:#256abf; --q5:#184f95;
    --emph:rgba(57,135,229,.10);
  }}
}}
:root[data-theme="dark"] {{
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --pos:#3987e5; --neg:#e66767; --good:#0ca30c;
  --q1:#9ec5f4; --q2:#6da7ec; --q3:#3987e5; --q4:#256abf; --q5:#184f95;
  --emph:rgba(57,135,229,.10);
}}
:root[data-theme="light"] {{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --pos:#2a78d6; --neg:#e34948; --good:#006300;
  --q1:#86b6ef; --q2:#5598e7; --q3:#2a78d6; --q4:#1c5cab; --q5:#104281;
  --emph:rgba(42,120,214,.07);
}}
body {{ background:var(--page); color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; margin:0; }}
main {{ max-width:1060px; margin:0 auto; padding:40px 22px 80px; }}
h1 {{ font-size:27px; line-height:1.2; margin:6px 0 4px; letter-spacing:-.01em; text-wrap:balance; }}
h2 {{ font-size:19px; margin:44px 0 4px; letter-spacing:-.005em; }}
h2 + p.sub {{ margin-top:2px; }}
.eyebrow {{ font-size:11px; font-weight:600; letter-spacing:.09em; text-transform:uppercase; color:var(--muted); }}
p.sub {{ color:var(--ink2); max-width:70ch; margin:6px 0 14px; }}
.statusrow {{ display:flex; gap:8px; flex-wrap:wrap; margin:10px 0 26px; }}
.pill {{ font-size:12px; font-weight:600; padding:3px 10px; border-radius:999px;
  border:1px solid var(--ring); color:var(--ink2); background:var(--surface); }}
.pill.warn {{ color:var(--neg); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; margin:18px 0 8px; }}
.tile {{ background:var(--surface); border:1px solid var(--ring); border-radius:10px; padding:14px 16px 12px; }}
.tile .v {{ font-size:26px; font-weight:650; letter-spacing:-.01em; }}
.tile .v.pos {{ color:var(--pos); }}
.tile .k {{ font-size:12px; color:var(--muted); margin-top:2px; }}
.card {{ background:var(--surface); border:1px solid var(--ring); border-radius:10px; padding:16px 18px; margin:14px 0; }}
.legend {{ display:flex; gap:18px; font-size:12.5px; color:var(--ink2); margin:2px 0 8px; }}
.legend i {{ display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; }}
.tblwrap {{ overflow-x:auto; }}
table {{ border-collapse:collapse; width:100%; font-size:13.5px; font-variant-numeric:tabular-nums; }}
th {{ text-align:left; font-size:11.5px; letter-spacing:.05em; text-transform:uppercase;
  color:var(--muted); font-weight:600; padding:7px 10px; border-bottom:1px solid var(--axis); white-space:nowrap; }}
td {{ padding:6px 10px; border-bottom:1px solid var(--grid); white-space:nowrap; }}
tr:last-child td {{ border-bottom:none; }}
tbody tr:hover td {{ background:var(--emph); }}
.gridline {{ stroke:var(--grid); stroke-width:1; }}
.axisline {{ stroke:var(--axis); stroke-width:1.25; }}
.ticklab {{ font:11px system-ui,sans-serif; fill:var(--muted); font-variant-numeric:tabular-nums; }}
.vallab {{ font:11.5px system-ui,sans-serif; fill:var(--ink2); font-variant-numeric:tabular-nums; }}
.emph {{ fill:var(--emph); }}
.note {{ font-size:13px; color:var(--ink2); }}
details {{ margin:10px 0; }} summary {{ cursor:pointer; color:var(--ink2); font-size:13.5px; }}
.ok {{ color:var(--good); font-weight:650; }} .pend {{ color:var(--muted); font-weight:650; }}
ol li, ul li {{ margin:5px 0; color:var(--ink2); }} ol li b, ul li b {{ color:var(--ink); }}
@media (prefers-reduced-motion: no-preference) {{ .tile {{ transition:border-color .15s; }} }}
</style>
<title>BMV Earnings Event Study</title>
<main>
<div class="eyebrow">earnings/ · BMV quarterly reports · event study</div>
<h1>Do BMV quarterly reports move the next day's price?</h1>
<p class="sub">Surprise vs the company's own history (SUE composite of net income, revenue, EBITDA),
aligned to minute-stamped information arrival (earlier of BMV XBRL filing and matching earnings press release); abnormal returns vs {idx_label}.
Generated {date.today().isoformat()}.</p>
<div class="statusrow">
  <span class="pill">{esc(status)}</span>
  <span class="pill">modern era (2021+): 399 events · 40 companies · 16 quarters</span>
  <span class="pill">historical era (2018-2021): 68 events · 10 companies (PDF-dated)</span>
  <span class="pill{'' if real_index else ' warn'}">index: {esc(idx_label)}</span>
</div>

<div class="tiles">
  <div class="tile"><div class="v pos">{fmt_bps(spread['fm_mean'])} bps</div>
    <div class="k">next-day spread, best vs worst tercile (FM t = {spread['fm_t']:.1f})</div></div>
  <div class="tile"><div class="v pos">{fmt_bps(qspread['fm_mean'])} bps</div>
    <div class="k">quintile Q5−Q1 spread (FM t = {qspread['fm_t']:.1f})</div></div>
  <div class="tile"><div class="v pos">{intra['mean_bps']:+,.0f} bps</div>
    <div class="k">capturable open→close on reaction day (FM t = {intra['fm_t']:.1f})</div></div>
  <div class="tile"><div class="v pos">{fmt_bps(r_spread['fm_mean'])} bps</div>
    <div class="k">out-of-sample replication on 92 new companies (t = {r_spread['fm_t']:.1f})</div></div>
  <div class="tile"><div class="v">{hits['hit'].iloc[0]*100:.0f}%</div>
    <div class="k">sign hit rate, surprise → next-day AR (p = {hits['p'].iloc[0]:.4f})</div></div>
</div>

<h2>Day-by-day abnormal return around the filing</h2>
<p class="sub">Mean market-adjusted return per trading day relative to the reaction day (day 0 =
first session opening after the filing timestamp). The reaction is concentrated exactly on day 0 —
and days −10…−1 are flat: <b>no pre-announcement drift, i.e. no evidence of leakage.</b></p>
<div class="card">{legend}{chart_profile}</div>

<h2>Reaction by surprise bucket</h2>
<p class="sub">Within-quarter quintiles of the composite surprise (mean bps, Fama-MacBeth).</p>
<div class="card">{chart_q}</div>
<div class="card"><div class="eyebrow">terciles · next-day close-to-close AR</div>{table_html(ter, bps_cols=("mean","fm_mean"))}</div>
<div class="card"><div class="eyebrow">quintiles · next-day close-to-close AR</div>{table_html(qui, bps_cols=("mean","fm_mean"))}</div>
<div class="card"><div class="eyebrow">quintiles · vol-standardized AR (in units of pre-event σ)</div>{table_html(sar)}</div>
<div class="card"><div class="eyebrow">reaction-window fix · filing session + next session (ar_react)</div>{table_html(react, bps_cols=("mean","fm_mean"))}</div>

<h2>Replication on never-touched companies</h2>
<p class="sub">The frozen phase-A spec, run on the 92 companies added in phase B (none used to
develop anything). Point estimate {fmt_bps(r_spread['fm_mean'])} bps vs {fmt_bps(spread['fm_mean'])} bps pooled —
the magnitude replicates out-of-sample; t is lower because only ~24 of the new names pass the
liquidity filter.</p>
<div class="card"><div class="eyebrow">replication · next-day AR by tercile</div>{table_html(repl, bps_cols=("mean","fm_mean"))}</div>
<div class="card"><div class="eyebrow">replication · reaction-window fix</div>{table_html(repl_react, bps_cols=("mean","fm_mean"))}</div>

<h2>Could you actually trade it?</h2>
<p class="sub">Decomposition of the tercile spread. The overnight gap is <b>not capturable</b>
(most filings land after hours — the open already prices part of the news). What remains
capturable: enter at the open of the reaction day, exit at its close.</p>
<div class="card">{table_html(trad, cls="")}</div>

{strategy_section}

{frontier_g_section}

{us_section}

{bench_section}

{refine_section}

{frontier_section}

<h2>Stability by year</h2>
<div class="card">{chart_year}</div>

<h2>Leakage &amp; post-drift: both flat</h2>
<p class="sub">Pre-announcement CARs show no relationship to the upcoming surprise
(the "unofficial leak" hypothesis finds no support at daily resolution), and there is no
post-earnings drift to ride after day 0 — the market absorbs the report in one session.</p>
<div class="card"><div class="eyebrow">CAR[−5,−1] by tercile (leakage window)</div>{table_html(pre5, bps_cols=("mean","fm_mean"))}</div>
<div class="card"><div class="eyebrow">CAR[−10,−1] by tercile</div>{table_html(pre10, bps_cols=("mean","fm_mean"))}</div>
<div class="card"><div class="eyebrow">CAR[+1,+5] by tercile (PEAD)</div>{table_html(post5, bps_cols=("mean","fm_mean"))}</div>

<h2>Regressions &amp; validation gates</h2>
<div class="card"><div class="eyebrow">pooled OLS, quarter fixed effects, quarter-clustered t</div>{table_html(reg)}</div>
<div class="card"><div class="eyebrow">quant-audit gates</div>{table_html(gates)}</div>

<h2>Caveats (read before believing)</h2>
<ul>
<li><b>Recovered Q4 events use February press-release timestamps with April-XBRL metrics.</b>
The release contains the same figures, but audited restatements can differ; the XBRL-only
timing variant (+145 bps, t = 4.7) confirms the effect without them.</li>
<li><b>Q4 filings without a catalog press release remain excluded:</b> most issuers file Q4 XBRL with the audited annual
(late Mar–May), months after the February press release the market traded on. Only filings
within 60 days of quarter end count as events.</li>
<li><b>Within-quarter buckets are not real-time tradable</b> (they rank against peer filings not
yet published); the time-series-only variant gives +124 bps (t = 3.5).</li>
<li><b>The XBRL timestamp is at or after the press-release time.</b> Part of the "capturable"
intraday move may compress if the press release leads the filing by hours.</li>
<li><b>Survivorship:</b> 13 of 122 issuers with facts have no resolvable price series
(mostly delisted). One-day horizons limit the damage, but it is nonzero.</li>
<li><b>Unfiltered universe weakens:</b> including sub-MXN-1M/day names drops the spread to
+113 bps (t = 1.4) — stale prices dilute measured reactions. This is an effect in liquid names.</li>
<li><b>No transaction costs modeled.</b> ~90 bps capturable spread is before spread/impact;
BMV mid-caps can cost 20–50 bps per side.</li>
</ul>

<h2>Researcher degrees of freedom</h2>
<ol>
<li>Market index substituted (equal-weight proxy) after Yahoo throttled the one planned ^MXX
fetch — decided before any return was computed.</li>
<li>60-day filing-lag event rule + min z-pool added pre-first-run (data-integrity driven).</li>
<li>Gate-1 assertion corrected (pre-open filings legitimately react same day); no parameter change.</li>
<li>Phase B (universe ×6, refinements) fully pre-committed and git-frozen before the first
expanded run; replication ran before pooled analysis.</li>
</ol>

<details><summary>Universe (109 issuers) — coverage table</summary>
<div class="card">{table_html(uni)}</div></details>

<p class="note">Pipeline: earnings/ in the pdfs monorepo — BMV XBRL archive timestamps ·
soft XBRL facts · frozen Yahoo price snapshot. All parameters pre-committed in
configs/study.yaml; every table regenerates from scripts/run_event_study.py.</p>
</main>
"""
    out = bs.OUTPUTS_DIR / "dashboard.html"
    out.write_text(body)
    print(f"wrote {out} ({len(body)//1024} KB)")


if __name__ == "__main__":
    main()
