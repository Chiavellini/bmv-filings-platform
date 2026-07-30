"""wayback.py — recover quarterly-report PDFs that have rolled off a company's
live IR page, using the Internet Archive Wayback Machine.

This is a *last-resort* recovery layer, invoked by ``scripts/fetch_company_reports``
only for periods still missing after the live IR engine (``download_from_ir``) and
the BMV XBRL archive have run. It is deliberately kept out of ``download_from_ir``'s
layer chain so archive.org is not hit on every normal run.

Flow:
  1. ``cdx_snapshots``        — query the Wayback CDX API for archived PDFs under
                                the IR host (prefix match), filtered to real PDFs.
  2. ``discover_archived_report_candidates`` — map each archived URL to a canonical
                                period via the shared ``infer_period_label`` normalizer,
                                keep periods >= the floor year, newest candidates first.
  3. ``download_missing``     — fetch only the requested missing periods straight to
                                canonical ``<period>.pdf`` files, trying alternate
                                snapshots when an archived PDF is truncated.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

from src.download.downloader import _QR_EXCLUDE_RE, _download_pdf, _make_session
from src.shared.report_index import infer_period_label, period_sort_key

CDX_API = "http://web.archive.org/cdx/search/cdx"
START_FLOOR_YEAR = 2016

# Re-transmission notices and material-event ("eventos relevantes") filings often share a
# period with the real report. They are NOT dropped — an audited "retransmisión … cifras
# dictaminadas" can be the genuine report — but they are tried last so a plain report under
# /reportes/ wins when both are archived for the same period.
_DEPRIORITIZE_RE = re.compile(r"retra[sn]+mis|reenv[ií]o|eventos?[_\s/-]*relevantes", re.IGNORECASE)

# Country-code TLDs with a mandatory second-level label (e.g. .com.mx, .co.uk).
# Used by _apex_domain to identify the true registrable domain.
_MULTI_PART_TLDS = {
    "com.mx", "org.mx", "edu.mx", "net.mx", "gob.mx",
    "co.uk", "org.uk", "me.uk",
    "com.br", "org.br", "net.br",
    "com.ar", "org.ar",
    "com.co", "org.co",
}


def _apex_domain(netloc: str) -> str:
    """Strip subdomains and return the registrable (apex) domain.

    Examples:
        www.elpuertodeliverpool.mx       → elpuertodeliverpool.mx
        inversionistas.grupochedraui.com.mx → grupochedraui.com.mx
        www.sportsworld.com.mx           → sportsworld.com.mx
        walmex.mx                        → walmex.mx
    """
    parts = netloc.split(".")
    tld2 = ".".join(parts[-2:]) if len(parts) >= 2 else ""
    if tld2 in _MULTI_PART_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return netloc


def _host_prefix(ir_url: str) -> str:
    """'https://inversionistas.x.com.mx/reportes/' -> 'inversionistas.x.com.mx/*'."""
    netloc = urlparse(ir_url).netloc
    return f"{netloc}/*"


def cdx_snapshots(
    ir_url: str,
    *,
    from_year: int = START_FLOOR_YEAR,
    session: requests.Session | None = None,
    timeout: int = 60,
) -> list[dict]:
    """Return archived PDF snapshots (``{original, timestamp}``) for the IR host.

    Uses the CDX API with a domain-level match on the registrable (apex) domain,
    restricted to PDF responses that returned HTTP 200, collapsed to one row per
    distinct URL. Domain-level matching is necessary because IR sites commonly
    serve historical reports from the bare apex domain, non-www subdomains, or
    alternate paths that a prefix-match on the www host would miss entirely.
    """
    netloc = urlparse(ir_url).netloc
    domain = _apex_domain(netloc)
    sess = session or _make_session()
    params = {
        "url": domain,
        "matchType": "domain",
        "filter": ["mimetype:application/pdf", "statuscode:200"],
        "collapse": "urlkey",
        "from": str(from_year),
        "output": "json",
        "fl": "original,timestamp",
    }
    resp = sess.get(CDX_API, params=params, timeout=timeout)
    resp.raise_for_status()
    rows = _parse_cdx_json(resp.text)
    return rows


def _parse_cdx_json(text: str) -> list[dict]:
    """Parse CDX JSON output. First row is the header; remaining rows are values."""
    text = text.strip()
    if not text:
        return []
    data = json.loads(text)
    if not data:
        return []
    header = data[0]
    return [dict(zip(header, row)) for row in data[1:]]


def wayback_pdf_url(timestamp: str, original: str) -> str:
    """Build the raw-archive URL. The ``id_`` suffix returns the original PDF bytes
    without the Wayback toolbar/HTML wrapper."""
    return f"https://web.archive.org/web/{timestamp}id_/{original}"


def discover_archived_reports(
    ir_url: str,
    *,
    from_year: int = START_FLOOR_YEAR,
    session: requests.Session | None = None,
) -> dict[str, str]:
    """Map archived PDFs to ``{canonical_period: wayback_url}``.

    Each snapshot's URL basename is run through ``infer_period_label``; periods
    earlier than ``from_year`` are dropped. When several snapshots resolve to the
    same period, the latest ``timestamp`` wins.
    """
    candidates = discover_archived_report_candidates(
        ir_url,
        from_year=from_year,
        session=session,
    )
    return {period: urls[0] for period, urls in candidates.items() if urls}


def discover_archived_report_candidates(
    ir_url: str,
    *,
    from_year: int = START_FLOOR_YEAR,
    session: requests.Session | None = None,
) -> dict[str, list[str]]:
    """Map archived PDFs to ``{canonical_period: [wayback_url, ...]}``.

    Candidate URLs are newest-first per period. Keeping alternates matters because
    some Wayback snapshots pass the PDF header check but are truncated; callers can
    try the next URL for the same period.
    """
    snapshots = cdx_snapshots(ir_url, from_year=from_year, session=session)
    by_period: dict[str, list[tuple[int, str, str]]] = {}
    for snap in snapshots:
        original = snap.get("original")
        timestamp = snap.get("timestamp")
        if not original or not timestamp:
            continue
        decoded = unquote(original).lower()
        # Drop conference-call invitations: they carry a period but are never the report.
        # The live IR engine applies this filter via its selectors; the CDX feed does not,
        # so without this such a snapshot can be served for a period that has a real report.
        if _QR_EXCLUDE_RE.search(decoded):
            continue
        stem = Path(urlparse(original).path).stem
        period = infer_period_label(stem)
        if period is None:
            continue
        if period_sort_key(period)[0] < from_year:
            continue
        rank = 1 if _DEPRIORITIZE_RE.search(decoded) else 0  # 0 = plain report (preferred)
        by_period.setdefault(period, []).append((rank, timestamp, wayback_pdf_url(timestamp, original)))
    # Plain reports first (rank 0), then newest timestamp within each rank.
    return {
        period: [url for _rank, _ts, url in sorted(rows, key=lambda r: (r[0], -int(r[1])))]
        for period, rows in by_period.items()
    }


def _pdf_has_eof_marker(path: Path) -> bool:
    """Return True when the PDF has an EOF marker near the end of the file."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read()
    except OSError:
        return False
    return b"%%EOF" in tail


