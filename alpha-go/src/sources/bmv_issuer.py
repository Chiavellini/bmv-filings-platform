"""Discovery adapter for BMV issuer pages (annual PDFs and relevant-event attachments)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from src.download.downloader import _make_session, _session_get
from src.sources.base import SourceRecord

BMV_BASE = "https://www.bmv.com.mx"
ISSUER_DIRECTORY_URL = (
    BMV_BASE + "/es/Grupo_BMV/Informacion_de_emisora/_rid/541/_mto/3/_mod/doSearch"
)
_DIRECTORY_PARAMS = {
    "idTipoMercado": "", "idTipoInstrumento": "", "idTipoEmpresa": "",
    "idSector": "", "idSubsector": "", "idRamo": "", "idSubramo": "",
}
_YEAR_RE = re.compile(r"\b(20\d{2})\b")
_DATE_RE = re.compile(r"^(\d{2})[-/]([A-Za-z]{3}|\d{2})[-/](20\d{2})(?:\s+(\d{2}):(\d{2}))?")
_MONTHS = {m.lower(): i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1
)}


@dataclass(frozen=True)
class BmvIssuer:
    ticker: str
    issuer_id: int
    company: str
    slug: str
    industry: str | None = None

    @property
    def key(self) -> str:
        return f"{self.ticker.upper()}-{self.issuer_id}-CGEN_CAPIT"

    @property
    def financial_url(self) -> str:
        return f"{BMV_BASE}/es/emisoras/informacionfinanciera/{self.key}"

    @property
    def events_url(self) -> str:
        return f"{BMV_BASE}/es/emisoras/eventosrelevantes/{self.key}"


@dataclass(frozen=True)
class BmvDirectoryIssuer:
    """One issuer record from BMV's official directory, retained before curation."""
    ticker: str
    issuer_id: int
    legal_name: str
    market: str
    sector: str | None = None


def parse_directory_jsonp(payload: str) -> dict[str, int]:
    """Return ``ticker -> issuer_id`` from the BMV anti-JSON-hijacking response."""
    return {row.ticker: row.issuer_id for row in _directory_records(payload, market="unknown")}


def _directory_rows(payload: str) -> list[dict]:
    text = (payload or "").strip()
    text = re.sub(r"^for\s*\(;;\s*\);\s*\(", "", text)
    text = re.sub(r"\)\s*$", "", text)
    raw = json.loads(text)
    return list((raw.get("response") or {}).get("resultado") or [])


def _directory_records(payload: str, *, market: str) -> list[BmvDirectoryIssuer]:
    out = []
    for row in _directory_rows(payload):
        if not row.get("claveEmisora") or not row.get("idEmisora"):
            continue
        out.append(BmvDirectoryIssuer(
            ticker=str(row["claveEmisora"]).upper(), issuer_id=int(row["idEmisora"]),
            legal_name=" ".join(str(row.get("razonSocial") or "").split()), market=market,
            sector=(str(row.get("clasifSectorial") or "").strip() or None),
        ))
    return out


def fetch_issuer_directory(
    session: requests.Session | None = None, *, markets: Iterable[str] = ("CGEN_CAPIT", "CGEN_ELDEU"),
) -> list[BmvDirectoryIssuer]:
    """Fetch a source-attributed BMV issuer snapshot, preserving legal names and market route."""
    session = session or _make_session()
    by_key: dict[tuple[str, int], BmvDirectoryIssuer] = {}
    for market in markets:
        response = _session_get(
            session, ISSUER_DIRECTORY_URL,
            params={**_DIRECTORY_PARAMS, "idTipoMercado": market}, timeout=120, verify_ssl=True,
        )
        for record in _directory_records(response.text, market=market):
            by_key[(record.ticker, record.issuer_id)] = record
    return sorted(by_key.values(), key=lambda item: (item.ticker, item.issuer_id))


def fetch_issuer_ids(session: requests.Session | None = None) -> dict[str, int]:
    # Some equity issuers (notably GMXT) resolve only through BMV's debt directory even though
    # their issuer information page is addressable with the capital route. Merge both catalogs.
    return {record.ticker: record.issuer_id for record in fetch_issuer_directory(session)}


def _published_at(raw: str) -> str | None:
    match = _DATE_RE.search(" ".join((raw or "").split()))
    if not match:
        return None
    day, month, year, hour, minute = match.groups()
    month_num = int(month) if month.isdigit() else _MONTHS.get(month.lower())
    if not month_num:
        return None
    return datetime(int(year), month_num, int(day), int(hour or 0), int(minute or 0)).isoformat(
        timespec="minutes"
    )


def _source_id(url: str) -> str:
    return urlparse(url).path.rsplit("/", 1)[-1]


