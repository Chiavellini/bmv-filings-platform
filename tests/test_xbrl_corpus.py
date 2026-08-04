"""Materializing XBRL filings into the paths the extractor actually reads."""

from __future__ import annotations

import json
from pathlib import Path

from src.download.xbrl_corpus import (
    SOURCE_FACTS,
    SOURCE_MDNA,
    SOURCE_PDF,
    load_provenance,
    materialize_xbrl_corpus,
    render_mdna_html,
)


# Aspose.Words splits every styled run into its own <span>, mid-word. This is
# the shape the real BMV filings have.
_RUN_SPLIT_HTML = """
<div>
<p><span>Al 3</span><span>0</span><span> de junio de </span><span>202</span><span>1</span></p>
<p><span>Deuda Total a EBITDA fue de </span><span>5.4</span><span> v</span><span>eces</span></p>
<table>
  <tr><td><span>Indicador</span></td><td><span>2T</span><span>21</span></td><td><span>2T20</span></td></tr>
  <tr><td><span>Ventas</span></td><td><span>1,2</span><span>34</span></td><td><span>1,100</span></td></tr>
</table>
</div>
"""


def test_render_repairs_runs_without_welding_paragraphs():
    text = render_mdna_html(_RUN_SPLIT_HTML)
    lines = text.splitlines()

    # Words and numbers survive the run boundaries...
    assert "Al 30 de junio de 2021" in lines
    assert "Deuda Total a EBITDA fue de 5.4 veces" in lines
    # ...and paragraphs do not run together, which "".join would cause.
    assert "2021Deuda" not in text


def test_render_keeps_table_rows_on_one_line_with_delimited_cells():
    lines = render_mdna_html(_RUN_SPLIT_HTML).splitlines()
    assert "Indicador | 2T21 | 2T20" in lines
    assert "Ventas | 1,234 | 1,100" in lines


def _filing(directory: Path, stem: str, *, mdna: str | None = _RUN_SPLIT_HTML) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{stem}.json").write_text("{}", encoding="utf-8")
    (directory / f"{stem}_facts.json").write_text(
        json.dumps({"facts": {"Revenue": [{"value": 1}]}}), encoding="utf-8"
    )
    if mdna is not None:
        (directory / f"{stem}_mdna.html").write_text(mdna, encoding="utf-8")


def test_ticker_prefixed_nested_layout_is_surfaced_canonically(tmp_path):
    """``download_ticker`` writes ``xbrl/ALSEA_2021-2T_facts.json``; the
    extractor globs ``<report_dir>/*_facts.json``. Bridge the two."""
    report_dir = tmp_path / "alsea"
    _filing(report_dir / "xbrl", "ALSEA_2021-2T")

    result = materialize_xbrl_corpus(report_dir)

    assert (report_dir / "2021-2T_facts.json").exists()
    assert (report_dir / "2021-2T.md").exists()
    assert result.provenance["2021-2T"].source == SOURCE_MDNA


def test_periods_with_a_pdf_keep_their_parsed_markdown(tmp_path):
    """The PDF parse owns those periods and is the better source; a rerun of
    the materializer must never clobber it."""
    report_dir = tmp_path / "herdez"
    _filing(report_dir / "xbrl", "HERDEZ_2021-2T")
    (report_dir / "2021-2T.pdf").write_bytes(b"%PDF-1.4\n")
    (report_dir / "2021-2T.md").write_text("PARSED FROM PDF", encoding="utf-8")

    result = materialize_xbrl_corpus(report_dir)

    assert (report_dir / "2021-2T.md").read_text(encoding="utf-8") == "PARSED FROM PDF"
    assert result.markdown_written == []
    assert result.provenance["2021-2T"].source == SOURCE_PDF


def test_existing_markdown_is_not_overwritten_without_the_flag(tmp_path):
    report_dir = tmp_path / "co"
    _filing(report_dir / "xbrl", "CO_2022-1T")
    (report_dir / "2022-1T.md").write_text("EXISTING", encoding="utf-8")

    materialize_xbrl_corpus(report_dir)
    assert (report_dir / "2022-1T.md").read_text(encoding="utf-8") == "EXISTING"

    # `overwrite` alone is not enough: the directory already has a corpus, so
    # touching it also requires opting in via fill_gaps.
    materialize_xbrl_corpus(report_dir, overwrite=True)
    assert (report_dir / "2022-1T.md").read_text(encoding="utf-8") == "EXISTING"

    materialize_xbrl_corpus(report_dir, overwrite=True, fill_gaps=True)
    assert (report_dir / "2022-1T.md").read_text(encoding="utf-8") != "EXISTING"


