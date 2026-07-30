"""
test_build_segments.py — the single-markdown CLI (scripts/build_segments.py).

Covers the pure pieces that don't need network or a real PDF parse: markdown
parsing, the metrics-list → outline compiler, slug/dirname derivation, IR-option
extraction from a tuned config, and the download canonicalizer.
"""

from __future__ import annotations

import importlib

import pandas as pd
import pytest

cli = importlib.import_module("scripts.build_segments")


# A tiny resolver stand-in: only "ebitda" and "revenue" resolve (exact), like the
# real MetricResolver would for unambiguous canonical keys.
class _FakeResolved:
    def __init__(self, key, conf):
        self.keys = [key]
        self.confidence = conf


class _FakeResolver:
    def resolve(self, label):
        norm = label.strip().lower()
        if norm == "ebitda":
            return _FakeResolved("ebitda", 1.0)
        if norm == "revenue":
            return _FakeResolved("revenue", 1.0)
        if norm == "total income":      # ambiguous: low-confidence fuzzy guess
            return _FakeResolved("rev_total_other", 0.70)
        return None


_VALID = {"revenue", "ebitda", "gross_profit", "net_income", "rev_total_other"}


def test_parse_input_extracts_name_link_and_metrics():
    md = (
        "# Walmex\n"
        "IR: https://www.walmex.mx/en/financial-information/quarterly.html\n\n"
        "## Metrics\n"
        "- revenue\n"
        "* gross profit\n"
        "1. operating income\n"
    )
    name, url, metrics = cli.parse_input(md)
    assert name == "Walmex"
    assert url == "https://www.walmex.mx/en/financial-information/quarterly.html"
    assert metrics == ["revenue", "gross profit", "operating income"]


def test_parse_input_ignores_url_inside_a_list_item():
    md = "# Co\n\n## Metrics\n- https://example.com/not-a-metric\n- revenue\n"
    name, url, metrics = cli.parse_input(md)
    assert url == "https://example.com/not-a-metric"   # first URL is the IR link
    assert metrics == ["revenue"]                       # the URL item is not a metric


@pytest.mark.parametrize("md,missing", [
    ("IR: https://x.com\n- revenue\n", "company name"),
    ("# Co\n- revenue\n", "IR link"),
    ("# Co\nIR: https://x.com\n", "metrics"),
])
def test_parse_input_requires_each_field(md, missing):
    with pytest.raises(cli.InputError) as exc:
        cli.parse_input(md)
    assert missing in str(exc.value)


def test_slugify_and_dirname():
    assert cli.slugify("Grupo Bimbo") == "grupo_bimbo"
    assert cli.slugify("La Comer, S.A.B.") == "la_comer_s_a_b"
    assert cli.dirname_for("Grupo Bimbo") == "Grupo_Bimbo"


def test_publish_latest_workbook_keeps_exactly_the_newest_model(tmp_path):
    source_dir = tmp_path / "company" / "excel"
    source_dir.mkdir(parents=True)
    source = source_dir / "Acme.xlsx"
    source.write_bytes(b"new workbook")

    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    (latest / "OldCo.xlsx").write_bytes(b"old workbook")
    (latest / "README.txt").write_text("stale", encoding="utf-8")
    (latest / "stale").mkdir()
    (latest / "stale" / "model.xlsx").write_bytes(b"stale workbook")

    published = cli.publish_latest_workbook(source, latest)

    assert published == latest / "Acme.xlsx"
    assert published.read_bytes() == b"new workbook"
    assert list(latest.iterdir()) == [published]
    assert source.read_bytes() == b"new workbook"


