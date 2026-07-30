#!/usr/bin/env python3
"""make_dissection_report.py — self-contained HTML dissection of the audit-v3
wave: timing correction (v2->v3), signal layer, analyst evaluation + in-session
verification, and the pre-committed analyst overlay run.

Emits outputs/dissection_report.html (artifact body, no doctype/head/body).
Reads only on-disk CSVs/MD — no recomputation. Chart helpers and the validated
dataviz palette are shared with make_dashboard.py.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from earnlib import bootstrap as bs

import pandas as pd

sys.path.insert(0, str(bs.EARNINGS_ROOT / "scripts"))
from make_dashboard import (esc, fmt_bps, simple_bars, svg_wrap,  # noqa: E402
                            table_html, _scale, _grid, W, H, PAD_L, PAD_B,
                            PAD_T)

V2 = bs.OUTPUTS_DIR / "results_v2"
V3 = bs.OUTPUTS_DIR / "results_v3"


def bars_f(labels, vals, colors, unit="", fmt="{:+.2f}") -> str:
    """simple_bars with decimal value labels (Sharpe / t-stat scale)."""
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
            f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" '
            f'height="{max(hh,1):.1f}" rx="3" fill="{colors[i % len(colors)]}">'
            f'<title>{esc(lab)}: {fmt.format(v)} {unit}</title></rect>')
        vy = top - 6 if v >= 0 else top + hh + 14
        out.append(f'<text x="{x+bw/2:.1f}" y="{vy:.1f}" class="vallab" '
                   f'text-anchor="middle">{fmt.format(v)}</text>')
        out.append(f'<text x="{PAD_L+i*slot+slot/2:.1f}" y="{H-10}" '
                   f'class="ticklab" text-anchor="middle">{esc(lab)}</text>')
    return svg_wrap("".join(out))


def read(p: Path) -> pd.DataFrame | None:
    return pd.read_csv(p) if p.exists() else None


def t3t1(res_dir: Path, tag: str) -> pd.Series:
    t = pd.read_csv(res_dir / f"{tag}_tercile_ar0_cc.csv")
    return t[t["bucket"] == "T3-T1"].iloc[0]


def paired_bars(labels, s1, s2, name1, name2, c1, c2, unit="") -> str:
    """Two-series grouped bars with value labels and native tooltips."""
    allv = [v for v in list(s1) + list(s2) if v == v]
    y, lo, hi = _scale(allv, H)
    n = len(labels)
    slot = (W - PAD_L - 16) / n
    bw = min(46.0, (slot - 24) / 2)
    out = [_grid(y, lo, hi, W)]
    y0 = y(0)
    for i, lab in enumerate(labels):
        x0 = PAD_L + i * slot + (slot - 2 * bw - 4) / 2
        for si, (v, name, col) in enumerate(((s1[i], name1, c1),
                                             (s2[i], name2, c2))):
            if v != v:
                continue
            yy = y(v)
            top, hh = (yy, y0 - yy) if v >= 0 else (y0, yy - y0)
            x = x0 + si * (bw + 4)
            out.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                f'height="{max(hh,1):.1f}" rx="3" fill="{col}">'
                f'<title>{esc(lab)} · {esc(name)}: {v:+,.0f} {unit}</title></rect>')
            vy = top - 6 if v >= 0 else top + hh + 14
            out.append(f'<text x="{x+bw/2:.1f}" y="{vy:.1f}" class="vallab" '
                       f'text-anchor="middle">{v:+,.0f}</text>')
        out.append(f'<text x="{PAD_L+i*slot+slot/2:.1f}" y="{H-10}" '
                   f'class="ticklab" text-anchor="middle">{esc(lab)}</text>')
    return svg_wrap("".join(out))


def main() -> None:
    assert bs.V3, "dissection reads v3 results: set EARNINGS_V3=1"

    m2, m3 = t3t1(V2, "dev_full_modern"), t3t1(V3, "dev_full_modern")
    f2, f3 = t3t1(V2, "dev_full"), t3t1(V3, "dev_full")
    strat2, strat3 = read(V2 / "strategy_sim.csv"), read(V3 / "strategy_sim.csv")
    sl = read(V3 / "signal_layer.csv")
    ov = read(V3 / "analyst_overlay_grid.csv")
    ov_sh = read(V3 / "analyst_overlay_shuffle.csv")
    ov_fl = read(V3 / "analyst_overlay_flips.csv")
    ov_cov = read(V3 / "analyst_overlay_covered.csv")
    dirmag = read(V3 / "dirmag_grid.csv")
    dirmag_sh = read(V3 / "dirmag_shuffle.csv")
    edge_auc = read(V3 / "analyst_edge_predict_auc.csv")
    edge_grid = read(V3 / "analyst_edge_abstain_grid.csv")
    edge_perm = read(V3 / "analyst_edge_abstain_perm.csv")
    edge_reads = read(V3 / "analyst_edge_case_reads.csv")
    gate3_m = read(V3 / "validation_shuffle_placebo_modern.csv")
    gate3_a = read(V3 / "validation_shuffle_placebo_all.csv")
    peryear = read(V3 / "dev_full_modern_per_year.csv")
    xvar = read(V3 / "strategy_execution_variants.csv")
    a_co = read(V3 / "analyst_eval_company.csv")

    def strat_sh(s, cost=25):
        r = s[(s["leg"].str.startswith("long")) & (s["cost_bps_rt"] == cost)]
        return float(r["sharpe_calendar"].iloc[0])

    inc2, inc3 = strat_sh(strat2), strat_sh(strat3)

    def ovc(w, legs="combined", sizing="kelly", cost=25):
        r = ov[(ov["w"] == w) & (ov["legs"] == legs) & (ov["sizing"] == sizing)
               & (ov["cost_bps"] == cost)]
        return r.iloc[0] if len(r) else None

    # ---- charts
    chart_v2v3 = paired_bars(
        ["modern T3−T1 spread", "modern FM mean", "full-dev T3−T1 spread",
         "full-dev FM mean"],
        [m2["mean"] * 1e4, m2["fm_mean"] * 1e4, f2["mean"] * 1e4,
         f2["fm_mean"] * 1e4],
        [m3["mean"] * 1e4, m3["fm_mean"] * 1e4, f3["mean"] * 1e4,
         f3["fm_mean"] * 1e4],
        "v2 (wrong days)", "v3 (corrected)", "var(--muted)", "var(--pos)",
        unit="bps")

    sharpe_labels = ["incumbent v2", "incumbent v3", "signal layer",
                     "overlay w=.25", "overlay w=.5", "overlay w=.5 equal"]
    sharpe_vals = [inc2, inc3, float(ovc(0.0)["sharpe_calendar"]),
                   float(ovc(0.25)["sharpe_calendar"]),
                   float(ovc(0.5)["sharpe_calendar"]),
                   float(ovc(0.5, sizing="equal")["sharpe_calendar"])]
    sharpe_cols = ["var(--muted)", "var(--pos)", "var(--pos)",
                   "var(--acc)", "var(--acc)", "var(--acc)"]
    chart_sharpe = bars_f(sharpe_labels, sharpe_vals, sharpe_cols,
                          unit="Sharpe(25bps, calendar)")

    hit_labels = ["all directional\nn=67", "full-strength n=27",
                  "soft n=40", "bullish n=39", "bearish n=28",
                  "matched null"]
    chart_hits = bars_f([h.split("\n")[0] for h in hit_labels],
                        [68.7, 92.6, 52.5, 61.5, 78.6, 54.6],
                        ["var(--pos)"] * 5 + ["var(--muted)"],
                        unit="hit rate", fmt="{:.1f}%")

    fm_diag = read(V3 / "analyst_overlay_grid.csv")  # noqa: F841
    chart_fm = bars_f(
        ["base s_ts", "overlay w=.25", "overlay w=.5"],
        [3.48, 3.91, 4.11],
        ["var(--pos)", "var(--acc)", "var(--acc)"], unit="FM t")

    legend_v = ('<div class="legend"><span><i style="background:var(--muted)">'
                '</i>v2 (certified, wrong days)</span><span><i style='
                '"background:var(--pos)"></i>v3 (timing-corrected)</span></div>')
    legend_s = ('<div class="legend"><span><i style="background:var(--muted)">'
                '</i>v2 reference</span><span><i style="background:var(--pos)">'
                '</i>model only</span><span><i style="background:var(--acc)">'
                '</i>with analyst overlay</span></div>')

    gate3_html = ""
    if gate3_m is not None or gate3_a is not None:
        parts = []
        for era, g in (("modern", gate3_m), ("all eras", gate3_a)):
            if g is not None:
                parts.append(f'<div class="eyebrow">gate 3 · era: {era} · '
                             'persisted this session</div>' + table_html(g))
        gate3_html = '<div class="card">' + "".join(parts) + "</div>"
    else:
        gate3_html = ('<p class="note">Gate-3 permutation run still executing '
                      '— table lands in results_v3/validation_shuffle_placebo_'
                      '*.csv.</p>')

    py_html = ""
    if peryear is not None:
        py_html = ('<div class="card"><div class="eyebrow">modern era · '
                   'T3−T1 bps by year (v3)</div>'
                   + table_html(peryear, bps_cols=("spread",)) + "</div>")

    body = f"""
