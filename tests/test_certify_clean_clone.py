from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from scripts.certify_clean_clone import (
    DirtyWorktreeError,
    MODULES,
    candidate_paths,
    check_no_estate_attached,
    create_snapshot_clone,
    inspect_interpreter,
    parse_outcome,
    prepare_candidate,
    selected_modules,
)


def _git(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def _fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@invalid.example")
    (repo / ".gitignore").write_text(".env\nignored/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("committed\n", encoding="utf-8")
    (repo / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "tracked.txt", "deleted.txt")
    _git(repo, "commit", "--quiet", "-m", "fixture HEAD")
    return repo


def test_dirty_head_is_refused_unless_snapshot_is_explicit(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    (repo / "tracked.txt").write_text("worktree\n", encoding="utf-8")

    with pytest.raises(DirtyWorktreeError, match="stale committed HEAD"):
        prepare_candidate(
            repo,
            tmp_path / "candidate",
            tmp_path / "workspace",
            snapshot=False,
        )


def test_snapshot_is_ephemeral_and_uses_git_publishable_paths(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    original_head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "tracked.txt").write_text("worktree\n", encoding="utf-8")
    (repo / "deleted.txt").unlink()
    (repo / "publish-me.txt").write_text("candidate\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=ignored\n", encoding="utf-8")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "cache.bin").write_bytes(b"ignored")
    status_before = _git(
        repo, "status", "--porcelain=v1", "--untracked-files=all"
    ).stdout

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    candidate_root = tmp_path / "candidate"
    candidate = create_snapshot_clone(repo, candidate_root, workspace)

    assert candidate.mode == "worktree snapshot"
    assert candidate.revision != original_head
    assert (candidate_root / "tracked.txt").read_text(encoding="utf-8") == "worktree\n"
    assert not (candidate_root / "deleted.txt").exists()
    assert (candidate_root / "publish-me.txt").read_text(encoding="utf-8") == "candidate\n"
    assert not (candidate_root / ".env").exists()
    assert not (candidate_root / "ignored").exists()
    assert ".env" not in candidate_paths(repo)

    # The snapshot commit is a child of source HEAD, but source itself is unchanged.
    parent = _git(candidate_root, "rev-parse", "HEAD^").stdout.strip()
    assert parent == original_head
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == original_head
    assert (
        _git(repo, "status", "--porcelain=v1", "--untracked-files=all").stdout
        == status_before
    )


def test_clean_head_clone_reports_committed_mode(tmp_path: Path) -> None:
    repo = _fixture_repo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    candidate = prepare_candidate(
        repo,
        tmp_path / "candidate",
        workspace,
        snapshot=False,
    )

    assert candidate.mode == "committed HEAD"
    assert candidate.dirty_entries == 0
    assert (tmp_path / "candidate" / "tracked.txt").is_file()


def test_scope_selection_is_explicit() -> None:
    assert [module.key for module in selected_modules("root")] == ["root"]
    assert [module.key for module in selected_modules("all")] == [
        "root",
        "alpha",
        "soft",
        "earnings",
    ]
    assert [module.python_family for module in MODULES] == [
        (3, 13),
        (3, 12),
        (3, 11),
        (3, 11),
    ]


def test_detached_estate_contract_rejects_data_and_absolute_root(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "estate.json").write_text(
        '{"estate_root": "/Volumes/example/estate"}',
        encoding="utf-8",
    )
    attached = clone / "soft" / "data" / "reports"
    attached.mkdir(parents=True)

    failures = check_no_estate_attached(clone)

    assert any("absolute estate_root" in failure for failure in failures)
    assert any("soft/data/reports is present" in failure for failure in failures)


def test_interpreter_identity_is_machine_readable() -> None:
    import sys

    ok, resolved, version = inspect_interpreter(sys.executable)

    assert ok
    assert Path(resolved).is_absolute()
    assert version == sys.version_info[:3]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "1017 passed, 4 deselected in 1.0s",
            {
                "passed": 1017,
                "failed": 0,
                "skipped": 0,
                "error": 0,
                "collection_errors": 0,
            },
        ),
        (
            "2 failed, 10 passed, 3 skipped, 1 error in 2.0s",
            {
                "passed": 10,
                "failed": 2,
                "skipped": 3,
                "error": 1,
                "collection_errors": 0,
            },
        ),
        (
            "collected 0 items / 2 errors\n2 errors during collection",
            {
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "error": 2,
                "collection_errors": 2,
            },
        ),
    ],
)
def test_parse_outcome(output: str, expected: dict[str, int]) -> None:
    assert parse_outcome(output) == expected
