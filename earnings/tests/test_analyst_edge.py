"""Analyst edge decomposition mechanics (eval_analyst_edge)."""
from __future__ import annotations

import sys
from pathlib import Path

EARNINGS_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "protocol"
sys.path.insert(0, str(EARNINGS_ROOT))
sys.path.insert(0, str(EARNINGS_ROOT / "scripts"))

import numpy as np
import pandas as pd
import pytest

from eval_analyst_edge import (A3_FEATURES, FEATURES, MIN_CELL, OUTCOME_COLS,
                               abstain_cells, bh_fdr, wilson)


def test_leakage_guards():
    assert set(FEATURES).isdisjoint(OUTCOME_COLS)
    assert set(A3_FEATURES) == {"s_ts", "margin_sue_z", "sigma_pre"}
    # kpi non-PIT columns must not be in the CV feature list
    for banned in ("ebitda_margin_yoy_pp", "net_margin_yoy_pp",
                   "rev_accel_pp", "nd_to_ebitda_ttm"):
        assert banned not in FEATURES


def test_bh_fdr_monotone_and_nan_aware():
    p = np.array([0.001, 0.01, 0.04, 0.2, np.nan])
    q = bh_fdr(p)
    assert np.isnan(q[4])
    assert q[0] <= q[1] <= q[2] <= q[3]
    assert q[0] == pytest.approx(0.004)          # 0.001 * 4 / 1


def test_abstain_cells_floor_and_counts():
    rng = np.random.default_rng(0)
    n = 60
    df = pd.DataFrame({
        "period": ["2025-1T"] * n,
        "s_ts": rng.normal(0, 2, n),
        "margin_sue_z": rng.normal(0, 1, n),
        "sigma_pre": rng.uniform(0.01, 0.05, n),
        "ar0_cc": rng.normal(0, 0.02, n),
    })
    g = abstain_cells(df, float(df["sigma_pre"].median()))
    assert len(g) == 12                              # frozen grid size
    base = g[(g.thr == 1.0) & (g.gate == "any") & (g.vol == "all")].iloc[0]
    assert base["n"] == int((df["s_ts"].abs() >= 1.0).sum())
    # gating only shrinks n (monotone coverage)
    for thr in (1.0, 1.5, 2.0):
        sub = g[g.thr == thr]
        n_any = sub[(sub.gate == "any") & (sub.vol == "all")]["n"].iloc[0]
        assert (sub["n"] <= n_any).all()
    assert MIN_CELL == 15


def test_wilson_bounds():
    lo, hi = wilson(25, 27)
    assert 0.75 < lo < 0.926 < hi <= 1.0
    assert wilson(0, 0) == (pytest.approx(np.nan, nan_ok=True),
                            pytest.approx(np.nan, nan_ok=True))


def test_case_reads_schema_when_present():
    p = EARNINGS_ROOT / "outputs/results_v3/analyst_edge_case_reads.csv"
    if not p.exists():
        pytest.skip("case reads not curated yet")
    df = pd.read_csv(p)
    assert {"slug", "period", "call", "blind_call_agrees",
            "plausible_reason", "in_structured_features",
            "hypothesis_code"} <= set(df.columns)
    assert set(df["hypothesis_code"]) <= {"expectations_prior",
                                          "qualitative_guidance",
                                          "margin_quality", "other"}


def test_protocol_fixture_schema_and_labels():
    """The protocol contract is tested with synthetic, publication-safe fixtures."""
    log = FIXTURE_ROOT / "log_3T26.csv"
    header = log.read_text().splitlines()[0].split(",")
    assert header == ["fecha_hora_registro", "ticker", "trimestre", "llamada",
                      "conviccion", "razon_principal", "razon_secundaria",
                      "notas"]
    proto = (FIXTURE_ROOT / "protocolo_llamadas_3T26.md").read_text()
    for label in ("Positive", "Negative", "Neutral", "N to P", "N to N",
                  "vs_expectativas", "guia_outlook", "margenes_calidad",
                  "ANTES"):
        assert label in proto
