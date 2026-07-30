"""
test_soriana_extractor.py — the deterministic income-statement recovery
(src/extract/soriana.py) for Soriana's char-spaced and clean-row layouts.
"""

from __future__ import annotations

from src.extract.soriana import extract_soriana_income_statement
from src.model.financial_model import METRICS

# A char-spaced income-statement column block (as pdfplumber emits it for
# post-2024 Soriana PDFs): rotated company-name garbage, the period header, then
# one number per line in fixed statement order, then the "%" column.
CHAR_SPACED = """\
            In g r e s o s T o t a le s
            (rotated header noise)
                 1 T 2 0 2 6
                 4 0 ,4 4 4
                 3 0 ,4 6 6
                 9 ,9 7 8
                 7 ,6 1 6
                 2 ,3 6 2
                  5 0 9
                 2 ,8 7 1
                 1 ,0 4 9
                 1 ,8 2 2
                  ( 5 8 8 )
                  1 1 3
                  ( 2 6 )
                  ( 5 0 2 )
                   5
                 1 ,3 2 6
                  4 9 2
                  8 3 4
                  8 2 8
                   6
                 1 ,1 6 6
                  %
                 1 0 0
                 7 5 .3
"""

# A clean-row layout (older reports): "Label  current  %  prior  %  var".
CLEAN_ROW = """\
The Company's Total Income reached $41.8 billion pesos.
Net Sales                     41,879 100   39,409 100    6.3
Cost of Sales                 32,142 76.8  30,040 76.2   7.0
Gross Income                  9,738  23.3   9,369 23.8   3.9
Operating Expenses            6,961  16.6   6,509 16.5   6.9
EBITDA                        2,955  7.1    2,906 7.4    1.7
Depreciation and Amortization  966   2.3    812   2.1    18.9
Operating Income              1,989  4.8    2,094 5.3   (5.0)
Comprehensive Financing Income (742) (1.8)  (439) (1.1)  68.9
Earnings Before Tax & Profit Sharing 1,207 2.9 1,595 4.1 (24.3)
Tax Provision                 418   1.0    806  2.0   (48.1)
Net Income                    789   1.9    789  2.0    0.1
"""


def _vals(text):
    rows = extract_soriana_income_statement(text, METRICS, "2026-1T")
    return {k: round(v.current) for k, v in rows.items()}


def test_char_spaced_block_maps_current_quarter_column():
    got = _vals(CHAR_SPACED)
    assert got["revenue"] == 40444
    assert got["cogs"] == 30466
    assert got["gross_profit"] == 9978
    assert got["operating_expense"] == 7616
    assert got["ebitda"] == 2871
    assert got["depreciation"] == 1049
    assert got["operating_income"] == 1822
    assert got["interest_expense"] == 502        # stored positive
    assert got["ebt"] == 1326
    assert got["tax_expense"] == 492
    assert got["net_income"] == 834


def test_clean_row_fallback_and_ignores_prose():
    got = _vals(CLEAN_ROW)
    # Gross from the TABLE row (9,738), not the prose "$41.8 billion" sentence.
    assert got["revenue"] == 41879
    assert got["gross_profit"] == 9738
    assert got["ebitda"] == 2955
    assert got["operating_income"] == 1989
    assert got["interest_expense"] == 742        # parenthesised → positive
    assert got["ebt"] == 1207                     # trailing "& Profit Sharing" tolerated
    assert got["net_income"] == 789


def test_percentage_column_is_rejected():
    # A column whose revenue cell is ~100 (the % column) must not be accepted.
    pct_only = "\n".join(["X", "%"] + ["100", "75", "25", "18", "6", "0",
                                       "7", "2", "5", "1", "0", "0", "1", "0",
                                       "3", "1", "2"])
    assert extract_soriana_income_statement(pct_only, METRICS, "2026-1T") == {}


# ---------------------------------------------------------------------------
# Store-format units & sales-floor area (extract_soriana_stores, text path)
# ---------------------------------------------------------------------------
from src.extract.soriana import extract_soriana_stores

