#!/usr/bin/env python3
"""Measure semantic table search against ground-truth actuals.

This is a focused extraction-phase harness. It reads the same PDF table rows and
compares two label matchers:

- legacy: metric aliases scored by parse_tables._label_score
- semantic: configs/metric_search.yaml via SemanticMatcher

It intentionally excludes XBRL, prose regex, derived formulas, and LLM fallback so
changes in the report are attributable to the table-search layer.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.eval.compare_extractions import (
    COMPANIES,
    classify,
    compute_error,
    parse_actuales,
    parse_period,
)
from src.extract.extract_metrics import MetricRow, parse_number
from src.extract.parse_tables import (
    MONETARY_UNITS,
    _MATCH_THRESHOLD,
    _aliases_for,
    _blocks_from_extract_tables,
    _blocks_from_words,
    _label_score,
    _norm,
    _rows_from_extract_tables,
    _rows_from_words,
    match_metrics_from_blocks,
)
from src.extract.semantic_search import SemanticMatcher
from src.model.financial_model import METRICS, MetricDef, apply_config, load_config


TableRows = list[tuple[str, list[str]]]
MatchedRows = dict[str, MetricRow]


def extract_table_rows(pdf_path: Path) -> TableRows:
    """Return the raw table-row candidates production Tier 2 would see."""
    import pdfplumber

    rows: TableRows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            rows.extend(_rows_from_extract_tables(page))
            rows.extend(_rows_from_words(page))
    return rows


def extract_table_blocks(pdf_path: Path) -> list:
    """Return header-aware blocks (period selection) for the same PDF."""
    import pdfplumber

    blocks: list = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            blocks.extend(_blocks_from_extract_tables(page))
            blocks.extend(_blocks_from_words(page))
    return blocks


def blocks_to_rows(blocks: list) -> TableRows:
    """Flatten blocks to (label, [cell_str]) so the legacy control sees the
    exact same rows as the period-aware semantic path."""
    return [(label, [cell for _, cell in pairs]) for block in blocks for label, pairs in block.rows]


def legacy_match_rows(
    candidates: TableRows,
    metric_defs: list[MetricDef],
    *,
    table_scale: float = 1.0,
    skip_keys: frozenset[str] = frozenset(),
) -> MatchedRows:
    """Replicate the pre-semantic alias matcher for before/after measurement."""
    norm_candidates = [(_norm(raw_label), raw_label, cells) for raw_label, cells in candidates]
    found: MatchedRows = {}
    for mdef in metric_defs:
        if mdef.key in skip_keys or (mdef.calc and not mdef.patterns and not mdef.aliases):
            continue
        aliases = _aliases_for(mdef)
        best_score = 0.0
        best_row: tuple[str, list[str]] | None = None
        for norm_label, raw_label, cells in norm_candidates:
            score = max((_legacy_label_score(norm_label, alias) for alias in aliases), default=0.0)
            if score > best_score:
                best_score = score
                best_row = (raw_label, cells)
        if best_score < _MATCH_THRESHOLD or best_row is None:
            continue
        row = _metric_row_from_cells(mdef, best_row[0], best_row[1], table_scale)
        if row is not None:
            found[mdef.key] = row
    return found


@lru_cache(maxsize=200_000)
def _legacy_label_score(norm_label: str, norm_alias: str) -> float:
    """Bound the legacy fuzzy score so full-corpus measurement is tractable."""
    if not norm_label or not norm_alias:
        return 0.0
    if norm_label.startswith(norm_alias):
        return 1.0

    head = norm_label[: len(norm_alias)]
    if not head or head[0] != norm_alias[0]:
        return 0.0

    head_tokens = set(head.split())
    alias_tokens = set(norm_alias.split())
    if alias_tokens and head_tokens and not (head_tokens & alias_tokens):
        return 0.0

    return _label_score(norm_label, norm_alias)


def semantic_match_rows(
    candidates: TableRows,
    metric_defs: list[MetricDef],
    *,
    table_scale: float = 1.0,
    skip_keys: frozenset[str] = frozenset(),
) -> MatchedRows:
    """Run the production semantic matcher over table rows."""
    matcher = SemanticMatcher(metric_defs, skip_keys=skip_keys)
    defs_by_key = {m.key: m for m in metric_defs}
    found: MatchedRows = {}
    for raw_label, cells in candidates:
        match = matcher.best_match(raw_label, threshold=_MATCH_THRESHOLD)
        if match is None or match.metric_key in found:
            continue
        row = _metric_row_from_cells(defs_by_key[match.metric_key], raw_label, cells, table_scale)
        if row is not None:
            found[match.metric_key] = row
    return found


def evaluate_company(company_name: str, *, limit: int | None = None) -> list[dict]:
    """Return per-observation legacy vs semantic measurements for one company."""
    comp = COMPANIES[company_name]
    cfg = load_config(comp["config"])
    metric_defs = apply_config(METRICS, cfg)
    actuals = parse_actuales(comp["actual_file"])
    source_dir = Path(comp["source_dir"])
    table_scale = float(cfg.get("table_scale", 1.0) or 1.0)
    skip_keys = frozenset(
        key for section in (cfg or {}).get("sections", []) for key in section.get("metrics", [])
    )
    currency_scale = float(comp.get("currency_scale", 1.0) or 1.0)

    results: list[dict] = []
    scanned = 0
    for md_file in sorted(source_dir.glob("*.md")):
        period = parse_period(md_file.stem)
        if period is None:
            continue
        pdf_path = md_file.with_suffix(".pdf")
        if not pdf_path.exists():
            continue
        if limit is not None and scanned >= limit:
            break
        scanned += 1

        blocks = extract_table_blocks(pdf_path)
        rows = blocks_to_rows(blocks)
        legacy = legacy_match_rows(
            rows, metric_defs, table_scale=table_scale, skip_keys=skip_keys
        )
        semantic = match_metrics_from_blocks(
            blocks, metric_defs, table_scale=table_scale, skip_keys=skip_keys, period=period
        )

        for key, (section, label, tol_type) in comp["metric_map"].items():
            actual_val = actuals.get((section, label), {}).get(period)
            if actual_val is None:
                continue
            legacy_val = _comparison_value(legacy.get(key), tol_type, currency_scale)
            semantic_val = _comparison_value(semantic.get(key), tol_type, currency_scale)
            results.append({
                "company": company_name,
                "file": md_file.stem,
                "period": period,
                "key": key,
                "actual": actual_val,
                "tol_type": tol_type,
                "legacy": legacy_val,
                "legacy_error": compute_error(legacy_val, actual_val, tol_type)
                if legacy_val is not None else None,
                "legacy_status": _status(legacy_val, actual_val, tol_type),
                "semantic": semantic_val,
                "semantic_error": compute_error(semantic_val, actual_val, tol_type)
                if semantic_val is not None else None,
                "semantic_status": _status(semantic_val, actual_val, tol_type),
            })
    return results


def print_summary(results: list[dict]) -> None:
    by_company: dict[str, list[dict]] = defaultdict(list)
    for row in results:
        by_company[row["company"]].append(row)

    total_legacy = total_semantic = total_obs = 0
    for company, rows in by_company.items():
        legacy = _summary(rows, "legacy")
        semantic = _summary(rows, "semantic")
        total_obs += legacy["total"]
        total_legacy += legacy["pass"]
        total_semantic += semantic["pass"]
        print(f"\n=== {company.upper()} semantic table-search measurement ===")
        _print_variant("legacy", legacy)
        _print_variant("semantic", semantic)
        print(
            f"delta: coverage {semantic['covered'] - legacy['covered']:+d}, "
            f"accurate {semantic['pass'] - legacy['pass']:+d}, "
            f"fail {semantic['fail'] - legacy['fail']:+d}"
        )

        print("\nmetric                         legacy       semantic")
        print("-" * 58)
        for key in sorted({row["key"] for row in rows}):
            metric_rows = [row for row in rows if row["key"] == key]
            left = _compact_metric_summary(metric_rows, "legacy")
            right = _compact_metric_summary(metric_rows, "semantic")
            print(f"{key:<30}{left:>12}{right:>16}")

    if len(by_company) > 1:
        print(
            f"\nTOTAL accurate observations: legacy={total_legacy}/{total_obs}, "
            f"semantic={total_semantic}/{total_obs}, delta={total_semantic - total_legacy:+d}"
        )


def print_details(results: list[dict]) -> None:
    interesting = [
        row for row in results
        if row["legacy_status"] != row["semantic_status"]
        or row["semantic_status"] == "FAIL"
    ]
    if not interesting:
        print("\nNo status changes or semantic failures.")
        return

    print("\nChanged rows and semantic failures")
    print("-" * 110)
    print(
        f"{'company':<8}{'period':<8}{'metric':<28}"
        f"{'legacy':>12}{'semantic':>12}{'actual':>12}{'legacy':>10}{'semantic':>10}"
    )
    for row in interesting:
        tol = row["tol_type"]
        print(
            f"{row['company']:<8}{row['period']:<8}{row['key']:<28}"
            f"{_fmt_value(row['legacy'], tol):>12}"
            f"{_fmt_value(row['semantic'], tol):>12}"
            f"{_fmt_value(row['actual'], tol):>12}"
            f"{row['legacy_status']:>10}{row['semantic_status']:>10}"
        )


def _metric_row_from_cells(
    mdef: MetricDef,
    raw_label: str,
    cells: list[str],
    table_scale: float,
) -> MetricRow | None:
    if mdef.unit in MONETARY_UNITS and (
        "%" in raw_label or any("%" in cell for cell in cells[:3])
    ):
        return None
    vals = [parse_number(cell) for cell in cells]
    vals = [val for val in vals if val is not None]
    if not vals:
        return None
    if table_scale != 1.0 and mdef.unit in MONETARY_UNITS:
        vals = [val * table_scale for val in vals]
    return MetricRow(
        metric=mdef.key,
        label_es=mdef.label_es,
        current=vals[0],
        prior=vals[1] if len(vals) >= 2 else None,
        var_pct=None,
        unit=mdef.unit,
        source_line=f"[table_measure] {raw_label} {' '.join(cells[:3])}".strip()[:100],
    )


def _comparison_value(row: MetricRow | None, tol_type: str, currency_scale: float) -> float | None:
    if row is None or row.current is None:
        return None
    value = row.current
    if tol_type in {"currency", "area"}:
        value *= currency_scale
    return value


def _status(value: float | None, actual: float, tol_type: str) -> str:
    if value is None:
        return "MISS"
    return classify(compute_error(value, actual, tol_type), tol_type)


def _summary(rows: list[dict], prefix: str) -> dict[str, int]:
    total = len(rows)
    covered = sum(1 for row in rows if row[f"{prefix}_status"] != "MISS")
    passed = sum(1 for row in rows if row[f"{prefix}_status"] == "PASS")
    failed = sum(1 for row in rows if row[f"{prefix}_status"] == "FAIL")
    missed = sum(1 for row in rows if row[f"{prefix}_status"] == "MISS")
    return {"total": total, "covered": covered, "pass": passed, "fail": failed, "miss": missed}


def _print_variant(label: str, summary: dict[str, int]) -> None:
    total = summary["total"]
    covered_pct = summary["covered"] / total * 100 if total else 0.0
    pass_pct = summary["pass"] / summary["covered"] * 100 if summary["covered"] else 0.0
    print(
        f"{label:<9} covered={summary['covered']}/{total} ({covered_pct:.0f}%) "
        f"accurate={summary['pass']}/{summary['covered']} ({pass_pct:.0f}%) "
        f"fail={summary['fail']} miss={summary['miss']}"
    )


def _compact_metric_summary(rows: list[dict], prefix: str) -> str:
    covered = sum(1 for row in rows if row[f"{prefix}_status"] != "MISS")
    passed = sum(1 for row in rows if row[f"{prefix}_status"] == "PASS")
    failed = sum(1 for row in rows if row[f"{prefix}_status"] == "FAIL")
    return f"{passed}/{covered}" + (f" F{failed}" if failed else "")


def _fmt_value(value: float | None, tol_type: str) -> str:
    if value is None:
        return "MISS"
    if tol_type in {"currency", "area"}:
        return f"{value:,.1f}"
    if tol_type == "pct":
        return f"{value:.2f}"
    return f"{value:.0f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "companies",
        nargs="*",
        default=["all"],
        help="companies to measure, or all",
    )
    parser.add_argument("--limit", type=int, help="maximum PDFs to scan per company")
    parser.add_argument("--details", action="store_true", help="print changed/failing observations")
    args = parser.parse_args()

    companies = list(COMPANIES) if args.companies == ["all"] else args.companies
    unknown = [company for company in companies if company not in COMPANIES]
    if unknown:
        raise SystemExit(f"Unknown company: {', '.join(unknown)}. Options: {', '.join(COMPANIES)}, all")

    results: list[dict] = []
    for company in companies:
        results.extend(evaluate_company(company, limit=args.limit))
    print_summary(results)
    if args.details:
        print_details(results)


if __name__ == "__main__":
    main()
