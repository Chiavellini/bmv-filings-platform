from __future__ import annotations

from pathlib import Path
import subprocess

from scripts import audit_git_payload as payload


def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "--quiet")
    _git(path, "config", "user.name", "Release Gate Test")
    _git(path, "config", "user.email", "release-gate@example.invalid")
    return path


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)


def test_path_policy_preserves_only_documented_bloomberg_fixture() -> None:
    assert payload.classify_path(
        "soft/data/bloomberg/walmex.csv",
        scope="candidate",
    ) == []

    findings = payload.classify_path(
        "soft/data/bloomberg/ac.csv",
        scope="candidate",
    )
    assert {item.code for item in findings} == {"bloomberg_export"}

    sidecar = payload.classify_path("catalog.sqlite3-wal", scope="candidate")
    assert {item.code for item in sidecar} == {"database_in_git"}

    analyst = payload.classify_path(
        "data/ground_truth/FEMSA_Model_post1Q26_Segments.csv",
        scope="candidate",
    )
    assert {item.code for item in analyst} == {"external_data_path"}

    sentiment = payload.classify_path(
        "alpha-go/eval/sentiment_gold.yaml",
        scope="candidate",
    )
    assert {item.code for item in sentiment} == {"external_research_data"}


def test_secret_finding_never_echoes_the_matched_value() -> None:
    secret = b"ghp_" + (b"A" * 40)
    findings = payload.scan_text(
        b"TOKEN=" + secret,
        scope="candidate",
        path="settings.txt",
    )

    assert {item.code for item in findings} == {"secret_shaped_text"}
    assert all(secret.decode() not in item.detail for item in findings)


def test_url_path_is_not_mistaken_for_a_linux_home_directory() -> None:
    findings = payload.scan_text(
        b"url=https://example.com/home/investors/reports",
        scope="candidate",
        path="issuer.yaml",
    )

    assert not any(item.code == "absolute_personal_path" for item in findings)


def test_history_audit_finds_a_database_deleted_from_head(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    database = repo / "catalog.db"
    database.write_bytes(b"SQLite format 3\x00fixture")
    _commit_all(repo, "add forbidden database")
    database.unlink()
    _commit_all(repo, "remove forbidden database")

    result = payload.audit(repo)

    assert not result.dirty_entries
    assert any(
        item.code == "database_in_git"
        and item.scope == "history"
        and item.path == "catalog.db"
        for item in result.findings
    )
    assert not any(
        item.code == "database_in_git" and item.scope == "candidate"
        for item in result.findings
    )
    assert result.failed


def test_dirty_tree_reports_stale_head_clean_clone_risk(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    script = repo / "scripts" / "certify_clean_clone.py"
    script.parent.mkdir()
    script.write_text("# clean clone fixture\n", encoding="utf-8")
    readme = repo / "README.md"
    readme.write_text("committed\n", encoding="utf-8")
    _commit_all(repo, "initial")
    readme.write_text("working candidate\n", encoding="utf-8")

    result = payload.audit(repo, include_history=False)

    codes = {item.code for item in result.findings}
    assert result.dirty_entries == 1
    assert "dirty_worktree" in codes
    assert "clean_clone_stale_head" in codes
    assert result.failed


def test_clean_safe_candidate_passes_without_history_scan(tmp_path: Path) -> None:
    repo = _repository(tmp_path / "repo")
    (repo / "README.md").write_text("portable source\n", encoding="utf-8")
    _commit_all(repo, "initial")

    result = payload.audit(repo, include_history=False)

    assert result.dirty_entries == 0
    assert result.findings == []
    assert not result.failed
