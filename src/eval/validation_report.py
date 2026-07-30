"""
validation_report.py — emit a per-company validation report for a built CSV.

This automates the manual check we run after every extraction:
  1. Per-metric confidence (extraction tier + cross-validation), from the
     confidence map ``pipeline.run`` attaches to ``df.attrs['confidence']``.
  2. An accounting-identity audit on the trusted identities (gross profit,
     EBITDA, net income), period by period.
  3. The low-confidence / flagged cells, listed explicitly.
  4. Ground-truth accuracy, when the company is registered in
     ``compare_extractions.COMPANIES`` (otherwise noted as unavailable).

It writes a Markdown report and returns its path. Confidence here means
extraction quality (tier + passed cross-checks), NOT measured accuracy — only
the ground-truth section measures accuracy against hand-labeled values.
"""
from __future__ import annotations

import contextlib
import io
import math
import os
import re
import statistics
from pathlib import Path

_PERIOD_RE = re.compile(r"(\d{4})-(\d)[TQ]", re.IGNORECASE)

# Trusted accounting identities — derived from the declarative table in
# series_checks so the Python audit and the in-sheet identity Check rows
# (segments_sheet) can never drift.
from src.eval.series_checks import IDENTITIES as _IDENTITY_TABLE, identity_expected as _identity_expected

_IDENTITIES = [
    (target, label,
     (lambda v, ident=ident: _identity_expected(v, ident)),
     (op_a, op_b, target))
    for ident in _IDENTITY_TABLE
    for target, op_a, _, op_b, label in (ident,)
]


def _num(x):
    try:
        f = float(x)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def _tier(source: str) -> str:
    m = re.search(r"\[(\w+)\]", source or "")
    return m.group(1) if m else "?"


def _close(a, b, rel=0.03, floor=3.0) -> bool:
    return abs(a - b) <= max(floor, rel * max(abs(a), abs(b)))


def _confidence_table(df, keys) -> tuple[str, dict]:
    conf = getattr(df, "attrs", {}).get("confidence", {}) or {}
    n_periods = df["period"].nunique() if "period" in df.columns else 0
    per_metric: dict = {}
    for (period, key), info in conf.items():
        if key not in keys:
            continue
        d = per_metric.setdefault(key, {"vals": [], "flagged": 0, "tiers": {}})
        d["vals"].append(info.get("confidence", 0.4))
        if info.get("flagged"):
            d["flagged"] += 1
        t = _tier(info.get("source", ""))
        d["tiers"][t] = d["tiers"].get(t, 0) + 1

    lines = ["| Metric | Coverage | Mean conf | Min | Flagged | Dominant source |",
             "|---|---|---|---|---|---|"]
    all_vals = []
    for k in keys:
        d = per_metric.get(k)
        if not d or not d["vals"]:
            lines.append(f"| {k} | 0/{n_periods} | — | — | — | (no values) |")
            continue
        vals = d["vals"]
        all_vals += vals
        tiers = ", ".join(f"{t}:{c}" for t, c in sorted(d["tiers"].items(), key=lambda x: -x[1]))
        lines.append(f"| {k} | {len(vals)}/{n_periods} | {statistics.mean(vals):.2f} | "
                     f"{min(vals):.2f} | {d['flagged']} | {tiers} |")
    overall = f"{statistics.mean(all_vals):.2f}" if all_vals else "—"
    lines.append(f"| **OVERALL** | {len(all_vals)} cells | **{overall}** | | | |")
    return "\n".join(lines), per_metric


def _identity_audit(df) -> str:
    by_period = {}
    for _, row in df.iterrows():
        by_period[str(row["period"])] = {c: _num(row[c]) for c in df.columns if c != "period"}
    violations = []
    for period in sorted(by_period):
        v = by_period[period]
        for key, label, expected_fn, operands in _IDENTITIES:
            if any(v.get(o) is None for o in operands):
                continue
            got = v[key]
            exp = expected_fn(v)
            if not _close(got, exp):
                violations.append(f"- **{period}** — {label}: got {got:,.0f}, expected {exp:,.0f}")
    if not violations:
        return "All trusted identities hold on every period with complete operands. ✓"
    return (f"{len(violations)} identity violation(s) (these cells are also "
            f"low-confidence/marked in the workbook):\n" + "\n".join(violations))


def _flagged_cells(df, keys) -> str:
    conf = getattr(df, "attrs", {}).get("confidence", {}) or {}
    val_by = {}
    for _, row in df.iterrows():
        val_by[str(row["period"])] = row
    rows = []
    for (period, key), info in sorted(conf.items()):
        if key not in keys:
            continue
        if not (info.get("flagged") or info.get("confidence", 1.0) < 0.5):
            continue
        val = _num(val_by.get(period, {}).get(key)) if period in val_by else None
        reason = "failed a cross-check" if info.get("flagged") else "low-confidence extraction"
        rows.append(f"- **{period} · {key}** = {val if val is None else f'{val:,.0f}'} "
                    f"— {reason} (source: {_tier(info.get('source',''))}, "
                    f"conf {info.get('confidence', 0):.2f})")
    if not rows:
        return "No flagged or sub-0.5-confidence cells. ✓"
    return f"{len(rows)} cell(s) to treat with caution:\n" + "\n".join(rows)