def _year_from(title: str, url: str, published_at: str | None) -> int | None:
    match = _YEAR_RE.search(title) or _YEAR_RE.search(url)
    if match:
        return int(match.group(1))
    if published_at:
        return int(published_at[:4]) - 1
    return None


def _table_after_heading(soup: BeautifulSoup, heading: str):
    for h2 in soup.find_all("h2"):
        if heading.casefold() in h2.get_text(" ", strip=True).casefold():
            return h2.find_next("table")
    return None


def parse_financial_page(html: str, issuer: BmvIssuer) -> list[SourceRecord]:
    """Discover narrative annual/sustainability PDFs; exclude XBRL and governance forms."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[SourceRecord] = []
    sections = (("REPORTES ANUALES", "annual_report"),
                ("REPORTE DE SUSTENTABILIDAD", "sustainability_report"))
    for heading, doc_type in sections:
        table = _table_after_heading(soup, heading)
        if table is None:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 3:
                continue
            title = cells[1].get_text(" ", strip=True)
            anchor = cells[2].find("a", href=True)
            if not anchor:
                continue
            url = urljoin(BMV_BASE, anchor["href"])
            low = title.casefold()
            if not urlparse(url).path.lower().endswith(".pdf"):
                continue
            if doc_type == "annual_report" and (
                not ("informe anual" in low or "reporte anual" in low)
                or "sostenibilidad" in low or "mejores pr" in low
            ):
                continue
            published = _published_at(cells[0].get_text(" ", strip=True))
            year = _year_from(title, url, published)
            period = f"{year}-FY" if year else None
            source_id = _source_id(url)
            out.append(SourceRecord(
                source="BMV issuer", source_record_id=source_id, company=issuer.slug,
                ticker=issuer.ticker, doc_type=doc_type, title=title, canonical_url=url,
                published_at=published, filed_at=published, period=period, language="es",
                mime_type="application/pdf",
                document_family_id=f"{issuer.slug}:{doc_type}:{period or source_id}",
                metadata={"industry": issuer.industry, "issuer_id": issuer.issuer_id,
                          "page_url": issuer.financial_url},
            ))
    return out


def parse_events_page(html: str, issuer: BmvIssuer) -> list[SourceRecord]:
    """Discover issuer relevant-event PDF attachments using source-native table boundaries."""
    soup = BeautifulSoup(html, "html.parser")
    table = _table_after_heading(soup, "EVENTOS RELEVANTES DE LA EMISORA")
    if table is None:
        return []
    out: list[SourceRecord] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 3:
            continue
        title = cells[1].get_text(" ", strip=True)
        pdf = next((a for a in cells[2].find_all("a", href=True)
                    if urlparse(a["href"]).path.lower().endswith(".pdf")), None)
        if not title or pdf is None:
            continue
        url = urljoin(BMV_BASE, pdf["href"])
        published = _published_at(cells[0].get_text(" ", strip=True))
        low = title.casefold()
        is_annual_notice = "reporte anual" in low or "informe anual" in low
        # An event attachment saying that an annual report was presented is normally a short
        # announcement, not the report body. Keep its source-native type and relationship instead
        # of inflating annual-report coverage with a false positive.
        doc_type = "relevant_event"
        year = _year_from(title, url, published) if is_annual_notice else None
        period = f"{year}-FY" if year else None
        source_id = _source_id(url)
        out.append(SourceRecord(
            source="BMV issuer", source_record_id=source_id, company=issuer.slug,
            ticker=issuer.ticker, doc_type=doc_type, title=title, canonical_url=url,
            published_at=published, filed_at=published, period=period, language="es",
            mime_type="application/pdf",
            document_family_id=f"{issuer.slug}:{doc_type}:{period or source_id}",
            metadata={"industry": issuer.industry, "issuer_id": issuer.issuer_id,
                      "page_url": issuer.events_url,
                      "related_doc_type": "annual_report" if is_annual_notice else None},
        ))
    return out


class BmvIssuerAdapter:
    name = "BMV issuer"

    def __init__(
        self, issuers: Iterable[BmvIssuer], *, include_events: bool = True,
        pages: Mapping[str, str] | None = None, session: requests.Session | None = None,
    ):
        self.issuers = tuple(issuers)
        self.include_events = include_events
        self.pages = pages or {}
        self.session = session or _make_session()

    def _html(self, url: str) -> str:
        return self.pages[url] if url in self.pages else _session_get(
            self.session, url, timeout=120, verify_ssl=True
        ).text

    def discover(self) -> Iterable[SourceRecord]:
        for issuer in self.issuers:
            yield from parse_financial_page(self._html(issuer.financial_url), issuer)
            if self.include_events:
                yield from parse_events_page(self._html(issuer.events_url), issuer)
