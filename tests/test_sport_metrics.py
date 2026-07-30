"""
test_sport_metrics.py — model phase: SPORT extended metric registry.

SPORT is the primary use case; this locks down the registry's integrity
(unique keys, extended-wins dedup, compilable patterns).
"""

from __future__ import annotations

import re

from src.model.financial_model import METRICS
from src.model.sport_metrics import SPORT_EXTENDED_METRICS, get_sport_metrics


def test_get_sport_metrics_unique_keys_extended_wins():
    combined = get_sport_metrics()
    keys = [m.key for m in combined]
    assert len(keys) == len(set(keys)), "duplicate metric keys in get_sport_metrics()"

    # Every extended metric is present and takes precedence over a base same-key.
    base_keys = {m.key for m in METRICS}
    by_key = {m.key for m in combined}
    for m in SPORT_EXTENDED_METRICS:
        assert m.key in by_key
    # An overlapping key resolves to the extended definition.
    overlap = {m.key for m in SPORT_EXTENDED_METRICS} & base_keys
    ext_by_key = {m.key: m for m in SPORT_EXTENDED_METRICS}
    combined_by_key = {m.key: m for m in combined}
    for k in overlap:
        assert combined_by_key[k] is ext_by_key[k]


def test_sport_extended_keys_are_unique():
    keys = [m.key for m in SPORT_EXTENDED_METRICS]
    assert len(keys) == len(set(keys))


def test_sport_extended_patterns_compile():
    for m in SPORT_EXTENDED_METRICS:
        for spec in m.patterns:
            # Each PatternSpec.regex must be a valid regex.
            re.compile(spec.regex)


def test_sport_registry_covers_expected_kpis():
    keys = {m.key for m in SPORT_EXTENDED_METRICS}
    # A few representative SPORT KPIs that the model relies on.
    for expected in ("clubs_sw_total", "gross_churn"):
        assert expected in keys, expected