def test_filing_without_narrative_is_recorded_as_facts_only(tmp_path):
    report_dir = tmp_path / "co"
    _filing(report_dir / "xbrl", "CO_2023-FY", mdna="<div></div>")

    result = materialize_xbrl_corpus(report_dir)

    assert not (report_dir / "2023-FY.md").exists()
    assert result.provenance["2023-FY"].source == SOURCE_FACTS


def test_provenance_round_trips_and_never_downgrades(tmp_path):
    report_dir = tmp_path / "co"
    _filing(report_dir / "xbrl", "CO_2022-1T")
    materialize_xbrl_corpus(report_dir)

    stored = load_provenance(report_dir)
    assert stored["2022-1T"].source == SOURCE_MDNA
    assert stored["2022-1T"].facts == "2022-1T_facts.json"

    # A PDF arrives later: the record must improve, not regress.
    (report_dir / "2022-1T.pdf").write_bytes(b"%PDF-1.4\n")
    materialize_xbrl_corpus(report_dir)
    assert load_provenance(report_dir)["2022-1T"].source == SOURCE_PDF


def test_existing_corpus_is_not_extended_by_default(tmp_path):
    """Adding previously-invisible facts adds *uncertified* observations.

    Materializing into corpora that already had reports raised the FAIL count
    for `lab`, `chedraui` and `femsa` against their pinned regression
    baselines. Extending an existing corpus must be a deliberate act.
    """
    report_dir = tmp_path / "lab"
    _filing(report_dir / "xbrl", "LAB_2022-1T")
    _filing(report_dir / "xbrl", "LAB_2022-2T")
    # An existing corpus: one period already delivered from a PDF.
    (report_dir / "2022-2T.pdf").write_bytes(b"%PDF-1.4\n")
    (report_dir / "2022-2T.md").write_text("parsed", encoding="utf-8")

    result = materialize_xbrl_corpus(report_dir)

    assert result.markdown_written == []
    assert result.facts_linked == []
    assert not (report_dir / "2022-1T.md").exists()
    assert not (report_dir / "2022-1T_facts.json").exists()

    # ...but the operator can still opt in, and then re-certify.
    opted_in = materialize_xbrl_corpus(report_dir, fill_gaps=True)
    assert opted_in.markdown_written == ["2022-1T"]
    assert (report_dir / "2022-1T_facts.json").exists()


def test_empty_directory_is_bootstrapped_without_a_flag(tmp_path):
    """The 72 XBRL-only issuers have nothing to lose and everything to gain."""
    report_dir = tmp_path / "alsea"
    _filing(report_dir / "xbrl", "ALSEA_2021-2T")

    result = materialize_xbrl_corpus(report_dir)

    assert result.markdown_written == ["2021-2T"]
    assert result.facts_linked == ["2021-2T"]


def test_is_idempotent(tmp_path):
    report_dir = tmp_path / "co"
    _filing(report_dir / "xbrl", "CO_2022-1T")

    first = materialize_xbrl_corpus(report_dir)
    second = materialize_xbrl_corpus(report_dir)

    assert first.markdown_written == ["2022-1T"]
    assert second.markdown_written == []
    assert second.facts_linked == []
    assert second.provenance["2022-1T"].source == SOURCE_MDNA


def test_facts_are_hardlinked_not_copied(tmp_path):
    """~1,500 facts artifacts averaging 1 MB; duplicating them would cost 1.5 GB."""
    report_dir = tmp_path / "co"
    _filing(report_dir / "xbrl", "CO_2022-1T")

    materialize_xbrl_corpus(report_dir)

    source = report_dir / "xbrl" / "CO_2022-1T_facts.json"
    dest = report_dir / "2022-1T_facts.json"
    assert dest.stat().st_ino == source.stat().st_ino
