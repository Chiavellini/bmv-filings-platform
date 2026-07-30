#!/usr/bin/env python3
"""
tiered_extract.py — the 4-tier extraction cascade.

Each metric is answered by the most trustworthy source available, in precision
order; a later tier only fills gaps an earlier tier left:

    Tier 1  XBRL structured facts   (xbrl_facts.extract_from_xbrl)     [xbrl]
    Tier 2  Command-F text search (text_search.extract_from_text_search) [search]
    Tier 3  Regex engine, UNCHANGED (extract_metrics.extract_metrics)  [prose]/[regex_table]
    Tier 2  Table cells, GAP-FILL   (parse_tables.extract_from_tables) [table]
    Tier 4  LLM fallback, opt-in    (llm_extract.llm_extract)          [llm]

Note on order: structured XBRL facts (Tier 1) are authoritative and win first, but
generic table-cell extraction (Tier 2) is LESS reliable than company-tuned regex
(Tier 3) — naive cell reads lack the regex layer's unit scaling and column logic.
So Tier 2 runs AFTER regex and only fills metrics regex left missing.

Tier 3 is the existing engine called verbatim, so `extract_metrics(text, defs)`
keeps its bare-string contract and every existing test stays valid. This module
adds a richer per-period input (`PeriodSource`) and a wrapper around it.

A `PeriodSource(text=..., facts=None, pdf_path=None)` reproduces today's output
exactly: Tiers 1, 2, 4 are skipped and only the regex engine runs.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from src.model.financial_model import MetricDef, compute_derived_metrics
from src.extract.extract_metrics import extract_metrics, extract_metrics_segmented


@dataclass
class PeriodSource:
    period: str
    text: str = ""                  # MD&A / layout text  → Tier 3 + Tier 4
    facts: dict | None = None       # *_facts.json ["facts"]  → Tier 1
    pdf_path: Path | None = None    # source PDF  → Tier 2
    period_end: str | None = None   # ISO quarter end, anchors XBRL context selection
    doc: object | None = None       # parse_pdf.DocMeta: detected scale + sections


_QUARTER_END = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}
_DEFAULT_TIER_ORDER = ["xbrl", "bmv", "search", "prose", "regex_table", "table", "llm", "calc"]
_MONETARY_UNITS = {"currency", "miles_mxn"}


def period_end_from_label(period: str | None) -> str | None:
    """'2026-1T' → '2026-03-31'. Returns None if the label isn't recognized."""
    if not period:
        return None
    # accept YYYY-QT and YYYY-QQ shapes
    import re
    m = re.match(r"(\d{4})\D*([1-4])", period)
    if not m:
        return None
    year, q = m.group(1), m.group(2)
    return f"{year}-{_QUARTER_END[q]}"


def _period_year(period: str | None) -> int | None:
    """Calendar year of a period label, whatever its shape.

    Handles the eval's '1Q16A' / '4Q18A' labels, the pipeline's '2026-1T', and ISO
    ends '2016-03-31' by reusing the canonical header parser (which also knows BMV
    date-range headers). Falls back to a leading 4-digit year for ISO dates."""
    if not period:
        return None
    from src.extract.table_periods import parse_header_token
    header = parse_header_token(period)
    if header is not None and header.year is not None:
        return header.year
    import re
    m = re.match(r"\s*((?:19|20)\d{2})", period)
    return int(m.group(1)) if m else None


def _source_tier(row) -> str:
    """Return the bracketed source tier from a MetricRow.source_line."""
    import re

    source_line = getattr(row, "source_line", "") or ""
    m = re.match(r"\[([^\]]+)\]", source_line)
    if not m:
        return "other"
    tag = m.group(1)
    return "calc" if tag in {"calc", "calculated"} else tag


def _precedence_for(cfg: dict | None, key: str) -> list[str] | None:
    """Config precedence for one metric, best tier first.

    Shape:
      tier_precedence:
        revenue: [xbrl, bmv, table, regex_table, prose]
        default: [...]

    No entry means legacy first-wins behavior.
    """
    spec = (cfg or {}).get("tier_precedence") or {}
    if not isinstance(spec, dict):
        return None
    order = spec.get(key, spec.get("default"))
    if not order:
        return None
    return [str(t) for t in order]


def _merge_rows(found: dict, incoming: dict, cfg: dict | None) -> None:
    """Merge candidate rows into found, honoring optional per-metric precedence."""
    for key, row in incoming.items():
        current = found.get(key)
        if current is None:
            found[key] = row
            continue
        order = _precedence_for(cfg, key)
        if not order:
            continue
        new_tier = _source_tier(row)
        cur_tier = _source_tier(current)
        try:
            new_rank = order.index(new_tier)
        except ValueError:
            new_rank = len(order)
        try:
            cur_rank = order.index(cur_tier)
        except ValueError:
            cur_rank = len(order)
        if new_rank < cur_rank:
            found[key] = row


