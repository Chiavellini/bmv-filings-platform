"""
test_compare_extractions.py — eval phase: ground-truth CSV parsing + tier tagging.

The accuracy harness was untested; parse_actuales has fiddly logic (header
detection, E→A period normalization, section headers, first-occurrence-wins
dedup) that must be pinned, plus the source-line → tier classifier.
"""

from __future__ import annotations

import csv

import pytest

from src.eval.compare_extractions import (
    COMPANIES, parse_actuales, parse_period, _tier_of, _parse_value,
)
from src.shared.paths import PROJECT_ROOT


def _write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def test_parse_actuales_sections_normalization_and_dedup(tmp_path):
    csv_path = tmp_path / "actual.csv"
    _write_csv(csv_path, [
        ["", "", "1Q24A", "2Q24A", "1Q25E"],          # header (E forecast tag)
        ["", "Revenues", "", "", ""],                   # section header
        ["", "Total Sales", "$ 100", "110", "120"],
        ["", "Stores", "", "", ""],                     # section header
        ["", "Total Stores", "1,000", "1,010", "1,020"],
        ["", "Total Stores", "2,000", "", ""],          # dup label → first-wins
    ])
    out = parse_actuales(str(csv_path))

    # Section headers are not data rows.
    assert ("", "Revenues") not in out and ("Revenues", "Revenues") not in out
    # Values parsed, and the 1Q25E forecast column normalizes to 1Q25A.
    assert out[("Revenues", "Total Sales")] == {"1Q24A": 100.0, "2Q24A": 110.0, "1Q25A": 120.0}
    # First-occurrence wins: the duplicate 2,000 does NOT overwrite 1,000.
    assert out[("Stores", "Total Stores")]["1Q24A"] == 1000.0


def test_parse_period_quarter_and_t_formats():
    assert parse_period("Walmex_1Q26_Release") == "1Q26A"
    assert parse_period("BMV_4T25") == "4Q25A"
    assert parse_period("no_period_here") is None


def test_parse_value_handles_currency_pct_and_parens():
    assert _parse_value("$ 3,347") == 3347.0
    assert _parse_value("6.4%") == 6.4
    assert _parse_value("(5.6%)") == -5.6
    assert _parse_value("") is None
    assert _parse_value("—") is None


def test_tier_of_classifies_source_line():
    assert _tier_of("[xbrl] ifrs-full_Revenue") == "xbrl"
    assert _tier_of("[search] Total revenue 100") == "search"
    assert _tier_of("[table] Total Revenues 100") == "table"
    assert _tier_of("[regex_table] Total Revenues 100") == "regex_table"
    assert _tier_of("[prose] revenue was ...") == "prose"
    assert _tier_of("[llm] guessed") == "llm"
    assert _tier_of("[calculated]") == "calc"
    assert _tier_of("plain line") == "other"

# ---------------------------------------------------------------------------
# Registration sanity: a newly-registered company's metric_map labels must all
# exist in its ground-truth CSV (the #1 failure mode is a section/label typo,
# which silently turns every cell into a MISS).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("slug", ["soriana", "herdez", "grupo_mexico", "orbia"])
def test_registered_company_metric_map_labels_resolve(slug):
    comp = COMPANIES[slug]
    gt_path = PROJECT_ROOT / comp["actual_file"]
    if not gt_path.is_file():
        pytest.skip(
            f"{slug}: external ground-truth CSV not attached: "
            f"{comp['actual_file']}"
        )
    actuals = parse_actuales(str(gt_path))           # {(section, label): {period: val}}
    present = set(actuals.keys())
    for key, spec in comp["metric_map"].items():
        section, label, tol = spec
        assert (section, label) in present, (
            f"{slug}: metric_map['{key}'] → ({section!r}, {label!r}) "
            f"not found in {comp['actual_file']}")
        assert tol in ("currency", "pct", "count", "area"), \
            f"{slug}: metric_map['{key}'] bad tolerance type {tol!r}"


def test_build_source_falls_back_to_xbrl_dir_facts(tmp_path):
    """Tier 1 must survive a sidecar wipe: with no `<stem>_facts.json` sibling,
    _build_source finds the period's facts artifact under <dir>/xbrl/ (the layout
    that survived the 2026-07-28 cleanup; see docs/SWEEP_LOG.md sweep #1)."""
    import json as _json

    from src.eval.compare_extractions import _build_source

    md = tmp_path / "2024-1T.md"
    md.write_text("", encoding="utf-8")
    xbrl_dir = tmp_path / "xbrl"
    xbrl_dir.mkdir()
    payload = {"facts": {"ifrs-full_Revenue": [{"value": 123.0}]}}
    (xbrl_dir / "ACME_2024-1T_facts.json").write_text(_json.dumps(payload), encoding="utf-8")
    # A different quarter must not be picked up.
    (xbrl_dir / "ACME_2023-4T_facts.json").write_text(
        _json.dumps({"facts": {"ifrs-full_Revenue": [{"value": 999.0}]}}), encoding="utf-8"
    )

    src = _build_source(md, "1Q24A", {"xbrl"})
    assert src.facts == payload["facts"]

    # Sibling sidecar still wins when present.
    sibling = {"facts": {"ifrs-full_Revenue": [{"value": 456.0}]}}
    md.with_name("2024-1T_facts.json").write_text(_json.dumps(sibling), encoding="utf-8")
    src = _build_source(md, "1Q24A", {"xbrl"})
    assert src.facts == sibling["facts"]


def test_build_source_no_xbrl_dir_stays_none(tmp_path):
    from src.eval.compare_extractions import _build_source

    md = tmp_path / "2024-1T.md"
    md.write_text("", encoding="utf-8")
    src = _build_source(md, "1Q24A", {"xbrl"})
    assert src.facts is None
