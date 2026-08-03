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
from src.extract.revisions import (
    DEFAULT_TRUSTED_TIERS,
    ExtractedMetrics,
    FactObservation,
    RevisionMode,
    normalize_period_label,
    policy_config,
    shift_canonical_period,
    unpack_extraction_result,
)


@dataclass
class PeriodSource:
    period: str
    text: str = ""                  # MD&A / layout text  → Tier 3 + Tier 4
    facts: dict | None = None       # *_facts.json ["facts"]  → Tier 1
    pdf_path: Path | None = None    # source PDF  → Tier 2
    period_end: str | None = None   # ISO quarter end, anchors XBRL context selection
    doc: object | None = None       # parse_pdf.DocMeta: detected scale + sections
    # Optional richer output supplied by a company extractor or source adapter.
    # These observations can target any fiscal period, not just ``period``.
    observations: tuple[FactObservation, ...] = ()
    # Artifact lineage carried by directory unions. These fields prevent a
    # current amended report from being silently paired with stale structured
    # facts/PDFs belonging to another estate document.
    source_path: Path | None = None
    source_document_id: str | None = None
    source_artifact_id: str | None = None
    pdf_document_id: str | None = None
    pdf_artifact_id: str | None = None
    facts_path: Path | None = None
    facts_document_id: str | None = None
    facts_artifact_id: str | None = None


_QUARTER_END = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}
_MONETARY_UNITS = {"currency", "miles_mxn"}

# Company-specific deterministic extractors: config ``custom_extractor`` value →
# (module path, function name, wants_period, wants_pdf_path). Onboarding a new
# custom extractor = one entry here plus the src/extract/<name>.py module; an
# unknown value warns loudly at dispatch instead of silently doing nothing.
_CUSTOM_EXTRACTORS = {
    "gruma":     ("src.extract.gruma", "extract_gruma_appendix", False, False),
    "lab":       ("src.extract.lab", "extract_lab_release", True, False),
    "liverpool": ("src.extract.liverpool", "extract_liverpool_release", True, False),
    "herdez":    ("src.extract.herdez", "extract_herdez", True, True),
    "soriana":   ("src.extract.soriana", "extract_soriana", True, True),
    "orbia":     ("src.extract.orbia", "extract_orbia_release", True, False),
    "gmexico":   ("src.extract.gmexico", "extract_gmexico", True, True),
}


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


def _tag_scale_unverified(rows: dict, metric_defs: list[MetricDef]) -> dict:
    """Mark monetary table rows whose scale had no positive evidence.

    The "[scale_unverified]" marker rides in source_line (like tier tags), so
    score_confidence can demote these cells and the verification gate lists them.
    Non-monetary rows (counts, pcts) are scale-free and pass through untouched.
    """
    unit_by_key = {m.key: m.unit for m in metric_defs}
    tagged = {}
    for key, row in rows.items():
        if unit_by_key.get(key) in _MONETARY_UNITS:
            tagged[key] = replace(
                row, source_line=f"[scale_unverified] {row.source_line or ''}"[:100])
        else:
            tagged[key] = row
    return tagged


