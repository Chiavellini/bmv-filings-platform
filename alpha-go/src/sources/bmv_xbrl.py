"""Official BMV XBRL discovery adapter."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from src.download.bmv_xbrl import XbrlFiling, fetch_archive_index, parse_archive_index
from src.sources.base import SourceRecord


@dataclass(frozen=True)
class BmvCompany:
    ticker: str
    slug: str
    company: str
    industry: str | None = None


class BmvXbrlAdapter:
    name = "BMV XBRL"

    def __init__(
        self,
        companies: Iterable[BmvCompany],
        *,
        kinds: frozenset[str] = frozenset({"quarterly", "annual"}),
        filings: Iterable[XbrlFiling] | None = None,
        archive_cache: Path | None = None,
    ):
        self.companies: Mapping[str, BmvCompany] = {
            c.ticker.upper(): c for c in companies
        }
        self.kinds = kinds
        self._filings = tuple(filings) if filings is not None else None
        self.archive_cache = archive_cache

    def discover(self) -> Iterable[SourceRecord]:
        filings = self._filings
        if filings is None:
            if self.archive_cache and self.archive_cache.exists():
                filings = tuple(parse_archive_index(
                    self.archive_cache.read_text(encoding="utf-8", errors="replace")
                ))
            else:
                filings = tuple(fetch_archive_index(cache_html_path=self.archive_cache))
        for filing in filings:
            company = self.companies.get(filing.ticker.upper())
            if not company or filing.kind not in self.kinds:
                continue
            # The archive's annual XBRL packages contain tagged regulatory facts but, unlike the
            # quarterly packages, no searchable narrative report. Actual annual reports come from
            # BMV issuer pages, issuer IR sites or SEC 20-F filings.
            doc_type = "regulatory_filing" if filing.kind == "annual" else "quarterly_release"
            source_id = f"{filing.ticker.upper()}:{filing.kind}:{filing.period}"
            yield SourceRecord(
                source=self.name, source_record_id=source_id, company=company.slug,
                ticker=company.ticker, doc_type=doc_type,
                title=f"{company.company} {filing.period}", canonical_url=filing.zip_url,
                filed_at=filing.filed_date, period=filing.period, language="es",
                mime_type="application/zip",
                document_family_id=f"{company.slug}:{doc_type}:{filing.period}",
                metadata={"legal_name": filing.razon_social,
                          "industry": company.industry, "kind": filing.kind},
            )
