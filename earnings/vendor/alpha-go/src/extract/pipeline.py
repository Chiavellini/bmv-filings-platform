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
from pathlib import Path

from src.model.financial_model import METRICS as _BASE_METRICS, apply_config, load_config
from src.extract.extract_metrics import MetricRow, extract_metrics, extract_metrics_segmented, display_metrics, parse_number
from src.extract.tiered_extract import PeriodSource, extract_metrics_tiered, period_end_from_label
from src.shared.report_index import index_report_files, infer_period_label, period_sort_key
from src.shared.validator import validate, score_confidence, validation_summary, flagged_metrics


# ---------------------------------------------------------------------------
# Core pipeline function
# ---------------------------------------------------------------------------

def run(
    source: str | Path,
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

    # Resolve source → list of markdown texts with period labels
    docs = _resolve_source(
        source,
        output_dir=output_dir,
        period_filter=period_filter,
        max_reports=max_reports,
        cfg_path=cfg_path,
    )

    if not docs:
        print("No documents to process.", file=sys.stderr)
        return pd.DataFrame()

    # Extract metrics from each document
    all_rows: list[dict] = []
    all_long_rows: list[dict] = []
    evidence_results: list = []
    use_evidence = use_evidence or diagnostics_path is not None or candidates_path is not None

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
            extracted = evidence_result.accepted
        else:
            extracted = extract_metrics_tiered(src, metric_defs, cfg, use_llm=use_llm)

        val_results: list = []
        conf_scores: dict[str, float] = {}
        flagged: set = set()
        if do_validate:
            val_results = validate(extracted)
            conf_scores = score_confidence(extracted, val_results)
            flagged = flagged_metrics(val_results)

        # Filter to requested metrics
        if metrics:
            extracted = {k: v for k, v in extracted.items() if k in metrics}

        # Wide row
        wide_row: dict = {"period": period}
        for key, row in extracted.items():
            wide_row[key] = row.current
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
        return pd.DataFrame()

    # Build ordered wide DataFrame
    ordered_keys = [m.key for m in metric_defs]
    df = pd.DataFrame(all_rows).sort_values("period").reset_index(drop=True)
    present_cols = ["period"] + [k for k in ordered_keys if k in df.columns]
    # Dynamic segment keys (e.g. revenue_mexico, ebitda_cam) are created at
    # extraction time and are not in the static registry; keep any that were
    # explicitly requested so they survive into the wide output.
    if metrics:
        present_cols += [k for k in metrics if k in df.columns and k not in present_cols]
    df = df[[c for c in present_cols if c in df.columns]]

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

def _resolve_source(
    source: str | Path,
    output_dir: Path,
    period_filter: str | None,
    max_reports: int,
    cfg_path: Path | None,
) -> dict[str, PeriodSource]:
    """Return dict mapping period_label → PeriodSource (text + optional facts/pdf)."""
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


def _from_url(
    url: str,
    output_dir: Path,
    period_filter: str | None,
    max_reports: int,
    cfg_path: Path | None,
) -> dict[str, str]:
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
    xbrl_docs: dict[str, str] = {}
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
            )
        except Exception as exc:
            print(f"WARN parse {path.name}: {exc}", file=sys.stderr)
    return docs


def _load_facts(json_path: Path) -> dict | None:
    """Read a filing's flat numeric facts (regenerating the artifact if missing)."""
    import json
    facts_path = json_path.with_name(json_path.stem + "_facts.json")
    if not facts_path.exists():
        try:
            from src.download.bmv_xbrl import extract_artifacts
            extract_artifacts(json_path)
        except Exception as exc:
            print(f"WARN xbrl facts {json_path.name}: {exc}", file=sys.stderr)
            return None
    if not facts_path.exists():
        return None
    try:
        return json.loads(facts_path.read_text(encoding="utf-8")).get("facts") or None
    except Exception:
        return None


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

    docs: dict[str, PeriodSource] = {}
    for path in json_paths:
        period = path.stem.split("_", 1)[-1]  # SPORT_2026-1T → 2026-1T
        if period_filter and period_filter not in period:
            continue
        try:
            text = load_mdna_text(path) or ""
        except Exception as exc:
            print(f"WARN xbrl mdna {path.name}: {exc}", file=sys.stderr)
            text = ""
        facts = _load_facts(path)
        if text or facts:
            docs[period] = PeriodSource(
                period=period, text=text, facts=facts,
                period_end=period_end_from_label(period),
            )
    if docs:
        print(f"BMV XBRL: {len(docs)} period(s) covered for {ticker}.", file=sys.stderr)
    return docs


def _from_single_file(path: Path) -> dict[str, PeriodSource]:
    period = infer_period_label(path.stem) or path.stem
    if path.suffix.lower() == ".pdf":
        from src.parse.parse_pdf import parse_pdf
        md_text, _, doc = parse_pdf(str(path), with_meta=True)
        return {period: PeriodSource(period=period, text=md_text, pdf_path=path,
                                     period_end=period_end_from_label(period), doc=doc)}
    else:
        return {period: PeriodSource(period=period, text=path.read_text(encoding="utf-8"),
                                     period_end=period_end_from_label(period))}


def _from_directory(directory: Path) -> dict[str, PeriodSource]:
    from src.parse.parse_pdf import parse_pdf

    docs: dict[str, PeriodSource] = {}
    indexed = index_report_files(directory.iterdir())
    for period, group in sorted(indexed.items(), key=lambda item: period_sort_key(item[0])):
        path = group.selected_path
        if path is None:
            continue
        try:
            if path.suffix.lower() == ".md":
                docs[period] = PeriodSource(
                    period=period, text=path.read_text(encoding="utf-8"),
                    period_end=period_end_from_label(period),
                )
            else:
                md_text, _, doc = parse_pdf(str(path), with_meta=True)
                docs[period] = PeriodSource(
                    period=period, text=md_text, pdf_path=path,
                    period_end=period_end_from_label(period), doc=doc,
                )
        except Exception as exc:
            print(f"WARN parse {path.name}: {exc}", file=sys.stderr)

    return docs


def _resolve_config(config: str | Path | None) -> Path | None:
    if config:
        p = Path(config)
        if not p.exists():
            print(f"Config not found: {p}", file=sys.stderr)
            return None
        return p
    # Auto-detect
    from src.shared.paths import CONFIGS_DIR
    for name in ("sport.yaml", "generic.yaml"):
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
        confidence_threshold=args.confidence_threshold,
        diagnostics_path=Path(args.diagnostics) if args.diagnostics else None,
        candidates_path=Path(args.candidates) if args.candidates else None,
    )


if __name__ == "__main__":
    main()
