"""certify_master — the GAP==0 gate that certifies the sheet is a flawless free-and-auto 100% fill.
Offline; fixtures use real UNIVERSE names (Walmex/GFNorte) so the roster join resolves. has_5y is
patched True so the CAGR columns are deterministically applicable."""
import csv

import pytest

from scripts import certify_master
from src.coverage.columns import COLUMNS

_HEADERS = ["Company", "Sector", "Template", "Ticker"] + [c[3] for c in COLUMNS]


@pytest.fixture(autouse=True)
def _stable_history(monkeypatch):
    # ≥5 annual filings → the 5y CAGR/σ AND 1y-YoY columns are deterministically applicable.
    from src.coverage import applicability
    monkeypatch.setattr(applicability, "_annual_periods", lambda slug: 5)
    # isolate from the generated residual-N/A override so gap fixtures behave rule-deterministically
    monkeypatch.setattr(applicability, "_RESIDUAL_NA", {})


def _write_csv(tmp_path, rows):
    """rows: list of (name, sector, template, clave, {header: value}). Missing headers → filled '10.0'
    unless the value is given (use '' for a gap, 'N/A' for a structural blank)."""
    p = tmp_path / "master.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(_HEADERS)
        for name, sector, template, clave, over in rows:
            cells = [over.get(h, "10.0") for h in _HEADERS[4:]]
            w.writerow([name, sector, template, clave] + cells)
    return p


def test_passes_on_complete_fixture(tmp_path):
    # every metric filled → na cells ignored, all applicable filled → GAP 0
    csvp = _write_csv(tmp_path, [
        ("Walmex", "Retail Selfservice", "industrial", "WALMEX", {}),
        ("GFNorte", "Banks", "financials", "GFNORTE", {}),
    ])
    _pg, gaps, totals = certify_master.certify(csvp)
    assert gaps == []
    n_filled, n_applicable, n_gap, _na_template, _na_source, _total = totals
    assert n_gap == 0 and n_filled == n_applicable > 0


def test_flags_a_gap(tmp_path):
    csvp = _write_csv(tmp_path, [
        ("Walmex", "Retail Selfservice", "industrial", "WALMEX", {"ROE": ""}),
    ])
    _pg, gaps, _t = certify_master.certify(csvp)
    assert ("Walmex", "ROE", "source-gap") in gaps


def test_na_cell_is_not_a_gap(tmp_path):
    # A bank's EV/EBITDA is structural N/A; leaving it "N/A" must NOT be a gap.
    csvp = _write_csv(tmp_path, [
        ("GFNorte", "Banks", "financials", "GFNORTE", {"EV/EBITDA": "N/A"}),
    ])
    _pg, gaps, _t = certify_master.certify(csvp)
    assert all(g[1] != "EV/EBITDA" for g in gaps)


def test_reduce_master_covers_all_gaps(monkeypatch):
    from scripts import reduce_master
    gaps = [("Walmex", "Current", "source-gap"), ("Bimbo", "P/E", "throttled-price")]
    monkeypatch.setattr(reduce_master, "certify", lambda: ({}, gaps, (0, 0, len(gaps))))
    monkeypatch.setattr(reduce_master, "_roster_index", lambda: {
        "Walmex": ("walmex", "WALMEX", "industrial", "retail_selfservice"),
        "Bimbo": ("bimbo", "BIMBO", "industrial", "food_staples")})
    residual, counts = reduce_master.build_residual()
    # header → label, and throttled-price → the distinct "price-unresolved" cause label
    assert residual["walmex"]["Current ratio"] == "source-gap"
    assert residual["bimbo"]["P/E (LTM)"] == "price-unresolved"
    # --only-source-gaps leaves the throttled cell for the refresh
    residual2, _ = reduce_master.build_residual(only_source_gaps=True)
    assert "bimbo" not in residual2 and residual2["walmex"]["Current ratio"] == "source-gap"


