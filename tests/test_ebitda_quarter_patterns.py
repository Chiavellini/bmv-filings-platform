"""Regression coverage for the SPORT EBITDA-family quarter/variant disambiguation.

All these failures shared one root cause: the extractor grabbed the annual/acumulado
or wrong-IFRS-variant figure instead of the quarter. These tests pin the fixed values
so the patterns can't silently drift back.
"""

from __future__ import annotations

from src.extract.extract_metrics import extract_metrics
from src.model.financial_model import METRICS, apply_config, load_config
from src.shared.paths import CONFIGS_DIR, REPORTS_DIR
from tests._corpus import requires_corpus_for

# Every test here reads a parsed SPORT filing from the gitignored corpus.
pytestmark = requires_corpus_for("sport")

_CFG = load_config(CONFIGS_DIR / "sport.yaml")
_DEFS = apply_config(METRICS, _CFG)


def _ebitda(stem: str, key: str = "ebitda"):
    text = (REPORTS_DIR / "sport" / stem).read_text(encoding="utf-8")
    return extract_metrics(text, _DEFS).get(key)


def test_ebitda_3q22_takes_quarter_not_acumulado():
    # "...finalizó el trimestre en $63.5 millones de pesos vs $28.8..." beats the
    # "EBITDA acumulado 2022 ... $209.7" lead, and captures the prior.
    row = _ebitda("2022-3T.md")
    assert row is not None
    assert row.current == 63_500
    assert row.prior == 28_800


def test_ebitda_2022q1_keeps_precise_table_value_and_prior():
    # 2022-1T states "...en $41.2 millones de pesos, es decir, $135.3..." (no "vs"),
    # so the prose pattern must NOT fire and the precise table value (+prior) wins.
    row = _ebitda("2022-1T.md")
    assert row is not None
    assert row.current == 41_209
    assert row.prior == -94_052


def test_ebitda_2025_takes_post_ifrs_headline_not_sin_ifrs():
    row = _ebitda("2025-1T.md")
    assert row is not None
    assert row.current == 194_800  # "con IFRS 16", not the $68.7 sin-IFRS figure


def test_ebitda_sin_ifrs_quarter_anchors():
    # quarterly figures pinned over the annual lead
    assert _ebitda("2024-4T.md", "ebitda_sin_ifrs").current == 70_400  # "último trimestre"
    assert _ebitda("2019-3T.md", "ebitda_sin_ifrs").current == 97_500  # paren "finalizó"
    assert _ebitda("2016-4T.md", "ebitda_sin_ifrs").current == 66_400  # "En el 4T16 la UAFIDA"


def test_ebitda_sin_ifrs_3q20_takes_quarter_not_acumulado():
    # 3T20 leads with the ACUMULADO bullet "(sin considerar IFRS 16) terminó en
    # -$96.2" (YTD); the quarter is "...fue -$26.8 millones de pesos en el 3T20".
    # The "en el NTYY" tail must pin the quarter (and the negative must survive).
    row = _ebitda("2020-3T.md", "ebitda_sin_ifrs")
    assert row is not None
    assert row.current == -26_800
