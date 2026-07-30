"""
bmv_xbrl.py — BMV XBRL archive source for quarterly financial filings.

The BMV publishes every listed issuer's IFRS XBRL filings on a single static page
(https://www.bmv.com.mx/es/emisoras/archivos-estadar-xbrl). Each filing is a zip
holding one JSON instance document with ~666 tagged IFRS concepts (Revenue,
ProfitLoss, ...) plus the full MD&A narrative embedded as HTML text blocks.

Coverage caveats (verified June 2026):
- Rolling ~5-year window: older zips are purged from BMV's servers (404). The
  archive page's date filters are ignored server-side.
- Financial-sector issuers (ACTINVR, GFNORTE, ...) file annual XBRL only — their
  quarterly reports exist solely as PDFs on their own IR pages (use downloader.py).

Usage:
    python3 bmv_xbrl.py --ticker SPORT --out downloads/sport
    python3 bmv_xbrl.py --list-tickers
    python3 bmv_xbrl.py --bulk-archive /path/to/snapshot
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from src.download.downloader import _make_session, _session_get

ARCHIVE_URL = "https://www.bmv.com.mx/es/emisoras/archivos-estadar-xbrl"

_QUARTER_TEXT_RE = re.compile(r"Trimestre\s+([1-4])\s+Del\s+A(?:ñ|&ntilde;)o\s+(20\d{2})", re.IGNORECASE)
_ANNUAL_TEXT_RE = re.compile(r"Anual.{0,40}?a(?:ñ|&ntilde;)o\s+(20\d{2})", re.IGNORECASE)
_ZIP_PERIOD_RE = re.compile(r"_(20\d{2})-0([1-4])d?_\d+\.zip$", re.IGNORECASE)
_ZIP_ANNUAL_RE = re.compile(r"_(20\d{2})_\d+\.zip$", re.IGNORECASE)

# Narrative text-block concepts worth keeping as the MD&A artifact.
_MDNA_CONCEPT_RE = re.compile(
    r"^(?:ifrs-mc_|mc_mx-cor_)|Explanatory$|BloqueDeTexto", re.IGNORECASE
)
_MDNA_MIN_CHARS = 2000


@dataclass(frozen=True)
class XbrlFiling:
    ticker: str
    razon_social: str
    filed_date: str   # raw "dd/mm/YYYY HH:MM" from the page
    period: str       # "2026-1T" (quarterly) or "2025-FY" (annual)
    kind: str         # "quarterly" | "annual"
    zip_url: str


# ---------------------------------------------------------------------------
# Archive index
# ---------------------------------------------------------------------------

def parse_archive_index(html: str) -> list[XbrlFiling]:
    """Parse the archive page's table into filings (pure function, testable)."""
    soup = BeautifulSoup(html, "html.parser")
    filings: list[XbrlFiling] = []

    for row in soup.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        anchor = row.find("a", href=True)
        if anchor is None:
            continue
        href = anchor["href"]
        docins = parse_qs(urlparse(href).query).get("docins", [""])[0]
        if not docins:
            continue
        # docins is relative to /docs-pub/ifrsxbrl/ (e.g. "../ifrsxbrl/ifrsxbrl_..._1.zip")
        zip_url = urljoin("https://www.bmv.com.mx/docs-pub/ifrsxbrl/", docins)
        if not zip_url.lower().endswith(".zip"):
            continue

        ticker = cells[0].get_text(strip=True)
        razon_social = cells[1].get_text(strip=True)
        filed_date = cells[2].get_text(strip=True)
        label = anchor.get_text(" ", strip=True)

        period, kind = _infer_period(label, zip_url)
        if not ticker or period is None:
            continue
        filings.append(XbrlFiling(ticker, razon_social, filed_date, period, kind, zip_url))

    return filings


def _infer_period(label: str, zip_url: str) -> tuple[str | None, str]:
    m = _QUARTER_TEXT_RE.search(label)
    if m:
        return f"{m.group(2)}-{m.group(1)}T", "quarterly"
    m = _ANNUAL_TEXT_RE.search(label)
    if m:
        return f"{m.group(1)}-FY", "annual"
    zip_name = zip_url.rsplit("/", 1)[-1]
    m = _ZIP_PERIOD_RE.search(zip_name)
    if m:
        return f"{m.group(1)}-{m.group(2)}T", "quarterly"
    m = _ZIP_ANNUAL_RE.search(zip_name)
    if m:
        return f"{m.group(1)}-FY", "annual"
    return None, "unknown"