<style>
:root {{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --pos:#2a78d6; --acc:#eb6834; --neg:#e34948; --good:#006300; --warn:#ec835a;
  --emph:rgba(42,120,214,.07);
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
    --pos:#3987e5; --acc:#d95926; --neg:#e66767; --good:#0ca30c; --warn:#ec835a;
    --emph:rgba(57,135,229,.10);
  }}
}}
:root[data-theme="dark"] {{
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --pos:#3987e5; --acc:#d95926; --neg:#e66767; --good:#0ca30c; --warn:#ec835a;
  --emph:rgba(57,135,229,.10);
}}
:root[data-theme="light"] {{
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --pos:#2a78d6; --acc:#eb6834; --neg:#e34948; --good:#006300; --warn:#ec835a;
  --emph:rgba(42,120,214,.07);
}}
body {{ background:var(--page); color:var(--ink);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; margin:0; }}
main {{ max-width:1060px; margin:0 auto; padding:40px 22px 80px; }}
h1 {{ font-size:27px; line-height:1.2; margin:6px 0 4px; letter-spacing:-.01em; text-wrap:balance; }}
h2 {{ font-size:19px; margin:44px 0 4px; letter-spacing:-.005em; }}
.eyebrow {{ font-size:11px; font-weight:600; letter-spacing:.09em; text-transform:uppercase; color:var(--muted); }}
p.sub {{ color:var(--ink2); max-width:72ch; margin:6px 0 14px; }}
.statusrow {{ display:flex; gap:8px; flex-wrap:wrap; margin:10px 0 26px; }}
.pill {{ font-size:12px; font-weight:600; padding:3px 10px; border-radius:999px;
  border:1px solid var(--ring); color:var(--ink2); background:var(--surface); }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:10px; margin:18px 0 8px; }}
