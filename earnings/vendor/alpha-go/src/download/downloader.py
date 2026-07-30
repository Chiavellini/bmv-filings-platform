"""
downloader.py — Download PDF reports from an Investor Relations website.

Uses requests + BeautifulSoup (static HTML only). For JavaScript-rendered pages
(SPAs using React/Angular/Vue), you will need Selenium or Playwright instead.

Usage (programmatic):
    from src.download.downloader import download_from_ir
    paths = download_from_ir("https://company.com/ir/", Path("./downloads"))

Usage (CLI):
    python3 downloader.py --url "https://company.com/ir/" --out ./downloads
    python3 downloader.py --url "..." --filter 2024 --max 10
"""

from __future__ import annotations

import argparse
import contextvars
import functools
import hashlib
import inspect
import json
import os
import re
import sys
import time
import shutil
import subprocess
import tempfile
from collections import deque
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlparse
from dataclasses import dataclass

try:
    import requests
    from bs4 import BeautifulSoup
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    print("Missing dependencies. Run: pip install requests beautifulsoup4", file=sys.stderr)
    sys.exit(1)


@dataclass(frozen=True)
class _PdfAnchor:
    url: str
    filename: str
    text: str


@dataclass(frozen=True)
class _FetchResult:
    text: str
    url: str


@dataclass
class _LayerReport:
    """Diagnostic record for one discovery layer attempt."""
    layer: str
    candidates: int = 0
    selected: int = 0
    note: str = ""


def _scoped_download_context(fn):
    """Isolate per-run language/document-kind selectors and restore them on every exit.

    ``ContextVar.set`` persists in the current context until its token is reset. The downloader
    previously set both values without restoring them, so an annual or English-only company could
    silently change the selector used by the next company (and by later tests). The wrapper binds
    defaults from the public signature and guarantees restoration even when a fetch raises.
    """
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        lang_token = _LANG_FILTER.set(_build_lang_spec(
            bound.arguments["language"], bound.arguments["lang_include"],
            bound.arguments["lang_exclude"],
        ))
        kind_token = _DOC_KIND.set(bound.arguments["doc_kind"])
        try:
            return fn(*args, **kwargs)
        finally:
            _DOC_KIND.reset(kind_token)
            _LANG_FILTER.reset(lang_token)

    return wrapped


# ---------------------------------------------------------------------------
# Main download function
# ---------------------------------------------------------------------------

@_scoped_download_context
def download_from_ir(
    url: str,
    output_dir: Path,
    *,
    period_filter: str | None = None,
    max_reports: int = 50,
    file_pattern: str = r"(?:quarterly|trimestral|reporte|informe|results?|report|earnings?|10-[kqKQ]).*\.pdf",
    delay_ms: int = 500,
    verify_ssl: bool = True,
    headers: dict | None = None,
    year_api_urls: list[str] | None = None,
    use_playwright: bool | None = None,
    floor_year: int | None = None,
    browser_first: bool = False,
    impersonate: str | None = None,
    language: str | None = None,
    lang_include: str | None = None,
    lang_exclude: str | None = None,
    doc_kind: str = "quarterly",
) -> list[Path]:
    """
    Scrape an IR webpage for PDF links and download them.

    Args:
        url:           IR page URL.
        output_dir:    Directory to save downloaded PDFs.
        period_filter: Optional string to filter filenames (e.g. "2024" keeps only
                       PDFs whose URL contains "2024").
        max_reports:   Maximum number of PDFs to download.
        file_pattern:  Regex applied to the PDF href/filename to select reports.
        delay_ms:      Milliseconds to wait between downloads (rate limiting).
        verify_ssl:    Set False to skip SSL verification (self-signed certs).
        headers:       Optional additional request headers.
        year_api_urls: Optional list of JSON API URLs to call for report data
                       (useful for JS-rendered IR pages where tab switching loads
                       per-year data from an API endpoint).
        use_playwright: If True, use Playwright headless Chromium to render the page
                        and click through year tabs before falling back to static HTML.
                        If False, never use Playwright. If None (default), try the
                        static layers first and fall back to Playwright automatically
                        when they find nothing and Playwright is installed.
                        Requires: pip install playwright && playwright install chromium
        floor_year:     When set, PDF links whose filename maps to a period before this
                        year are skipped before downloading. Links whose period cannot be
                        inferred from the URL are kept (safe fallback).
        browser_first:  If True, run the Playwright discovery layer before the initial
                        requests fetch. Use this for IR pages that return bot/403
                        responses to plain HTTP clients but render in a browser.
        impersonate:    Browser profile (e.g. "chrome") to replay via curl_cffi for every
                        fetch. Set this for hosts behind a Cloudflare TLS-fingerprint bot
                        wall that 403s plain HTTP clients; it skips the wasted first
                        request. When None (default), a Cloudflare block is still detected
                        automatically and retried with impersonation.

    Returns:
        Sorted list of Path objects for successfully downloaded PDFs.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    session = _make_session(verify_ssl=verify_ssl, extra_headers=headers)
    if impersonate:
        # Route all fetches through curl_cffi browser impersonation (Cloudflare-gated host).
        session._impersonate_profile = impersonate

    # URL → preferred filename (populated by Playwright layer with year-injected names)
    _hint_fnames: dict[str, str] = {}

    layer_reports: list[_LayerReport] = []
    rejected_samples: list[tuple[_PdfAnchor, str]] = []
    pdf_links: list[str] = []

    page_url = url
    base_url = url
    page_html = ""
    playwright_ran = False

    def _run_playwright_layer() -> list[str]:
        nonlocal _hint_fnames, playwright_ran
        playwright_ran = True
        playwright_anchors = _playwright_collect_pdf_links(url, base_url, verify_ssl)
        links: list[str] = []
        if playwright_anchors:
            _hint_fnames = {a.url: a.filename for a in playwright_anchors}
            links, rejected = _select_pdf_links_with_diag(playwright_anchors, file_pattern)
            rejected_samples.extend(rejected)
        layer_reports.append(_LayerReport(
            "playwright", len(playwright_anchors), len(links),
            "" if playwright_anchors else "browser rendered no PDF anchors (or playwright not installed)",
        ))
        return links

    if browser_first:
        pdf_links = _run_playwright_layer()

    if not pdf_links:
        print(f"Fetching IR page: {url}", file=sys.stderr)
        try:
            resp = _session_get(session, url, timeout=30, verify_ssl=verify_ssl)
        except requests.RequestException as exc:
            raise RuntimeError(f"Failed to fetch IR page {url}: {exc}") from exc

        page_url = getattr(resp, "url", url)
        page_html = resp.text
        base_url = _effective_base_url(page_html, page_url)

    # Layer 0: config-supplied year API URLs (highest priority, user-specified)
    if not pdf_links and year_api_urls:
        pdf_links = _fetch_year_api_links(session, year_api_urls, base_url, file_pattern, verify_ssl)
        layer_reports.append(_LayerReport(
            "year_api", len(pdf_links), len(pdf_links),
            "" if pdf_links else "configured APIs returned no matching PDFs",
        ))
    elif not pdf_links:
        layer_reports.append(_LayerReport("year_api", note="skipped: no year_api_urls configured"))

    # Layer 1: Walmex-style quarterly archive JSON API
    if not pdf_links:
        archive_diag: list[str] = []
        pdf_links = _extract_quarterly_archive_links(
            session, page_html, page_url, max_reports, delay_ms, diag=archive_diag,
        )
        layer_reports.append(_LayerReport(
            "archive_json", len(pdf_links), len(pdf_links), "; ".join(archive_diag),
        ))

    # Layer 2: Next.js __NEXT_DATA__ + page data endpoint
    if not pdf_links:
        nextjs_anchors = _extract_nextjs_links(page_html, base_url)
        page_data_anchors = _extract_nextjs_page_data_links(session, page_url, page_html, verify_ssl)
        combined = {a.url: a for a in nextjs_anchors}
        combined.update({a.url: a for a in page_data_anchors})
        if combined:
            pdf_links, rejected = _select_pdf_links_with_diag(list(combined.values()), file_pattern)
            rejected_samples.extend(rejected)
            layer_reports.append(_LayerReport("nextjs", len(combined), len(pdf_links)))
            if not pdf_links:
                print(
                    f"  Next.js: found {len(combined)} PDF(s) in page data but none matched "
                    "the quarterly-report filter. Supply year_api_urls in the config if the "
                    "site loads past years dynamically.",
                    file=sys.stderr,
                )
        else:
            layer_reports.append(_LayerReport("nextjs", note="no __NEXT_DATA__ script on page"))

    # Layer 2.5: static per-year sibling pages (year <select> navigation, year URL tokens)
    if not pdf_links:
        year_diag: list[str] = []
        pdf_links = _crawl_year_variant_pages(
            session,
            page_html,
            page_url,
            verify_ssl=verify_ssl,
            delay_ms=delay_ms,
            diag=year_diag,
        )
        layer_reports.append(_LayerReport(
            "year_pages", len(pdf_links), len(pdf_links), "; ".join(year_diag),
        ))

    # Layer 2.6: ASP.NET WebForms year/quarter filters (e.g. GRUMA)
    if not pdf_links:
        aspnet_diag: list[str] = []
        pdf_links = _crawl_aspnet_quarter_filter_links(
            session,
            page_html,
            page_url,
            max_reports=max_reports,
            file_pattern=file_pattern,
            verify_ssl=verify_ssl,
            delay_ms=delay_ms,
            diag=aspnet_diag,
        )
        layer_reports.append(_LayerReport(
            "aspnet_quarter_filter", len(pdf_links), len(pdf_links), "; ".join(aspnet_diag),
        ))

    # Layer 3: Playwright headless browser, when explicitly enabled (config/CLI)
    if not pdf_links and use_playwright and not playwright_ran:
        pdf_links = _run_playwright_layer()
    elif not pdf_links and use_playwright is False:
        layer_reports.append(_LayerReport("playwright", note="skipped: use_playwright disabled"))

    # Layer 4: static HTML crawl with pagination
    if not pdf_links:
        pdf_links = _crawl_paginated_ir_links(
            session,
            page_html,
            page_url,
            max_reports=max_reports,
            file_pattern=file_pattern,
            verify_ssl=verify_ssl,
        )
        layer_reports.append(_LayerReport("static_crawl", len(pdf_links), len(pdf_links)))

    # Layer 5: automatic Playwright fallback — only when nothing else worked, the
    # caller did not explicitly disable it, and Playwright is actually installed.
    if not pdf_links and use_playwright is None:
        if _playwright_available() and not playwright_ran:
            print(
                "No PDFs found via static layers; retrying with Playwright headless browser...",
                file=sys.stderr,
            )
            pdf_links = _run_playwright_layer()
        else:
            note = (
                "auto-fallback skipped: playwright already ran"
                if playwright_ran
                else "auto-fallback skipped: playwright not installed"
            )
            layer_reports.append(_LayerReport(
                "playwright", note=note,
            ))

    if period_filter:
        before_filter = len(pdf_links)
        pdf_links = [lnk for lnk in pdf_links if period_filter in lnk]
        if before_filter and not pdf_links:
            layer_reports.append(_LayerReport(
                "period_filter", before_filter, 0,
                f"period filter {period_filter!r} removed all links",
            ))

    # Extension-less links (Liferay-style /documents/d/...) may point at anything;
    # confirm they serve PDFs before they consume download slots.
    pdf_links = _verify_extensionless_links(session, pdf_links, verify_ssl)

    if len(pdf_links) < 3:
        _print_layer_diagnostics(layer_reports, rejected_samples, page_html, base_url, pdf_links)

    if not pdf_links:
        print("No PDF links found on the IR page.", file=sys.stderr)
        print(
            "If the page renders PDFs via JavaScript, set use_playwright: true in the "
            "company config (requires: pip install playwright && playwright install chromium), "
            "or try supplying year_api_urls in the config, or download PDFs manually and use --dir.",
            file=sys.stderr,
        )
        return []

    # Drop links whose filename is recognisably before the floor year.
    # Links whose period cannot be inferred are kept (safe default).
    if floor_year is not None:
        from src.shared.report_index import infer_period_label as _ipl, period_sort_key as _psk

        def _pre_floor(u: str) -> bool:
            stem = Path(urlparse(u).path).stem
            p = _ipl(stem)
            return p is not None and _psk(p)[0] < floor_year

        before = len(pdf_links)
        pdf_links = [u for u in pdf_links if not _pre_floor(u)]
        dropped = before - len(pdf_links)
        if dropped:
            print(f"  [floor={floor_year}] skipped {dropped} pre-{floor_year} link(s)", file=sys.stderr)

    pdf_links = pdf_links[:max_reports]
    print(f"Found {len(pdf_links)} PDF link(s) to download.", file=sys.stderr)

    downloaded: list[Path] = []
    used_names = {p.name for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"}
    for i, pdf_url in enumerate(pdf_links, 1):
        filename = _slugify_url(pdf_url, used_names, hint_filename=_hint_fnames.get(pdf_url))
        dest = output_dir / filename
        try:
            path = _download_pdf(session, pdf_url, dest, verify_ssl=verify_ssl, referer=page_url)
            downloaded.append(path)
            print(f"  [{i}/{len(pdf_links)}] Downloaded: {filename}", file=sys.stderr)
        except Exception as exc:
            print(f"  [{i}/{len(pdf_links)}] FAILED {pdf_url}: {exc}", file=sys.stderr)
        if i < len(pdf_links):
            time.sleep(delay_ms / 1000)

    return sorted(downloaded)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _print_layer_diagnostics(
    layer_reports: list[_LayerReport],
    rejected_samples: list[tuple[_PdfAnchor, str]],
    page_html: str,
    base_url: str,
    pdf_links: list[str],
) -> None:
    """Print a per-layer summary and sample rejections when results are empty or sparse."""
    print("Discovery layer summary:", file=sys.stderr)
    for r in layer_reports:
        note = f" — {r.note}" if r.note else ""
        print(f"  {r.layer}: candidates={r.candidates}, selected={r.selected}{note}", file=sys.stderr)

    samples = rejected_samples
    if not samples:
        # No layer produced anchor-level diagnostics; explain the entry page's raw candidates.
        selected = set(pdf_links)
        try:
            candidates = _extract_pdf_candidates(page_html, base_url)
        except Exception:
            candidates = []
        samples = [(a, _explain_rejection(a)) for a in candidates if a.url not in selected]

    if samples:
        print(
            f"  {len(samples)} candidate PDF(s) found but not selected (showing up to 10):",
            file=sys.stderr,
        )
        for anchor, reason in samples[:10]:
            label = anchor.filename or anchor.url
            print(f"    - {label}: {reason}", file=sys.stderr)


def _make_session(
    verify_ssl: bool = True,
    extra_headers: dict | None = None,
    retries: int = 3,
) -> requests.Session:
    """Create a requests.Session with retry logic and a browser-like User-Agent."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=1.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.verify = verify_ssl
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
    })
    if extra_headers:
        session.headers.update(extra_headers)
    return session


