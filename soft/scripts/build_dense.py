#!/usr/bin/env python3
"""Emit the DENSE coverage matrix — a genuinely 100% block where EVERY cell is a real computed value.

The main matrix reaches "100%" only by excluding honest N/A cells from the denominator (its true
fill is ~76% of all cells). This builds the opposite: reduce BOTH axes — drop the sparse metrics and
the companies that lack them — so the remaining rectangle has ZERO N/A and zero blanks. Every number
shown is real.

Default block (the max-real-data point on the exhaustive optimal frontier): **13 metrics** (drops
CET1 banks-only, FCF yield, and NI CAGR 5y which is undefined through a sign-crossing earnings series)
× the companies that carry a real value for all 13.

    python3 scripts/build_dense.py                 # default 13-metric block
    python3 scripts/build_dense.py --metrics "P/E,P/BV,ROE,Div yield,Rev CAGR 5y,Rev σ"

Reads the shipped core master (``outputs/_master/soft_coverage_master.csv``) as the value source — a
real value is a numeric cell; "N/A"/blank is not real — so no rebuild is needed. Writes
``outputs/_master/soft_coverage_dense.{csv,html}`` and HARD-ASSERTS the block is 100% real.
"""
from __future__ import annotations

import argparse
import csv
import html as _html
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_master import (  # noqa: E402
    MASTER_HTML_STYLE, _fmt_val, _heat_css, _pretty_sector)
from src.coverage.columns import COLUMNS  # noqa: E402

MASTER_CSV = ROOT / "outputs" / "_master" / "soft_coverage_master.csv"

# The max-real-data block (13 metrics), in COLUMNS band order. Drops CET1 / FCF yield / NI CAGR 5y.
DENSE_METRICS = ["P/E", "EV/EBITDA", "P/BV", "Div yield", "ROE", "ROIC", "EBITDA mgn", "Net mgn",
                 "Rev CAGR 5y", "EBITDA YoY", "Rev σ", "NetDebt/EBITDA", "Current"]

# header -> (band, unit, good_direction) from the single source of truth.
_META = {c[3]: (c[0], c[4], c[5]) for c in COLUMNS}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build(metrics: list[str]) -> tuple[list[dict], list[str]]:
    """Return (kept_rows, metrics) — companies whose EVERY selected metric is a real number."""
    rows = list(csv.reader(MASTER_CSV.open(encoding="utf-8")))
    hdr = rows[0]
    idx = {h: i for i, h in enumerate(hdr)}
    for m in metrics:
        if m not in idx:
            raise SystemExit(f"metric {m!r} not in master columns {hdr[4:]}")
    kept = []
    for r in rows[1:]:
        if len(r) < len(hdr):
            continue
        vals = {m: _num(r[idx[m]]) for m in metrics}
        if any(v is None for v in vals.values()):
            continue                                    # a hole → drop this company
        kept.append({"name": r[0], "sector": r[1], "template": r[2], "clave": r[3], "vals": vals})
    return kept, metrics


