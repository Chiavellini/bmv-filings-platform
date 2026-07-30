"""Per-industry metric profiles — which master columns each industry actually *shows*.

The master matrix is one physical grid (shared column contract in ``columns.py``), but each
industry only applies a subset of the metrics: a bank has no EV/EBITDA, a FIBRA's earnings-based
P/E is structurally N/A (IAS 40 fair-value gains contaminate net income — see
``configs/metric_applicability.yaml``). This module is the single source of truth for that
per-industry subset, derived from the **same** structural-N/A tables ``applicability.py`` uses, so
"the metrics shown per industry" and "the N/A map" can never diverge.

Previously this module also carried a MUTED state (show-but-de-emphasise) for the fair-value-
distorted REIT metrics, duplicated a third time as ``_REIT_FV_DISTORTED`` in
``scripts/build_master.py``. All three copies are gone: those metrics are now na_template (hidden,
not shown-with-an-asterisk) via ``metric_applicability.yaml``, with a written rationale instead of a
hardcoded label set. Only two flags remain.
"""
from __future__ import annotations

from src.coverage.applicability import _structural_na
from src.coverage.columns import COLUMNS

# Flags a column can carry within an industry's profile.
SHOW, OFF = "show", "off"


def industry_profile(template: str, sector: str) -> list[str]:
    """Ordered list of column labels this industry SHOWS (its applicable metrics), in COLUMNS order.
    A label structurally N/A for the ``(template, sector)`` is excluded outright."""
    na = _structural_na(template, sector)
    return [c[2] for c in COLUMNS if c[2] not in na]


def profile_flags(template: str, sector: str) -> dict[str, str]:
    """``{label: SHOW | OFF}`` for every master column, for this industry.

    * ``OFF``  — structurally N/A for the industry (not in its profile).
    * ``SHOW`` — applicable for the industry.
    """
    na = _structural_na(template, sector)
    return {label: (OFF if label in na else SHOW) for (_b, _blk, label, _h, _u, _g) in COLUMNS}
