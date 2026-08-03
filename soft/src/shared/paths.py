"""
Central project paths.

Modules used to locate ``configs/`` and report data via ``Path(__file__).parent``,
which assumed they lived at the repo root. After the role-based reorg they live
under ``src/<role>/``, so all such lookups resolve through here instead.
"""
from __future__ import annotations

from pathlib import Path
import os
import sys

# src/shared/paths.py -> parents[2] == project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

CONFIGS_DIR = PROJECT_ROOT / "configs"
DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = Path(os.environ.get("PDFS_REPORTS_DIR", DATA_DIR / "reports")).expanduser().resolve()
GROUND_TRUTH_DIR = DATA_DIR / "ground_truth"
STYLE_DIR = DATA_DIR / "style"

# Deliverable routes (clean input/output scheme):
#   inputs/<company>.md                       — the markdown spec (IR link + outline)
#   outputs/latest/<Company>.xlsx             — analyst handoff (exactly one workbook)
#   outputs/archive/deliverables/<build>/      — exact prior analyst handoffs
#   outputs/<Company>/{excel,csv,validation}/ — build artifacts and review evidence
# Raw PDFs and parsed markdown stay in REPORTS_DIR (a durable cache), never
# duplicated into the output tree.
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = Path(os.environ.get("PDFS_OUTPUTS_DIR", PROJECT_ROOT / "outputs")).expanduser().resolve()
LATEST_OUTPUT_DIR = OUTPUTS_DIR / "latest"
LATEST_OUTPUT_RECEIPT = OUTPUTS_DIR / "latest_manifest.json"
DELIVERABLE_ARCHIVE_DIR = Path(
    os.environ.get(
        "PDFS_DELIVERABLE_ARCHIVE_DIR",
        OUTPUTS_DIR / "archive" / "deliverables",
    )
).expanduser().resolve()


# ── shared document estate (lazy) ──────────────────────────────────────────────
# Soft remains importable standalone, where REPORTS_DIR defaults to its private
# data/reports/. Deployments can point PDFS_REPORTS_DIR at the root estate's
# views/reports directory without importing the parent package.
#
# This module nevertheless used to walk up the filesystem for the parent's
# estate_bridge.py and call load_estate_bridge() AT IMPORT TIME, binding three
# names — DOCUMENT_ESTATE_DIR, DOCUMENT_ESTATE_DB, SHARED_REPORTS_DIR — that a
# repo-wide grep shows nothing in soft/ ever read. The parsed-report view follows
# the same lazy boundary so standalone Soft imports remain estate-independent.
#
# Because src/download/market_data.py imports this module, that made the entire
# daily pricing path fail at import if the parent's bridge were missing or
# malformed, in exchange for nothing. It also quietly contradicted soft's own
# stated invariant ("never imports from the parent", soft/README.md).
#
# The names still resolve for any future caller, but only when actually touched,
# and the failure is then attributable to the caller rather than to importing an
# unrelated module. Prefer estate_bridge() explicitly over the aliases.
_ESTATE_ATTRIBUTES = {
    "ESTATE_BRIDGE": lambda bridge: bridge,
    "DOCUMENT_ESTATE_DIR": lambda bridge: bridge.estate_root,
    "DOCUMENT_ESTATE_DB": lambda bridge: bridge.catalog_path,
    "SHARED_REPORTS_DIR": lambda bridge: bridge.reports_view_dir,
    "SHARED_PARSED_REPORTS_DIR": lambda bridge: bridge.estate_root / "views" / "parsed",
}


def estate_bridge():
    """Resolve the parent repository's estate bridge on demand.

    Raises ``ModuleNotFoundError`` if soft is checked out standalone, which is a
    supported configuration — soft does not need the estate to build coverage.
    """
    for ancestor in PROJECT_ROOT.parents:
        if (ancestor / "estate_bridge.py").is_file():
            if str(ancestor) not in sys.path:
                sys.path.append(str(ancestor))
            break
    from estate_bridge import load_estate_bridge

    return load_estate_bridge(project_root=PROJECT_ROOT)


def __getattr__(name: str):
    """PEP 562 lazy module attributes for the estate names."""
    resolver = _ESTATE_ATTRIBUTES.get(name)
    if resolver is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return resolver(estate_bridge())