def _extract_pdf_links(html: str, base_url: str, pattern: str | None) -> list[str]:
    """Parse HTML and return absolute URLs of PDF links matching the best pattern."""
    effective_base_url = _effective_base_url(html, base_url)
    candidates = _extract_pdf_candidates(html, effective_base_url)
    seen = {a.url for a in candidates}
    for anchor in _extract_extensionless_candidates(html, effective_base_url):
        if anchor.url not in seen:
            seen.add(anchor.url)
            candidates.append(anchor)
    return _select_pdf_links(candidates, pattern)


def _extract_pdf_candidates(html: str, base_url: str) -> list[_PdfAnchor]:
    """Extract candidate PDF URLs from common HTML attributes and raw text."""
    soup = BeautifulSoup(html, "html.parser")
    anchors: list[_PdfAnchor] = []
    seen: set[str] = set()

    def add_candidate(raw_url: str, text: str = "") -> None:
        candidate = raw_url.strip()
        if not candidate:
            return
        if _is_signed_storage_url(candidate):
            return
        abs_url = urljoin(base_url, candidate)
        parsed = urlparse(abs_url)
        path_part = parsed.path.lower()
        query_part = parsed.query.lower()
        if not path_part.endswith(".pdf") and ".pdf" not in query_part:
            return
        if abs_url in seen:
            return
        seen.add(abs_url)
        anchors.append(_PdfAnchor(
            url=abs_url,
            filename=Path(parsed.path).name,
            text=text,
        ))

    for tag in soup.find_all(True):
        text = tag.get_text(" ", strip=True)
        for attr_value in tag.attrs.values():
            values = attr_value if isinstance(attr_value, list) else [attr_value]
            for value in values:
                if not isinstance(value, str):
                    continue
                lower = value.lower()
                if ".pdf" not in lower:
                    continue
                for raw_url in _pdf_url_pattern().findall(value):
                    add_candidate(raw_url, text)

    for raw_url in _pdf_url_pattern().findall(html):
        add_candidate(raw_url)

    return anchors


# Hosted-document URL shapes that commonly serve PDFs without a .pdf extension
# (Liferay /documents/d/..., generic download endpoints, id-style query strings).
_EXTENSIONLESS_DOC_PATH_RE = re.compile(
    r"(?:/documents?/|/download|/getfile|/descarga|[?&](?:id|file|doc|document|attachment)=)",
    re.IGNORECASE,
)


def _extract_extensionless_candidates(html: str, base_url: str) -> list[_PdfAnchor]:
    """Extract document links that look like quarterly reports but lack a .pdf extension.

    Sites on Liferay (e.g. /documents/d/{site}/{slug}) and similar CMSs serve PDFs from
    extension-less endpoints that _extract_pdf_candidates can never find. Only anchors
    that already pass the quarterly-report filter are returned, so junk documents from
    the same CMS (legal notices, forms) never reach the permissive selector fallbacks.
    """
    soup = BeautifulSoup(html, "html.parser")
    anchors: list[_PdfAnchor] = []
    seen: set[str] = set()

    def add_candidate(raw_url: str, text: str) -> None:
        candidate = (raw_url or "").strip()
        if not candidate or candidate.startswith(("javascript:", "mailto:", "tel:", "#")):
            return
        if _is_signed_storage_url(candidate):
            return
        abs_url = urljoin(base_url, candidate)
        parsed = urlparse(abs_url)
        if parsed.scheme not in {"http", "https"}:
            return
        path_lower = parsed.path.lower()
        if path_lower.endswith(".pdf") or ".pdf" in parsed.query.lower():
            return  # handled by _extract_pdf_candidates
        if Path(parsed.path).suffix.lower() in {".html", ".htm", ".aspx", ".php", ".jsp", ".xml", ".zip"}:
            return
        if not _EXTENSIONLESS_DOC_PATH_RE.search(abs_url):
            return
        if abs_url in seen:
            return
        slug = Path(parsed.path).name
        anchor = _PdfAnchor(url=abs_url, filename=slug, text=text or "")
        if not _looks_like_quarterly_report(anchor):
            return
        seen.add(abs_url)
        anchors.append(anchor)

    for tag in soup.find_all(True):
        text = tag.get_text(" ", strip=True)
        if tag.name == "a" and tag.get("href"):
            add_candidate(str(tag.get("href")), text)
        for attr_name, attr_value in tag.attrs.items():
            if attr_name in {"href", "class", "id", "style"}:
                continue
            values = attr_value if isinstance(attr_value, list) else [attr_value]
            for value in values:
                if not isinstance(value, str):
                    continue
                if attr_name.startswith("data-") and value.startswith(("/", "http")):
                    add_candidate(value, text)
                elif attr_name == "onclick":
                    for m in re.finditer(
                        r"(?:window\.open|location\.href\s*=|window\.location(?:\.href)?\s*=)"
                        r"\s*\(?\s*['\"]([^'\"]+)['\"]",
                        value,
                    ):
                        add_candidate(m.group(1), text)

    return anchors


def _verify_extensionless_links(
    session: requests.Session,
    pdf_links: list[str],
    verify_ssl: bool,
    max_checks: int = 60,
) -> list[str]:
    """Drop extension-less links that do not actually serve PDFs (capped HEAD checks)."""
    checked = 0
    kept: list[str] = []
    for link in pdf_links:
        parsed = urlparse(link)
        is_extensionless = (
            not parsed.path.lower().endswith(".pdf") and ".pdf" not in parsed.query.lower()
        )
        if not is_extensionless or checked >= max_checks:
            kept.append(link)
            continue
        checked += 1
        if _verify_pdf_url(session, link, verify_ssl):
            kept.append(link)
        else:
            print(f"  Skipping non-PDF document link: {link}", file=sys.stderr)
    return kept


def _verify_pdf_url(session: requests.Session, url: str, verify_ssl: bool = True) -> bool:
    """Check that an extension-less URL actually serves a PDF (HEAD, then ranged GET)."""
    try:
        resp = session.head(url, timeout=15, verify=verify_ssl, allow_redirects=True)
        ctype = (resp.headers.get("Content-Type") or "").lower()
        disposition = (resp.headers.get("Content-Disposition") or "").lower()
        if resp.status_code < 400:
            if ctype.startswith("application/pdf"):
                return True
            if ctype.startswith("application/octet-stream") and ".pdf" in disposition:
                return True
            if ctype.startswith(("text/html", "image/")):
                return False
    except requests.RequestException:
        pass
    # HEAD unsupported/inconclusive: fetch the first KB and check the PDF magic bytes.
    try:
        resp = session.get(
            url,
            timeout=15,
            verify=verify_ssl,
            headers={"Range": "bytes=0-1023"},
            stream=True,
        )
        if resp.status_code >= 400:
            return False
        chunk = next(resp.iter_content(chunk_size=1024), b"")
        resp.close()
        return chunk.lstrip()[:5] == b"%PDF-"
    except requests.RequestException:
        return False


