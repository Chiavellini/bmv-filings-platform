"""The dense matrix — a genuine 100% block: reduce companies + metrics until every cell is a real
value (no N/A, no blanks). Offline; drives build() over a synthetic master CSV."""
from __future__ import annotations

import csv

from scripts import build_dense


def _master(tmp_path, rows):
    p = tmp_path / "soft_coverage_master.csv"
    hdr = ["Company", "Sector", "Template", "Ticker", "P/E", "P/BV", "ROE", "Div yield"]
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(hdr)
        w.writerows(rows)
    return p


def test_drops_company_with_a_hole(tmp_path, monkeypatch):
    monkeypatch.setattr(build_dense, "MASTER_CSV", _master(tmp_path, [
        ["Walmex", "Retail", "industrial", "WALMEX", "17", "3.5", "20", "3.4"],   # full → kept
        ["Bimbo", "Food", "industrial", "BIMBO", "18", "2.0", "11", ""],          # blank Div → dropped
        ["GNP", "Insurance", "financials", "GNP", "N/A", "N/A", "N/A", "N/A"],     # all N/A → dropped
    ]))
    rows, metrics = build_dense.build(["P/E", "P/BV", "ROE", "Div yield"])
    assert [r["name"] for r in rows] == ["Walmex"]           # only the fully-real company survives
    assert metrics == ["P/E", "P/BV", "ROE", "Div yield"]


def test_kept_block_is_100pct_real(tmp_path, monkeypatch):
    monkeypatch.setattr(build_dense, "MASTER_CSV", _master(tmp_path, [
        ["A", "S", "industrial", "A", "10", "1.0", "9", "2"],
        ["B", "S", "industrial", "B", "12", "1.2", "8", "1"],
        ["C", "S", "industrial", "C", "", "1.2", "8", "1"],     # dropped
    ]))
    rows, metrics = build_dense.build(["P/E", "P/BV", "ROE", "Div yield"])
    holes = [(r["name"], m) for r in rows for m in metrics
             if not isinstance(r["vals"][m], (int, float))]
    assert not holes and len(rows) == 2                       # every kept cell is a real number


def test_default_metric_block_is_the_thirteen():
    # The shipped default drops exactly CET1, FCF yield, NI CAGR 5y (the sparse/undefined columns).
    assert len(build_dense.DENSE_METRICS) == 13
    assert "CET1" not in build_dense.DENSE_METRICS
    assert "FCF yield" not in build_dense.DENSE_METRICS
    assert "NI CAGR 5y" not in build_dense.DENSE_METRICS
    assert "P/E" in build_dense.DENSE_METRICS and "EV/EBITDA" in build_dense.DENSE_METRICS