# Modern clean-row layout: "Format curUnits priorUnits curArea pct%".
STORE_MODERN = """\
            Operational Information
            Below is a comparative table by store format at the close of 4Q23.
                                 Units     Sales-Floor Area (sqm)
                      Hiper      368     369   2,646,996 -0.2%
                      Super      130     127   269,482   3.8%
                      Mercado    162     163   703,228   -0.3%
                      Express    106     105   143,744   1.4%
                      City Club   39     37    312,453   5.7%
                      Total      805     801   4,075,903 0.5%
                      Sodimac     13     12    115,122   3.5%

            Total Current Asset         49,734 31.7  49,100 32.4  1.3
            Total Assets               156,852 100   151,504 100   3.5
"""

# 2019–21 layout: Spanish "Hipermercados"/"Soriana Mercado" + two area columns.
STORE_SPANISH = """\
            Operational Information by store format at the close of 2Q2020.
                                 Units            Sales Floor Area
            Hipermercados     376     379   2,689,261 2,736,445
            Soriana Súper     125     126    254,434   257,439
            Soriana Mercado   162     165    707,067   721,068
            Soriana Express   104     103    140,124   138,930
            City Club          34     35     272,151   280,862
            Total             801    808   4,063,036 4,134,744
"""


from src.model.financial_model import apply_config, load_config
from src.shared.paths import CONFIGS_DIR

_SORIANA_DEFS = apply_config(METRICS, load_config(CONFIGS_DIR / "soriana.yaml"))


def _stores(text):
    out = extract_soriana_stores(text, _SORIANA_DEFS, "2023-4T")
    return {k: v.current for k, v in out.items()}


def test_store_modern_layout_units_and_area():
    s = _stores(STORE_MODERN)
    assert s["units_hiper"] == 368 and s["area_hiper"] == 2646996
    assert s["units_express"] == 106 and s["area_express"] == 143744
    assert s["units_cityclub"] == 39 and s["area_cityclub"] == 312453
    assert s["sodimac_units"] == 13 and s["sodimac_area"] == 115122
    # The ops Total (805 / 4,075,903), NOT the balance-sheet "Total Assets".
    assert s["total_units"] == 805 and s["total_sales_floor"] == 4075903


def test_store_spanish_two_area_layout():
    s = _stores(STORE_SPANISH)
    # "Hipermercados" maps to hiper; current (first) area column wins over prior.
    assert s["units_hiper"] == 376 and s["area_hiper"] == 2689261
    assert s["units_mercado"] == 162 and s["area_mercado"] == 707067
    assert s["units_express"] == 104 and s["area_express"] == 140124
    assert s["total_units"] == 801 and s["total_sales_floor"] == 4063036


# ---------------------------------------------------------------------------
# Quarterly capex (extract_soriana_capex) — positive, quarter-anchored only
# ---------------------------------------------------------------------------
from src.extract.soriana import extract_soriana_capex


def _capex(text):
    out = extract_soriana_capex(text, _SORIANA_DEFS, "2025-2T")
    return out["capex"].current if "capex" in out else None


def test_capex_quarterly_prose_variants():
    assert _capex("The capex invested during the quarter was $705 million pesos") == 705
    assert _capex("Capex of $1,476 million pesos in the quarter") == 1476
    assert _capex("Invested Capex of $1.815 billion pesos in the quarter") == 1815
    assert _capex("3Q2020, CAPEX is $668 million pesos") == 668
    assert _capex("CAPEX invertido en el 1T26 fue de $578 millones") == 578


def test_capex_ignores_annual_and_cashflow_negatives():
    # Annual headline must NOT be captured as quarterly capex.
    assert _capex("Annual Capex of $7.5 billion pesos.") is None
    # Cash-flow cumulative outflow (negative) must never be returned.
    assert _capex("Adquisición de inmuebles, mobiliario y equipo (2,587)") is None
    # Social investment is not capex.
    assert _capex("a social investment of $33.1 million pesos in total") is None
