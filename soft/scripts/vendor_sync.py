"""
vendor_sync — copy the reusable infra from the parent repo into soft/.

soft/ is self-contained: it does NOT import from the parent ``src/``. Instead it
vendors (copies) the reusable parent packages under its own ``src/`` so that running
from the soft/ root, ``import src.<role>.<module>`` resolves to *this* copy.

This script records exactly what was vendored and re-runs the copy so the provenance
is reproducible. It NEVER modifies the parent repo.

Usage (from the soft/ directory):

    python3 scripts/vendor_sync.py --check     # report drift vs the parent, no writes
    python3 scripts/vendor_sync.py --sync      # re-copy from the parent (overwrites soft/src vendored pkgs)

The set of vendored packages is the authoritative list in ``VENDORED_PACKAGES`` and is
mirrored in ``docs/REUSE_MAP.md``.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SOFT_ROOT = Path(__file__).resolve().parents[1]
PARENT_ROOT = SOFT_ROOT.parent  # the financial-reports repo root

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
                (f"src/{pkg}/{relative}", parent_file, SOFT_ROOT / "src" / pkg / relative)
            )
    for pattern in VENDORED_CONFIGS:
        for parent_file in sorted((PARENT_ROOT / "configs").glob(pattern)):
            pairs.append(
                (f"configs/{parent_file.name}", parent_file, SOFT_ROOT / "configs" / parent_file.name)
            )
    return pairs


def drift() -> list[tuple[str, str]]:
    """Files soft has locally modified relative to the parent.

    Only files present in BOTH trees are compared. Soft-only additions — the
    NET_NEW modules and soft's 160-plus extra company configs — are intentional
    and are not drift.
    """
    net_new = {f"src/{pkg}/{name}" for pkg, names in NET_NEW.items() for name in names}
    changed: list[tuple[str, str]] = []
    for label, parent_file, soft_file in _vendored_files():
        if label in net_new or not soft_file.exists():
            continue
        if parent_file.read_bytes() != soft_file.read_bytes():
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
        changed = report_drift()
        sync(write=False)
        sys.exit(1 if changed else 0)

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
