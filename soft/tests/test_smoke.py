"""Phase-0 smoke tests — prove the scaffold is wired and self-contained.

These assert the *foundation*, not behavior: vendored infra resolves to soft's own copy,
paths re-root inside soft/, the new feature packages import, and no module reaches back
into the parent repo. Feature tests arrive per-phase.
"""
from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

SOFT_ROOT = Path(__file__).resolve().parents[1]


def test_paths_reroot_into_soft():
    from src.shared.paths import CONFIGS_DIR, PROJECT_ROOT
    assert PROJECT_ROOT == SOFT_ROOT
    assert CONFIGS_DIR == SOFT_ROOT / "configs"
    assert CONFIGS_DIR.is_dir()


@pytest.mark.parametrize(
    "mod",
    [
        # vendored core we depend on
        "src.extract.tiered_extract",
        "src.extract.pipeline",
        "src.extract.extract_metrics",
        "src.model.financial_model",
        "src.excel.segments_sheet",
        "src.shared.paths",
        "src.shared.validator",
        "src.eval.verification_gate",
    ],
)
def test_vendored_module_imports(mod):
    assert importlib.import_module(mod) is not None


@pytest.mark.parametrize(
    "mod",
    [
        "src.bloomberg.schema",
        "src.bloomberg.template",
        "src.bloomberg.ingest",
        "src.coverage.spec",
        "src.coverage.fundamentals",
        "src.coverage.peers",
        "src.coverage.valuation",
        "src.coverage.validate",
        "src.sheets.valuation_sheet",
    ],
)
def test_new_modules_import(mod):
    assert importlib.import_module(mod) is not None


def test_self_contained_no_parent_imports():
    """No vendored/new module may reach back into the parent repo."""
    suspects = ["import pdfs", "from pdfs", "Desktop/pdfs/src", "Desktop/pdfs/configs"]
    for path in (SOFT_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for s in suspects:
            assert s not in text, f"{path} references the parent repo ({s!r})"


def test_no_module_level_parent_bridge_import():
    """Self-containment must hold at *import time*, not just as a string check.

    The literal scan above passed for a long time while src/shared/paths.py did
    ``from estate_bridge import load_estate_bridge`` at module scope after walking
    the filesystem upward to find the parent's copy. None of the three names it
    bound were ever read, yet importing any module that pulled in paths.py — which
    includes src/download/market_data.py, the daily pricing path — turned a missing
    or malformed parent bridge into a hard failure here.

    Reaching the estate lazily, inside a function, is fine. Doing it at module
    scope is not.
    """
    offenders: list[str] = []
    for path in (SOFT_ROOT / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # module scope only — nested defs are lazy and allowed
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.split(".")[0] == "estate_bridge" for name in names):
                offenders.append(f"{path.relative_to(SOFT_ROOT)}:{node.lineno}")

    assert not offenders, (
        "module-scope import of the parent's estate_bridge — move it inside a "
        "function so soft still imports standalone:\n  " + "\n  ".join(offenders)
    )


def test_importing_soft_does_not_load_the_parent_bridge():
    """Runtime proof of the same invariant, independent of how the code is written."""
    probe = (
        "import sys;"
        "import src.shared.paths;"
        "import src.download.market_data;"
        "sys.exit(1 if 'estate_bridge' in sys.modules else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=SOFT_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, (
        "importing soft modules pulled in the parent's estate_bridge; soft must "
        f"resolve the estate lazily.\n{result.stderr}"
    )


def test_walmex_config_and_filings_cached():
    """Validate the optional WALMEX corpus when it is attached.

    ``data/reports`` is deliberately excluded from Git, so its absence in a
    clean clone is an environment condition rather than a code failure.
    """
    from src.shared.paths import REPORTS_DIR

    assert (SOFT_ROOT / "configs" / "walmex.yaml").is_file()
    reports = REPORTS_DIR / "walmex"
    if not reports.is_dir():
        pytest.skip("optional WALMEX filing corpus is not attached")
    assert len(list(reports.glob("*.md"))) >= 20
