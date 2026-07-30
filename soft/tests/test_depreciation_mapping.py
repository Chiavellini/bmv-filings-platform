"""D&A concept resolution — `depreciation` prefers the first NON-ZERO concept, so a present-but-0.0
line (some issuers file ifrs-full Adjustments as an empty line while the real value is in mx_ccd_)
no longer shadows the real value. Offline, no network."""
from src.extract.xbrl_facts import extract_from_xbrl
from src.model.financial_model import MetricDef


def _dur(value):
    return {"value": value, "instant": None, "period_start": "2025-01-01",
            "period_end": "2025-03-31", "dimensions": None, "unit": "ISO4217:MXN", "decimals": "-3"}


def _md(key, concepts):
    return MetricDef(key=key, label=key, label_es=key, section="income", unit="currency",
                     patterns=[], xbrl_concepts=concepts)


def test_depreciation_skips_zero_concept_for_real_value():
    facts = {"ZeroLine": [_dur(0.0)], "RealDA": [_dur(413.0e6)]}
    # depreciation is in _PREFER_NONZERO → skips the 0.0 ZeroLine, picks the non-zero RealDA.
    out = extract_from_xbrl(facts, [_md("depreciation", ["ZeroLine", "RealDA"])], period_end="2025-03-31")
    assert out["depreciation"].current == 413000.0         # the non-zero RealDA (scaled), not 0.0
    assert "RealDA" in out["depreciation"].source_line


def test_nonprefer_metric_keeps_first_present_even_if_zero():
    facts = {"ZeroLine": [_dur(0.0)], "RealDA": [_dur(413.0e6)]}
    # a metric NOT in _PREFER_NONZERO keeps first-present-wins (0.0 stays).
    out = extract_from_xbrl(facts, [_md("some_metric", ["ZeroLine", "RealDA"])], period_end="2025-03-31")
    assert out["some_metric"].current == 0.0


def test_depreciation_uses_zero_only_as_last_resort():
    facts = {"ZeroLine": [_dur(0.0)]}          # only a zero-valued concept exists
    out = extract_from_xbrl(facts, [_md("depreciation", ["ZeroLine"])], period_end="2025-03-31")
    assert out["depreciation"].current == 0.0  # falls back to the zero rather than dropping the metric
