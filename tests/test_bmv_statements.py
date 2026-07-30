"""Tests for the generic CNBV/BMV standardized-statement tier (bmv_statements).

These use small synthetic statement blocks (no PDF), exercising the column logic
that is the correctness core: balance instants extract in every quarter, income
extracts the single-quarter column (Acumulado in Q1, Trimestre in Q2-Q4), and an
Acumulado-only block in Q2-Q4 yields NOTHING (an honest gap, never a YTD value).
"""

from __future__ import annotations

from src.model.financial_model import METRICS, apply_config
from src.extract.bmv_statements import extract_from_statements

CFG = {"company": {"unit": "millions"}}      # values printed in full pesos → millions
MDEFS = apply_config(METRICS, CFG)


def _run(text, period):
    return extract_from_statements(text, MDEFS, CFG, period)


# A balance block ([210000], instant columns: Actual, prior year-end).
BALANCE = """
      [210000] Estado de situación financiera
      Concepto                         Cierre Trimestre Cierre Ejercicio
                                          Actual    Anterior
                                         2024-03-31 2023-12-31
      Efectivo y equivalentes de efectivo   23,286,625,000 29,807,166,000
      Total de activos                     253,778,222,000 259,154,168,000
      Total pasivos                        107,447,713,000 111,654,449,000
      Total de capital contable            146,330,509,000 147,499,719,000
"""

# A Q1 income block ([310000], Acumulado only — YTD == the quarter in Q1).
INCOME_Q1 = """
      [310000] Estado de resultados, resultado del periodo, por función de gasto
      Concepto                          Acumulado Año Actual Acumulado Año Anterior
                                        2024-01-01 - 2024-03-31 2023-01-01 - 2023-03-31
      Ingresos                                41,220,343,000 37,569,510,000
      Costo de ventas                         23,913,087,000 22,352,686,000
      Utilidad bruta                          17,307,256,000 15,216,824,000
      Utilidad (pérdida) de operación          4,557,816,000  3,885,793,000
      Ingresos financieros                       540,424,000    540,437,000
      Utilidad (pérdida) neta                  2,883,648,000  2,146,070,000
"""

# A Q3 income block with both Acumulado and Trimestre columns (4 value cols).
INCOME_Q3_TRIM = """
      [310000] Estado de resultados, resultado del periodo, por función de gasto
      Concepto                  Acumulado Año Acumulado Año Trimestre Año Trimestre Año
                                  Actual    Anterior    Actual    Anterior
                               2024-01-01 - 2024- 2023-01-01 - 2023- 2024-07-01 - 2024- 2023-07-01 - 2023-
                                  09-30      09-30      09-30      09-30
      Ingresos                       139,513,123,000 126,858,391,000 46,055,315,000 41,701,751,000
      Utilidad (pérdida) neta        13,505,653,000 10,821,355,000 4,391,768,000 3,970,932,000
"""

# A Q3 income block with ONLY Acumulado columns (no single-quarter data).
INCOME_Q3_YTD_ONLY = """
      [310000] Estado de resultados, resultado del periodo, por función de gasto
      Concepto                          Acumulado Año Actual Acumulado Año Anterior
                                        2024-01-01 - 2024-09-30 2023-01-01 - 2023-09-30
      Ingresos                                139,513,123,000 126,858,391,000
      Utilidad (pérdida) neta                  13,505,653,000 10,821,355,000
"""


def test_balance_extracts_in_any_quarter():
    for period in ("2024-1T", "2024-3T"):
        out = _run(BALANCE, period)
        assert abs(out["total_assets"].current - 253_778.222) < 0.01
        assert abs(out["total_liabilities"].current - 107_447.713) < 0.01
        assert abs(out["equity"].current - 146_330.509) < 0.01
        assert abs(out["cash"].current - 23_286.625) < 0.01


def test_income_q1_uses_acumulado_as_quarter():
    out = _run(INCOME_Q1, "2024-1T")
    assert abs(out["revenue"].current - 41_220.343) < 0.01
    assert abs(out["cogs"].current - 23_913.087) < 0.01
    assert abs(out["gross_profit"].current - 17_307.256) < 0.01
    assert abs(out["operating_income"].current - 4_557.816) < 0.01
    assert abs(out["net_income"].current - 2_883.648) < 0.01


def test_income_q3_picks_trimestre_column():
    out = _run(INCOME_Q3_TRIM, "2024-3T")
    # the single-quarter (Trimestre) value, NOT the 139,513 YTD figure
    assert abs(out["revenue"].current - 46_055.315) < 0.01
    assert abs(out["net_income"].current - 4_391.768) < 0.01


def test_income_q3_ytd_only_emits_nothing():
    out = _run(INCOME_Q3_YTD_ONLY, "2024-3T")
    assert "revenue" not in out          # never pass off a YTD value as the quarter
    assert "net_income" not in out


def test_income_q1_ytd_only_still_emits():
    # The same YTD-only shape IS valid in Q1 (YTD == the quarter).
    out = _run(INCOME_Q3_YTD_ONLY.replace("09-30", "03-31"), "2024-1T")
    assert abs(out["revenue"].current - 139_513.123) < 0.01


def test_label_precision_ingresos_not_subline():
    # "Ingresos financieros" / "Otros ingresos" must NOT be read as revenue.
    out = _run(INCOME_Q1, "2024-1T")
    assert abs(out["revenue"].current - 41_220.343) < 0.01   # the headline, not 540 (financieros)


def test_unknown_period_returns_empty():
    assert _run(INCOME_Q1, "not-a-period") == {}
