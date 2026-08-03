"""
vendor_sync — copy the reusable infra from the parent repo into soft/.

soft/ is self-contained: it does NOT import from the parent ``src/``. Instead it
vendors (copies) the reusable parent packages under its own ``src/`` so that running
from the soft/ root, ``import src.<role>.<module>`` resolves to *this* copy.

This script records exactly what was vendored and re-runs the copy so the provenance
is reproducible. It NEVER modifies the parent repo.

Usage (from the soft/ directory):

    python3 scripts/vendor_sync.py --check     # verify drift against exact-hash approvals
    python3 scripts/vendor_sync.py --sync      # re-copy from the parent (overwrites soft/src vendored pkgs)

The set of vendored packages is authoritative in ``VENDORED_PACKAGES`` and
mirrored in ``docs/REUSE_MAP.md``. Intentional compatibility forks are pinned
in ``vendor_divergences.json``; those hashes prove provenance, not semantic
parity with root.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

SOFT_ROOT = Path(__file__).resolve().parents[1]
PARENT_ROOT = SOFT_ROOT.parent  # the financial-reports repo root
APPROVAL_MANIFEST = SOFT_ROOT / "vendor_divergences.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_APPROVAL_CAVEAT = (
    "Exact hashes approve an app-compatibility snapshot only; they do not prove "
    "semantic parity with the parent."
)

# Whole packages copied verbatim from <parent>/src into <soft>/src.
VENDORED_PACKAGES = [
    "download",
    "parse",
    "extract",
    "shared",
    "model",
    "excel",
    "eval",
]

# Config assets copied verbatim from <parent>/configs into <soft>/configs.
# "*.yaml" means: all company configs, segment outlines, metric_search, xbrl_concepts.
VENDORED_CONFIGS = ["*.yaml"]

# Things we deliberately do NOT vendor:
#   - src/ui/  (the parent's retired Streamlit app — soft/ ships its own scripts/)
#   - tests/   (parent tests reference parent corpora — soft/ has its own tests/)
EXCLUDE = {"__pycache__", "ui"}

# Soft-only NET-NEW modules that live INSIDE a vendored package dir but do not exist in
# the parent. A --sync rmtree+copytree would silently delete them, so they are backed up
# before the copy and restored after. Paths are relative to <soft>/src/<pkg>/.
NET_NEW = {
    # market_data/macro: native price + macro sourcing (soft's [price]/[macro] pillars)
    # cnbv: CNBV ICAP bank-capital table (CET1/ICAP ratios per banca-múltiple bank)
    "download": ["market_data.py", "macro.py", "cnbv.py"],
    # Live deps of src/coverage/native.py — a --sync without these entries would
    # silently delete them and break every bank/REIT company's native metrics.
    "extract": ["bank_ratios.py", "fibra_kpis.py"],
}


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in EXCLUDE or n.endswith(".pyc")}


def _vendored_files() -> list[tuple[str, Path, Path]]:
    """(label, parent_path, soft_path) for every file --sync would overwrite."""
    pairs: list[tuple[str, Path, Path]] = [
        (
            "src/__init__.py",
            PARENT_ROOT / "src" / "__init__.py",
            SOFT_ROOT / "src" / "__init__.py",
        )
    ]
    for pkg in VENDORED_PACKAGES:
        parent_pkg = PARENT_ROOT / "src" / pkg
        if not parent_pkg.is_dir():
            continue
        for parent_file in sorted(parent_pkg.rglob("*.py")):
            relative = parent_file.relative_to(parent_pkg)
            if any(part in EXCLUDE for part in relative.parts):
                continue
            pairs.append(
                (f"src/{pkg}/{relative}", parent_file, SOFT_ROOT / "src" / pkg / relative)
            )
    for pattern in VENDORED_CONFIGS:
        for parent_file in sorted((PARENT_ROOT / "configs").glob(pattern)):
            pairs.append(
                (f"configs/{parent_file.name}", parent_file, SOFT_ROOT / "configs" / parent_file.name)
            )
    return pairs


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _vendor_only_files() -> list[str]:
    """Unexpected Python files inside managed packages with no parent counterpart."""
    allowed = {f"src/{pkg}/{name}" for pkg, names in NET_NEW.items() for name in names}
    unexpected: list[str] = []
    for pkg in VENDORED_PACKAGES:
        vendor_pkg = SOFT_ROOT / "src" / pkg
        parent_pkg = PARENT_ROOT / "src" / pkg
        if not vendor_pkg.is_dir():
            continue
        for vendor_file in sorted(vendor_pkg.rglob("*.py")):
            relative = vendor_file.relative_to(vendor_pkg)
            if any(part in EXCLUDE for part in relative.parts):
                continue
            label = f"src/{pkg}/{relative}"
            if label not in allowed and not (parent_pkg / relative).is_file():
                unexpected.append(label)
    return unexpected


def drift() -> list[tuple[str, str]]:
    """Files soft has locally modified relative to the parent.

    Declared Soft-only additions and Soft's extra company configs are intentional
    and are not drift. A missing managed file or undeclared vendor-only Python
    file is drift and blocks ``--sync`` unless explicitly forced.
    """
    changed: list[tuple[str, str]] = []
    for label, parent_file, soft_file in _vendored_files():
        if not parent_file.is_file():
            changed.append((label, "missing from parent"))
            continue
        if not soft_file.is_file():
            changed.append((label, "missing from soft"))
            continue
        if parent_file.read_bytes() != soft_file.read_bytes():
            changed.append((label, "differs from parent"))
    changed.extend((label, "has no parent counterpart or NET_NEW declaration")
                   for label in _vendor_only_files())
    return changed


def _load_approvals(
    manifest_path: Path | None = None,
) -> tuple[dict[str, dict[str, str]], list[tuple[str, str]]]:
    """Load and strictly validate the exact-hash approval manifest."""
    path = manifest_path or APPROVAL_MANIFEST
    issues: list[tuple[str, str]] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, [(str(path), "approval manifest is missing")]
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [(str(path), f"approval manifest is unreadable: {exc}")]
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        issues.append((str(path), "schema_version must be 1"))
        return {}, issues
    entries = data.get("approved_divergences")
    if not isinstance(entries, list):
        issues.append((str(path), "approved_divergences must be a list"))
        return {}, issues

    approvals: dict[str, dict[str, str]] = {}
    for index, entry in enumerate(entries):
        where = f"{path}:approved_divergences[{index}]"
        if not isinstance(entry, dict):
            issues.append((where, "entry must be an object"))
            continue
        label = entry.get("path")
        parent_hash = entry.get("parent_sha256")
        vendor_hash = entry.get("vendor_sha256")
        rationale = entry.get("rationale")
        if not isinstance(label, str) or not label or label.startswith("/") or ".." in Path(label).parts:
            issues.append((where, "path must be a non-empty relative path"))
            continue
        if label in approvals:
            issues.append((label, "duplicate approval"))
            continue
        if not isinstance(parent_hash, str) or not _SHA256_RE.fullmatch(parent_hash):
            issues.append((label, "parent_sha256 must be 64 lowercase hexadecimal characters"))
            continue
        if not isinstance(vendor_hash, str) or not _SHA256_RE.fullmatch(vendor_hash):
            issues.append((label, "vendor_sha256 must be 64 lowercase hexadecimal characters"))
            continue
        if not isinstance(rationale, str) or not rationale.strip():
            issues.append((label, "rationale is required"))
            continue
        approvals[label] = {
            "parent_sha256": parent_hash,
            "vendor_sha256": vendor_hash,
            "rationale": rationale.strip(),
        }
    return approvals, issues


def approval_status(
    manifest_path: Path | None = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ``(approved_divergences, policy_issues)`` for the current trees."""
    approvals, issues = _load_approvals(manifest_path)
    pairs = {label: (parent, vendor) for label, parent, vendor in _vendored_files()}
    approved: list[tuple[str, str]] = []

    for label, (parent_file, vendor_file) in pairs.items():
        if not parent_file.is_file():
            issues.append((label, "managed parent file is missing"))
            continue
        if not vendor_file.is_file():
            issues.append((label, "managed vendored file is missing"))
            continue
        parent_hash = _sha256(parent_file)
        vendor_hash = _sha256(vendor_file)
        entry = approvals.get(label)
        if parent_hash == vendor_hash:
            if entry is not None:
                issues.append((label, "stale approval: parent and vendor now match"))
            continue
        if entry is None:
            issues.append((label, "unapproved divergence"))
            continue
        mismatches = []
        if entry["parent_sha256"] != parent_hash:
            mismatches.append("parent changed")
        if entry["vendor_sha256"] != vendor_hash:
            mismatches.append("vendor changed")
        if mismatches:
            issues.append((label, "approval hash mismatch: " + ", ".join(mismatches)))
        else:
            approved.append((label, entry["rationale"]))

    for label in approvals:
        if label not in pairs:
            issues.append((label, "stale approval: path is no longer parent-managed"))
    for label in _vendor_only_files():
        issues.append((label, "undeclared vendor-only file"))
    return approved, issues