def _write_csv(path: Path, rows: list[dict], metrics: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Company", "Sector", "Template", "Ticker"] + metrics)
        for r in rows:
            w.writerow([r["name"], r["sector"], r["template"], r["clave"]]
                       + [r["vals"][m] for m in metrics])


def _write_html(path: Path, rows: list[dict], metrics: list[str]) -> None:
    n_comp, n_met = len(rows), len(metrics)
    cells_total = n_comp * n_met
    # per-column heat stats (P/E scaled on positive values only, like the master)
    colstats = {}
    for m in metrics:
        vs = [r["vals"][m] for r in rows]
        if m == "P/E":
            vs = [v for v in vs if 0 < v <= 100] or vs
        colstats[m] = (min(vs), statistics.median(vs), max(vs)) if vs else (0, 0, 0)

    # band header spans + sub headers
    band_groups: list[tuple[str, int]] = []
    for m in metrics:
        band = _META[m][0]
        if band_groups and band_groups[-1][0] == band:
            band_groups[-1] = (band, band_groups[-1][1] + 1)
        else:
            band_groups.append((band, 1))
    head_band = ['<th class="rail band" colspan="2">Company</th>']
    for band, span in band_groups:
        head_band.append(f'<th class="band" colspan="{span}">{_html.escape(band)}</th>')
    head_sub = ['<th class="rail sub">Name</th>', '<th class="rail sub">Sector</th>']
    for m in metrics:
        head_sub.append(f'<th class="sub">{_html.escape(m)}</th>')

    by_sector: dict[str, list[dict]] = {}
    seen: list[str] = []
    for r in rows:
        by_sector.setdefault(r["sector"], []).append(r)
        if r["sector"] not in seen:
            seen.append(r["sector"])
    total_cols = 2 + n_met
    body = []
    for sector in seen:
        members = sorted(by_sector[sector], key=lambda x: x["name"].lower())
        body.append(f'<tr class="secrow"><td class="sec" colspan="{total_cols}">'
                    f'{_html.escape(_pretty_sector(sector))}<span class="seccount">{len(members)}</span></td></tr>')
        for r in members:
            cells = [f'<th class="rail coname">{_html.escape(r["name"])}</th>'
                     f'<td class="rail sect">{_html.escape(_pretty_sector(sector))}</td>']
            for m in metrics:
                v = r["vals"][m]
                _band, unit, good = _META[m]
                lo, med, hi = colstats[m]
                if m == "P/E" and v <= 0:               # loss-maker: real but not "best" (neutral)
                    cells.append(f'<td class="v" style="background:rgb(238,240,243);" '
                                 f'title="negative P/E — loss-maker">{_fmt_val(v, unit)}</td>')
                else:
                    bg = _heat_css(v, lo, med, hi, good)
                    cells.append(f'<td class="v" style="background:{bg};">{_fmt_val(v, unit)}</td>')
            body.append(f'<tr class="co">{"".join(cells)}</tr>')

    footer = (
        f"<b>Dense block — a genuine 100%: every one of the {cells_total:,} cells is a real computed "
        f"value (0 N/A, 0 blank).</b> Reduced from the full universe on BOTH axes to reach true density: "
        f"kept the {n_met} metrics that {n_comp} companies all share, dropping the sparse columns "
        f"<b>CET1</b> (banks-only), <b>FCF yield</b>, and <b>NI CAGR 5y</b> (undefined when earnings "
        f"cross zero), plus every company missing any kept metric. Heatmap is a per-column red→green "
        f"scale (green = better; low P/E good, high ROE good; a loss-maker's negative P/E renders "
        f"neutral). Source: <code>outputs/_master/soft_coverage_master.csv</code> (subset). "
        f"<b>{n_comp} companies × {n_met} metrics = {cells_total:,} real values.</b>")

    doc = (
        '<title>Soft Coverage — Dense 100% Matrix</title>\n'
        f'<style>{MASTER_HTML_STYLE}</style>\n'
        '<div class="wrap">\n'
        '  <div class="legend"><span class="grad"><span>Worse</span><span class="bar"></span>'
        '<span>Better</span><span style="color:var(--ink)">— per-column vs the kept universe</span>'
        '</span><span class="key">Every cell is a real value — no N/A, no blanks.</span></div>\n'
        '  <div class="tablecard"><div class="scroll"><table>\n'
        f'    <thead>\n      <tr>{"".join(head_band)}</tr>\n      <tr>{"".join(head_sub)}</tr>\n    </thead>\n'
        f'    <tbody>\n      {"".join(body)}\n    </tbody>\n'
        '    </table></div></div>\n'
        f'  <footer>{footer}</footer>\n'
        '</div>\n'
    )
    path.write_text(doc, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--metrics", default=None,
                    help="comma-separated metric headers to keep (default: the 13-metric max-data block)")
    args = ap.parse_args()
    metrics = [m.strip() for m in args.metrics.split(",")] if args.metrics else list(DENSE_METRICS)

    rows, metrics = build(metrics)
    out_dir = ROOT / "outputs" / "_master"
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "soft_coverage_dense.csv"
    html_path = out_dir / "soft_coverage_dense.html"
    _write_csv(csv_path, rows, metrics)
    _write_html(html_path, rows, metrics)

    # HARD assertion: the block must be 100% real.
    total = len(rows) * len(metrics)
    real = sum(1 for r in rows for m in metrics if isinstance(r["vals"][m], (int, float)))
    assert real == total, f"dense block not 100% real: {real}/{total}"
    print(f"[dense] {len(rows)} companies × {len(metrics)} metrics = {total} cells · {real}/{total} "
          f"REAL (100.0%) · 0 N/A · 0 blank")
    print(f"[dense] kept metrics: {', '.join(metrics)}")
    print(f"[dense] wrote {csv_path}")
    print(f"[dense] wrote {html_path}")


if __name__ == "__main__":
    main()