.tile {{ background:var(--surface); border:1px solid var(--ring); border-radius:10px; padding:14px 16px 12px; }}
.tile .v {{ font-size:26px; font-weight:650; letter-spacing:-.01em; }}
.tile .v.pos {{ color:var(--pos); }} .tile .v.acc {{ color:var(--acc); }}
.tile .k {{ font-size:12px; color:var(--muted); margin-top:2px; }}
.card {{ background:var(--surface); border:1px solid var(--ring); border-radius:10px; padding:16px 18px; margin:14px 0; }}
.card.warnbox {{ border-left:4px solid var(--warn); }}
.card.okbox {{ border-left:4px solid var(--good); }}
.legend {{ display:flex; gap:18px; font-size:12.5px; color:var(--ink2); margin:2px 0 8px; flex-wrap:wrap; }}
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
.note {{ font-size:13px; color:var(--ink2); }}
ol li, ul li {{ margin:5px 0; color:var(--ink2); }} ol li b, ul li b {{ color:var(--ink); }}
</style>
<title>Audit v3 — dissection &amp; analyst overlay</title>
<main>
<div class="eyebrow">earnings/ · BMV event study · audit v3 wave</div>
<h1>What the timing fix changed, what the analyst adds, and what survives</h1>
<p class="sub">Dissection of the 2026-07-28 session: reaction-day timing audit and correction,
shared-infra swap (parity-gated), Kelly signal layer, analyst evaluation — plus this session's
run of the pre-committed analyst overlay and a three-way verification of the analyst's 92.6%
full-strength hit rate. All numbers are development-sample; the sacred holdout and the 2026-2T
walk-forward were never re-touched. Generated {date.today().isoformat()}.</p>
<div class="statusrow">
  <span class="pill">dev-only · holdout untouched</span>
  <span class="pill">arbiter: one-shot 2026-3T walk-forward (~Oct 2026)</span>
  <span class="pill">analyst material: DIAGNOSTIC ONLY (pre-committed)</span>
