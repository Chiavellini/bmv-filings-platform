#!/usr/bin/env python3
"""Build a company's Soft Coverage workbook end-to-end.

    python3 scripts/build_coverage.py inputs/walmex.md

Flow: spec → native fundamentals (vendored cascade) → native pack (prices/peers/macro) → merge the
filled residual Bloomberg pack (if present) → valuation model → **hard math-verification gate** →
workbook + CSV + validation report, written to ``outputs/<Name>/{excel,csv,validation}``.

The gate (``src.coverage.math_audit``) re-derives every ratio independently and refuses to emit a
deliverable that contains a math error (wrong formula, broken EV bridge, margin ≠ profit/revenue,
``#DIV/0!``, ``NaN``). On failure it writes ``validation/<slug>_MATH_FAILED.md`` and exits non-zero.
"""
from __future__ import annotations

import argparse
import csv
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src.bloomberg.ingest import load_pack  # noqa: E402
from src.bloomberg.template import build_rows, write_template  # noqa: E402
from src.coverage.fundamentals import load_fundamentals  # noqa: E402
from src.coverage.math_audit import audit, report_markdown  # noqa: E402
from src.coverage.native import build_native_pack, filled_cells, merge_packs  # noqa: E402
from src.coverage.spec import parse_spec  # noqa: E402
from src.coverage.valuation import build_model  # noqa: E402
from src.coverage.validate import validate_model, write_report  # noqa: E402
from src.sheets.valuation_sheet import build_valuation_workbook  # noqa: E402


def _engine_config() -> dict:
    p = ROOT / "configs" / "soft.yaml"
    if p.exists():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {}


def _fundamentals_list(spec, cfg) -> list[str]:
    by_slug = cfg.get("fundamentals_by_slug", {}) or {}
    if spec.slug in by_slug:
        list_key = by_slug[spec.slug]
    elif spec.template == "financials":
        list_key = "financials_fundamentals"
    elif spec.template == "reit":
        list_key = "reit_fundamentals"
    else:
        list_key = "industrial_fundamentals"
    return cfg.get(list_key, []) or cfg.get("subject_fundamentals", []), list_key


def _write_csv(model, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["block", "label", "value", "unit", "source", "note"])
        for b in model.blocks:
            for c in b.rows:
                w.writerow([b.id, c.label, "" if c.value is None else c.value,
                            c.unit, c.source, c.note])


@dataclass
class BuildResult:
    slug: str
    name: str
    template: str
    emitted: bool
    audit_passed: bool
    n_errors: int
    n_advisories: int
    n_blank: int
    has_bbg_pack: bool
    n_periods: int
    report: object = None
    model: object = None
    error: str = ""  # set when the pipeline itself blew up
    # Completeness verdict (from validate_model): "PASS" | "INCOMPLETE" | "BROKEN" | "" (pipeline died)
    coverage_status: str = ""
    n_missing_engine: int = 0   # required cells blank due to an engine gap (a bug we own)
    n_missing_data: int = 0     # required cells pending user-supplied Bloomberg/market data
    n_sentinels: int = 0        # present-but-wrong values (always bugs)


