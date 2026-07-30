#!/usr/bin/env python3
"""Read-only release audit for the Git payload.

The document estate is deliberately not a Git artifact.  This command inspects
the files that Git already tracks plus non-ignored untracked files, and (by
default) every blob reachable from ``HEAD``.  It never stages, modifies, or
deletes anything and never prints a matched credential value.

Examples:

    python3 scripts/audit_git_payload.py
    python3 scripts/audit_git_payload.py --json
    python3 scripts/audit_git_payload.py --allow-dirty --no-history
    python3 scripts/audit_git_payload.py --all-refs
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Iterable


DEFAULT_MAX_BYTES = 50 * 1024 * 1024
MAX_TEXT_SCAN_BYTES = 10 * 1024 * 1024
SYNTHETIC_BLOOMBERG_FIXTURES = {
    "soft/data/bloomberg/walmex.csv",
}

DATABASE_SUFFIXES = (
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".duckdb",
    ".duckdb.wal",
)
GENERATED_SUFFIXES = (
    ".err",
    ".log",
    ".pid",
    ".bak",
)
GENERATED_PATH_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".airflow-venv",
}
EXTERNAL_DATA_PREFIXES = (
    "data/document_estate/",
    "data/ground_truth/",
    "data/reports/",
    "data/verified/",
    "alpha-go/data/corpus/",
    "alpha-go/data/index/",
    "alpha-go/data/raw/",
    "soft/data/cache/",
    "soft/data/cnbv/",
    "soft/data/reports/",
    "earnings/audit/",
    "earnings/data/",
    "earnings/outputs/",
    "earnings/vendor/alpha-go/data/",
)

EXTERNAL_RESEARCH_FILES = {
    "alpha-go/eval/analog_candidates2_unlabeled.yaml",
    "alpha-go/eval/analog_relevance.yaml",
    "alpha-go/eval/analog_relevance2.yaml",
    "alpha-go/eval/sentiment_gold.yaml",
    "alpha-go/eval/sentiment_labels.yaml",
    "alpha-go/eval/sentiment_sample2_labeled.yaml",
    "alpha-go/eval/sentiment_sample2_unlabeled.yaml",
}

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("aws_access_key", re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "github_token",
        re.compile(
            rb"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"
        ),
    ),
    (
        "openai_api_key",
        re.compile(rb"\bsk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    ("slack_token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google_api_key", re.compile(rb"\bAIza[0-9A-Za-z_-]{20,}\b")),
    (
        "private_key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    (
        "credential_url",
        re.compile(
            rb"[A-Za-z][A-Za-z0-9+.-]*://"
            rb"[^/\s:@]{2,}:[^@\s/]{6,}@"
        ),
    ),
)

PERSONAL_PATH_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "mac_user_path",
        re.compile(
            rb"(?<![A-Za-z0-9._:/-])/Users/"
            rb"(?P<owner>[A-Za-z0-9._-]+)(?:/|\b)"
        ),
    ),
    (
        "linux_user_path",
        re.compile(
            rb"(?<![A-Za-z0-9._:/-])/home/"
            rb"(?P<owner>[A-Za-z0-9._-]+)(?:/|\b)"
        ),
    ),
    (
        "mac_volume_path",
        re.compile(
            rb"(?<![A-Za-z0-9._:/-])/Volumes/"
            rb"(?P<owner>[A-Za-z0-9._ -]+)(?:/|\b)"
        ),
    ),
    (
        "windows_user_path",
        re.compile(
            rb"\b[A-Za-z]:\\Users\\(?P<owner>[A-Za-z0-9._-]+)(?:\\|\b)"
        ),
    ),
)
PATH_PLACEHOLDERS = {
    b"...",
    b"example",
    b"path",
    b"name",
    b"user",
    b"username",
}


@dataclass(frozen=True, order=True)
class Finding:
    severity: str
    code: str
    scope: str
    path: str
    detail: str


@dataclass
class AuditResult:
    repo: str
    ref: str
    commit: str
    candidate_files: int
    history_blobs: int
    dirty_entries: int
    findings: list[Finding]

    @property
    def failed(self) -> bool:
        return any(item.severity == "error" for item in self.findings)


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
        raise RuntimeError(stderr.strip() or f"git {' '.join(arguments)} failed")
    return result


def repository_root(path: Path) -> Path:
    result = _git(path, "rev-parse", "--show-toplevel", text=True)
    return Path(result.stdout.strip()).resolve()


def candidate_paths(repo: Path) -> list[str]:
    result = _git(
        repo,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    return sorted(
        {
            item.decode("utf-8", "surrogateescape")
            for item in result.stdout.split(b"\0")
            if item
        }
    )


def dirty_status(repo: Path) -> list[bytes]:
    result = _git(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    return [item for item in result.stdout.split(b"\0") if item]


def classify_path(path: str, *, scope: str) -> list[Finding]:
    normalized = path.replace("\\", "/")
    lowered = normalized.lower()
    pure = PurePosixPath(normalized)
    findings: list[Finding] = []

    def add(code: str, detail: str) -> None:
        findings.append(Finding("error", code, scope, normalized, detail))

    if lowered.endswith(".pdf"):
        add("pdf_in_git", "PDFs belong in the external document estate")
    if lowered.endswith(DATABASE_SUFFIXES):
        add("database_in_git", "database files belong in the external estate")

    name = pure.name.lower()
    if name in {".env", ".env.local", ".env.production"}:
        add("environment_secret_file", "runtime environment files must not be committed")

    if any(part.lower() in GENERATED_PATH_PARTS for part in pure.parts):
        add("generated_cache", "generated cache or virtualenv path is in the payload")
    if lowered.endswith(GENERATED_SUFFIXES) or ".bak-" in name:
        add("generated_artifact", "log, state, or backup artifact is in the payload")
    if normalized == "data/night_start.txt":
        add("generated_artifact", "local run-state marker is in the payload")

    if any(lowered.startswith(prefix) for prefix in EXTERNAL_DATA_PREFIXES):
        add("external_data_path", "external or regenerated data is in the Git payload")
    if normalized in EXTERNAL_RESEARCH_FILES:
        add(
            "external_research_data",
            "labeled or corpus-derived research data requires separate publication review",
        )

    if (
        lowered.startswith("soft/data/bloomberg/")
        and lowered.endswith(".csv")
        and normalized not in SYNTHETIC_BLOOMBERG_FIXTURES
    ):
        add(
            "bloomberg_export",
            "non-synthetic Bloomberg exports require external storage and license review",
        )
    return findings


def _looks_text(data: bytes) -> bool:
    return b"\0" not in data[:8192]


def scan_text(data: bytes, *, scope: str, path: str) -> list[Finding]:
    if not _looks_text(data):
        return []
    findings: list[Finding] = []

    for code, pattern in SECRET_PATTERNS:
        match = pattern.search(data)
        if match is None:
            continue
        if code == "credential_url":
            candidate = match.group(0).lower()
            if any(
                placeholder in candidate
                for placeholder in (
                    b"user:password@",
                    b"username:password@",
                    b"replace-me",
                    b"example",
                )
            ):
                continue
        severity = "warning" if code == "credential_url" else "error"
        findings.append(
            Finding(
                severity,
                "secret_shaped_text",
                scope,
                path,
                f"contains material matching {code}; value intentionally suppressed",
            )
        )

    for code, pattern in PERSONAL_PATH_PATTERNS:
        match = pattern.search(data)
        if match is None:
            continue
        owner = match.groupdict().get("owner", b"").lower()
        if owner in PATH_PLACEHOLDERS:
            continue
        findings.append(
            Finding(
                "error",
                "absolute_personal_path",
                scope,
                path,
                f"contains a host-specific {code}; path intentionally suppressed",
            )
        )
    return findings


def _deduplicate(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(set(findings), key=lambda item: (item.severity, item.code, item.scope, item.path))


def scan_candidate(
    repo: Path,
    paths: Iterable[str],
    *,
    max_bytes: int,
) -> list[Finding]:
    findings: list[Finding] = []
    for relative in paths:
        path = repo / relative
        if not path.exists() and not path.is_symlink():
            # ``git ls-files`` includes tracked paths deleted in the candidate.
            continue
        findings.extend(classify_path(relative, scope="candidate"))
        if not path.is_file() or path.is_symlink():
            continue
        size = path.stat().st_size
        if size >= max_bytes:
            findings.append(
                Finding(
                    "error",
                    "oversized_file",
                    "candidate",
                    relative,
                    f"{size} bytes exceeds the {max_bytes}-byte release limit",
                )
            )
        if size > MAX_TEXT_SCAN_BYTES:
            findings.append(
                Finding(
                    "warning",
                    "content_scan_skipped",
                    "candidate",
                    relative,
                    f"{size} bytes exceeds the bounded text-scan limit",
                )
            )
            continue
        try:
            content = path.read_bytes()
        except OSError as error:
            findings.append(
                Finding(
                    "error",
                    "unreadable_file",
                    "candidate",
                    relative,
                    type(error).__name__,
                )
            )
            continue
        findings.extend(scan_text(content, scope="candidate", path=relative))
    return findings


def _history_objects(repo: Path, revision: str) -> tuple[list[str], dict[str, str]]:
    result = _git(repo, "rev-list", "--objects", revision)
    object_ids: list[str] = []
    paths: dict[str, str] = {}
    for raw_line in result.stdout.splitlines():
        if not raw_line:
            continue
        raw_oid, separator, raw_path = raw_line.partition(b" ")
        oid = raw_oid.decode("ascii")
        object_ids.append(oid)
        if separator:
            paths.setdefault(oid, raw_path.decode("utf-8", "surrogateescape"))
    return object_ids, paths


def scan_history(
    repo: Path,
    revision: str,
    *,
    max_bytes: int,
) -> tuple[int, list[Finding]]:
    object_ids, paths = _history_objects(repo, revision)
    findings: list[Finding] = []
    blob_count = 0

    process = subprocess.Popen(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        for oid in object_ids:
            process.stdin.write(oid.encode("ascii") + b"\n")
            process.stdin.flush()
            header = process.stdout.readline()
            fields = header.rstrip(b"\n").split()
            if len(fields) < 3 or fields[1] == b"missing":
                raise RuntimeError(f"could not inspect Git object {oid}")
            object_type = fields[1]
            size = int(fields[2])
            content: bytes | None
            if object_type == b"blob" and size > MAX_TEXT_SCAN_BYTES:
                content = None
                remaining = size
                while remaining:
                    chunk = process.stdout.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise RuntimeError(f"truncated Git object {oid}")
                    remaining -= len(chunk)
            else:
                content = process.stdout.read(size)
            terminator = process.stdout.read(1)
            if (
                (content is not None and len(content) != size)
                or terminator != b"\n"
            ):
                raise RuntimeError(f"truncated Git object {oid}")
            if object_type != b"blob":
                continue

            blob_count += 1
            path = paths.get(oid, f"<blob:{oid[:12]}>")
            findings.extend(classify_path(path, scope="history"))
            if size >= max_bytes:
                findings.append(
                    Finding(
                        "error",
                        "oversized_file",
                        "history",
                        path,
                        f"{size} bytes exceeds the {max_bytes}-byte release limit",
                    )
                )
            if content is None:
                findings.append(
                    Finding(
                        "warning",
                        "content_scan_skipped",
                        "history",
                        path,
                        f"{size} bytes exceeds the bounded text-scan limit",
                    )
                )
            if content is not None:
                findings.extend(scan_text(content, scope="history", path=path))
    finally:
        process.stdin.close()
        process.stdout.close()
        stderr = process.stderr.read() if process.stderr is not None else b""
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError(stderr.decode("utf-8", "replace").strip())

    return blob_count, findings


def audit(
    repo: Path,
    *,
    ref: str = "HEAD",
    all_refs: bool = False,
    include_history: bool = True,
    allow_dirty: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> AuditResult:
    root = repository_root(repo)
    resolved_ref = "--all" if all_refs else ref
    commit = _git(root, "rev-parse", ref, text=True).stdout.strip()
    paths = candidate_paths(root)
    status = dirty_status(root)
    findings = scan_candidate(root, paths, max_bytes=max_bytes)

    if status:
        severity = "warning" if allow_dirty else "error"
        findings.append(
            Finding(
                severity,
                "dirty_worktree",
                "worktree",
                ".",
                f"{len(status)} changed path entries are not represented by committed HEAD",
            )
        )
        if (root / "scripts" / "certify_clean_clone.py").is_file():
            findings.append(
                Finding(
                    severity,
                    "clean_clone_stale_head",
                    "worktree",
                    "scripts/certify_clean_clone.py",
                    "clean-clone certification clones HEAD and cannot certify these changes",
                )
            )

    history_blobs = 0
    if include_history:
        history_blobs, history_findings = scan_history(
            root,
            resolved_ref,
            max_bytes=max_bytes,
        )
        findings.extend(history_findings)

    return AuditResult(
        repo=str(root),
        ref=resolved_ref,
        commit=commit,
        candidate_files=len(paths),
        history_blobs=history_blobs,
        dirty_entries=len(status),
        findings=_deduplicate(findings),
    )


def _render_text(result: AuditResult, *, limit_per_code: int) -> str:
    state = "FAIL" if result.failed else "PASS"
    lines = [
        f"GIT PAYLOAD AUDIT {state}",
        (
            f"repo={result.repo} ref={result.ref} commit={result.commit[:12]} "
            f"candidate_files={result.candidate_files} "
            f"history_blobs={result.history_blobs} dirty_entries={result.dirty_entries}"
        ),
    ]
    grouped: dict[tuple[str, str], list[Finding]] = {}
    for finding in result.findings:
        grouped.setdefault((finding.severity, finding.code), []).append(finding)
    for (severity, code), findings in sorted(grouped.items()):
        lines.append(f"{severity.upper()} {code}: {len(findings)}")
        for finding in findings[:limit_per_code]:
            lines.append(
                f"  {finding.scope}: {finding.path} — {finding.detail}"
            )
        omitted = len(findings) - limit_per_code
        if omitted > 0:
            lines.append(f"  ... {omitted} additional finding(s) suppressed")
    if not result.findings:
        lines.append("No release-payload violations found.")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument(
        "--all-refs",
        action="store_true",
        help="audit every local ref (useful before a mirror; HEAD is safer for normal pushes)",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="inspect only the working candidate, not committed history",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="report dirty/stale-HEAD state as warnings instead of errors",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help=f"maximum allowed file/blob size (default: {DEFAULT_MAX_BYTES})",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--limit-per-code", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = audit(
            arguments.repo,
            ref=arguments.ref,
            all_refs=arguments.all_refs,
            include_history=not arguments.no_history,
            allow_dirty=arguments.allow_dirty,
            max_bytes=arguments.max_bytes,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"git payload audit could not run: {error}", file=sys.stderr)
        return 2

    if arguments.json:
        payload = {
            "status": "fail" if result.failed else "pass",
            **{
                key: value
                for key, value in asdict(result).items()
                if key != "findings"
            },
            "findings": [asdict(item) for item in result.findings],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            _render_text(
                result,
                limit_per_code=max(0, arguments.limit_per_code),
            )
        )
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
