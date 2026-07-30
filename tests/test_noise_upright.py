"""test_noise_upright.py — Regression tests for float-noise "rotated" text.

pdfminer flags a char non-upright whenever its text matrix has b*c > 0, which
fires on skew noise like (0.94, -5e-08, -1e-08, 0.94). Layout mode then emitted
each char on its own line (WALMEX 1Q26 p.2 main-figures table; SPORT 2025-3T),
and word extraction grouped them as vertical runs. normalize_upright() re-marks
those chars upright; genuinely rotated text must be left alone.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.parse.parse_pdf import _noise_upright, normalize_upright, parse_pdf


# ---------------------------------------------------------------------------
# Unit: the noise predicate
# ---------------------------------------------------------------------------

def _char(upright: bool, matrix: tuple) -> dict:
    return {"text": "N", "upright": upright, "matrix": matrix}


def test_noise_skew_is_normalized():
    c = _char(False, (0.94, -4.7e-08, -1.3e-08, 0.94, 79.7, 656.3))
    assert _noise_upright(c)
    fixed, changed = normalize_upright([c])
    assert changed and fixed[0]["upright"] is True


def test_genuinely_rotated_text_is_untouched():
    # 90° rotation: (0, 1, -1, 0) — skew terms are full-scale, not noise
    c = _char(False, (0.0, 1.0, -1.0, 0.0, 100.0, 100.0))
    assert not _noise_upright(c)
    fixed, changed = normalize_upright([c])
    assert not changed and fixed[0]["upright"] is False


def test_upright_chars_pass_through_same_object():
    chars = [_char(True, (1, 0, 0, 1, 0, 0))]
    fixed, changed = normalize_upright(chars)
    assert not changed and fixed is chars  # healthy pages keep the exact old path


# ---------------------------------------------------------------------------
# Integration: the WALMEX 1Q26 main-figures table must parse as aligned rows
# ---------------------------------------------------------------------------

def test_walmex_1q26_main_figures_table_reconstructs():
    pdf = ROOT / "data" / "reports" / "walmex" / "Walmex_1Q26_Release.pdf"
    if not pdf.exists():
        pytest.skip(f"Report not found: {pdf}")
    md, _blocks = parse_pdf(pdf)
    # Page 2 previously rendered as one-char-per-line columns ("N e t S a le s",
    # "2 4 3 ,2 6 3"). Fixed output has label and both quarter values on ONE line.
    page2 = md.split("===== Página 2 =====")[1].split("===== Página 3 =====")[0]
    row = next((ln for ln in page2.splitlines() if "Total Revenues" in ln), None)
    assert row is not None, "Total Revenues row missing from page 2"
    assert "245,018" in row and "240,975" in row
    assert "T o ta l" not in page2  # char-spaced ghost columns are gone