</div>

<div class="tiles">
  <div class="tile"><div class="v pos">{fmt_bps(m3['fm_mean'])} bps</div>
    <div class="k">modern T3−T1 after timing fix (FM t = {m3['fm_t']:.2f}) — was {fmt_bps(m2['fm_mean'])}, t {m2['fm_t']:.2f}</div></div>
  <div class="tile"><div class="v">{inc3:.2f}</div>
    <div class="k">incumbent Sharpe(25bps) after fix — was {inc2:.2f}. Part of the certified edge was wrong-day returns</div></div>
  <div class="tile"><div class="v acc">{float(ovc(0.5)['sharpe_calendar']):.2f}</div>
    <div class="k">analyst overlay w=0.5, combined/kelly/25bps (shuffle p = {float(ov_sh[ov_sh['w']==0.5]['p'].iloc[0]):.3f})</div></div>
  <div class="tile"><div class="v pos">92.6%</div>
    <div class="k">analyst full-strength hit rate — verified 3 independent ways; ~84–89% under conservative cuts</div></div>
</div>

<h2>1 · The verdict box</h2>
<div class="card okbox"><ul>
<li><b>The timing caveat was justified and the correction weakens the study honestly.</b>
38.5% of calendar-covered events were trading the wrong day (19.9% after the staleness gate).
Post-fix agreement with the analyst's Cierre/Apertura calendar: 97.0%.
Modern-era T3−T1 survives ({fmt_bps(m3['fm_mean'])} bps, FM t {m3['fm_t']:.2f}, p {m3['fm_p']:.3f});
the <b>full-dev sample now fails</b> the pre-committed t&gt;2 bar (t {f3['fm_t']:.2f}, p {f3['fm_p']:.3f}).
Per the pre-commit: "if the correction weakens the result, that IS the finding."</li>
<li><b>The signal layer meets its dev bar</b> (0.50 vs 0.40, shuffle p 0.007) — but the bar moved down
with the incumbent it references (see §4 flags).</li>
<li><b>The analyst overlay — run this session per the 2026-07-28 pre-commit — improves the system on
every cell it touches</b>: w=0.5 lifts combined Sharpe(25) 0.50→0.52 (equal-weight 0.35→0.55), the
analyst-component shuffle is p=0.001, and the covered-quarter FM t rises 3.48→4.11 (+0.63, above the
+0.5 re-specification bar). It remains <b>diagnostic-only</b>; adoption runs through re-specification
and the one-shot 2026-3T walk-forward.</li>
</ul></div>