def _extract_quarterly_archive_links(
    session: requests.Session,
    html: str,
    base_url: str,
    max_reports: int,
    delay_ms: int = 0,
    diag: list[str] | None = None,
) -> list[str]:
    """Walk Walmex-style quarterly archive JSON pages and return release PDFs."""
    archive = _discover_quarterly_archive(html, base_url)
    if not archive:
        archive = _walmex_quarterly_archive(base_url)
    if not archive:
        if diag is not None:
            diag.append("no js-filter archive form in HTML and not a walmex.mx quarterly page")
        return []

    archive_url, params = archive
    seen: set[str] = set()
    links: list[str] = []

    page = 1
    while len(links) < max_reports:
        page_params = dict(params)
        page_params["page"] = page
        try:
            resp = _session_get(
                session,
                archive_url,
                params=page_params,
                timeout=30,
                verify_ssl=getattr(session, "verify", True),
            )
            payload = _response_json(resp)
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            print(f"Archive page {page} failed: {exc}", file=sys.stderr)
            break

        page_links = _extract_release_links_from_archive(payload)
        if not page_links:
            break

        added = 0
        for link in page_links:
            if link in seen:
                continue
            seen.add(link)
            links.append(link)
            added += 1
            if len(links) >= max_reports:
                break

        if not added:
            break

        page_total = _archive_page_total(payload)
        if page_total is not None and page >= page_total:
            break
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)
        page += 1

    return links


_YEAR_TEXT_RE = re.compile(r"^'?\s*(20\d{2})\s*$")


def _enumerate_year_page_urls(html: str, page_url: str) -> list[str]:
    """Find per-year page URLs behind year navigation (selects, links, URL year tokens).

    Covers two common IR-page shapes:
    - ``<select>`` whose option values are full per-year URLs (Liferay/Actinver style)
    - a year token embedded in the page URL itself, swapped for each year that
      appears as a year-labelled control in the DOM (no blind probing).
    """
    soup = BeautifulSoup(html, "html.parser")
    current_key = _normalize_page_url(page_url)
    current_netloc = urlparse(page_url).netloc.lower()
    urls: list[str] = []
    seen: set[str] = {current_key}
    dom_years: set[str] = set()

    def add_candidate(raw_url: str) -> None:
        candidate = (raw_url or "").strip()
        if not candidate or candidate.startswith(("javascript:", "mailto:", "tel:", "#")):
            return
        abs_url = urljoin(page_url, candidate)
        parsed = urlparse(abs_url)
        if parsed.scheme not in {"http", "https"}:
            return
        if parsed.netloc.lower() != current_netloc:
            return
        if parsed.path.lower().endswith(".pdf"):
            return
        key = _normalize_page_url(abs_url)
        if key in seen:
            return
        seen.add(key)
        urls.append(abs_url)

    for option in soup.find_all("option"):
        text = option.get_text(strip=True)
        year_match = _YEAR_TEXT_RE.match(text)
        if year_match:
            dom_years.add(year_match.group(1))
        value = (option.get("value") or "").strip()
        if year_match and (value.startswith(("http://", "https://", "/")) or "/" in value):
            add_candidate(value)

    for tag in soup.find_all(("a", "button", "li")):
        text = tag.get_text(strip=True)
        year_match = _YEAR_TEXT_RE.match(text)
        if not year_match:
            continue
        dom_years.add(year_match.group(1))
        if tag.name == "a" and tag.get("href"):
            add_candidate(str(tag.get("href")))

    # Swap the year token in the page URL for each other year present in the DOM
    # (e.g. ...-grupofinanciero-2026 → ...-grupofinanciero-2025).
    url_year = re.search(r"20\d{2}", page_url)
    if url_year and len(dom_years) >= 2:
        for year in sorted(dom_years, reverse=True):
            if year != url_year.group(0):
                add_candidate(page_url[:url_year.start()] + year + page_url[url_year.end():])

    return urls[:25]


def _crawl_year_variant_pages(
    session: requests.Session,
    html: str,
    page_url: str,
    *,
    verify_ssl: bool,
    delay_ms: int = 0,
    diag: list[str] | None = None,
) -> list[str]:
    """Fetch each per-year sibling page and select quarterly reports across all of them.

    Selection is strict (quarterly filter only, no permissive fallbacks): year pages
    with no reports yet ("Próximamente") would otherwise contribute unrelated PDFs.
    """
    year_urls = _enumerate_year_page_urls(html, page_url)
    if len(year_urls) < 2:
        if diag is not None:
            diag.append("no year-navigation controls found on page")
        return []

    if diag is not None:
        diag.append(f"found {len(year_urls)} year page(s)")

    def page_anchors(page_html: str, current_url: str) -> list[_PdfAnchor]:
        base = _effective_base_url(page_html, current_url)
        anchors = _extract_pdf_candidates(page_html, base)
        known = {a.url for a in anchors}
        for anchor in _extract_extensionless_candidates(page_html, base):
            if anchor.url not in known:
                known.add(anchor.url)
                anchors.append(anchor)
        return anchors

    combined: dict[str, _PdfAnchor] = {a.url: a for a in page_anchors(html, page_url)}
    for year_url in year_urls:
        try:
            resp = _session_get(session, year_url, timeout=30, verify_ssl=verify_ssl)
        except requests.RequestException as exc:
            print(f"  Year page {year_url} failed: {exc}", file=sys.stderr)
            continue
        for anchor in page_anchors(resp.text, getattr(resp, "url", year_url)):
            combined.setdefault(anchor.url, anchor)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000)

    return _select_quarterly_report_links(list(combined.values()))


def _crawl_aspnet_quarter_filter_links(
    session: requests.Session,
    html: str,
    page_url: str,
    *,
    max_reports: int,
    file_pattern: str | None,
    verify_ssl: bool,
    delay_ms: int = 0,
    diag: list[str] | None = None,
) -> list[str]:
    """Submit ASP.NET WebForms year/quarter filters and collect report PDFs."""
    spec = _discover_aspnet_quarter_filter(html, page_url)
    if spec is None:
        if diag is not None:
            diag.append("no ASP.NET year+quarter filter form found")
        return []

    action_url, payload, year_name, years, quarter_name, quarters, event_target = spec
    if diag is not None:
        diag.append(f"{len(years)} year option(s), {len(quarters)} quarter option(s)")

    seen: set[str] = set()
    selected: list[str] = []

    def harvest(page_html: str, base: str) -> None:
        anchors = _extract_pdf_candidates(page_html, _effective_base_url(page_html, base))
        seen_urls = {a.url for a in anchors}
        for anchor in _extract_extensionless_candidates(page_html, base):
            if anchor.url not in seen_urls:
                anchors.append(anchor)
                seen_urls.add(anchor.url)
        for link in _select_pdf_links(anchors, file_pattern):
            if link in seen:
                continue
            seen.add(link)
            selected.append(link)

    harvest(html, page_url)

    for year in years:
        for quarter in quarters:
            if len(selected) >= max_reports:
                return selected[:max_reports]
            data = dict(payload)
            data[year_name] = year
            data[quarter_name] = quarter
            if event_target:
                data["__EVENTTARGET"] = event_target
                data.setdefault("__EVENTARGUMENT", "")
            try:
                resp = session.post(
                    action_url,
                    data=data,
                    timeout=30,
                    verify=verify_ssl,
                    headers={"Referer": page_url},
                )
                resp.raise_for_status()
            except requests.RequestException as exc:
                if diag is not None:
                    diag.append(f"post failed for {year} Q{quarter}: {exc}")
                continue

            resp_url = getattr(resp, "url", action_url)
            harvest(resp.text, resp_url)
            # Keep WebForms hidden state fresh for sites that rotate it.
            refreshed = _discover_aspnet_quarter_filter(resp.text, resp_url)
            if refreshed is not None:
                _, payload, _, _, _, _, event_target = refreshed
            if delay_ms > 0:
                time.sleep(delay_ms / 1000)

    return selected[:max_reports]


