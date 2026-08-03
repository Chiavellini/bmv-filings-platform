#!/usr/bin/env python3
"""
pipeline.py — End-to-end financial data extraction pipeline.

Given an IR URL, a local directory of PDFs, or individual markdown files,
this module downloads reports, parses PDFs, extracts metrics, cross-validates,
and outputs a clean, confidence-scored CSV.

Usage:
    python3 pipeline.py --url "https://company.com/ir/" --csv output.csv
    python3 pipeline.py --url "..." --config configs/sport.yaml --metrics revenue,ebitda
    python3 pipeline.py --dir ./downloads --csv output.csv
    python3 pipeline.py reportes/2026-1T.md
    python3 pipeline.py reportes/ --csv batch.csv
    python3 pipeline.py --list-metrics
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from src.model.financial_model import METRICS as _BASE_METRICS, apply_config, load_config
from src.extract.extract_metrics import MetricRow, extract_metrics, extract_metrics_segmented, display_metrics, parse_number
from src.extract.tiered_extract import PeriodSource, extract_metrics_tiered, period_end_from_label
from src.extract.revisions import (
    ObservationRole,
    apply_observation_revisions,
    normalize_period_label,
    unpack_extraction_result,
)
from src.shared.report_index import (
    controlled_report_directories,
    index_report_files,
    infer_period_label,
    period_sort_key,
)
from src.shared.validator import validate, score_confidence, validation_summary, flagged_metrics


# ---------------------------------------------------------------------------
# Series-level unit-artifact gate
# ---------------------------------------------------------------------------

def _quarantine_series_magnitude(extracted_by_period: dict, metric_defs) -> None:
    """Drop monetary cells orders of magnitude off their own series (in place).

    Complements the per-period validator: a printed-scale artifact (×1000 unit
    slip, a stray regex-table grab like the LAB 1.9e23 case) is invisible inside
    one period but glaring against the metric's history. A cell ≥300× the series
    median magnitude is dropped to an honest MISS. Only oversized outliers are
    gated — undersized values are legitimate (near-zero net income quarters).
    Authoritative tiers ([xbrl]/[bmv]/[statement]) are never dropped, matching
    the per-period gate. Escape hatch: VALIDATOR_GATE=0.
    """
    import os
    from statistics import median

    if os.environ.get("VALIDATOR_GATE") == "0":
        return
    monetary = {m.key for m in metric_defs if m.unit in ("currency", "miles_mxn")}
    by_key: dict[str, list[float]] = {}
    for rows in extracted_by_period.values():
        for key, row in rows.items():
            cur = getattr(row, "current", None)
            if key in monetary and cur:
                by_key.setdefault(key, []).append(abs(cur))
    for key, vals in by_key.items():
        if len(vals) < 5:          # too little history to call an outlier
            continue
        med = median(vals)
        if med <= 0:
            continue
        for period, rows in extracted_by_period.items():
            row = rows.get(key)
            cur = getattr(row, "current", None) if row else None
            if not cur or abs(cur) / med < 300:
                continue
            src_line = getattr(row, "source_line", "") or ""
            if any(t in src_line for t in ("[xbrl]", "[bmv]", "[statement]")):
                continue
            rows.pop(key, None)
            print(f"pipeline: magnitude gate dropped {key} {period} "
                  f"({cur:.4g} vs series median {med:.4g})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Core pipeline function
# ---------------------------------------------------------------------------

def _effective_input_metadata(
    docs: dict[str, PeriodSource],
) -> tuple[tuple[str, ...], tuple[dict[str, object], ...]]:
    """Return exact post-precedence files and their per-period catalog lineage.

    Paths deliberately use ``absolute()`` rather than ``resolve()``: an estate
    compatibility-view path is itself the cataloged artifact consumed by the
    pipeline, while resolving its symlink could substitute an immutable backing
    path with a different artifact identity.
    """
    paths: list[str] = []
    seen: set[str] = set()
    lineage: list[dict[str, object]] = []

    def absolute(path: Path | None) -> str | None:
        return str(Path(path).expanduser().absolute()) if path is not None else None

    for period, src in sorted(docs.items(), key=lambda item: period_sort_key(item[0])):
        source_path = absolute(src.source_path)
        pdf_path = absolute(src.pdf_path)
        facts_path = absolute(src.facts_path)
        record: dict[str, object] = {
            "period": period,
            "source_path": source_path,
            "source_document_id": src.source_document_id,
            "source_artifact_id": src.source_artifact_id,
            "pdf_path": pdf_path,
            "pdf_document_id": src.pdf_document_id,
            "pdf_artifact_id": src.pdf_artifact_id,
            "facts_path": facts_path,
            "facts_document_id": src.facts_document_id,
            "facts_artifact_id": src.facts_artifact_id,
        }
        lineage.append(record)
        for path in (source_path, pdf_path, facts_path):
            if path is not None and path not in seen:
                seen.add(path)
                paths.append(path)
    return tuple(paths), tuple(lineage)

def run(
    source: str | Path | list[str | Path] | tuple[str | Path, ...],
    *,
    metrics: list[str] | None = None,
    config: str | Path | None = None,
    output_csv: Path | None = None,
    output_dir: Path = Path("downloads"),
    do_validate: bool = True,
    long_format: bool = False,
    fmt: str = "simple",
    period_filter: str | None = None,
    max_reports: int = 50,
    use_llm: bool = False,
    use_evidence: bool = False,
    use_llm_labeling: bool = False,
    crosscheck: bool = False,
    confidence_threshold: float = 0.75,
    diagnostics_path: Path | None = None,
    candidates_path: Path | None = None,
    verbose: bool = True,
) -> "pd.DataFrame":
    """
    Run the full extraction pipeline.

    Args:
        source:        One of:
                         - HTTP/HTTPS URL  → download PDFs from IR page
                         - Path to directory → process all PDFs/MDs found
                         - Path to .md file → extract directly
                         - Path to .pdf file → parse then extract
                         - Sequence of directories → one deduplicated period union
        metrics:       Optional list of metric keys to include in output.
                       None = include all.
        config:        Path to a company YAML config. None = auto-detect
                       configs/sport.yaml if present, else configs/generic.yaml.
        output_csv:    If set, write the result CSV here.
        output_dir:    Directory to store downloaded PDFs (URL mode only).
        do_validate:   Run cross-validation checks.
        long_format:   If True, CSV has one row per metric per period (includes
                       confidence and source columns). Otherwise wide format.
        fmt:           Terminal table format (tabulate format string).
        period_filter: Optional string to filter downloaded PDFs by period.
        max_reports:   Maximum PDFs to download (URL mode).

    Returns:
        Wide-format pandas DataFrame: one row per period, columns = metric keys.
    """
    import pandas as pd

    # Resolve metric definitions
    cfg_path = _resolve_config(config)
    cfg: dict = {}
    metric_defs = _BASE_METRICS
    if cfg_path:
        cfg = load_config(cfg_path)
        metric_defs = apply_config(_BASE_METRICS, cfg)
    # LLM fallback may also be switched on per-company in the config.
    use_llm = use_llm or bool((cfg.get("llm") or {}).get("enabled"))
    # Advisory second-model cross-check (never affects gate verdicts).
    crosscheck = crosscheck or bool((cfg.get("llm_crosscheck") or {}).get("enabled"))

    # Resolve source → list of markdown texts with period labels
    docs = _resolve_source(
        source,
        output_dir=output_dir,
        period_filter=period_filter,
        max_reports=max_reports,
        cfg_path=cfg_path,
    )
    input_paths, input_lineage = _effective_input_metadata(docs)

    if not docs:
        print("No documents to process.", file=sys.stderr)
        empty = pd.DataFrame()
        empty.attrs["input_paths"] = input_paths
        empty.attrs["input_lineage"] = input_lineage
        return empty

    # Extract metrics from each document
    all_rows: list[dict] = []
    all_long_rows: list[dict] = []
    # Per-(period, metric) confidence/flag, surfaced to the excel builder via
    # df.attrs so low-confidence / flagged cells can be visually marked.
    conf_map: dict[tuple[str, str], dict] = {}
    evidence_results: list = []
    use_evidence = use_evidence or diagnostics_path is not None or candidates_path is not None

    extracted_by_period: dict[str, dict] = {}
    supplied_observations: list = []
    for period, src in sorted(docs.items()):
        # 4-tier cascade: XBRL facts → table cells → regex (segmented if the
        # config defines sections) → optional LLM fallback. A text-only source
        # reduces to exactly the previous regex behavior.
        if use_evidence:
            from src.extract.evidence import extract_with_evidence
            evidence_result = extract_with_evidence(
                src,
                metric_defs,
                cfg,
                metric_keys=metrics,
                confidence_threshold=confidence_threshold,
                use_llm_labeling=use_llm_labeling or use_llm,
            )
            evidence_results.append(evidence_result)
            extraction_result = evidence_result.accepted
        else:
            extraction_result = extract_metrics_tiered(src, metric_defs, cfg, use_llm=use_llm)

        # Per-company extractors may now return an ExtractionBatch containing
        # observations about arbitrary earlier fiscal periods.  Legacy dicts
        # remain the default and pass through unchanged.
        extracted, batch_observations = unpack_extraction_result(extraction_result)
        supplied_observations.extend(batch_observations)
        # Evidence mode does not route through tiered_extract, and monkeypatched
        # or third-party legacy extractors may return a plain dict.  Preserve any
        # observations attached directly to the source in those cases.
        if not batch_observations:
            supplied_observations.extend(getattr(src, "observations", ()) or ())

        # Drop physically-impossible negatives a fallback tier may have left as the
        # sole candidate (config-driven; e.g. Soriana capex from the cash-flow tier).
        for _k in (cfg.get("reject_negative") or []):
            _r = extracted.get(_k)
            if _r is not None and getattr(_r, "current", None) is not None and _r.current < 0:
                extracted.pop(_k, None)

        extracted_by_period[period] = extracted

    # A trusted comparative is revision evidence for a period already selected
    # into this run; it must not make a one-file extraction unexpectedly sprout
    # historical rows merely because the filing carries prior contexts. An
    # explicitly marked restatement remains allowed to introduce a missing cell.
    selected_periods = {
        normalize_period_label(period) for period in extracted_by_period
    }
    supplied_observations = [
        observation for observation in supplied_observations
        if (
            normalize_period_label(observation.observed_period) in selected_periods
            or observation.role is ObservationRole.RESTATED
            or observation.explicit_restatement
        )
    ]

    # Advisory cross-check: an independent LLM re-extracts what the engine
    # produced and the agreement rides along in conf_map (never gates).
    crosscheck_by_period: dict[str, dict] = {}
    if crosscheck:
        from src.extract.llm_crosscheck import crosscheck_metrics
        for period, src in sorted(docs.items()):
            found = extracted_by_period.get(period) or {}
            if found and getattr(src, "text", ""):
                checks = crosscheck_metrics(found, src.text, cfg)
                if checks:
                    crosscheck_by_period[period] = checks

    # Typed arbitrary-period revisions run first; explicit restated_prior cells
    # remain the final authority and therefore run second.
    revision_events = apply_observation_revisions(
        extracted_by_period, supplied_observations, metric_defs, cfg,
    )

    # Restated comparatives: configured (and optionally trusted automatic) cells
    # take the next year's same-quarter prior-column value; see tiered_extract.
    from src.extract.tiered_extract import apply_restated_priors
    revision_events.extend(apply_restated_priors(extracted_by_period, metric_defs, cfg))

    # Cross-period gate: needs every period extracted, so it runs between the
    # extraction loop and the row-building loop.
    _quarantine_series_magnitude(extracted_by_period, metric_defs)

    skip_rules = set((cfg.get("validator") or {}).get("skip_rules") or [])
    for period, extracted in sorted(extracted_by_period.items()):
        val_results: list = []
        conf_scores: dict[str, float] = {}
        flagged: set = set()
        if do_validate:
            val_results = validate(extracted, skip_rules=skip_rules)
            conf_scores = score_confidence(extracted, val_results)
            flagged = flagged_metrics(val_results)

        # Filter to requested metrics
        if metrics:
            extracted = {k: v for k, v in extracted.items() if k in metrics}

        # Wide row
        wide_row: dict = {"period": period}
        for key, row in extracted.items():
            wide_row[key] = row.current
            conf_map[(period, key)] = {
                "confidence": conf_scores.get(key, 0.4),
                "flagged": key in flagged,
                "source": row.source_line[:80] if row.source_line else "",
            }
            check = (crosscheck_by_period.get(period) or {}).get(key)
            if check is not None:
                conf_map[(period, key)]["crosscheck"] = check
        all_rows.append(wide_row)

        # Long rows (one per metric)
        if long_format or output_csv:
            for key, row in extracted.items():
                all_long_rows.append({
                    "period":     period,
                    "metric":     row.metric,
                    "label":      row.label_es,
                    "current":    row.current,
                    "prior":      row.prior,
                    "var_pct":    row.var_pct,
                    "unit":       row.unit,
                    "confidence": conf_scores.get(key, 0.4),
                    "validated":  all(r.passed for r in val_results
                                      if key in r.metrics_involved),
                    "flagged":    key in flagged,
                    "source":     row.source_line[:80] if row.source_line else "",
                })

    if not all_rows:
        empty = pd.DataFrame()
        empty.attrs["input_paths"] = input_paths
        empty.attrs["input_lineage"] = input_lineage
        return empty

    # Build ordered wide DataFrame
    ordered_keys = [m.key for m in metric_defs]
    df = pd.DataFrame(all_rows).sort_values("period").reset_index(drop=True)
    # Config-driven cross-period delta metrics: e.g. net_new_stores = period-over-
    # period change in total_units. Computed on the chronologically-sorted frame;
    # the first period is NaN (no prior). Reusable across companies via config.
    for tgt, base in ((cfg.get("delta_metrics") or {}).items() if cfg else ()):
        if base in df.columns and (not metrics or tgt in metrics):
            df[tgt] = df[base].diff()

    # ── [verified] override layer ──────────────────────────────────────────────
    # Manually-verified values (data/verified/<slug>.csv) reinforce the sheet at
    # the highest precedence: they fill cells extraction missed and correct wrong
    # ones, tagged "[verified]" with confidence 1.0 so the workbook renders them
    # green. Manual work survives every re-extraction. An UNRESOLVED row keeps
    # the extracted value untouched but records that verification was attempted
    # and failed — the cell ships with its red-flag comment and stops re-entering
    # the gate's worklist.
    for (vp, vk), (vval, vnote) in _load_verified(cfg_path).items():
        if metrics and vk not in metrics:
            continue
        if vk not in df.columns:
            df[vk] = float("nan")
        mask = df["period"] == vp
        if mask.any():
            if vval is UNRESOLVED:
                if (vp, vk) in conf_map:
                    conf_map[(vp, vk)]["verify_status"] = "unresolved"
                    if vnote:
                        conf_map[(vp, vk)]["verify_note"] = vnote
            elif vval is None:
                # blank a wrong extraction (verified not-disclosed) → stays red/missing
                df.loc[mask, vk] = float("nan")
                conf_map.pop((vp, vk), None)
            else:
                df.loc[mask, vk] = vval
                conf_map[(vp, vk)] = {"confidence": 1.0, "flagged": False,
                                      "source": f"[verified] {vnote}"[:80]}

    present_cols = ["period"] + [k for k in ordered_keys if k in df.columns]
    # Dynamic segment keys (e.g. revenue_mexico, ebitda_cam) are created at
    # extraction time and are not in the static registry; keep any that were
    # explicitly requested so they survive into the wide output.
    if metrics:
        present_cols += [k for k in metrics if k in df.columns and k not in present_cols]
    df = df[[c for c in present_cols if c in df.columns]]
    # Carry per-cell confidence/flag for downstream styling (excel marks low-conf
    # cells). df.attrs survives the slicing above and the return.
    df.attrs["confidence"] = conf_map
    # Exact artifacts that survived period/directory precedence. Publication
    # freshness consumes ``input_paths``; it must never receive every candidate
    # present in a historical view. ``input_lineage`` is the audit companion.
    df.attrs["input_paths"] = input_paths
    df.attrs["input_lineage"] = input_lineage
    # Explicit provenance for every applied revision and every typed conflict.
    # Workbook/consumer code can surface this without reparsing source strings.
    df.attrs["revisions"] = revision_events
    # Preserve configured FY semantics for the shared series-suspect gate. Flow
    # metrics (aggregation=sum) must compare quarterly values only with quarters
    # and annual totals only with FY peers; stocks/ratios retain the conservative
    # mixed-period behavior.
    df.attrs["metric_aggregations"] = {
        metric.key: metric.aggregation for metric in metric_defs
    }
    # Cell-level sign/range/magnitude suspicion, shared with the verification
    # gate (series_checks) so the workbook comments and the Phase-6 scorecard
    # always agree. Computed AFTER the override layer: [verified] cells are
    # exempt, unresolved ones keep their entry so the red-flag comment renders.
    # Nothing is dropped here — a flagged low quarter is often a REAL low
    # quarter; it gets a comment and a worklist item, not a deletion.
    from src.eval.series_checks import compute_cell_suspects
    expectations = (cfg.get("metric_expectations") or {}) if cfg else {}
    df.attrs["suspects"] = compute_cell_suspects(
        df, [c for c in df.columns if c != "period"], expectations, conf=conf_map)

    # Print to terminal (skip when called programmatically with verbose=False)
    if verbose:
        try:
            from tabulate import tabulate
            print(tabulate(df, headers="keys", tablefmt=fmt, showindex=False, floatfmt=",.0f"))
        except ImportError:
            print(df.to_string(index=False))

    # Write CSV
    if output_csv:
        if long_format and all_long_rows:
            pd.DataFrame(all_long_rows).sort_values(["period", "metric"]).to_csv(
                output_csv, index=False
            )
        else:
            df.to_csv(output_csv, index=False)
        print(f"\nSaved → {output_csv}", file=sys.stderr)

    if evidence_results and diagnostics_path:
        from src.extract.evidence import write_diagnostics
        write_diagnostics(diagnostics_path, evidence_results)
        print(f"Saved diagnostics → {diagnostics_path}", file=sys.stderr)

    if evidence_results and candidates_path:
        from src.extract.evidence import write_candidates_csv
        write_candidates_csv(candidates_path, evidence_results)
        print(f"Saved candidates → {candidates_path}", file=sys.stderr)

    return df


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

class AmbiguousPeriodSourceError(RuntimeError):
    """Multiple versioned estate derivatives exist with no safe current winner."""


def _resolve_source(
    source: str | Path | list[str | Path] | tuple[str | Path, ...],
    output_dir: Path,
    period_filter: str | None,
    max_reports: int,
    cfg_path: Path | None,
) -> dict[str, PeriodSource]:
    """Return dict mapping period_label → PeriodSource (text + optional facts/pdf)."""
    if isinstance(source, (list, tuple)):
        sources = [Path(item) if not str(item).startswith(("http://", "https://")) else str(item)
                   for item in source]
        directories = [item for item in sources if isinstance(item, Path) and item.is_dir()]
        if len(directories) == len(sources):
            # Resolve all paths as one ordered union so duplicate periods collapse
            # before a PDF is parsed. Later directories have explicit precedence:
            # pass [views/reports/<slug>, views/parsed/<slug>] so the current
            # derivative wins legacy compatibility Markdown while keeping its PDF.
            return _from_directories(directories)

        docs: dict[str, PeriodSource] = {}
        for item in sources:
            incoming = _resolve_source(
                item, output_dir, period_filter, max_reports, cfg_path,
            )
            for period, period_source in incoming.items():
                docs[period] = _merge_period_sources(docs.get(period), period_source)
        return docs

    source = str(source)
    docs: dict[str, PeriodSource] = {}

    if source.startswith(("http://", "https://")):
        docs = _from_url(source, output_dir, period_filter, max_reports, cfg_path)
    elif Path(source).is_file():
        docs = _from_single_file(Path(source))
    elif Path(source).is_dir():
        docs = _from_directory(Path(source))
    else:
        print(f"Source not recognized: {source}", file=sys.stderr)

    return docs


def _merge_period_sources(
    existing: PeriodSource | None, incoming: PeriodSource,
) -> PeriodSource:
    """Merge a later mixed-source item only when its lineage is compatible.

    Directory unions have their own artifact-level selector. This function is
    for lists containing individual files/URLs or a mixture of source kinds.
    Later items are explicit precedence winners. Complementary text/PDF/facts
    may cross-fill only when both sides prove the same catalog document, or when
    neither side has catalog lineage and their artifacts are co-located as one
    explicit local bundle. One-sided or conflicting lineage never cross-pairs.
    """
    if existing is None:
        return incoming

    def document_ids(src: PeriodSource) -> set[str]:
        return {
            value for value in (
                src.source_document_id,
                src.pdf_document_id,
                src.facts_document_id,
            ) if value
        }

    def artifact_parents(src: PeriodSource) -> set[Path]:
        return {
            Path(path).absolute().parent for path in (
                src.source_path,
                src.pdf_path,
                src.facts_path,
            ) if path is not None
        }

    existing_ids = document_ids(existing)
    incoming_ids = document_ids(incoming)
    if existing_ids or incoming_ids:
        compatible = (
            len(existing_ids) == 1
            and len(incoming_ids) == 1
            and existing_ids == incoming_ids
        )
    else:
        compatible = bool(artifact_parents(existing) & artifact_parents(incoming))

    if not compatible:
        # Do not retain observations from the losing artifact either: they may
        # assert values from the stale document version being replaced.
        return incoming

    observations = list(existing.observations)
    for observation in incoming.observations:
        if observation not in observations:
            observations.append(observation)
    return PeriodSource(
        period=incoming.period,
        text=incoming.text or existing.text,
        facts=incoming.facts or existing.facts,
        pdf_path=incoming.pdf_path or existing.pdf_path,
        period_end=incoming.period_end or existing.period_end,
        doc=incoming.doc or existing.doc,
        observations=tuple(observations),
        source_path=incoming.source_path or existing.source_path,
        source_document_id=(incoming.source_document_id
                            or existing.source_document_id),
        source_artifact_id=(incoming.source_artifact_id
                            or existing.source_artifact_id),
        pdf_document_id=(incoming.pdf_document_id
                         or existing.pdf_document_id),
        pdf_artifact_id=(incoming.pdf_artifact_id
                         or existing.pdf_artifact_id),
        facts_path=incoming.facts_path or existing.facts_path,
        facts_document_id=(incoming.facts_document_id
                           or existing.facts_document_id),
        facts_artifact_id=(incoming.facts_artifact_id
                           or existing.facts_artifact_id),
    )


def _from_url(
    url: str,
    output_dir: Path,
    period_filter: str | None,
    max_reports: int,
    cfg_path: Path | None,
) -> dict[str, "PeriodSource"]:
    from src.download.downloader import download_from_ir
    from src.parse.parse_pdf import parse_pdf

    file_pattern = None
    delay_ms = 500
    year_api_urls = None
    use_playwright = None  # auto: static layers first, Playwright as fallback
    browser_first = False
    xbrl_ticker = None
    if cfg_path:
        cfg = load_config(cfg_path)
        ir_cfg = cfg.get("ir_website", {})
        file_pattern = ir_cfg.get("pdf_link_pattern") or None
        delay_ms = int(ir_cfg.get("delay_ms", delay_ms) or delay_ms)
        cfg_max = ir_cfg.get("max_reports")
        if isinstance(cfg_max, int) and cfg_max > 0:
            max_reports = min(max_reports, cfg_max)
        year_api_urls = ir_cfg.get("year_api_urls") or None
        raw_pw = ir_cfg.get("use_playwright")
        use_playwright = None if raw_pw is None else bool(raw_pw)
        browser_first = bool(ir_cfg.get("browser_first", False))
        xbrl_ticker = ir_cfg.get("xbrl_ticker") or None

    # BMV XBRL source first: tagged filings for ~2021+ (quarters it covers win);
    # the IR-page PDFs below fill older periods and financial-sector issuers.
    xbrl_docs: dict[str, "PeriodSource"] = {}
    if xbrl_ticker:
        xbrl_docs = _from_bmv_xbrl(xbrl_ticker, output_dir, period_filter, max_reports)

    pdf_paths = download_from_ir(
        url, output_dir,
        period_filter=period_filter,
        max_reports=max_reports,
        file_pattern=file_pattern or None,
        delay_ms=delay_ms,
        year_api_urls=year_api_urls,
        use_playwright=use_playwright,
        browser_first=browser_first,
    )

    docs: dict[str, PeriodSource] = dict(xbrl_docs)
    indexed = index_report_files(pdf_paths)
    for period, group in sorted(indexed.items(), key=lambda item: period_sort_key(item[0])):
        if period in docs:
            continue  # XBRL filing already covers this quarter
        path = group.selected_path
        if path is None:
            continue
        try:
            md_text, _, doc = parse_pdf(str(path), with_meta=True)
            docs[period] = PeriodSource(
                period=period, text=md_text,
                pdf_path=path if path.suffix.lower() == ".pdf" else None,
                period_end=period_end_from_label(period), doc=doc,
                source_path=path,
            )
        except Exception as exc:
            print(f"WARN parse {path.name}: {exc}", file=sys.stderr)
    return docs


def _facts_artifact_path(json_path: Path) -> Path | None:
    """Return a filing's generated facts artifact, creating it when necessary."""
    from src.download.bmv_xbrl import _logical_stem
    facts_path = json_path.with_name(_logical_stem(json_path) + "_facts.json")
    if not facts_path.exists():
        try:
            from src.download.bmv_xbrl import extract_artifacts
            extract_artifacts(json_path)
        except Exception as exc:
            print(f"WARN xbrl facts {json_path.name}: {exc}", file=sys.stderr)
            return None
    if not facts_path.exists():
        return None
    return facts_path


