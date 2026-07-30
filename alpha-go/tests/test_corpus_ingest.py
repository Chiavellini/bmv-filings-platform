"""Tests for corpus ingestion (Phase 1).

Offline-by-default: the fixture corpus ships pre-parsed .md files (no PDFs, no network),
so ingest builds the manifest purely from local files. A real-download test is opt-in
behind the `network` marker.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.corpus.ingest import _infer_doc_type, ingest_source
from src.corpus.manifest import load_manifest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_corpus" / "acme"


def _make_corpus(tmp_path: Path) -> Path:
    """Copy the fixture .md files into a temp corpus at <tmp>/corpus/acme/."""
    corpus_dir = tmp_path / "corpus"
    dest = corpus_dir / "acme"
    dest.mkdir(parents=True)
    for md in FIXTURE.glob("*.md"):
        shutil.copy2(md, dest / md.name)
    return corpus_dir


SOURCE = {"slug": "acme", "company": "Acme Corp", "doc_types": ["report"],
          "language": "en", "ir_website": {}}


def test_ingest_builds_one_document_per_period(tmp_path):
    corpus_dir = _make_corpus(tmp_path)
    manifest = ingest_source(SOURCE, corpus_dir)

    periods = sorted(d.period for d in manifest.documents)
    assert periods == ["2024-1T", "2024-2T"]

    doc = next(d for d in manifest.documents if d.period == "2024-1T")
    assert doc.doc_id == "acme/2024-1T"
    assert doc.company == "acme"
    assert doc.title == "Acme Corp 2024-1T"
    assert doc.doc_type == "quarterly_release"       # taxonomy: "2024-1T" → 1T keyword
    assert doc.pdf_path is None                      # md-primary fixture
    assert Path(doc.markdown_path).exists()
    assert "Total Revenues" in Path(doc.markdown_path).read_text(encoding="utf-8")


def test_ingest_persists_manifest(tmp_path):
    corpus_dir = _make_corpus(tmp_path)
    ingest_source(SOURCE, corpus_dir)
    reloaded = load_manifest(corpus_dir)
    assert len(reloaded.documents) == 2
    assert {d.doc_id for d in reloaded.documents} == {"acme/2024-1T", "acme/2024-2T"}


def test_ingest_labels_annual_report_with_fy_period(tmp_path):
    """An annual report on disk ingests as a YYYY-FY period, labeled annual_report."""
    corpus_dir = tmp_path / "corpus"
    dest = corpus_dir / "acme"
    dest.mkdir(parents=True)
    (dest / "Acme_Integrated_Annual_Report_2024.md").write_text(
        "Acme integrated annual report for the full year 2024.", encoding="utf-8")

    manifest = ingest_source(SOURCE, corpus_dir)

    doc = next(d for d in manifest.documents if d.period == "2024-FY")
    assert doc.doc_id == "acme/2024-FY"
    assert doc.doc_type == "annual_report"          # taxonomy: "annual"/"informe anual" keyword


def test_annual_pass_invokes_downloader_with_annual_kind(tmp_path, monkeypatch):
    """A source with an annual_reports block triggers a doc_kind='annual' download pass."""
    calls: list[tuple[str, dict]] = []
    import src.download.downloader as dl
    monkeypatch.setattr(dl, "download_from_ir",
                        lambda url, dest, **kw: calls.append((url, kw)) or [])

    corpus_dir = tmp_path / "corpus"
    (corpus_dir / "acme").mkdir(parents=True)
    source = {**SOURCE, "annual_reports": {"url": "https://x/annual.html",
                                           "pdf_link_pattern": r"annual.*\.pdf"}}

    ingest_source(source, corpus_dir, force_download=True)

    annual = [kw for url, kw in calls if kw.get("doc_kind") == "annual"]
    assert annual, "expected an annual (doc_kind='annual') download pass"
    assert annual[0]["file_pattern"] == r"annual.*\.pdf"


def test_reuse_if_exists_skips_download(tmp_path, monkeypatch):
    """With files already present and force_download=False, no network call is made."""
    corpus_dir = _make_corpus(tmp_path)

    import src.download.downloader as dl

    def _boom(*a, **k):
        raise AssertionError("download_from_ir must not be called when files already exist")

    monkeypatch.setattr(dl, "download_from_ir", _boom)
    # A url is present, but reuse-if-exists must short-circuit before downloading.
    src_with_url = {**SOURCE, "ir_website": {"url": "https://example.invalid/ir"}}
    manifest = ingest_source(src_with_url, corpus_dir)
    assert len(manifest.documents) == 2


def test_ingest_reuses_local_pdf_and_markdown_mirror(tmp_path):
    """A local mirror's parsed Markdown wins over its PDF and is copied into the corpus."""
    mirror = tmp_path / "reports"
    mirror.mkdir()
    (mirror / "acme_1T24.pdf").write_bytes(b"placeholder PDF")
    (mirror / "acme_1T24.md").write_text("Already parsed local report.", encoding="utf-8")

    manifest = ingest_source(
        {**SOURCE, "local_pdf_dir": str(mirror)},
        tmp_path / "corpus",
    )

    doc = manifest.documents[0]
    assert doc.period == "2024-1T"
    assert doc.pdf_path == str(tmp_path / "corpus" / "acme" / "acme_1T24.pdf")
    assert doc.original_path == doc.pdf_path
    assert doc.original_format == "pdf"
    assert Path(doc.markdown_path).read_text(encoding="utf-8") == "Already parsed local report."


