#!/usr/bin/env python3
"""Certify a Git release candidate from a genuinely clean checkout.

The default mode certifies committed ``HEAD`` only when the source worktree is
clean.  This prevents a successful test of stale committed code from being
mistaken for evidence about uncommitted release work.

Use ``--snapshot`` deliberately when the release candidate is still in the
worktree.  Snapshot mode:

1. clones ``HEAD`` into a temporary repository;
2. overlays exactly the paths Git considers publishable (tracked paths plus
   non-ignored untracked paths);
3. makes an ephemeral commit in that temporary repository; and
4. clones that commit before running any checks.

The real repository is never staged, committed, or modified.  Ignored local
state (including an ignored ``.env``) cannot spill into the snapshot.  A secret
in a tracked or non-ignored path is intentionally still part of the candidate;
run ``scripts/audit_git_payload.py`` as the separate payload/secrets gate.

The historical default remains a root-only certification.  It is now labelled
as such so it cannot be confused with a whole-repository result.  ``--scope
all`` installs and tests the root, alpha-go, soft, and earnings in independent
environments using their declared interpreter families and dependencies.

Examples:

    python3 scripts/certify_clean_clone.py
    python3 scripts/certify_clean_clone.py --snapshot --quick
    python3 scripts/certify_clean_clone.py --snapshot --scope all \\
        --python-root /path/to/python3.13 \\
        --python-alpha /path/to/python3.12 \\
        --python-soft /path/to/python3.11 \\
        --python-earnings /path/to/python3.11
    python3 scripts/certify_clean_clone.py --keep
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]


class CandidateError(RuntimeError):
    """The requested clean-checkout candidate could not be constructed."""


class DirtyWorktreeError(CandidateError):
    """HEAD certification was requested while release work is uncommitted."""


@dataclass(frozen=True)
class Candidate:
    mode: str
    revision: str
    dirty_entries: int
    candidate_files: int


@dataclass(frozen=True)
class ModuleSpec:
    key: str
    label: str
    relative_root: Path
    python_family: tuple[int, int]
    install_arguments: tuple[str, ...]
    marker_expression: str
    required_files: tuple[str, ...]


MODULES: tuple[ModuleSpec, ...] = (
    ModuleSpec(
        key="root",
        label="root platform",
        relative_root=Path("."),
        python_family=(3, 13),
        install_arguments=("-e", ".[test]"),
        marker_expression="not network and not model",
        required_files=(
            "estate_bridge.py",
            "estate.json",
            "pyproject.toml",
            "README.md",
            ".python-version",
            ".env.example",
            "requirements/acquisition-worker.txt",
            "requirements/airflow-native.txt",
            "deploy/airflow/dags/quarterly_estate.py",
            "deploy/airflow/native/.env.example",
            "scripts/configure_airflow.py",
            "scripts/install_airflow_native.sh",
        ),
    ),
    ModuleSpec(
        key="alpha",
        label="alpha-go",
        relative_root=Path("alpha-go"),
        python_family=(3, 12),
        install_arguments=("-r", "requirements.txt"),
        marker_expression="not network and not model",
        required_files=(".python-version", "pyproject.toml", "requirements.txt", "README.md"),
    ),
    ModuleSpec(
        key="soft",
        label="soft",
        relative_root=Path("soft"),
        python_family=(3, 11),
        install_arguments=("-r", "requirements.txt"),
        marker_expression="not network and not model",
        required_files=(".python-version", "pyproject.toml", "requirements.txt", "README.md"),
    ),
    ModuleSpec(
        key="earnings",
        label="earnings",
        relative_root=Path("earnings"),
        python_family=(3, 11),
        install_arguments=("-r", "requirements.txt"),
        marker_expression="not network and not model",
        required_files=(".python-version", "pyproject.toml", "requirements.txt", "README.md"),
    ),
)

EXTERNAL_ATTACHMENTS: tuple[Path, ...] = (
    Path("data/document_estate"),
    Path("data/reports"),
    Path("alpha-go/data/corpus"),
    Path("alpha-go/data/index"),
    Path("alpha-go/data/raw"),
    Path("soft/data/reports"),
    Path("earnings/vendor/alpha-go/data/catalog/documents.db"),
)


def _run(
    command: Sequence[str | os.PathLike[str]],
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [os.fspath(item) for item in command],
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


def _git(
    repo: Path,
    *arguments: str,
    text: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=text,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr if text else result.stderr.decode("utf-8", "replace")
        raise CandidateError(stderr.strip() or f"git {' '.join(arguments)} failed")
    return result


def _step(message: str) -> None:
    print(f"\n\033[1m==> {message}\033[0m", flush=True)


def _decode_paths(raw: bytes) -> list[str]:
    return [
        item.decode("utf-8", "surrogateescape")
        for item in raw.split(b"\0")
        if item
    ]


def candidate_paths(repo: Path) -> list[str]:
    """Return the worktree paths that would be candidates for ``git add``."""
    result = _git(
        repo,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    return sorted(set(_decode_paths(result.stdout)))


def head_paths(repo: Path) -> list[str]:
    result = _git(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD")
    return _decode_paths(result.stdout)


def worktree_status(repo: Path) -> list[bytes]:
    result = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    return [item for item in result.stdout.split(b"\0") if item]


def _validate_relative_git_path(relative: str) -> None:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise CandidateError(f"Git returned an unsafe candidate path: {relative!r}")


def _remove_temp_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _copy_worktree_path(source: Path, destination: Path) -> None:
    _remove_temp_path(destination)
    if source.is_symlink():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(os.readlink(source))
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
    elif source.exists():
        raise CandidateError(
            f"cannot snapshot non-file Git candidate {source}; "
            "submodules must be certified separately"
        )


def clone_repository(source: Path, destination: Path) -> str:
    result = _run(
        ["git", "clone", "--no-hardlinks", "--quiet", str(source), str(destination)]
    )
    if result.returncode != 0:
        raise CandidateError(result.stderr.strip() or "git clone failed")
    head = _git(destination, "rev-parse", "HEAD", text=True)
    return head.stdout.strip()


def materialize_worktree_payload(source: Path, temporary_repo: Path) -> int:
    """Overlay tracked + nonignored worktree state onto a temporary HEAD clone."""
    publishable = candidate_paths(source)
    publishable_set = set(publishable)

    # A path staged or deleted out of the candidate must also disappear from
    # the HEAD clone; merely copying existing paths would leave stale files.
    for relative in head_paths(source):
        _validate_relative_git_path(relative)
        if relative not in publishable_set:
            _remove_temp_path(temporary_repo / relative)

    for relative in publishable:
        _validate_relative_git_path(relative)
        source_path = source / relative
        destination = temporary_repo / relative
        if source_path.is_symlink() or source_path.exists():
            _copy_worktree_path(source_path, destination)
        else:
            # Unstaged deletion of a tracked path.
            _remove_temp_path(destination)
    return len(publishable)


def create_snapshot_clone(source: Path, destination: Path, workspace: Path) -> Candidate:
    """Create and clone an ephemeral commit representing the current worktree."""
    unmerged = _git(source, "ls-files", "-u").stdout
    if unmerged:
        raise CandidateError("cannot snapshot a worktree with unresolved merge entries")

    temporary_repo = workspace / "snapshot-source"
    clone_repository(source, temporary_repo)
    count = materialize_worktree_payload(source, temporary_repo)

    staged = _git(temporary_repo, "add", "-A", "--", text=True, check=False)
    if staged.returncode != 0:
        raise CandidateError(staged.stderr.strip() or "could not stage the temporary snapshot")
    committed = _git(
        temporary_repo,
        "-c",
        "user.name=Clean Clone Certification",
        "-c",
        "user.email=clean-clone@invalid.example",
        "commit",
        "--allow-empty",
        "--no-gpg-sign",
        "--quiet",
        "-m",
        "Ephemeral release-candidate snapshot",
        text=True,
        check=False,
    )
    if committed.returncode != 0:
        raise CandidateError(
            committed.stdout.strip()
            + ("\n" if committed.stdout and committed.stderr else "")
            + committed.stderr.strip()
        )

    revision = clone_repository(temporary_repo, destination)
    return Candidate(
        mode="worktree snapshot",
        revision=revision,
        dirty_entries=len(worktree_status(source)),
        candidate_files=count,
    )


def prepare_candidate(
    source: Path,
    destination: Path,
    workspace: Path,
    *,
    snapshot: bool,
) -> Candidate:
    """Prepare a clean checkout without ever mutating ``source``."""
    dirty = worktree_status(source)
    paths = candidate_paths(source)
    if dirty and not snapshot:
        raise DirtyWorktreeError(
            f"source worktree has {len(dirty)} changed path entries; refusing to "
            "certify stale committed HEAD. Commit the intended release, or rerun "
            "with --snapshot to certify an ephemeral worktree candidate."
        )
    if snapshot:
        return create_snapshot_clone(source, destination, workspace)

    revision = clone_repository(source, destination)
    return Candidate(
        mode="committed HEAD",
        revision=revision,
        dirty_entries=0,
        candidate_files=len(paths),
    )


def selected_modules(scope: str) -> tuple[ModuleSpec, ...]:
    return MODULES if scope == "all" else MODULES[:1]


def check_delivery_contract(clone: Path, modules: Sequence[ModuleSpec]) -> list[str]:
    failures: list[str] = []
    for module in modules:
        module_root = clone / module.relative_root
        for relative in module.required_files:
            path = module_root / relative
            present = path.is_file()
            display = (module.relative_root / relative).as_posix()
            print(f"    {'OK     ' if present else 'MISSING'} {display}")
            if not present:
                failures.append(f"{display} is absent from the release candidate")
    return failures


def check_no_estate_attached(clone: Path) -> list[str]:
    """Prove the candidate is not borrowing this host's external corpora."""
    failures: list[str] = []
    config_path = clone / "estate.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        raw_root = config["estate_root"]
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ValueError("estate_root must be a non-empty string")
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        return [f"estate.json cannot establish the detached-estate contract: {error}"]

    configured_root = Path(raw_root).expanduser()
    if configured_root.is_absolute():
        failures.append(
            "estate.json uses an absolute estate_root; a clean checkout could attach "
            "host data instead of exercising the portable relative default"
        )

    for relative in EXTERNAL_ATTACHMENTS:
        path = clone / relative
        present = path.exists() or path.is_symlink()
        print(f"    {'ATTACHED' if present else 'ABSENT  '} {relative.as_posix()}")
        if present:
            failures.append(
                f"{relative.as_posix()} is present in the candidate; the no-estate "
                "certification would be contaminated"
            )
    return failures