def _load_facts(json_path: Path) -> dict | None:
    """Read a filing's flat numeric facts (regenerating the artifact if missing)."""
    facts_path = _facts_artifact_path(json_path)
    return _read_facts_artifact(facts_path) if facts_path is not None else None


def _read_facts_artifact(facts_path: Path) -> dict | None:
    """Read either the standard ``{"facts": ...}`` artifact or a flat map."""
    import json

    try:
        payload = json.loads(facts_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    nested = payload.get("facts")
    if isinstance(nested, dict):
        return nested or None
    return payload or None


@dataclass(frozen=True)
class _SourceArtifact:
    path: Path
    source_index: int
    document_id: str | None
    artifact_id: str | None


@dataclass(frozen=True)
class _FactsArtifact:
    path: Path
    facts: dict
    source_index: int
    document_id: str | None
    artifact_id: str | None


def _estate_root_for_artifact(path: Path) -> Path | None:
    """Nearest estate root for a compatibility/parsed-view artifact."""
    for parent in path.absolute().parents:
        if (parent / "catalog.db").is_file() and (parent / "views").is_dir():
            return parent
    return None


def _artifact_lineage(
    path: Path,
    cache: dict[Path, tuple[str | None, str | None]],
) -> tuple[str | None, str | None]:
    """Resolve one view path to ``(document_id, artifact_id)``, read-only."""
    key = path.absolute()
    if key in cache:
        return cache[key]
    estate_root = _estate_root_for_artifact(path)
    if estate_root is None:
        cache[key] = (None, None)
        return cache[key]

    import sqlite3

    catalog = estate_root / "catalog.db"
    values = [str(path.absolute())]
    try:
        values.append(path.absolute().relative_to(estate_root).as_posix())
    except ValueError:
        pass
    values.extend((str(path), str(path.resolve())))
    values = list(dict.fromkeys(values))
    try:
        conn = sqlite3.connect(f"{catalog.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        selected: tuple[str | None, str | None] | None = None
        for value in values:
            rows = conn.execute(
                "SELECT document_id,artifact_id FROM artifacts WHERE path=?",
                (value,),
            ).fetchall()
            document_ids = {str(row["document_id"]) for row in rows}
            artifact_ids = {str(row["artifact_id"]) for row in rows}
            if len(document_ids) == 1:
                selected = (
                    next(iter(document_ids)),
                    next(iter(artifact_ids)) if len(artifact_ids) == 1 else None,
                )
                break
        cache[key] = selected or (None, None)
    except sqlite3.Error:
        cache[key] = (None, None)
    finally:
        if "conn" in locals():
            conn.close()
    return cache[key]


def _artifact_document_id(
    path: Path,
    cache: dict[Path, tuple[str | None, str | None]],
) -> str | None:
    """Compatibility helper returning only the catalog document id."""
    return _artifact_lineage(path, cache)[0]


def _collect_facts_artifacts(
    directories: list[Path],
    document_cache: dict[Path, tuple[str | None, str | None]],
) -> dict[str, list[_FactsArtifact]]:
    found: dict[str, list[_FactsArtifact]] = {}
    for source_index, directory in enumerate(directories):
        for path in sorted(directory.glob("*_facts.json"),
                           key=lambda item: (len(item.name), item.name.lower())):
            period = infer_period_label(path.stem)
            facts = _read_facts_artifact(path)
            if period and facts:
                document_id, artifact_id = _artifact_lineage(path, document_cache)
                found.setdefault(period, []).append(_FactsArtifact(
                    path=path,
                    facts=facts,
                    source_index=source_index,
                    document_id=document_id,
                    artifact_id=artifact_id,
                ))
    return found


def _artifact_preference(candidate) -> tuple:
    """Later source directory first; deterministic shortest filename within it."""
    return (-candidate.source_index, len(candidate.path.name), candidate.path.name.lower())


def _lineage_compatible(selected: _SourceArtifact, candidate) -> bool:
    """True for proven same-document artifacts or an explicit co-located bundle."""
    if selected.document_id or candidate.document_id:
        return bool(
            selected.document_id
            and candidate.document_id
            and selected.document_id == candidate.document_id
        )
    return selected.source_index == candidate.source_index


def _select_compatible_artifact(selected: _SourceArtifact, candidates: list):
    for candidate in sorted(candidates, key=_artifact_preference):
        if _lineage_compatible(selected, candidate):
            return candidate
    return None


def _directory_facts(directory: Path) -> dict[str, tuple[Path, dict]]:
    """Index valid sibling ``*_facts.json`` artifacts by canonical period."""
    return _directories_facts([directory])


def _directories_facts(directories: list[Path]) -> dict[str, tuple[Path, dict]]:
    """Compatibility wrapper: preferred facts per period across controlled dirs."""
    expanded = list(controlled_report_directories(directories))
    candidates = _collect_facts_artifacts(expanded, {})
    return {
        period: (selected.path, selected.facts)
        for period, choices in candidates.items()
        for selected in [sorted(choices, key=_artifact_preference)[0]]
    }


def _from_bmv_xbrl(
    ticker: str,
    output_dir: Path,
    period_filter: str | None,
    max_reports: int,
) -> dict[str, PeriodSource]:
    """Period → PeriodSource (MD&A text + structured facts) from BMV XBRL filings.

    Tier 1 reads the structured facts; the MD&A text is kept so prose-only and
    proprietary metrics (EBITDA, net_debt, clubs…) still reach Tier 3. Empty dict
    on any failure → the pipeline falls back to IR-page PDFs.
    """
    try:
        from src.download.bmv_xbrl import download_ticker, load_mdna_text
        json_paths = download_ticker(ticker, output_dir, max_filings=max_reports, delay_ms=600)
    except Exception as exc:
        print(
            f"BMV XBRL source unavailable for {ticker!r} ({exc}); using IR page only.",
            file=sys.stderr,
        )
        return {}

    from src.download.bmv_xbrl import _logical_stem
    docs: dict[str, PeriodSource] = {}
    for path in json_paths:
        period = _logical_stem(path).split("_", 1)[-1]  # SPORT_2026-1T(.json.gz) → 2026-1T
        if period_filter and period_filter not in period:
            continue
        try:
            text = load_mdna_text(path) or ""
        except Exception as exc:
            print(f"WARN xbrl mdna {path.name}: {exc}", file=sys.stderr)
            text = ""
        facts_path = _facts_artifact_path(path)
        facts = _read_facts_artifact(facts_path) if facts_path is not None else None
        if text or facts:
            docs[period] = PeriodSource(
                period=period, text=text, facts=facts,
                period_end=period_end_from_label(period),
                source_path=path,
                facts_path=facts_path if facts else None,
            )
    if docs:
        print(f"BMV XBRL: {len(docs)} period(s) covered for {ticker}.", file=sys.stderr)
    return docs


def _from_single_file(path: Path) -> dict[str, PeriodSource]:
    period = infer_period_label(path.stem) or path.stem
    if path.name.lower().endswith("_facts.json"):
        facts = _read_facts_artifact(path)
        cache: dict[Path, tuple[str | None, str | None]] = {}
        document_id, artifact_id = _artifact_lineage(path, cache)
        return ({period: PeriodSource(period=period, facts=facts,
                                      period_end=period_end_from_label(period),
                                      facts_path=path,
                                      facts_document_id=document_id,
                                      facts_artifact_id=artifact_id)}
                if facts else {})

    cache = {}
    document_id, artifact_id = _artifact_lineage(path, cache)
    selected = _SourceArtifact(
        path=path, source_index=0,
        document_id=document_id,
        artifact_id=artifact_id,
    )
    facts_choices = _collect_facts_artifacts([path.parent], cache).get(
        normalize_period_label(period), [])
    facts_artifact = _select_compatible_artifact(selected, facts_choices)
    facts = facts_artifact.facts if facts_artifact else None
    common = {
        "facts": facts,
        "source_path": path,
        "source_document_id": selected.document_id,
        "source_artifact_id": selected.artifact_id,
        "pdf_document_id": selected.document_id if path.suffix.lower() == ".pdf" else None,
        "pdf_artifact_id": selected.artifact_id if path.suffix.lower() == ".pdf" else None,
        "facts_path": facts_artifact.path if facts_artifact else None,
        "facts_document_id": facts_artifact.document_id if facts_artifact else None,
        "facts_artifact_id": facts_artifact.artifact_id if facts_artifact else None,
    }
    if path.suffix.lower() == ".pdf":
        from src.parse.parse_pdf import parse_pdf
        md_text, _, doc = parse_pdf(str(path), with_meta=True)
        return {period: PeriodSource(
            period=period, text=md_text, pdf_path=path,
            period_end=period_end_from_label(period), doc=doc, **common,
        )}
    else:
        return {period: PeriodSource(
            period=period, text=path.read_text(encoding="utf-8"),
            period_end=period_end_from_label(period), **common,
        )}


def _from_directory(directory: Path) -> dict[str, PeriodSource]:
    return _from_directories([directory])


def _parsed_view_root(directory: Path) -> Path | None:
    """Return the estate root when ``directory`` is ``views/parsed/<company>``."""
    resolved = directory.resolve()
    parsed = resolved.parent
    if parsed.name == "parsed" and parsed.parent.name == "views":
        return parsed.parent.parent
    return None


def _version_order(value: object) -> tuple:
    """Comparable natural version key (``2026.10`` sorts after ``2026.2``)."""
    import re

    if value is None:
        return ()
    return tuple((0, int(part)) if part.isdigit() else (1, part.lower())
                 for part in re.split(r"(\d+)", str(value)) if part)


def _select_parsed_view_candidate(
    directory: Path, period: str, candidates: list[Path],
) -> Path:
    """Select a current estate derivative, or fail instead of filename guessing."""
    candidates = sorted(candidates, key=lambda path: path.name.lower())
    if len(candidates) == 1:
        return candidates[0]

    estate_root = _parsed_view_root(directory)
    catalog = estate_root / "catalog.db" if estate_root else None
    if catalog is None or not catalog.is_file():
        names = ", ".join(path.name for path in candidates)
        raise AmbiguousPeriodSourceError(
            f"ambiguous parsed derivatives for {period}: {names}; "
            "estate catalog is unavailable, so current version cannot be proven"
        )

    import sqlite3

    candidate_by_path = {path.resolve(): path for path in candidates}
    try:
        conn = sqlite3.connect(f"{catalog.as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        tables = {
            row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required = {"artifacts", "documents", "document_derivations"}
        if not required.issubset(tables):
            raise AmbiguousPeriodSourceError(
                f"ambiguous parsed derivatives for {period}: catalog lacks "
                f"{', '.join(sorted(required - tables))}"
            )
        rows = conn.execute(
            """SELECT a.path,a.document_id,d.created_at,
                      d.processor_name,d.processor_version
               FROM artifacts a
               JOIN documents doc ON doc.document_id=a.document_id
               JOIN document_derivations d ON d.output_artifact_id=a.artifact_id
               WHERE doc.period=? AND a.role='parsed_text' AND a.format='md'""",
            (period,),
        ).fetchall()
        matched = []
        for row in rows:
            catalog_path = Path(row["path"])
            if not catalog_path.is_absolute():
                catalog_path = estate_root / catalog_path
            candidate = candidate_by_path.get(catalog_path.resolve())
            if candidate is not None:
                matched.append((row, candidate))
        if len(matched) != len(candidates):
            raise AmbiguousPeriodSourceError(
                f"ambiguous parsed derivatives for {period}: not every candidate "
                "has verified catalog lineage"
            )

        # Source-record current_document_id is the authority across immutable
        # filing versions. Different current documents imply different families
        # (e.g. release vs statement), which cannot be ranked safely here.
        if {"source_records", "source_record_versions"}.issubset(tables):
            document_ids = sorted({row["document_id"] for row, _ in matched})
            placeholders = ",".join("?" for _ in document_ids)
            current_rows = conn.execute(
                f"""SELECT srv.document_id,sr.current_document_id
                    FROM source_record_versions srv
                    JOIN source_records sr
                      ON sr.source_key=srv.source_key
                     AND sr.source_record_id=srv.source_record_id
                    WHERE srv.document_id IN ({placeholders})""",
                document_ids,
            ).fetchall()
            current_ids = {
                row["document_id"] for row in current_rows
                if row["document_id"] == row["current_document_id"]
            }
            if len(current_ids) == 1:
                matched = [pair for pair in matched if pair[0]["document_id"] in current_ids]
            elif len(current_ids) > 1:
                raise AmbiguousPeriodSourceError(
                    f"ambiguous parsed derivatives for {period}: multiple current "
                    "document families are present"
                )

        document_ids = {row["document_id"] for row, _ in matched}
        if len(document_ids) != 1:
            raise AmbiguousPeriodSourceError(
                f"ambiguous parsed derivatives for {period}: no unique current "
                "estate document can be proven"
            )

        ranked = sorted(
            matched,
            key=lambda pair: (
                pair[0]["created_at"] or "",
                _version_order(pair[0]["processor_version"]),
                pair[0]["processor_name"] or "",
            ),
            reverse=True,
        )
        best_key = (
            ranked[0][0]["created_at"] or "",
            _version_order(ranked[0][0]["processor_version"]),
            ranked[0][0]["processor_name"] or "",
        )
        tied = [pair for pair in ranked if (
            pair[0]["created_at"] or "",
            _version_order(pair[0]["processor_version"]),
            pair[0]["processor_name"] or "",
        ) == best_key]
        if len(tied) != 1:
            raise AmbiguousPeriodSourceError(
                f"ambiguous parsed derivatives for {period}: latest processor "
                "version is tied"
            )
        return ranked[0][1]
    finally:
        if "conn" in locals():
            conn.close()


def _from_directories(directories: list[Path]) -> dict[str, PeriodSource]:
    """Build one period index; later directories explicitly override earlier ones."""
    from src.parse.parse_pdf import parse_pdf

    directories = list(controlled_report_directories(directories))
    docs: dict[str, PeriodSource] = {}
    document_cache: dict[Path, tuple[str | None, str | None]] = {}
    facts_by_period = _collect_facts_artifacts(directories, document_cache)
    selected_by_period: dict[str, _SourceArtifact] = {}
    pdf_by_period: dict[str, list[_SourceArtifact]] = {}
    for source_index, directory in enumerate(directories):
        indexed = index_report_files(directory.iterdir())
        for period, group in indexed.items():
            selected = group.selected_path
            if _parsed_view_root(directory) is not None and group.md_paths:
                selected = _select_parsed_view_candidate(directory, period, group.md_paths)
            if selected is not None:
                document_id, artifact_id = _artifact_lineage(selected, document_cache)
                selected_by_period[period] = _SourceArtifact(
                    path=selected,
                    source_index=source_index,
                    document_id=document_id,
                    artifact_id=artifact_id,
                )
            for pdf_path in group.pdf_paths:
                document_id, artifact_id = _artifact_lineage(pdf_path, document_cache)
                pdf_by_period.setdefault(period, []).append(_SourceArtifact(
                    path=pdf_path,
                    source_index=source_index,
                    document_id=document_id,
                    artifact_id=artifact_id,
                ))

    for period, selected in sorted(selected_by_period.items(),
                                   key=lambda item: period_sort_key(item[0])):
        path = selected.path
        # Keep the sibling PDF reachable even when the parsed .md is selected, so
        # custom extractors can recover tables the markdown mangles (e.g. Soriana's
        # rotated ops table that needs page-geometry reconstruction). Cross-source
        # pairing requires proven same-document lineage; co-located files are an
        # explicit bundle. The same rule protects Tier-1 facts from stale versions.
        pdf_artifact = (
            selected if path.suffix.lower() == ".pdf"
            else _select_compatible_artifact(selected, pdf_by_period.get(period, []))
        )
        sibling_pdf = pdf_artifact.path if pdf_artifact else None
        facts_choices = facts_by_period.get(period, [])
        facts_artifact = _select_compatible_artifact(selected, facts_choices)
        if facts_choices and facts_artifact is None:
            skipped = ", ".join(choice.path.name for choice in facts_choices)
            print(
                f"pipeline: ignored lineage-incompatible facts for {period} "
                f"({skipped}); selected report is {path.name}",
                file=sys.stderr,
            )
        common = {
            "facts": facts_artifact.facts if facts_artifact else None,
            "source_path": path,
            "source_document_id": selected.document_id,
            "source_artifact_id": selected.artifact_id,
            "pdf_document_id": pdf_artifact.document_id if pdf_artifact else None,
            "pdf_artifact_id": pdf_artifact.artifact_id if pdf_artifact else None,
            "facts_path": facts_artifact.path if facts_artifact else None,
            "facts_document_id": (facts_artifact.document_id
                                  if facts_artifact else None),
            "facts_artifact_id": (facts_artifact.artifact_id
                                   if facts_artifact else None),
        }
        try:
            if path.suffix.lower() == ".md":
                docs[period] = PeriodSource(
                    period=period, text=path.read_text(encoding="utf-8"),
                    pdf_path=sibling_pdf,
                    period_end=period_end_from_label(period),
                    **common,
                )
            else:
                md_text, _, doc = parse_pdf(str(path), with_meta=True)
                docs[period] = PeriodSource(
                    period=period, text=md_text,
                    pdf_path=path,
                    period_end=period_end_from_label(period), doc=doc,
                    **common,
                )
        except Exception as exc:
            print(f"WARN parse {path.name}: {exc}", file=sys.stderr)

    # A structured filing may be the only artifact available for a period.  It
    # is still a complete Tier-1 input and must not disappear merely because no
    # parsed markdown/PDF sibling exists.
    for period, choices in facts_by_period.items():
        facts_artifact = sorted(choices, key=_artifact_preference)[0]
        docs.setdefault(period, PeriodSource(
            period=period,
            facts=facts_artifact.facts,
            period_end=period_end_from_label(period),
            facts_path=facts_artifact.path,
            facts_document_id=facts_artifact.document_id,
            facts_artifact_id=facts_artifact.artifact_id,
        ))

    return docs


# Sentinel: verification was attempted for this cell and could not resolve it
# (source unreadable/ambiguous). The extracted value stays, ships with a
# red-flag comment, and is exempt from the gate's worklist.
UNRESOLVED = object()


def _load_verified(cfg_path: Path | None) -> dict[tuple[str, str], tuple[float, str]]:
    """Load manually-verified overrides for a company.

    Resolves ``data/verified/<slug>.csv`` (slug = config stem) with columns
    ``period,key,value,note``. Returns ``{(period, key): (value, note)}`` where
    value is a float, ``None`` (BLANK — verified not-disclosed) or the
    ``UNRESOLVED`` sentinel; empty when the file is absent or unreadable
    (never raises).
    """
    if cfg_path is None:
        return {}
    import csv
    from src.shared.paths import PROJECT_ROOT
    path = PROJECT_ROOT / "data" / "verified" / f"{Path(cfg_path).stem}.csv"
    if not path.exists():
        return {}
    out: dict[tuple[str, str], tuple[float, str]] = {}
    try:
        with path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                period = (row.get("period") or "").strip()
                key = (row.get("key") or "").strip()
                raw = (row.get("value") or "").strip()
                if not period or not key or raw == "":
                    continue
                if raw.upper() == "BLANK":
                    # manual instruction to blank a wrong extraction (not disclosed)
                    out[(period, key)] = (None, (row.get("note") or "").strip())
                    continue
                if raw.upper() == "UNRESOLVED":
                    # verification attempted, source unreadable → keep value + red flag
                    out[(period, key)] = (UNRESOLVED, (row.get("note") or "").strip())
                    continue
                try:
                    val = float(raw.replace(",", ""))
                except ValueError:
                    continue
                out[(period, key)] = (val, (row.get("note") or "").strip())
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARN: verified-overrides file {path} is unreadable ({exc}) — "
            f"ALL manual [verified] overrides for this company are DISABLED "
            f"this run; fix or remove the file.",
            file=sys.stderr,
        )
    return out


def _resolve_config(config: str | Path | None) -> Path | None:
    if config:
        p = Path(config)
        if not p.exists():
            print(f"Config not found: {p}", file=sys.stderr)
            return None
        return p
    # Auto-detect: neutral generic config first — sport.yaml is a company
    # config and must never leak its overrides into unconfigured runs.
    from src.shared.paths import CONFIGS_DIR
    for name in ("generic.yaml", "sport.yaml"):
        candidate = CONFIGS_DIR / name
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="End-to-end IR financial extraction pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("source", nargs="?", default="reportes/",
                    help="IR URL, local directory, or .md/.pdf file (default: reportes/)")
    ap.add_argument("--url", metavar="URL",
                    help="IR website URL (alternative to positional source)")
    ap.add_argument("--dir", metavar="DIR",
                    help="Local directory of PDFs/MDs (alternative to positional source)")
    ap.add_argument("--config", metavar="YAML",
                    help="Company config YAML (default: auto-detect configs/sport.yaml)")
    ap.add_argument("--csv", metavar="FILE", help="Output CSV path")
    ap.add_argument("--long", action="store_true",
                    help="Long CSV format: one row per metric (with confidence & source)")
    ap.add_argument("--metrics", metavar="KEY,...",
                    help="Comma-separated metric keys to extract")
    ap.add_argument("--section", metavar="SECTION",
                    choices=["income", "balance", "cashflow", "ratio", "kpi"],
                    help="Filter output by financial statement section")
    ap.add_argument("--no-validate", action="store_true",
                    help="Skip cross-validation")
    ap.add_argument("--fmt", default="simple",
                    choices=["simple", "grid", "pipe", "latex"],
                    help="Terminal table format")
    ap.add_argument("--filter", metavar="TEXT",
                    help="Period filter for URL downloads (e.g. '2024')")
    ap.add_argument("--max", type=int, default=50,
                    help="Max PDFs to download from IR page")
    ap.add_argument("--out", default="downloads",
                    help="Directory for downloaded PDFs")
    ap.add_argument("--list-metrics", action="store_true",
                    help="Print all available metric keys and exit")
    ap.add_argument("--llm", action="store_true",
                    help="Enable Tier 4 LLM fallback for metrics the deterministic "
                         "tiers miss (requires ANTHROPIC_API_KEY; off by default)")
    ap.add_argument("--crosscheck", action="store_true",
                    help="Advisory second-model cross-check of extracted values "
                         "(DeepSeek by default; requires DEEPSEEK_API_KEY; never "
                         "affects gate verdicts — results land in the validation report)")
    ap.add_argument("--evidence", action="store_true",
                    help="Use evidence-backed candidate resolution instead of first-hit cascade.")
    ap.add_argument("--use-llm-labeling", action="store_true",
                    help="Allow LLM-backed candidates for evidence extraction gaps.")
    ap.add_argument("--confidence-threshold", type=float, default=0.75,
                    help="Minimum confidence for evidence-backed values (default: 0.75).")
    ap.add_argument("--diagnostics", metavar="FILE",
                    help="Write evidence extraction missing/low-confidence diagnostics JSON.")
    ap.add_argument("--candidates", metavar="FILE",
                    help="Write all evidence extraction candidates CSV.")
    args = ap.parse_args()

    if args.list_metrics:
        from src.model.financial_model import list_metrics
        list_metrics()
        return

    # Resolve source
    source = args.url or args.dir or args.source

    # Resolve metric filter
    metrics_filter: list[str] | None = None
    if args.metrics:
        metrics_filter = [k.strip() for k in args.metrics.split(",")]
    if args.section:
        cfg_path = _resolve_config(args.config)
        defs = _BASE_METRICS
        if cfg_path:
            defs = apply_config(_BASE_METRICS, load_config(cfg_path))
        sec_keys = [m.key for m in defs if m.section == args.section]
        if metrics_filter is None:
            metrics_filter = sec_keys
        else:
            metrics_filter = [k for k in metrics_filter if k in sec_keys]

    run(
        source,
        metrics=metrics_filter,
        config=args.config,
        output_csv=Path(args.csv) if args.csv else None,
        output_dir=Path(args.out),
        do_validate=not args.no_validate,
        long_format=args.long,
        fmt=args.fmt,
        period_filter=args.filter,
        max_reports=args.max,
        use_llm=args.llm,
        use_evidence=args.evidence,
        use_llm_labeling=args.use_llm_labeling,
        crosscheck=args.crosscheck,
        confidence_threshold=args.confidence_threshold,
        diagnostics_path=Path(args.diagnostics) if args.diagnostics else None,
        candidates_path=Path(args.candidates) if args.candidates else None,
    )


if __name__ == "__main__":
    main()
