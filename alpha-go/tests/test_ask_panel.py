"""Ask panel rendering against a recording Streamlit stand-in: threads rail, agents, evidence
mode turn with research plan + sources, LLM-mode citation chips, redline view, and the
Search-side hand-offs ("Ask about this document", multi-select)."""
from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace

from src.search.filters import SearchFilters
from src.search.snippets import Snippet

from app.components import ask as ask_panel
from app.components import panels
from tests.test_reader_pane import _DOC, _RecordingSt, _Rerun, _Retriever, _Store


class _AskSt(_RecordingSt):
    """Adds the widgets the Ask panel uses (text_area, selectbox, spinner) to the recorder."""

    def __init__(self, click=None, *, text=""):
        super().__init__(click=click)
        self._text = text
        self.text_areas: list = []

    def text_area(self, label, *a, **k):
        self.text_areas.append(label)
        # what the analyst typed wins over the seeded value, as in the real widget
        return self._text or self.session_state.get(k.get("key"), "")

    def selectbox(self, label, options, *a, **k):
        idx = k.get("index", 0)
        return options[idx] if options else None

    def spinner(self, *a, **k):
        return nullcontext()


def _hit(chunk_id, md_path, text, doc_id="acme/2024-1T"):
    return SimpleNamespace(chunk_id=chunk_id, doc_id=doc_id, company="acme", period="2024-1T",
                           doc_type="quarterly_release", title="Q1 results", score=1.0,
                           snippet=Snippet(text=text, spans=[(0, 9)]), markdown_path=md_path,
                           char_start=0, char_end=60)


class _HitRetriever(_Retriever):
    def __init__(self, store, hits):
        super().__init__(store)
        self._hits = hits

    def search(self, *a, **k):
        return list(self._hits)


def _facets(company_periods):
    return SimpleNamespace(companies=list(company_periods), company_periods=company_periods)


def _render(tmp_path, st, *, config=None, filters=None, query="pricing pressure", hits=None,
            facets=None, store=None):
    doc = tmp_path / "acme_2024-1T.md"
    doc.write_text(_DOC, encoding="utf-8")
    store = store or _Store(str(doc))
    hits = hits if hits is not None else [_hit("c1", str(doc), "Net sales grew 8% and margins expanded.")]
    try:
        ask_panel.render_ask(st, _HitRetriever(store, hits), store, query=query,
                             filters=filters or SearchFilters(), config=config or {},
                             facets=facets or _facets({"acme": ["2024-1T"]}),
                             company_label=lambda c: c.upper(),
                             doc_type_label=lambda d: {"quarterly_release": "Quarterly release"}.get(d, d),
                             threads_path=tmp_path / "threads.db")
    except _Rerun:
        pass
    return st