def default_interpreter(module: ModuleSpec) -> str:
    major, minor = module.python_family
    if sys.version_info[:2] == (major, minor):
        return sys.executable
    return shutil.which(f"python{major}.{minor}") or f"python{major}.{minor}"


def inspect_interpreter(executable: str) -> tuple[bool, str, tuple[int, int, int] | None]:
    try:
        result = _run(
            [
                executable,
                "-c",
                (
                    "import json,sys; "
                    "print(json.dumps({'executable':sys.executable,"
                    "'version':list(sys.version_info[:3])}))"
                ),
            ]
        )
    except OSError as error:
        return False, str(error), None
    if result.returncode != 0:
        return False, result.stdout + result.stderr, None
    try:
        payload = json.loads(result.stdout)
        version = tuple(int(item) for item in payload["version"])
        # Preserve a virtualenv launcher path. Resolving its symlink to the base
        # interpreter silently drops that environment's site-packages in
        # --quick mode.
        resolved = str(Path(payload["executable"]).absolute())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        return False, f"could not parse interpreter identity: {error}", None
    if len(version) != 3:
        return False, f"unexpected interpreter version: {version}", None
    return True, resolved, version


def build_environment(
    clone: Path,
    workspace: Path,
    module: ModuleSpec,
    *,
    quick: bool,
    base_executable: str,
) -> tuple[bool, str, str]:
    """Build one module's isolated declared environment."""
    ok, identity, version = inspect_interpreter(base_executable)
    if not ok or version is None:
        return False, base_executable, identity
    if version[:2] != module.python_family:
        expected = ".".join(str(item) for item in module.python_family)
        actual = ".".join(str(item) for item in version)
        return (
            False,
            identity,
            f"{module.label} requires Python {expected}.x; {identity} is Python {actual}",
        )
    if quick:
        return (
            True,
            identity,
            f"reused {identity} (Python {'.'.join(map(str, version))}; --quick)",
        )

    venv_dir = workspace / "venvs" / module.key
    created = _run([identity, "-m", "venv", str(venv_dir)])
    if created.returncode != 0:
        return False, identity, created.stdout + created.stderr

    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    install = _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--no-input",
            *module.install_arguments,
        ],
        cwd=clone / module.relative_root,
    )
    if install.returncode != 0:
        return False, str(python), install.stdout + install.stderr
    return (
        True,
        str(python),
        f"installed {module.label} from its declared dependencies using {identity}",
    )