def finalize(model, spec, fund, pack, out_root: Path, *, gate: bool = True,
             verbose: bool = False):
    """Build the workbook to a staging file, run the math gate, then emit or quarantine.

    Returns ``(emitted: bool, report)``. When ``gate`` is True and the audit finds a gating error,
    nothing is written under ``out_root`` except ``validation/<slug>_MATH_FAILED.md`` and
    ``emitted`` is False.
    """
    xlsx = out_root / "excel" / f"{spec.name}.xlsx"
    csv_out = out_root / "csv" / f"{spec.slug}_coverage.csv"
    val_out = out_root / "validation" / f"{spec.slug}_validation.md"
    math_out = out_root / "validation" / f"{spec.slug}_math.md"
    fail_out = out_root / "validation" / f"{spec.slug}_MATH_FAILED.md"

    # Build to a staging path first so a failing deliverable never lands in outputs/.
    with tempfile.TemporaryDirectory() as td:
        staged = Path(td) / f"{spec.name}.xlsx"
        build_valuation_workbook(model, spec, fund, pack, staged)
        report = audit(model, spec, fund, pack, staged)

        if gate and not report.passed:
            fail_out.parent.mkdir(parents=True, exist_ok=True)
            fail_out.write_text(report_markdown(report, spec.name), encoding="utf-8")
            # Remove a stale (previously-passing) workbook so a broken build can't masquerade.
            if xlsx.exists():
                xlsx.unlink()
            if verbose:
                print(f"[gate] ❌ {spec.name}: {len(report.errors)} math error(s) — NOT emitted")
                for c in report.errors:
                    print(f"       - {c.name}: {c.detail}")
            return False, report

        # Passed (or gate disabled) → emit the full deliverable.
        xlsx.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(xlsx))

    _write_csv(model, csv_out)
    write_report(model, spec, fund, pack, val_out)
    math_out.parent.mkdir(parents=True, exist_ok=True)
    math_out.write_text(report_markdown(report, spec.name), encoding="utf-8")
    if fail_out.exists():
        fail_out.unlink()  # clear a stale failure marker from a prior broken run
    if verbose:
        verdict = "✅ PASS" if report.passed else "⚠️ emitted with gate disabled"
        print(f"[gate] {verdict} {spec.name}: {len(report.advisories)} advisory")
    return True, report


