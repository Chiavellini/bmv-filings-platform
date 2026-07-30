"""
test_parse_pdf.py — parse-time structure: superscript stripping, scale detection,
section detection, and the additive with_meta contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.parse.parse_pdf import parse_pdf, detect_scale, detect_sections, DocMeta


# ---------------------------------------------------------------------------
# Caption-only scale detection (must NOT false-positive on prose)
# ---------------------------------------------------------------------------

def test_detect_scale_caption():
    assert detect_scale("Income statement (million pesos)") == (1.0, "header")
    assert detect_scale("Estado de resultados — Cifras en millones") == (1.0, "header")
    assert detect_scale("Cifras en miles de pesos") == (0.001, "header")


def test_detect_scale_ignores_prose():
    # lacomer trap: prose states the figure's unit, NOT the table's (full pesos)
    assert detect_scale("las ventas ascendieron a $6,184 millones de pesos") == (1.0, "default")


def test_detect_scale_absent():
    assert detect_scale("no units mentioned here") == (1.0, "default")


# ---------------------------------------------------------------------------
# Section detection (generic anchors)
# ---------------------------------------------------------------------------

def test_detect_sections():
    text = ("Consolidated ... Mexico main figures are : ... "
            "Central America main figures are : ... Appendix 1: Quarterly Income")
    names = [n for n, _ in detect_sections(text)]
    assert names == ["mexico", "cam", "appendix"]


def test_detect_sections_none():
    assert detect_sections("a report with no segment headers") == []


# ---------------------------------------------------------------------------
# Contract: with_meta is additive
# ---------------------------------------------------------------------------

def test_parse_pdf_backward_compat():
    pytest.importorskip("pdfplumber")
    pdf = ROOT / "data" / "reports" / "sport" / "2017-1T.pdf"
    if not pdf.exists():
        pytest.skip("sample PDF not on disk")
    out = parse_pdf(str(pdf))
    assert isinstance(out, tuple) and len(out) == 2     # (markdown, blocks)


def test_parse_pdf_with_meta_returns_docmeta():
    pytest.importorskip("pdfplumber")
    pdf = ROOT / "data" / "reports" / "sport" / "2017-1T.pdf"
    if not pdf.exists():
        pytest.skip("sample PDF not on disk")
    md, blocks, doc = parse_pdf(str(pdf), with_meta=True)
    assert isinstance(doc, DocMeta)
    assert isinstance(md, str) and md


# ---------------------------------------------------------------------------
# Stage 1: superscript footnote stripping fixes the 49¹→491 artifact
# ---------------------------------------------------------------------------

def test_superscript_stripped_on_fresh_parse():
    pytest.importorskip("pdfplumber")
    pdf = ROOT / "data" / "reports" / "sport" / "2017-1T.pdf"
    if not pdf.exists():
        pytest.skip("sample PDF not on disk")
    md, _, _ = parse_pdf(str(pdf), with_meta=True)
    # ground truth is 49 clubs; the old flat parse produced "491" (49 + footnote ¹)
    assert "491" not in md
    assert "49 clubes" in md or "49  clubes" in md


def test_fresh_parse_preserves_numbers():
    """Superscript stripping must not corrupt numeric extraction."""
    pytest.importorskip("pdfplumber")
    pdf = ROOT / "data" / "reports" / "walmex" / "Walmex_2Q25_Release.pdf"
    if not pdf.exists():
        pytest.skip("sample PDF not on disk")
    import yaml
    from src.model.financial_model import METRICS, apply_config
    from src.extract.extract_metrics import extract_metrics_segmented
    cfg = yaml.safe_load(open(ROOT / "configs" / "walmex.yaml"))
    defs = apply_config(METRICS, cfg)
    md, _, _ = parse_pdf(str(pdf), with_meta=True)
    m = extract_metrics_segmented(md, defs, cfg)
    assert m["revenue"].current == 246_254
    assert m["total_stores"].current == 4_124
