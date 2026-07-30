"""
test_tiered_extract.py — the 4-tier cascade orchestrator.

Verifies (a) a text-only PeriodSource reproduces the current regex engine exactly
(no behavior change for PDF-only periods), and (b) tier precedence: a structured
XBRL fact wins over a regex value for the same metric.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.extract.extract_metrics import extract_metrics
from src.extract.tiered_extract import PeriodSource, extract_metrics_tiered, period_end_from_label


def test_period_end_from_label():
    assert period_end_from_label("2026-1T") == "2026-03-31"
    assert period_end_from_label("2025-2T") == "2025-06-30"
    assert period_end_from_label("2024-3T") == "2024-09-30"
    assert period_end_from_label("2023-4T") == "2023-12-31"
    assert period_end_from_label("garbage") is None


def test_text_only_reproduces_current_engine(sport_2024_text, sport_metric_defs):
    """PeriodSource(text only) must equal extract_metrics(text, defs) — no drift."""
    baseline = extract_metrics(sport_2024_text, sport_metric_defs)
    src = PeriodSource(period="2024-1T", text=sport_2024_text)
    tiered = extract_metrics_tiered(src, sport_metric_defs, tiers={"prose"})

    assert set(tiered) == set(baseline)
    for key in baseline:
        assert tiered[key].current == baseline[key].current, key
        assert tiered[key].source_line == baseline[key].source_line, key


def test_xbrl_tier_wins_over_regex(sport_2024_text, sport_metric_defs):
    """Facts present → revenue answered by Tier 1 even though regex would find it."""
    # regex on the 2024 report finds revenue 517,708
    assert extract_metrics(sport_2024_text, sport_metric_defs)["revenue"].current == 517_708

    facts = {
        "ifrs-full_Revenue": [
            {"value": 589_053_000.0, "period_start": "2026-01-01",
             "period_end": "2026-03-31", "instant": None, "unit": "ISO4217:MXN",
             "decimals": "-3", "dimensions": None},
        ]
    }
    src = PeriodSource(period="2026-1T", text=sport_2024_text, facts=facts,
                       period_end="2026-03-31")
    out = extract_metrics_tiered(src, sport_metric_defs)
    assert out["revenue"].current == 589_053.0            # Tier 1 value, not regex's
    assert "[xbrl]" in out["revenue"].source_line


def test_table_tier_from_pdf(sport_metric_defs):
    """Tier 2 path works through the orchestrator with only a pdf_path."""
    pytest.importorskip("pdfplumber")
    pdf = ROOT / "data" / "reports" / "sport" / "2024-1T.pdf"
    if not pdf.exists():
        pytest.skip("sample report PDF not on disk")
    src = PeriodSource(period="2024-1T", text="", pdf_path=pdf)
    out = extract_metrics_tiered(src, sport_metric_defs)
    assert "revenue" in out
    assert "[table]" in out["revenue"].source_line
    assert abs(out["revenue"].current - 517_708) / 517_708 < 0.01


def test_search_tier_finds_command_f_alias_line():
    from src.model.financial_model import MetricDef

    defs = [
        MetricDef(
            key="revenue_merchandise",
            label="Revenue From Sales of Merchandise",
            label_es="Revenue From Sales of Merchandise",
            section="income",
            unit="currency",
            patterns=[],
            aliases=["Revenue From Sales of Merchandise"],
        )
    ]
    text = "Revenue From Sales of Merchandise Ps.12,293,230 Ps.9,391,177 30.9%"
    src = PeriodSource(period="4Q23A", text=text)
    out = extract_metrics_tiered(src, defs, tiers={"search"})
    assert out["revenue_merchandise"].current == 12_293_230
    assert out["revenue_merchandise"].prior == 9_391_177
    assert out["revenue_merchandise"].source_line.startswith("[search]")


def test_search_tier_skips_accumulated_columns_when_quarter_header_present():
    from src.model.financial_model import MetricDef

    defs = [
        MetricDef(
            key="revenue_commercial",
            label="Commercial Income",
            label_es="Venta de bienes",
            section="income",
            unit="currency",
            patterns=[],
            aliases=["Venta de bienes"],
        )
    ]
    text = "\n".join([
        "Acum Actual  Acum Anterior  Trimestre Año Actual  Trimestre Año Anterior",
        "Venta de bienes 74,515,364,000 66,467,715,000 42,028,145,000 38,177,324,000",
    ])
    src = PeriodSource(period="4Q25A", text=text)
    out = extract_metrics_tiered(src, defs, {"company": {"unit": "millions"}}, tiers={"search"})
    assert out["revenue_commercial"].current == 42_028.145
    assert out["revenue_commercial"].prior == 38_177.324


def test_empty_source_yields_nothing(sport_metric_defs):
    src = PeriodSource(period="2024-1T")
    assert extract_metrics_tiered(src, sport_metric_defs) == {}


def test_regex_wins_over_table(monkeypatch, sport_2024_text, sport_metric_defs):
    """Tier 2 is gap-fill: a table value must NOT override a regex value."""
    from src.extract import parse_tables
    from src.extract.extract_metrics import MetricRow

    # regex finds revenue 517,708 from the real 2024 text
    assert extract_metrics(sport_2024_text, sport_metric_defs)["revenue"].current == 517_708

    def fake_tables(pdf_path, metric_defs, **kw):
        return {"revenue": MetricRow("revenue", "x", 999_999, None, None,
                                     "currency", "[table] fake")}

    monkeypatch.setattr(parse_tables, "extract_from_tables", fake_tables)
    src = PeriodSource(period="2024-1T", text=sport_2024_text,
                       pdf_path=Path("/tmp/does_not_matter.pdf"))
    out = extract_metrics_tiered(src, sport_metric_defs, tiers={"prose", "table"})
    assert out["revenue"].current == 517_708            # regex value kept
    assert "fake" not in out["revenue"].source_line     # table value rejected


def test_table_fills_gap_regex_missed(monkeypatch, sport_metric_defs):
    """Tier 2 still fills a metric regex left missing."""
    from src.extract import parse_tables
    from src.extract.extract_metrics import MetricRow

    def fake_tables(pdf_path, metric_defs, **kw):
        return {"cogs": MetricRow("cogs", "Costo", 12_345, None, None,
                                  "currency", "[table] cell")}

    monkeypatch.setattr(parse_tables, "extract_from_tables", fake_tables)
    # text has no cogs line → regex misses it → table fills it
    src = PeriodSource(period="2024-1T", text="Nothing useful here.",
                       pdf_path=Path("/tmp/x.pdf"))
    out = extract_metrics_tiered(src, sport_metric_defs)
    assert out["cogs"].current == 12_345
    assert "[table]" in out["cogs"].source_line