def test_ingest_local_md_only_ignores_pdf_only_periods(tmp_path):
    """A curated local mirror can exclude unparsed archival PDFs from the manifest."""
    mirror = tmp_path / "reports"
    mirror.mkdir()
    (mirror / "acme_1T24.md").write_text("Maintained report.", encoding="utf-8")
    (mirror / "acme_2T24.pdf").write_bytes(b"archival PDF")

    manifest = ingest_source(
        {**SOURCE, "local_pdf_dir": str(mirror), "local_md_only": True},
        tmp_path / "corpus",
    )

    assert [d.period for d in manifest.documents] == ["2024-1T"]


def test_ingest_links_grouped_pdf_when_markdown_has_a_canonical_name(tmp_path):
    """A raw PDF with the issuer's filename remains the reader original for its Markdown doc."""
    mirror = tmp_path / "reports"
    mirror.mkdir()
    (mirror / "2024-1T.md").write_text("Maintained report.", encoding="utf-8")
    (mirror / "Acme_results_1T24.pdf").write_bytes(b"original PDF")

    manifest = ingest_source({**SOURCE, "local_pdf_dir": str(mirror)}, tmp_path / "corpus")

    doc = manifest.documents[0]
    assert Path(doc.pdf_path).name == "Acme_results_1T24.pdf"
    assert doc.original_format == "pdf"


def test_ingest_backfills_a_legacy_pdf_path_as_the_original(tmp_path):
    """A new ingest upgrades older manifest rows that already reference a usable PDF."""
    corpus_dir = _make_corpus(tmp_path)
    pdf = corpus_dir / "legacy.pdf"
    pdf.write_bytes(b"original")
    from src.corpus.manifest import CorpusManifest, Document, save_manifest
    save_manifest(CorpusManifest([Document(
        doc_id="legacy/2024-1T", company="legacy", period="2024-1T", doc_type="report",
        title="Legacy", source_url=None, pdf_path=str(pdf), markdown_path=str(pdf),
    )]), corpus_dir)

    manifest = ingest_source(SOURCE, corpus_dir)
    legacy = next(d for d in manifest.documents if d.doc_id == "legacy/2024-1T")
    assert legacy.original_path == str(pdf)
    assert legacy.original_format == "pdf"


def test_infer_doc_type():
    from src.corpus.doc_types import load_taxonomy
    tax = load_taxonomy(None)                        # builtin taxonomy
    assert _infer_doc_type("acme_annual_report_2024", tax) == "annual_report"
    assert _infer_doc_type("acme_4Q24_release", tax) == "quarterly_release"
    assert _infer_doc_type("acme_generic_memo", tax) == "quarterly_release"   # default


@pytest.mark.network
def test_ingest_real_source_smoke(tmp_path):
    """Opt-in: real download from a configured source. Run with `pytest -m network`."""
    import yaml

    cfg = yaml.safe_load((Path(__file__).parents[1] / "configs" / "alpha_go.yaml").read_text())
    source = cfg["sources"][0]
    manifest = ingest_source(source, tmp_path / "corpus", force_download=True)
    assert manifest.documents  # at least one report ingested
