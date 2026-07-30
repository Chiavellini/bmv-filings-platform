#!/usr/bin/env python3
"""zero_shot_eval.py — GT-free extraction scorecard for UNCONFIGURED companies.

The extraction layer's ultimate goal is to work on companies it has never been
refined on. This harness measures that cold-start capability over the shared
document-estate view (STRICTLY read-only) without requiring a company config or
ground truth. Per company it reports:

  1. key-mapping rate     — template keys extracted at least once
  2. cell coverage        — filled cells / (keys x periods)
  3. validator pass rate  — accounting identities holding where they apply
  4. suspect rate         — sign/range/magnitude/cross-check flags from the gate
  5. mean confidence      — tier-based confidence over filled cells
  6. LLM agreement rate   — advisory DeepSeek cross-check (the headline GT-free
                            accuracy proxy; requires DEEPSEEK_API_KEY)

Metric surface = the soft coverage templates (industrial/financials/reit key
lists from soft/configs/soft.yaml) — the exact keys downstream consumers need.

Usage:
    python3 scripts/zero_shot_eval.py banorte grupo_bafar cemex gap ...
    python3 scripts/zero_shot_eval.py gap --template industrial --no-crosscheck
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.model.financial_model import METRICS
from src.shared.paths import OUTPUTS_DIR, PROJECT_ROOT, REPORTS_DIR, SHARED_REPORTS_DIR
from src.shared.report_index import index_report_files, infer_period_label, period_sort_key

SOFT_TEMPLATES = PROJECT_ROOT / "soft" / "configs" / "soft.yaml"
OUT_DIR = OUTPUTS_DIR / "_zero_shot"

# Pilot slug → soft template (extend as companies are evaluated).
TEMPLATE_BY_SLUG = {
    "banorte": "financials",
    "danhos": "reit", "fibra_uno": "reit", "fideal": "reit", "fsites": "reit",
    "fibrahd": "reit", "fibramq": "reit", "fibrapl": "reit", "fibratc": "reit",
    "fiho": "reit", "finn": "reit", "fmty": "reit", "fshop": "reit",
}

# Neutral zero-shot config: BMV filings print full pesos → thousands MXN is the
# default project unit. Deliberately NO currency key (configs/generic.yaml says
# USD, which would make the XBRL tier reject MXN-tagged facts).
ZERO_SHOT_CFG = {"company": {"name": "zero-shot", "unit": "miles_mxn"}}


def template_keys(template: str) -> list[str]:
    spec = yaml.safe_load(SOFT_TEMPLATES.read_text(encoding="utf-8"))
    return list(spec[f"{template}_fundamentals"])


def view_dir(slug: str) -> Path | None:
    for base in (SHARED_REPORTS_DIR, REPORTS_DIR):
        d = base / slug
        if d.is_dir() and any(d.glob("*.md")):
            return d
    return None


def collect_periods(view: Path) -> dict[str, dict]:
    """period → {"md": Path, "pdf": Path|None, "facts": dict|None} (deduped)."""
    groups = index_report_files(p for p in view.iterdir() if p.is_file())
    out: dict[str, dict] = {}
    for period, group in groups.items():
        sel = group.selected_path
        if sel is None or sel.suffix.lower() != ".md":
            md = next((p for p in getattr(group, "md_paths", []) or []), None)
            if md is None:
                continue
            sel = md
        pdf = (group.pdf_paths[0] if getattr(group, "pdf_paths", None) else None)
        out[period] = {"md": sel, "pdf": pdf, "facts": None}
    # XBRL facts artifacts live under xbrl/ with ticker-prefixed names.
    xbrl = view / "xbrl"
    if xbrl.is_dir():
        for fp in sorted(xbrl.glob("*_facts.json")):
            period = infer_period_label(fp.stem.replace("_facts", ""))
            if period in out and out[period]["facts"] is None:
                try:
                    facts = json.loads(fp.read_text(encoding="utf-8")).get("facts")
                except Exception:
                    facts = None
                out[period]["facts"] = facts
    return out


def is_xbrl_text(md_path: Path) -> bool:
    try:
        head = md_path.read_text(encoding="utf-8", errors="replace")[:200]
    except Exception:
        return False
    return "BMV XBRL" in head


def evaluate(slug: str, template: str, *, crosscheck: bool = True,
             crosscheck_periods: int = 8, max_periods: int | None = None,
             tiers: set[str] | None = None) -> dict | None:
    import pandas as pd

    from src.extract.llm_crosscheck import agreement_summary, crosscheck_metrics
    from src.extract.tiered_extract import (
        PeriodSource, extract_metrics_tiered, period_end_from_label,
    )
    from src.eval.verification_gate import score_metrics
    from src.shared.validator import flagged_metrics, score_confidence, validate

    view = view_dir(slug)
    if view is None:
        print(f"{slug}: no parsed reports in the estate view or data/reports — skipped",
              file=sys.stderr)
        return None
    keys = template_keys(template)
    defs = [m for m in METRICS if m.key in set(keys)]
    cfg = dict(ZERO_SHOT_CFG)

    periods = collect_periods(view)
    ordered = sorted(periods, key=period_sort_key)
    if max_periods:
        ordered = ordered[-max_periods:]
    cc_targets = set(ordered[-crosscheck_periods:]) if crosscheck else set()

    rows: list[dict] = []
    conf_map: dict[tuple[str, str], dict] = {}
    checks_all: dict[tuple[str, str], dict] = {}
    n_rules = n_rules_passed = 0
    xbrl_text_periods = 0

    for period in ordered:
        info = periods[period]
        text = info["md"].read_text(encoding="utf-8", errors="replace")
        if is_xbrl_text(info["md"]):
            xbrl_text_periods += 1
        src = PeriodSource(period=period, text=text, facts=info["facts"],
                           pdf_path=info["pdf"],
                           period_end=period_end_from_label(period))
        extracted = extract_metrics_tiered(src, defs, cfg, tiers=tiers)
        extracted = {k: v for k, v in extracted.items() if k in set(keys)}

        val_results = validate(extracted)
        n_rules += len(val_results)
        n_rules_passed += sum(1 for r in val_results if r.passed)
        conf_scores = score_confidence(extracted, val_results)
        flagged = flagged_metrics(val_results)

        wide = {"period": period}
        for k, row in extracted.items():
            if row.current is None:
                continue
            wide[k] = row.current
            conf_map[(period, k)] = {
                "confidence": conf_scores.get(k, 0.4),
                "flagged": k in flagged,
                "source": (row.source_line or "")[:80],
            }
        rows.append(wide)

        if period in cc_targets and text.strip():
            checkable = {k: r for k, r in extracted.items() if r.current is not None}
            if checkable:
                for k, res in crosscheck_metrics(checkable, text, cfg).items():
                    checks_all[(period, k)] = res
                    if (period, k) in conf_map:
                        conf_map[(period, k)]["crosscheck"] = res

    if not rows:
        print(f"{slug}: no periods evaluated", file=sys.stderr)
        return None

    df = pd.DataFrame(rows)
    df.attrs["confidence"] = conf_map
    scores, _, worklist = score_metrics(df, keys)

    filled_keys = [k for k in keys if any(k in r for r in rows)]
    n_cells = len(conf_map)
    confs = [i["confidence"] for i in conf_map.values()]
    suspects = [w for w in worklist if w["priority"] == 1]
    cc = agreement_summary(checks_all) if checks_all else None

    return {
        "slug": slug,
        "template": template,
        "source": ("xbrl-text" if xbrl_text_periods > len(ordered) / 2 else "pdf-parsed"),
        "periods": len(ordered),
        "key_rate": f"{len(filled_keys)}/{len(keys)}",
        "cell_coverage": n_cells / (len(keys) * len(ordered)),
        "validator_pass": (n_rules_passed / n_rules) if n_rules else None,
        "suspect_cells": len(suspects),
        "mean_conf": statistics.mean(confs) if confs else None,
        "crosscheck": cc,
        "scores": scores,
        "worklist": suspects,
        "checks": checks_all,
    }


def _fmt_pct(x) -> str:
    return f"{x*100:.0f}%" if x is not None else "—"


def slug_report(r: dict) -> str:
    cc = r["crosscheck"]
    lines = [
        f"# Zero-shot extraction — {r['slug']}",
        "",
        f"- Template: **{r['template']}** ({r['key_rate']} keys extracted) · "
        f"Source: **{r['source']}** · Periods: **{r['periods']}**",
        f"- Cell coverage: **{_fmt_pct(r['cell_coverage'])}** · "
        f"Validator pass rate: **{_fmt_pct(r['validator_pass'])}** · "
        f"Mean confidence: **{r['mean_conf']:.2f}**" if r["mean_conf"] is not None else "",
        f"- Suspect cells: **{r['suspect_cells']}**",
    ]
    if cc:
        rate = _fmt_pct(cc["rate"])
        lines.append(f"- **LLM agreement: {rate}** "
                     f"({cc['agreed']}/{cc['checked']} checked · "
                     f"{cc['unchecked']} uncheckable)")
        dis = [(pk, res) for pk, res in sorted(r["checks"].items())
               if res.get("agree") is False]
        if dis:
            lines += ["", "## Disagreeing cells (verify manually)", ""]
            for (period, key), res in dis:
                lines.append(f"- {period} · {key}: engine {res['engine_value']:,.2f} "
                             f"vs {res['model']} {res['value']:,.2f}")
    lines += ["", "## Per-metric", "",
              "| Metric | Status | Coverage | Mean conf | LLM agree |", "|---|---|---|---|---|"]
    for s in r["scores"]:
        mc = f"{s.mean_conf:.2f}" if s.mean_conf is not None else "—"
        lines.append(f"| {s.key} | {s.status} | {s.covered}/{s.window} | {mc} | "
                     f"{s.llm_agree or '—'} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="GT-free zero-shot extraction scorecard.")
    ap.add_argument("slugs", nargs="+")
    ap.add_argument("--template", choices=["industrial", "financials", "reit"],
                    help="override the per-slug template default (industrial)")
    ap.add_argument("--no-crosscheck", action="store_true",
                    help="skip the DeepSeek advisory cross-check")
    ap.add_argument("--crosscheck-periods", type=int, default=8,
                    help="cross-check only the N most recent periods (cost cap; default 8)")
    ap.add_argument("--max-periods", type=int, default=None)
    ap.add_argument("--table", action="store_true",
                    help="include the pdfplumber table tier (slow — can stall on "
                         "large BMV filings; off by default for zero-shot sweeps)")
    args = ap.parse_args()
    tiers = {"xbrl", "bmv", "note", "search", "prose", "regex_table", "calc"}
    if args.table:
        tiers.add("table")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for slug in args.slugs:
        template = args.template or TEMPLATE_BY_SLUG.get(slug, "industrial")
        print(f"=== {slug} ({template}) ===", file=sys.stderr)
        r = evaluate(slug, template, crosscheck=not args.no_crosscheck,
                     crosscheck_periods=args.crosscheck_periods,
                     max_periods=args.max_periods, tiers=tiers)
        if r is None:
            continue
        results.append(r)
        path = OUT_DIR / f"{slug}.md"
        path.write_text(slug_report(r), encoding="utf-8")
        print(f"  → {path}", file=sys.stderr)

    if results:
        lines = ["# Zero-shot scorecard (GT-free)", "",
                 "XBRL-text companies exercise the bmv/xbrl path; only pdf-parsed "
                 "companies test the full PDF cascade. LLM agreement is the "
                 "headline accuracy proxy (advisory DeepSeek cross-check).", "",
                 "| Company | Template | Source | Periods | Keys | Cell cov | "
                 "Validator | Suspects | Mean conf | LLM agree |",
                 "|---|---|---|---|---|---|---|---|---|---|"]
        for r in results:
            cc = r["crosscheck"]
            agree = _fmt_pct(cc["rate"]) if cc else "—"
            mc = f"{r['mean_conf']:.2f}" if r["mean_conf"] is not None else "—"
            lines.append(
                f"| {r['slug']} | {r['template']} | {r['source']} | {r['periods']} | "
                f"{r['key_rate']} | {_fmt_pct(r['cell_coverage'])} | "
                f"{_fmt_pct(r['validator_pass'])} | {r['suspect_cells']} | {mc} | {agree} |")
        out = OUT_DIR / "zero_shot_scorecard.md"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\nFleet scorecard → {out}")


if __name__ == "__main__":
    main()