def _crosscheck_section(df, keys) -> str | None:
    """Advisory second-model agreement (returns None when the check didn't run).

    Purely informational: agreement is a GT-free accuracy proxy; a disagreement
    marks a cell worth a manual look but never changes any gate verdict.
    """
    conf = getattr(df, "attrs", {}).get("confidence", {}) or {}
    checks = {pk: info["crosscheck"] for pk, info in conf.items()
              if pk[1] in keys and isinstance(info.get("crosscheck"), dict)}
    if not checks:
        return None
    from src.extract.llm_crosscheck import agreement_summary
    summary = agreement_summary(checks)
    model = next(iter(checks.values())).get("model", "?")
    rate = f"{summary['rate']*100:.0f}%" if summary["rate"] is not None else "—"
    lines = [
        f"Independent re-extraction by **{model}** (advisory — never gates):",
        "",
        f"- Cells checked: **{summary['checked']}** "
        f"(agreed {summary['agreed']} · disagreed {summary['disagreed']} · "
        f"uncheckable {summary['unchecked']})",
        f"- **Agreement rate: {rate}**",
    ]
    disagreements = [(pk, r) for pk, r in sorted(checks.items())
                     if r.get("agree") is False]
    if disagreements:
        lines += ["", f"{len(disagreements)} disagreeing cell(s) — prime candidates "
                      "for manual verification:"]
        for (period, key), r in disagreements:
            lines.append(f"- **{period} · {key}** — engine {r['engine_value']:,.2f} "
                         f"vs {r['model']} {r['value']:,.2f}"
                         + (f" (\"{r['evidence']}\")" if r.get("evidence") else ""))
    return "\n".join(lines)


def _ground_truth(slug: str, keys) -> str:
    try:
        from src.eval.compare_extractions import COMPANIES, run_comparison
    except Exception as exc:  # noqa: BLE001
        return f"_Ground-truth check unavailable ({type(exc).__name__})._"
    if slug not in COMPANIES:
        return ("_No ground-truth file registered for this company "
                "(`compare_extractions.COMPANIES`). Confidence above is the only "
                "signal; deeper verification needs hand-labeled values._")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            results = run_comparison(slug)
    except Exception as exc:  # noqa: BLE001
        return f"_Ground-truth comparison errored: {type(exc).__name__}: {exc}._"
    scored = [r for r in results if r["status"] != "EXCLUDED"]
    if not scored:
        return "_Ground-truth registered but no comparable cells._"
    per: dict = {}
    for r in scored:
        d = per.setdefault(r["key"], {"pass": 0, "miss": 0, "fail": 0})
        d[{"PASS": "pass", "MISS": "miss", "FAIL": "fail"}.get(r["status"], "fail")] += 1
    lines = ["Measured against hand-labeled ground truth (deterministic cascade):",
             "", "| Metric | Correct | Miss | Fail | Accuracy |", "|---|---|---|---|---|"]
    tp = tm = tf = 0
    for k in keys:
        d = per.get(k)
        if not d:
            continue
        n = d["pass"] + d["miss"] + d["fail"]
        tp += d["pass"]; tm += d["miss"]; tf += d["fail"]
        acc = f"{d['pass']/n*100:.0f}%" if n else "—"
        lines.append(f"| {k} | {d['pass']} | {d['miss']} | {d['fail']} | {acc} |")
    total = tp + tm + tf
    lines.append(f"| **TOTAL** | {tp} | {tm} | {tf} | "
                 f"**{tp/total*100:.0f}%** |" if total else "| **TOTAL** | 0 | 0 | 0 | — |")
    return "\n".join(lines)


def build_validation_report(df, keys, *, name: str, slug: str) -> str:
    """Return the Markdown validation report for an extracted wide DataFrame."""
    keys = list(keys)
    n_periods = df["period"].nunique() if "period" in df.columns else 0
    periods = sorted(str(p) for p in df["period"]) if "period" in df.columns else []
    span = f"{periods[0]} → {periods[-1]}" if periods else "—"

    conf_table, _ = _confidence_table(df, keys)
    parts = [
        f"# {name} — extraction validation report",
        "",
        f"- Periods: **{n_periods}** ({span})",
        f"- Mapped metrics: **{len(keys)}**",
        "- Confidence = extraction tier + passed cross-checks (NOT accuracy). "
        "Accuracy is only the ground-truth section below.",
        "",
        "## 1. Per-metric confidence",
        "",
        conf_table,
        "",
        "## 2. Accounting-identity audit",
        "",
        _identity_audit(df),
        "",
        "## 3. Flagged / low-confidence cells",
        "",
        _flagged_cells(df, keys),
        "",
    ]
    crosscheck = _crosscheck_section(df, keys)
    if crosscheck:
        parts += ["## 3b. LLM cross-check (advisory)", "", crosscheck, ""]
    parts += [
        "## 4. Ground-truth accuracy",
        "",
        _ground_truth(slug, keys),
        "",
    ]
    return "\n".join(parts)


def write_validation_report(df, keys, *, name: str, slug: str, out_path: Path) -> Path:
    """Build and write the validation report; return the path written."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_validation_report(df, keys, name=name, slug=slug),
                        encoding="utf-8")
    return out_path
