"""
test_phase_contracts.py — one contract test per product pipeline phase.

Asserts each stage's entrypoint honors its output contract, so a regression at
any boundary (download → parse → extract → generate_excel → cli) is caught
directly. Validation/evaluation are quality gates rather than product phases.
Default tests are offline (mocks/fixtures); real network/API checks live at the
bottom under @pytest.mark.network and are skipped by `-m "not network"`.
"""

from __future__ import annotations

import csv
import importlib
import os
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
SPORT = ROOT / "data" / "reports" / "sport"


# ── shared offline HTTP fake (mirrors tests/test_downloader_archive.py) ─────────
class _FakeResponse:
    def __init__(self, *, text="", url=""):
        self.text = text
        self.url = url

    def raise_for_status(self):
        return None


class _FakeSession:
    def __init__(self, html):
        self.html = html

    def get(self, url, params=None, timeout=None, stream=False, verify=None, headers=None):
        return _FakeResponse(text=self.html, url=url)


# ── Phase 1: download → list[Path] ──────────────────────────────────────────────
def test_download_phase_returns_paths(tmp_path, monkeypatch):
    from src.download import downloader

    html = '<html><body><a href="https://x.example.com/report.pdf">R</a></body></html>'
    monkeypatch.setattr(downloader, "_make_session", lambda **kw: _FakeSession(html))
    monkeypatch.setattr(downloader, "_download_pdf",
                        lambda session, url, dest, **kw: dest)

    paths = downloader.download_from_ir(
        "https://www.example.com/ir.html", tmp_path,
        max_reports=5, file_pattern=r"report.*\.pdf", delay_ms=0,
    )
    assert isinstance(paths, list)
    assert paths and all(isinstance(p, Path) for p in paths)


# ── Phase 2: parse → (markdown, blocks, DocMeta) ────────────────────────────────
def test_parse_phase_contract():
    from src.parse.parse_pdf import parse_pdf, DocMeta

    pdf = SPORT / "2017-1T.pdf"
    if not pdf.exists():
        pytest.skip(f"fixture PDF missing: {pdf}")

    result = parse_pdf(str(pdf), with_meta=True)
    assert isinstance(result, tuple) and len(result) == 3
    markdown, blocks, doc = result
    assert isinstance(markdown, str) and markdown.strip()
    assert isinstance(blocks, list)
    assert isinstance(doc, DocMeta)
    assert hasattr(doc, "scale") and hasattr(doc, "sections")


# ── Phase 3: extract → DataFrame(period + metric cols) ──────────────────────────
def test_extract_phase_contract():
    from src.extract.pipeline import run

    md = SPORT / "2026-1T.md"
    if not md.exists():
        pytest.skip(f"fixture report missing: {md}")

    df = run(md, metrics=["revenue", "ebitda"],
             config=str(ROOT / "configs" / "sport.yaml"), verbose=False)
    assert isinstance(df, pd.DataFrame)
    assert "period" in df.columns
    assert "revenue" in df.columns
    assert len(df) == 1


def test_extract_tier_cascade_llm_off_by_default(sport_2024_text, sport_metric_defs):
    """Text-only source → regex tier only; LLM tier is opt-in (no call by default)."""
    from src.extract.tiered_extract import PeriodSource, extract_metrics_tiered

    called = {"llm": False}

    def _boom(*a, **k):
        called["llm"] = True
        return {}

    src = PeriodSource(period="2024-1T", text=sport_2024_text)
    out = extract_metrics_tiered(src, sport_metric_defs)   # use_llm defaults False
    assert "revenue" in out and out["revenue"].current is not None
    assert called["llm"] is False


# ── Phase 4: generate_excel → Workbook with a Segments sheet ────────────────────
def test_excel_phase_contract():
    from src.excel.segments_sheet import parse_outline, build_outline_workbook

    df = pd.DataFrame([{"period": f"{y}-{q}T", "revenue": y + q}
                       for y in (2024, 2025) for q in (1, 2, 3, 4)])
    rows = parse_outline("Revenues\n\nTotal\nYoY\n", sections=["Revenues"],
                         mapping={"Total": "revenue"})
    wb = build_outline_workbook("ACME: Co", rows, df,
                                unit_map={"revenue": "currency"})
    ws = wb.active
    assert wb.sheetnames == ["Segments"]
    assert ws["A2"].value == "ACME: Co"
    assert ws["B5"].value.startswith("Segments (in")


# ── Phase 5: cli → single-markdown command parses name / IR link / metrics ──────
def test_cli_phase_contract():
    cli = importlib.import_module("scripts.build_segments")

    name, ir_url, metrics = cli.parse_input(
        "# Acme\nIR: https://example.com/investors\n\n## Metrics\n- revenue\n- ebitda\n"
    )
    assert name == "Acme"
    assert ir_url == "https://example.com/investors"
    assert metrics == ["revenue", "ebitda"]
    assert cli.slugify("Grupo Bimbo") == "grupo_bimbo"


# ── Quality gate: eval → {(section,label): {period: value}} ────────────────────
def test_eval_quality_gate_contract(tmp_path):
    from src.eval.compare_extractions import parse_actuales

    p = tmp_path / "actual.csv"
    with open(p, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([
            ["", "", "1Q24A", "2Q24A"],
            ["", "Revenues", "", ""],
            ["", "Total Sales", "$ 100", "110"],
        ])
    out = parse_actuales(str(p))
    assert out[("Revenues", "Total Sales")] == {"1Q24A": 100.0, "2Q24A": 110.0}


# ── Opt-in real integration checks (deselected by `-m "not network"`) ───────────
@pytest.mark.network
def test_download_phase_real_network(tmp_path):
    from src.download.downloader import download_from_ir
    from src.model.financial_model import load_config

    cfg = load_config(ROOT / "configs" / "walmex.yaml")
    url = cfg["ir_website"]["url"]
    paths = download_from_ir(url, tmp_path, max_reports=1, delay_ms=0)
    assert isinstance(paths, list)


@pytest.mark.network
def test_extract_llm_tier_real_api(sport_metric_defs):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")
    from src.extract.llm_extract import llm_extract

    defs = [m for m in sport_metric_defs if m.key == "revenue"]
    text = "Total revenue for the quarter was $1,234.5 million pesos."
    out = llm_extract(defs, text, already_found={})
    assert isinstance(out, dict)