<h2>2 · The timing audit — what was wrong</h2>
<p class="sub">The analyst's Excel calendar (ground truth for 3Q24–2Q26) exposed two mechanisms,
both identified from timing data alone before any return was recomputed: the press-release recovery
regex matched <b>pre-announcement notices</b> ("X will report on…"), pulling WALMEX 5 days early every
quarter and CEMEX 4–7 days; and XBRL filings <b>lag the true release</b> (31.7% of covered events).
The pre-committed rule: outside coverage, press dates survive only when XBRL is stale &gt;60d (the
legitimate Q4 regime); inside coverage, info_dt := min(XBRL filed, calendar-implied 06:00/16:00).
93 press recoveries reverted; 8 residual mismatches deliberately trust an earlier hard XBRL timestamp.</p>
<div class="card"><div class="eyebrow">reaction-day agreement with the calendar</div>
<div class="tblwrap"><table><thead><tr><th>sample</th><th>pre-fix</th><th>post-fix</th></tr></thead>
<tbody><tr><td>all matched calendar rows (n=265)</td><td>61.5%</td><td>97.0%</td></tr>
<tr><td>study-eligible (n≈196)</td><td>80.1%</td><td>96.9%</td></tr></tbody></table></div></div>

<h2>3 · How the numbers moved (v2 → v3)</h2>
<p class="sub">Dev quarters only — holdout and walk-forward reruns are pre-committed OFF, so the
corrected spec has faced no out-of-sample gate yet. The core modern effect survives smaller; the
tradable strategy took the bigger hit because wrong-day returns flattered it.</p>
<div class="card">{legend_v}{chart_v2v3}</div>
<div class="card"><div class="eyebrow">tercile T3−T1 of next-day AR (FM basis)</div>
<div class="tblwrap"><table><thead><tr><th>sample</th><th>spread</th><th>FM bps</th><th>FM t</th><th>FM p</th><th>n</th></tr></thead><tbody>
<tr><td>modern · v2</td><td>{fmt_bps(m2['mean'])}</td><td>{fmt_bps(m2['fm_mean'])}</td><td>{m2['fm_t']:.2f}</td><td>{m2['fm_p']:.4f}</td><td>{int(m2['n'])}</td></tr>
<tr><td>modern · v3</td><td>{fmt_bps(m3['mean'])}</td><td>{fmt_bps(m3['fm_mean'])}</td><td>{m3['fm_t']:.2f}</td><td>{m3['fm_p']:.4f}</td><td>{int(m3['n'])}</td></tr>
<tr><td>full dev · v2</td><td>{fmt_bps(f2['mean'])}</td><td>{fmt_bps(f2['fm_mean'])}</td><td>{f2['fm_t']:.2f}</td><td>{f2['fm_p']:.4f}</td><td>{int(f2['n'])}</td></tr>
<tr><td>full dev · v3</td><td>{fmt_bps(f3['mean'])}</td><td>{fmt_bps(f3['fm_mean'])}</td><td>{f3['fm_t']:.2f}</td><td>{f3['fm_p']:.4f}</td><td>{int(f3['n'])}</td></tr>
</tbody></table></div></div>
{gate3_html}
{py_html}