def download_missing(
    ir_url: str,
    out_dir: Path,
    missing_periods: set[str],
    *,
    from_year: int = START_FLOOR_YEAR,
    delay_ms: int = 500,
    session: requests.Session | None = None,
) -> list[Path]:
    """Recover ``missing_periods`` from the Wayback Machine into ``out_dir`` as
    canonical ``<period>.pdf`` files. Returns the paths actually written."""
    if not missing_periods:
        return []
    sess = session or _make_session()
    archived = discover_archived_report_candidates(ir_url, from_year=from_year, session=sess)
    targets = sorted(missing_periods & set(archived), key=period_sort_key)
    if not targets:
        print("Wayback: no archived PDFs for the missing periods.", file=sys.stderr)
        return []
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    recovered: list[Path] = []
    for i, period in enumerate(targets, 1):
        dest = out_dir / f"{period}.pdf"
        if dest.exists():
            continue
        last_exc: Exception | None = None
        for candidate_url in archived[period]:
            try:
                path = _download_pdf(sess, candidate_url, dest, referer=ir_url)
                if not _pdf_has_eof_marker(path):
                    path.unlink(missing_ok=True)
                    raise ValueError("archived PDF appears truncated (missing %%EOF marker)")
                recovered.append(path)
                print(f"  Wayback [{i}/{len(targets)}] recovered {period}.pdf", file=sys.stderr)
                break
            except Exception as exc:  # noqa: BLE001 — try the next archived URL
                last_exc = exc
                dest.unlink(missing_ok=True)
        else:
            detail = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "no candidates"
            print(f"  Wayback FAILED {period}: {detail}", file=sys.stderr)
        if i < len(targets):
            time.sleep(delay_ms / 1000)
    return recovered
