"""
conftest.py — pytest fixtures and ground-truth values for SPORT report tests.

Ground-truth values were manually verified against the source markdown files.
Table-extracted values are exact integers; prose-extracted values may differ
by up to 0.5% from the table's exact value (rounding at 1 decimal of millones).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure project root is on the path when running from tests/
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.extract.extract_metrics import MetricRow, parse_number


# ---------------------------------------------------------------------------
# Ground-truth dictionaries
# Values are in "miles de pesos" (thousands of MXN) unless unit is pct/count/ratio
# ---------------------------------------------------------------------------

# 2026-1T ground truth (manually verified against reportes/2026-1T.md)
SPORT_2026_1T: dict[str, float] = {
    # Income statement
    "revenue":              589_053,   # TABLE: "Total de ingresos 589,053"
    "ebitda":               213_900,   # PROSE ×1000: "$213.9 millones"
    "ebitda_margin":         36.3,     # PROSE: "con IFRS 16 de 36.3%"
    "ebitda_sin_ifrs":       97_300,   # PROSE ×1000: "$97.3 millones"
    "ebitda_margin_sin_ifrs": 16.5,    # PROSE: "del 16.5%"
    "operating_income":     107_900,   # PROSE ×1000: "llegando a $107.9 millones"
    "operating_profit_con_ifrs": 107_900,  # SPORT prose/con IFRS 16
    "operating_profit_sin_ifrs":  55_300,  # SPORT prose/sin IFRS 16
    # Balance sheet
    "cash":                 369_200,   # PROSE ×1000: "$369.2 millones"
    "net_debt":           1_241_000,   # PROSE ×1000: "incluyendo el IFRS 16"
    # Custom KPIs
    "clientes_activos":      96_616,   # TABLE exact
    "net_churn":               5.6,    # TABLE: "5.6%"
    "monthly_visits":       877_678,   # TABLE exact
    "visits_per_member":       9.3,    # TABLE: "9.3"
    "clubs_count":              49,    # PROSE: "con 49 clubes"
    # Revenue sub-lines
    "revenue_memberships":  461_200,   # PROSE ×1000: "$461.2 millones"
    "revenue_sports":        90_100,   # PROSE ×1000: "$90.1 millones"
    "revenue_sponsorships":  37_600,   # PROSE ×1000: "$37.6 millones"
}

# 2024-1T ground truth
SPORT_2024_1T: dict[str, float] = {
    "revenue":            517_708,   # TABLE exact
    "ebitda":             192_119,   # TABLE exact
    "ebitda_margin":       37.1,     # PROSE
    "ebitda_sin_ifrs":     78_000,   # PROSE ×1000: "$78.0 millones"
    "ebitda_margin_sin_ifrs": 15.1,
    "cash":               284_600,   # PROSE ×1000: "$284.6 millones"
    "clientes_activos":    82_379,   # PROSE: "fue 82,379"
    "monthly_visits":     697_764,   # PROSE: "697,764 visitas"
    "visits_per_member":    9.3,
    "clubs_count":          50,
    "revenue_memberships": 399_659,  # TABLE exact
    "operating_expense":   439_700,  # PROSE ×1000: "$439.7 millones"
}

# 2022-1T ground truth (COVID recovery period — EBITDA sin IFRS negative)
SPORT_2022_1T: dict[str, float] = {
    "revenue":            249_793,   # TABLE exact
    "ebitda":              41_209,   # TABLE exact
    "ebitda_margin":       16.5,
    "ebitda_sin_ifrs":    -66_000,   # PROSE ×1000: "-$66.0 millones" — negative
    "cash":                72_800,   # PROSE ×1000: "$72.8 millones"
    "clientes_activos":    59_430,   # TABLE exact
    "net_churn":            2.6,     # TABLE: "2.6%"
    "monthly_visits":     348_037,   # TABLE exact
    "visits_per_member":    6.9,
    "clubs_count":          57,
    "revenue_memberships": 204_103,  # TABLE exact
}

# 2016-1T ground truth (pre-IFRS 16, UAFIDA era)
SPORT_2016_1T: dict[str, float] = {
    "revenue":            314_925,   # TABLE exact: "Total de Ingresos Netos 314,925"
    "ebitda":              45_179,   # TABLE exact: "UAFIDA 45,179"
    "ebitda_margin":       14.3,     # TABLE: "Margen UAFIDA 14.3%"
    "cash":               131_568,   # TABLE exact: "Efectivo y Equivalentes 131,568"
    "net_debt":           302_200,   # PROSE ×1000: "$302.2 millones"
    "net_churn":            5.5,     # PROSE
    "monthly_visits":     518_000,   # PROSE: "518,000 visitas"
    "clubs_count":          47,
}

# 2017-1T — key test: clubs_count must be 49 not 491 (footnote artifact)
SPORT_2017_1T: dict[str, float] = {
    "revenue":   358_252,
    "ebitda":     51_814,
    "clubs_count":   49,   # pdfplumber renders "49¹" as "491" — must be corrected
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _read_report(stem: str) -> str:
    path = ROOT / "data" / "reports" / "sport" / f"{stem}.md"
    if not path.exists():
        pytest.skip(f"Report file not found: {path}")
    # A dataless (iCloud-evicted) file still exists() but read_text() would block for
    # minutes on the fetch — materialize it first (bounded wait). See src/shared/materialize.
    from src.shared.materialize import materialize
    materialize([path], per_file_timeout=180)
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="session", autouse=True)
def _warm_corpus():
    """Fire a non-blocking iCloud download request for data/reports at session start so
    files materialize in the background while the suite runs (opt out with PDFS_MATERIALIZE=0)."""
    if os.environ.get("PDFS_MATERIALIZE", "1") != "0":
        from src.shared.materialize import request_download
        request_download(ROOT / "data" / "reports")
    yield


@pytest.fixture(scope="session")
def sport_2026_text():
    return _read_report("2026-1T")


@pytest.fixture(scope="session")
def sport_2024_text():
    return _read_report("2024-1T")


@pytest.fixture(scope="session")
def sport_2022_text():
    return _read_report("2022-1T")


@pytest.fixture(scope="session")
def sport_2016_text():
    return _read_report("2016-1T")


@pytest.fixture(scope="session")
def sport_2017_text():
    return _read_report("2017-1T")


@pytest.fixture(scope="session")
def sport_metric_defs():
    """MetricDef list with SPORT config applied."""
    from src.model.financial_model import METRICS, apply_config, load_config
    from src.model.sport_metrics import SPORT_EXTENDED_METRICS
    cfg = load_config(ROOT / "configs" / "sport.yaml")
    return apply_config(METRICS, cfg) + SPORT_EXTENDED_METRICS


# ---------------------------------------------------------------------------
# Shared tolerance helper
# ---------------------------------------------------------------------------

def assert_close(actual: float | None, expected: float, unit: str, metric_key: str,
                 source: str = "unknown"):
    """Assert metric value is within tolerance.

    Rules (per plan):
      - Table values: exact match (no tolerance)
      - Prose values: ≤0.5% relative difference
    """
    assert actual is not None, f"{metric_key}: expected {expected}, got None"
    if source == "table":
        assert actual == expected, \
            f"{metric_key} [TABLE]: expected {expected}, got {actual} — must be exact"
    else:
        if expected == 0:
            assert actual == 0, f"{metric_key}: expected 0, got {actual}"
        else:
            rel_err = abs(actual - expected) / abs(expected)
            assert rel_err <= 0.005, (
                f"{metric_key} [PROSE]: expected {expected}, got {actual} "
                f"(relative error {rel_err:.3%} exceeds 0.5% tolerance)"
            )


def pytest_configure(config):
    config.addinivalue_line("markers", "network: tests that hit the real network")
