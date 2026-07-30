#!/usr/bin/env python3
"""
build_segments.py — one command, one markdown input, one Segments workbook.

Replaces the old Streamlit dashboard. You write a single, regularized markdown
file describing the company, its Investor-Relations page, and the metrics you
want, then run::

    python3 scripts/build_segments.py inputs/company.md

Clean input/output scheme:
  * INPUT  — the markdown spec lives in ``inputs/<company>.md``.
  * OPEN   — the newest analyst-ready workbook is always the only file in
             ``outputs/latest/``::

        outputs/latest/
            <Company>.xlsx       # open this

  * REVIEW — supporting build artifacts stay in ``outputs/<Company>/``::

        outputs/<Company>/
            csv/                # <slug>_metrics.csv — extracted metric values
            excel/              # retained company-specific workbook copy
            validation/         # <slug>_validation.md — confidence + identity +
                                #   ground-truth report

Raw <period>.pdf downloads and <period>.md parses stay in the durable cache at
``data/reports/<slug>/`` (the single source of truth) and are never duplicated
into the output tree.

─────────────────────────────────────────────────────────────────────────────
Markdown input format — the metrics list is a lightweight outline
─────────────────────────────────────────────────────────────────────────────
    # Walmex
    IR: https://www.walmex.mx/en/financial-information/quarterly.html

    ## Total Income
    - Total Income {revenue}
    - YoY

    ## Profitability
    - Gross Income {gross_profit}
    - Margin
    - bps change
    - EBITDA {ebitda}
    - Margin
    - Net Income {net_income}
    - Margin

The fields are distinguished by structure:
  * Company name = first level-1 heading (``# ...``).
  * IR link      = first ``http(s)://`` URL found anywhere in the file.
  * ``## Heading`` = a section header in the workbook.
  * ``- Label``    = a data row. Its metric key is auto-resolved when the name is
                     unambiguous (exact / alias match); ambiguous names stay blank.
  * ``- Label {metric_key}`` = a data row with a PINNED key — authoritative, this
                     is how you fix any mis-mapping (e.g. ``Total Income {revenue}``).
  * ``- YoY`` / ``- Margin`` / ``- As % of Total`` / ``- bps change`` / ``- Check``
    / ``- 2-year comp`` = derived rows, rendered as live Excel formulas.

The whole list runs through the full pipeline (download → parse → extract → Excel)
via the existing outline machinery (``parse_outline`` → ``pipeline.run`` →
``build_outline_workbook``). Rows the cascade can't fill render as graceful blanks;
a resolution report prints exactly what mapped vs. what was left blank.

Extraction is generic by default. If ``configs/<slug>.yaml`` already exists for
the company (slug = lowercased name) it is reused automatically for its tuned
download patterns and extraction overrides — and if ``data/reports/<slug>/`` holds
already-parsed reports, those are reused and download/parse are skipped.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

# Make the project root importable when run as ``python3 scripts/build_segments.py``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.shared.paths import (  # noqa: E402
    PROJECT_ROOT, CONFIGS_DIR, REPORTS_DIR, SHARED_REPORTS_DIR, OUTPUTS_DIR,
    LATEST_OUTPUT_DIR,
)
from src.shared.report_index import (  # noqa: E402
    index_report_files,
    infer_period_label,
    period_sort_key,
)

_URL_RE = re.compile(r"https?://\S+")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+?)\s*$")


# ---------------------------------------------------------------------------
# Markdown parsing
# ---------------------------------------------------------------------------

class InputError(Exception):
    """The markdown input was missing a required field."""


def _display_path(path: Path) -> str:
    """Readable project-relative path, while supporting configured external estate roots."""
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def parse_input(md_text: str) -> tuple[str, str, list[str]]:
    """Pull ``(company_name, ir_url, metrics)`` out of the regularized markdown.

    Raises ``InputError`` if the company name, IR link, or metric list is absent.
    """
    name: str | None = None
    ir_url: str | None = None
    metrics: list[str] = []

    for raw in md_text.splitlines():
        line = raw.rstrip()
        if name is None:
            m = _H1_RE.match(line)
            if m:
                name = m.group(1).strip()
                continue
        if ir_url is None:
            m = _URL_RE.search(line)
            if m:
                ir_url = m.group(0).rstrip(").,;>")
                # A URL that is itself a list item is the link, not a metric.
                continue
        m = _LIST_RE.match(line)
        if m:
            item = m.group(1).strip()
            if _URL_RE.search(item):
                continue
            if item:
                metrics.append(item)

    if not name:
        raise InputError("no company name found — add a '# Company Name' heading.")
    if not ir_url:
        raise InputError("no IR link found — add a line with an http(s):// URL.")
    if not metrics:
        raise InputError("no metrics found — add a markdown list of metric names.")
    return name, ir_url, metrics


# ── Outline compiler ─────────────────────────────────────────────────────────
# The metrics list is a lightweight outline that maps onto segments_sheet.parse_outline:
#   `## Heading`          → section
#   `- Label`             → data row (key auto-resolved if unambiguous)
#   `- Label {metric_key}`→ data row with a pinned key (authoritative)
#   `- YoY` / `- Margin` / `- As % of Total` / `- bps change` / `- Check` / `- 2-year comp`
#                         → derived rows (rendered as Excel formulas / placeholders)

_HEADING_RE = re.compile(r"^#{2,}\s+(.+?)\s*$")
_PIN_RE = re.compile(r"\{([A-Za-z0-9_]+)\}\s*$")
_AUTO_CONFIDENCE = 0.9  # only auto-map exact/alias hits; ambiguous labels stay blank


def _split_pin(item: str) -> tuple[str, str | None]:
    """Split a trailing ``{metric_key}`` pin off a bullet: 'X {k}' → ('X', 'k')."""
    m = _PIN_RE.search(item)
    if m:
        return item[: m.start()].strip(), m.group(1)
    return item.strip(), None


def _derived_token(label: str) -> str | None:
    """Return the canonical derived-row token parse_outline recognizes, or None.

    Only clean, standalone derived phrasings convert; anything with extra words
    (e.g. 'SSS YoY') stays a data row so it isn't mistaken for a derived formula.
    """
    norm = re.sub(r"\s+", " ", label.strip().lower())
    if norm in ("yoy", "yoy %", "yoy%", "% yoy", "yoy growth", "yoy change"):
        return "YoY"
    if norm in ("2-year comp", "2 year comp", "2yr comp", "2-yr comp", "two-year comp"):
        return "2-year comp"
    if norm == "bps change" or norm in ("bps", "bps delta", "bps δ", "bps chg") \
            or norm.startswith("bps "):
        return "bps change"
    if norm == "check" or norm.startswith("check:") or norm.startswith("check "):
        return "Check"
    if norm.startswith(("as % of total", "% of total", "as percent of total", "percent of total")):
        return "As % of Total"
    if norm.startswith(("as % of", "% of")):
        return "As % of"
    if norm in ("margin", "+ margin", "margin %"):
        return "Margin"
    if norm.endswith(" margin"):
        return label.strip()          # e.g. 'EBITDA Margin' — parse_outline handles it
    if norm == "fx effect":
        return "fx effect"
    # Productivity / ratio calculations rendered as live cross-row formulas.
    if norm in ("effective tax rate", "avg store size", "average store size",
                "sales per m²", "sales per m2", "sales per store",
                "capex per store", "capex per m²", "capex per m2"):
        return label.strip()
    if norm.startswith("yoy "):     # "YoY Sales per m²" etc. (bare "yoy" handled above)
        return label.strip()
    return None


def compile_outline(md_text: str, resolver, valid_keys: set[str]) -> tuple[str, list[str], dict, list[tuple]]:
    """Turn the markdown body into (outline_text, sections, mapping, report).

    Skips the H1 title and the IR-link line. ``report`` rows are
    ``(label, kind, detail)`` where kind ∈ {section, derived, pinned, auto, unmapped, bad-pin}.
    """
    out_lines: list[str] = []
    sections: list[str] = []
    mapping: dict[str, str] = {}
    report: list[tuple] = []

    name_seen = False
    url_seen = False
    cur_section = ""
    for raw in md_text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not name_seen and _H1_RE.match(line):
            name_seen = True
            continue
        if not url_seen and _URL_RE.search(line) and not _LIST_RE.match(line):
            url_seen = True
            continue
        if not stripped:
            out_lines.append("")
            continue
        m = _HEADING_RE.match(stripped)
        if m:
            sec = m.group(1).strip()
            sections.append(sec)
            cur_section = sec
            out_lines.append("")
            out_lines.append(sec)
            report.append((sec, "section", ""))
            continue
        m = _LIST_RE.match(line)
        if not m:
            continue  # prose / unrecognized line — ignored
        label, pin = _split_pin(m.group(1).strip())
        if not label:
            continue
        dt = _derived_token(label)
        if dt is not None:
            out_lines.append(dt)
            report.append((label, "derived", dt))
            continue
        # Section-qualify the mapping key so a section heading that shares a label
        # with a data row (e.g. "Total Income") is not swallowed as a data row.
        mkey = f"{cur_section}/{label}" if cur_section else label
        if pin:
            if pin in valid_keys:
                mapping[mkey] = pin
                report.append((label, "pinned", pin))
            else:
                report.append((label, "bad-pin", pin))   # unknown key → blank row
        else:
            r = resolver.resolve(label)
            if r and r.keys and r.confidence >= _AUTO_CONFIDENCE and r.keys[0] in valid_keys:
                mapping[mkey] = r.keys[0]
                report.append((label, "auto", f"{r.keys[0]} ({r.confidence:.2f})"))
            else:
                guess = r.keys[0] if (r and r.keys) else None
                report.append((label, "unmapped", guess))   # blank row; pin to fill
        out_lines.append(label)

    return "\n".join(out_lines), sections, mapping, report


def _print_resolution_report(report: list[tuple]) -> None:
    """Show what each metric label mapped to, so mis-maps and blanks are visible."""
    data = [r for r in report if r[1] != "section"]
    filled = [r for r in data if r[1] in ("pinned", "auto")]
    print(f"\n  Metric mapping — {len(filled)}/{len(data)} data row(s) mapped to a key")
    print("  " + "─" * 64)
    for label, kind, detail in data:
        if kind == "pinned":
            mark, note = "✓", f"{detail}  (pinned)"
        elif kind == "auto":
            mark, note = "✓", f"{detail}"
        elif kind == "derived":
            mark, note = "·", f"derived → {detail}"
        elif kind == "bad-pin":
            mark, note = "✗", f"unknown key '{detail}' → blank row"
        else:
            hint = f" (fuzzy guess: {detail}; pin with {{key}} to fill)" if detail else ""
            mark, note = "○", f"unmapped → blank row{hint}"
        print(f"   {mark} {label:<42} {note}")
    print()


def slugify(name: str) -> str:
    """Lowercase config slug: 'Grupo Bimbo' → 'grupo_bimbo'."""
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "company"


def dirname_for(name: str) -> str:
    """Filesystem-safe, human-readable directory name from the company name."""
    s = re.sub(r"[^\w.-]+", "_", name.strip()).strip("_")
    return s or "Company"


def publish_latest_workbook(source: Path, latest_dir: Path = LATEST_OUTPUT_DIR) -> Path:
    """Replace the analyst handoff with exactly one freshly built workbook.

    The new directory is staged beside ``latest_dir`` and swapped in only after
    the workbook copy succeeds. A failed copy therefore leaves the prior analyst
    handoff intact.
    """
    source = Path(source)
    latest_dir = Path(latest_dir)
    if not source.is_file():
        raise FileNotFoundError(f"workbook not found: {source}")
    if source.suffix.lower() != ".xlsx":
        raise ValueError(f"latest deliverable must be an .xlsx workbook: {source}")

    parent = latest_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging_dir = Path(tempfile.mkdtemp(prefix=f".{latest_dir.name}-staging-", dir=parent))
    staged_workbook = staging_dir / source.name
    backup_dir: Path | None = None

    try:
        shutil.copy2(source, staged_workbook)
        if latest_dir.exists():
            if not latest_dir.is_dir():
                raise NotADirectoryError(f"latest deliverable path is not a directory: {latest_dir}")
            backup_dir = Path(tempfile.mkdtemp(
                prefix=f".{latest_dir.name}-previous-", dir=parent,
            ))
            backup_dir.rmdir()
            latest_dir.replace(backup_dir)
        try:
            staging_dir.replace(latest_dir)
        except Exception:
            if backup_dir is not None and backup_dir.exists() and not latest_dir.exists():
                backup_dir.replace(latest_dir)
            raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)

    if backup_dir is not None:
        shutil.rmtree(backup_dir, ignore_errors=True)
    return latest_dir / source.name


# ---------------------------------------------------------------------------
# Phase orchestration
# ---------------------------------------------------------------------------

def _ir_options(config_path: Path | None) -> dict:
    """Read IR download tuning from a tuned config's ``ir_website`` block, if any."""
    opts: dict = {}
    if not config_path or not config_path.exists():
        return opts
    import yaml

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    ir = cfg.get("ir_website", {}) or {}
    if ir.get("url"):
        opts["url"] = ir["url"]
    if ir.get("pdf_link_pattern"):
        opts["file_pattern"] = ir["pdf_link_pattern"]
    if ir.get("year_api_urls"):
        opts["year_api_urls"] = ir["year_api_urls"]
    if ir.get("use_playwright") is not None:
        opts["use_playwright"] = bool(ir["use_playwright"])
    if ir.get("impersonate"):
        opts["impersonate"] = ir["impersonate"]
    if ir.get("delay_ms") is not None:
        opts["delay_ms"] = int(ir["delay_ms"])
    if ir.get("max_reports") is not None:
        opts["max_reports"] = int(ir["max_reports"])
    return opts