def fetch_archive_index(
    session: requests.Session | None = None,
    *,
    verify_ssl: bool = True,
    cache_html_path: Path | None = None,
) -> list[XbrlFiling]:
    """Download and parse the full archive page (~4 MB, lists every filing)."""
    session = session or _make_session(verify_ssl=verify_ssl)
    print(f"Fetching BMV XBRL archive index: {ARCHIVE_URL}", file=sys.stderr)
    resp = _session_get(session, ARCHIVE_URL, timeout=120, verify_ssl=verify_ssl)
    if cache_html_path is not None:
        cache_html_path = Path(cache_html_path)
        cache_html_path.parent.mkdir(parents=True, exist_ok=True)
        cache_html_path.write_text(resp.text, encoding="utf-8")
    filings = parse_archive_index(resp.text)
    if not filings:
        raise RuntimeError(
            "BMV XBRL archive page parsed to zero filings — page layout likely changed "
            f"({len(resp.text)} bytes fetched)."
        )
    print(f"Archive index: {len(filings)} filings, "
          f"{len({f.ticker for f in filings})} issuers.", file=sys.stderr)
    return filings


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------

def _safe_name(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "", token) or "UNKNOWN"


def _download_filing_json(
    session: requests.Session,
    filing: XbrlFiling,
    dest: Path,
    *,
    verify_ssl: bool = True,
) -> Path:
    """Download a filing zip and write its inner JSON instance document to dest."""
    resp = _session_get(session, filing.zip_url, timeout=120, verify_ssl=verify_ssl)
    payload = resp.content if hasattr(resp, "content") else resp.text.encode()
    if not payload.startswith(b"PK"):
        raise ValueError(f"Not a zip archive: {filing.zip_url}")
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        json_names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not json_names:
            raise ValueError(f"No JSON member in {filing.zip_url} (members: {zf.namelist()})")
        data = zf.read(json_names[0])
    tmp = dest.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.rename(dest)
    return dest


def download_ticker(
    ticker: str,
    out_dir: Path,
    *,
    index: list[XbrlFiling] | None = None,
    filings: Iterable[XbrlFiling] | None = None,
    session: requests.Session | None = None,
    include_annual: bool = False,
    kinds: frozenset[str] | None = None,
    max_filings: int | None = None,
    delay_ms: int = 1000,
    verify_ssl: bool = True,
    write_artifacts: bool = True,
) -> list[Path]:
    """Download all available XBRL filings for one ticker; returns JSON paths.

    Idempotent: filings whose JSON already exists are skipped (but artifacts are
    still refreshed when write_artifacts is set).
    """
    session = session or _make_session(verify_ssl=verify_ssl)
    if filings is not None and index is not None:
        raise ValueError("pass either index or filings, not both")
    if filings is not None:
        available = list(filings)
    else:
        if index is None:
            index = fetch_archive_index(session, verify_ssl=verify_ssl)
        available = index
    wanted = [f for f in available if f.ticker.upper() == ticker.upper()]
    if kinds is not None:
        unknown = set(kinds) - {"quarterly", "annual"}
        if unknown:
            raise ValueError(f"Unknown XBRL filing kind(s): {sorted(unknown)}")
        wanted = [f for f in wanted if f.kind in kinds]
    elif not include_annual:
        wanted = [f for f in wanted if f.kind == "quarterly"]
    wanted.sort(key=lambda f: f.period, reverse=True)
    if max_filings is not None:
        wanted = wanted[:max_filings]
    if not wanted:
        known = sorted({f.ticker for f in available})
        raise RuntimeError(
            f"No requested XBRL filings for ticker {ticker!r} in the BMV archive. "
            f"(Some issuers/document types require the IR or SEC adapter.) "
            f"{len(known)} tickers available."
        )

    xbrl_dir = Path(out_dir) / "xbrl"
    xbrl_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []

    for i, filing in enumerate(wanted, 1):
        dest = xbrl_dir / f"{_safe_name(filing.ticker)}_{filing.period}.json"
        if dest.exists():
            print(f"  [{i}/{len(wanted)}] exists: {dest.name}", file=sys.stderr)
        else:
            try:
                _download_filing_json(session, filing, dest, verify_ssl=verify_ssl)
                print(f"  [{i}/{len(wanted)}] downloaded: {dest.name}", file=sys.stderr)
            except Exception as exc:
                print(f"  [{i}/{len(wanted)}] FAILED {filing.zip_url}: {exc}", file=sys.stderr)
                continue
            if i < len(wanted) and delay_ms > 0:
                time.sleep(delay_ms / 1000)
        saved.append(dest)
        if write_artifacts:
            try:
                extract_artifacts(dest)
            except Exception as exc:
                print(f"    artifact extraction failed for {dest.name}: {exc}", file=sys.stderr)

    return sorted(saved)