def _quarantine_implausible(found: dict, cfg: dict | None = None) -> None:
    """Drop values that fail a validator rule with an unambiguous culprit.

    A generic, data-driven safety net (not per-company): a value that fails a
    validator rule pinpointing one offender (segment > consolidated revenue,
    non-positive revenue, a negative cost line, a capex unit artifact) is dropped
    so the result is an honest MISS rather than a confident wrong value. Never
    drops an ``[xbrl]``/``[bmv]``/``[statement]`` value (authoritative tiers,
    matching pipeline._quarantine_series_magnitude). Escape: VALIDATOR_GATE=0.

    ``validator.allow_negative`` (config) exempts listed metrics from the
    negative-cost sanity rule only — e.g. Orbia prints income-tax BENEFITS as
    negative "Income tax" rows, which are genuine, not sign errors.
    """
    if os.environ.get("VALIDATOR_GATE") == "0":
        return
    allow_negative = set(((cfg or {}).get("validator") or {}).get("allow_negative") or [])
    try:
        from src.shared.validator import validate, offending_metric
        for res in validate(found):
            key = offending_metric(res)
            if not key:
                continue
            if key in allow_negative and res.rule == f"sanity_nonneg_{key}":
                continue
            row = found.get(key)
            src_line = getattr(row, "source_line", "") or "" if row is not None else ""
            # Authoritative tiers are never dropped — same exemption set as
            # pipeline._quarantine_series_magnitude.
            if row is None or any(t in src_line for t in ("[xbrl]", "[bmv]", "[statement]")):
                continue
            found.pop(key, None)
    except Exception as exc:
        print(f"tiered: validator gate skipped: {exc}", file=sys.stderr)


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
    found = ExtractedMetrics(observations=src.observations)
    _on = lambda name: tiers is None or name in tiers

    # ---- Tier 1: XBRL structured facts -------------------------------------
    if src.facts and _on("xbrl"):
        try:
            from src.extract.xbrl_facts import (
                extract_comparative_observations_from_xbrl,
                extract_from_xbrl, fact_value_divisor_for, iso_currency_for,
            )
            period_end = src.period_end or period_end_from_label(src.period)
            # Two currency regimes, selected per caller via config:
            #  - default (deliverable/certified path): report in the filing's native
            #    currency, guarded by the expected-currency FILTER (orbia/gmexico
            #    certify USD as printed).
            #  - "convert_to_mxn" (coverage path — soft injects this at call time):
            #    facts-detected USD converts via USDMXN + per-filing millions
            #    sensing, no filter.
            currency_mode = (
                ((cfg or {}).get("xbrl") or {}).get("currency_mode") or "native"
            )
            if currency_mode == "convert_to_mxn":
                ppu = fact_value_divisor_for(
                    cfg, src.facts, currency_mode="convert_to_mxn",
                )
                expected = None
            else:
                ppu = fact_value_divisor_for(cfg, src.facts, currency_mode="native")
                expected = iso_currency_for(cfg)
            _merge_rows(found, extract_from_xbrl(src.facts, metric_defs, period_end, ppu,
                                                 expected_currency=expected), cfg)
            found.observations.extend(extract_comparative_observations_from_xbrl(
                src.facts,
                metric_defs,
                report_period=src.period,
                period_end=period_end,
                pesos_per_unit=ppu,
                expected_currency=expected,
                source_document_id=(src.facts_document_id
                                    or src.source_document_id
                                    or str(src.facts_path or src.source_path or "")),
            ))
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

    # ---- Tier 'note': header-aligned CNBV [800200] income-note segments ------
    # Generalizes the per-company positional regexes that grab segment-revenue
    # rows from the income note: it reads the column header and selects the
    # single-quarter ("Trimestre … Actual") column whose quarter matches the
    # target, so the note's varying column ORDER no longer matters. Opt-in per
    # company via the `income_note:` config block; degrades to {} (positional
    # regex fallback) whenever the header can't be confidently aligned.
    if src.text and (cfg or {}).get("income_note", {}).get("enabled") and _on("note"):
        try:
            from src.extract.income_note import extract_from_income_note
            _merge_rows(found, extract_from_income_note(src.text, metric_defs, cfg, src.period), cfg)
        except Exception as exc:
            print(f"tiered: note tier failed for {src.period}: {exc}", file=sys.stderr)

    # ---- Company-specific deterministic extractors -------------------------
    # Uniformly gated on _on("search") so tier-ablation evals can switch them
    # off together; "search" is in every full tier list, so production and
    # certification runs are unaffected.
    custom = (cfg or {}).get("custom_extractor")
    if src.text and custom:
        if custom not in _CUSTOM_EXTRACTORS:
            print(f"tiered: unknown custom_extractor '{custom}' — not in "
                  f"_CUSTOM_EXTRACTORS (tiered_extract.py); extractor NOT run",
                  file=sys.stderr)
        elif _on("search"):
            mod_path, fn_name, wants_period, wants_pdf = _CUSTOM_EXTRACTORS[custom]
            try:
                import importlib
                fn = getattr(importlib.import_module(mod_path), fn_name)
                args = [src.text, metric_defs]
                if wants_period:
                    args.append(src.period)
                kwargs = {"pdf_path": src.pdf_path} if wants_pdf else {}
                custom_rows, custom_observations = unpack_extraction_result(fn(*args, **kwargs))
                _merge_rows(found, custom_rows, cfg)
                found.observations.extend(custom_observations)
            except Exception as exc:
                print(f"tiered: custom extractor '{custom}' failed for {src.period}: {exc}",
                      file=sys.stderr)

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
                scale, scale_evidence = resolve_table_scale(
                    raw_rows, found, cfg, metric_defs, doc=getattr(src, "doc", None),
                    with_evidence=True,
                )
                table_rows = _scale_table_rows(raw_rows, metric_defs, scale)
                if scale_evidence == "default":
                    # Scale fell through to 1.0 with NO positive signal (no XBRL
                    # overlap, no caption): monetary cells may be off by 1000×.
                    # Tag them so confidence drops and the verification worklist
                    # surfaces them, instead of a silent unit artifact.
                    table_rows = _tag_scale_unverified(table_rows, metric_defs)
            _merge_rows(found, table_rows, cfg)
        except Exception as exc:
            print(f"tiered: Tier 2 (tables) failed for {src.period}: {exc}", file=sys.stderr)

    # ---- Validator safety-net gate: quarantine data-implausible values -----
    # Runs after table gap-fill, before LLM (which may re-seek a dropped metric).
    # A second pass runs after derived ratios below, so a `calc` fallback can't
    # resurrect an implausible value (e.g. tax_expense = ebt - net_income < 0).
    _quarantine_implausible(found, cfg)

    # ---- Tier 4: LLM fallback (opt-in) -------------------------------------
    if use_llm and _on("llm"):
        missing = [m for m in metric_defs if m.key not in found and m.patterns]
        if missing:
            try:
                from src.extract.llm_extract import llm_extract
                # Alpha's approved LLM compatibility fork predates the optional
                # root ``cfg`` keyword; keep the isolated app boundary callable.
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

    # Second gate pass: catch implausible values introduced by `calc` fallbacks.
    _quarantine_implausible(found, cfg)

    return found


