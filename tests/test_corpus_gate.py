"""An empty directory is not a corpus.

`tests/_corpus.py` decides whether corpus-dependent tests run or skip. It used
to accept `any(target.iterdir())`, so a single empty directory satisfied it.
A failed `build_segments` run left an empty `data/reports/ac/` behind, which
un-skipped 17 tests that then failed for want of data they never had.

That inverts the contract the module documents: a skip means "I do not have this
data", a failure means "this code is broken". `scripts/certify_clean_clone.py`
treats any failure on a data-less clone as a release defect, so this could fail
a release certification for a stray directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import _corpus


@pytest.fixture()
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.setattr(_corpus, "REPORTS_DIR", root)
    return root


def test_empty_company_directory_is_not_a_corpus(reports: Path) -> None:
    """The exact residue a failed build_segments run leaves behind."""
    (reports / "ac").mkdir()
    assert _corpus.has_corpus() is False
    assert _corpus.has_corpus("ac") is False


def test_a_real_filing_counts(reports: Path) -> None:
    (reports / "ac").mkdir()
    (reports / "ac" / "2025-1T.md").write_text("filing", encoding="utf-8")
    assert _corpus.has_corpus() is True
    assert _corpus.has_corpus("ac") is True


def test_files_nested_one_level_count(reports: Path) -> None:
    """The corpus also stores `data/reports/<slug>/xbrl/<file>`."""
    (reports / "kof" / "xbrl").mkdir(parents=True)
    (reports / "kof" / "xbrl" / "facts.json").write_text("{}", encoding="utf-8")
    assert _corpus.has_corpus("kof") is True
    assert _corpus.has_corpus() is True


def test_missing_directories_are_not_a_corpus(reports: Path) -> None:
    assert _corpus.has_corpus("never-onboarded") is False
    assert _corpus.has_corpus() is False