def test_empty_panel_shows_evidence_notice_agents_and_scope(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    st = _render(tmp_path, _AskSt())
    assert any("Evidence mode" in c for c in st.captions)
    assert "＋ New thread" in st.buttons and "Ask" in st.buttons
    assert "Quarter-over-quarter redline" in st.buttons and "Key themes" in st.buttons
    assert st.text_areas == ["Ask the corpus a question"]
    assert st.session_state[ask_panel._Q_KEY] == "pricing pressure"      # seeded from the search
    assert any(m.startswith("Scope: ") and "Whole corpus" in m for m in st.markdowns)


def test_asking_in_evidence_mode_persists_a_turn_with_plan_and_evidence(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    st = _AskSt(click={"Ask"}, text="What drove net sales?")
    _render(tmp_path, st)
    assert st.reran
    from src.state.threads import ThreadStore

    store = ThreadStore(tmp_path / "threads.db")
    threads = store.list_threads()
    assert len(threads) == 1 and threads[0].title == "What drove net sales?"
    turn = store.turns(threads[0].thread_id)[0]
    assert turn.mode == "evidence" and turn.answer_text is None
    assert turn.plan["sub_queries"][0] == "net sales"
    assert turn.plan["docs_read"][0]["doc_id"] == "acme/2024-1T"
    assert turn.citations[0]["marker"] == 1 and turn.citations[0]["snippet"].startswith("Net sales")
    # the question box is cleared for the next run via the staged write
    assert st.session_state[panels._PENDING_SCOPE][ask_panel._Q_KEY] == ""

    # re-render the thread: plan expander, evidence pack, read-in-context button
    st2 = _AskSt()
    st2.session_state[ask_panel._THREAD_KEY] = threads[0].thread_id
    _render(tmp_path, st2)
    # "net sales" carries curated ES/EN equivalents → several sub-queries in the plan
    assert any(lbl.startswith("Research plan — ") and "1 document(s) read · evidence pack" in lbl
               for lbl in st2.expanders), st2.expanders
    assert any("- 🔍 net sales" == m for m in st2.markdowns)
    assert any(m.startswith("**Evidence pack**") for m in st2.markdowns)
    assert "Read in context ›" in st2.buttons
    assert st2.text_areas == ["Ask a follow-up…"] and "Ask follow-up" in st2.buttons


def test_llm_mode_turn_renders_citation_chips(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    from src.qa import ask as ask_mod

    monkeypatch.setattr(ask_mod.llm, "complete", lambda *a, **k: "Net sales grew 8% [1].")
    cfg = {"qa": {"enabled": True, "provider": "deepseek", "cache": False}}
    st = _AskSt(click={"Ask"}, text="Did sales grow?")
    _render(tmp_path, st, config=cfg)
    from src.state.threads import ThreadStore

    tid = ThreadStore(tmp_path / "threads.db").list_threads()[0].thread_id
    st2 = _AskSt()
    st2.session_state[ask_panel._THREAD_KEY] = tid
    _render(tmp_path, st2, config=cfg)
    assert any("Cited answers via deepseek" in c for c in st2.captions)
    answer_blocks = [m for m in st2.markdowns if "Net sales grew 8%" in m and "[1]" in m]
    assert answer_blocks and "title='source 1'" in answer_blocks[0]
    assert any(m == "**Sources**" for m in st2.markdowns)


def test_agent_pill_prefills_the_question_and_warns_without_a_company(tmp_path):
    st = _render(tmp_path, _AskSt(click={"Bull & bear debates"}))
    assert st.reran
    assert st.session_state[ask_panel._PREFILL_KEY].startswith("Lay out the bull case and the bear case for the company")
    assert "one company" in st.session_state["_ask_notice"]
    st2 = _render(tmp_path, _AskSt(click={"Bull & bear debates"}),
                  filters=SearchFilters(companies=["acme"]))
    assert "for ACME using only evidence" in st2.session_state[ask_panel._PREFILL_KEY]
    assert "_ask_notice" not in st2.session_state


def test_redline_agent_renders_the_diff_view(tmp_path):
    old = tmp_path / "rl_old_q1.md"
    new = tmp_path / "rl_new_q2.md"
    old.write_text("# R\n\nNet sales grew 8% in the quarter driven by traffic.\nWe opened 120 stores in the period.\n")
    new.write_text("# R\n\nNet sales grew 5% in the quarter driven by traffic.\nWe opened 186 stores in the period.\nDemand softened and traffic declined sharply.\n")

    class _TwoDocStore(_Store):
        def document_rows(self, where="", params=()):
            rows = [{"doc_id": "acme/2024-1T", "company": "acme", "period": "2024-1T",
                     "doc_type": "quarterly_release", "title": "Q1", "markdown_path": str(old)},
                    {"doc_id": "acme/2024-2T", "company": "acme", "period": "2024-2T",
                     "doc_type": "quarterly_release", "title": "Q2", "markdown_path": str(new)}]
            if params and len(params) == 2:
                return [r for r in rows if r["period"] == params[1]]
            return rows

    st = _AskSt()
    st.session_state[ask_panel._VIEW_KEY] = "redline"
    _render(tmp_path, st, store=_TwoDocStore(str(old)),
            facets=_facets({"acme": ["2024-1T", "2024-2T"]}))
    assert any(m.startswith("**ACME 2024-1T → ACME 2024-2T**") for m in st.markdowns), st.markdowns
    assert "**Reworded** (figures first)" in st.markdowns
    assert "**Added in the latest filing**" in st.markdowns
    assert any("grew 8%" in m and "grew 5%" in m for m in st.markdowns)
    assert any(c.startswith("Tone tally") for c in st.captions)
    assert "← Back to questions" in st.buttons


def test_search_rail_hand_offs_stage_ask_mode(tmp_path):
    from tests.test_reader_pane import _render as render_search_fake

    st = render_search_fake(tmp_path, _RecordingSt(click={"✨ Ask about this document"}))
    assert st.reran
    staged = st.session_state[panels._PENDING_SCOPE]
    assert staged["explore-mode"] == "Ask" and staged["flt-documents"] == ["acme/2024-1T"]
    assert st.session_state["_ask_prefill"] == "pricing pressure"
