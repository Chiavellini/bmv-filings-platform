"""
vendor_sync — copy the reusable infra from the parent repo into alpha-go.

alpha-go is self-contained: it does NOT import from the parent ``src/``. Instead it
vendors (copies) the reusable parent packages under its own ``src/`` so that running
from the alpha-go root, ``import src.<role>.<module>`` resolves to *this* copy.

This script records exactly what was vendored and re-runs the copy so the provenance
is reproducible. It NEVER modifies the parent repo.

Usage (from the alpha-go/ directory):

    python3 scripts/vendor_sync.py --check     # report drift vs the parent, no writes
    python3 scripts/vendor_sync.py --sync      # re-copy from the parent (overwrites alpha-go/src vendored pkgs)

The set of vendored packages is the authoritative list in ``VENDORED_PACKAGES`` and is
mirrored in ``docs/REUSE_MAP.md``.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ALPHA_GO_ROOT = Path(__file__).resolve().parents[1]
PARENT_ROOT = ALPHA_GO_ROOT.parent  # the financial-reports repo root

# Whole packages copied verbatim from <parent>/src into <alpha-go>/src.
VENDORED_PACKAGES = [
    "download",
    "parse",
    "extract",
    "shared",
    "model",
    "excel",
    "eval",
]

# Config assets copied verbatim from <parent>/configs into <alpha-go>/configs.
# "*" means: all *.yaml (company configs, segment outlines, metric_search, xbrl_concepts).
VENDORED_CONFIGS = ["*.yaml"]

# Things we deliberately do NOT vendor:
#   - src/ui/  (the parent's retired Streamlit app — alpha-go ships its own app/)
#   - tests/   (parent tests reference parent corpora — alpha-go has its own tests/)
EXCLUDE = {"__pycache__", "ui"}

# Alpha-go-only NET-NEW modules that live INSIDE a vendored package directory but
# have no counterpart in the parent. --sync is rmtree + copytree, so without this
# backup/restore they are silently DELETED. edgar.py is the SEC EDGAR adapter used
# by scripts/onboard_edgar.py; the parent has no EDGAR adapter at all.
# Paths are relative to <alpha-go>/src/<pkg>/.
NET_NEW = {
    "download": ["edgar.py"],
}


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in EXCLUDE or n.endswith(".pyc")}


def _vendored_files() -> list[tuple[str, Path, Path]]:
    """(label, parent_path, alpha_path) for every file --sync would overwrite."""
    pairs: list[tuple[str, Path, Path]] = []
    for pkg in VENDORED_PACKAGES:
        parent_pkg = PARENT_ROOT / "src" / pkg
        if not parent_pkg.is_dir():
            continue
        for parent_file in sorted(parent_pkg.rglob("*.py")):
            relative = parent_file.relative_to(parent_pkg)
            if any(part in EXCLUDE for part in relative.parts):
                continue
            pairs.append(
                (f"src/{pkg}/{relative}", parent_file, ALPHA_GO_ROOT / "src" / pkg / relative)
            )
    for pattern in VENDORED_CONFIGS:
        for parent_file in sorted((PARENT_ROOT / "configs").glob(pattern)):
            pairs.append(
                (
                    f"configs/{parent_file.name}",
                    parent_file,
                    ALPHA_GO_ROOT / "configs" / parent_file.name,
                )
            )
    return pairs


def drift() -> list[tuple[str, str]]:
    """Files alpha-go has locally modified relative to the parent.

    Only files present in BOTH trees are compared; alpha-go-only additions are
    intentional and are not drift.
    """
    net_new = {f"src/{pkg}/{name}" for pkg, names in NET_NEW.items() for name in names}
    changed: list[tuple[str, str]] = []
    for label, parent_file, alpha_file in _vendored_files():
        if label in net_new or not alpha_file.exists():
            continue
        if parent_file.read_bytes() != alpha_file.read_bytes():
            changed.append((label, "differs from parent"))
    return changed


def report_drift() -> list[tuple[str, str]]:
    changed = drift()
    if not changed:
        print("[vendor_sync] no drift: every vendored file matches the parent.")
        return changed
    print(f"[vendor_sync] {len(changed)} vendored file(s) diverge from the parent:")
    for label, status in changed:
        print(f"  {label} — {status}")
    return changed


def sync(write: bool) -> int:
    """Copy vendored packages + configs from the parent. Returns count of items synced."""
    if not (PARENT_ROOT / "src").is_dir():
        print(f"[vendor_sync] parent src/ not found at {PARENT_ROOT}", file=sys.stderr)
        return -1

    count = 0
    # src/__init__.py marks the namespace package root.
    if write:
        shutil.copy2(PARENT_ROOT / "src" / "__init__.py", ALPHA_GO_ROOT / "src" / "__init__.py")
    for pkg in VENDORED_PACKAGES:
        srcp = PARENT_ROOT / "src" / pkg
        dstp = ALPHA_GO_ROOT / "src" / pkg
        action = "sync" if write else "would-sync"
        print(f"[vendor_sync] {action}: src/{pkg}/")
        if write:
            # Preserve alpha-only net-new modules across the destructive rmtree+copytree.
            preserved = {}
            for rel in NET_NEW.get(pkg, []):
                candidate = dstp / rel
                if candidate.exists():
                    preserved[rel] = candidate.read_bytes()
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
            dst = ALPHA_GO_ROOT / "configs" / cfg.name
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
        changed = report_drift()
        sync(write=False)
        sys.exit(1 if changed else 0)

    # --sync is rmtree + copytree. Anything alpha-go changed locally in a vendored
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
