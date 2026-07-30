"""Sector carry-forward parse + ensemble rules (eval_simple_analyst)."""
from __future__ import annotations

import sys
from pathlib import Path

EARNINGS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EARNINGS_ROOT))
sys.path.insert(0, str(EARNINGS_ROOT / "scripts"))

import pandas as pd
import pytest
from openpyxl import Workbook

from eval_simple_analyst import ensemble_hits, sector_map


def test_sector_map_carry_forward(tmp_path):
    sectors = {
        "Transport - Airports": ["GAP", "ASUR", "OMA", "AEROMEX"],
        "Consumer - Supermarkets": ["ALSEA", "WALMEX", "CHDRAUI", "LACOMER"],
        "Consumer - Food & Bev": ["CUERVO", "BIMBO", "KOF", "GRUMA"],
        "Transport - Land": ["FORION", "GMXT", "TRAXION", "TMM"],
        "Industrial Conglomerates": ["ALFA", "ORBIA", "NEMAK", "ALPEK"],
        "Telecom and Media": ["AMX", "TLEVISA", "MEGA", "AXTEL"],
        "Banks and Finance": ["GFNORTE", "GFINBUR", "REGIONAL", "BBAJIO"],
        "Real Estate Trusts": ["FUNO", "FIBRAMQ", "FMTY", "DANHOS"],
        "Mining and Materials": ["GMEXICO", "PENOLES", "MINFRISCO", "AUTLAN"],
        "Consumer Retail": ["LIVERPOOL", "SORIANA", "FRAGUA", "HERDEZ"],
    }
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Hoja1"
    row = 2
    for sector, tickers in sectors.items():
        sheet.cell(row=row, column=2, value=sector)
        row += 1
        for ticker in tickers:
            sheet.cell(row=row, column=2, value=ticker)
            row += 1
    workbook_path = tmp_path / "analyst_sectors.xlsx"
    workbook.save(workbook_path)

    smap = sector_map(workbook_path)
    assert len(smap) == 40 and len(set(smap.values())) == 10
    assert smap["GAP"].startswith("Transport - Airports")
    assert smap["ALSEA"] == "Consumer - Supermarkets"
    assert smap["CUERVO"] == "Consumer - Food & Bev"
    assert "FORION" in smap          # ticker present, sector Transport - Land
    assert smap["FORION"] == "Transport - Land"


def test_ensemble_rules_synthetic():
    d = pd.DataFrame({
        # analyst: 2 strong, 2 soft, 1 neutral, 1 strong-disagree
        "direction":   [1,  -1,  1, -1,  0,  1],
        "strength":    [1.0, 1.0, .5, .5, 0.0, 1.0],
        "model_dir":   [1,  -1, -1,  1,  1, -1],
        "realized_up": [True, False, False, False, True, False],
    })
    a, b = ensemble_hits(d)
    # both agree: rows 0,1 -> both hit
    assert a["n"] == 2 and a["aciertos"] == 2 and a["pct"] == 100.0
    assert a["cobertura_pct"] == pytest.approx(100 * 2 / 5, abs=0.1)
    # strong-overrides: rows 0,1,5 use analyst (hit,hit,miss->realized down
    # with direction +1 = miss); rows 2,3,4 use model (-1 vs down=hit,
    # +1 vs down=miss, +1 vs up=hit) -> 4/6
    assert b["n"] == 6 and b["aciertos"] == 4


def test_model_conviction_boundary():
    s = pd.Series([1.0, -1.0, 1.0001, -1.2, 0.3])
    alta = s.abs() > 1.0
    assert alta.tolist() == [False, False, True, True, False]
