"""
Central project paths.

Modules used to locate ``configs/`` and report data via ``Path(__file__).parent``,
which assumed they lived at the repo root. After the role-based reorg they live
under ``src/<role>/``, so all such lookups resolve through here instead.
"""
from __future__ import annotations

from pathlib import Path
import os

from estate_bridge import load_estate_bridge

# src/shared/paths.py -> parents[2] == source-checkout root. Packaged workers
# set PDFS_PROJECT_ROOT explicitly so configs and scripts are not inferred from
# the interpreter's site-packages directory.
PROJECT_ROOT = Path(
    os.environ.get("PDFS_PROJECT_ROOT", Path(__file__).resolve().parents[2])
).expanduser().resolve()
ESTATE_BRIDGE = load_estate_bridge(project_root=PROJECT_ROOT)

CONFIGS_DIR = PROJECT_ROOT / "configs"
DATA_DIR = PROJECT_ROOT / "data"
REPORTS_DIR = Path(os.environ.get("PDFS_REPORTS_DIR", DATA_DIR / "reports")).expanduser().resolve()
DOCUMENT_ESTATE_DIR = ESTATE_BRIDGE.estate_root
DOCUMENT_ESTATE_DB = ESTATE_BRIDGE.catalog_path
SHARED_REPORTS_DIR = ESTATE_BRIDGE.reports_view_dir
GROUND_TRUTH_DIR = DATA_DIR / "ground_truth"
STYLE_DIR = DATA_DIR / "style"

# Deliverable routes (clean input/output scheme):
#   inputs/<company>.md                       — the markdown spec (IR link + outline)
#   outputs/latest/<Company>.xlsx             — analyst handoff (exactly one workbook)
#   outputs/<Company>/{excel,csv,validation}/ — build artifacts and review evidence
# Raw PDFs and parsed markdown stay in REPORTS_DIR (a durable cache), never
# duplicated into the output tree.
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = Path(os.environ.get("PDFS_OUTPUTS_DIR", PROJECT_ROOT / "outputs")).expanduser().resolve()
LATEST_OUTPUT_DIR = OUTPUTS_DIR / "latest"