def bulk_archive(
    out_dir: Path,
    *,
    session: requests.Session | None = None,
    delay_ms: int = 1000,
    verify_ssl: bool = True,
) -> int:
    """Snapshot every zip in the current archive window (resumable, skip-existing).

    The window rolls: each passing quarter the oldest quarter is purged from BMV.
    Expect roughly 5,700 zips / 6-10 GB.
    """
    session = session or _make_session(verify_ssl=verify_ssl)
    index = fetch_archive_index(session, verify_ssl=verify_ssl,
                                cache_html_path=Path(out_dir) / "archive_index.html")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "archive_index.json").write_text(
        json.dumps([asdict(f) for f in index], ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    fetched = 0
    for i, filing in enumerate(index, 1):
        zip_name = filing.zip_url.rsplit("/", 1)[-1]
        dest = out_dir / _safe_name(filing.ticker) / zip_name
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            resp = _session_get(session, filing.zip_url, timeout=120, verify_ssl=verify_ssl)
            payload = resp.content if hasattr(resp, "content") else resp.text.encode()
            if not payload.startswith(b"PK"):
                raise ValueError("not a zip")
            tmp = dest.with_suffix(".tmp")
            tmp.write_bytes(payload)
            tmp.rename(dest)
            fetched += 1
            print(f"  [{i}/{len(index)}] {filing.ticker} {filing.period}: {zip_name}", file=sys.stderr)
        except Exception as exc:
            print(f"  [{i}/{len(index)}] FAILED {filing.zip_url}: {exc}", file=sys.stderr)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
    print(f"Bulk archive: {fetched} new zip(s) under {out_dir}", file=sys.stderr)
    return fetched


# ---------------------------------------------------------------------------
# Artifact extraction: numeric facts + MD&A narrative
# ---------------------------------------------------------------------------

def _is_true(value) -> bool:
    return value is True or value == "True" or value == "true"


def extract_artifacts(json_path: Path, out_dir: Path | None = None) -> dict[str, Path]:
    """Split an XBRL instance JSON into the two artifacts the pipeline consumes.

    - ``<stem>_facts.json``: numeric facts flattened to
      ``{concept: [{value, instant, period_start, period_end, unit, decimals, dimensions}]}``
    - ``<stem>_mdna.html``: the embedded narrative text blocks (management
      commentary, performance indicators, notes), the PDF-text analogue.
    """
    json_path = Path(json_path)
    out_dir = Path(out_dir) if out_dir is not None else json_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = json.loads(json_path.read_text(encoding="utf-8"))

    facts_by_concept = doc.get("HechosPorIdConcepto") or {}
    facts_by_id = doc.get("HechosPorId") or {}
    contexts = doc.get("ContextosPorId") or {}
    units = doc.get("UnidadesPorId") or {}

    def unit_label(unit_id) -> str | None:
        unit = units.get(unit_id) or {}
        measures = unit.get("Medidas") or []
        if measures and isinstance(measures[0], dict):
            return measures[0].get("Etiqueta") or measures[0].get("Nombre")
        return None

    def context_fields(ctx_id) -> dict:
        ctx = contexts.get(ctx_id) or {}
        periodo = ctx.get("Periodo") or {}
        def day(value):
            return value[:10] if isinstance(value, str) else None
        return {
            "instant": day(periodo.get("FechaInstante")),
            "period_start": day(periodo.get("FechaInicio")),
            "period_end": day(periodo.get("FechaFin")),
            "dimensions": ctx.get("ValoresDimension"),
        }

    flat: dict[str, list[dict]] = {}
    mdna_blocks: list[tuple[str, str]] = []

    for concept, fact_ids in facts_by_concept.items():
        for fact_id in fact_ids or []:
            fact = facts_by_id.get(fact_id) or {}
            if _is_true(fact.get("EsValorNil")):
                continue
            value = fact.get("Valor")
            if _is_true(fact.get("EsNumerico")):
                numeric = fact.get("ValorNumerico")
                try:
                    number = float(numeric if numeric not in (None, "None", "") else value)
                except (TypeError, ValueError):
                    continue
                entry = {"value": number}
                entry.update(context_fields(fact.get("IdContexto")))
                entry["unit"] = unit_label(fact.get("IdUnidad"))
                entry["decimals"] = fact.get("Decimales")
                flat.setdefault(concept, []).append(entry)
            elif (
                isinstance(value, str)
                and len(value) >= _MDNA_MIN_CHARS
                and _MDNA_CONCEPT_RE.search(concept)
            ):
                mdna_blocks.append((concept, value))

    stem = json_path.stem
    facts_path = out_dir / f"{stem}_facts.json"
    facts_path.write_text(
        json.dumps({"source": json_path.name, "facts": flat}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    # Management-commentary concepts first, then accounting-policy/notes blocks.
    mdna_blocks.sort(key=lambda item: (not item[0].startswith(("ifrs-mc_", "mc_mx-cor_")), item[0]))
    mdna_path = out_dir / f"{stem}_mdna.html"
    parts = [f"<!-- extracted from {json_path.name} -->"]
    for concept, block in mdna_blocks:
        parts.append(f'<section data-concept="{concept}">\n{block}\n</section>')
    mdna_path.write_text("\n".join(parts), encoding="utf-8")

    return {"facts": facts_path, "mdna": mdna_path}


def load_mdna_text(json_path: Path) -> str | None:
    """Plain-text view of a filing's MD&A artifact (regenerating it if missing)."""
    json_path = Path(json_path)
    mdna_path = json_path.with_name(json_path.stem + "_mdna.html")
    if not mdna_path.exists():
        extract_artifacts(json_path)
    if not mdna_path.exists():
        return None
    soup = BeautifulSoup(mdna_path.read_text(encoding="utf-8"), "html.parser")
    text = soup.get_text("\n", strip=True)
    return text or None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download BMV XBRL quarterly filings.")
    parser.add_argument("--ticker", help="BMV ticker (e.g. SPORT, ALSEA, WALMEX)")
    parser.add_argument("--out", type=Path, help="Output directory")
    parser.add_argument("--annual", action="store_true", help="Include annual (anexo N) filings")
    parser.add_argument("--kind", choices=("quarterly", "annual"),
                        help="Download only one filing kind (overrides --annual)")
    parser.add_argument("--max", type=int, default=None, help="Max filings (newest first)")
    parser.add_argument("--delay-ms", type=int, default=1000)
    parser.add_argument("--no-artifacts", action="store_true", help="Skip facts/MD&A extraction")
    parser.add_argument("--no-verify-ssl", action="store_true")
    parser.add_argument("--list-tickers", action="store_true", help="List issuers in the archive")
    parser.add_argument("--bulk-archive", type=Path, metavar="DIR",
                        help="Snapshot every zip in the current window (~6-10 GB)")
    args = parser.parse_args(argv)
    verify_ssl = not args.no_verify_ssl

    if args.list_tickers:
        index = fetch_archive_index(verify_ssl=verify_ssl)
        counts: dict[str, list[str]] = {}
        for filing in index:
            counts.setdefault(filing.ticker, []).append(filing.period)
        for ticker in sorted(counts):
            periods = sorted(counts[ticker])
            quarterly = [p for p in periods if p.endswith("T")]
            print(f"{ticker}\t{len(quarterly)} quarterly\t{periods[0]}..{periods[-1]}")
        return 0

    if args.bulk_archive:
        bulk_archive(args.bulk_archive, delay_ms=args.delay_ms, verify_ssl=verify_ssl)
        return 0

    if not args.ticker or not args.out:
        parser.error("--ticker and --out are required (or use --list-tickers / --bulk-archive)")

    paths = download_ticker(
        args.ticker,
        args.out,
        include_annual=args.annual,
        kinds=frozenset({args.kind}) if args.kind else None,
        max_filings=args.max,
        delay_ms=args.delay_ms,
        verify_ssl=verify_ssl,
        write_artifacts=not args.no_artifacts,
    )
    print(f"\n{len(paths)} filing(s) under {Path(args.out) / 'xbrl'}", file=sys.stderr)
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
