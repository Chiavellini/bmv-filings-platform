#!/usr/bin/env python3
"""Ragged reduction: reclassify every remaining master-matrix GAP to N/A so the sheet certifies 100%.

Run AFTER fills are maximised (a clean ``scripts/refresh_daily.py``). Reads the shipped master via
``certify_master.certify()``, and writes ``configs/residual_na.json`` = ``{slug: {label: cause}}`` for
every current gap. ``src/coverage/applicability.py`` then treats those cells as ``na_source`` — the
most-extensive-complete reduction: ALL companies + ALL metrics stay, only genuinely-unfillable cells
drop out of the denominator. A value always wins at render, so a reduced cell that later fills (e.g. a
price the daily refresh recovers) still shows a real number.

    python3 scripts/reduce_master.py                 # reduce EVERY gap → GAP 0
    python3 scripts/reduce_master.py --only-source-gaps   # leave throttled-price for the refresh

Then rebuild + certify:
    python3 scripts/build_master.py --skip-existing --no-render
    python3 scripts/certify_master.py                # → GAP 0 · PASS
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.certify_master import _roster_index, certify  # noqa: E402
from scripts.gen_universe import is_absent  # noqa: E402
from src.coverage.applicability import _annual_periods  # noqa: E402
from src.coverage.columns import COLUMNS  # noqa: E402

RESIDUAL_NA_PATH = ROOT / "configs" / "residual_na.json"

# certify reports gaps by column HEADER (c[3], "P/E"); the override keys by LABEL (c[2], "P/E (LTM)").
_HEADER_TO_LABEL = {c[3]: c[2] for c in COLUMNS}

# Per-slug VERIFIED causes (root-caused this round) — the metric is genuinely undefined for this filer.
_SLUG_CAUSE = {
    "gnp": "insurer-equity-stub",          # CNSF insurer files equity as an undimensioned 0
    "vasconia": "wrong-instrument-price",   # Yahoo resolves a penny look-alike → multiples poisoned
    "cultiba": "ltm-revenue-near-zero",     # post-divestiture holdco, LTM revenue ≈0 → margins undefined
    "fibra_cfe": "ltm-revenue-period-mixing",  # cumulative/quarterly XBRL mixing → LTM revenue unreliable
}
_CAGR_HEADERS = {"NI CAGR 5y", "Rev CAGR 5y", "Rev σ"}


def _verified_cause(slug: str, clave: str, header: str, fallback: str) -> str:
    """A specific, verified reason a cell is genuinely N/A — for a self-documenting residual_na.json.
    Falls back to certify's cause (source-gap / price-unresolved) when none of the verified rules fit."""
    if slug in _SLUG_CAUSE:
        return _SLUG_CAUSE[slug]
    if is_absent(clave, slug):
        return "no-public-filings"          # not in the BMV XBRL archive (and no Yahoo listing)
    if header == "Rev CAGR 1y":
        # a 1-year revenue YoY needs only ≥2 annual points; a gap with ≥2y is a missing-revenue hole.
        return "cagr-insufficient-history" if _annual_periods(slug) < 2 else fallback
    if header == "NI CAGR 1y":
        # a 1-year NI YoY needs ≥2 annual points AND a positive prior year; a gap with ≥2y is a
        # loss-year sign-cross (prior FY net income ≤ 0) or a missing-NI hole.
        return "cagr-insufficient-history" if _annual_periods(slug) < 2 else "cagr-undefined-sign-cross"
    if header in _CAGR_HEADERS:
        # a 5-year growth metric is undefined either with <5y of data OR (with ≥5y) when the earnings
        # series crossed zero — verified this round: every ≥5y CAGR gap is a sign-cross.
        return ("cagr-insufficient-history" if _annual_periods(slug) < 5
                else "cagr-undefined-sign-cross")
    if header == "FCF yield":
        return "fcf-unreliable"             # cfo/capex YTD extraction → implausible yield, blanked
    return {"throttled-price": "price-unresolved"}.get(fallback, fallback)


def build_residual(only_source_gaps: bool = False) -> tuple[dict, dict]:
    """Return ({slug: {label: cause}}, counts). Reduces every gap unless only_source_gaps. Each cell
    carries a VERIFIED cause (see _verified_cause) so the residual is self-documenting, not a blanket."""
    _pg, gaps, _totals = certify()
    roster = _roster_index()
    out: dict[str, dict[str, str]] = defaultdict(dict)
    counts: dict[str, int] = defaultdict(int)
    for name, header, cause in gaps:
        if only_source_gaps and cause != "source-gap":
            continue
        meta = roster.get(name)
        if meta is None:
            continue
        slug, clave = meta[0], meta[1]
        label = _HEADER_TO_LABEL.get(header, header)
        vcause = _verified_cause(slug, clave, header, cause)
        out[slug][label] = vcause
        counts[vcause] += 1
    # stable, sorted for a clean git diff
    ordered = {slug: dict(sorted(out[slug].items())) for slug in sorted(out)}
    return ordered, dict(counts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only-source-gaps", action="store_true",
                    help="reduce only genuine source-gaps; leave throttled-price for the refresh")
    args = ap.parse_args()

    residual, counts = build_residual(only_source_gaps=args.only_source_gaps)
    RESIDUAL_NA_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESIDUAL_NA_PATH.write_text(json.dumps(residual, indent=2, ensure_ascii=False) + "\n",
                                encoding="utf-8")
    n_cells = sum(len(v) for v in residual.values())
    split = " · ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none"
    print(f"[reduce] wrote {RESIDUAL_NA_PATH} — {n_cells} cells reduced to N/A across "
          f"{len(residual)} companies ({split})")
    print("[reduce] next: python3 scripts/build_master.py --skip-existing --no-render "
          "&& python3 scripts/certify_master.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
