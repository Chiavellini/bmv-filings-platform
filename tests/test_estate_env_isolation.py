"""The suite must never resolve paths from the operator's shell.

``PDFS_DOCUMENT_ESTATE`` overrides ``estate.json``, and ``src/shared/paths.py``
resolves the bridge at import time.  Before this guard existed, running pytest
in a shell with ``.env`` sourced pointed every default path and every
``tmp_path`` bundle at the real external estate; on 2026-08-07 that leaked
fixture rows into the production catalog.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from tests.conftest import ESTATE_ENV_VARS

ROOT = Path(__file__).resolve().parent.parent

FAKE_ESTATE = "/tmp/definitely-not-the-real-estate"


def test_estate_env_vars_are_absent_during_tests() -> None:
    """The autouse fixture keeps the shell's estate variables out of every test."""
    leaked = {name: os.environ[name] for name in ESTATE_ENV_VARS if name in os.environ}
    assert leaked == {}


def test_estate_root_resolves_inside_the_checkout() -> None:
    """With no estate attached the bridge stays checkout-relative."""
    from src.shared import paths

    assert Path(paths.ESTATE_BRIDGE.estate_root).is_relative_to(ROOT)


def test_import_time_guard_clears_a_sourced_env() -> None:
    """Importing conftest with the variables exported must strip them.

    A function-scoped fixture runs too late: ``tests/conftest.py`` imports
    ``src`` at module level, so the bridge would already have resolved against
    the operator's estate.  This proves the clearing happens first.
    """
    env = dict(os.environ)
    for name in ESTATE_ENV_VARS:
        env[name] = FAKE_ESTATE

    program = (
        "import importlib.util, json, os, sys;"
        "sys.path.insert(0, '.');"
        "spec = importlib.util.spec_from_file_location('_ct', 'tests/conftest.py');"
        "mod = importlib.util.module_from_spec(spec);"
        "spec.loader.exec_module(mod);"
        "print(json.dumps([n for n in mod.ESTATE_ENV_VARS if n in os.environ]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    still_set = completed.stdout.strip().splitlines()[-1]
    assert still_set == "[]", f"conftest left estate variables set: {still_set}"