def _scale_table_rows(rows: dict, metric_defs: list[MetricDef], scale: float) -> dict:
    """Apply an inferred monetary table scale without reparsing the PDF."""
    if scale == 1.0:
        return rows
    unit_by_key = {m.key: m.unit for m in metric_defs}
    scaled = {}
    for key, row in rows.items():
        if unit_by_key.get(key) not in _MONETARY_UNITS:
            scaled[key] = row
            continue
        scaled[key] = replace(
            row,
            current=row.current * scale if row.current is not None else None,
            prior=row.prior * scale if row.prior is not None else None,
        )
    return scaled


def extract_metrics_tiered(
    src: PeriodSource,
    metric_defs: list[MetricDef],
    cfg: dict | None = None,
    *,
    use_llm: bool = False,
    tiers: set | None = None,
) -> dict:
    """Run the cascade for one period; return {metric_key: MetricRow}.

    Earlier tiers win per metric (first confident answer kept). Derived ratios
    are computed once on the union at the end.

    ``tiers`` gates which source tiers may fire (None = all, the production
    default). It lets the eval ablate a single tier (e.g. measure the marginal
    lift of the BMV statement tier) without relying on which inputs are present.
    Names: "xbrl", "bmv", "search", "prose", "table", "llm".
    """
    found: dict = {}
    _on = lambda name: tiers is None or name in tiers

    # ---- Tier 1: XBRL structured facts -------------------------------------
    if src.facts and _on("xbrl"):
        try:
            from src.extract.xbrl_facts import extract_from_xbrl, pesos_per_unit_for
            period_end = src.period_end or period_end_from_label(src.period)
            ppu = pesos_per_unit_for(cfg)
            _merge_rows(found, extract_from_xbrl(src.facts, metric_defs, period_end, ppu), cfg)
        except Exception as exc:
            print(f"tiered: Tier 1 (xbrl) failed for {src.period}: {exc}", file=sys.stderr)

    # ---- Tier 1.5: generic CNBV/BMV standardized statement blocks ----------
    # Config-free extraction of core financials from the regulatory [210000] /
    # [310000] statements (full pesos, fixed taxonomy labels). Authoritative like
    # XBRL but available for the pre-2021 PDF-only era too; gap-fills so XBRL wins.
    if src.text and _on("bmv"):
        try:
            from src.extract.bmv_statements import extract_from_statements
            _merge_rows(found, extract_from_statements(src.text, metric_defs, cfg, src.period), cfg)
        except Exception as exc:
            print(f"tiered: Tier 1.5 (bmv) failed for {src.period}: {exc}", file=sys.stderr)

    # ---- Company-specific deterministic extractors -------------------------
    if src.text and (cfg or {}).get("custom_extractor") == "gruma":
        try:
            from src.extract.gruma import extract_gruma_appendix
            _merge_rows(found, extract_gruma_appendix(src.text, metric_defs), cfg)
        except Exception as exc:
            print(f"tiered: custom extractor failed for {src.period}: {exc}", file=sys.stderr)
    if src.text and (cfg or {}).get("custom_extractor") == "lab" and _on("search"):
        try:
            from src.extract.lab import extract_lab_release
            _merge_rows(found, extract_lab_release(src.text, metric_defs, src.period), cfg)
        except Exception as exc:
            print(f"tiered: LAB custom extractor failed for {src.period}: {exc}", file=sys.stderr)
    if src.text and (cfg or {}).get("custom_extractor") == "liverpool" and _on("search"):
        try:
            from src.extract.liverpool import extract_liverpool_release
            _merge_rows(found, extract_liverpool_release(src.text, metric_defs, src.period), cfg)
        except Exception as exc:
            print(f"tiered: Liverpool custom extractor failed for {src.period}: {exc}", file=sys.stderr)

    # ---- Tier 2: command-F analog over parsed markdown ---------------------
    # Find aliases/row labels in the report text and pick the nearby numeric
    # cells with simple period-aware rules. This is intentionally deterministic
    # and auditable; regex and PDF table extraction remain reinforcements.
    if src.text and _on("search"):
        try:
            from src.extract.text_search import extract_from_text_search
            _merge_rows(found, extract_from_text_search(src.text, metric_defs, cfg, period=src.period), cfg)
        except Exception as exc:
            print(f"tiered: Tier 2 (search) failed for {src.period}: {exc}", file=sys.stderr)

    # ---- Tier 3: regex engine (unchanged) — wins over generic table cells --
    if src.text and _on("prose"):
        if cfg and cfg.get("sections"):
            regex_result = extract_metrics_segmented(src.text, metric_defs, cfg)
        else:
            regex_result = extract_metrics(src.text, metric_defs)
        _merge_rows(found, regex_result, cfg)

    # ---- Tier 2: table cells — GAP-FILL only (runs after regex) ------------
    table_cfg = (cfg or {}).get("table_extract") or {}
    table_disabled = isinstance(table_cfg, dict) and table_cfg.get("disabled")
    if src.pdf_path and _on("table") and not table_disabled:
        try:
            from src.extract.parse_tables import extract_from_tables, resolve_table_scale
            # The printed→stored unit mapping is company-specific. When the config
            # sets `table_scale` it stays authoritative (resolve_table_scale returns
            # it unchanged). Otherwise we infer it: read the table once at scale 1.0,
            # cross-check monetary cells against the XBRL values already in `found`
            # (and the caption) to recover the true scale — so an unseen company
            # scales correctly with no config. Conservative: defaults to 1.0.
            if cfg is not None and "table_scale" in cfg:
                table_rows = extract_from_tables(
                    src.pdf_path, metric_defs,
                    table_scale=float(cfg["table_scale"]), cfg=cfg, period=src.period,
                )
            else:
                raw_rows = extract_from_tables(
                    src.pdf_path, metric_defs, table_scale=1.0, cfg=cfg, period=src.period,
                )
                scale = resolve_table_scale(
                    raw_rows, found, cfg, metric_defs, doc=getattr(src, "doc", None)
                )
                table_rows = _scale_table_rows(raw_rows, metric_defs, scale)
            _merge_rows(found, table_rows, cfg)
        except Exception as exc:
            print(f"tiered: Tier 2 (tables) failed for {src.period}: {exc}", file=sys.stderr)

    # ---- Validator safety-net gate: quarantine data-implausible values -----
    # A generic, data-driven check (not per-company): a value that fails a
    # validator rule with an unambiguous culprit (a segment exceeding consolidated
    # revenue → wrong-row; non-positive revenue) is dropped so the result is an
    # honest MISS instead of a confident wrong value, and so it can't poison
    # derived ratios. Runs after table gap-fill, before LLM (which may re-seek it).
    # Never drops an [xbrl] value (Tier 1 is authoritative). Escape: VALIDATOR_GATE=0.
    if os.environ.get("VALIDATOR_GATE") != "0":
        try:
            from src.shared.validator import validate, offending_metric
            for res in validate(found):
                key = offending_metric(res)
                if not key:
                    continue
                row = found.get(key)
                if row is None or "[xbrl]" in (getattr(row, "source_line", "") or ""):
                    continue
                found.pop(key, None)
        except Exception as exc:
            print(f"tiered: validator gate skipped for {src.period}: {exc}", file=sys.stderr)

    # ---- Tier 4: LLM fallback (opt-in) -------------------------------------
    if use_llm and _on("llm"):
        missing = [m for m in metric_defs if m.key not in found and m.patterns]
        if missing:
            try:
                from src.extract.llm_extract import llm_extract
                for key, row in llm_extract(missing, src.text, found).items():
                    found.setdefault(key, row)
            except Exception as exc:
                print(f"tiered: Tier 4 (llm) skipped for {src.period}: {exc}", file=sys.stderr)

    # ---- Era gates: drop metrics not yet reported in this period's era -----
    # Some metrics did not exist before a given year, so any value is a guess.
    # E.g. SPORT adopted IFRS 16 in 2019; pre-2019 reports carry only pre-IFRS
    # UAFIDA (captured by `ebitda_sin_ifrs`), so post-IFRS `ebitda` does not
    # exist then — MISS is correct. Runs BEFORE derived ratios so a gated input
    # also drops dependents (e.g. net_debt_to_ebitda). No `era_gates` → no-op.
    era_gates = (cfg or {}).get("era_gates") or {}
    if era_gates:
        year = _period_year(src.period) or _period_year(src.period_end)
        if year is not None:
            for key, gate in era_gates.items():
                start = (gate or {}).get("available_from_year")
                if isinstance(start, int) and year < start:
                    found.pop(key, None)

    # ---- Derived ratios on the union --------------------------------------
    # Legacy behavior is still gap-fill. When a config explicitly ranks `calc`
    # above a source tier, derived rows can replace noisy raw ratio cells.
    _merge_rows(found, compute_derived_metrics(found, metric_defs, include_existing=True), cfg)

    return found