# ── Restated comparatives (cross-period post-pass) ──────────────────────────
_PERIOD_LABEL_RE = None  # compiled lazily (re imported locally to match module style)


def _shift_period_label(label: str, years: int = 1) -> str | None:
    """Same-quarter period label ``years`` later, preserving the input format.

    Handles both label conventions used across the project:
    eval-style ``2Q21A`` / ``2Q21`` and pipeline-style ``2021-2T``.
    """
    import re
    global _PERIOD_LABEL_RE
    if _PERIOD_LABEL_RE is None:
        _PERIOD_LABEL_RE = re.compile(
            r"^(?:(?P<y1>\d{4})-(?P<q1>[1-4])T|(?P<q2>[1-4])Q(?P<y2>\d{2})(?P<sfx>A?))$")
    m = _PERIOD_LABEL_RE.match(label or "")
    if not m:
        return None
    if m.group("y1"):
        return f"{int(m.group('y1')) + years}-{m.group('q1')}T"
    yy = (int(m.group("y2")) + years) % 100
    return f"{m.group('q2')}Q{yy:02d}{m.group('sfx')}"


def apply_restated_priors(
    extracted_by_period: dict, metric_defs: list, cfg: dict | None,
) -> list[dict]:
    """Promote next-year comparatives according to explicit and automatic policy.

    Mexican issuers routinely restate comparatives — discontinued operations
    (Bimbo/Ricolino 2021), IFRS-16 adoption (2019), IAS-29 re-expression of
    hyperinflationary subsidiaries (LatAm segments, every year). Analyst models
    track the RESTATED series, which is printed only in the following year's
    filing as the prior-year column. For each metric listed in
    ``cfg['restated_prior']`` (value: list of period labels, or ``all``), period
    P's value is replaced by extracted[P+1y].prior when that capture exists.
    Configuration labels are normalized, so eval-style ``2Q21A`` works against
    production's ``2021-2T`` keys.

    A separate, conservative ``latest_comparative`` policy may be ``off``
    (default), ``allow`` (trusted/official source tiers only), or ``force``.
    Explicit ``restated_prior`` cells always retain their historical behavior and
    take precedence over the automatic policy.  Returns audit events; callers
    which previously ignored the ``None`` return remain compatible.
    """
    spec = (cfg or {}).get("restated_prior") or {}
    if not isinstance(spec, dict):
        spec = {}
    auto_mode, auto_details = policy_config(cfg, "latest_comparative", default="off")
    if not spec and auto_mode is RevisionMode.OFF:
        return []

    raw_auto_metrics = auto_details.get("metrics")
    if isinstance(raw_auto_metrics, str):
        auto_metrics = {raw_auto_metrics}
    elif raw_auto_metrics:
        auto_metrics = {str(key) for key in raw_auto_metrics}
    else:
        auto_metrics = None
    trusted_tiers = {
        str(tier).strip().lower().strip("[]")
        for tier in (auto_details.get("trusted_tiers") or DEFAULT_TRUSTED_TIERS)
    }

    canonical_to_actual = {
        normalize_period_label(period): period for period in extracted_by_period
    }

    def explicitly_configured(key: str, canonical_period: str) -> bool:
        periods = spec.get(key)
        if periods == "all":
            return True
        if isinstance(periods, str):
            periods = [periods]
        return canonical_period in {
            normalize_period_label(item) for item in (periods or [])
        }

    touched: set = set()
    events: list[dict] = []
    # Snapshot the items: a future typed-observation projection may have added a
    # period, and this pass must not mutate the mapping while iterating it.
    for period, rows in list(extracted_by_period.items()):
        canonical_period = normalize_period_label(period)
        next_canonical = shift_canonical_period(canonical_period, 1)
        next_actual = canonical_to_actual.get(next_canonical or "")
        nxt = extracted_by_period.get(next_actual) if next_actual else None
        if not nxt:
            continue
        for key, nrow in nxt.items():
            explicit = explicitly_configured(key, canonical_period)
            automatic = auto_mode is not RevisionMode.OFF and (
                auto_metrics is None or key in auto_metrics
            )
            if not explicit and not automatic:
                continue
            if nrow is None or nrow.prior is None:
                continue
            if not explicit and auto_mode is RevisionMode.ALLOW:
                if _source_tier(nrow) not in trusted_tiers:
                    continue
            old = rows.get(key)
            old_value = getattr(old, "current", None) if old is not None else None
            # Automatic promotion is intentionally quiet when the later filing
            # merely confirms the as-reported number.  Explicit policy still
            # annotates the cell, preserving its established semantics.
            if not explicit and old_value is not None and old_value == nrow.prior:
                continue
            policy = "configured" if explicit else f"auto:{auto_mode.value}"
            provenance = (
                f"[restated] {policy} latest comparative from {next_actual} prior "
                f"for {period} — {nrow.source_line}"
            )
            rows[key] = replace(
                nrow,
                current=nrow.prior,
                prior=None,
                var_pct=None,
                source_line=provenance[:160],
            )
            touched.add(period)
            events.append({
                "kind": "latest_comparative",
                "policy": policy,
                "metric": key,
                "observed_period": canonical_period,
                "source_report_period": normalize_period_label(next_actual),
                "previous_value": old_value,
                "selected_value": nrow.prior,
                "selected_source_tier": _source_tier(nrow),
                "applied": True,
            })
    # Restated components must flow into derived ratios: recompute [calc] rows
    # (only cells that are calc-sourced or absent — raw-extracted ratios stay).
    for period in touched:
        rows = extracted_by_period[period]
        derived = compute_derived_metrics(rows, metric_defs, include_existing=True)
        for key, drow in derived.items():
            old = rows.get(key)
            if old is None or "[calc" in (old.source_line or ""):
                rows[key] = drow
    return events