def test_reduce_master_verified_causes(monkeypatch):
    from scripts import reduce_master
    gaps = [("Alpek", "NI CAGR 5y", "source-gap"),      # ≥5y history → sign-cross undefined
            ("Grupo Agro", "P/E", "source-gap"),         # AGRO ∈ ABSENT → no public filings
            ("GNP", "P/BV", "source-gap"),               # per-slug verified cause
            ("Traxion", "FCF yield", "source-gap"),      # fcf-unreliable
            ("Nutrisa", "P/E", "source-gap")]            # young → generic source-gap
    monkeypatch.setattr(reduce_master, "certify", lambda: ({}, gaps, (0, 0, len(gaps))))
    monkeypatch.setattr(reduce_master, "_roster_index", lambda: {
        "Alpek": ("alpek", "ALPEK", "industrial", "chemicals"),
        "Grupo Agro": ("grupo_agro", "AGRO", "industrial", "food_staples"),
        "GNP": ("gnp", "GNP", "financials", "insurance"),
        "Traxion": ("traxion", "TRAXION", "industrial", "transport"),
        "Nutrisa": ("nutrisa", "NUTRISA", "industrial", "food_staples")})
    monkeypatch.setattr(reduce_master, "_annual_periods",
                        lambda slug: 5 if slug == "alpek" else 4)
    residual, counts = reduce_master.build_residual()
    assert residual["alpek"]["Net income CAGR 5y"] == "cagr-undefined-sign-cross"
    assert residual["grupo_agro"]["P/E (LTM)"] == "no-public-filings"
    assert residual["gnp"]["P/BV"] == "insurer-equity-stub"
    assert residual["traxion"]["FCF yield"] == "fcf-unreliable"
    assert residual["nutrisa"]["P/E (LTM)"] == "source-gap"   # honest generic when no verified rule fits


def test_certify_zero_after_reduction(tmp_path, monkeypatch):
    from src.coverage import applicability
    csvp = _write_csv(tmp_path, [
        ("Walmex", "Retail Selfservice", "industrial", "WALMEX", {"ROE": ""}),
    ])
    _pg, before, _ = certify_master.certify(csvp)
    assert any(g[1] == "ROE" for g in before)          # ROE is a gap pre-reduction
    monkeypatch.setattr(applicability, "_RESIDUAL_NA", {"walmex": {"ROE": "source-gap"}})
    _pg, after, _ = certify_master.certify(csvp)
    assert all(g[1] != "ROE" for g in after)           # reduced → no longer a gap


def test_filled_na_source_cell_counts_as_filled(tmp_path, monkeypatch):
    # Regression for the 27-cell undercount: a cell applicability marks na_source but that
    # nonetheless carries a value must count as FILLED (mirrors build_master.cell_state's
    # "value wins" rule — a real number is never hidden), while a BLANK na_source cell stays
    # excluded (neither filled nor a gap). Proven by the delta between a filled and a blank run
    # (sector-label agnostic: certify derives the sector from the real UNIVERSE, not the CSV).
    from src.coverage import applicability
    monkeypatch.setattr(applicability, "_annual_periods", lambda slug: 2)  # <5 → 5y cols na_source

    def run(rev5y):
        csvp = _write_csv(tmp_path, [
            ("Walmex", "Retail Selfservice", "industrial", "WALMEX", {"Rev CAGR 5y": rev5y}),
        ])
        return certify_master.certify(csvp)

    _pgf, gaps_f, tf = run("7.5")   # Rev CAGR 5y is na_source but carries a value
    _pgb, gaps_b, tb = run("")      # same cell blank
    # filling the na_source cell adds exactly one applicable AND one filled; blank excludes it
    assert (tf[1] - tb[1], tf[0] - tb[0]) == (1, 1)
    # neither the filled nor the blank na_source cell is ever reported as a gap
    assert all(not (n == "Walmex" and h == "Rev CAGR 5y") for n, h, _c in gaps_f)
    assert all(not (n == "Walmex" and h == "Rev CAGR 5y") for n, h, _c in gaps_b)


def test_gap_cause_throttled_vs_source(tmp_path):
    # Row A: every price-derived cell blank → those are throttled-price (whole-row price outage).
    price_blank = {h: "" for h in ["P/E", "EV/EBITDA", "P/BV", "Div yield", "FCF yield"]}
    # Row B: prices present but a non-price fundamental blank → source-gap.
    csvp = _write_csv(tmp_path, [
        ("Walmex", "Retail Selfservice", "industrial", "WALMEX", price_blank),
        ("Chedraui", "Retail Selfservice", "industrial", "CHDRAUI", {"Current": ""}),
    ])
    _pg, gaps, _t = certify_master.certify(csvp)
    causes = {(n, h): c for n, h, c in gaps}
    assert causes[("Walmex", "P/E")] == "throttled-price"
    assert causes[("Walmex", "FCF yield")] == "throttled-price"
    assert causes[("Chedraui", "Current")] == "source-gap"
