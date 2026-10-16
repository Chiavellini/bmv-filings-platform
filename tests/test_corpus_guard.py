"""The corpus guard must not mistake an empty directory tree for a corpus."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests import _corpus


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    monkeypatch.setattr(_corpus, "REPORTS_DIR", root)
    return root


def test_absent_reports_dir_is_not_a_corpus(reports: Path) -> None:
    assert not _corpus.has_corpus()
    assert not _corpus.has_corpus("walmex")


def test_empty_slug_directories_are_not_a_corpus(reports: Path) -> None:
    # What a Segments run without an attached estate leaves behind.
    (reports / "ac").mkdir(parents=True)
    assert not _corpus.has_corpus()
    assert not _corpus.has_corpus("ac")


def test_any_filing_file_counts_as_a_corpus(reports: Path) -> None:
    (reports / "walmex").mkdir(parents=True)
    (reports / "walmex" / "2025Q4.pdf").write_bytes(b"%PDF-1.7")
    assert _corpus.has_corpus()
    assert _corpus.has_corpus("walmex")
    assert not _corpus.has_corpus("ac")
