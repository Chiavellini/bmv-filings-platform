#!/usr/bin/env python3
"""
extract_metrics.py — Extract financial metrics from quarterly report markdown files.

Works for any company: uses the comprehensive financial_model.METRICS registry
(60+ metrics, EN + ES patterns) plus company-specific overrides from a YAML config.

Usage:
    python3 extract_metrics.py reportes/2026-1T.md
    python3 extract_metrics.py reportes/2026-1T.md --config configs/sport.yaml
    python3 extract_metrics.py --batch [--config configs/sport.yaml]
    python3 extract_metrics.py --batch --metrics revenue,ebitda,net_income
    python3 extract_metrics.py --batch --csv historico.csv --long
    python3 extract_metrics.py reportes/2026-1T.md --debug
    python3 extract_metrics.py --list-metrics [--section income]
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from src.model.financial_model import (
    METRICS as _BASE_METRICS,
    MetricDef,
    PatternSpec,
    apply_config,
    compute_derived_metrics,
    list_metrics,
    load_config,
    _N, _NL, _FN,
)


# ---------------------------------------------------------------------------
# Core data structure returned by extract_metrics()
# ---------------------------------------------------------------------------

@dataclass
class MetricRow:
    metric:      str
    label_es:    str
    current:     float | None
    prior:       float | None
    var_pct:     float | None
    unit:        str          # "currency" | "pct" | "count" | "ratio" | "per_share"
    source_line: str          # matched text snippet; prefixed "[table]" or "[prose]"


# ---------------------------------------------------------------------------
# Number parsing
# ---------------------------------------------------------------------------

def parse_number(s: str | None) -> float | None:
    """Convert a Spanish/English format number string to float.

    Handles:
      '1,234.5'    → 1234.5
      '( 94,052)'  → -94052.0   (parenthetical negative, space allowed)
      '(143.8%)'   → -143.8
      '14.3%'      → 14.3
      '-94,052'    → -94052.0
      '$589.0'     → 589.0
      '-'          → None
    """
    if not s:
        return None
    s = s.strip()
    if s in ("-", "", "n.a.", "N/A", "—", "n/a"):
        return None
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1]
    s = s.replace(",", "").replace("%", "").replace("$", "").replace(" ", "").strip()
    try:
        val = float(s)
        return -val if negative else val
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Extraction engine
# ---------------------------------------------------------------------------

def _try_pattern(text: str, pspec: PatternSpec) -> tuple[tuple, str] | None:
    """Search full document text for pspec.regex (MULTILINE).
    Returns (float_vals_tuple, annotated_snippet) or None.
    """
    m = re.search(pspec.regex, text, re.IGNORECASE | re.MULTILINE)
    if not m:
        return None
    vals: list[float | None] = []
    for g in m.groups():
        v = parse_number(g)
        if v is not None:
            v *= pspec.multiplier
        vals.append(v)
    if not vals or vals[0] is None:
        return None
    snippet = m.group(0).replace("\n", " ").strip()[:100]
    # Keep PDF table-cell extraction distinct from regexes that happen to read
    # flattened table text. This lets the cascade prefer a header-aware cell read
    # over a brittle fixed-column regex for configured metrics.
    source = getattr(pspec, "source", "table")
    tag = "regex_table" if source == "table" else source
    src_tag = f"[{tag}] "
    return tuple(vals), f"{src_tag}{snippet}"


def _build_row(mdef: MetricDef, vals: tuple, snippet: str) -> MetricRow:
    """Map captured groups to MetricRow fields.

    Convention:
      1 group  → (current,)
      2 groups → (current, prior)
      3 groups → (current, prior, var_pct)
      4 groups → (current, prior, var_abs, var_pct)  — skip var_abs
    """
    current = vals[0] if len(vals) >= 1 else None
    prior   = vals[1] if len(vals) >= 2 else None
    var_pct = vals[-1] if len(vals) >= 3 else None
    return MetricRow(
        metric=mdef.key,
        label_es=mdef.label_es,
        current=current,
        prior=prior,
        var_pct=var_pct,
        unit=mdef.unit,
        source_line=snippet,
    )


def _load_text(source: str | Path) -> str:
    """Load text from a file path or return the string as-is."""
    src = str(source)
    try:
        if isinstance(source, Path) or ("\n" not in src and Path(src).is_file()):
            return Path(src).read_text(encoding="utf-8")
    except OSError:
        pass
    return src


def _split_sections(text: str, anchors: list[tuple[str, str]]) -> dict[str, str]:
    """Split document text into named sections by regex header anchors.

    anchors: ordered list of (section_name, header_regex) pairs.
    Returns {"consolidated": text_before_first_anchor, name1: slice1, ...}.
    Sections that have no matching anchor are absent from the result.
    """
    positions: list[tuple[int, str]] = []
    for name, pattern in anchors:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            positions.append((m.start(), name))
    positions.sort()

    result: dict[str, str] = {}
    prev_end = 0
    prev_name = "consolidated"
    for pos, name in positions:
        result[prev_name] = text[prev_end:pos]
        prev_name = name
        prev_end = pos
    result[prev_name] = text[prev_end:]
    return result


def extract_metrics_segmented(
    source: str | Path,
    metric_defs: list[MetricDef],
    config: dict,
) -> dict[str, MetricRow]:
    """Extract metrics from a multi-section report (e.g. Consolidated / Mexico / CAM).

    Runs extract_metrics on the full text first (captures consolidated P&L and all
    KPI/prose metrics), then re-runs on each named section and adds prefixed keys
    (e.g. "revenue_mexico", "ebitda_cam").

    Falls back to plain extract_metrics when config has no "sections" key.
    """
    sections_cfg = config.get("sections", [])
    if not sections_cfg:
        return extract_metrics(source, metric_defs)

    text = _load_text(source)

    # Split sections first so consolidated-scoping can reference the slices.
    anchors = [(s["name"], s["header_pattern"]) for s in sections_cfg]
    sections = _split_sections(text, anchors)

    # Step 1: consolidated extraction.
    consolidated_cfgs = [s for s in sections_cfg if s.get("role") == "consolidated"]
    if consolidated_cfgs:
        # A segment table (Mexico/CAM) and the consolidated P&L both carry rows
        # like "Total Revenues"; whichever is first in the document wins a naive
        # match. Scope the segment-ambiguous metrics to the consolidated region
        # (pre-first-anchor text + the consolidated-role sections, e.g. an
        # appendix) so the segment tables can't shadow them — this replaces the
        # per-metric "consolidated anchor" regex band-aids. Everything else is
        # extracted document-wide as before; a metric missing from the scope
        # fails open to the full document.
        segment_keys: set[str] = set()
        for s in sections_cfg:
            if s.get("role") != "consolidated":
                segment_keys.update(s.get("metrics", []))
        scope = sections.get("consolidated", "")
        for s in consolidated_cfgs:
            scope += "\n" + sections.get(s["name"], "")

        result = {}
        if segment_keys and scope.strip():
            cons_defs = [m for m in metric_defs if m.key in segment_keys]
            result = extract_metrics(scope, cons_defs)
        other_defs = [m for m in metric_defs if m.key not in result]
        for key, row in extract_metrics(text, other_defs).items():
            result.setdefault(key, row)
    else:
        # Legacy path (unchanged): full-document extraction.
        result = extract_metrics(text, metric_defs)

    # Step 2: per-section extraction with prefixed keys (segment sections only)
    for sec_cfg in sections_cfg:
        if sec_cfg.get("role") == "consolidated":
            continue
        name = sec_cfg["name"]
        sec_text = sections.get(name, "")
        if not sec_text.strip():
            continue
        sec_keys = set(sec_cfg.get("metrics", []))
        sec_defs = [m for m in metric_defs if m.key in sec_keys] if sec_keys else metric_defs
        for key, row in extract_metrics(sec_text, sec_defs).items():
            result[f"{key}_{name}"] = MetricRow(
                metric=f"{key}_{name}",
                label_es=f"{row.label_es} ({name.upper()})",
                current=row.current,
                prior=row.prior,
                var_pct=row.var_pct,
                unit=row.unit,
                source_line=row.source_line,
            )

    # Step 3: global_override — if a section defines global_override keys, re-run
    # extraction on that section's text and replace Step 1 results for those keys.
    # Used to handle reports where the consolidated P&L appears after segment tables
    # (e.g. WALMEX 1Q26 where Appendix 1 contains the true consolidated values).
    for sec_cfg in sections_cfg:
        override_keys = sec_cfg.get("global_override", [])
        if not override_keys:
            continue
        override_text = sections.get(sec_cfg["name"], "")
        if not override_text.strip():
            continue
        override_defs = [m for m in metric_defs if m.key in override_keys]
        for key, row in extract_metrics(override_text, override_defs).items():
            if row.current is not None:
                result[key] = row

    return result


def extract_metrics(
    source: str | Path,
    metric_defs: list[MetricDef] | None = None,
) -> dict[str, MetricRow]:
    """Extract financial metrics from one document.

    Args:
        source:      Path to a .md file, or raw markdown/text string.
        metric_defs: Optional list of MetricDef to use. Defaults to _BASE_METRICS.
                     Pass apply_config(METRICS, cfg) for company-specific extraction.

    Returns:
        Ordered dict mapping metric key → MetricRow.
        Missing metrics are absent (not None-filled).
        Derived ratios are computed after extraction and appended.
    """
    if metric_defs is None:
        metric_defs = _BASE_METRICS

    text = _load_text(source)

    found: dict[str, MetricRow] = {}

    for mdef in metric_defs:
        if mdef.calc and not mdef.patterns:
            continue   # pure derived — handled after extraction
        for pspec in mdef.patterns:
            result = _try_pattern(text, pspec)
            if result is not None:
                vals, snippet = result
                found[mdef.key] = _build_row(mdef, vals, snippet)
                break

    # Compute derived metrics (ratios, calculated fields)
    derived = compute_derived_metrics(found, metric_defs)
    for key, row in derived.items():
        if key not in found:
            found[key] = row

    return found


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _fmt(v: float | None, unit: str, sign: bool = False) -> str:
    if v is None:
        return "—"
    if unit in ("currency", "miles_mxn"):
        return f"{v:,.0f}"
    if unit in ("count", "visits"):
        return f"{v:,.0f}"
    if unit == "pct":
        prefix = "+" if (sign and v > 0) else ""
        return f"{prefix}{v:.1f}%"
    if unit in ("ratio", "per_share"):
        return f"{v:.2f}"
    return f"{v}"


def display_metrics(
    metrics: dict[str, MetricRow],
    tablefmt: str = "simple",
    validation_results=None,
) -> str:
    """Return a formatted table string for terminal display."""
    rows = []
    for row in metrics.values():
        rows.append([
            row.label_es,
            row.unit,
            _fmt(row.current, row.unit),
            _fmt(row.prior, row.unit),
            _fmt(row.var_pct, "pct", sign=True) if row.var_pct is not None else "—",
        ])

    headers = ["Metric", "Unit", "Current", "Prior", "% Var"]

    try:
        from tabulate import tabulate
        table = tabulate(rows, headers=headers, tablefmt=tablefmt,
                         colalign=("left", "left", "right", "right", "right"))
    except ImportError:
        col_w = [max(len(h), max((len(str(r[i])) for r in rows), default=0))
                 for i, h in enumerate(headers)]
        sep = "  ".join("-" * w for w in col_w)
        hdr = "  ".join(h.ljust(col_w[i]) for i, h in enumerate(headers))
        lines = [hdr, sep]
        for r in rows:
            lines.append("  ".join(str(r[i]).ljust(col_w[i]) for i in range(len(headers))))
        table = "\n".join(lines)

    if validation_results:
        from src.shared.validator import validation_summary
        table += "\n" + validation_summary(validation_results)

    return table


# ---------------------------------------------------------------------------
# Batch extraction
# ---------------------------------------------------------------------------

def batch_extract(
    root: Path | None = None,
    glob_pattern: str = "data/reports/sport/*.md",
    metric_defs: list[MetricDef] | None = None,
) -> "pd.DataFrame":
    """Run extract_metrics over all matching files; return wide-format DataFrame."""
    try:
        import pandas as pd
    except ImportError:
        print("pandas required. pip install pandas", file=sys.stderr)
        sys.exit(1)

    if metric_defs is None:
        metric_defs = _BASE_METRICS

    from src.shared.paths import PROJECT_ROOT
    root = root or PROJECT_ROOT
    files = sorted(root.glob(glob_pattern))

    if not files:
        print(f"No files found matching {root / glob_pattern}", file=sys.stderr)
        return pd.DataFrame()

    rows: list[dict] = []
    for f in files:
        period = f.stem
        try:
            metrics = extract_metrics(f, metric_defs)
        except Exception as exc:
            print(f"WARN {f.name}: {exc}", file=sys.stderr)
            continue
        row: dict = {"period": period}
        for key, m in metrics.items():
            row[key] = m.current
        rows.append(row)

    if not rows:
        return pd.DataFrame()

    ordered_cols = ["period"] + [m.key for m in metric_defs if any(m.key in r for r in rows)]
    df = pd.DataFrame(rows).sort_values("period").reset_index(drop=True)
    return df[[c for c in ordered_cols if c in df.columns]]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Extract financial metrics from IR report markdown files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("inputs", nargs="*", metavar="FILE",
                    help=".md report file(s) to process")
    ap.add_argument("--batch", action="store_true",
                    help="Process all reportes/*.md → combined table")
    ap.add_argument("--config", metavar="YAML",
                    help="Company config file (default: configs/sport.yaml if present)")
    ap.add_argument("--csv", metavar="FILE", help="Save output as CSV")
    ap.add_argument("--long", action="store_true",
                    help="Long format CSV: one row per metric (includes confidence)")
    ap.add_argument("--fmt", default="simple",
                    choices=["simple", "grid", "pipe", "latex"],
                    help="Terminal table format (default: simple)")
    ap.add_argument("--metrics", metavar="KEY,...",
                    help="Comma-separated metric keys to include")
    ap.add_argument("--section", metavar="SECTION",
                    choices=["income", "balance", "cashflow", "ratio", "kpi"],
                    help="Filter by financial statement section")
    ap.add_argument("--validate", action="store_true", default=True,
                    help="Run cross-validation checks (default: on)")
    ap.add_argument("--no-validate", action="store_false", dest="validate")
    ap.add_argument("--debug", action="store_true",
                    help="Show matched source snippet for each metric")
    ap.add_argument("--list-metrics", action="store_true",
                    help="List all available metric keys and exit")
    args = ap.parse_args()

    # Load config
    config_path = args.config
    if config_path is None:
        # Neutral generic config first — sport.yaml is a company config and
        # must never leak its overrides into unconfigured runs.
        from src.shared.paths import CONFIGS_DIR
        for name in ("generic.yaml", "sport.yaml"):
            default_cfg = CONFIGS_DIR / name
            if default_cfg.exists():
                config_path = str(default_cfg)
                break

    metric_defs = _BASE_METRICS
    if config_path:
        cfg = load_config(config_path)
        metric_defs = apply_config(_BASE_METRICS, cfg)

    if args.list_metrics:
        list_metrics(section=args.section)
        return

    key_filter: set[str] | None = None
    if args.metrics:
        key_filter = {k.strip() for k in args.metrics.split(",")}
    if args.section:
        section_keys = {m.key for m in metric_defs if m.section == args.section}
        key_filter = section_keys if key_filter is None else key_filter & section_keys

    # ---- batch mode ----
    if args.batch or not args.inputs:
        df = batch_extract(metric_defs=metric_defs)
        if df.empty:
            print("No data extracted.", file=sys.stderr)
            sys.exit(1)
        if key_filter:
            keep = ["period"] + [k for k in df.columns if k in key_filter]
            df = df[[c for c in keep if c in df.columns]]
        try:
            from tabulate import tabulate
            print(tabulate(df, headers="keys", tablefmt=args.fmt,
                           showindex=False, floatfmt=",.0f"))
        except ImportError:
            print(df.to_string(index=False))
        if args.csv:
            df.to_csv(args.csv, index=False)
            print(f"\nSaved → {args.csv}", file=sys.stderr)
        return

    # ---- single / multi-file mode ----
    for path in args.inputs:
        metrics = extract_metrics(path, metric_defs)
        if key_filter:
            metrics = {k: v for k, v in metrics.items() if k in key_filter}

        val_results = None
        if args.validate:
            from src.shared.validator import validate
            val_results = validate(metrics)

        found = len(metrics)
        total = len(metric_defs)
        print(f"\n{'─' * 66}")
        print(f"  {path}  ({found}/{total} metrics found)")
        print(f"{'─' * 66}")
        print(display_metrics(metrics, tablefmt=args.fmt,
                               validation_results=val_results))

        if args.debug and metrics:
            print()
            for row in metrics.values():
                print(f"  [{row.metric}]  {row.source_line}")

    # CSV export (single file, long format)
    if args.csv and len(args.inputs) == 1:
        try:
            import pandas as pd
            from src.shared.validator import validate, score_confidence
            metrics = extract_metrics(args.inputs[0], metric_defs)
            val_results = validate(metrics)
            scores = score_confidence(metrics, val_results)

            if args.long:
                rows = []
                for r in metrics.values():
                    rows.append({
                        "metric":       r.metric,
                        "label":        r.label_es,
                        "current":      r.current,
                        "prior":        r.prior,
                        "var_pct":      r.var_pct,
                        "unit":         r.unit,
                        "confidence":   scores.get(r.metric, 0.4),
                        "source":       r.source_line,
                    })
                pd.DataFrame(rows).to_csv(args.csv, index=False)
            else:
                row = {m: metrics[m].current for m in metrics}
                pd.DataFrame([row]).to_csv(args.csv, index=False)

            print(f"\nSaved → {args.csv}", file=sys.stderr)
        except ImportError:
            print("pandas required for CSV export.", file=sys.stderr)


if __name__ == "__main__":
    main()
