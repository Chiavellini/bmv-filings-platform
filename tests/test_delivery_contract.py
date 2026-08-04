"""
test_delivery_contract.py — the repository must be deliverable from a clean clone.

Every file listed in DELIVERY_CONTRACT is load-bearing for at least one consumer
(root, Alpha Go, Soft, Earnings).  They were all present on disk but *untracked*
at one point, which meant a fresh ``git clone`` produced a tree where all three
subprojects failed at import: each one imports ``estate_bridge`` at module load.
These tests fail loudly if that regresses.

The bridge must also degrade gracefully — importing root code with no estate
present is a supported state (``validate()`` reports problems, nothing raises),
because that is exactly what a clean clone looks like before a bundle is attached.
"""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest


ROOT = Path(__file__).parent.parent

# Files a consumer cannot function without.  Tracked, not merely present.
DELIVERY_CONTRACT = (
    "estate_bridge.py",       # the one code<->estate bridge; imported by all 3 consumers
    "estate.json",            # the one user-facing location contract
    "pyproject.toml",         # packaging, pytest config, console scripts
    "README.md",              # the entry map
    ".python-version",        # the interpreter this tree is certified on
    ".env.example",           # documents every environment variable consumers read
    "requirements/acquisition-worker.txt",
    "requirements/airflow-native.txt",
    "deploy/airflow/dags/quarterly_estate.py",
    "deploy/airflow/native/.env.example",
    "deploy/airflow/native/launchd/com.bmv.airflow.COMPONENT.plist.template",
    "deploy/airflow/native/systemd/bmv-airflow@.service",
    "scripts/airflow_native_status.py",
    "scripts/configure_airflow.py",
    "scripts/install_airflow_native.sh",
    "scripts/run_airflow_native.sh",
    "src/deployment/airflow_task.py",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _is_git_repo() -> bool:
    return _git("rev-parse", "--git-dir").returncode == 0


@pytest.mark.parametrize("relative_path", DELIVERY_CONTRACT)
def test_contract_file_exists(relative_path: str):
    assert (ROOT / relative_path).is_file(), f"missing delivery file: {relative_path}"


@pytest.mark.parametrize("relative_path", DELIVERY_CONTRACT)
def test_contract_file_is_tracked_in_git(relative_path: str):
    """Present on disk is not enough — a clone only carries what git tracks."""
    if not _is_git_repo():
        pytest.skip("not a git checkout (exported tree); tracking cannot be verified")
    result = _git("ls-files", "--error-unmatch", "--", relative_path)
    assert result.returncode == 0, (
        f"{relative_path} exists but is UNTRACKED — it would be absent from a fresh "
        f"clone, breaking every consumer that imports it. Run: git add {relative_path}"
    )


def test_estate_bridge_imports_and_degrades_without_an_estate(tmp_path):
    """A clean clone has no estate. Import must succeed; validate() must report."""
    absent = tmp_path / "no_such_estate"
    probe = (
        "import estate_bridge as eb;"
        "b = eb.load_estate_bridge();"
        "problems = b.validate();"
        "assert problems, 'expected validate() to report a missing estate';"
        "print('OK')"
    )
    env = {**os.environ, "PDFS_DOCUMENT_ESTATE": str(absent)}
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT, capture_output=True, text=True, env=env, check=False,
    )
    assert result.returncode == 0, (
        "estate_bridge must import and validate cleanly with no estate present; "
        f"got:\n{result.stderr}"
    )


def test_root_paths_import_without_an_estate(tmp_path):
    """``src.shared.paths`` resolves the bridge at import time (103 modules import it),
    so an absent estate must not turn every root import into a hard failure."""
    absent = tmp_path / "no_such_estate"
    env = {**os.environ, "PDFS_DOCUMENT_ESTATE": str(absent)}
    result = subprocess.run(
        [sys.executable, "-c", "import src.shared.paths; print('OK')"],
        cwd=ROOT, capture_output=True, text=True, env=env, check=False,
    )
    assert result.returncode == 0, (
        f"root import chain requires a live estate; got:\n{result.stderr}"
    )


def test_declared_console_scripts_resolve():
    """Every [project.scripts] entry must point at a real importable callable."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    scripts = pyproject.get("project", {}).get("scripts", {})
    assert scripts, "expected at least one declared console script"

    for name, target in scripts.items():
        module_name, _, attribute = target.partition(":")
        module = importlib.import_module(module_name)
        resolved = module
        for part in attribute.split("."):
            resolved = getattr(resolved, part, None)
            assert resolved is not None, f"{name}: cannot resolve {target}"
        assert callable(resolved), f"{name}: {target} is not callable"


def test_declared_runtime_dependencies_are_importable():
    """A dependency the code imports but pyproject does not declare is a hidden
    dependency — it works here and fails on a clean install."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    declared = pyproject.get("project", {}).get("dependencies", [])
    # distribution name -> module name where they differ
    module_for = {
        "PyYAML": "yaml",
        "beautifulsoup4": "bs4",
        "python-dateutil": "dateutil",
    }
    missing: list[str] = []
    for requirement in declared:
        distribution = (
            requirement.split("==")[0].split(">=")[0].split("[")[0].strip()
        )
        module_name = module_for.get(distribution, distribution.replace("-", "_"))
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append(f"{distribution} (import {module_name})")

    assert not missing, "declared dependencies not importable: " + ", ".join(missing)
