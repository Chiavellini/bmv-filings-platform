"""Grid panel rendering against a recording Streamlit stand-in: the row seeding from Search, the
cost gates (nothing runs unrequested, the cap refuses, big runs need confirming), the chunked
resumable run, and the honest offline cell.

The load-bearing test here is ``test_nothing_runs_until_the_run_button_is_clicked`` — it renders
with a retriever that raises on any search, so a regression that computes cells on a plain rerun
fails loudly instead of quietly costing money.
"""
from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from src.qa import ask, grid as grid_mod
from src.search.filters import SearchFilters
from src.search.snippets import Snippet

from app.components import grid as grid_panel
from tests.test_reader_pane import _DOC, _RecordingSt, _Rerun, _Store


class _GridSt(_RecordingSt):
    """Adds the optional widgets the Grid uses; each can be switched off to prove the fallbacks."""

    def __init__(self, click=None, *, text="", checked=False, widgets=True):
        super().__init__(click=click)
        self._text = text
        self._checked = checked
        self._widgets = widgets
        self.text_inputs: list = []
        self.checkboxes: list = []
        self.progress_calls: list = []
        self.downloads: list = []
        self.multiselects: list = []

    def __getattribute__(self, name):
        if name in {"text_input", "checkbox", "progress", "download_button", "multiselect"}:
            if not object.__getattribute__(self, "_widgets"):
                raise AttributeError(name)
        return super().__getattribute__(name)

    def text_input(self, label, *a, **k):
        self.text_inputs.append(label)
        return self._text

    def checkbox(self, label, *a, **k):
        self.checkboxes.append(label)
        return self._checked

    def progress(self, value, *a, **k):
        self.progress_calls.append((value, k.get("text", "")))

    def download_button(self, label, data, *a, **k):
        self.downloads.append((label, data))
        return False

    def multiselect(self, label, options, *a, **k):
        self.multiselects.append(label)
        default = k.get("default")
        return list(default if default is not None else options)

    def spinner(self, *a, **k):
        return nullcontext()


def _choice(doc_id, period="2024-1T"):
    return SimpleNamespace(doc_id=doc_id, title="Q1 results", company="acme", period=period,
                           doc_type="quarterly_release", companies=("acme",))


class _Facets:
    def __init__(self, choices):
        self._choices = choices

    def documents_for(self, companies, industries, period_from=None, period_to=None,
                      doc_types=None):
        return list(self._choices)


def _hit(md_path, doc_id="acme/2024-1T"):
    return SimpleNamespace(chunk_id=f"c-{doc_id}", doc_id=doc_id, company="acme",
                           period="2024-1T", doc_type="quarterly_release", title="Q1 results",
                           score=1.0, snippet=Snippet(text="Net sales grew 8%.", spans=[(0, 9)]),
                           markdown_path=md_path, char_start=0, char_end=17)


class _Retriever:
    def __init__(self, md_path):
        self.store = None
        self._md = md_path
        self.searches = 0

    def search(self, query, *, filters=None, limit=20, **kw):
        self.searches += 1
        doc_id = (filters.doc_ids or ["acme/2024-1T"])[0] if filters is not None else "acme/2024-1T"
        return [_hit(self._md, doc_id)]


class _ExplodingRetriever:
    """Any search at all is a bug: rendering must never compute cells."""

    store = None

    def search(self, *a, **k):
        raise AssertionError("the grid searched without an explicit Run")


def _render(st, retriever, *, choices=None, config=None, filters=None):
    if choices is None:
        choices = [_choice("acme/2024-1T"), _choice("acme/2024-2T", "2024-2T")]
    try:
        grid_panel.render_grid(st, retriever, None, query="", filters=filters or SearchFilters(),
                               config=config or {}, facets=_Facets(choices),
                               company_label=lambda c: c.upper(),
                               doc_type_label=lambda d: "Quarterly release")
    except _Rerun:
        pass


