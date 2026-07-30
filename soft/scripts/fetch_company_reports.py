#!/usr/bin/env python3
"""fetch_company_reports.py — fetch a company's quarterly report PDFs and save them
under CANONICAL period filenames (``YYYY-NT.pdf``) in ``data/reports/<company>/``.

The scorer (``scripts/pipeline_scorecard.py`` via ``compare_extractions.parse_period``)
only reads canonical ``YYYY-NT.pdf`` files, so this script's job is to (1) discover
every quarterly report a company has published from 2016 onward and (2) save each as
``<period>.pdf``.

Discovery delegates to the full engine rather than re-implementing crawling:
  1. ``src.download.downloader.download_from_ir`` — the 6-layer IR-page engine
     (year-API, archive JSON, Next.js, year-variant pages, ASP.NET filters,
     Playwright, paginated crawl). Native filenames are normalized to canonical
     periods via ``report_index.infer_period_label`` + ``index_report_files``.
  2. ``src.download.bmv_xbrl.download_ticker`` — BMV XBRL archive (~2021+) for
     ``_facts.json`` artifacts (aids extraction; does not itself create PDFs).
  3. ``src.download.wayback`` — last-resort recovery of periods that have rolled
     off the live IR page, from the Internet Archive.

A hard 2016 floor is enforced: periods earlier than ``--floor-year`` (default 2016)
are never saved. Companies that IPO'd after 2016 simply have no earlier reports to
find, so the single floor guard satisfies "start at 2016, or earliest available".

Usage: python3 scripts/fetch_company_reports.py <company> [--max N] [--floor-year Y]
                                                 [--force-refresh] [--parse] [--jsonl]
                                                 [--no-xbrl] [--no-wayback]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import warnings
from pathlib import Path

import yaml

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.download.downloader import download_from_ir  # noqa: E402
from src.shared.report_index import (  # noqa: E402
    index_report_files,
    infer_period_label,
    period_sort_key,
)

START_FLOOR_YEAR = 2016
REFRESH_SUFFIXES = {".pdf", ".md", ".jsonl"}


def period_from_url(url: str) -> str | None:
    """Map a report URL/filename to a canonical 'YYYY-NT' period label, or None.

    Thin shim over the shared, battle-tested normalizer in ``report_index`` so this
    script no longer maintains its own (narrower) regex table.
    """
    from urllib.parse import urlparse

    return infer_period_label(Path(urlparse(url).path).stem)


def _before_floor(period: str, floor_year: int) -> bool:
    return period_sort_key(period)[0] < floor_year


def _canonicalize_into(
    staged: list[Path], out_dir: Path, floor_year: int
) -> list[str]:
    """Rename staged native-name PDFs into ``out_dir`` as ``<period>.pdf``.

    Periods before ``floor_year`` are skipped. When several staged files map to the
    same period (e.g. preliminary vs ``Dictaminado``), ``index_report_files`` picks
    the preferred one via its report-type precedence. Idempotent: an existing
    canonical target is left untouched.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    groups = index_report_files(staged)
    for period in sorted(groups, key=period_sort_key):
        if _before_floor(period, floor_year):
            continue
        target = out_dir / f"{period}.pdf"
        if target.exists():
            continue
        src = groups[period].selected_path
        if src is None or src.suffix.lower() != ".pdf":
            continue
        target.write_bytes(src.read_bytes())
        saved.append(period)
        print(f"  saved {period}.pdf  ({target.stat().st_size // 1024} KB)")
    return saved


def _generated_report_artifacts(report_dir: Path) -> list[Path]:
    """Generated artifacts that a report refresh may replace.

    Facts/XBRL JSON files are deliberately excluded; they are supplemental inputs,
    not parse products from the PDF downloader/parser phase.
    """
    if not report_dir.exists():
        return []
    return sorted(
        p
        for p in report_dir.iterdir()
        if p.is_file() and p.suffix.lower() in REFRESH_SUFFIXES
    )


def _clear_generated_report_artifacts(report_dir: Path) -> int:
    removed = 0
    for path in _generated_report_artifacts(report_dir):
        path.unlink()
        removed += 1
    return removed


def _copy_generated_artifacts(staged_dir: Path, out_dir: Path, *, overwrite: bool) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for src in sorted(staged_dir.iterdir()):
        if not src.is_file() or src.suffix.lower() not in REFRESH_SUFFIXES:
            continue
        dest = out_dir / src.name
        if dest.exists() and not overwrite:
            continue
        shutil.copy2(src, dest)
        copied += 1
    return copied


def _periods_in_dir(report_dir: Path, floor_year: int) -> set[str]:
    periods = {
        p
        for p in (infer_period_label(f.stem) for f in report_dir.glob("*.pdf"))
        if p and not _before_floor(p, floor_year)
    }
    return periods