def run_suite(
    python: str,
    clone: Path,
    workspace: Path,
    module: ModuleSpec,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run one no-network/no-model suite with no document estate attached."""
    module_root = clone / module.relative_root
    report = workspace / f"{module.key}-junit.xml"
    empty_home = workspace / "empty-home"
    empty_home.mkdir(parents=True, exist_ok=True)
    python_path = Path(python)
    path_entries = []
    if python_path.is_absolute():
        path_entries.append(str(python_path.parent))
    path_entries.extend(("/usr/bin", "/bin", "/usr/sbin", "/sbin"))
    environment = {
        "PATH": os.pathsep.join(path_entries),
        "HOME": str(empty_home),
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PDFS_MATERIALIZE": "0",
        "PDFS_ESTATE_BRIDGE": str(clone / "estate.json"),
    }
    # PDFS_DOCUMENT_ESTATE and PDFS_REPORTS_DIR are deliberately absent.
    # Forcing either override changes the semantics that bridge/path unit tests
    # are meant to verify. Pinning PDFS_ESTATE_BRIDGE to the candidate's own
    # relative config prevents inherited host configuration from discovering
    # this machine's live estate.
    result = _run(
        [
            python,
            "-m",
            "pytest",
            "-q",
            "-m",
            module.marker_expression,
            "-p",
            "no:cacheprovider",
            "--tb=line",
            f"--junit-xml={report}",
        ],
        cwd=module_root,
        env=environment,
    )
    return result, result.stdout + result.stderr


def parse_outcome(output: str) -> dict[str, int]:
    counts = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "error": 0,
        "collection_errors": 0,
    }
    patterns = {
        "passed": r"(\d+)\s+passed\b",
        "failed": r"(\d+)\s+failed\b",
        "skipped": r"(\d+)\s+skipped\b",
        "error": r"(\d+)\s+errors?\b",
        "collection_errors": r"(\d+)\s+errors?\s+during\s+collection\b",
    }
    for key, pattern in patterns.items():
        matches = re.findall(pattern, output)
        if matches:
            counts[key] = max(int(item) for item in matches)
    return counts


def evaluate_suite(
    module: ModuleSpec,
    result: subprocess.CompletedProcess[str],
    output: str,
) -> list[str]:
    counts = parse_outcome(output)
    failures: list[str] = []
    prefix = module.label
    if counts["collection_errors"]:
        failures.append(
            f"{prefix}: {counts['collection_errors']} collection error(s) without "
            "an estate"
        )
    if counts["error"]:
        failures.append(f"{prefix}: {counts['error']} test error(s) without an estate")
    if counts["failed"]:
        failures.append(f"{prefix}: {counts['failed']} test failure(s)")
    if result.returncode != 0 and not failures:
        failures.append(
            f"{prefix}: pytest exited {result.returncode} without a parseable failure summary"
        )
    if counts["passed"] == 0 and not failures:
        failures.append(f"{prefix}: no tests passed; the suite did not actually execute")
    return failures


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--keep", action="store_true", help="keep temporary candidate and venvs")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="reuse selected interpreters and skip venv creation/dependency installation",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="certify an ephemeral tracked+nonignored worktree snapshot",
    )
    parser.add_argument(
        "--scope",
        choices=("root", "all"),
        default="root",
        help="root-only (backward-compatible default) or every subproject",
    )
    parser.add_argument(
        "--all-modules",
        action="store_true",
        help="convenience alias for --scope all",
    )
    parser.add_argument("--python-root", help="Python 3.13 executable for the root module")
    parser.add_argument("--python-alpha", help="Python 3.12 executable for alpha-go")
    parser.add_argument("--python-soft", help="Python 3.11 executable for soft")
    parser.add_argument("--python-earnings", help="Python 3.11 executable for earnings")
    return parser


def _interpreter_arguments(arguments: argparse.Namespace) -> dict[str, str | None]:
    return {
        "root": arguments.python_root,
        "alpha": arguments.python_alpha,
        "soft": arguments.python_soft,
        "earnings": arguments.python_earnings,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    scope = "all" if arguments.all_modules else arguments.scope
    modules = selected_modules(scope)
    scope_label = "ALL-MODULE" if scope == "all" else "ROOT-ONLY"

    workspace = Path(tempfile.mkdtemp(prefix="certify-clean-clone-"))
    clone = workspace / "clone"
    failures: list[str] = []
    candidate: Candidate | None = None

    try:
        _step(
            f"Preparing {scope_label.lower()} release candidate "
            f"({'worktree snapshot' if arguments.snapshot else 'committed HEAD'})"
        )
        try:
            candidate = prepare_candidate(
                REPO_ROOT,
                clone,
                workspace,
                snapshot=arguments.snapshot,
            )
        except CandidateError as error:
            failures.append(str(error))
            return _finish(
                failures,
                workspace,
                arguments.keep,
                scope_label=scope_label,
                candidate=None,
            )
        print(
            f"    mode={candidate.mode} revision={candidate.revision[:12]} "
            f"candidate_files={candidate.candidate_files} "
            f"source_dirty_entries={candidate.dirty_entries}"
        )

        _step(f"Checking the {scope_label.lower()} delivery contract")
        failures.extend(check_delivery_contract(clone, modules))

        _step("Proving no external document estate or legacy corpus is attached")
        failures.extend(check_no_estate_attached(clone))

        requested = _interpreter_arguments(arguments)
        for module in modules:
            _step(
                f"Building {module.label} environment "
                f"(Python {module.python_family[0]}.{module.python_family[1]})"
            )
            executable = requested[module.key] or default_interpreter(module)
            ok, python, detail = build_environment(
                clone,
                workspace,
                module,
                quick=arguments.quick,
                base_executable=executable,
            )
            if ok:
                print(f"    {detail}")
            else:
                print(f"    FAILED: {detail}", file=sys.stderr)
                failures.append(
                    f"{module.label}: could not build its declared environment"
                )
                continue

            _step(
                f"Running {module.label} suite "
                f"(-m {module.marker_expression!r}, no estate)"
            )
            result, output = run_suite(python, clone, workspace, module)
            tail = "\n".join(output.strip().splitlines()[-25:])
            print(tail)
            counts = parse_outcome(output)
            print(
                f"\n    passed={counts['passed']} failed={counts['failed']} "
                f"skipped={counts['skipped']} errors={counts['error']} "
                f"collection_errors={counts['collection_errors']} "
                f"returncode={result.returncode}"
            )
            failures.extend(evaluate_suite(module, result, output))

        return _finish(
            failures,
            workspace,
            arguments.keep,
            scope_label=scope_label,
            candidate=candidate,
        )
    finally:
        if not arguments.keep and workspace.exists():
            shutil.rmtree(workspace, ignore_errors=True)


def _finish(
    failures: list[str],
    workspace: Path,
    keep: bool,
    *,
    scope_label: str,
    candidate: Candidate | None,
) -> int:
    print()
    if failures:
        print(f"\033[1m{scope_label} CLEAN-CHECKOUT CERTIFICATION FAILED\033[0m")
        for failure in failures:
            print(f"  - {failure}")
        if keep:
            print(f"\n  candidate kept at {workspace}")
        return 1

    assert candidate is not None
    print(f"\033[1m{scope_label} CLEAN-CHECKOUT CERTIFICATION PASSED\033[0m")
    print(
        f"  Certified {candidate.mode} {candidate.revision[:12]} from an isolated "
        "checkout with no document estate attached."
    )
    if scope_label == "ROOT-ONLY":
        print("  This result does not certify alpha-go, soft, or earnings.")
    if keep:
        print(f"\n  candidate kept at {workspace}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