def _discover_aspnet_quarter_filter(
    html: str,
    page_url: str,
) -> tuple[str, dict[str, str], str, list[str], str, list[str], str | None] | None:
    """Return the postback form payload and select names for ASP.NET archives."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.find("input", attrs={"name": "__VIEWSTATE"}) is None:
        return None

    chosen = None
    year_select = None
    quarter_select = None
    years: list[str] = []
    quarters: list[str] = []

    for form in soup.find_all("form"):
        form_year_select = None
        form_quarter_select = None
        form_years: list[str] = []
        form_quarters: list[str] = []
        for sel in form.find_all("select"):
            opts = [(o.get("value") or o.get_text(" ", strip=True), o.get_text(" ", strip=True))
                    for o in sel.find_all("option")]
            year_values = [
                value.strip() for value, text in opts
                if re.fullmatch(r"20\d{2}", value.strip())
                or re.fullmatch(r"20\d{2}", text.strip())
            ]
            quarter_values = [
                value.strip() for value, text in opts
                if re.fullmatch(r"[1-4]", value.strip())
                or re.fullmatch(r"[1-4]", text.strip())
            ]
            if year_values and form_year_select is None:
                form_year_select = sel
                form_years = year_values
            elif quarter_values and form_quarter_select is None:
                form_quarter_select = sel
                form_quarters = quarter_values
        if form_year_select is not None and form_quarter_select is not None:
            chosen = form
            year_select = form_year_select
            quarter_select = form_quarter_select
            years = form_years
            quarters = form_quarters
            break

    if chosen is None or year_select is None or quarter_select is None:
        return None
    year_name = year_select.get("name")
    quarter_name = quarter_select.get("name")
    if not year_name or not quarter_name:
        return None

    payload: dict[str, str] = {}
    for field in chosen.find_all(["input", "select", "textarea"]):
        name = field.get("name")
        if not name:
            continue
        if field.name == "select":
            selected_opt = field.find("option", selected=True) or field.find("option")
            payload[name] = (selected_opt.get("value") if selected_opt else "") or ""
        elif field.name == "textarea":
            payload[name] = field.get_text()
        else:
            field_type = (field.get("type") or "").lower()
            if field_type in {"button", "submit", "image", "file"}:
                continue
            payload[name] = field.get("value") or ""

    event_target = _find_aspnet_filter_event_target(chosen)
    action = chosen.get("action") or page_url
    action_url = urljoin(page_url, action)
    years = sorted(dict.fromkeys(years), reverse=True)
    quarters = sorted(dict.fromkeys(quarters), key=lambda x: int(x), reverse=True)
    return action_url, payload, year_name, years, quarter_name, quarters, event_target


def _find_aspnet_filter_event_target(soup: BeautifulSoup) -> str | None:
    for tag in soup.find_all(True):
        blob = " ".join(str(tag.get(attr) or "") for attr in ("href", "onclick", "id", "name"))
        if not re.search(r"(filtrar|filter)", blob, re.IGNORECASE):
            continue
        m = re.search(r"__doPostBack\('([^']+)'\s*,\s*'[^']*'\)", str(tag))
        if m:
            return m.group(1)
        for attr in ("name", "id"):
            value = tag.get(attr)
            if value:
                return str(value)
    return None


def _crawl_paginated_ir_links(
    session: requests.Session,
    html: str,
    page_url: str,
    *,
    max_reports: int,
    file_pattern: str | None,
    verify_ssl: bool,
) -> list[str]:
    """Collect report PDFs from a page plus same-site pagination links."""
    queue: deque[tuple[str, str | None, str | None]] = deque()
    queue.append((_normalize_page_url(page_url), html, None))
    queued: set[str] = {_normalize_page_url(page_url)}
    visited: set[str] = set()
    pdf_links: list[str] = []
    seen_pdfs: set[str] = set()

    while queue and len(pdf_links) < max_reports:
        current_url, current_html, referer = queue.popleft()
        page_key = _normalize_page_url(current_url)
        if page_key in visited:
            continue

        if current_html is None:
            try:
                resp = _session_get(
                    session,
                    current_url,
                    timeout=30,
                    verify_ssl=verify_ssl,
                    extra_headers={"Referer": referer} if referer else None,
                )
            except requests.RequestException:
                continue
            current_html = resp.text
            current_url = getattr(resp, "url", current_url)
            page_key = _normalize_page_url(current_url)

        if page_key in visited:
            continue
        visited.add(page_key)

        current_base = _effective_base_url(current_html, current_url)
        for link in _extract_pdf_links(current_html, current_base, file_pattern):
            if link in seen_pdfs:
                continue
            seen_pdfs.add(link)
            pdf_links.append(link)
            if len(pdf_links) >= max_reports:
                break

        if len(pdf_links) >= max_reports:
            break

        for next_url in _extract_pagination_links(current_html, current_base, current_url):
            next_key = _normalize_page_url(next_url)
            if next_key in visited or next_key in queued:
                continue
            queue.append((next_url, None, current_url))
            queued.add(next_key)

    return pdf_links


def _discover_quarterly_archive(html: str, base_url: str) -> tuple[str, dict[str, str]] | None:
    """Locate the hidden quarterly archive form used by Walmex."""
    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        if "js-filter" not in (form.get("class") or []):
            continue
        action_input = form.find("input", attrs={"name": "action", "value": "getInfoTrimestral"})
        if action_input is None:
            continue
        action = form.get("action")
        if not action:
            continue
        params: dict[str, str] = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            value = inp.get("value")
            if not name or value is None:
                continue
            params[name] = value.strip()
        params["action"] = "getInfoTrimestral"
        params.pop("page", None)
        return _resolve_archive_action(base_url, action), params
    return None


def _resolve_archive_action(base_url: str, action: str) -> str:
    """Resolve site-root IR AJAX paths that are emitted without a leading slash."""
    if action.startswith(("http://", "https://", "/")):
        return urljoin(base_url, action)

    parsed = urlparse(base_url)
    if action.startswith("Code/") and parsed.scheme and parsed.netloc:
        return urljoin(f"{parsed.scheme}://{parsed.netloc}/", action)

    return urljoin(base_url, action)


def _effective_base_url(html: str, page_url: str) -> str:
    """Use <base href> when present; otherwise resolve links from the page URL."""
    soup = BeautifulSoup(html, "html.parser")
    base_tag = soup.find("base", href=True)
    if base_tag:
        return urljoin(page_url, base_tag["href"].strip())
    # If the URL path has no file extension, it's a directory-style URL.
    # Ensure it ends with "/" so urljoin resolves relative links correctly.
    parsed = urlparse(page_url)
    if not Path(parsed.path).suffix:
        page_url = page_url.rstrip("/") + "/"
    return page_url


def _is_signed_storage_url(url: str) -> bool:
    """Return True for pre-signed GCS/S3 URLs — they expire in minutes and should be skipped."""
    lower = url.lower()
    return "x-goog-signature=" in lower or "x-amz-signature=" in lower


def _extract_nextjs_links(html: str, base_url: str) -> list[_PdfAnchor]:
    """Parse a Next.js __NEXT_DATA__ JSON blob and return all PDF anchors found inside."""
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script or not script.string:
        return []
    try:
        data = json.loads(script.string)
    except (json.JSONDecodeError, ValueError):
        return []

    seen: set[str] = set()
    anchors: list[_PdfAnchor] = []

    def _walk(obj: object) -> None:
        if isinstance(obj, str) and obj.strip().lower().endswith(".pdf"):
            raw = obj.strip()
            if _is_signed_storage_url(raw):
                return
            abs_url = urljoin(base_url, raw)
            if abs_url not in seen:
                seen.add(abs_url)
                anchors.append(_PdfAnchor(
                    url=abs_url,
                    filename=Path(urlparse(abs_url).path).name,
                    text="",
                ))
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(data)
    if anchors:
        print(f"Found {len(anchors)} PDF(s) in __NEXT_DATA__.", file=sys.stderr)
    return anchors


def _extract_nextjs_buildid(html: str) -> str | None:
    """Return the Next.js build ID from __NEXT_DATA__ or a script src path."""
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script and script.string:
        try:
            return json.loads(script.string).get("buildId")
        except (json.JSONDecodeError, ValueError):
            pass
    for tag in soup.find_all("script", src=True):
        m = re.search(r"/_next/static/([^/]+)/", str(tag.get("src", "")))
        if m and m.group(1) not in ("chunks", "css", "media"):
            return m.group(1)
    return None


def _extract_nextjs_page_data_links(
    session: requests.Session,
    page_url: str,
    html: str,
    verify_ssl: bool,
) -> list[_PdfAnchor]:
    """Fetch /_next/data/{buildId}/page.json and walk it for PDF links."""
    build_id = _extract_nextjs_buildid(html)
    if not build_id:
        return []
    parsed = urlparse(page_url)
    slug = parsed.path.rstrip("/") or "/"
    data_url = f"{parsed.scheme}://{parsed.netloc}/_next/data/{build_id}{slug}.json"
    try:
        resp = _session_get(session, data_url, timeout=15, verify_ssl=verify_ssl)
        payload = _response_json(resp)
    except Exception as exc:
        print(f"Next.js page data endpoint failed: {exc}", file=sys.stderr)
        return []

    seen: set[str] = set()
    anchors: list[_PdfAnchor] = []
    base = f"{parsed.scheme}://{parsed.netloc}/"

    def _walk(obj: object) -> None:
        if isinstance(obj, str) and obj.strip().lower().endswith(".pdf"):
            raw = obj.strip()
            if _is_signed_storage_url(raw):
                return
            abs_url = urljoin(base, raw)
            if abs_url not in seen:
                seen.add(abs_url)
                anchors.append(_PdfAnchor(
                    url=abs_url,
                    filename=Path(urlparse(abs_url).path).name,
                    text="",
                ))
        elif isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    _walk(payload)
    return anchors


def _fetch_year_api_links(
    session: requests.Session,
    year_api_urls: list[str],
    base_url: str,
    file_pattern: str | None,
    verify_ssl: bool,
) -> list[str]:
    """Call each year API URL, walk the JSON response for PDF links, and return filtered results."""
    seen: set[str] = set()
    all_anchors: list[_PdfAnchor] = []
    parsed_base = urlparse(base_url)
    site_root = f"{parsed_base.scheme}://{parsed_base.netloc}/"

    for api_url in year_api_urls:
        try:
            resp = _session_get(session, api_url, timeout=15, verify_ssl=verify_ssl)
            payload = _response_json(resp)
        except Exception as exc:
            print(f"year_api_url failed ({api_url}): {exc}", file=sys.stderr)
            continue

        def _walk(obj: object) -> None:
            if isinstance(obj, str) and obj.strip().lower().endswith(".pdf"):
                raw = obj.strip()
                if _is_signed_storage_url(raw):
                    return
                abs_url = urljoin(site_root, raw)
                if abs_url not in seen:
                    seen.add(abs_url)
                    all_anchors.append(_PdfAnchor(
                        url=abs_url,
                        filename=Path(urlparse(abs_url).path).name,
                        text="",
                    ))
            elif isinstance(obj, dict):
                for v in obj.values():
                    _walk(v)
            elif isinstance(obj, list):
                for item in obj:
                    _walk(item)

        _walk(payload)

    return _select_pdf_links(all_anchors, file_pattern)


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def _playwright_click_quarterly_tabs(page) -> None:
    """Click tab-like controls that reveal quarterly (vs annual) report content."""
    try:
        # Find visible tab controls whose text suggests quarterly reports and use
        # Playwright's native click (handles scroll-into-view and event bubbling).
        btns = page.locator("button, a, [role='tab']").all()
        clicked = 0
        for btn in btns:
            try:
                t = btn.text_content(timeout=500).strip().lower()
                if not re.search(r"trimest|quarterly|(?<![a-z])bmv(?![a-z])|results?", t):
                    continue
                if re.search(r"anual|annual|presentaci|webcast", t):
                    continue
                # Never click anchors that would navigate away from the page.
                href = ""
                try:
                    href = (btn.get_attribute("href") or "").strip()
                except Exception:
                    pass
                if href and not href.startswith("#") and not href.lower().startswith("javascript:"):
                    continue
                btn.scroll_into_view_if_needed()
                btn.click(timeout=2000)
                clicked += 1
            except Exception:
                pass
        if clicked:
            page.wait_for_timeout(1000)
    except Exception:
        pass


def _playwright_settle(page, ms: int = 1200) -> None:
    """Wait for the page to settle after an interaction: network idle, then a fixed floor.

    The fixed wait stays as a floor because client-side state changes (React tab
    switches) resolve networkidle before the DOM actually updates.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass
    try:
        page.wait_for_timeout(ms)
    except Exception:
        pass


# Year-control scopes tried in priority order. The Herdez section IDs stay first so
# the long-verified Spanish-page flow keeps clicking exactly what it clicked before.
_PW_YEAR_SCOPES = (
    "#historicoreporte button",
    "#reportsarchive button",
    "#historicoreporte a, #historicoreporte li, #historicoreporte [role='tab']",
    "#reportsarchive a, #reportsarchive li, #reportsarchive [role='tab']",
    "button",
    "a, li, [role='tab']",
)

# JS: list distinct 4-digit-year texts among elements matching the scope selector,
# skipping anchors that would navigate (real hrefs) since we cannot safely click those.
_PW_YEAR_DISCOVERY_JS = (
    "(sel) => {"
    "  const seen = new Set(), out = [];"
    "  document.querySelectorAll(sel).forEach(b => {"
    "    const t = b.textContent.trim();"
    "    if (!/^20\\d{2}$/.test(t) || seen.has(t)) return;"
    "    if (b.tagName === 'A') {"
    "      const href = (b.getAttribute('href') || '').trim().toLowerCase();"
    "      if (href && !href.startsWith('#') && !href.startsWith('javascript:')) return;"
    "    }"
    "    seen.add(t); out.push(t);"
    "  });"
    "  return out;"
    "}"
)

# JS: click the year control matching [selector, yearText], preferring real buttons.
_PW_YEAR_CLICK_JS = (
    "([sel, y]) => {"
    "  const order = {BUTTON: 0, A: 2, LI: 3};"
    "  const matches = Array.from(document.querySelectorAll(sel))"
    "    .filter(b => b.textContent.trim() === y)"
    "    .filter(b => {"
    "      if (b.tagName !== 'A') return true;"
    "      const href = (b.getAttribute('href') || '').trim().toLowerCase();"
    "      return !href || href.startsWith('#') || href.startsWith('javascript:');"
    "    })"
    "    .sort((a, b) => (order[a.tagName] ?? 1) - (order[b.tagName] ?? 1));"
    "  if (matches.length) { matches[0].click(); return true; }"
    "  return false;"
    "}"
)