def report_drift() -> list[tuple[str, str]]:
    changed = drift()
    if not changed:
        print("[vendor_sync] no drift: every vendored file matches the parent.")
        return changed
    print(f"[vendor_sync] {len(changed)} vendored file(s) diverge from the parent:")
    for label, status in changed:
        print(f"  {label} — {status}")
    return changed


def report_approval_status() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    approved, issues = approval_status()
    if approved:
        print(
            f"[vendor_sync] {len(approved)} exact-hash approved divergence(s). "
            f"{_APPROVAL_CAVEAT}"
        )
        for label, rationale in approved:
            print(f"  {label} — approved: {rationale}")
    if issues:
        print(f"[vendor_sync] approval policy FAILED with {len(issues)} issue(s):", file=sys.stderr)
        for label, status in issues:
            print(f"  {label} — {status}", file=sys.stderr)
    elif not approved:
        print("[vendor_sync] no drift and no stale approvals.")
    return approved, issues


def sync(write: bool) -> int:
    """Copy vendored packages + configs from the parent. Returns count of items synced."""
    if not (PARENT_ROOT / "src").is_dir():
        print(f"[vendor_sync] parent src/ not found at {PARENT_ROOT}", file=sys.stderr)
        return -1

    count = 0
    # src/__init__.py marks the namespace package root.
    if write:
        shutil.copy2(PARENT_ROOT / "src" / "__init__.py", SOFT_ROOT / "src" / "__init__.py")
    for pkg in VENDORED_PACKAGES:
        srcp = PARENT_ROOT / "src" / pkg
        dstp = SOFT_ROOT / "src" / pkg
        action = "sync" if write else "would-sync"
        print(f"[vendor_sync] {action}: src/{pkg}/")
        if write:
            # Preserve soft-only net-new modules across the destructive rmtree+copytree.
            preserved = {}
            for rel in NET_NEW.get(pkg, []):
                f = dstp / rel
                if f.exists():
                    preserved[rel] = f.read_bytes()
            if dstp.exists():
                shutil.rmtree(dstp)
            shutil.copytree(srcp, dstp, ignore=_ignore)
            for rel, data in preserved.items():
                (dstp / rel).write_bytes(data)
            if preserved:
                print(f"[vendor_sync]   preserved net-new: {', '.join(sorted(preserved))}")
        count += 1

    for pattern in VENDORED_CONFIGS:
        for cfg in sorted((PARENT_ROOT / "configs").glob(pattern)):
            dst = SOFT_ROOT / "configs" / cfg.name
            if write:
                shutil.copy2(cfg, dst)
            count += 1
    print(f"[vendor_sync] {'synced' if write else 'pending'} {count} package/config items.")
    return count


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--sync", action="store_true", help="re-copy vendored infra from the parent")
    g.add_argument("--check", action="store_true", help="dry-run; report drift vs the parent")
    ap.add_argument(
        "--force",
        action="store_true",
        help="sync even though local changes to vendored files would be discarded",
    )
    args = ap.parse_args()

    if args.check:
        _approved, issues = report_approval_status()
        sync(write=False)
        sys.exit(1 if issues else 0)

    # --sync is rmtree + copytree. Anything soft changed locally in a vendored
    # file is destroyed with no undo, so refuse rather than silently discard it.
    changed = drift()
    if changed and not args.force:
        print(
            f"\n[vendor_sync] REFUSING TO SYNC: {len(changed)} vendored file(s) have local "
            "changes that --sync would permanently discard.",
            file=sys.stderr,
        )
        for label, status in changed:
            print(f"  {label} — {status}", file=sys.stderr)
        print(
            "\nUpstream the changes into the parent first, or re-run with --force to "
            "discard them deliberately.",
            file=sys.stderr,
        )
        sys.exit(2)
    if changed:
        print(f"[vendor_sync] --force: discarding local changes to {len(changed)} file(s).")

    rc = sync(write=True)
    sys.exit(0 if rc >= 0 else 1)


if __name__ == "__main__":
    main()
