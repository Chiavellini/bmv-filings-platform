"""Yahoo key-stats fallback for dividend / distribution yield — the guard that resolves Yahoo's
computed yield into a subject fill, and the workbook note that labels it by provenance. All offline
(pure functions; no network)."""
from __future__ import annotations

from src.coverage.native import _yahoo_yield_fill
from src.coverage.valuation import _yahoo_note


def _ks(dy):
    return {"symbol": "X.MX", "dividend_yield": dy}


def test_industrial_fill_maps_to_dvd_yield():
    assert _yahoo_yield_fill(_ks(6.8), "industrial") == ("dvd_yield", 6.8)


def test_financials_template_uses_dvd_yield():
    assert _yahoo_yield_fill(_ks(4.2), "financials") == ("dvd_yield", 4.2)


def test_reit_fill_maps_to_distribution_yield():
    assert _yahoo_yield_fill(_ks(9.1), "reit") == ("distribution_yield", 9.1)


def test_yield_above_band_rejected():
    # 25% is outside the 0-15% plausibility band → dropped, never shipped.
    assert _yahoo_yield_fill(_ks(25.0), "industrial") is None


def test_zero_and_negative_rejected():
    assert _yahoo_yield_fill(_ks(0.0), "industrial") is None
    assert _yahoo_yield_fill(_ks(-1.0), "industrial") is None


def test_missing_or_none_keystats_yields_nothing():
    assert _yahoo_yield_fill(None, "industrial") is None
    assert _yahoo_yield_fill({}, "industrial") is None
    assert _yahoo_yield_fill(_ks(None), "industrial") is None


def test_bool_is_not_a_valid_yield():
    # guard against a stray True (== 1) sneaking through the numeric check
    assert _yahoo_yield_fill(_ks(True), "industrial") is None


class _Pack:
    def __init__(self, filled):
        self.yahoo_filled = set(filled)


def test_note_tags_yahoo_sourced_dividend_cell():
    pack = _Pack({"dvd_yield"})
    assert _yahoo_note(pack, "dvd_yield", "") == "source: Yahoo"
    assert _yahoo_note(pack, "dvd_yield", "+3% vs peer median") == "+3% vs peer median · source: Yahoo"


def test_note_tags_reit_distribution_cell():
    # REIT distribution surfaces under the 'dist_yield' multiple key, backed by 'distribution_yield'.
    pack = _Pack({"distribution_yield"})
    assert _yahoo_note(pack, "dist_yield", "") == "source: Yahoo"


def test_note_untouched_when_not_yahoo_sourced():
    pack = _Pack(set())
    assert _yahoo_note(pack, "dvd_yield", "note") == "note"
    # a non-yield multiple is never tagged even if the set is non-empty
    assert _yahoo_note(_Pack({"dvd_yield"}), "pe_ltm", "note") == "note"
