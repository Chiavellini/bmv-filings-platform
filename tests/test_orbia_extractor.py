"""Orbia release extractor — era-stratified fixtures (lines lifted verbatim).

Eras: 2018 Mexichem (Selected Financial Results / split EBT label), 2020 brand
blocks (Vestolit/Netafim, "Total Revenues"/"Net Revenue" rows), 2021 Financial
Highlights, 2024+ bare-year consolidated header, prose-only group quarters
(2023-3T), and the leverage-ratio prose/table variants.
"""

import yaml

from src.extract.extract_metrics import apply_config
from src.extract.orbia import extract_orbia_release
from src.model.financial_model import METRICS


def _defs():
    cfg = yaml.safe_load(open("configs/orbia.yaml"))
    return apply_config(METRICS, cfg)


def test_modern_consolidated_bare_year_header():
    # 2024-2T: consolidated block header is a bare "2024   2023    % Var"
    text = "\n".join([
        "mm US$                            Second Quarter",
        "2024   2023    % Var",
        "Net sales                     1,976  2,177   -9%",
        "Cost of sales                 1,474  1,541   -4%",
        "Operating income              173     297    -42%",
        "EBITDA                        334     444    -25%",
        "EBITDA margin                 16.9%  20.4%  -352 bps",
        "Earnings before taxes         139     161    -13%",
        "Income tax                    (85)    129    N/A",
        "Net majority income           195      8    2309%",
        "Capital expenditures          (107)  (162)   -34%",
        "Free cash inflow (outflow)    (130)   30     N/A",
        "Net debt                      3,838  3,430   12%",
    ])
    rows = extract_orbia_release(text, _defs(), "2024-2T")
    assert rows["revenue"].current == 1976 and rows["revenue"].prior == 2177
    assert rows["ebitda"].current == 334
    assert rows["tax_expense"].current == -85          # tax benefit keeps sign
    assert rows["net_income"].current == 195           # majority line
    assert rows["capex"].current == 107                # outflow → positive
    assert rows["free_cash_flow"].current == -130      # genuine outflow kept
    assert rows["net_debt"].current == 3838


def test_2018_income_statement_split_ebt_and_majority():
    text = "\n".join([
        "Income Statement             2018   2017    %     2018   2017   %",
        "Net sales                   1,785  1,503  19%    5,509  4,360  26%",
        "Cost of sales               1,289  1,129  14%    3,964  3,289  21%",
        "Income (loss) from continuing operations before",
        "                            183     157   17%    607    385    58%",
        "income tax",
        "Consolidated net income (loss) 120   91    32%    437    236    85%",
        "Minority stockholders        37     30    23%    113     56   102%",
        "Net income (loss)             82     61    34%    323    180    79%",
    ])
    rows = extract_orbia_release(text, _defs(), "2018-3T")
    assert rows["revenue"].current == 1785 and rows["revenue"].prior == 1503
    assert rows["ebt"].current == 183 and rows["ebt"].prior == 157
    assert rows["net_income"].current == 82             # majority, not 120
    assert rows["minority_interest"].current == 37


def test_2020_brand_blocks_map_to_modern_keys():
    text = "\n".join([
        "Vestolit                           2020  2019  %Var.",
        "Volume (K tons)                    571    635   -10%",
        "Total Revenues                     427    588   -27%",
        "EBITDA                              64    112   -43%",
        "",
        "Netafim                             2020   2019  %Var.",
        "Net Revenue                         247    295   -16%",
        "EBITDA                               52    58    -10%",
    ])
    rows = extract_orbia_release(text, _defs(), "2020-2T")
    assert rows["ns_polymer"].current == 427 and rows["ns_polymer"].prior == 588
    assert rows["ebitda_polymer"].current == 64
    assert rows["ns_precision_ag"].current == 247
    assert rows["ebitda_precision_ag"].current == 52


def test_prose_only_group_quarter():
    # 2023-3T published Building & Infrastructure figures only in prose
    text = "\n".join([
        "Building and Infrastructure (Wavin), 35% of Revenues",
        "Orbia's Building and Infrastructure business group is redefining pipes.",
        "Revenues of $694 million decreased 1%, EBITDA of $79 million increased 13%.",
    ])
    rows = extract_orbia_release(text, _defs(), "2023-3T")
    assert rows["ns_building"].current == 694
    assert rows["ebitda_building"].current == 79


def test_prose_sentences_never_read_as_table_rows():
    text = "\n".join([
        "Financial Highlights        2020 2019 %Var.",
        "Net sales of $1,412 million decreased 23% compared to $1,839 million.",
    ])
    rows = extract_orbia_release(text, _defs(), "2020-2T")
    assert "revenue" not in rows


def test_leverage_ratio_variants():
    defs = _defs()
    cases = [
        ("Net Debt/EBITDA 12 M                   2.31x        2.05x", 2.31),
        ("we have reached 1.98x net debt to EBITDA ratio at the end of this quarter", 1.98),
        ("The Company's net debt-to-EBITDA ratio decreased from 3.70x to 3.64x compared to the prior", 3.64),
        ("Leverage ratio (net debt- to-EBITDA) decreased to 1.34x, due to the increase in EBITDA", 1.34),
    ]
    for line, want in cases:
        rows = extract_orbia_release(line, defs, "2024-1T")
        assert rows["net_debt_to_ebitda"].current == want, line


def test_net_debt_decomposition_prose():
    text = ("Net debt of $3,838 million includes total debt of $4,635 million, "
            "less cash and cash equivalents of $797 million.")
    rows = extract_orbia_release(text, _defs(), "2024-2T")
    assert rows["net_debt"].current == 3838
    assert rows["total_debt"].current == 4635
    assert rows["cash"].current == 797
