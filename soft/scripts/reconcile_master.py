#!/usr/bin/env python3
"""Reconcile the coverage workbook against independent sources → a diagnostic flag report.

    python3 scripts/reconcile_master.py                 # structural + golden + live Yahoo oracle
    python3 scripts/reconcile_master.py --no-network    # deterministic layers only (no Yahoo)
    python3 scripts/reconcile_master.py --only walmex,herdez

For every company it reads the already-emitted per-company coverage CSV (no rebuild) and runs the
layered checks in :mod:`src.coverage.reconcile`:
  * structural  — network-free relationships that must hold (net_margin<=EBITDA_margin, EV bridge,
                  mktcap=px*shares, sane P/S). Catches scale/currency errors the arithmetic gate misses.
  * golden      — hand-verified ranges for large caps (eval/reconcile_golden.yaml).
  * oracle      — Yahoo's own P/E, P/BV, ROE, margins, shares (independent second opinion; best-effort).

Writes a dated, worst-first report to outputs/_reconcile/reconciliation_<date>.{md,csv}. It never
edits the workbook — findings are for a human to investigate and fix at the source.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from scripts.build_master import _CORE_EXCLUDE, roster  # noqa: E402
from src.coverage.reconcile import CompanyValues, Finding, reconcile_company  # noqa: E402


def _load_master_rows() -> dict:
    """{company_name: {metric_header: float}} from the shipped core master — the values actually
    displayed (post _admit_cell / guardrail), so the oracle/golden layers verify what SHIPPED."""
    p = ROOT / "outputs" / "_master" / "soft_coverage_master.csv"
    out: dict[str, dict] = {}
    if not p.exists():
        return out
    rows = list(csv.reader(p.open(encoding="utf-8")))
    hdr = rows[0]
    for r in rows[1:]:
        if len(r) < len(hdr):
            continue
        d = {}
        for i, h in enumerate(hdr[4:], start=4):
            try:
                d[h] = float(r[i])
            except (ValueError, IndexError):
                d[h] = None
        out[r[0]] = d
    return out


def _cv_from_model(model, template: str, shipped: dict | None) -> "CompanyValues | None":
    """Build CompanyValues from a freshly-rebuilt model: structural INPUTS (price/shares/mktcap/ev/
    net_debt/revenue) from ``model.subject_inputs``; the RATIOS from the shipped master row (so the
    oracle/golden layers verify the displayed number, not an un-admitted one)."""
    if model is None:
        return None
    from scripts.build_master import model_to_map, pick_col
    si = model.subject_inputs
    vmap = model_to_map(model)
    s = shipped or {}

    def gi(attr):
        v = getattr(si, attr, None)
        return v if isinstance(v, (int, float)) else None

    return CompanyValues(
        template=template,
        price=gi("px_last"), shares_out=gi("shares_out"), market_cap=gi("market_cap"),
        net_debt=gi("net_debt"), minority_interest=gi("minority_interest"), ev=gi("ev"),
        revenue=gi("sales"),
        pe=s.get("P/E"), pbv=s.get("P/BV"), ev_ebitda=s.get("EV/EBITDA"),
        div_yield=s.get("Div yield"), roe=s.get("ROE"), roic=s.get("ROIC"),
        ebitda_margin=s.get("EBITDA mgn"), net_margin=s.get("Net mgn"),
        gross_margin=pick_col(vmap, "financial_analysis", "Gross margin"),
    )


def _load_golden() -> dict:
    p = ROOT / "eval" / "reconcile_golden.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


def _load_waivers() -> dict:
    p = ROOT / "eval" / "reconcile_waivers.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def _apply_waivers(findings: list[Finding], waivers: dict) -> None:
    """Downgrade explicitly-waived (company, metric) findings to kind='expected' in place."""
    for f in findings:
        w = waivers.get(f.company)
        if w and f.metric in (w.get("metrics") or []):
            f.kind = "expected"
            f.kind_reason = f.kind_reason or (w.get("reason") or "waived")


def _fetch_oracle(clave: str, ticker: str | None, verify_ssl: bool):
    """Best-effort Yahoo key-stats; None on any failure (keeps the run going)."""
    try:
        from src.download.market_data import fetch_key_stats
    except Exception:
        return None
    try:
        return fetch_key_stats(clave, ticker=ticker, verify_ssl=verify_ssl)
    except Exception:
        return None


def _company_ticker(slug: str) -> str | None:
    p = ROOT / "configs" / f"{slug}.yaml"
    if not p.exists():
        return None
    cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return (cfg.get("company") or {}).get("ticker")


def run(only: set[str] | None, *, no_network: bool, core: bool = False,
        rebuild: bool = False) -> list[Finding]:
    golden = _load_golden()
    waivers = _load_waivers()
    companies = [c for c in roster() if (ROOT / "inputs" / f"{c[0]}.md").exists()]
    if core:
        companies = [c for c in companies if c[0] not in _CORE_EXCLUDE]
    if only:
        companies = [c for c in companies if c[0] in only]

    shipped_rows = _load_master_rows() if rebuild else {}
    all_findings: list[Finding] = []
    n_checked = n_oracle = 0
    for slug, name, clave, template, _sector in companies:
        values = None
        if rebuild:
            # rebuild via the exact fast path that produced the matrix, so the verified numbers ARE
            # the shipped ones (the per-company CSV can be stale vs the core master).
            from scripts.build_coverage import build_one
            try:
                res = build_one(ROOT / "inputs" / f"{slug}.md", no_network=False,
                                offline_fundamentals=True, subject_facts_only=True)
                values = _cv_from_model(res.model, template, shipped_rows.get(name)) if res.emitted else None
            except Exception:
                values = None
            if values is None:
                continue
        elif not (ROOT / "outputs" / name / "csv" / f"{slug}_coverage.csv").exists():
            continue
        n_checked += 1
        yahoo = None
        if not no_network:
            yahoo = _fetch_oracle(clave, _company_ticker(slug), verify_ssl=False)
            if yahoo:
                n_oracle += 1
        f = reconcile_company(name, slug, clave, template,
                              golden=golden.get(name), yahoo=yahoo, values=values)
        all_findings.extend(f)

    _apply_waivers(all_findings, waivers)
    all_findings.sort(key=lambda x: x.severity, reverse=True)
    n_real = sum(1 for f in all_findings if f.kind == "real")
    print(f"[reconcile] {n_checked} companies checked · "
          f"{'oracle OFF' if no_network else f'oracle hit {n_oracle}/{n_checked}'} · "
          f"{n_real} actionable · {len(all_findings) - n_real} suppressed "
          f"({len(all_findings)} total)")
    return all_findings


def write_report(findings: list[Finding], n_companies: int) -> tuple[Path, Path]:
    out_dir = ROOT / "outputs" / "_reconcile"
    out_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()
    csv_path = out_dir / f"reconciliation_{date}.csv"
    md_path = out_dir / f"reconciliation_{date}.md"

    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Company", "Metric", "Layer", "Kind", "Ours", "Reference", "RootCause",
                    "Detail", "Severity"])
        for f in findings:
            ours = "" if f.ours is None else f"{f.ours:.4g}"
            w.writerow([f.company, f.metric, f.layer, f.kind, ours, f.reference, f.root_cause,
                        f.detail, f"{f.severity:.0f}"])

    real = [f for f in findings if f.kind == "real"]
    suppressed = [f for f in findings if f.kind != "real"]
    by_company = {f.company for f in real}

    def _row(f):
        ours = "" if f.ours is None else f"{f.ours:.4g}"
        detail = f.detail.replace("|", "\\|")
        return (f"| {f.company} | {f.metric} | {f.layer} | {ours} | {f.reference} | "
                f"{f.root_cause} | {detail} |")

    lines = [f"# Coverage reconciliation — {date}", ""]
    lines.append(f"- **{len(real)} actionable** findings across **{len(by_company)}** companies · "
                 f"**{len(suppressed)} suppressed** (noise/expected) · {n_companies} companies checked.")
    lines.append("")
    lines.append("> Layers — **structural**: relationships that must hold for any real company "
                 "(net income ≤ EBITDA, EV bridge, market cap = price×shares, sane P/S). "
                 "**golden**: hand-verified large-cap ranges. **oracle**: Yahoo's own computed ratios. "
                 "*Actionable* = a candidate engine defect to investigate; *suppressed* = a known "
                 "source artifact (Yahoo denominator / BMV absolute counts) or an expected 'blank this "
                 "cell' action — kept below for audit, not a defect.")
    lines.append("")
    lines.append("## Actionable (real)")
    lines.append("")
    if real:
        lines.append("| Company | Metric | Layer | Ours | Reference | Likely root cause | Detail |")
        lines.append("|---|---|---|---|---|---|---|")
        lines.extend(_row(f) for f in real)
    else:
        lines.append("_None — every finding is a known source artifact or expected action._")
    lines.append("")

    # Suppressed: collapse to a per-reason count, then the detail rows.
    by_reason: dict[str, int] = {}
    for f in suppressed:
        key = f.kind_reason or f.kind
        by_reason[key] = by_reason.get(key, 0) + 1
    lines.append(f"## Suppressed ({len(suppressed)})")
    lines.append("")
    for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {n}× — {reason}")
    lines.append("")
    lines.append("<details><summary>Suppressed detail</summary>")
    lines.append("")
    lines.append("| Company | Metric | Layer | Kind | Ours | Reference | Detail |")
    lines.append("|---|---|---|---|---|---|---|")
    for f in suppressed:
        ours = "" if f.ours is None else f"{f.ours:.4g}"
        detail = f.detail.replace("|", "\\|")
        lines.append(f"| {f.company} | {f.metric} | {f.layer} | {f.kind} | {ours} | "
                     f"{f.reference} | {detail} |")
    lines.append("")
    lines.append("</details>")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path, csv_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", default=None, help="comma-separated slugs")
    ap.add_argument("--no-network", action="store_true", help="deterministic layers only (skip Yahoo)")
    ap.add_argument("--core", action="store_true", help="verify only the core set (drop _CORE_EXCLUDE)")
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild each company (build_one) so the verified numbers match the shipped "
                         "master, instead of reading the possibly-stale per-company CSV")
    args = ap.parse_args()
    only = set(args.only.split(",")) if args.only else None

    findings = run(only, no_network=args.no_network, core=args.core, rebuild=args.rebuild)
    if args.rebuild:
        n_companies = sum(1 for c in roster() if (ROOT / "inputs" / f"{c[0]}.md").exists()
                          and (not args.core or c[0] not in _CORE_EXCLUDE)
                          and (not only or c[0] in only))
    else:
        n_companies = sum(1 for c in roster()
                          if (ROOT / "outputs" / c[1] / "csv" / f"{c[0]}_coverage.csv").exists()
                          and (not args.core or c[0] not in _CORE_EXCLUDE)
                          and (not only or c[0] in only))
    md_path, csv_path = write_report(findings, n_companies)
    print(f"[reconcile] wrote {md_path}")
    print(f"[reconcile] wrote {csv_path}")
    # top actionable findings to console (suppressed noise/expected omitted)
    for f in [x for x in findings if x.kind == "real"][:15]:
        rc = f" [{f.root_cause}]" if f.root_cause else ""
        print(f"  {f.layer:10s} {f.company:16s} {f.metric:16s} {f.detail}{rc}")


if __name__ == "__main__":
    main()
