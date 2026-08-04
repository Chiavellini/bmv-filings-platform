"""The per-period coverage gate that stage 2 of /onboard-company enforces."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import report_coverage
from scripts.fetch_company_reports import _expected_periods
from src.download.xbrl_corpus import SOURCE_FACTS, SOURCE_MDNA, SOURCE_PDF


def test_expected_periods_are_bounded_by_the_listing_quarter():
    """Without this, a 2021 IPO reports twenty phantom gaps back to 2016 and
    the missing-period signal is too noisy to gate on."""
    unbounded = _expected_periods(2016)
    bounded = _expected_periods(2016, "2021-2T")

    assert "2016-1T" in unbounded
    assert "2016-1T" not in bounded
    assert "2021-1T" not in bounded
    assert "2021-2T" in bounded
    assert bounded < unbounded


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """One company directory wired as the only corpus source."""
    reports = tmp_path / "reports"
    company = reports / "co"
    company.mkdir(parents=True)
    monkeypatch.setattr(report_coverage, "REPORTS_DIR", reports)
    monkeypatch.setattr(report_coverage, "SHARED_REPORTS_DIR", tmp_path / "absent_a")
    monkeypatch.setattr(report_coverage, "SHARED_PARSED_REPORTS_DIR", tmp_path / "absent_b")
    return company


def _observed(slug: str = "co") -> dict:
    return report_coverage._observed_sources(slug)


def test_parsed_pdf_beats_mdna_beats_facts(corpus):
    (corpus / "2024-1T.pdf").write_bytes(b"%PDF-1.4\n")
    (corpus / "2024-1T.md").write_text("parsed", encoding="utf-8")
    (corpus / "2024-2T.md").write_text("narrative", encoding="utf-8")
    (corpus / "2024-3T_facts.json").write_text("{}", encoding="utf-8")

    observed = _observed()
    assert observed["2024-1T"] == SOURCE_PDF
    assert observed["2024-2T"] == SOURCE_MDNA
    assert observed["2024-3T"] == SOURCE_FACTS


def test_unparsed_pdf_is_not_counted_as_narrative(corpus):
    """`gruma` had 41 PDFs and zero Markdown yet looked fully covered, while
    certification — which globs *.md — scored it zero."""
    (corpus / "2024-1T.pdf").write_bytes(b"%PDF-1.4\n")

    assert _observed()["2024-1T"] == report_coverage.SOURCE_PDF_RAW

    row = report_coverage.PeriodCoverage("2024-1T", report_coverage.SOURCE_PDF_RAW)
    assert row.ok is True
    assert row.has_narrative is False


def test_provenance_claiming_pdf_is_demoted_when_markdown_is_absent(corpus):
    """provenance.json records origin, not whether the parse ever ran."""
    (corpus / "2024-1T.pdf").write_bytes(b"%PDF-1.4\n")
    (corpus / "provenance.json").write_text(
        json.dumps(
            {
                "version": 1,
                "periods": {
                    "2024-1T": {
                        "period": "2024-1T",
                        "source": "pdf",
                        "markdown": "2024-1T.md",
                        "facts": None,
                        "instance": None,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert _observed()["2024-1T"] == report_coverage.SOURCE_PDF_RAW


def test_summary_separates_coverage_from_narrative_coverage():
    rows = [
        report_coverage.PeriodCoverage("2024-1T", SOURCE_PDF),
        report_coverage.PeriodCoverage("2024-2T", SOURCE_MDNA),
        report_coverage.PeriodCoverage("2024-3T", SOURCE_FACTS),
        report_coverage.PeriodCoverage("2024-4T", report_coverage.MISSING),
    ]
    summary = report_coverage._summarize(rows)

    assert summary["expected"] == 4
    assert summary["covered"] == 3
    assert summary["narrative"] == 2
    assert summary["coverage"] == 0.75
    assert summary["narrative_coverage"] == 0.5
    assert summary["missing"] == ["2024-4T"]


def test_cli_exits_non_zero_below_the_threshold(corpus, monkeypatch, capsys):
    """A silent partial corpus is the failure mode this gate exists to stop."""
    from src.shared.company_source import CompanySource

    monkeypatch.setattr(
        report_coverage,
        "resolve_company_source",
        lambda slug: CompanySource(slug=slug, ticker="CO", xbrl_ticker="CO"),
    )
    (corpus / "2024-1T.md").write_text("narrative", encoding="utf-8")

    assert report_coverage.main(["co", "--min-coverage", "0.9"]) == 1
    assert report_coverage.main(["co", "--min-coverage", "0"]) == 0


def test_cli_reports_unknown_company_as_failure(monkeypatch):
    def _raise(slug):
        from src.shared.company_source import UnknownCompanyError

        raise UnknownCompanyError(slug)

    monkeypatch.setattr(report_coverage, "resolve_company_source", _raise)
    assert report_coverage.main(["ghost", "--min-coverage", "0"]) == 1
