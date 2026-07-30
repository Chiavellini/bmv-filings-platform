"""Regression coverage for the SPORT revenue-segment concepts added to
configs/metric_search.yaml.

These three SPORT KPIs (revenue_memberships / revenue_sports / revenue_sponsorships)
are company custom_metrics whose ``label_es`` does NOT match the actual table-row
label (e.g. label_es "Membresías y Mantenimiento" vs the row "Ingresos por cuotas de
mantenimiento y membresías"). Before the concepts existed, Tier 2 could not match
these rows. We assert through the stable Tier-2 public API (``match_metrics_from_rows``)
so the test is decoupled from the matcher's internal engine.
"""

from __future__ import annotations

from src.extract.parse_tables import match_metrics_from_rows
from src.model.financial_model import METRICS, apply_config, load_config
from src.shared.paths import CONFIGS_DIR


def _sport_defs():
    return apply_config(METRICS, load_config(CONFIGS_DIR / "sport.yaml"))


def test_sport_segment_rows_resolve_to_their_metric():
    defs = _sport_defs()
    # (row label as it appears in SPORT PDFs, current cell, prior cell) -> metric key
    rows = [
        ("Ingresos por cuotas de mantenimiento y membresías", ["385,565", "305,658"],
         "revenue_memberships"),
        ("Ingresos Deportivos y Otros Ingresos del Negocio", ["67,900", "52,800"],
         "revenue_sports"),
    ]
    candidates = [(label, cells) for label, cells, _ in rows]
    found = match_metrics_from_rows(candidates, defs)

    for _label, _cells, key in rows:
        assert key in found, f"{key} not matched from its segment row"

    assert found["revenue_memberships"].current == 385565
    assert found["revenue_sports"].current == 67900


def test_consolidated_revenue_row_is_not_stolen_by_a_segment():
    defs = _sport_defs()
    found = match_metrics_from_rows([("Total de ingresos", ["589,053", "517,700"])], defs)
    assert "revenue" in found
    assert found["revenue"].current == 589053