# JS: within the given section selector (or document if empty), find the first
# "Ver más"/"Load more"-style button and click it. Returns true if clicked.
_PW_LOAD_MORE_JS = (
    "(sel) => {"
    "  const scope = sel ? document.querySelector(sel) : document;"
    "  if (!scope) return false;"
    "  const candidates = Array.from(scope.querySelectorAll('button, a'));"
    "  for (const el of candidates) {"
    "    const t = (el.textContent || '').trim();"
    "    if (!/ver\\s*m[aá]s|load\\s*more|show\\s*more|cargar\\s*m[aá]s|ver\\s*todos|see\\s*more|m[aá]s\\s*resultados/i.test(t)) continue;"
    "    const href = (el.getAttribute('href') || '').trim();"
    "    if (href && !href.startsWith('#') && !/^javascript:/i.test(href)) continue;"
    "    el.click(); return true;"
    "  }"
    "  return false;"
    "}"
)

# JS: find <select> controls whose options are year labels (Peñoles-style year
# dropdowns) and return [selectIndex, [optionValues...]] pairs. The single-element
# list argument keeps this call distinguishable from the year-click call shape.
_PW_SELECT_DISCOVERY_JS = (
    "(_args) => {"
    "  const out = [];"
    "  document.querySelectorAll('select').forEach((s, i) => {"
    "    const vals = [];"
    "    Array.from(s.options).forEach(o => {"
    "      const t = (o.textContent || '').trim();"
    "      if (/^'?\\s*20\\d{2}\\s*$/.test(t) && o.value && o.value !== '0') vals.push(o.value);"
    "    });"
    "    if (vals.length >= 2) out.push([i, vals]);"
    "  });"
    "  return out;"
    "}"
)

# JS: set a select's value and fire a change event ([index, value, marker] shape).
_PW_SELECT_SET_JS = (
    "([idx, value, _marker]) => {"
    "  const sels = document.querySelectorAll('select');"
    "  if (idx >= sels.length) return false;"
    "  const s = sels[idx];"
    "  s.value = value;"
    "  s.dispatchEvent(new Event('change', {bubbles: true}));"
    "  return true;"
    "}"
)


def _playwright_collect_pdf_links(
    url: str,
    base_url: str,
    verify_ssl: bool = True,
    timeout_ms: int = 30000,
) -> list[_PdfAnchor]:
    """Use Playwright headless Chromium to render the page, click year tabs, and collect PDF hrefs."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "Playwright not installed. To enable JS-rendered IR pages:\n"
            "  pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return []

    collected: dict[str, _PdfAnchor] = {}
    sniffed: dict[str, _PdfAnchor] = {}

    def _add_anchor(href: str, text: str, year_hint: str | None = None) -> None:
        if not href or _is_signed_storage_url(href):
            return
        abs_url = urljoin(base_url, href.strip())
        parsed = urlparse(abs_url)
        if parsed.scheme not in {"http", "https"}:
            return
        if abs_url in collected:
            return
        fname = Path(parsed.path).name
        is_pdf = ".pdf" in abs_url.lower()
        if not is_pdf:
            # Extension-less document endpoints: keep only quarterly-looking ones so
            # CMS junk (legal notices, forms) never reaches the selector fallbacks.
            anchor = _PdfAnchor(url=abs_url, filename=fname, text=text or "")
            if not (_EXTENSIONLESS_DOC_PATH_RE.search(abs_url)
                    and _looks_like_quarterly_report(anchor)):
                return
            collected[abs_url] = anchor
            return
        # Inject the clicked year into filenames like "1_T_Quarter_results_pdf_{hash}.pdf"
        # that carry no 4-digit year, so report_index can assign a period label.
        if (year_hint
                and re.match(r"^[1-4]_[TQtq]_", fname)
                and not re.search(r"20\d{2}", fname)):
            yy = year_hint[-2:]
            fname = re.sub(r"^([1-4])_([TQtq])_", rf"\1_\g<2>{yy}_", fname, count=1)
        collected[abs_url] = _PdfAnchor(url=abs_url, filename=fname, text=text or "")

    def _harvest(page, year_hint: str | None = None) -> None:
        try:
            links = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => [a.href, a.textContent.trim()])"
            )
        except Exception:
            links = []
        for href, text in (links or []):
            _add_anchor(href, text, year_hint=year_hint)

    def _on_response(response) -> None:
        # Network sniffing: capture any PDF the page loads, regardless of how the
        # link is rendered in the DOM (JS-built buttons, fetch-then-open flows...).
        try:
            rurl = response.url
            if _is_signed_storage_url(rurl):
                return
            ctype = (response.headers.get("content-type") or "").lower()
            if ".pdf" in rurl.lower() or ctype.startswith("application/pdf"):
                if rurl not in sniffed:
                    sniffed[rurl] = _PdfAnchor(
                        url=rurl,
                        filename=Path(urlparse(rurl).path).name,
                        text="",
                    )
        except Exception:
            pass

    def _iterate_year_selects(page) -> None:
        # Peñoles-style year dropdowns: set each year value and fire a change event.
        # The list-shaped evaluate arguments are deliberate — they cannot be confused
        # with the (str) year-discovery or ([sel, year]) click call shapes.
        try:
            selects = page.evaluate(_PW_SELECT_DISCOVERY_JS, ["__year_selects__"]) or []
        except Exception:
            selects = []
        if not selects:
            return
        print(f"  Playwright: iterating {len(selects)} year dropdown(s).", file=sys.stderr)
        for entry in selects:
            try:
                idx, values = entry[0], list(entry[1])
            except (TypeError, IndexError):
                continue
            for value in values:
                try:
                    changed = page.evaluate(_PW_SELECT_SET_JS, [idx, value, "__set_select__"])
                    if not changed:
                        continue
                    _playwright_settle(page)
                    year_hint = value if re.fullmatch(r"20\d{2}", str(value)) else None
                    _harvest(page, year_hint=year_hint)
                except Exception as exc:
                    print(f"  Playwright: select value {value!r} failed: {exc}", file=sys.stderr)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            ignore_https_errors=not verify_ssl,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            page.on("response", _on_response)
        except Exception:
            pass  # test fakes have no event API
        try:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            _harvest(page)

            # Click any "quarterly" type tabs to ensure the quarterly view is active
            # before iterating year buttons (some pages have annual/quarterly toggles).
            _playwright_click_quarterly_tabs(page)
            _harvest(page)

            # Discover year controls, trying scopes in priority order (Herdez section
            # IDs first, then page-wide). Re-query the DOM fresh per click — React
            # re-renders recreate nodes, making pre-cached element handles stale.
            btn_scope = "button"
            year_values: list[str] = []
            for scope in _PW_YEAR_SCOPES:
                found = page.evaluate(_PW_YEAR_DISCOVERY_JS, scope) or []
                if found:
                    btn_scope = scope
                    year_values = found
                    break
            print(
                f"  Playwright: year buttons in {btn_scope!r}: {year_values}",
                file=sys.stderr,
            )

            # Extract section ID for scoping load-more clicks (e.g. "#historicoreporte button" → "#historicoreporte")
            _m_scope = re.match(r"^(#[^\s,]+)", btn_scope)
            _section_sel = _m_scope.group(1) if _m_scope else ""

            for year in year_values:
                try:
                    clicked = page.evaluate(_PW_YEAR_CLICK_JS, [btn_scope, year])
                    if not clicked:
                        continue
                    _playwright_settle(page)
                    _harvest(page, year_hint=year)
                    # Click "Ver más" / load-more buttons to reveal paginated documents
                    for _ in range(8):
                        try:
                            clicked_more = page.evaluate(_PW_LOAD_MORE_JS, _section_sel)
                        except Exception:
                            clicked_more = False
                        if not clicked_more:
                            break
                        _playwright_settle(page)
                        _harvest(page, year_hint=year)
                except Exception as exc:
                    print(f"  Playwright: year {year!r} click failed: {exc}", file=sys.stderr)

            _iterate_year_selects(page)

            print(f"  Playwright: collected {len(collected)} PDF(s) total.", file=sys.stderr)
        except Exception as exc:
            print(f"  Playwright navigation failed: {exc}", file=sys.stderr)
        finally:
            context.close()
            browser.close()

    for rurl, anchor in sniffed.items():
        collected.setdefault(rurl, anchor)

    if os.environ.get("HERDEZ_DEBUG"):
        for _url, _a in sorted(collected.items(), key=lambda x: x[1].filename):
            print(f"  ANCHOR: {_a.filename}  text={_a.text[:60]!r}", file=sys.stderr)

    return list(collected.values())


def _walmex_quarterly_archive(base_url: str) -> tuple[str, dict[str, str]] | None:
    """Return Walmex's quarterly archive endpoint when the IR page is recognized."""
    parsed = urlparse(base_url)
    if "walmex.mx" not in parsed.netloc.lower():
        return None
    if "quarterly" not in parsed.path.lower():
        return None
    root = f"{parsed.scheme}://{parsed.netloc}/"
    return (
        urljoin(root, "Code/snippets/rest/Informacion"),
        {"action": "getInfoTrimestral", "lang": "en"},
    )


def _extract_release_links_from_archive(payload: dict) -> list[str]:
    """Return PDF release URLs from one quarterly archive JSON payload."""
    links: list[str] = []
    for year_block in payload.get("response", []) or []:
        for item in year_block.get("items", []) or []:
            for asset in item.get("archivos", []) or []:
                name = str(asset.get("name", "")).strip().lower()
                tipo = str(asset.get("tipo", "")).strip().lower()
                archivo = str(asset.get("archivo", "")).strip()
                if name != "release" or tipo != "pdf" or not archivo.lower().endswith(".pdf"):
                    continue
                links.append(archivo)
    return links


