"""Canonical doc-type taxonomy — inference, labels, legacy mapping."""
from __future__ import annotations

from pathlib import Path

import yaml

from src.corpus.doc_types import load_taxonomy

_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "alpha_go.yaml"


def _tax():
    return load_taxonomy({
        "default_doc_type": "quarterly_release",
        "doc_types": [
            {"key": "press_release", "label": "Press release",
             "keywords": ["comunicado", "prensa", "press release", "press_release"]},
            {"key": "quarterly_release", "label": "Quarterly release",
             "keywords": ["release", "1t", "2t"]},
            {"key": "annual_report", "label": "Annual report",
             "keywords": ["annual", "10-k"]},
            {"key": "transcript", "label": "Earnings-call transcript",
             "keywords": ["transcript", "call"]},
            {"key": "presentation", "label": "Investor presentation",
             "keywords": ["presentation", "deck"]},
            {"key": "internal", "label": "Internal / Other", "keywords": []},
        ],
    })


def test_infer_by_keyword():
    t = _tax()
    assert t.infer("bimbo_annual_report_2024") == "annual_report"
    assert t.infer("q1_earnings_call_transcript") == "transcript"
    assert t.infer("investor_presentation_deck") == "presentation"
    assert t.infer("walmex_4Q24_release") == "quarterly_release"


def test_press_release_wins_over_generic_release():
    # A press release is distinguished from a quarterly earnings release: its specific keyword
    # (ordered before quarterly_release) takes the file, while a bare "release" stays quarterly.
    t = _tax()
    assert t.infer("bimbo_comunicado_de_prensa_2024") == "press_release"
    assert t.infer("walmex_press_release_1t25") == "press_release"
    assert t.infer("walmex_4Q24_release") == "quarterly_release"


def test_real_config_has_press_release():
    # Validates the shipped configs/alpha_go.yaml taxonomy, not just a local fixture.
    t = load_taxonomy(yaml.safe_load(_CONFIG.read_text()))
    assert "press_release" in t.keys()
    assert t.infer("herdez_comunicado_prensa.pdf") == "press_release"
    assert t.infer("herdez_4t24_release.pdf") == "quarterly_release"


def test_infer_default_when_no_keyword():
    # A period-named file (no type keyword) falls back to the configured default.
    assert _tax().infer("2026-1T") == "quarterly_release"
    assert _tax().infer("strategy memo") == "quarterly_release"


def test_legacy_keys_map_to_canonical_label():
    t = _tax()
    assert t.label_for("release") == "Quarterly release"
    assert t.label_for("report") == "Quarterly release"
    assert t.canonical("release") == "quarterly_release"


def test_label_key_roundtrip_and_unknown():
    t = _tax()
    assert t.key_for_label("Annual report") == "annual_report"
    assert t.label_for("annual_report") == "Annual report"
    assert t.label_for("mystery_type") == "mystery_type"   # unknown → verbatim


def test_builtin_taxonomy_loads_without_config():
    t = load_taxonomy(None)
    assert "quarterly_release" in t.keys()
    assert t.default_key == "quarterly_release"
