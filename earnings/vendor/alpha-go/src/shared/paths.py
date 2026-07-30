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
GROUND_TRUTH_DIR = DATA_DIR / "ground_truth"
STYLE_DIR = DATA_DIR / "style"