def _archive_page_total(payload: dict) -> int | None:
    """Best-effort extraction of the archive's reported total page count."""
    for key in ("pagetotal", "pageTotal", "total_pages", "totalPages"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            total = int(value)
        except (TypeError, ValueError):
            continue
        return total if total > 0 else None
    return None


def _response_json(resp) -> dict:
    if hasattr(resp, "json"):
        return resp.json()
    return json.loads(getattr(resp, "text", ""))


def _extract_pagination_links(html: str, base_url: str, current_url: str) -> list[str]:
    """Extract same-site pagination URLs from common HTML patterns."""
    soup = BeautifulSoup(html, "html.parser")
    current_key = _normalize_page_url(current_url)
    current_netloc = urlparse(current_url).netloc.lower()
    seen: set[str] = set()
    links: list[str] = []

    def add_candidate(raw_url: str, text: str = "", rel: str = "") -> None:
        candidate = raw_url.strip()
        if not candidate or candidate.startswith(("javascript:", "mailto:", "tel:")):
            return
        abs_url = urljoin(base_url, candidate)
        parsed = urlparse(abs_url)
        if parsed.scheme not in {"http", "https"}:
            return
        if parsed.netloc.lower() != current_netloc:
            return
        if parsed.path.lower().endswith(".pdf"):
            return
        key = _normalize_page_url(abs_url)
        if key == current_key or key in seen:
            return
        if _looks_like_pagination_url(abs_url, text=text, rel=rel):
            seen.add(key)
            links.append(abs_url)

    for tag in soup.find_all(True):
        text = tag.get_text(" ", strip=True)
        rel = " ".join(tag.get("rel") or [])
        if tag.name in {"a", "link"} and tag.get("href"):
            add_candidate(str(tag.get("href")), text=text, rel=rel)
        if rel and "next" in rel.lower():
            if tag.get("href"):
                add_candidate(str(tag.get("href")), text=text, rel=rel)
        for attr_name, attr_value in tag.attrs.items():
            if attr_name == "href":
                continue
            values = attr_value if isinstance(attr_value, list) else [attr_value]
            for value in values:
                if not isinstance(value, str):
                    continue
                lower = value.lower()
                if any(token in lower for token in ("page=", "paged=", "/page/", "offset=", "start=", "next", "siguiente", "more")):
                    for raw_url in _pdf_url_pattern().findall(value):
                        # Skip PDF URLs; this helper only returns page URLs.
                        if raw_url.lower().endswith(".pdf"):
                            continue
                    for raw_url in _pagination_url_pattern().findall(value):
                        add_candidate(raw_url, text=text, rel=rel)

    for raw_url in _pagination_url_pattern().findall(html):
        add_candidate(raw_url)

    return links


def _looks_like_pagination_url(url: str, *, text: str = "", rel: str = "") -> bool:
    haystack = " ".join(part for part in (url, text, rel) if part).lower()
    if re.search(r"(?:next|siguiente|older|more|load\s*more|page|paged|offset|start)", haystack):
        if re.search(r"(?:[?&](?:page|paged|offset|start)=\d+|/page/\d+/?|[?&]p=\d+)", url, re.IGNORECASE):
            return True
        if re.search(r"(?:next|siguiente|older|more|load\s*more)", haystack):
            return True
    return bool(re.search(r"(?:[?&](?:page|paged|offset|start)=\d+|/page/\d+/?|[?&]p=\d+)", url, re.IGNORECASE))


def _normalize_page_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


def _select_pdf_links_with_diag(
    anchors: list[_PdfAnchor],
    pattern: str | None,
) -> tuple[list[str], list[tuple[_PdfAnchor, str]]]:
    """Like _select_pdf_links, but also return (anchor, reason) for unselected candidates."""
    links = _select_pdf_links(anchors, pattern)
    chosen = set(links)
    rejected = [
        (a, _explain_rejection(a))
        for a in anchors
        if a.url not in chosen
    ]
    return links, rejected


# --- English-only language filtering ----------------------------------------------------
# When a source declares ``language: en`` we keep only English-looking PDF anchors. Signals are
# read off the same ``unquote(filename + text + url).lower()`` haystack the quarterly-report
# heuristic uses, so a Spanish-only report ("reporte trimestral", "/es/") is dropped while an
# English one ("results", "/en/") survives. The active spec is carried in a ContextVar so the
# single chokepoint ``_select_pdf_links`` (which every discovery strategy funnels through) can
# apply it without threading a parameter through all six crawlers.

_EN_PATH_RE = re.compile(r"(?:/en/|/eng/|/english/|[_-]en[_./-]|[_-]eng[_./-])", re.IGNORECASE)
_ES_PATH_RE = re.compile(r"(?:/es/|/esp/|/spanish/|[_-]es[_./-]|[_-]esp[_./-])", re.IGNORECASE)
_EN_WORD_RE = re.compile(r"\b(?:report|reports|results?|earnings?|quarter(?:ly)?|english"
                         r"|press\s+release|financial\s+statements)\b", re.IGNORECASE)
_ES_WORD_RE = re.compile(r"(?:reporte|trimestre|trimestral|resultados|informe|espa[nñ]ol"
                         r"|comunicado|estados?\s+financieros)", re.IGNORECASE)

# Active English-only spec: None (no filtering) or {"include": re|None, "exclude": re|None}.
_LANG_FILTER: "contextvars.ContextVar[dict | None]" = contextvars.ContextVar(
    "downloader_lang_filter", default=None
)
# "quarterly" (default) or "annual" — selects which report selectors _select_pdf_links uses.
_DOC_KIND: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "downloader_doc_kind", default="quarterly"
)


def _is_english_only(language: str | None) -> bool:
    return (language or "").strip().lower() in ("en", "eng", "english")


def _build_lang_spec(
    language: str | None, include: str | None, exclude: str | None
) -> dict | None:
    """Build the active language-filter spec for a ``download_from_ir`` run.

    Explicit ``include``/``exclude`` regexes win (and force filtering on). Otherwise English-only
    sources get the built-in EN-keep/ES-drop heuristic, and everything else is a no-op (``None``).
    """
    if include or exclude:
        return {
            "include": re.compile(include, re.IGNORECASE) if include else None,
            "exclude": re.compile(exclude, re.IGNORECASE) if exclude else None,
        }
    if _is_english_only(language):
        return {"include": None, "exclude": None}  # built-in heuristic
    return None


def _filter_anchors_by_language(
    anchors: list[_PdfAnchor], spec: dict | None
) -> list[_PdfAnchor]:
    """Drop non-English anchors per ``spec`` (set by :func:`download_from_ir`).

    ``spec`` of ``None`` is a no-op (preserves behavior for Spanish/untagged sources). When
    ``include``/``exclude`` regexes are supplied they win; otherwise the built-in EN-keep /
    ES-drop heuristic applies: drop an anchor only when it shows a Spanish signal and no English
    one (so bilingual filenames under an ``/en/`` path are kept).
    """
    if not spec or not anchors:
        return anchors
    include = spec.get("include")
    exclude = spec.get("exclude")
    kept: list[_PdfAnchor] = []
    for a in anchors:
        hay = unquote(" ".join((a.filename, a.text, a.url))).lower()
        if include is not None or exclude is not None:
            if include is not None and not include.search(hay):
                continue
            if exclude is not None and exclude.search(hay):
                continue
            kept.append(a)
            continue
        has_en = bool(_EN_PATH_RE.search(hay) or _EN_WORD_RE.search(hay))
        has_es = bool(_ES_PATH_RE.search(hay) or _ES_WORD_RE.search(hay))
        if has_es and not has_en:
            continue
        kept.append(a)
    return kept


def _select_pdf_links(anchors: list[_PdfAnchor], pattern: str | None) -> list[str]:
    """Choose the best PDF links using hidden, ordered selectors.

    Reads the active ``_DOC_KIND`` (set per run in :func:`download_from_ir`): ``"annual"`` keeps
    annual/integrated/20-F links the quarterly path drops; the default is the old behavior.
    """
    anchors = _filter_anchors_by_language(anchors, _LANG_FILTER.get())
    if not anchors:
        return []

    if pattern:
        configured = _match_selector(anchors, pattern)
        if configured:
            return configured

    if _DOC_KIND.get() == "annual":
        # No permissive last resort: only anchors that actually look annual, so a mixed page
        # never leaks quarterly PDFs into the annual pass.
        return _select_annual_report_links(anchors)

    quarterly_reports = _select_quarterly_report_links(anchors)
    if quarterly_reports:
        return quarterly_reports

    selectors: list[tuple[str, str]] = [
        ("release", r"(?:^|[^a-z])release(?:[^a-z]|$)"),
    ]
    selectors.extend([
        ("earnings_release", r"(?:earnings?\s+release|release\s+earnings?)"),
        ("results", r"\bresults?\b"),
        ("report", r"\breport\b"),
        ("quarterly", r"(?:quarterly|quarter|trimestral|trimestre|informe|reporte|earning)"),
    ])

    for _, selector in selectors:
        matched = _match_selector(anchors, selector)
        if matched:
            return matched

    return [a.url for a in anchors if not _is_excluded_report_asset(a)]


def _select_quarterly_report_links(anchors: list[_PdfAnchor]) -> list[str]:
    seen: set[str] = set()
    selected: list[str] = []
    for anchor in anchors:
        if not _looks_like_quarterly_report(anchor):
            continue
        if anchor.url in seen:
            continue
        seen.add(anchor.url)
        selected.append(anchor.url)
    return selected


_QR_EXCLUDE_RE = re.compile(
    r"(?:webcast|transcript|script|presentation|presentaci[oó]n|infograf|infographic|postcard|carrusel"
    r"|informe.{0,8}anual|reporte.{0,8}anual|(?<![a-z])annual(?![a-z])"
    r"|sostenibilidad|sustainability|carta.{0,12}accionistas|gobierno.{0,12}corporativo"
    r"|governance|(?<![a-z])proxy(?![a-z])|prospecto|prospectus|xbrl?"
    # Conference-call invitations carry a period in their name but are never the report.
    # (Re-transmission / eventos-relevantes docs are NOT excluded here: an audited
    # "retransmisión ... cifras dictaminadas" can be the real report. Those are handled by
    # de-prioritising — not dropping — in the Wayback recovery instead.)
    r"|conference[-_\s]*call|conferencia[-_\s]*telef[oó]nica|invitation|invitaci[oó]n)",
    re.IGNORECASE,
)

_QR_REPORT_SIGNAL_RE = re.compile(
    r"(?:(?<![a-z])bmv(?![a-z])|release|results?|earnings?|report(?:e|s)?|quarter|comunicado|trimestr"
    r"|(?<![a-z])notas?(?![a-z])|estados?.{0,3}financieros?"
    r"|(?:^|[^0-9a-z])[1-4][_][tq]d[_]\d{2,4}(?:[^0-9a-z]|$)"
    r"|(?:^|[^0-9a-z])[1-4][_][tq]\d{2,4}(?:[^0-9a-z]|$))",
    re.IGNORECASE,
)

_QR_QUARTER_SIGNAL_RE = re.compile(
    r"(?:"
    r"(?:^|[^0-9a-z])[1-4][tq]\d{2}(?!\d)"
    r"|(?:^|[^0-9a-z])[1-4][tq]\d{2,4}(?=[^0-9a-z]|[a-z]*\.pdf|$)"
    r"|(?:^|[^0-9a-z])[1-4][tq][-_. ]?20\d{2}(?:[^0-9a-z]|$)"
    r"|(?:^|[^0-9a-z])[1-4][tq][-_. ]\d{2}(?:[^0-9a-z]|$)"
    r"|(?:first|second|third|fourth)[-_ ]*quarter(?:[-_ ]*and[-_ ]*year)?[-_ ]*20\d{2}"
    r"|(?:^|[^0-9a-z])[1-4][_\s][tq]d[_\s]20\d{2}"
    r"|(?:^|[^0-9a-z])20\d{2}[-_. ]?[tq][1-4](?:[^0-9a-z]|$)"
    r"|(?:^|[^0-9a-z])20\d{2}[-_. ]?[1-4][tq](?:[^0-9a-z]|$)"
    r"|(?:^|[^0-9a-z])[1-4](?:er|do|to)?[-_ ]*trimestre[-_ ]*(?:y[-_ ]*a[nñ]o[-_ ]*)?20\d{2}"
    r"|(?:primer|segundo|tercer|cuarto)[-_ ]*trimestre[-_ ]*(?:y[-_ ]*a[nñ]o[-_ ]*)?20\d{2}"
    r"|(?:^|[^0-9a-z])q[1-4][-_. ]?20\d{2}(?:[^0-9a-z]|$)"
    r"|(?:^|[^0-9a-z])20\d{2}[-_. ]?q[1-4](?:[^0-9a-z]|$)"
    r"|(?:^|[^0-9a-z])[1-4](?:er|do|to)?[-_ ]*trim(?:estre)?[-_ .]*20\d{2}"
    r"|(?:^|[^0-9a-z])[1-4][_\s][tq][_\s](?:quarter|trimestre|results?|report)"
    r"|(?:^|[^0-9a-z])[1-4][_][tq]\d{2,4}(?:[^0-9a-z]|$)"
    r")",
    re.IGNORECASE,
)