def _company_ticker(config_path: Path | None) -> str:
    if not config_path or not config_path.exists():
        return ""
    import yaml

    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return str((cfg.get("company") or {}).get("ticker") or "")


def _units_label(config_path: Path | None) -> str:
    """Header units string, e.g. 'MXN mn', derived from the config; default 'P$mn'."""
    if not config_path or not config_path.exists():
        return "P$mn"
    import yaml

    co = (yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}).get("company") or {}
    currency = str(co.get("currency") or "").strip()
    abbr = {"millions": "mn", "thousands": "k", "miles_mxn": "k"}.get(
        str(co.get("unit") or "").strip(), "mn")
    return f"{currency} {abbr}".strip() if currency else "P$mn"


def download_reports(ir_url: str, downloads_dir: Path, ir_opts: dict,
                     max_reports: int) -> list[str]:
    """Download PDFs from the IR page and canonicalize them to ``<period>.pdf``.

    Returns the sorted list of canonical period labels saved into ``downloads_dir``.
    """
    from src.download.downloader import download_from_ir

    staging = downloads_dir / ".staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)

    url = ir_opts.get("url") or ir_url
    download_from_ir(
        url=url,
        output_dir=staging,
        max_reports=ir_opts.get("max_reports", max_reports),
        file_pattern=ir_opts.get("file_pattern"),
        delay_ms=ir_opts.get("delay_ms", 500),
        year_api_urls=ir_opts.get("year_api_urls"),
        use_playwright=ir_opts.get("use_playwright"),
        impersonate=ir_opts.get("impersonate"),
    )

    saved = _canonicalize(list(staging.glob("**/*.pdf")), downloads_dir)
    shutil.rmtree(staging, ignore_errors=True)
    return saved