def build_one(spec_path: str | Path, *, no_network: bool = False, base_year: int | None = None,
              gate: bool = True, verbose: bool = False,
              offline_fundamentals: bool | None = None, subject_facts_only: bool = False,
              bloomberg_dir: str | None = None) -> BuildResult:
    """Full pipeline for one company. Never raises for expected data gaps — pipeline failures are
    captured into ``BuildResult.error`` so a batch run can keep going.

    ``offline_fundamentals`` decouples the XBRL cache read from market data: set it True with
    ``no_network=False`` to use **cached filings + live Yahoo prices/macro** — the fast path that
    skips the ~85s-per-company archive-index fetch while still getting real prices. Defaults to
    ``no_network``."""
    if offline_fundamentals is None:
        offline_fundamentals = no_network
    spec = parse_spec(str(spec_path))
    cfg = _engine_config()
    eng = cfg.get("engine", {})
    try:
        subject_fundamentals, list_key = _fundamentals_list(spec, cfg)
        if verbose:
            print(f"[build] template={spec.template}  fundamentals={list_key} "
                  f"({len(subject_fundamentals)} keys)")

        config_path = ROOT / "configs" / f"{spec.slug}.yaml"
        reports_dir = ROOT / "data" / "reports" / spec.slug
        # subject_facts_only skips the subject's MD&A parse (20 × 30 MB for a quarterly reporter) —
        # safe when every fundamental is an XBRL concept (standard industrial/financial); NOT for
        # companies that need prose metrics (WALMEX segments). bank_ratios/fibra_kpis read the raw
        # filing directly and are unaffected.
        fund = load_fundamentals(spec.slug, reports_dir, config_path, subject_fundamentals,
                                 offline=offline_fundamentals, facts_only=subject_facts_only)

        native_pack = build_native_pack(spec, fund, verify_ssl=False,
                                        with_prices=not no_network, with_macro=not no_network,
                                        offline=offline_fundamentals, with_peers=not subject_facts_only)
        filled = filled_cells(native_pack, spec)

        residual_tmpl = ROOT / "inputs" / f"{spec.slug}.bloomberg.csv"
        write_template(spec, residual_tmpl, base_year=base_year, filled=filled)

        # Optional bloomberg_dir override; defaults to the residual pack dir data/bloomberg/.
        bbg_dir = ROOT / (bloomberg_dir or eng.get("bloomberg_dir", "data/bloomberg"))
        pack_path = bbg_dir / f"{spec.slug}.csv"
        residual_pack = None
        if pack_path.exists():
            residual_pack = load_pack(pack_path, slug=spec.slug).pack
        pack = merge_packs(native_pack, residual_pack)

        model = build_model(spec, fund, pack)
    except Exception as e:  # unexpected pipeline failure — report, don't crash the batch
        return BuildResult(spec.slug, spec.name, spec.template, emitted=False, audit_passed=False,
                           n_errors=0, n_advisories=0, n_blank=0, has_bbg_pack=False,
                           n_periods=0, error=f"{type(e).__name__}: {e}")

    out_root = ROOT / "outputs" / spec.name
    emitted, report = finalize(model, spec, fund, pack, out_root, gate=gate, verbose=verbose)
    n_blank = sum(1 for b in model.blocks for c in b.rows if c.value is None)
    cov = validate_model(model, spec, fund, pack)  # completeness verdict for the scorecard
    # Persist a machine-readable status so the whole-book scorecard can aggregate without a rebuild.
    if emitted:
        try:
            import json
            status_path = out_root / "validation" / f"{spec.slug}_status.json"
            status_path.parent.mkdir(parents=True, exist_ok=True)
            status_path.write_text(json.dumps({
                "slug": spec.slug, "name": spec.name, "template": spec.template,
                "status": cov.status, "checks_passed": cov.checks_passed,
                "audit_passed": report.passed,
                "missing_engine": cov.missing_engine, "missing_data": cov.missing_data,
                "sentinels": cov.sentinels, "n_blank": n_blank,
            }, indent=2), encoding="utf-8")
        except Exception:
            pass
    return BuildResult(
        slug=spec.slug, name=spec.name, template=spec.template, emitted=emitted,
        audit_passed=report.passed, n_errors=len(report.errors), n_advisories=len(report.advisories),
        n_blank=n_blank, has_bbg_pack=(pack_path.exists()), n_periods=len(fund.periods),
        report=report, model=model,
        coverage_status=cov.status, n_missing_engine=len(cov.missing_engine),
        n_missing_data=len(cov.missing_data), n_sentinels=len(cov.sentinels),
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("spec", help="path to inputs/<slug>.md")
    ap.add_argument("--base-year", type=int, default=None)
    ap.add_argument("--no-network", action="store_true",
                    help="skip native price/macro fetch (fundamentals + filled residual pack only)")
    ap.add_argument("--no-gate", action="store_true",
                    help="DEBUG ONLY: emit even if the math gate fails")
    ap.add_argument("--fast", action="store_true",
                    help="skip the subject MD&A parse (XBRL fundamentals only) — big speedup for "
                         "quarterly reporters; NOT for companies needing prose metrics (WALMEX)")
    args = ap.parse_args()

    res = build_one(args.spec, no_network=args.no_network, base_year=args.base_year,
                    gate=not args.no_gate, verbose=True, subject_facts_only=args.fast)
    if res.error:
        print(f"[build] ERROR {res.name}: {res.error}", file=sys.stderr)
        sys.exit(2)
    out_root = ROOT / "outputs" / res.name
    if res.emitted:
        print(f"[build] wrote workbook   -> {out_root / 'excel' / (res.name + '.xlsx')}")
        print(f"[build] wrote csv        -> {out_root / 'csv' / (res.slug + '_coverage.csv')}")
        print(f"[build] wrote validation -> {out_root / 'validation'}")
        if not res.has_bbg_pack:
            print("[build] note: no residual Bloomberg pack — market/estimate cells are blank "
                  "(on the worklist), not wrong.")
        sys.exit(0)
    else:
        print(f"[build] BLOCKED by math gate: {res.n_errors} error(s). See "
              f"{out_root / 'validation' / (res.slug + '_MATH_FAILED.md')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