<h2>4 · Signal layer — honest flags</h2>
<p class="sub">Buy/neutral/short at ±1.0 s_ts, PIT logistic conviction (trailing 8 quarters),
quarter-Kelly (λ=0.25, 10% cap). The pre-registered cell (combined/kelly/25bps) posts Sharpe 0.50
against a bar of 0.40, shuffle p=0.007 — a legitimate dev-bar pass with caveats:</p>
<div class="card warnbox"><ul>
<li><b>The bar moved with the incumbent.</b> The +0.10 bar references the v3 incumbent (0.30), which the
timing fix itself cut from 0.55. Against the v2 bar (0.65) the same candidate would fail.</li>
<li><b>The short leg is 18 trades</b> over ~7 years, hit rate 0.50, not individually significant
(FM p 0.12), Kelly-inert (equal ≡ kelly cell-for-cell), and assumes BMV borrow. The "combined"
headline is essentially the long leg plus noise.</li>
<li><b>long-only/kelly/25 posts 0.54</b> — better than the reported combined cell. Reported, not
switched: the pre-registered cell stays the headline.</li>
<li><b>The effect is T1-avoidance, not T3-selection</b> (T2 ≈ T3 statistically), concentrated in
2025 (+183 bps/yr), and the strongest IC sits in the non-capturable overnight gap
(ar0_gap IC 0.22 vs open→close FM t 0.71).</li>
<li><b>X4 (exit at close t+1) posts Sharpe 0.83</b> vs 0.30 for the pre-committed exit — unexplained,
given PEAD is flat. Treat as an open question, not an edge.</li>
</ul></div>
<div class="card"><div class="eyebrow">signal layer grid (v3)</div>{table_html(sl)}</div>

<h2>5 · The analyst's calls — evaluated, then verified exhaustively</h2>
<p class="sub">138 calls (2Q24–2Q26); 20 in holdout quarters never joined to returns; 73 matched dev
events, 67 directional. Ex-ante recording confirmed by the analyst (2026-07-28); prospective logging
from 2026-3T remains the independent proof.</p>
<div class="card">{chart_hits}</div>
<div class="card okbox">
<div class="eyebrow">in-session verification of the 92.6% (three independent recomputes)</div>
<ul>
<li><b>Replicates exactly</b>: 25/27 from a from-scratch reparse of the raw workbook (hash-pinned)
joined to the v3 dev sample; 1:1 join, zero duplicates, no zero-return ambiguity; exact binomial
p = 6×10⁻⁶.</li>
<li><b>Not a base-rate artifact</b>: mix is 14 Positive / 13 Negative (bearish full-strength 13/13);
a label-and-quarter-matched null expects 54.6% and puts P(≥25) ≈ 1×10⁻⁵.</li>
<li><b>Timing correction contributes ~8pp</b> (v2 wrong-day timing: 84.6%) — but both decisive flips
(WALMEX 2025-2T, CEMEX 2025-3T) came from reverting press false-matches to hard XBRL timestamps,
independently corroborated. No circularity from using the analyst's own calendar.</li>
<li><b>Conservative range ~84–89%</b>: 88.9% on raw (unadjusted) returns; 84.2% (32/38) including
every observable excluded call. All cuts far above any mechanical null.</li>
<li>Soft calls are a coin flip (52.5%) — the analyst's information lives entirely in the 27
full-conviction calls. Only 2 misses: OMA and CHDRAUI 2025-1T, both small negative reactions.</li>
</ul></div>
<div class="card"><div class="eyebrow">per-company (≥3 directional calls; Bonferroni applies)</div>
{table_html(a_co) if a_co is not None else '<p class="note">table missing</p>'}</div>

