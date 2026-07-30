"""test_income_note.py — header-aligned extraction of the CNBV [800200] income note.

Covers the three real Liverpool layouts (4-col, 2025 reordered, 2-col Q1), the
prefix-collision rows, and the no-regression degrade cases (garbled header,
YTD-only filing, wrong target year, disabled config) where the tier returns {}
so the caller's positional-regex fallback keeps today's behavior.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.extract.income_note import extract_from_income_note
from src.model.financial_model import MetricDef


def _defs() -> list[MetricDef]:
    keys = {
        "revenue": "Ingresos",
        "revenue_commercial": "Venta de bienes",
        "revenue_financial": "Intereses",
        "revenue_real_estate": "Arrendamiento",
    }
    return [
        MetricDef(key=k, label=k, label_es=es, section="income", unit="currency",
                  patterns=[], validate=[], xbrl_concepts=[], aliases=[])
        for k, es in keys.items()
    ]


def _cfg(**over):
    cfg = {
        "company": {"unit": "millions"},
        "income_note": {
            "enabled": True,
            "code": "800200",
            "rows": {
                # commercial = goods + services (operating segment from revenue types)
                "revenue_commercial": ["Venta de bienes", "Servicios"],
                "revenue_financial": "Intereses",
                "revenue_real_estate": "Arrendamiento",
                "revenue": "Total de ingresos",
            },
        },
    }
    cfg["income_note"].update(over)
    return cfg


# Real Liverpool [800200] blocks (trimmed). Full pesos; expect ÷1e6 → millions.

NOTE_4COL = """\
[800200] Notas - Análisis de ingresos y gastos

Concepto                    Acumulado Año Acumulado Año Trimestre Año Actual Trimestre Año
                              Actual     Anterior 2024-07-01 - 2024-09- Anterior
                          2024-01-01 - 2024-09- 2023-01-01 - 2023-09- 30 2023-07-01 - 2023-09-
                              30          30                     30
Ingresos [sinopsis]
Servicios                      1,281,437,000 626,756,000 400,623,000 265,653,000
Venta de bienes               120,133,137,000 110,482,581,000 39,472,800,000 35,967,217,000
Intereses                     13,797,765,000 11,615,692,000 4,686,749,000 4,029,402,000
Arrendamiento                  3,567,829,000 3,408,985,000 1,223,758,000 1,093,782,000
Otros ingresos                  732,955,000 724,377,000 271,385,000 345,697,000
Total de ingresos             139,513,123,000 126,858,391,000 46,055,315,000 41,701,751,000
Total de ingresos financieros  3,222,905,000 1,376,191,000 1,152,364,000 553,804,000
Intereses devengados a cargo   3,035,460,000 3,031,854,000 1,008,530,000 1,008,158,000
"""

NOTE_REORDERED = """\
[800200] Notas - Análisis de ingresos y gastos

Concepto                   Trimestre Año Actual Acumulado Año Trimestre Año Acumulado Año
                           2025-07-01 - 2025-09- Actual Anterior Anterior
                               30      2025-01-01 - 2025-09- 2024-07-01 - 2024-09- 2024-01-01 - 2024-09-
                                           30         30          30
Ingresos [sinopsis]
Servicios                       438,105,000 1,366,580,000 400,623,000 1,281,437,000
Venta de bienes               40,629,723,000 127,939,346,000 39,472,800,000 120,133,137,000
Intereses                      5,421,686,000 16,013,087,000 4,686,749,000 13,797,765,000
Arrendamiento                  1,312,812,000 3,904,512,000 1,223,758,000 3,567,829,000
Total de ingresos             48,061,901,000 150,013,262,000 46,055,315,000 139,513,123,000
Intereses devengados a cargo   1,363,760,000 4,146,548,000 1,008,530,000 3,035,460,000
"""

NOTE_Q1 = """\
[800200] Notas - Análisis de ingresos y gastos

Concepto                                  Acumulado Año Actual Acumulado Año Anterior
                                          2024-01-01 - 2024-03-31 2023-01-01 - 2023-03-31
