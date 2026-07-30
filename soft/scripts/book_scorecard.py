#!/usr/bin/env python3
"""Whole-book trust scorecard — one row per company answering "can I trust this row of the book?".

Joins three signals that already exist, into a single worst-first view:
  * completeness status — PASS / INCOMPLETE / BROKEN, from ``<slug>_status.json`` (emitted by
    build_coverage via ``validate_model``). Missing → the company never built cleanly.
  * actionable findings — the de-noised reconciler output (``Kind == real`` in the latest
    ``outputs/_reconcile/reconciliation_<date>.csv``). Noise/expected rows are ignored.
  * coverage — populated vs blank metric cells in the per-company coverage CSV.

Each company gets a TRUST grade:
  * GREEN  — built PASS, zero actionable findings.
  * AMBER  — INCOMPLETE (external data pending) or only non-scale actionable findings.
  * RED    — BROKEN (engine gap / sentinel) or any scale/shares/currency actionable finding, or the
             company never emitted a status.

Reads only already-emitted artifacts (no rebuild, no network). Run ``reconcile_master.py`` first so
the findings are fresh.

    python3 scripts/book_scorecard.py
    python3 scripts/book_scorecard.py --only walmex,cemex
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_master import _CORE_EXCLUDE, roster  # noqa: E402

# Root-cause tags that mean the number is mis-scaled/wrong (not merely incomplete) → RED.
_SCALE_ROOTS = {"net_income scale", "shares/revenue scale", "shares/price", "golden range"}


def _incomplete_names() -> set[str]:
    """Company names that have an actual GAP in the SHIPPED core matrix (a cell that is applicable-free
    yet blank). Uses the same N/A-aware certifier as the matrix, so an honest verified-N/A cell does NOT
    count as 'missing' — unlike the per-company status.json's stricter _REQUIRED gate, which is unaware of
    the residual_na machinery and stales a fully-N/A-resolved company to INCOMPLETE."""
    try:
        from scripts.certify_master import certify
        _pg, gaps, _t = certify()
        return {g[0] for g in gaps}
    except Exception:
        return set()


def _load_waivers() -> dict:
    p = ROOT / "eval" / "reconcile_waivers.yaml"
    if not p.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _material_sentinels(sentinels: list[str], waived_metrics: tuple = ()) -> list[str]:
    """Keep only sentinels that flag a wrong value the MASTER BOOK actually shows. Dropped classes:
      * non-positive multiples — ``build_master._admit_cell`` blanks them, so the book shows a blank.
      * a sentinel whose metric carries an evidence-backed WAIVER in ``eval/reconcile_waivers.yaml``
        (e.g. GBM's real broker P/E shown per policy, Cultiba's ≈0-revenue P/S whose multiples are
        already N/A) — human-reviewed as not-an-engine-defect, so it must not RED the row.
    What stays material: uncorroborated 'implausibly cheap', 'impossible ROE/ROIC denominator',
    'implausible for a bank' WITHOUT a waiver, and placeholder-pack flags."""
    out = []
    for s in (sentinels or []):
        if "non-positive multiple" in s.lower():
            continue
        if any(m and m in s for m in waived_metrics):   # a waived metric appears in this sentinel
            continue
        out.append(s)
    return out


def _latest_reconcile_csv() -> Path | None:
    d = ROOT / "outputs" / "_reconcile"
    files = sorted(d.glob("reconciliation_*.csv")) if d.exists() else []
    return files[-1] if files else None


def _load_real_findings(only: set[str] | None) -> dict[str, list[dict]]:
    """company name -> list of its Kind==real finding rows (from the latest reconcile CSV)."""
    path = _latest_reconcile_csv()
    out: dict[str, list[dict]] = {}
    if not path:
        return out
    for row in csv.DictReader(path.open(encoding="utf-8")):
        # tolerate the pre-Kind schema (all rows treated as real) for older reports
        if row.get("Kind", "real") != "real":
            continue
        out.setdefault(row["Company"], []).append(row)
    return out


def _load_status(name: str, slug: str) -> dict | None:
    p = ROOT / "outputs" / name / "validation" / f"{slug}_status.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _coverage_counts(name: str, slug: str) -> tuple[int, int]:
    """(#populated, #total) headline metric cells in the per-company coverage CSV."""
    p = ROOT / "outputs" / name / "csv" / f"{slug}_coverage.csv"
    if not p.exists():
        return (0, 0)
    pop = tot = 0
    for row in csv.reader(p.open(encoding="utf-8")):
        if len(row) < 3 or row[0] == "block":
            continue
        tot += 1
        if (row[2] or "").strip():
            pop += 1
    return (pop, tot)


def _grade(status: dict | None, real: list[dict], core_complete: bool,
           waived_metrics: tuple = ()) -> str:
    """Grade the trustworthiness of the SHOWN numbers, separating correctness from completeness:
      RED    — a present-but-wrong value (sentinel) or a scale/shares/currency reconciler finding.
      AMBER  — not core-complete (a real GAP remains) or a non-scale finding to review.
      GREEN  — core-complete (all applicable-free cells filled or honest N/A) with no actionable findings.
    Completeness comes from the N/A-AWARE core matrix (``_incomplete_names``), not the stricter/staler
    per-company status.json — a fully-N/A-resolved 100% row is complete, not AMBER."""
    sentinels = _material_sentinels((status or {}).get("sentinels") or [], waived_metrics)
    has_scale = any(r.get("RootCause") in _SCALE_ROOTS for r in real)
    if sentinels or has_scale:
        return "RED"
    if (not core_complete) or real:
        return "AMBER"
    return "GREEN"


def build_rows(only: set[str] | None, core: bool = False) -> list[dict]:
    findings = _load_real_findings(only)
    incomplete = _incomplete_names()
    waivers = _load_waivers()
    rows = []
    for slug, name, _clave, _template, sector in roster():
        if core and slug in _CORE_EXCLUDE:
            continue
        if only and slug not in only:
            continue
        cov_csv = ROOT / "outputs" / name / "csv" / f"{slug}_coverage.csv"
        if not cov_csv.exists():
            continue  # never built → not part of the book
        st = _load_status(name, slug)
        real = findings.get(name, [])
        pop, tot = _coverage_counts(name, slug)
        wm = tuple((waivers.get(name) or {}).get("metrics") or ())
        n_sent = len(_material_sentinels((st or {}).get("sentinels") or [], wm))
        rows.append({
            "slug": slug, "name": name, "sector": sector,
            "status": (st.get("status") if st else None) or "—",
            "grade": _grade(st, real, core_complete=name not in incomplete, waived_metrics=wm),
            "n_real": len(real), "n_sent": n_sent,
            "findings": "; ".join(sorted({f"{r['Metric']}" for r in real}))[:80],
            "coverage": f"{pop}/{tot}" if tot else "—",
        })
    order = {"RED": 0, "AMBER": 1, "GREEN": 2}
    rows.sort(key=lambda r: (order.get(r["grade"], 3), -r["n_real"], r["name"]))
    return rows


def write_report(rows: list[dict]) -> tuple[Path, Path]:
    out_dir = ROOT / "outputs" / "_reconcile"
    out_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()
    csv_path = out_dir / "book_scorecard.csv"
    md_path = out_dir / "book_scorecard.md"

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Grade", "Company", "Sector", "Status", "Sentinels", "RealFindings",
                    "Coverage", "Findings"])
        for r in rows:
            w.writerow([r["grade"], r["name"], r["sector"], r["status"], r["n_sent"], r["n_real"],
                        r["coverage"], r["findings"]])

    by_grade: dict[str, int] = {}
    for r in rows:
        by_grade[r["grade"]] = by_grade.get(r["grade"], 0) + 1
    emoji = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}

    lines = [f"# Book trust scorecard — {date}", ""]
    lines.append(f"- **{len(rows)} companies** · "
                 + " · ".join(f"{emoji.get(g,'')} {g} {by_grade.get(g,0)}"
                              for g in ("GREEN", "AMBER", "RED")) + ".")
    lines.append("")
    lines.append("> **GREEN** built PASS, zero actionable reconciler findings — trust the row. "
                 "**AMBER** complete but external data pending, or a non-scale finding to review. "
                 "**RED** engine gap / sentinel or a scale/shares/currency defect — do not trust until fixed.")
    lines.append("")
    lines.append("| Grade | Company | Sector | Status | Wrong-value flags | Real | Coverage "
                 "| Actionable metrics |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(f"| {emoji.get(r['grade'],'')} {r['grade']} | {r['name']} | {r['sector']} | "
                     f"{r['status']} | {r['n_sent']} | {r['n_real']} | {r['coverage']} | "
                     f"{r['findings']} |")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, csv_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None, help="comma-separated slugs")
    ap.add_argument("--core", action="store_true", help="grade only the core set (drop _CORE_EXCLUDE)")
    args = ap.parse_args()
    only = set(args.only.split(",")) if args.only else None

    rows = build_rows(only, core=args.core)
    md_path, csv_path = write_report(rows)
    n = {g: sum(1 for r in rows if r["grade"] == g) for g in ("GREEN", "AMBER", "RED")}
    print(f"[scorecard] {len(rows)} companies · GREEN {n['GREEN']} · AMBER {n['AMBER']} · RED {n['RED']}")
    print(f"[scorecard] wrote {md_path}")
    print(f"[scorecard] wrote {csv_path}")
    for r in [x for x in rows if x["grade"] == "RED"][:20]:
        print(f"  RED  {r['name']:18s} {r['status']:11s} {r['findings']}")


if __name__ == "__main__":
    main()
