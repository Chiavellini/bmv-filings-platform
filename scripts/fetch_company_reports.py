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
from src.download.xbrl_corpus import materialize_xbrl_corpus  # noqa: E402
from src.shared.company_source import (  # noqa: E402
    UnknownCompanyError,
    resolve_company_source,
)
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


def _clear_generated_report_artifacts(report_dir: Path, floor_year: int) -> int:
    """Delete refreshable artifacts, but never files below the refill floor.

    The refill enforces ``floor_year``, so anything older (e.g. chedraui's
    2010–2015 Wayback-recovered PDFs) would be deleted and never restored —
    permanent data loss. Files whose period can't be inferred are also kept.
    """
    removed = 0
    for path in _generated_report_artifacts(report_dir):
        period = infer_period_label(path.stem)
        if not period or _before_floor(period, floor_year):
            continue
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


def _parse_pdfs(
    report_dir: Path, *, jsonl: bool = False, skip_existing: bool = False
) -> list[str]:
    """Parse canonical PDFs to sibling Markdown, optionally checkpointing completed work.

    ``skip_existing`` checkpoints completed work so an interrupted run resumes
    cheaply — but it must not let *MD&A-derived* Markdown shadow a report PDF.
    An issuer that was XBRL-only when first fetched has `<period>.md` rendered
    from its XBRL narrative; once a binding is discovered and the real earnings
    release arrives, the PDF is strictly the better source and has to win.
    Those periods are re-parsed even when their Markdown exists.
    """
    from src.download.xbrl_corpus import SOURCE_MDNA, load_provenance
    from src.parse.parse_pdf import parse_pdf

    mdna_periods = {
        period
        for period, entry in load_provenance(report_dir).items()
        if entry.source == SOURCE_MDNA
    }

    parsed: list[str] = []
    pdfs = [
        p
        for p in report_dir.glob("*.pdf")
        if infer_period_label(p.stem) is not None
    ]
    for pdf in sorted(pdfs, key=lambda p: period_sort_key(infer_period_label(p.stem) or p.stem)):
        md_dest = pdf.with_suffix(".md")
        period = infer_period_label(pdf.stem)
        if skip_existing and md_dest.exists() and period not in mdna_periods:
            continue
        try:
            md, blocks = parse_pdf(pdf)
        except Exception as exc:  # noqa: BLE001 — keep parsing remaining reports
            print(f"WARN parse {pdf.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        md_dest.write_text(md, encoding="utf-8")
        if jsonl:
            jsonl_dest = pdf.with_suffix(".jsonl")
            with jsonl_dest.open("w", encoding="utf-8") as fh:
                for block in blocks:
                    fh.write(json.dumps(block, ensure_ascii=False) + "\n")
        parsed.append(pdf.stem)
    return parsed


def _annual_reports_config(company: str) -> dict:
    """The optional ``annual_reports:`` block from ``configs/<slug>.yaml``.

    Annual sections live only on the per-company surface; the issuer registry
    has no equivalent, so this stays a direct read and tolerates the file being
    absent entirely.
    """
    path = ROOT / "configs" / f"{company}.yaml"
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    annual = raw.get("annual_reports") if isinstance(raw, dict) else None
    return annual if isinstance(annual, dict) else {}


def _print_missing_summary(
    company: str,
    report_dir: Path,
    floor_year: int,
    coverage_from: str | None = None,
) -> None:
    have = _periods_in_dir(report_dir, floor_year)
    missing = sorted(
        _expected_periods(floor_year, coverage_from) - have, key=period_sort_key
    )
    if missing:
        print(
            f"{company}: missing {len(missing)} expected period(s) after refresh: "
            + ", ".join(missing),
            file=sys.stderr,
        )


def _expected_periods(floor_year: int, coverage_from: str | None = None) -> set[str]:
    """Canonical periods from the floor year through the current quarter.

    ``coverage_from`` (a ``YYYY-nT`` derived from the registry's ``listed_from``)
    trims quarters before the issuer was listed. Without it a 2021 IPO reports
    twenty phantom gaps back to 2016, which makes the missing-period signal too
    noisy to gate on.
    """
    from datetime import date

    today = date.today()
    last_q = (today.month - 1) // 3  # quarters fully begun; current quarter likely unreported
    periods: set[str] = set()
    for year in range(floor_year, today.year + 1):
        for q in range(1, 5):
            if year == today.year and q > max(last_q, 1):
                continue
            periods.add(f"{year}-{q}T")
    if coverage_from:
        floor_key = period_sort_key(coverage_from)
        periods = {p for p in periods if period_sort_key(p) >= floor_key}
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
    fill_xbrl_gaps: bool = False,
) -> None:
    # Resolve across all three config surfaces. A company with only a roster
    # entry still resolves — it simply has no IR URL, and the BMV XBRL archive
    # below covers it from ~2021 on.
    source = resolve_company_source(company)
    url = source.ir_url
    coverage_from = source.coverage_from_period
    out_dir = ROOT / "data" / "reports" / company
    out_dir.mkdir(parents=True, exist_ok=True)

    if source.max_reports:
        max_reports = max(max_reports, int(source.max_reports))
    if source.floor_year and floor_year == START_FLOOR_YEAR:
        floor_year = int(source.floor_year)
    if not url:
        print(
            f"{company}: no IR binding (config surfaces: {', '.join(source.provenance) or 'none'}); "
            "using the BMV XBRL archive only. Run scripts/onboard_source.py "
            f"{company} to discover one.",
            file=sys.stderr,
        )

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        raw_dir = run_dir / "raw"
        stage_out = run_dir / "canonical"
        raw_dir.mkdir()
        stage_out.mkdir()

        # 1) Live IR engine — full discovery into a staging dir, then canonicalize.
        # Skipped entirely when the company has no IR binding.
        if url:
            kwargs: dict = {"max_reports": max_reports, "floor_year": floor_year}
            kwargs.update(source.ir_kwargs())
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
        annual = _annual_reports_config(company)
        if annual.get("url"):
            a_kwargs: dict = {"max_reports": max_reports, "doc_kind": "annual",
                              "floor_year": floor_year}
            if annual.get("pdf_link_pattern"):
                a_kwargs["file_pattern"] = annual["pdf_link_pattern"]
            if annual.get("delay_ms", source.delay_ms) is not None:
                a_kwargs["delay_ms"] = int(annual.get("delay_ms", source.delay_ms))
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
        if source.direct_url_templates:
            have = _periods_in_dir(stage_out, floor_year)
            if not force_refresh:
                have |= _periods_in_dir(out_dir, floor_year)
            missing = _expected_periods(floor_year, coverage_from) - have
            if missing:
                try:
                    from src.download.downloader import download_from_url_templates

                    templated = download_from_url_templates(
                        list(source.direct_url_templates), stage_out, missing,
                        delay_ms=int(source.delay_ms or 300),
                    )
                    print(f"{company}: {len(templated)} period(s) staged via URL templates")
                except Exception as exc:  # noqa: BLE001 — best-effort, like the other layers
                    print(f"{company}: URL-template fetch skipped: {type(exc).__name__}: {exc}",
                          file=sys.stderr)

        # 2) Wayback Machine — recover periods still missing after live IR.
        # Needs the IR URL as its host key, so it only applies to bound companies.
        if use_wayback and url:
            have = _periods_in_dir(stage_out, floor_year)
            if not force_refresh:
                have |= _periods_in_dir(out_dir, floor_year)
            missing = _expected_periods(floor_year, coverage_from) - have
            if missing:
                try:
                    from src.download.wayback import download_missing

                    recovered = download_missing(url, stage_out, missing, from_year=floor_year)
                    print(f"{company}: {len(recovered)} period(s) staged via Wayback")
                except Exception as exc:  # noqa: BLE001 — archive recovery is best-effort
                    print(f"{company}: Wayback recovery skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

        copied_before_parse = 0
        if parse and not force_refresh:
            # Commit downloaded originals before the expensive parse. A later interruption can
            # then resume from permanent PDFs and skip each already-completed Markdown sibling.
            copied_before_parse = _copy_generated_artifacts(
                stage_out, out_dir, overwrite=False
            )
            parsed = _parse_pdfs(out_dir, jsonl=jsonl, skip_existing=True)
            print(f"{company}: parsed {len(parsed)} new permanent PDF(s)")
        elif parse:
            parsed = _parse_pdfs(stage_out, jsonl=jsonl)
            print(f"{company}: parsed {len(parsed)} staged PDF(s)")

        staged_pdfs = sorted(stage_out.glob("*.pdf"))
        if force_refresh and not staged_pdfs:
            print(
                f"{company}: force refresh aborted because no PDFs were staged; existing artifacts left untouched",
                file=sys.stderr,
            )
        elif force_refresh:
            removed = _clear_generated_report_artifacts(out_dir, floor_year)
            copied = _copy_generated_artifacts(stage_out, out_dir, overwrite=True)
            print(f"{company}: removed {removed} old artifact(s), copied {copied} refreshed artifact(s)")
        else:
            copied = _copy_generated_artifacts(stage_out, out_dir, overwrite=False)
            print(
                f"{company}: copied {copied + copied_before_parse} new artifact(s)"
            )

    # 3) BMV XBRL archive (~2021+) — the regulator source. Every roster issuer
    # resolves a ticker, so this runs even with no IR binding at all, and for
    # unbound companies it is the *only* source of coverage.
    if use_xbrl and source.xbrl_ticker:
        try:
            from src.download.bmv_xbrl import download_ticker

            download_ticker(source.xbrl_ticker, out_dir=out_dir, include_annual=annual_facts)
        except Exception as exc:  # noqa: BLE001 — XBRL is a best-effort supplement
            print(f"{company}: XBRL fetch skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

    # 4) Surface the XBRL filings at the canonical paths the extractor reads:
    # <period>_facts.json and, where no PDF exists, <period>.md from the MD&A.
    # Bootstrap-only unless asked: extending a corpus that already has reports
    # adds uncertified observations and moves pinned regression baselines.
    # Always writes provenance.json so every period says where its text came from.
    result = materialize_xbrl_corpus(out_dir, fill_gaps=True if fill_xbrl_gaps else None)
    if result.markdown_written or result.facts_linked:
        print(
            f"{company}: materialized {len(result.markdown_written)} MD&A markdown, "
            f"{len(result.facts_linked)} facts artifact(s)"
        )

    total = len(list(out_dir.glob("*.pdf")))
    print(f"{company}: {total} PDF(s) in {out_dir}")
    _print_missing_summary(company, out_dir, floor_year, coverage_from)


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
    ap.add_argument("--fill-xbrl-gaps", action="store_true",
                    help="materialize XBRL facts/MD&A into a corpus that already has reports "
                         "(adds uncertified observations \u2014 re-certify the company afterwards)")
    args = ap.parse_args()
    try:
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
            fill_xbrl_gaps=args.fill_xbrl_gaps,
        )
    except UnknownCompanyError as exc:
        print(f"{args.company}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