def test_publish_latest_workbook_preserves_previous_on_copy_failure(tmp_path, monkeypatch):
    source = tmp_path / "Acme.xlsx"
    source.write_bytes(b"new workbook")
    latest = tmp_path / "outputs" / "latest"
    latest.mkdir(parents=True)
    previous = latest / "Previous.xlsx"
    previous.write_bytes(b"previous workbook")

    def fail_copy(*_args, **_kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(cli.shutil, "copy2", fail_copy)
    with pytest.raises(OSError, match="copy failed"):
        cli.publish_latest_workbook(source, latest)

    assert list(latest.iterdir()) == [previous]
    assert previous.read_bytes() == b"previous workbook"


def test_canonicalize_renames_to_period_pdfs(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "Walmex_1Q26_Release.pdf").write_bytes(b"%PDF-1.4\n")
    (staging / "Walmex_4Q25_Earnings_Release.pdf").write_bytes(b"%PDF-1.4\n")
    out = tmp_path / "downloads"

    saved = cli._canonicalize(list(staging.glob("*.pdf")), out)

    assert sorted(saved) == ["2025-4T", "2026-1T"]
    assert (out / "2026-1T.pdf").exists()
    assert (out / "2025-4T.pdf").exists()


def test_ir_options_reads_tuned_config(tmp_path):
    cfg = tmp_path / "co.yaml"
    cfg.write_text(
        "company:\n  ticker: CO\n"
        "ir_website:\n"
        "  url: https://tuned.example.com/ir\n"
        "  pdf_link_pattern: 'report.*\\.pdf'\n"
        "  use_playwright: true\n"
        "  delay_ms: 250\n",
        encoding="utf-8",
    )
    opts = cli._ir_options(cfg)
    assert opts["url"] == "https://tuned.example.com/ir"
    assert opts["file_pattern"] == "report.*\\.pdf"
    assert opts["use_playwright"] is True
    assert opts["delay_ms"] == 250
    assert cli._company_ticker(cfg) == "CO"


def test_ir_options_reads_impersonate():
    import tempfile
    import pathlib
    with tempfile.TemporaryDirectory() as d:
        cfg = pathlib.Path(d) / "co.yaml"
        cfg.write_text("ir_website:\n  url: https://x\n  impersonate: chrome\n", encoding="utf-8")
        assert cli._ir_options(cfg)["impersonate"] == "chrome"


def test_ir_options_empty_without_config():
    assert cli._ir_options(None) == {}


# ── Outline compiler ─────────────────────────────────────────────────────────

def test_derived_token_normalization():
    assert cli._derived_token("YoY") == "YoY"
    assert cli._derived_token("YoY %") == "YoY"
    assert cli._derived_token("bps Δ") == "bps change"
    assert cli._derived_token("% of Total Units") == "As % of Total"
    assert cli._derived_token("As % of Consolidated") == "As % of"
    assert cli._derived_token("2-year comp") == "2-year comp"
    assert cli._derived_token("EBITDA Margin") == "EBITDA Margin"   # passes through
    # Not a derived row — keeps its own identity as a data label:
    assert cli._derived_token("SSS YoY") is None
    assert cli._derived_token("Total Income") is None


def test_compile_outline_sections_pins_and_autoresolve():
    md = (
        "# Co\nIR: https://x\n\n"
        "## Total Income\n"
        "- Total Income {revenue}\n"     # pinned
        "- YoY\n"                          # derived
        "## Profitability\n"
        "- EBITDA\n"                       # auto-resolves (exact)
        "- Margin\n"                       # derived
        "- Total Income\n"                 # ambiguous → unmapped (blank)
    )
    outline, sections, mapping, report = cli.compile_outline(md, _FakeResolver(), _VALID)

    assert sections == ["Total Income", "Profitability"]
    # Pinned + section-qualified so the heading isn't swallowed as a data row:
    assert mapping["Total Income/Total Income"] == "revenue"
    assert mapping["Profitability/EBITDA"] == "ebitda"
    # The ambiguous "Total Income" under Profitability stays unmapped (no wrong key):
    assert "Profitability/Total Income" not in mapping
    kinds = {(label, kind) for label, kind, _ in report}
    assert ("YoY", "derived") in kinds
    assert ("EBITDA", "auto") in kinds
    assert ("Total Income", "unmapped") in kinds


def test_compile_outline_bad_pin_is_blank_not_wrong():
    md = "# Co\nIR: https://x\n\n## S\n- Mystery Metric {no_such_key}\n"
    _o, _s, mapping, report = cli.compile_outline(md, _FakeResolver(), _VALID)
    assert mapping == {}                                  # nothing mapped to a bad key
    assert ("Mystery Metric", "bad-pin") in {(l, k) for l, k, _ in report}


def test_outline_workbook_fills_data_and_makes_formulas():
    # End-to-end of the pure path: compiled outline + a fake df → workbook.
    from src.excel.segments_sheet import parse_outline, build_outline_workbook

    md = ("# Co\nIR: https://x\n\n## Profitability\n"
          "- EBITDA {ebitda}\n- YoY\n- Net Income {net_income}\n")
    outline, sections, mapping, _r = cli.compile_outline(md, _FakeResolver(), _VALID)
    rows = parse_outline(outline, sections, mapping)

    df = pd.DataFrame([
        {"period": f"{y}-{q}T", "ebitda": 100 + q, "net_income": 50 + q}
        for y in (2024, 2025) for q in (1, 2, 3, 4)
    ])
    wb = build_outline_workbook("CO: Co", rows, df, units="MXN mn")
    ws = wb["Segments"]
    flat = [c.value for row in ws.iter_rows() for c in row if c.value is not None]
    # A YoY formula exists, and an extracted hard input made it into a cell.
    assert any(isinstance(v, str) and v.startswith("=IFERROR(") for v in flat)
    assert any(v in (101, 102, 103, 104, 105) for v in flat)
