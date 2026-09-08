"""Grid orchestration (src/qa/grid.py): the cost ceilings, the per-cell scoping, the honest
status of a failed vs a genuinely offline cell, and the exports."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.qa import ask, grid
from src.search.snippets import Snippet


def _hit(chunk_id="c1", doc_id="acme/2024-1T", text="Net sales grew 8% and margins expanded."):
    return SimpleNamespace(chunk_id=chunk_id, doc_id=doc_id, company="acme", period="2024-1T",
                           doc_type="quarterly_release", title="Q1 results", score=1.0,
                           snippet=Snippet(text=text, spans=[(0, 9)]),
                           markdown_path="/tmp/acme.md", char_start=0, char_end=60)


class _Retriever:
    """Records the filters every cell searched under."""

    def __init__(self, hits=None):
        self._hits = hits if hits is not None else [_hit()]
        self.calls = []

    def search(self, query, *, filters=None, limit=20, **kw):
        self.calls.append((query, filters))
        return list(self._hits)


def _row(doc_id="acme/2024-1T"):
    return grid.GridRow(doc_id=doc_id, company="acme", period="2024-1T",
                        doc_type="quarterly_release", title="Q1 results")


def _spec(n_rows=2, n_questions=2):
    rows = [_row(f"acme/2024-{i + 1}T") for i in range(n_rows)]
    questions = [grid.make_question(f"question number {i + 1}") for i in range(n_questions)]
    return grid.GridSpec(rows=rows, questions=questions)


def _llm_config(**over):
    return {"qa": {"enabled": True, "provider": "deepseek", "cache": False, **over}}


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def keyed(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(ask.llm_cache, "_CACHE_ROOT", tmp_path / "cache")


# --- keys and rows --------------------------------------------------------------------------
def test_question_key_is_stable_across_whitespace_and_case():
    assert grid.question_key("  What is the GUIDANCE? ") == grid.question_key("what is the guidance?")
    assert grid.question_key("a") != grid.question_key("b")


def test_question_key_survives_removing_and_re_adding_a_column():
    # Cells are keyed by (doc_id, question_key), so re-adding the same question restores them free.
    first = grid.make_question("What guidance is given?")
    again = grid.make_question("what   guidance is given?")
    assert first.key == again.key


def test_rows_from_choices_truncates_at_the_row_cap_and_reports_the_drop():
    choices = [SimpleNamespace(doc_id=f"d{i}", company="acme", period=None,
                               doc_type="report", title="t") for i in range(30)]
    rows, dropped = grid.rows_from_choices(choices)
    assert len(rows) == grid.HARD_MAX_ROWS and dropped == 30 - grid.HARD_MAX_ROWS


# --- the ceilings ---------------------------------------------------------------------------
def test_config_cannot_raise_the_hard_ceilings():
    lim = grid.limits({"grid": {"max_cells": 5000, "max_rows": 999, "max_columns": 40,
                                "chunk_size": 1000}})
    assert lim.max_cells == grid.HARD_MAX_CELLS
    assert lim.max_rows == grid.HARD_MAX_ROWS
    assert lim.max_columns == grid.HARD_MAX_COLUMNS
    assert lim.chunk_size == grid.HARD_MAX_CHUNK


def test_config_may_lower_a_ceiling_and_garbage_falls_back():
    assert grid.limits({"grid": {"max_cells": 6}}).max_cells == 6
    assert grid.limits({"grid": {"max_cells": "many"}}).max_cells == grid.HARD_MAX_CELLS
    assert grid.limits(None).max_cells == grid.HARD_MAX_CELLS


def test_plan_run_refuses_over_the_cell_cap_instead_of_truncating(offline):
    spec = _spec(n_rows=12, n_questions=6)          # 72 cells, cap is 24
    plan = grid.plan_run(spec, {})
    assert plan.blocked and "over the 24-cell cap" in plan.blocked
    assert plan.to_run == []


def test_run_batch_raises_above_the_cap_so_the_ui_cannot_bypass_the_guard(offline):
    spec = _spec(n_rows=12, n_questions=6)
    with pytest.raises(grid.GridLimitError):
        list(grid.run_batch(spec, spec.coords(), _Retriever()))


def test_plan_run_skips_cells_that_are_already_answered(offline):
    spec = _spec(2, 2)
    done = spec.coords()[0]
    existing = {done: grid.GridCell(doc_id=done[0], question_key=done[1], status=grid.OK)}
    plan = grid.plan_run(spec, existing)
    assert plan.already_done == 1 and done not in plan.to_run and len(plan.to_run) == 3


def test_plan_run_failed_only_targets_error_cells(offline):
    spec = _spec(2, 2)
    coords = spec.coords()
    existing = {coords[0]: grid.GridCell(*coords[0], status=grid.OK),
                coords[1]: grid.GridCell(*coords[1], status=grid.ERROR, error="Timeout")}
    assert grid.plan_run(spec, existing, only="failed").to_run == [coords[1]]
    assert grid.plan_run(spec, existing, only="all").to_run == coords


def test_llm_calls_is_zero_without_a_provider(offline):
    plan = grid.plan_run(_spec(2, 2), {})
    assert plan.mode == "evidence" and plan.llm_calls == 0 and len(plan.to_run) == 4
    assert plan.needs_confirmation is False


def test_a_big_llm_run_needs_confirmation(keyed):
    plan = grid.plan_run(_spec(4, 3), {}, config=_llm_config())     # 12 cells > threshold 8
    assert plan.mode == "llm" and plan.llm_calls == 12 and plan.needs_confirmation


# --- the cell -------------------------------------------------------------------------------
def test_a_cell_is_scoped_to_exactly_one_document(offline):
    retriever = _Retriever()
    grid.run_cell(grid.make_question("pricing pressure"), _row("acme/2024-1T"), retriever)
    assert retriever.calls, "the cell must actually search"
    for _query, filters in retriever.calls:
        assert filters.doc_ids == ["acme/2024-1T"]
        assert not filters.companies and not filters.doc_types and not filters.period_from


def test_offline_cell_returns_passages_not_answers(offline):
    cell = grid.run_cell(grid.make_question("margins"), _row(), _Retriever())
    assert cell.status == grid.OK and cell.mode == "evidence"
    assert cell.answer_text is None and cell.citations
    assert "Net sales grew" in grid.cell_summary(cell)


def test_offline_cell_makes_no_api_call(offline, monkeypatch):
    monkeypatch.setattr(ask.llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert grid.run_cell(grid.make_question("margins"), _row(), _Retriever()).status == grid.OK


def test_a_document_that_never_mentions_it_reads_not_mentioned(offline):
    cell = grid.run_cell(grid.make_question("margins"), _row(), _Retriever(hits=[]))
    assert cell.status == grid.NO_HITS
    assert grid.cell_summary(cell) == "— not mentioned in this document"


def test_no_hits_makes_no_api_call_even_with_a_provider(keyed, monkeypatch):
    monkeypatch.setattr(ask.llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    cell = grid.run_cell(grid.make_question("margins"), _row(), _Retriever(hits=[]),
                         config=_llm_config())
    assert cell.status == grid.NO_HITS


def test_llm_cell_records_the_answer_provider_and_citations(keyed, monkeypatch):
    monkeypatch.setattr(ask.llm, "complete", lambda *a, **k: "Net sales grew 8% [1].")
    cell = grid.run_cell(grid.make_question("margins"), _row(), _Retriever(),
                         config=_llm_config())
    assert cell.status == grid.OK and cell.mode == "llm"
    assert cell.answer_text == "Net sales grew 8% [1]."
    assert cell.provider and "deepseek" in cell.provider
    assert [c["marker"] for c in cell.citations] == [1]


def test_a_provider_failure_is_an_error_cell_not_a_silent_evidence_cell(keyed, monkeypatch):
    # run_ask swallows the exception and returns the evidence pack; without the llm_ready
    # comparison the grid would present a broken call as a legitimate offline cell.
    def _boom(*a, **k):
        raise TimeoutError("upstream")

    monkeypatch.setattr(ask.llm, "complete", _boom)
    cell = grid.run_cell(grid.make_question("margins"), _row(), _Retriever(),
                         config=_llm_config())
    assert cell.status == grid.ERROR and cell.error == "TimeoutError"
    assert grid.cell_summary(cell).startswith("— could not run")


def test_not_found_reads_as_not_found_never_as_blank(keyed, monkeypatch):
    from src.qa.prompts import NOT_FOUND

    monkeypatch.setattr(ask.llm, "complete", lambda *a, **k: NOT_FOUND)
    cell = grid.run_cell(grid.make_question("margins"), _row(), _Retriever(),
                         config=_llm_config())
    assert cell.status == grid.NOT_FOUND
    assert grid.cell_summary(cell) == "— not found in this document"


def test_a_cell_narrows_retrieval_depth_for_a_single_document():
    cfg = grid.cell_config({"qa": {"enabled": True, "top_k": 8}})
    assert cfg["qa"]["top_k"] == 6 and cfg["qa"]["enabled"] is True


def test_cell_config_does_not_mutate_the_app_config():
    original = {"qa": {"enabled": True, "top_k": 8}}
    grid.cell_config(original)
    assert original["qa"]["top_k"] == 8


# --- the runner -----------------------------------------------------------------------------
def test_run_batch_yields_one_cell_per_coordinate_in_order(offline):
    spec = _spec(2, 2)
    cells = list(grid.run_batch(spec, spec.coords(), _Retriever()))
    assert [(c.doc_id, c.question_key) for c in cells] == spec.coords()


def test_run_batch_skips_coordinates_the_grid_no_longer_has(offline):
    spec = _spec(1, 1)
    stale = [("gone/2024-1T", "nope-000000")] + spec.coords()
    assert len(list(grid.run_batch(spec, stale, _Retriever()))) == 1


# --- export ---------------------------------------------------------------------------------
def test_matrix_csv_is_one_row_per_document_and_one_column_per_question(offline):
    spec = _spec(2, 2)
    cells = {(c.doc_id, c.question_key): c
             for c in grid.run_batch(spec, spec.coords(), _Retriever())}
    import csv as _csv

    rows = list(_csv.reader(grid.to_matrix_csv(spec, cells).splitlines()))
    assert rows[0] == ["doc_id", "company", "period", "doc_type", "title",
                       "question number 1", "question number 2", "grid_mode"]
    assert len(rows) == 3
    assert rows[1][0] == "acme/2024-1T"


def test_matrix_csv_labels_evidence_cells_so_they_cannot_read_as_answers(offline):
    spec = _spec(1, 1)
    cells = {(c.doc_id, c.question_key): c
             for c in grid.run_batch(spec, spec.coords(), _Retriever())}
    out = grid.to_matrix_csv(spec, cells)
    assert "[evidence]" in out and out.rstrip().endswith("evidence")


def test_citations_csv_is_one_row_per_citation(offline):
    spec = _spec(1, 1)
    cells = {(c.doc_id, c.question_key): c
             for c in grid.run_batch(spec, spec.coords(), _Retriever())}
    import csv as _csv

    rows = list(_csv.reader(grid.to_citations_csv(spec, cells).splitlines()))
    assert rows[0][:3] == ["doc_id", "question", "marker"]
    assert len(rows) == 2 and rows[1][1] == "question number 1"


def test_export_of_an_unrun_grid_is_empty_not_broken():
    spec = _spec(2, 2)
    assert len(grid.to_matrix_csv(spec, {}).splitlines()) == 3
    assert len(grid.to_citations_csv(spec, {}).splitlines()) == 1


# --- presets --------------------------------------------------------------------------------
def test_shipped_presets_load_and_fit_the_column_cap():
    presets = grid.load_presets()
    assert presets, "configs/grid_presets.yaml must ship usable presets"
    cap = grid.limits().max_columns
    for preset in presets:
        assert preset["questions"] and len(preset["questions"]) <= cap
        assert preset["name"] and preset["description"]


def test_missing_presets_file_degrades_to_empty(tmp_path):
    assert grid.load_presets({"grid_presets_path": str(tmp_path / "nope.yaml")}) == []