Ingresos [sinopsis]
Servicios                                        355,770,000      258,124,000
Venta de bienes                                 35,155,657,000  32,487,219,000
Intereses                                       4,370,833,000    3,583,727,000
Arrendamiento                                   1,151,760,000    1,032,614,000
Total de ingresos                               41,220,343,000  37,569,510,000
"""


def test_4col_layout_picks_trimestre_actual_3rd_column():
    out = extract_from_income_note(NOTE_4COL, _defs(), _cfg(), "2024-3T")
    assert round(out["revenue"].current, 3) == 46055.315
    # commercial = Venta de bienes (39,472.8) + Servicios (400.623)
    assert round(out["revenue_commercial"].current, 3) == 39873.423
    assert round(out["revenue_financial"].current, 3) == 4686.749
    assert round(out["revenue_real_estate"].current, 3) == 1223.758
    # prior = prior-year quarter (Trim-Anterior)
    assert round(out["revenue"].prior, 3) == 41701.751
    assert out["revenue"].source_line.startswith("[note] 800200")


def test_reordered_layout_picks_trimestre_actual_1st_column():
    # The regression target: positional "3rd number" here = prior-year quarter.
    # Header alignment must pick the 1st column (Trim-Actual).
    out = extract_from_income_note(NOTE_REORDERED, _defs(), _cfg(), "2025-3T")
    # commercial = 40,629.723 + 438.105
    assert round(out["revenue_commercial"].current, 3) == 41067.828
    assert round(out["revenue"].current, 3) == 48061.901
    assert round(out["revenue_financial"].current, 3) == 5421.686


def test_q1_two_column_layout_uses_acumulado_actual():
    out = extract_from_income_note(NOTE_Q1, _defs(), _cfg(), "2024-1T")
    assert round(out["revenue"].current, 3) == 41220.343
    # commercial = 35,155.657 + 355.770
    assert round(out["revenue_commercial"].current, 3) == 35511.427


def test_prefix_collision_rows_are_not_confused():
    # "Intereses" (revenue) must not pick up "Intereses devengados a cargo"
    # (a financial expense) or "Total de ingresos financieros".
    out = extract_from_income_note(NOTE_4COL, _defs(), _cfg(), "2024-3T")
    assert round(out["revenue_financial"].current, 3) == 4686.749  # not 1,008.53


def test_disabled_config_returns_empty():
    assert extract_from_income_note(NOTE_4COL, _defs(), _cfg(enabled=False), "2024-3T") == {}
    assert extract_from_income_note(NOTE_4COL, _defs(), {"company": {"unit": "millions"}}, "2024-3T") == {}


def test_wrong_target_year_degrades():
    # Header carries only 2024/2023; a 2026 target must degrade (return {}).
    assert extract_from_income_note(NOTE_4COL, _defs(), _cfg(), "2026-3T") == {}


def test_garbled_header_degrades():
    # Strip the type-word header line → no clean Trimestre/Acumulado sequence.
    garbled = NOTE_4COL.replace(
        "Concepto                    Acumulado Año Acumulado Año Trimestre Año Actual Trimestre Año",
        "Concepto",
    )
    assert extract_from_income_note(garbled, _defs(), _cfg(), "2024-3T") == {}


def test_ytd_only_q3_emits_nothing():
    # A Q3 filing with only the two Acumulado columns (no Trimestre column) has no
    # single-quarter value — must emit nothing rather than pass YTD as the quarter.
    ytd_only = """\
[800200] Notas - Análisis de ingresos y gastos

Concepto                                  Acumulado Año Actual Acumulado Año Anterior
                                          2024-01-01 - 2024-09-30 2023-01-01 - 2023-09-30
Venta de bienes                                 120,133,137,000  110,482,581,000
Total de ingresos                               139,513,123,000  126,858,391,000
"""
    assert extract_from_income_note(ytd_only, _defs(), _cfg(), "2024-3T") == {}


def test_real_reports_if_present():
    # Integration: run against the cached Liverpool reports when available.
    base = ROOT / "data" / "reports" / "liverpool"
    cases = {  # (commercial = goods+services, consolidated revenue)
        "2024-3T.md": (39873.423, 46055.315),
        "2025-3T.md": (41067.828, 48061.901),
        "2024-1T.md": (35511.427, 41220.343),
    }
    for fname, (commercial, revenue) in cases.items():
        fp = base / fname
        if not fp.exists():
            continue
        period = fname.replace(".md", "")
        out = extract_from_income_note(fp.read_text(encoding="utf-8"), _defs(), _cfg(), period)
        assert round(out["revenue_commercial"].current, 3) == commercial, fname
        assert round(out["revenue"].current, 3) == revenue, fname
