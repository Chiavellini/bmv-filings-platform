"""Exact-hash policy for intentional Alpha/Soft vendored compatibility forks."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
APPS = ("alpha-go", "soft")


def _module(app: str):
    path = ROOT / app / "scripts" / "vendor_sync.py"
    spec = importlib.util.spec_from_file_location(
        f"vendor_sync_{app.replace('-', '_')}", path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("app", APPS)
def test_checked_in_manifest_exactly_approves_current_snapshot(app: str):
    module = _module(app)
    approved, issues = module.approval_status()
    assert approved
    assert not issues
    assert len(approved) == len(module.drift())


@pytest.mark.parametrize("app", APPS)
def test_check_cli_passes_but_sync_without_force_still_refuses(app: str):
    root = ROOT / app
    check = subprocess.run(
        [sys.executable, "scripts/vendor_sync.py", "--check"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert check.returncode == 0, check.stderr
    assert "do not prove semantic parity" in check.stdout

    sync = subprocess.run(
        [sys.executable, "scripts/vendor_sync.py", "--sync"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert sync.returncode == 2
    assert "REFUSING TO SYNC" in sync.stderr


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("hash_field", ("parent_sha256", "vendor_sha256"))
def test_changed_hash_fails_approval(tmp_path: Path, app: str, hash_field: str):
    module = _module(app)
    data = json.loads(module.APPROVAL_MANIFEST.read_text(encoding="utf-8"))
    changed_path = data["approved_divergences"][0]["path"]
    data["approved_divergences"][0][hash_field] = "0" * 64
    manifest = tmp_path / f"{app}-changed.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    _approved, issues = module.approval_status(manifest)

    assert any(
        label == changed_path and "approval hash mismatch" in status
        for label, status in issues
    )


@pytest.mark.parametrize("app", APPS)
def test_unapproved_and_stale_entries_fail(tmp_path: Path, app: str):
    module = _module(app)
    data = json.loads(module.APPROVAL_MANIFEST.read_text(encoding="utf-8"))
    removed = data["approved_divergences"].pop(0)
    matching = next(
        (label, parent, vendor)
        for label, parent, vendor in module._vendored_files()
        if parent.read_bytes() == vendor.read_bytes()
    )
    label, parent, vendor = matching
    data["approved_divergences"].append({
        "path": label,
        "parent_sha256": module._sha256(parent),
        "vendor_sha256": module._sha256(vendor),
        "rationale": "Deliberately stale test approval.",
    })
    manifest = tmp_path / f"{app}-unapproved-stale.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")

    _approved, issues = module.approval_status(manifest)

    assert (removed["path"], "unapproved divergence") in issues
    assert (label, "stale approval: parent and vendor now match") in issues


@pytest.mark.parametrize("app", APPS)
@pytest.mark.parametrize("missing_side", ("parent", "vendor"))
def test_missing_managed_file_is_a_hard_failure(
    tmp_path: Path, monkeypatch, app: str, missing_side: str,
):
    module = _module(app)
    parent = tmp_path / "parent.py"
    vendor = tmp_path / "vendor.py"
    parent.write_text("value = 1\n", encoding="utf-8")
    vendor.write_text("value = 1\n", encoding="utf-8")
    (parent if missing_side == "parent" else vendor).unlink()
    manifest = tmp_path / "empty.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "approved_divergences": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module, "_vendored_files",
        lambda: [("src/example.py", parent, vendor)],
    )
    monkeypatch.setattr(module, "_vendor_only_files", lambda: [])

    _approved, issues = module.approval_status(manifest)

    expected = (
        "managed parent file is missing"
        if missing_side == "parent"
        else "managed vendored file is missing"
    )
    assert ("src/example.py", expected) in issues
