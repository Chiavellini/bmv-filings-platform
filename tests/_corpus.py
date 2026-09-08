"""Shared guards for tests that need the filing corpus.

``data/reports/`` is ~15 GB of downloaded filings and is deliberately gitignored,
so it is absent from a fresh clone. Tests that read it must SKIP there, not fail.

The distinction matters: a skip says "this needs data I do not have", which is
honest and actionable. A failure says "this code is broken", which is false and
buries real regressions in noise. ``scripts/certify_clean_clone.py`` enforces
exactly this — it treats any failure on a data-less clone as a defect.

Ground truth (``data/ground_truth/``) is hand-curated research data and may also
be absent from a public clone. Tests that integrate with a particular ground-
truth CSV must skip when that file is not attached; parser and other synthetic
unit coverage must continue to run.
"""
from __future__ import annotations

from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
REPORTS_DIR = ROOT / "data" / "reports"
GROUND_TRUTH_DIR = ROOT / "data" / "ground_truth"

_REASON = (
    "filing corpus not checked out (data/reports/ is gitignored; "
    "run the acquisition engine or attach an estate bundle)"
)


def _holds_files(folder: Path) -> bool:
    """True when `folder` contains at least one real file, one level deep.

    The corpus is ``data/reports/<slug>/<file>`` with an optional ``xbrl/``
    subdirectory, so two levels is the whole layout.
    """
    try:
        for entry in folder.iterdir():
            if entry.is_file():
                return True
            if entry.is_dir() and any(child.is_file() for child in entry.iterdir()):
                return True
    except OSError:
        return False
    return False


def has_corpus(slug: str | None = None) -> bool:
    """True when the corpus — or one company's slice of it — is on disk.

    This checks for actual *files*. It previously returned True for
    ``any(target.iterdir())``, so a single empty directory was enough: a failed
    `build_segments` run left behind an empty ``data/reports/ac/``, which
    un-skipped 17 corpus tests that then failed for want of data they never had.
    That inverts this module's contract -- a skip says "I do not have this data",
    a failure says "this code is broken" -- and `certify_clean_clone.py` treats
    any failure on a data-less clone as a release defect.
    """
    if not REPORTS_DIR.is_dir():
        return False
    target = REPORTS_DIR / slug if slug else REPORTS_DIR
    if not target.is_dir():
        return False
    if slug:
        return _holds_files(target)
    return _holds_files(target) or any(
        _holds_files(child) for child in target.iterdir() if child.is_dir()
    )


def skip_without_corpus(slug: str | None = None) -> None:
    """Call inside a test to skip when its corpus slice is missing."""
    if not has_corpus(slug):
        pytest.skip(f"{slug or 'corpus'}: {_REASON}")


#: Module- or class-level guard: ``pytestmark = requires_corpus``
requires_corpus = pytest.mark.skipif(not has_corpus(), reason=_REASON)


def requires_corpus_for(slug: str):
    """Guard for one company's corpus: ``@requires_corpus_for("sport")``."""
    return pytest.mark.skipif(not has_corpus(slug), reason=f"{slug}: {_REASON}")
