"""Tests for the post-parse Spanish safety net (require_language gate) in ingest."""
from __future__ import annotations

from src.corpus.ingest import _looks_spanish

_ENGLISH = """
Grupo Bimbo reports first quarter 2024 results. Net sales rose 13% driven by solid organic
growth in Mexico and North America, with an expansion in the gross margin reflecting lower raw
material costs. Operating income increased and the company reiterated its full-year guidance for
profitability and free cash flow generation across all of its regions.
"""

_SPANISH = """
Grupo Bimbo reporta sus resultados del primer trimestre de 2024. Las ventas netas crecieron 13%
impulsadas por un sólido crecimiento orgánico en México, con una expansión del margen bruto que
refleja menores costos de materias primas. La utilidad de operación aumentó durante el trimestre
y la compañía reiteró su guía para el año en rentabilidad y generación de flujo de efectivo.
"""


def test_looks_spanish_true_for_spanish():
    assert _looks_spanish(_SPANISH) is True


def test_looks_spanish_false_for_english():
    assert _looks_spanish(_ENGLISH) is False


def test_looks_spanish_false_for_short_text():
    # Too little text to judge → not flagged (avoids dropping headers/boilerplate).
    assert _looks_spanish("Q1 2024 Results") is False
