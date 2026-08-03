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

# Alpha Go is a self-contained vendored application. Its project/config/data
# root must remain Alpha's checkout even when a parent worker exports its own
# ``PDFS_PROJECT_ROOT``.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# The bridge has a unique top-level module name so Alpha Go can keep its
# vendored ``src`` package while sharing the parent estate location contract.
for _ancestor in PROJECT_ROOT.parents:
    if (_ancestor / "estate_bridge.py").is_file():
        if str(_ancestor) not in sys.path:
            sys.path.append(str(_ancestor))
        break
from estate_bridge import load_estate_bridge  # noqa: E402

ESTATE_BRIDGE = load_estate_bridge(project_root=PROJECT_ROOT)

CONFIGS_DIR = PROJECT_ROOT / "configs"
DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = Path(os.environ.get("PDFS_REPORTS_DIR", DATA_DIR / "reports")).expanduser().resolve()
DOCUMENT_ESTATE_DIR = ESTATE_BRIDGE.estate_root
DOCUMENT_ESTATE_DB = ESTATE_BRIDGE.catalog_path
SHARED_REPORTS_DIR = ESTATE_BRIDGE.reports_view_dir
# The root derivative consumer writes versioned Markdown here. Onboarding
# reads it together with SHARED_REPORTS_DIR as a zero-copy union.
SHARED_PARSED_REPORTS_DIR = DOCUMENT_ESTATE_DIR / "views" / "parsed"
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
# Receipt is deliberately adjacent to, not inside, ``latest`` so the handoff
# directory remains exactly one workbook while its cryptographic provenance is
# still machine-readable.
LATEST_OUTPUT_RECEIPT = OUTPUTS_DIR / "latest_manifest.json"
DELIVERABLE_ARCHIVE_DIR = Path(
    os.environ.get("PDFS_DELIVERABLE_ARCHIVE_DIR", OUTPUTS_DIR / "archive" / "deliverables")
).expanduser().resolve()
