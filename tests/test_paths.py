"""
test_paths.py — shared/paths.py (the project-path helper introduced by the reorg).

Confirms PROJECT_ROOT resolves to the repo root and the data/config dir constants
are the expected children that survived the role-based reorganization.
"""

from __future__ import annotations

import os
from pathlib import Path

from src.shared import paths
from tests._corpus import skip_without_corpus


def _overridable(env_var: str, default: Path) -> Path:
    """The value ``paths`` binds for an env-overridable constant.

    ``paths`` snapshots the environment at *import* time, so a monkeypatch
    fixture cannot neutralize an exported override — the expectation has to be
    derived the same way the module derives it. Mirrors the
    ``Path(...).expanduser().resolve()`` treatment in ``src/shared/paths.py``.
    """
    raw = os.environ.get(env_var)
    return Path(raw).expanduser().resolve() if raw else default


def test_project_root_is_repo_root():
    # The repo root is the directory that holds src/ and configs/.
    assert (paths.PROJECT_ROOT / "src").is_dir()
    assert (paths.PROJECT_ROOT / "configs").is_dir()
    assert (paths.PROJECT_ROOT / "pyproject.toml").exists()


def test_dir_constants_are_expected_children():
    assert paths.CONFIGS_DIR == paths.PROJECT_ROOT / "configs"
    assert paths.DATA_DIR == paths.PROJECT_ROOT / "data"
    # REPORTS_DIR is deliberately redirectable: attaching an estate exports
    # PDFS_REPORTS_DIR at its views/reports compatibility view, so asserting the
    # repo-local child unconditionally fails whenever the documented setup was
    # followed. Honor the override, and pin the default when it is absent.
    assert paths.REPORTS_DIR == _overridable("PDFS_REPORTS_DIR", paths.DATA_DIR / "reports")
    assert paths.SHARED_PARSED_REPORTS_DIR == paths.DOCUMENT_ESTATE_DIR / "views" / "parsed"
    assert paths.GROUND_TRUTH_DIR == paths.DATA_DIR / "ground_truth"
    assert paths.STYLE_DIR == paths.DATA_DIR / "style"
    assert paths.LATEST_OUTPUT_DIR == paths.OUTPUTS_DIR / "latest"
    assert paths.LATEST_OUTPUT_RECEIPT == paths.OUTPUTS_DIR / "latest_manifest.json"
    assert paths.DELIVERABLE_ARCHIVE_DIR == _overridable(
        "PDFS_DELIVERABLE_ARCHIVE_DIR", paths.OUTPUTS_DIR / "archive" / "deliverables"
    )


def test_key_data_dirs_exist():
    # configs/ is tracked, so it must always be present.
    assert paths.CONFIGS_DIR.is_dir()
    # The corpora back the test fixtures + the dashboard scan, but data/reports/
    # is gitignored and absent from a fresh clone — skip rather than fail there.
    skip_without_corpus("sport")
    assert (paths.REPORTS_DIR / "sport").is_dir()
    assert (paths.REPORTS_DIR / "walmex").is_dir()