def _looks_like_quarterly_report(anchor: _PdfAnchor) -> bool:
    haystack = unquote(" ".join((anchor.filename, anchor.text, anchor.url))).lower()
    if _QR_EXCLUDE_RE.search(haystack):
        return False
    return bool(_QR_REPORT_SIGNAL_RE.search(haystack) and _QR_QUARTER_SIGNAL_RE.search(haystack))


def _is_excluded_report_asset(anchor: _PdfAnchor) -> bool:
    haystack = unquote(" ".join((anchor.filename, anchor.text, anchor.url))).lower()
    return bool(_QR_EXCLUDE_RE.search(haystack))


# --- Annual-report selection (doc_kind="annual") -------------------------------------------
# Same non-report exclusions as the quarterly path EXCEPT the annual/anual clauses — which are the
# whole point in annual mode. An annual report is year-only, so it requires an annual signal + a
# year rather than a quarter signal.
_AR_EXCLUDE_RE = re.compile(
    r"(?:webcast|transcript|script|presentation|presentaci[oó]n|infograf|infographic|postcard|carrusel"
    r"|sostenibilidad|sustainability|carta.{0,12}accionistas|gobierno.{0,12}corporativo"
    r"|governance|(?<![a-z])proxy(?![a-z])|prospecto|prospectus|xbrl?"
    r"|conference[-_\s]*call|conferencia[-_\s]*telef[oó]nica|invitation|invitaci[oó]n)",
    re.IGNORECASE,
)
_AR_REPORT_SIGNAL_RE = re.compile(
    r"(?:informe.{0,8}anual|reporte.{0,8}anual|integrated.{0,8}(?:annual.{0,8})?report"
    r"|annual.{0,8}report|(?<![a-z])annual(?![a-z])|(?<![a-z])anual(?![a-z])|\b20-?f\b)",
    re.IGNORECASE,
)
_AR_YEAR_SIGNAL_RE = re.compile(r"(?<!\d)20\d{2}(?!\d)")


def _looks_like_annual_report(anchor: _PdfAnchor) -> bool:
    haystack = unquote(" ".join((anchor.filename, anchor.text, anchor.url))).lower()
    if _AR_EXCLUDE_RE.search(haystack):
        return False
    return bool(_AR_REPORT_SIGNAL_RE.search(haystack) and _AR_YEAR_SIGNAL_RE.search(haystack))


def _is_excluded_annual_asset(anchor: _PdfAnchor) -> bool:
    haystack = unquote(" ".join((anchor.filename, anchor.text, anchor.url))).lower()
    return bool(_AR_EXCLUDE_RE.search(haystack))


def _select_annual_report_links(anchors: list[_PdfAnchor]) -> list[str]:
    seen: set[str] = set()
    selected: list[str] = []
    for anchor in anchors:
        if not _looks_like_annual_report(anchor):
            continue
        if anchor.url in seen:
            continue
        seen.add(anchor.url)
        selected.append(anchor.url)
    return selected


def _explain_rejection(anchor: _PdfAnchor) -> str:
    """Explain why an anchor does not pass the quarterly-report filter (for diagnostics)."""
    haystack = unquote(" ".join((anchor.filename, anchor.text, anchor.url))).lower()
    excluded = _QR_EXCLUDE_RE.search(haystack)
    if excluded:
        return f"excluded (matched {excluded.group(0)!r})"
    missing = []
    if not _QR_REPORT_SIGNAL_RE.search(haystack):
        missing.append("report signal (e.g. 'reporte', 'results', 'bmv')")
    if not _QR_QUARTER_SIGNAL_RE.search(haystack):
        missing.append("quarter signal (e.g. '1T24', '1T-2026', 'Q1 2024')")
    if missing:
        return "missing " + " and ".join(missing)
    return "passes quarterly filter"


def _match_selector(anchors: list[_PdfAnchor], pattern: str) -> list[str]:
    excluded = (_is_excluded_annual_asset if _DOC_KIND.get() == "annual"
                else _is_excluded_report_asset)
    seen: set[str] = set()
    selected: list[str] = []
    for anchor in anchors:
        if excluded(anchor):
            continue
        haystacks = (anchor.filename, anchor.text, anchor.url)
        if not any(re.search(pattern, hay, re.IGNORECASE) for hay in haystacks if hay):
            continue
        if anchor.url in seen:
            continue
        seen.add(anchor.url)
        selected.append(anchor.url)
    return selected


def _download_pdf(
    session: requests.Session,
    url: str,
    dest: Path,
    verify_ssl: bool = True,
    chunk_size: int = 65536,
    referer: str | None = None,
) -> Path:
    """Download a single PDF and verify it's a real PDF file."""
    request_headers = {"Accept": "application/pdf,*/*"}
    if referer:
        request_headers["Referer"] = referer
    tmp = dest.with_suffix(".tmp")
    profile = _impersonate_profile(session)
    if profile:
        # Known Cloudflare-protected host: fetch with a browser TLS fingerprint up front.
        _impersonate_download(
            url,
            tmp,
            timeout=60,
            profile=profile,
            headers=request_headers,
            session_headers=dict(getattr(session, "headers", {})),
            verify_ssl=verify_ssl,
        )
    else:
        try:
            resp = session.get(
                url,
                stream=True,
                timeout=60,
                verify=verify_ssl,
                headers=request_headers,
            )
            resp.raise_for_status()
            # Write to a temp file first, then rename on success
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if chunk:
                        fh.write(chunk)
        except requests.RequestException as exc:
            if _is_cloudflare_block(getattr(exc, "response", None)):
                _impersonate_download(
                    url,
                    tmp,
                    timeout=60,
                    profile=_DEFAULT_IMPERSONATE,
                    headers=request_headers,
                    session_headers=dict(getattr(session, "headers", {})),
                    verify_ssl=verify_ssl,
                )
            elif _should_fallback_to_curl(exc):
                _curl_download(
                    url,
                    tmp,
                    headers=request_headers,
                    session_headers=dict(getattr(session, "headers", {})),
                    verify_ssl=verify_ssl,
                    timeout=60,
                )
            else:
                raise

    # Validate: PDF magic bytes
    with tmp.open("rb") as fh:
        header = fh.read(5)
    if not header.startswith(b"%PDF-"):
        tmp.unlink()
        raise ValueError(f"Downloaded file is not a PDF (header: {header!r})")

    tmp.rename(dest)
    return dest


def _session_get(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    verify_ssl: bool,
    params: dict[str, str] | None = None,
    stream: bool = False,
    extra_headers: dict | None = None,
) -> requests.Response:
    profile = _impersonate_profile(session)
    if profile:
        # Known Cloudflare-protected host: fetch with a browser TLS fingerprint up front.
        resp = _impersonate_get(
            url,
            timeout=timeout,
            profile=profile,
            params=params,
            extra_headers=extra_headers,
            session_headers=dict(getattr(session, "headers", {})),
            verify_ssl=verify_ssl,
        )
        resp.raise_for_status()
        return resp

    request_kwargs: dict = {
        "timeout": timeout,
        "verify": verify_ssl,
        "stream": stream,
    }
    if params is not None:
        request_kwargs["params"] = params
    if extra_headers:
        request_kwargs["headers"] = extra_headers

    try:
        resp = session.get(url, **request_kwargs)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        if _is_cloudflare_block(getattr(exc, "response", None)):
            resp = _impersonate_get(
                url,
                timeout=timeout,
                profile=_DEFAULT_IMPERSONATE,
                params=params,
                extra_headers=extra_headers,
                session_headers=dict(getattr(session, "headers", {})),
                verify_ssl=verify_ssl,
            )
            resp.raise_for_status()
            return resp
        if not _should_fallback_to_curl(exc):
            raise
        return _curl_get(session, url, timeout=timeout, verify_ssl=verify_ssl, params=params, extra_headers=extra_headers)


def _curl_get(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
    verify_ssl: bool,
    params: dict[str, str] | None = None,
    extra_headers: dict | None = None,
) -> requests.Response:
    body, final_url, status_code = _curl_fetch_text(
        url,
        timeout=timeout,
        verify_ssl=verify_ssl,
        params=params,
        extra_headers=extra_headers,
        session_headers=dict(getattr(session, "headers", {})),
    )
    return _CurlResponse(text=body, url=final_url, status_code=status_code)


def _curl_download(
    url: str,
    dest: Path,
    *,
    timeout: int,
    verify_ssl: bool,
    headers: dict | None = None,
    session_headers: dict | None = None,
) -> None:
    _curl_fetch_to_path(
        url,
        dest,
        timeout=timeout,
        verify_ssl=verify_ssl,
        extra_headers=headers,
        session_headers=session_headers,
    )


def _curl_fetch_text(
    url: str,
    *,
    timeout: int,
    verify_ssl: bool,
    params: dict[str, str] | None = None,
    extra_headers: dict | None = None,
    session_headers: dict[str, str] | None = None,
) -> tuple[str, str, int]:
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        final_url, status_code = _run_curl(
            url,
            output_path=tmp_path,
            timeout=timeout,
            verify_ssl=verify_ssl,
            params=params,
            extra_headers=extra_headers,
            session_headers=session_headers,
        )
        return tmp_path.read_text(encoding="utf-8", errors="replace"), final_url, status_code
    finally:
        tmp_path.unlink(missing_ok=True)


def _curl_fetch_to_path(
    url: str,
    output_path: Path,
    *,
    timeout: int,
    verify_ssl: bool,
    params: dict[str, str] | None = None,
    extra_headers: dict | None = None,
    session_headers: dict[str, str] | None = None,
) -> str:
    final_url, status_code = _run_curl(
        url,
        output_path=output_path,
        timeout=timeout,
        verify_ssl=verify_ssl,
        params=params,
        extra_headers=extra_headers,
        session_headers=session_headers,
    )
    if status_code >= 400:
        output_path.unlink(missing_ok=True)
        raise RuntimeError(f"HTTP {status_code} fetching {url}")
    return final_url


