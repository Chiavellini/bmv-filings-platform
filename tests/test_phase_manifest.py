from __future__ import annotations

import importlib
import importlib.util
import re
import subprocess
from pathlib import Path

from src.shared.phase_registry import (
    DEFAULT_MANIFEST_PATH,
    REQUIRED_PRODUCT_PHASES,
    load_phase_registry,
)


ROOT = Path(__file__).parent.parent


def test_phase_manifest_loads_expected_product_graph():
    registry = load_phase_registry()

    assert registry.version == 1
    assert tuple(phase.id for phase in registry.product_phases) == REQUIRED_PRODUCT_PHASES
    assert registry.phase("download").downstream == ("parse",)
    assert registry.phase("parse").upstream == ("download",)
    assert registry.phase("extract").downstream == ("generate_excel", "cli")
    assert "validate" in {gate.id for gate in registry.quality_gates}
    assert "evaluate" in {gate.id for gate in registry.quality_gates}


def test_phase_manifest_file_paths_exist():
    registry = load_phase_registry()
    assert DEFAULT_MANIFEST_PATH.exists()

    for phase in (*registry.product_phases, *registry.quality_gates):
        package_path = ROOT / phase.package.replace(".", "/")
        module_path = package_path.with_suffix(".py")
        assert package_path.exists() or module_path.exists(), (
            f"missing package/module for {phase.id}: {phase.package}"
        )

        for test_ref in phase.tests:
            test_path = ROOT / test_ref.split("::", 1)[0]
            assert test_path.exists(), f"missing test reference for {phase.id}: {test_ref}"


def test_phase_manifest_python_entrypoints_resolve():
    registry = load_phase_registry()

    for phase in (*registry.product_phases, *registry.quality_gates):
        for entrypoint in phase.entrypoints:
            if entrypoint.kind != "python":
                continue
            module_name, attr = entrypoint.symbol.split(":", 1)
            assert importlib.util.find_spec(module_name), module_name
            module = importlib.import_module(module_name)
            target = module
            for part in attr.split("."):
                target = getattr(target, part)
            assert callable(target), entrypoint.symbol


def test_phase_manifest_command_entrypoints_are_explicit():
    registry = load_phase_registry()
    cli = registry.phase("cli")

    assert any(
        entrypoint.kind == "command"
        and entrypoint.command == "python3 scripts/build_segments.py <company>.md"
        for entrypoint in cli.entrypoints
    )


def test_cli_phase_manifest_declares_single_file_analyst_handoff():
    cli = load_phase_registry().phase("cli")
    latest = next(item for item in cli.outputs if item.name == "latest_analyst_workbook")

    assert latest.artifacts == ("outputs/latest/<Company>.xlsx",)
    assert "exactly one .xlsx" in latest.contract


def test_extract_phase_manifest_references_semantic_search_dictionary():
    registry = load_phase_registry()
    extract = registry.phase("extract")

    assert any(
        item.name == "search_dictionary" and "configs/metric_search.yaml" in item.contract
        for item in extract.inputs
    )
    assert any(
        entrypoint.symbol == "src.extract.semantic_search:score_metric_label"
        for entrypoint in extract.entrypoints
    )
    assert "tests/test_semantic_search.py" in extract.tests
    assert "tests/test_measure_semantic_search.py" in extract.tests
    assert any("scripts/measure_semantic_search.py" in command for command in extract.commands)
    assert any("data/ground_truth" in gate for gate in extract.quality_gates)


def _is_git_ignored(relative_path: str) -> bool:
    """True when a path is a declared generated location (and so may be absent).

    Both spellings are tried: a directory-only pattern such as ``data/reports/``
    does not match the bare path ``data/reports`` when that path does not exist
    on disk, because git cannot tell it is a directory. A fresh clone is exactly
    the case where it does not exist.
    """
    for candidate in (relative_path, relative_path.rstrip("/") + "/"):
        result = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-q", "--", candidate],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return True
    return False


def test_manifest_artifact_roots_are_real_or_generated():
    """Declared output locations must exist, or be declared generated.

    The manifest calls itself the authoritative phase graph, so a path here that
    no longer exists is worse than no documentation: it previously pointed
    generate_excel at ``excels/``, a directory deleted long before.

    A generated location — ``outputs/``, ``data/reports/`` — is legitimately
    absent from a fresh clone because it is gitignored. That is fine. What is
    not fine is a path that neither exists nor is declared generated, which is
    precisely what a deleted-and-forgotten directory looks like.

    Only the fixed prefix is checked: the part before any ``<placeholder>`` or
    glob, since leaf files are per-company.
    """
    registry = load_phase_registry()
    missing: list[str] = []

    for phase in (*registry.product_phases, *registry.quality_gates):
        for item in phase.outputs:
            for artifact in getattr(item, "artifacts", ()):
                fixed_parts: list[str] = []
                for part in Path(artifact.strip('"')).parts:
                    if "<" in part or "*" in part or "?" in part:
                        break
                    fixed_parts.append(part)
                if not fixed_parts:
                    continue
                relative = "/".join(fixed_parts)
                if (ROOT / relative).exists() or _is_git_ignored(relative):
                    continue
                missing.append(
                    f"{phase.id}: {artifact} — root {relative!r} does not exist and is "
                    "not a declared generated location"
                )

    assert not missing, "phase manifest declares unusable artifact paths:\n" + "\n".join(
        f"  {entry}" for entry in missing
    )


def test_manifest_command_scripts_exist():
    """Every ``scripts/...py`` named in a manifest command must be a real file."""
    registry = load_phase_registry()
    referenced = re.compile(r"(scripts/[\w/]+\.py)")
    missing: list[str] = []

    for phase in (*registry.product_phases, *registry.quality_gates):
        for command in phase.commands:
            for script in referenced.findall(command):
                if not (ROOT / script).is_file():
                    missing.append(f"{phase.id}: {script}")

    assert not missing, "phase manifest commands reference missing scripts:\n" + "\n".join(
        f"  {entry}" for entry in missing
    )
