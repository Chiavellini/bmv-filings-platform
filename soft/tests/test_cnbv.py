"""CNBV bank-capital integration — parse the ICAP_BM table, map issuers→slugs, and expose the
authoritative CET1/ICAP. Offline (no network, no PDF): tests the text parser + mapping directly."""
import pytest

from src.download import cnbv

# A faithful slice of the layout-extracted ICAP_BM text, incl. the tricky long name ("Banco del
# Bajío" is a SINGLE space from its number) and the accented aggregate row that must be excluded.
_SAMPLE = """                        INDICADORES DE CAPITALIZACIÓN
             Institución            CCB      CCF      ICAP    Categoría
Banorte                            19.34    12.75    19.74      I
Inbursa                            23.25    23.25    23.25      I
Compartamos                        32.33    32.33    32.33      I
Banco del Bajío 15.99 15.99 15.99 I
Banregio                           15.06    15.06    15.06      I
Total Banca Múltiple 18.39 17.13 20.32
"""


def test_parse_extracts_each_bank_with_cet1_as_ccf():
    banks = cnbv.parse_icap_text(_SAMPLE)
    # CCF is the CET1 column (Banorte's CET1 12.75 differs from its Tier-1/CCB 19.34 and ICAP 19.74).
    assert banks["banorte"] == {"ccb": 19.34, "cet1": 12.75, "icap": 19.74}
    assert banks["inbursa"]["cet1"] == 23.25
    assert banks["compartamos"]["cet1"] == 32.33


def test_parse_handles_long_name_single_space():
    banks = cnbv.parse_icap_text(_SAMPLE)
    assert banks["banco del bajio"]["cet1"] == 15.99  # accent-stripped key, 1-space split


def test_parse_excludes_total_row():
    banks = cnbv.parse_icap_text(_SAMPLE)
    assert not any(k.startswith("total") for k in banks)


def test_norm_strips_accents_and_case():
    assert cnbv._norm("Banco del Bajío") == "banco del bajio"
    assert cnbv._norm("  BANREGIO  ") == "banregio"


def test_bank_capital_for_slug_maps_universe_banks():
    banks = cnbv.parse_icap_text(_SAMPLE)
    assert cnbv.bank_capital_for_slug("gfnorte", banks)["cet1"] == 12.75    # → Banorte
    assert cnbv.bank_capital_for_slug("gentera", banks)["cet1"] == 32.33    # → Compartamos
    assert cnbv.bank_capital_for_slug("regional", banks)["cet1"] == 15.06   # → Banregio


def test_bank_capital_for_slug_newly_mapped_banks():
    # santander / bbva mexico / monex are in the ICAP table and now mapped — their CET1 was cached
    # but previously unreachable for want of a SLUG_TO_CNBV entry (fills CET1 for 3 more banks).
    sample = _SAMPLE + (
        "Santander                          15.36    14.09    19.51      I\n"
        "BBVA Mexico                        16.88    16.88    20.39      I\n"
        "Monex                              18.83    18.83    18.83      I\n"
    )
    banks = cnbv.parse_icap_text(sample)
    assert cnbv.bank_capital_for_slug("banco_santander_mexico", banks)["cet1"] == 14.09
    assert cnbv.bank_capital_for_slug("bbva_mexico", banks)["cet1"] == 16.88
    assert cnbv.bank_capital_for_slug("monex", banks)["cet1"] == 18.83


def test_bank_capital_for_slug_none_for_non_mapped():
    banks = cnbv.parse_icap_text(_SAMPLE)
    assert cnbv.bank_capital_for_slug("walmex", banks) is None      # not a mapped bank
    assert cnbv.bank_capital_for_slug("gf_multiva", banks) is None  # mapped (→multiva), absent here


def test_parse_rejects_absurd_cet1():
    # a mis-parsed line with an out-of-band CET1 is dropped (never shipped)
    assert cnbv.parse_icap_text("Bogus 0.50 0.50 0.50 I") == {}