def _run_curl(
    url: str,
    *,
    output_path: Path,
    timeout: int,
    verify_ssl: bool,
    params: dict[str, str] | None = None,
    extra_headers: dict | None = None,
    session_headers: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Run curl and return (final_url, http_status_code)."""
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("TLS fallback requires `curl`, but it was not found on PATH.")

    command = [
        curl,
        "-sSL",
        "--compressed",
        "--max-time",
        str(timeout),
        "-o",
        str(output_path),
        "-w",
        "%{url_effective}\n%{http_code}",
    ]
    if not verify_ssl:
        command.append("-k")
    for key, value in _merged_headers(session_headers, extra_headers).items():
        command.extend(["-H", f"{key}: {value}"])
    if params:
        query = urlencode(params, doseq=True)
        url = f"{url}{'&' if '?' in url else '?'}{query}"
    command.append(url)

    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(f"curl fetch failed for {url}: {exc}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.strip() if completed.stderr else f"exit code {completed.returncode}"
        raise RuntimeError(f"curl fetch failed for {url}: {stderr}")

    parts = [p for p in completed.stdout.splitlines() if p.strip()]
    final_url = url
    status_code = 200
    if len(parts) >= 2:
        final_url = parts[-2].strip() or url
        try:
            status_code = int(parts[-1].strip())
        except ValueError:
            pass
    elif len(parts) == 1:
        try:
            status_code = int(parts[0].strip())
        except ValueError:
            final_url = parts[0].strip() or url

    return final_url or url, status_code


def _merged_headers(session_headers: dict | None, extra_headers: dict | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    for source in (session_headers or {}, extra_headers or {}):
        for key, value in source.items():
            merged[str(key)] = str(value)
    return merged


def _should_fallback_to_curl(exc: Exception) -> bool:
    if not isinstance(exc, requests.RequestException):
        return False
    message = str(exc).lower()
    tokens = (
        "tlsv1 alert protocol version",
        "wrong version number",
        "sslv3 alert handshake failure",
        "handshake failure",
        "protocol version",
        "certificate verify failed",
        "eof occurred in violation of protocol",
        "dh key too small",
        "unsafe legacy renegotiation",
        "bad handshake",
        "connection reset",
        "tls",
        "ssl",
    )
    return any(token in message for token in tokens)


@dataclass(frozen=True)
class _CurlResponse:
    text: str
    url: str
    status_code: int = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} from {self.url}")

    def json(self):
        return json.loads(self.text)


# ---------------------------------------------------------------------------
# Cloudflare bot-wall bypass via browser TLS impersonation (curl_cffi)
# ---------------------------------------------------------------------------
#
# Some IR hosts (e.g. organizacionsoriana.com) sit behind a Cloudflare WAF that
# blocks on the client's TLS/JA3 fingerprint, returning HTTP 403 to *any* plain
# HTTP client — `requests` and a `curl` subprocess alike — regardless of
# User-Agent or headers. A real browser fingerprint passes; `curl_cffi` replays
# one. We try `requests` first (fast, no native deps in the hot path) and fall
# back to impersonation only when a Cloudflare block is detected, or jump
# straight to it when a host is known-protected (session._impersonate_profile).

_DEFAULT_IMPERSONATE = "chrome"


def _require_curl_cffi():
    """Import curl_cffi.requests, raising a clear, actionable error if it is absent."""
    try:
        from curl_cffi import requests as cffi_requests  # noqa: PLC0415 — optional dep
    except ImportError as exc:  # pragma: no cover — environment-dependent
        raise RuntimeError(
            "Browser TLS impersonation requires curl_cffi, which is not installed. "
            "Run: pip install curl_cffi"
        ) from exc
    return cffi_requests

# Status codes Cloudflare uses for blocks / managed challenges.
_CF_BLOCK_STATUSES = {403, 429, 503}

# Markers in the response body of a Cloudflare interstitial / block page.
_CF_BODY_MARKERS = (
    "attention required",
    "sorry, you have been blocked",
    "cloudflare ray id",
    "cf-error",
    "/cdn-cgi/",
)


def _is_cloudflare_block(resp) -> bool:
    """True when ``resp`` looks like a Cloudflare bot-wall block/challenge.

    Accepts a ``requests.Response`` (including the one attached to a raised
    ``HTTPError`` via ``exc.response``) or None. Checks cheap header/cookie
    signals before reading the body.
    """
    if resp is None:
        return False
    status = getattr(resp, "status_code", None)
    if status not in _CF_BLOCK_STATUSES:
        return False
    headers = getattr(resp, "headers", {}) or {}
    server = str(headers.get("Server", "")).lower()
    if "cloudflare" in server or headers.get("cf-ray") or headers.get("CF-RAY"):
        return True
    cookies = getattr(resp, "cookies", None)
    if cookies is not None and "__cf_bm" in cookies:
        return True
    try:
        body = (resp.text or "").lower()
    except Exception:  # noqa: BLE001 — body may be unreadable on a streamed error
        return False
    return any(marker in body for marker in _CF_BODY_MARKERS)


def _impersonate_profile(session: requests.Session | None) -> str | None:
    """Return the impersonation profile stashed on a session, if any."""
    return getattr(session, "_impersonate_profile", None)


def _impersonate_get(
    url: str,
    *,
    timeout: int,
    profile: str | None = None,
    params: dict[str, str] | None = None,
    extra_headers: dict | None = None,
    session_headers: dict | None = None,
    verify_ssl: bool = True,
) -> _CurlResponse:
    """Fetch ``url`` with a browser TLS fingerprint via curl_cffi.

    Returns a ``_CurlResponse`` so callers that expect a ``requests``-like text
    response are unaffected.
    """
    _cffi_requests = _require_curl_cffi()

    headers = _merged_headers(session_headers, extra_headers) or None
    resp = _cffi_requests.get(
        url,
        impersonate=profile or _DEFAULT_IMPERSONATE,
        timeout=timeout,
        params=params,
        headers=headers,
        verify=verify_ssl,
    )
    return _CurlResponse(
        text=resp.text,
        url=str(getattr(resp, "url", url)),
        status_code=resp.status_code,
    )


def _impersonate_download(
    url: str,
    dest: Path,
    *,
    timeout: int,
    profile: str | None = None,
    headers: dict | None = None,
    session_headers: dict | None = None,
    verify_ssl: bool = True,
) -> None:
    """Download ``url`` to ``dest`` with a browser TLS fingerprint via curl_cffi.

    Non-streaming: report PDFs are at most a few MB, so the body fits in memory.
    """
    _cffi_requests = _require_curl_cffi()

    merged = _merged_headers(session_headers, headers) or None
    resp = _cffi_requests.get(
        url,
        impersonate=profile or _DEFAULT_IMPERSONATE,
        timeout=timeout,
        headers=merged,
        verify=verify_ssl,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} fetching {url}")
    dest.write_bytes(resp.content)


def _slugify_url(
    url: str,
    used_names: set[str] | None = None,
    hint_filename: str | None = None,
) -> str:
    """Derive a filesystem-safe filename from a PDF URL.

    If ``hint_filename`` is provided (e.g. a year-injected name from the
    Playwright layer), it is used instead of the URL path basename.
    """
    parsed = urlparse(url)
    if hint_filename:
        name = _clean_filename_part(hint_filename)
    else:
        path_parts = [part for part in Path(parsed.path).parts if part not in {"/", ""}]
        name = _clean_filename_part(path_parts[-1]) if path_parts else "download"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"

    candidate = name
    if used_names is not None and candidate in used_names:
        if not hint_filename:
            path_parts = [part for part in Path(parsed.path).parts if part not in {"/", ""}]
            context_parts = [_clean_filename_part(part) for part in path_parts[-3:-1]]
            context = "_".join(part for part in context_parts if part)
            if context:
                candidate = f"{context}_{name}"
        if candidate in used_names:
            digest = hashlib.sha1((parsed.path + "?" + parsed.query).encode("utf-8")).hexdigest()[:8]
            candidate = f"{Path(name).stem}_{digest}.pdf"
        counter = 2
        while candidate in used_names:
            candidate = f"{Path(candidate).stem}_{counter}.pdf"
            counter += 1

    if used_names is not None:
        used_names.add(candidate)
    return candidate


def _clean_filename_part(value: str) -> str:
    value = unquote(value.strip())
    if not value:
        return ""
    value = re.sub(r"[?#&].*$", "", value)
    value = re.sub(r"[^\w.\-]+", "_", value)
    return value.strip("._-") or "file"


_PDF_URL_RE = re.compile(
    r"(?P<url>(?:https?:)?//[^\s\"'<>]+?\.pdf(?:[?#][^\s\"'<>]*)?|/[^\s\"'<>]+?\.pdf(?:[?#][^\s\"'<>]*)?|"
    r"[A-Za-z0-9._~%/-]+\.pdf(?:[?#][^\s\"'<>]*)?)",
    re.IGNORECASE,
)

_PAGINATION_URL_RE = re.compile(
    r"(?P<url>(?:https?:)?//[^\s\"'<>]+?(?:[?&](?:page|paged|offset|start|p)=\d+|/page/\d+/?)"
    r"[^\s\"'<>]*|/[^\s\"'<>]+?(?:[?&](?:page|paged|offset|start|p)=\d+|/page/\d+/?)"
    r"[^\s\"'<>]*|\?[^\s\"'<>]*?(?:page|paged|offset|start|p)=\d+[^\s\"'<>]*)",
    re.IGNORECASE,
)


def _pdf_url_pattern() -> re.Pattern[str]:
    return _PDF_URL_RE


def _pagination_url_pattern() -> re.Pattern[str]:
    return _PAGINATION_URL_RE


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Download PDF reports from an Investor Relations webpage."
    )
    ap.add_argument("--url", required=True, help="IR page URL")
    ap.add_argument("--out", default="downloads", help="Output directory (default: ./downloads)")
    ap.add_argument("--filter", metavar="TEXT",
                    help="Only download PDFs whose URL contains TEXT (e.g. '2024')")
    ap.add_argument("--max", type=int, default=50, help="Max reports to download (default: 50)")
    ap.add_argument("--pattern", default=None, help="Regex for PDF filename filter")
    ap.add_argument("--delay", type=int, default=500,
                    help="Milliseconds between downloads (default: 500)")
    ap.add_argument("--no-verify-ssl", action="store_true",
                    help="Disable SSL certificate verification")
    ap.add_argument("--year-api-url", action="append", metavar="URL", dest="year_api_urls",
                    help="JSON API URL to fetch report data from (repeatable; for JS-rendered sites)")
    ap.add_argument("--playwright", action="store_true",
                    help="Use Playwright headless browser for JS-rendered pages "
                         "(requires: pip install playwright && playwright install chromium)")
    ap.add_argument("--browser-first", action="store_true",
                    help="Run Playwright discovery before the initial HTTP fetch "
                         "(for browser-gated IR pages)")
    ap.add_argument("--impersonate", metavar="PROFILE", default=None,
                    help="Replay a browser TLS fingerprint via curl_cffi (e.g. 'chrome') "
                         "for hosts behind a Cloudflare bot wall")
    args = ap.parse_args()

    kwargs: dict = {}
    if args.pattern:
        kwargs["file_pattern"] = args.pattern
    if args.year_api_urls:
        kwargs["year_api_urls"] = args.year_api_urls
    if args.playwright:
        kwargs["use_playwright"] = True
    if args.browser_first:
        kwargs["browser_first"] = True
    if args.impersonate:
        kwargs["impersonate"] = args.impersonate

    paths = download_from_ir(
        url=args.url,
        output_dir=Path(args.out),
        period_filter=args.filter,
        max_reports=args.max,
        delay_ms=args.delay,
        verify_ssl=not args.no_verify_ssl,
        **kwargs,
    )
    if paths:
        print(f"\n{len(paths)} file(s) saved to {args.out}/")
    else:
        print("No files downloaded.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
