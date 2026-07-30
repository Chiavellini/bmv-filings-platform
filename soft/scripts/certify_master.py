#!/usr/bin/env python3
"""Certify the master matrix: every applicable-free cell is filled (GAP == 0).

Read-only, offline. Reads the *shipped* ``outputs/_master/soft_coverage_master.csv`` (so it tests
the real artifact, not a fresh re-build), joins it with the roster + the applicability map, and:

  * builds a per-(sector, column) fill table over applicable-free cells,
  * lists every GAP — an ``applicable`` cell that is blank — with a probable cause,
  * exits non-zero if any GAP remains, and writes ``outputs/_reconcile/master_certification.md``.

GAP causes:
  * ``throttled-price`` — a price-derived column on a company whose price fetch failed (no
    price-derived cell filled anywhere in its row). RECOVERABLE: re-run the clean refresh.
        python3 scripts/refresh_daily.py                 # polite full re-price pass
        python3 scripts/certify_master.py                # re-check
        python3 scripts/refresh_daily.py --only slugA,slugB   # targeted retry for stragglers
  * ``source-gap`` — anything else: the free source genuinely does not yield this cell for this
    company. Fix the extractor, OR mark the (company, column) na_source in
    ``src/coverage/applicability.py`` (or drop it from a block). Then re-certify. Loop until GAP == 0.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.gen_universe import UNIVERSE, slugify  # noqa: E402
from src.coverage.applicability import row_applicability  # noqa: E402
from src.coverage.columns import COLUMNS, PRICE_DERIVED_LABELS  # noqa: E402

MASTER_CSV = ROOT / "outputs" / "_master" / "soft_coverage_master.csv"
REPORT = ROOT / "outputs" / "_reconcile" / "master_certification.md"

# header (c[3]) -> (colkey, label). The CSV lays metric columns out in COLUMNS order by header.
_HEADER_TO_COL = {c[3]: ((c[1], c[2]), c[2]) for c in COLUMNS}
_PRICE_HEADERS = {c[3] for c in COLUMNS if c[2] in PRICE_DERIVED_LABELS}


def _roster_index():
    """{name: (slug, clave, template, sector)} — raw sector (banks/reit/...) for applicability."""
    idx = {}
    for sector, (template, members) in UNIVERSE.items():
        for clave, name in members:
            idx[name] = (slugify(name), clave, template, sector)
    return idx


def certify(master_csv: Path = MASTER_CSV):
    """Return (per_group, gaps, totals). per_group[(sector,header)] = [filled, applicable];
    gaps = [(name, header, cause)];
    totals = (n_filled, n_applicable, n_gap, n_na_template, n_na_source, n_total_cells).

    ``n_na_template`` / ``n_na_source`` are the two N/A flavours applicability.py distinguishes —
    reported separately so "% of all cells" can't be inflated by conflating a convention-grounded
    structural exclusion (na_template — a FIBRA has no meaningful P/E) with a fill-availability gap
    (na_source — no free source yet, incl. reduce_master.py's residual reclassification). Only
    na_source cells are ever candidates for a future fill; na_template cells never will be.
    """
    rows = list(csv.reader(master_csv.open(encoding="utf-8")))
    header = rows[0]
    metric_headers = header[4:]
    roster = _roster_index()

    per_group: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    gaps: list[tuple[str, str, str]] = []
    n_filled = n_applicable = n_na_template = n_na_source = 0

    for r in rows[1:]:
        if len(r) < 4:
            continue
        name = r[0]
        meta = roster.get(name)
        if meta is None:
            continue
        slug, clave, template, sector = meta
        applic = row_applicability(template, sector, clave, slug)
        cells = dict(zip(metric_headers, r[4:]))
        # whole-row price outage? (no price-derived cell filled → price fetch failed for this name)
        row_has_price = any(cells.get(h, "") not in ("", "N/A") for h in _PRICE_HEADERS)

        for h in metric_headers:
            colkey, label = _HEADER_TO_COL[h]
            status = applic.get(colkey)
            val = cells.get(h, "")
            has_val = val not in ("", "N/A")
            # Mirror build_master.cell_state precedence EXACTLY so this certification agrees with the
            # build-time coverage_counts (they previously disagreed by 27 cells). na_template ALWAYS
            # excludes (structurally meaningless even when a value computed — e.g. a FIBRA's P/E).
            # Otherwise a present value ALWAYS counts as filled: residual_na's "value wins" rule means
            # a real number must never be hidden (this is where the 27 undercounted cells lived —
            # na_source cells that nonetheless carry a value, e.g. history-labels whose live
            # has_5y_history check dips below 5 while a prior-build value sits on the sheet). Only a
            # BLANK na_source cell is excluded as no-free-source.
            if status == "na_template":
                n_na_template += 1
                continue                       # structurally N/A — never counted, never a candidate
            if not has_val and status == "na_source":
                n_na_source += 1
                continue                       # blank AND no free source (yet) — never counted
            per_group[(sector, h)][1] += 1
            n_applicable += 1
            if has_val:
                per_group[(sector, h)][0] += 1
                n_filled += 1
            else:
                cause = ("throttled-price"
                         if (h in _PRICE_HEADERS and not row_has_price) else "source-gap")
                gaps.append((name, h, cause))

    n_total_cells = n_applicable + n_na_template + n_na_source
    return per_group, gaps, (n_filled, n_applicable, len(gaps), n_na_template, n_na_source, n_total_cells)


def _write_report(per_group, gaps, totals):
    n_filled, n_applicable, _, n_na_template, n_na_source, n_total_cells = totals
    n_gap = len(gaps)
    cov = (100.0 * n_filled / n_applicable) if n_applicable else 0.0
    cov_all = (100.0 * n_filled / n_total_cells) if n_total_cells else 0.0
    thr = sum(1 for _, _, c in gaps if c == "throttled-price")
    src = n_gap - thr

    lines = ["# Master matrix — free-source certification", ""]
    lines.append(f"**{n_filled:,} / {n_applicable:,} applicable-free cells filled "
                 f"({cov:.1f}% of applicable)** · GAP **{n_gap}** "
                 f"(throttled-price {thr}, source-gap {src})")
    lines.append(f"**{n_filled:,} / {n_total_cells:,} of ALL cells filled ({cov_all:.1f}%)** — "
                 f"na_template {n_na_template:,} (convention-grounded, never a candidate to fill) · "
                 f"na_source {n_na_source:,} (no free source yet, incl. residual reduction below).")
    lines.append("")
    lines.append("Coverage counts only cells that are applicable *and* have a free auto-refreshable "
                 "source; structural / no-source N/A is excluded. GAP == 0 means the sheet is a "
                 "flawless free-and-auto 100% fill. The two denominators measure different things: "
                 "'% of applicable' answers 'is everything we claim to source actually filled', "
                 "'% of all cells' answers 'how much of the physical grid carries a real number' — "
                 "growing na_template (e.g. hiding a FIBRA's meaningless P/E) correctly LOWERS the "
                 "second without touching the first, and should never be read as the sheet getting "
                 "worse.")
    lines.append("")
    lines.append(f"na_template is grounded in accounting/regulatory convention, with a written "
                 "rationale per entry — see `configs/metric_applicability.yaml`. It is NOT derived "
                 "from observed fill, and it never overlaps with the residual reduction below (that "
                 "reclassifies na_source, not na_template).")
    lines.append("")
    from src.coverage.applicability import residual_na_summary
    n_reduced, red_causes = residual_na_summary()
    if n_reduced:
        split = ", ".join(f"{v} {k}" for k, v in sorted(red_causes.items()))
        lines.append(f"**Ragged reduction active:** {n_reduced} of the {n_na_source:,} na_source "
                     f"cells were reduced from an unfillable GAP (no guaranteed free source: {split}) "
                     "— full list in `configs/residual_na.json`. All companies and all metrics are "
                     "retained; only unfillable cells drop out.")
        lines.append("")
    if gaps:
        lines.append("## Gaps")
        lines.append("")
        lines.append("| Company | Column | Cause |")
        lines.append("|---|---|---|")
        for name, h, cause in sorted(gaps, key=lambda g: (g[2], g[0], g[1])):
            lines.append(f"| {name} | {h} | {cause} |")
        lines.append("")
    # per-(sector,column) fill table, only rows with any applicable cells
    lines.append("## Fill by sector × column (applicable-free)")
    lines.append("")
    sectors = sorted({s for (s, _h) in per_group})
    cols = [c[3] for c in COLUMNS]
    lines.append("| Sector | " + " | ".join(cols) + " |")
    lines.append("|" + "---|" * (len(cols) + 1))
    for sec in sectors:
        cells = []
        for h in cols:
            f, a = per_group.get((sec, h), [0, 0])
            cells.append(f"{f}/{a}" if a else "·")
        lines.append(f"| {sec} | " + " | ".join(cells) + " |")
    lines.append("")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", default=str(MASTER_CSV), help="master CSV to certify")
    ap.add_argument("--quiet", action="store_true", help="only print the headline + exit code")
    args = ap.parse_args()

    per_group, gaps, totals = certify(Path(args.csv))
    _write_report(per_group, gaps, totals)
    n_filled, n_applicable, _, n_na_template, n_na_source, n_total_cells = totals
    n_gap = len(gaps)
    cov = (100.0 * n_filled / n_applicable) if n_applicable else 0.0
    cov_all = (100.0 * n_filled / n_total_cells) if n_total_cells else 0.0
    thr = sum(1 for _, _, c in gaps if c == "throttled-price")
    src = n_gap - thr

    print(f"[certify] {n_filled}/{n_applicable} applicable-free filled ({cov:.1f}%) · "
          f"GAP {n_gap} (throttled-price {thr}, source-gap {src})")
    print(f"[certify] {n_filled}/{n_total_cells} of ALL cells filled ({cov_all:.1f}%) · "
          f"na_template {n_na_template} · na_source {n_na_source}")
    print(f"[certify] report → {REPORT}")
    if not args.quiet and gaps:
        for name, h, cause in sorted(gaps, key=lambda g: (g[2], g[0], g[1]))[:40]:
            print(f"    GAP  {cause:16s} {name:24s} {h}")
        if n_gap > 40:
            print(f"    … and {n_gap - 40} more (see report)")
    if n_gap == 0:
        print("[certify] PASS — flawless free-and-auto 100% fill")
        return 0
    print("[certify] FAIL — gaps remain (see causes above)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