<h2>6 · The analyst-weighted system — the pre-committed overlay, finally run</h2>
<p class="sub">study.yaml pre-committed (2026-07-28, before any overlay result existed):
score = s_ts + w·direction·strength, w ∈ {{0.25, 0.5}}, 25bps focus, its own shuffle seed.
This session implemented and ran it. Guardrails: w=0 reproduced the certified signal-layer grid
exactly before any overlay number was computed; the join is post-holdout-exclusion; uncovered
events keep the base signal (calls exist only from 2024-3T).</p>
<div class="card">{legend_s}{chart_sharpe}</div>
<div class="card"><div class="eyebrow">focus cell · combined / kelly / 25 bps</div>
{table_html(ov[(ov.legs=='combined')&(ov.sizing=='kelly')&(ov.cost_bps==25)])}
<div class="eyebrow" style="margin-top:12px">analyst-component shuffle (1000 within-quarter permutations
of direction·strength among covered events; base signal + fitted Kelly maps fixed; seed pre-reserved)</div>
{table_html(ov_sh)}</div>
<div class="card"><div class="eyebrow">covered-quarter FM tercile diagnostic (adoption-bar context:
needs incumbent + 0.5)</div>{chart_fm}
<p class="note">Base FM t 3.48 → 3.91 (w=0.25) → 4.11 (w=0.5) on the six covered quarters —
w=0.5 clears the +0.5 re-specification bar with shuffle p=0.001. Equal-weight sizing benefits most
(0.35→0.55 at 25bps) because Kelly's conviction model already does part of what the analyst adds.</p></div>
<div class="card"><div class="eyebrow">what the overlay actually changes (covered events only)</div>
{table_html(ov_fl)}
<p class="note">Few classification flips ({int(ov_fl[ov_fl.w==0.25]['n'][ov_fl[ov_fl.w==0.25].base_action!=ov_fl[ov_fl.w==0.25].action].sum())} at w=0.25,
{int(ov_fl[ov_fl.w==0.5]['n'][ov_fl[ov_fl.w==0.5].base_action!=ov_fl[ov_fl.w==0.5].action].sum())} at w=0.5) —
most of the lift comes from re-ranking scores inside the buy leg and from the handful of
analyst-triggered entries/exits. On the identical covered event set:
{table_html(ov_cov)}
Soft-calls-only sensitivity (post-hoc, labeled): overlay collapses to the base 0.50 — the entire
contribution is the full-strength calls, consistent with §5.</p></div>

<h2>6b · Direction × magnitude (post-hoc battery, run 2026-07-29)</h2>
<p class="sub">Analyst supplies direction, system supplies size. POST-HOC (motivated after the
analyst eval was seen; logged in §8). Full-strength sleeve: <b>+302 bps/trade net of 25bps
(t=4.24, 27 trades, hit 0.85, Sharpe_cal 1.11)</b>. The permutation nulls decompose it:
direction-permutation p=0.001, size-permutation p=1.0 — <b>the direction leg is everything;
sizing by |s_ts| or sigma_pre only de-levers</b> (quarter-Kelly saturates the 10% cap at these hit
rates, collapsing to equal). The winning spec is pre-registered in study.yaml `analyst_direction`
(prospective hash-committed logging required); arbiter one-shot 2026-3T.</p>
{('<div class="card"><div class="eyebrow">grid (dirmag_grid.csv)</div>' + table_html(dirmag)
  + '<div class="eyebrow" style="margin-top:12px">permutation nulls</div>'
  + table_html(dirmag_sh) + '</div>') if dirmag is not None else ''}

<h2>6c · Analyst edge decomposition (post-hoc, run 2026-07-29)</h2>
<p class="sub">Why is the analyst so much better? Spec frozen in study.yaml `analyst_edge` before
computing. Three answers: (1) their <b>conviction choice is invisible to our features</b>
(full-strength-ness OOF AUC 0.26, perm p 0.997 — decisively outside the data); direction is
borderline (AUC 0.647, under the frozen 0.65 bar). (2) A real precision ladder exists for the
model — |s_ts|≥2 + margin-SUE agreement + low vol reaches <b>79.3%</b> (max-stat adjusted p=0.003)
— but plateaus far below 92.6%. (3) Outcome-blinded case reads show the missing information is
IN the report text: FX-flattered revenue, one-offs distorting the SUE, adjusted-vs-reported
margins, pre-announced effects the market already expected. Full detail: outputs/ANALYST_EDGE.md;
prospective reason-code protocol: outputs/protocolo_llamadas_3T26.md + data/analyst/log_3T26.csv.</p>
{('<div class="card"><div class="eyebrow">predict-the-analyst (frozen interpretation rule)</div>'
  + table_html(edge_auc) + '</div>') if edge_auc is not None else ''}
{('<div class="card"><div class="eyebrow">learn-to-abstain grid (PIT features, tradable universe)'
  '</div>' + table_html(edge_grid)
  + ('<div class="eyebrow" style="margin-top:12px">max-statistic control</div>'
     + table_html(edge_perm) if edge_perm is not None else '')
  + '</div>') if edge_grid is not None else ''}
{('<div class="card"><div class="eyebrow">outcome-blinded case reads (outcome-selected events — '
  'caveat)</div>' + table_html(edge_reads) + '</div>') if edge_reads is not None else ''}

