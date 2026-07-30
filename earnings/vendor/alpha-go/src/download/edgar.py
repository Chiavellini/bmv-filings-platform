"""edgar.py — fetch English earnings releases from SEC EDGAR for foreign issuers (6-K filers).

FEMSA (and many Mexican issuers cross-listed in the US) file their English quarterly earnings
releases with the SEC as the **EX-99.1 exhibit of a 6-K**. EDGAR is static, free, and reliable
(no JS/Playwright, unlike the issuer IR sites), which makes it a far better English source than
scraping ``femsa.gcs-web.com``.

Pipeline: ``iter_filings`` (submissions API, all 6-K shards) → ``select_earnings_exhibit`` (pick
the EX-99.1) → ``fetch_text`` → ``parse_period_label`` (read "1Q 2025 Results" off the header).
The pure helpers (selection + period parsing + HTML→text) are unit-testable offline; the
network functions are thin and marked for opt-in integration runs.

SEC fair-access: send a descriptive User-Agent with contact info and stay well under 10 req/s.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

_DELAY_S = 0.2  # polite spacing between SEC requests

_EX99_RE = re.compile(r"ex[-_]?99", re.IGNORECASE)
# "1Q 2025", "4Q and Full Year 2024", with the quarter then (eventually) the 4-digit year.
_QUARTER_RE = re.compile(r"\b([1-4])Q\b[^0-9]{0,24}(20\d{2})", re.IGNORECASE)
_WORD_QUARTER_RE = re.compile(r"\b(first|second|third|fourth)\s+quarter[^0-9]{0,24}(20\d{2})",
                              re.IGNORECASE)
_WORD_Q = {"first": "1", "second": "2", "third": "3", "fourth": "4"}


@dataclass
class Filing:
    form: str
    filing_date: str          # YYYY-MM-DD
    accession: str            # dashed, e.g. 0001104659-25-039841
    primary_doc: str


# --- pure helpers (offline-testable) ------------------------------------------------------

def select_earnings_exhibit(items: "list[tuple[str, int]]") -> str | None:
    """From ``(filename, size)`` pairs in a filing, pick the earnings-release exhibit.

    The release is the EX-99.1 document: prefer a name matching ``ex99`` (largest such), else the
    largest ``.htm``/``.html`` that is not the index or the small 6-K cover page. Returns None when
    nothing plausible is present.
    """
    htm = [(n, s) for n, s in items
           if n.lower().endswith((".htm", ".html")) and "index" not in n.lower()]
    if not htm:
        return None
    ex99 = [(n, s) for n, s in htm if _EX99_RE.search(n)]
    pool = ex99 or [(n, s) for n, s in htm if "cover" not in n.lower() and "_6k" not in n.lower()]
    pool = pool or htm
    return max(pool, key=lambda ns: ns[1])[0]


def parse_period_label(text: str) -> str | None:
    """Read a canonical period (``YYYY-NT``) from an earnings-release header.

    Handles "1Q 2025 Results" and "4Q and Full Year 2024 Results" (Q4 of the stated year), plus
    the spelled-out "First Quarter 2025" form. Looks only at the opening of the document.
    """
    head = " ".join(text[:600].split())
    m = _QUARTER_RE.search(head) or _WORD_QUARTER_RE.search(head)
    if not m:
        return None
    q = m.group(1)
    q = _WORD_Q.get(q.lower(), q)
    return f"{m.group(2)}-{q}T"


# 20-F cover: "For the fiscal year ended December 31, 2024". Non-greedy across the date (which
# itself carries digits like "31") to reach the 4-digit fiscal year.
_FISCAL_YEAR_RE = re.compile(r"fiscal year ended.{0,40}?(20\d{2})", re.IGNORECASE)


def select_annual_document(items: "list[tuple[str, int]]") -> str | None:
    """From ``(filename, size)`` pairs in a filing, pick the 20-F annual body.

    Unlike a 6-K, a 20-F's report *is* the primary filing document (there is no EX-99.1). Take the
    largest ``.htm``/``.html`` that is not the index or the cover page. Returns None if none present.
    """
    htm = [(n, s) for n, s in items
           if n.lower().endswith((".htm", ".html")) and "index" not in n.lower()]
    if not htm:
        return None
    pool = [(n, s) for n, s in htm if "cover" not in n.lower()] or htm
    return max(pool, key=lambda ns: ns[1])[0]


def annual_period_label(text: str, filing_date: str | None = None) -> str | None:
    """Canonical annual period (``YYYY-FY``) for a 20-F.

    Prefer the fiscal year stated on the cover ("fiscal year ended … 2024"); otherwise infer it
    from the filing date (a 20-F is filed the year after the fiscal close, so ``filing_year - 1``).
    """
    head = " ".join((text or "")[:2000].split())
    m = _FISCAL_YEAR_RE.search(head)
    if m:
        return f"{m.group(1)}-FY"
    if filing_date and filing_date[:4].isdigit():
        return f"{int(filing_date[:4]) - 1}-FY"
    return None


def html_to_text(html: str) -> str:
    return clean_release_text(BeautifulSoup(html, "html.parser").get_text("\n", strip=True))


# EDGAR prepends a document separator ("EX-99.1  2  <file>.htm  EXHIBIT 99.1  Exhibit 99.1")
# before the actual release. Strip it (bounded to the opening) so the corpus starts at the report.
_EXHIBIT_PREFIX_RE = re.compile(
    r"(?is)^.{0,160}?exhibit\s+99\.1\s+(?=[1-4]Q|first|second|third|fourth|highlights)"
)


def clean_release_text(text: str) -> str:
    return _EXHIBIT_PREFIX_RE.sub("", text, count=1).lstrip()


# --- network (opt-in integration) ---------------------------------------------------------

def _request_headers() -> dict[str, str]:
    user_agent = os.environ.get("SEC_EDGAR_USER_AGENT", "").strip()
    if not user_agent:
        raise RuntimeError(
            "SEC_EDGAR_USER_AGENT is required for EDGAR network access; "
            "set it to a descriptive application name and monitored contact address"
        )
    return {"User-Agent": user_agent}


def _get(url: str) -> requests.Response:
    time.sleep(_DELAY_S)
    r = requests.get(url, headers=_request_headers(), timeout=30)
    r.raise_for_status()
    return r


def iter_filings(cik: str, forms: "tuple[str, ...]" = ("6-K",)) -> "list[Filing]":
    """All filings of the given ``forms`` for a zero-padded ``cik`` (recent + older shards)."""
    cik = cik.zfill(10)
    base = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()
    shards = [base["filings"]["recent"]]
    for extra in base["filings"].get("files", []):
        shards.append(_get(f"https://data.sec.gov/submissions/{extra['name']}").json())
    out: list[Filing] = []
    for s in shards:
        for form, date, acc, doc in zip(
            s["form"], s["filingDate"], s["accessionNumber"], s["primaryDocument"]
        ):
            if form in forms:
                out.append(Filing(form, date, acc, doc))
    return out


def filing_documents(cik: str, accession: str) -> "list[tuple[str, int]]":
    """``(filename, size)`` pairs for one filing's directory."""
    acc = accession.replace("-", "")
    idx = _get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/index.json").json()
    return [(it["name"], int(it.get("size") or 0)) for it in idx["directory"]["item"]]


def fetch_document_html(cik: str, accession: str, name: str) -> str:
    """Fetch the original SEC exhibit markup for portable corpus preservation."""
    acc = accession.replace("-", "")
    return _get(f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}/{name}").text


def fetch_document_text(cik: str, accession: str, name: str) -> str:
    """Fetch an SEC exhibit and return its normalized text for indexing."""
    return html_to_text(fetch_document_html(cik, accession, name))
