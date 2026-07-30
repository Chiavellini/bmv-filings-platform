from __future__ import annotations

from scripts.measure_semantic_search import (
    blocks_to_rows,
    legacy_match_rows,
    semantic_match_rows,
)
from src.extract.parse_tables import TableBlock, match_metrics_from_blocks
from src.model.financial_model import METRICS, attach_concept_map


def _defs(*keys):
    defs = attach_concept_map(METRICS)
    return [m for m in defs if m.key in keys]


def test_measurement_harness_exposes_dictionary_only_alias_lift():
    rows = [("Resultado operativo", ["107,900", "95,000"])]
    defs = _defs("operating_income")

    legacy = legacy_match_rows(rows, defs)
    semantic = semantic_match_rows(rows, defs)

    assert "operating_income" not in legacy
    assert semantic["operating_income"].current == 107_900
    assert semantic["operating_income"].prior == 95_000


def test_blocks_to_rows_flattens_in_column_order():
    blocks = [TableBlock(
        header_by_col={0: "1T16", 1: "1T15"},
        rows=[("Total de ingresos", [(0, "589,053"), (1, "517,708")])],
    )]
    assert blocks_to_rows(blocks) == [("Total de ingresos", ["589,053", "517,708"])]


def test_harness_semantic_path_selects_period_column():
    # The legacy control (flat rows) is positional; the period-aware path picks
    # the target quarter even when it is not the first numeric cell.
    block = TableBlock(
        header_by_col={0: "1T15", 1: "1T16"},
        rows=[("Total de ingresos", [(0, "517,708"), (1, "589,053")])],
    )
    defs = _defs("revenue")
    legacy = legacy_match_rows(blocks_to_rows([block]), defs)
    semantic = match_metrics_from_blocks([block], defs, period="1Q16A")
    assert legacy["revenue"].current == 517_708        # positional → first cell
    assert semantic["revenue"].current == 589_053      # period → 1T16 column