<h2>7 · How to strengthen the system with the analyst's view</h2>
<div class="card"><ol>
<li><b>The analyst is an independent information source, not a duplicate.</b> Directional agreement
with the model's terciles is 50.7% (≈ orthogonal), yet Frisch–Waugh incremental slope is
+3.5% per unit (t 6.67, 6 clusters). The overlay converts that into measured performance.</li>
<li><b>Candidate mechanisms, in adoption-bar order</b>:
(a) <b>overlay weight w=0.5</b> — now measured: +0.63 FM t, shuffle p 0.001, Sharpe +0.02 (kelly) /
+0.20 (equal);
(b) <b>analyst-as-short-veto / short-enabler</b> — bearish calls are the analyst's strongest suit
(78.6% all, 13/13 full-strength) exactly where the model is weakest (18-trade short leg);
(c) <b>analyst conviction as a feature in the PIT logistic</b> (needs quarters of coverage).</li>
<li><b>The binding constraints are coverage and provenance, not signal quality.</b> 138 calls cover
73 dev events; only ~6 quarters. Fixes: log every covered name every quarter, and from 2026-3T
<b>pre-register the calls</b> — timestamped, hash-committed before each release date — so the next
walk-forward doubles as the provenance proof.</li>
<li><b>Adoption path (pre-committed)</b>: re-specify the exact overlay in study.yaml before 2026-3T,
then the one-shot walk-forward arbitrates. Nothing from this session enters the traded spec.</li>
</ol></div>

<h2>8 · Open-issues ledger</h2>
<div class="card warnbox"><ul>
<li><b>Survivorship (v2 audit issue I) still open</b> — long-side levels likely overstated.</li>
<li><b>The entire v3 wave is uncommitted in git</b> — pre-commitment timestamps are not independently
verifiable until committed. Recommended: commit immediately as remediation.</li>
<li><b>Post-hoc analyses this session</b> (logged as researcher degrees of freedom): soft-calls-only
sensitivity; covered-subsample comparison; the 92.6% verification's alternative cuts; the
analyst-report cuts (sector, per-company, liquidity terciles, model conviction bands, and the two
ensemble rules in results_v3/simple_*.csv); the direction×magnitude sizing battery (§6b,
results_v3/dirmag_*.csv), whose winner is pre-registered post-hoc in study.yaml
`analyst_direction` for the 2026-3T arbiter. None enter any traded spec.</li>
<li><b>Full-dev core result now fails its pre-committed criterion</b> (FM t 1.82 &lt; 2) — the study's
claim is modern-era; say so wherever the result is quoted.</li>
<li><b>X4 exit anomaly and 2025 concentration</b> remain uninvestigated.</li>
<li>Execution-variant reference: {('Sharpe X4 = ' + str(xvar[xvar.variant.str.contains('X4', na=False)]['sharpe_calendar'].iloc[0])) if xvar is not None and len(xvar[xvar.variant.str.contains('X4', na=False)]) else 'strategy_execution_variants.csv'} vs incumbent {inc3:.2f}.</li>
</ul></div>

<p class="note">Sources: outputs/TIMING_AUDIT.md · outputs/parity/PARITY.md · outputs/SIGNAL_LAYER.md ·
outputs/ANALYST_EVAL.md · outputs/OVERLAY.md · results_v3/*.csv · in-session verification
(three independent agents, 2026-07-28). Spec: configs/study.yaml (audit_v3, analyst, signal_layer).</p>
</main>
"""
    out = bs.OUTPUTS_DIR / "dissection_report.html"
    out.write_text(body)
    print(f"wrote {out} ({len(body)//1024} KB)")


if __name__ == "__main__":
    main()
