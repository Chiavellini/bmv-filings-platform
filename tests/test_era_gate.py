"""Era-gate: metrics not reported before a given year MISS rather than guess.

SPORT adopted IFRS 16 in 2019; pre-2019 reports carry only pre-IFRS UAFIDA, which
belongs to `ebitda_sin_ifrs`. Post-IFRS `ebitda` therefore does not exist pre-2019 —
the cascade must drop it (config: `era_gates.ebitda.available_from_year: 2019`).
"""

from __future__ import annotations

from src.extract.tiered_extract import (
    PeriodSource,
    _period_year,
    extract_metrics_tiered,
)
from src.model.financial_model import METRICS, apply_config, load_config
from src.shared.paths import CONFIGS_DIR, REPORTS_DIR
from tests._corpus import requires_corpus_for

#: _period_year is a pure helper and stays runnable; the gate tests themselves
#: read SPORT filings from the gitignored corpus.
_requires_sport = requires_corpus_for("sport")


def _sport():
    cfg = load_config(CONFIGS_DIR / "sport.yaml")
    return apply_config(METRICS, cfg), cfg


def test_period_year_handles_every_label_shape():
    # eval labels (quarter-first, 2-digit year), pipeline labels, ISO ends
    assert _period_year("1Q16A") == 2016
    assert _period_year("4Q18A") == 2018
    assert _period_year("1Q19A") == 2019
    assert _period_year("2026-1T") == 2026
    assert _period_year("2016-03-31") == 2016
    assert _period_year(None) is None


@_requires_sport
def test_era_gate_drops_pre_2019_ebitda_but_keeps_sin_ifrs():
    defs, cfg = _sport()
    text = (REPORTS_DIR / "sport" / "2016-1T.md").read_text(encoding="utf-8")

    gated = extract_metrics_tiered(PeriodSource(period="1Q16A", text=text), defs, cfg)
    assert "ebitda" not in gated, "post-IFRS ebitda must MISS pre-2019"
    # the pre-IFRS figure is still captured under ebitda_sin_ifrs
    assert gated.get("ebitda_sin_ifrs") is not None


@_requires_sport
def test_era_gate_keeps_ebitda_in_2019_plus():
    defs, cfg = _sport()
    text = (REPORTS_DIR / "sport" / "2019-1T.md").read_text(encoding="utf-8")
    out = extract_metrics_tiered(PeriodSource(period="1Q19A", text=text), defs, cfg)
    assert out.get("ebitda") is not None, "ebitda must survive the gate from 2019 on"


@_requires_sport
def test_era_gate_is_noop_without_config():
    """A config without `era_gates` must not drop anything."""
    defs, cfg = _sport()
    text = (REPORTS_DIR / "sport" / "2019-1T.md").read_text(encoding="utf-8")
    cfg_no_gate = {k: v for k, v in cfg.items() if k != "era_gates"}
    out = extract_metrics_tiered(PeriodSource(period="1Q16A", text=text), defs, cfg_no_gate)
    # 2019 text under a 2016 label, but with no gate ebitda is retained
    assert "ebitda" in out