def _parse_pdfs(report_dir: Path, *, jsonl: bool = False) -> list[str]:
    """Parse every canonical PDF in ``report_dir`` to sibling ``.md`` files."""
    from src.parse.parse_pdf import parse_pdf

    parsed: list[str] = []
    pdfs = [
        p
        for p in report_dir.glob("*.pdf")
        if infer_period_label(p.stem) is not None
    ]
    for pdf in sorted(pdfs, key=lambda p: period_sort_key(infer_period_label(p.stem) or p.stem)):
        try:
            md, blocks = parse_pdf(pdf)
        except Exception as exc:  # noqa: BLE001 — keep parsing remaining reports
            print(f"WARN parse {pdf.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        md_dest = pdf.with_suffix(".md")
        md_dest.write_text(md, encoding="utf-8")
        if jsonl:
            jsonl_dest = pdf.with_suffix(".jsonl")
            with jsonl_dest.open("w", encoding="utf-8") as fh:
                for block in blocks:
                    fh.write(json.dumps(block, ensure_ascii=False) + "\n")
        parsed.append(pdf.stem)
    return parsed


def _print_missing_summary(company: str, report_dir: Path, floor_year: int) -> None:
    have = _periods_in_dir(report_dir, floor_year)
    missing = sorted(_expected_periods(floor_year) - have, key=period_sort_key)
    if missing:
        print(
            f"{company}: missing {len(missing)} expected period(s) after refresh: "
            + ", ".join(missing),
            file=sys.stderr,
        )


def _expected_periods(floor_year: int) -> set[str]:
    """All canonical periods from the floor year through the current quarter."""
    from datetime import date

    today = date.today()
    last_q = (today.month - 1) // 3  # quarters fully begun; current quarter likely unreported
    periods: set[str] = set()
    for year in range(floor_year, today.year + 1):
        for q in range(1, 5):
            if year == today.year and q > max(last_q, 1):
                continue
            periods.add(f"{year}-{q}T")
    return periods


def fetch(
    company: str,
    *,
    max_reports: int = 120,
    floor_year: int = START_FLOOR_YEAR,
    use_xbrl: bool = True,
    use_wayback: bool = True,
    force_refresh: bool = False,
    parse: bool = False,
    jsonl: bool = False,
    annual_facts: bool = False,
) -> None:
    cfg = yaml.safe_load((ROOT / "configs" / f"{company}.yaml").read_text())
    ir = cfg.get("ir_website", {})
    url = ir["url"]
    out_dir = ROOT / "data" / "reports" / company
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg_max = int(ir.get("max_reports", max_reports))
    max_reports = max(max_reports, cfg_max)

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        raw_dir = run_dir / "raw"
        stage_out = run_dir / "canonical"
        raw_dir.mkdir()
        stage_out.mkdir()

        # 1) Live IR engine — full discovery into a staging dir, then canonicalize.
        kwargs: dict = {"max_reports": max_reports}
        if ir.get("pdf_link_pattern"):
            kwargs["file_pattern"] = ir["pdf_link_pattern"]
        if ir.get("delay_ms") is not None:
            kwargs["delay_ms"] = int(ir["delay_ms"])
        if ir.get("use_playwright") is not None:
            kwargs["use_playwright"] = bool(ir["use_playwright"])
        if ir.get("year_api_urls"):
            kwargs["year_api_urls"] = ir["year_api_urls"]
        if ir.get("browser_first") is not None:
            kwargs["browser_first"] = bool(ir["browser_first"])
        if ir.get("impersonate"):
            kwargs["impersonate"] = str(ir["impersonate"])
        kwargs["floor_year"] = floor_year
        try:
            staged = download_from_ir(url, raw_dir, **kwargs)
        except Exception as exc:  # noqa: BLE001 — IR discovery is best-effort
            print(f"{company}: IR discovery failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            staged = []
        saved = _canonicalize_into(staged, stage_out, floor_year)
        print(f"{company}: {len(saved)} canonical PDF(s) staged from IR engine")

        # 1.b) Annual / integrated reports — an opt-in second IR section fetched with the annual
        # selectors (doc_kind="annual"), so annual/integrated/20-F links the quarterly pass drops
        # are kept and canonicalized as <YYYY>-FY.pdf alongside the quarterly periods.
        annual = cfg.get("annual_reports") or {}
        if annual.get("url"):
            a_kwargs: dict = {"max_reports": max_reports, "doc_kind": "annual",
                              "floor_year": floor_year}
            if annual.get("pdf_link_pattern"):
                a_kwargs["file_pattern"] = annual["pdf_link_pattern"]
            if annual.get("delay_ms", ir.get("delay_ms")) is not None:
                a_kwargs["delay_ms"] = int(annual.get("delay_ms", ir.get("delay_ms")))
            if annual.get("use_playwright") is not None:
                a_kwargs["use_playwright"] = bool(annual["use_playwright"])
            try:
                a_staged = download_from_ir(annual["url"], raw_dir, **a_kwargs)
            except Exception as exc:  # noqa: BLE001 — annual discovery is best-effort, like the rest
                print(f"{company}: annual IR discovery failed: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                a_staged = []
            a_saved = _canonicalize_into(a_staged, stage_out, floor_year)
            print(f"{company}: {len(a_saved)} canonical annual PDF(s) staged from IR engine")

        # 1.5) Direct URL templates — for bot-blocked index pages whose document
        # URLs are deterministic (ir_website.direct_url_templates). Only attempts
        # periods still missing after live IR discovery.
        if ir.get("direct_url_templates"):
            have = _periods_in_dir(stage_out, floor_year)
            if not force_refresh:
                have |= _periods_in_dir(out_dir, floor_year)
            missing = _expected_periods(floor_year) - have
            if missing:
                try:
                    from src.download.downloader import download_from_url_templates

                    templated = download_from_url_templates(
                        list(ir["direct_url_templates"]), stage_out, missing,
                        delay_ms=int(ir.get("delay_ms", 300)),
                    )
                    print(f"{company}: {len(templated)} period(s) staged via URL templates")
                except Exception as exc:  # noqa: BLE001 — best-effort, like the other layers
                    print(f"{company}: URL-template fetch skipped: {type(exc).__name__}: {exc}",
                          file=sys.stderr)

        # 2) Wayback Machine — recover periods still missing after live IR.
        if use_wayback:
            have = _periods_in_dir(stage_out, floor_year)
            if not force_refresh:
                have |= _periods_in_dir(out_dir, floor_year)
            missing = _expected_periods(floor_year) - have
            if missing:
                try:
                    from src.download.wayback import download_missing

                    recovered = download_missing(url, stage_out, missing, from_year=floor_year)
                    print(f"{company}: {len(recovered)} period(s) staged via Wayback")
                except Exception as exc:  # noqa: BLE001 — archive recovery is best-effort
                    print(f"{company}: Wayback recovery skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

        if parse:
            parsed = _parse_pdfs(stage_out, jsonl=jsonl)
            print(f"{company}: parsed {len(parsed)} staged PDF(s)")

        staged_pdfs = sorted(stage_out.glob("*.pdf"))
        if force_refresh and not staged_pdfs:
            print(
                f"{company}: force refresh aborted because no PDFs were staged; existing artifacts left untouched",
                file=sys.stderr,
            )
        elif force_refresh:
            removed = _clear_generated_report_artifacts(out_dir)
            copied = _copy_generated_artifacts(stage_out, out_dir, overwrite=True)
            print(f"{company}: removed {removed} old artifact(s), copied {copied} refreshed artifact(s)")
        else:
            copied = _copy_generated_artifacts(stage_out, out_dir, overwrite=False)
            print(f"{company}: copied {copied} new artifact(s)")

    # 3) BMV XBRL archive (~2021+) — facts artifacts to aid extraction.
    if use_xbrl and ir.get("xbrl_ticker"):
        try:
            from src.download.bmv_xbrl import download_ticker

            download_ticker(ir["xbrl_ticker"], out_dir=out_dir, include_annual=annual_facts)
        except Exception as exc:  # noqa: BLE001 — XBRL is a best-effort supplement
            print(f"{company}: XBRL fetch skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

    total = len(list(out_dir.glob("*.pdf")))
    print(f"{company}: {total} PDF(s) in {out_dir}")
    _print_missing_summary(company, out_dir, floor_year)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("company")
    ap.add_argument("--max", type=int, default=120, help="max reports from the IR engine")
    ap.add_argument("--floor-year", type=int, default=START_FLOOR_YEAR)
    ap.add_argument("--no-xbrl", action="store_true", help="skip BMV XBRL fetch")
    ap.add_argument("--no-wayback", action="store_true", help="skip Wayback recovery")
    ap.add_argument("--force-refresh", action="store_true",
                    help="replace generated PDFs/MD/JSONL artifacts after a staged refresh")
    ap.add_argument("--parse", action="store_true", help="parse refreshed PDFs to sibling markdown")
    ap.add_argument("--jsonl", action="store_true",
                    help="with --parse, also write page-block JSONL files")
    ap.add_argument("--annual-facts", action="store_true",
                    help="also fetch annual (YYYY-FY) XBRL facts, not just quarterly")
    args = ap.parse_args()
    fetch(
        args.company,
        max_reports=args.max,
        floor_year=args.floor_year,
        use_xbrl=not args.no_xbrl,
        use_wayback=not args.no_wayback,
        force_refresh=args.force_refresh,
        parse=args.parse,
        jsonl=args.jsonl,
        annual_facts=args.annual_facts,
    )


if __name__ == "__main__":
    main()