@pytest.fixture
def doc(tmp_path):
    path = tmp_path / "acme_2024-1T.md"
    path.write_text(_DOC, encoding="utf-8")
    return str(path)


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def keyed(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(ask.llm_cache, "_CACHE_ROOT", tmp_path / "cache")


def _llm_config(**over):
    return {"qa": {"enabled": True, "provider": "deepseek", "cache": False}, **over}


def _seed(st, questions=("what about margins",), rows=("acme/2024-1T",)):
    st.session_state[grid_panel._QUESTIONS_KEY] = [
        {"key": grid_mod.question_key(q), "text": q} for q in questions]
    st.session_state[grid_panel._ROWS_KEY] = list(rows)


# --- scaffolding ----------------------------------------------------------------------------
def test_empty_grid_offers_presets_and_a_question_box(offline, doc):
    st = _GridSt()
    _render(st, _ExplodingRetriever())
    assert any("Question sets" in m for m in st.markdowns)
    assert "Quarterly scorecard" in st.buttons
    assert "Add a question" in st.text_inputs


def test_rows_seed_from_the_documents_ticked_in_search(offline, doc):
    st = _GridSt()
    st.session_state["ask_picked"] = ["acme/2024-2T"]
    _render(st, _ExplodingRetriever())
    assert st.session_state[grid_panel._ROWS_KEY] == ["acme/2024-2T"]


def test_a_preset_click_fills_the_question_columns(offline, doc):
    st = _GridSt(click={"Quarterly scorecard"})
    _render(st, _ExplodingRetriever())
    questions = st.session_state[grid_panel._QUESTIONS_KEY]
    assert questions and len(questions) <= grid_mod.HARD_MAX_COLUMNS
    assert any("revenue" in q["text"].lower() for q in questions)


def test_no_documents_in_scope_says_so_instead_of_rendering_an_empty_grid(offline):
    st = _GridSt()
    _render(st, _ExplodingRetriever(), choices=[])
    assert any("No documents in the current scope" in str(i) for i in st.infos)


# --- the cost gates -------------------------------------------------------------------------
def test_nothing_runs_until_the_run_button_is_clicked(offline, doc):
    """The cost-safety regression test: rendering must never touch the retriever."""
    st = _GridSt()
    _seed(st)
    _render(st, _ExplodingRetriever())          # would raise AssertionError on any search
    assert grid_panel._RUN_KEY not in st.session_state
    assert not st.session_state.get(grid_panel._CELLS_KEY)


def test_the_budget_line_states_the_cells_and_the_call_count(offline, doc):
    st = _GridSt()
    _seed(st, questions=("margins", "guidance"), rows=("acme/2024-1T", "acme/2024-2T"))
    _render(st, _ExplodingRetriever())
    line = next(c for c in st.captions if "cell(s)" in c and "to run" in c)
    assert "**4 cell(s)**" in line and "0 already answered" in line and "4 to run" in line
    assert "no LLM key — evidence mode, no API calls" in line


def test_the_budget_line_reports_real_llm_calls_when_keyed(keyed, doc):
    st = _GridSt()
    _seed(st, questions=("margins",), rows=("acme/2024-1T", "acme/2024-2T"))
    _render(st, _ExplodingRetriever(), config=_llm_config())
    line = next(c for c in st.captions if "cell(s)" in c and "to run" in c)
    assert "2 LLM call(s)" in line and "deepseek" in line


def test_running_the_button_starts_a_batch_and_the_run_writes_cells(offline, doc):
    st = _GridSt(click={"▶ Run 2 cell(s)"})
    _seed(st, questions=("margins",), rows=("acme/2024-1T", "acme/2024-2T"))
    retriever = _Retriever(doc)
    _render(st, retriever)                                   # press -> flag written, reruns
    assert st.session_state[grid_panel._RUN_KEY]["total"] == 2
    _render(st, retriever)                                   # next script run -> the chunk runs
    cells = st.session_state[grid_panel._CELLS_KEY]
    assert len(cells) == 2 and all(c.status == grid_mod.OK for c in cells.values())
    assert grid_panel._RUN_KEY not in st.session_state


def test_a_large_run_is_refused_until_the_confirmation_is_ticked(keyed, doc):
    questions = tuple(f"question {i}" for i in range(3))
    rows = tuple(f"acme/2024-{i}T" for i in range(1, 5))
    choices = [_choice(d, f"2024-{i}T") for i, d in enumerate(rows, start=1)]

    unticked = _GridSt(click={"▶ Run 12 cell(s)"}, checked=False)
    _seed(unticked, questions=questions, rows=rows)
    _render(unticked, _ExplodingRetriever(), choices=choices, config=_llm_config())
    assert grid_panel._RUN_KEY not in unticked.session_state
    assert any("Tick the confirmation" in str(w) for w in unticked.warnings)
    assert any("up to 12 LLM calls" in c for c in unticked.checkboxes)

    ticked = _GridSt(click={"▶ Run 12 cell(s)"}, checked=True)
    _seed(ticked, questions=questions, rows=rows)
    _render(ticked, _ExplodingRetriever(), choices=choices, config=_llm_config())
    assert ticked.session_state[grid_panel._RUN_KEY]["total"] == 12


def test_the_confirmation_gate_fails_closed_without_a_checkbox_widget(keyed, doc):
    questions = tuple(f"question {i}" for i in range(3))
    rows = tuple(f"acme/2024-{i}T" for i in range(1, 5))
    choices = [_choice(d, f"2024-{i}T") for i, d in enumerate(rows, start=1)]
    st = _GridSt(click={"▶ Run 12 cell(s)"}, widgets=False)
    _seed(st, questions=questions, rows=rows)
    _render(st, _ExplodingRetriever(), choices=choices, config=_llm_config())
    assert grid_panel._RUN_KEY not in st.session_state


def test_a_grid_over_the_cell_cap_is_refused_and_offers_no_run_button(offline, doc):
    rows = tuple(f"acme/2024-{i}T" for i in range(1, 13))
    choices = [_choice(d, f"2024-{i}T") for i, d in enumerate(rows, start=1)]
    st = _GridSt()
    _seed(st, questions=tuple(f"question {i}" for i in range(6)), rows=rows)
    _render(st, _ExplodingRetriever(), choices=choices)
    assert any("over the 24-cell cap" in str(w) for w in st.warnings)
    assert not any(b.startswith("▶ Run") for b in st.buttons)


def test_answered_cells_are_skipped_so_adding_a_row_is_cheap(offline, doc):
    st = _GridSt()
    _seed(st, questions=("margins",), rows=("acme/2024-1T", "acme/2024-2T"))
    key = ("acme/2024-1T", grid_mod.question_key("margins"))
    st.session_state[grid_panel._CELLS_KEY] = {
        key: grid_mod.GridCell(doc_id=key[0], question_key=key[1], status=grid_mod.OK)}
    _render(st, _ExplodingRetriever())
    line = next(c for c in st.captions if "cell(s)" in c and "to run" in c)
    assert "1 already answered" in line and "1 to run" in line


# --- the chunked run ------------------------------------------------------------------------
def test_a_run_processes_one_chunk_per_script_run_and_resumes(offline, doc):
    st = _GridSt()
    _seed(st, questions=("margins", "guidance"), rows=("acme/2024-1T", "acme/2024-2T"))
    config = {"grid": {"chunk_size": 1}}
    st.session_state[grid_panel._RUN_KEY] = {
        "pending": [list(c) for c in
                    grid_mod.GridSpec(rows=[grid_mod.GridRow("acme/2024-1T", "acme", "2024-1T",
                                                             "quarterly_release", "t"),
                                            grid_mod.GridRow("acme/2024-2T", "acme", "2024-2T",
                                                             "quarterly_release", "t")],
                                      questions=[grid_mod.make_question("margins"),
                                                 grid_mod.make_question("guidance")]).coords()],
        "done": 0, "total": 4, "only": None}
    retriever = _Retriever(doc)

    _render(st, retriever, config=config)
    assert len(st.session_state[grid_panel._CELLS_KEY]) == 1
    assert st.session_state[grid_panel._RUN_KEY]["done"] == 1
    assert st.reran and st.progress_calls

    for _ in range(3):
        _render(st, retriever, config=config)
    assert len(st.session_state[grid_panel._CELLS_KEY]) == 4
    assert grid_panel._RUN_KEY not in st.session_state


def test_stop_clears_the_batch_and_keeps_what_finished(offline, doc):
    st = _GridSt(click={"⏹ Stop"})
    _seed(st, questions=("margins",), rows=("acme/2024-1T", "acme/2024-2T"))
    done = ("acme/2024-1T", grid_mod.question_key("margins"))
    st.session_state[grid_panel._CELLS_KEY] = {
        done: grid_mod.GridCell(doc_id=done[0], question_key=done[1], status=grid_mod.OK)}
    st.session_state[grid_panel._RUN_KEY] = {
        "pending": [["acme/2024-2T", grid_mod.question_key("margins")]],
        "done": 1, "total": 2, "only": None}
    _render(st, _ExplodingRetriever())
    assert grid_panel._RUN_KEY not in st.session_state
    assert done in st.session_state[grid_panel._CELLS_KEY]


def test_a_stale_coordinate_left_by_an_edited_grid_is_skipped(offline, doc):
    st = _GridSt()
    _seed(st, questions=("margins",), rows=("acme/2024-1T",))
    st.session_state[grid_panel._RUN_KEY] = {
        "pending": [["gone/2024-1T", "nope-000000"]], "done": 0, "total": 1, "only": None}
    _render(st, _Retriever(doc))
    assert grid_panel._RUN_KEY not in st.session_state


# --- the cells ------------------------------------------------------------------------------
def test_an_offline_cell_shows_the_passage_and_says_it_is_not_an_answer(offline, doc):
    st = _GridSt()
    _seed(st)
    st.session_state[grid_panel._RUN_KEY] = {
        "pending": [["acme/2024-1T", grid_mod.question_key("what about margins")]],
        "done": 0, "total": 1, "only": None}
    _render(st, _Retriever(doc))
    _render(st, _Retriever(doc))
    assert any("**not an answer**" in c for c in st.captions)
    # the query term is <mark>-wrapped, so assert on the tail of the passage
    assert any("grew 8%" in m and "<mark>" in m for m in st.markdowns)
    assert any(e.startswith("Sources (") for e in st.expanders)


def test_a_document_that_does_not_mention_it_reads_not_mentioned(offline, doc):
    st = _GridSt()
    _seed(st)
    key = ("acme/2024-1T", grid_mod.question_key("what about margins"))
    st.session_state[grid_panel._CELLS_KEY] = {
        key: grid_mod.GridCell(doc_id=key[0], question_key=key[1], status=grid_mod.NO_HITS)}
    _render(st, _ExplodingRetriever())
    assert any("not mentioned in this document" in m for m in st.markdowns)


def test_an_unrun_cell_reads_not_run_yet_rather_than_looking_empty(offline, doc):
    st = _GridSt()
    _seed(st)
    _render(st, _ExplodingRetriever())
    assert any("not run yet" in m for m in st.markdowns)


def test_read_in_context_opens_the_passage_below_the_matrix(offline, doc):
    st = _GridSt()
    _seed(st)
    key = ("acme/2024-1T", grid_mod.question_key("what about margins"))
    st.session_state[grid_panel._CELLS_KEY] = {key: grid_mod.GridCell(
        doc_id=key[0], question_key=key[1], status=grid_mod.OK,
        citations=[{"marker": 1, "doc_id": key[0], "chunk_id": "c1", "title": "Q1 results",
                    "company": "acme", "period": "2024-1T", "doc_type": "quarterly_release",
                    "markdown_path": doc, "char_start": 0, "char_end": 17,
                    "snippet": "Net sales grew 8%.", "spans": [[0, 9]]}])}
    open_label = f"grid-open-{key[0]}-{key[1]}-1"
    assert open_label  # the key shape the button uses
    st2 = _GridSt(click={"Read in context ›"})
    st2.session_state.update(st.session_state)
    _render(st2, _ExplodingRetriever())
    assert st2.session_state[grid_panel._OPEN_KEY] == (key[0], key[1], 1)

    st3 = _GridSt()
    st3.session_state.update(st2.session_state)
    _render(st3, _ExplodingRetriever())
    assert any("Passage [1]" in m for m in st3.markdowns)


# --- columns and export ---------------------------------------------------------------------
def test_adding_and_removing_a_question_column(offline, doc):
    st = _GridSt(click={"＋ Add column"}, text="What about refinancing?")
    _seed(st, questions=())
    _render(st, _ExplodingRetriever())
    assert [q["text"] for q in st.session_state[grid_panel._QUESTIONS_KEY]] == \
        ["What about refinancing?"]

    dropped = _GridSt(click={"✕"})
    dropped.session_state.update(st.session_state)
    dropped.session_state[grid_panel._ROWS_KEY] = ["acme/2024-1T"]
    _render(dropped, _ExplodingRetriever())
    assert dropped.session_state[grid_panel._QUESTIONS_KEY] == []


def test_a_seventh_question_is_refused(offline, doc):
    st = _GridSt(click={"＋ Add column"}, text="one more")
    _seed(st, questions=tuple(f"question {i}" for i in range(6)))
    _render(st, _ExplodingRetriever())
    assert len(st.session_state[grid_panel._QUESTIONS_KEY]) == 6
    assert any("at most 6 questions" in str(w) for w in st.warnings)


def test_both_csv_exports_are_offered(offline, doc):
    st = _GridSt()
    _seed(st)
    _render(st, _ExplodingRetriever())
    labels = [label for label, _ in st.downloads]
    assert "⬇︎ Matrix CSV" in labels and "⬇︎ Citations CSV" in labels
    matrix = next(data for label, data in st.downloads if label == "⬇︎ Matrix CSV")
    assert "what about margins" in matrix


def test_the_panel_renders_without_any_optional_widget(offline, doc):
    st = _GridSt(widgets=False)
    _seed(st)
    _render(st, _ExplodingRetriever())          # must not raise
    assert any("not run yet" in m for m in st.markdowns)