def _canonicalize(staged: list[Path], out_dir: Path) -> list[str]:
    """Copy staged native-name PDFs into ``out_dir`` as ``<period>.pdf``.

    When several files map to the same period, ``index_report_files`` picks the
    preferred one via its report-type precedence. Idempotent.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    groups = index_report_files(staged)
    for period in sorted(groups, key=period_sort_key):
        target = out_dir / f"{period}.pdf"
        src = groups[period].selected_path
        if src is None or src.suffix.lower() != ".pdf":
            continue
        if not target.exists():
            target.write_bytes(src.read_bytes())
        saved.append(period)
    return saved


def parse_reports(downloads_dir: Path, parses_dir: Path) -> list[str]:
    """Parse each canonical PDF in ``downloads_dir`` into ``parses_dir/<period>.md``."""
    from src.parse.parse_pdf import parse_pdf

    parses_dir.mkdir(parents=True, exist_ok=True)
    pdfs = [p for p in downloads_dir.glob("*.pdf")
            if infer_period_label(p.stem) is not None]
    parsed: list[str] = []
    for pdf in sorted(pdfs, key=lambda p: period_sort_key(infer_period_label(p.stem) or p.stem)):
        md_dest = parses_dir / f"{pdf.stem}.md"
        if md_dest.exists():
            parsed.append(pdf.stem)
            continue
        try:
            md, _blocks = parse_pdf(pdf)
        except Exception as exc:  # noqa: BLE001 — keep parsing the rest
            print(f"  WARN parse {pdf.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        md_dest.write_text(md, encoding="utf-8")
        parsed.append(pdf.stem)
    return parsed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _report_cache(slug: str, *, force_download: bool = False) -> Path:
    """Prefer the monorepo-wide zero-copy estate view when it has parsed reports.

    The shared view is STRICTLY read-only from this pipeline's perspective — it is
    regenerated from the estate catalog by a separate process, and writing into it
    would race that builder. Fresh downloads and parse products always land in the
    root project's writable cache (``data/reports/<slug>``); the estate picks them
    up on its next rebuild.
    """
    shared = SHARED_REPORTS_DIR / slug
    if not force_download and shared.is_dir() and any(shared.glob("*.md")):
        return shared
    return REPORTS_DIR / slug


def run(md_path: Path, *, max_reports: int = 100, force_download: bool = False) -> Path:
    """Run the full pipeline and return the analyst-facing latest .xlsx path.

    If ``data/reports/<slug>`` already holds parsed ``.md`` reports, those are
    reused and the download/parse phases are skipped (unless ``force_download``).
    """
    md_text = md_path.read_text(encoding="utf-8")
    name, ir_url, _flat = parse_input(md_text)        # validates name + IR link present
    slug = slugify(name)

    # Deliverables live under outputs/<Company>/; raw PDFs + parsed markdown stay
    # in the data/reports/<slug> cache (single source of truth, never duplicated).
    out_root = OUTPUTS_DIR / dirname_for(name)
    csv_dir = out_root / "csv"
    excel_dir = out_root / "excel"
    validation_dir = out_root / "validation"
    for d in (csv_dir, excel_dir, validation_dir):
        d.mkdir(parents=True, exist_ok=True)

    cache = _report_cache(slug, force_download=force_download)
    writable_cache = REPORTS_DIR / slug
    if cache == writable_cache:
        cache.mkdir(parents=True, exist_ok=True)
    # else: cache is the estate view — read-only; never mkdir/write there.

    config_path = CONFIGS_DIR / f"{slug}.yaml"
    config_arg = str(config_path) if config_path.exists() else None

    # Compile the metrics outline and resolve each data label to a metric key.
    from src.excel.segments_sheet import load_metric_defs
    from src.extract.interface import MetricResolver

    defs = load_metric_defs(config_arg)
    resolver = MetricResolver(defs)
    valid_keys = {m.key for m in defs}
    outline_text, sections, mapping, report = compile_outline(md_text, resolver, valid_keys)

    print(f"Company : {name}  (slug: {slug})")
    print(f"Config  : {'configs/' + config_path.name + ' (reused)' if config_arg else 'generic (no tuned config)'}")
    print(f"Output  : {out_root}")
    print(f"Outline : {len(sections)} section(s), {sum(1 for r in report if r[1] != 'section')} metric row(s)")
    _print_resolution_report(report)

    reuse = (not force_download
             and cache.is_dir()
             and any(cache.glob("*.md")))

    if reuse:
        # ── Phases 1–2 · Reuse already-downloaded + parsed reports ───────────
        print(f"\n[1-2/5] Reusing cached reports in {_display_path(cache)} "
              "— skipping download/parse.")
        parsed = [md.stem for md in sorted(cache.glob("*.md"))
                  if infer_period_label(md.stem) is not None]
        print(f"        {len(parsed)} parsed report(s) available")
    else:
        # ── Phase 1 · Download (always into the WRITABLE cache, never the
        # estate view — the view is read-only and owned by the estate builder) ─
        cache = writable_cache
        cache.mkdir(parents=True, exist_ok=True)
        print("\n[1/5] Downloading reports from IR page…")
        ir_opts = _ir_options(config_path if config_arg else None)
        periods = download_reports(ir_url, cache, ir_opts, max_reports)
        print(f"      {len(periods)} report(s) in {_display_path(cache)}")
        if not periods:
            print(f"      No PDFs downloaded. The IR page may render links via JavaScript; "
                  f"drop PDFs into {_display_path(cache)} manually and re-run.",
                  file=sys.stderr)

        # ── Phase 2 · Parse (siblings in the cache) ──────────────────────────
        print("[2/5] Parsing PDFs → markdown…")
        parsed = parse_reports(cache, cache)
        print(f"      {len(parsed)} report(s) parsed")

    if not parsed:
        print("      Nothing to extract — aborting.", file=sys.stderr)
        raise SystemExit(1)

    # ── Phase 3 · Extract (only the mapped keys, through the full cascade) ─────
    print("[3/5] Extracting metrics…")
    from src.excel.segments_sheet import (
        build_outline_workbook,
        parse_outline,
        segments_title,
        _unit_map,
    )
    from src.extract import pipeline

    rows = parse_outline(outline_text, sections, mapping)
    keys = sorted({r.key for r in rows if r.key})
    csv_path = csv_dir / f"{slug}_metrics.csv"
    df = pipeline.run(
        cache,
        metrics=keys,
        config=config_arg,
        output_csv=csv_path,
        verbose=False,
    )
    filled = [k for k in keys if k in getattr(df, "columns", [])]
    print(f"      CSV → {_display_path(csv_path)}  "
          f"({len(df)} period(s); {len(filled)}/{len(keys)} mapped key(s) extracted)")

    # ── Phase 4 · Excel (outline mode: sections + derived rows as formulas) ────
    print("[4/5] Building Segments workbook…")
    from src.excel.segments_sheet import augment_outline_checks
    from src.model.financial_model import load_config as _load_cfg
    title = segments_title(name, _company_ticker(config_path if config_arg else None))
    units = _units_label(config_path if config_arg else None)
    auto_checks = bool((_load_cfg(config_arg) if config_arg else {}).get("auto_checks", True))
    if auto_checks:
        _, n_auto = augment_outline_checks(rows, df)
        if n_auto:
            print(f"      {n_auto} auto check row(s) inserted (segment-sum / identity)")
    wb = build_outline_workbook(title, rows, df, units=units, unit_map=_unit_map(config_arg),
                                auto_checks=auto_checks)
    xlsx_path = excel_dir / f"{dirname_for(name)}.xlsx"
    wb.save(xlsx_path)

    # ── Phase 5 · Validation report (confidence + identities + ground truth) ──
    print("[5/5] Writing validation report…")
    from src.eval.validation_report import write_validation_report
    report_path = validation_dir / f"{slug}_validation.md"
    write_validation_report(df, keys, name=name, slug=slug, out_path=report_path)
    print(f"      Report → {_display_path(report_path)}")

    # ── Phase 6 · Verification gate (strength scorecard + worklist + Excel audit) ─
    import json
    from src.eval.verification_gate import score_metrics, scorecard_markdown
    from src.excel.segments_sheet import audit_outline
    from src.model.financial_model import load_config
    cfg_dict = load_config(config_arg) if config_arg else {}
    expectations = (cfg_dict or {}).get("metric_expectations") or {}
    scores, is_strong, worklist = score_metrics(df, keys, expectations=expectations)
    # Excel audit: the workbook must == the deliverable — no blank rows, every
    # derived row a live formula. Any unreachable row the outline still lists is an
    # audit failure (remove it from inputs/<slug>.md) and blocks STRONG.
    excel_issues = audit_outline(rows, df)
    if excel_issues:
        is_strong = False
        for it in excel_issues:
            worklist.append({"key": "(excel)", "period": it["kind"], "value": None,
                             "reason": f"EXCEL AUDIT — drop from outline: {it['label']} "
                                       f"({it['reason']})", "priority": 0})
    worklist_path = validation_dir / f"{slug}_worklist.json"
    worklist_path.write_text(json.dumps(worklist, indent=2), encoding="utf-8")
    card = scorecard_markdown(scores, is_strong)
    print("[6/6] Verification gate:")
    print("      " + card.splitlines()[0])
    if excel_issues:
        print(f"      ⚠ EXCEL AUDIT: {len(excel_issues)} unreachable row(s) still in the "
              f"outline — remove them from inputs/{slug}.md:")
        for it in excel_issues[:20]:
            print(f"          • {it['label']}  ({it['reason']})")
    print(f"      Worklist ({len(worklist)} item(s)) → {_display_path(worklist_path)}")
    # Append the scorecard + audit to the validation report so they travel with the deliverable.
    with report_path.open("a", encoding="utf-8") as fh:
        fh.write("\n## 5. Verification-gate scorecard\n\n" + card + "\n")
        if excel_issues:
            fh.write("\n## 6. Excel audit — rows to remove from the outline\n\n")
            for it in excel_issues:
                fh.write(f"- **{it['label']}** ({it['kind']}) — {it['reason']}\n")

    latest_path = publish_latest_workbook(xlsx_path)

    print(f"\n✓ Done — {len(keys)} mapped metric(s) over {len(df)} period(s)  "
          f"[{'STRONG' if is_strong else 'NEEDS VERIFICATION'}]")
    print(f"  OPEN THIS → {_display_path(latest_path)}")
    print(f"  Review files → {_display_path(out_root)}/  (excel/ · csv/ · validation/)")
    return latest_path


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="build_segments.py",
        description="Build a company's Segments workbook from a single markdown file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("input", help="Path to the regularized company markdown file.")
    ap.add_argument("--max-reports", type=int, default=100,
                    help="Cap on PDFs to download from the IR page (default: 100).")
    ap.add_argument("--force-download", action="store_true",
                    help="Download fresh even if data/reports/<slug> already has parsed reports.")
    args = ap.parse_args()

    md_path = Path(args.input).expanduser()
    if not md_path.exists():
        ap.error(f"input file not found: {md_path}")

    try:
        run(md_path, max_reports=args.max_reports, force_download=args.force_download)
    except InputError as exc:
        ap.error(f"bad markdown input: {exc}")


if __name__ == "__main__":
    main()
