from __future__ import annotations

from src.download.bmv_xbrl import XbrlFiling
from src.sources.base import SourceRecord
from src.sources.bmv_xbrl import BmvCompany, BmvXbrlAdapter


def test_source_record_id_is_stable_and_separates_doc_types():
    base = dict(
        source="BMV XBRL", source_record_id="AC:annual:2024-FY", company="ac",
        doc_type="annual_report", title="AC 2024-FY", canonical_url="https://x/ac.zip",
        period="2024-FY",
    )
    first = SourceRecord(**base)
    second = SourceRecord(**base)
    quarterly = SourceRecord(**{**base, "source_record_id": "AC:quarterly:2024-4T",
                                "doc_type": "quarterly_release", "period": "2024-4T"})
    assert first.proposed_doc_id == second.proposed_doc_id
    assert first.proposed_doc_id != quarterly.proposed_doc_id
    assert first.proposed_doc_id.startswith("ac/annual_report/2024-fy/")


def test_bmv_adapter_emits_normalized_annual_and_filters_unknown_tickers():
    filings = [
        XbrlFiling("AC", "Arca Continental", "01/04/2025", "2024-FY", "annual",
                   "https://bmv/ac-2024.zip"),
        XbrlFiling("AC", "Arca Continental", "15/04/2025", "2025-1T", "quarterly",
                   "https://bmv/ac-2025-1t.zip"),
        XbrlFiling("OTHER", "Other", "01/04/2025", "2024-FY", "annual",
                   "https://bmv/other.zip"),
    ]
    adapter = BmvXbrlAdapter(
        [BmvCompany("AC", "ac", "Arca Continental", "beverage")],
        kinds=frozenset({"annual"}), filings=filings,
    )
    records = list(adapter.discover())
    assert len(records) == 1
    record = records[0]
    assert record.doc_type == "regulatory_filing"
    assert record.source_record_id == "AC:annual:2024-FY"
    assert record.metadata["industry"] == "beverage"
