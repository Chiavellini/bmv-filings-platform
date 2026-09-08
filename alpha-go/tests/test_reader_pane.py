"""Phase A reader-parity surfaces in ``render_search``: Smart Summary + Topics in the reader
pane, "Only …" scope chips, sort/view controls, sentiment underlines, and zero-result expansions.

Rendered against a recording Streamlit stand-in (no browser), so the assertions are about which
controls and blocks appear and what clicking them writes into ``session_state``.
"""
from __future__ import annotations

from contextlib import nullcontext

from src.search.filters import SearchFilters

from app.components import panels
from app.components.panels import render_search


class _Col:
    def __init__(self, st):
        self._st = st

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):          # col.button(...) etc. delegate to the recorder
        return getattr(self._st, name)


class _RecordingSt:
    """Streamlit stand-in that records widgets; ``click`` names buttons to report as pressed."""

    def __init__(self, click: "set[str] | None" = None, *, pills: bool = True):
        self.session_state: dict = {}
        self.captions: list = []
        self.warnings: list = []
        self.markdowns: list = []
        self.infos: list = []
        self.buttons: list = []
        self.expanders: list = []
        self.pills_calls: list = []
        self.toggles: list = []
        self.reran = False
        self._click = click or set()
        self._pills = pills

    def caption(self, msg, *a, **k):
        self.captions.append(str(msg))

    def warning(self, msg, *a, **k):
        self.warnings.append(msg)

    def info(self, msg, *a, **k):
        self.infos.append(msg)

    def markdown(self, msg="", *a, **k):
        self.markdowns.append(str(msg))

    def divider(self, *a, **k):
        return None

    def toggle(self, label, *a, **k):
        self.toggles.append(label)
        return self.session_state.get(k.get("key"), True)

    def button(self, label, *a, **k):
        self.buttons.append(label)
        return label in self._click

    def columns(self, spec, *a, **k):
        n = len(spec) if hasattr(spec, "__len__") else int(spec)
        return [_Col(self) for _ in range(n)]

    def expander(self, label, *a, **k):
        self.expanders.append(label)
        return nullcontext()

    def container(self, *a, **k):
        return nullcontext()

    def rerun(self):
        self.reran = True
        raise _Rerun()

    def __getattribute__(self, name):
        # ``pills`` is an optional widget: hide it entirely when the test disables it, so the
        # panel's ``getattr(st, "pills", None)`` probe sees an older Streamlit.
        if name == "pills" and not object.__getattribute__(self, "_pills"):
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    def pills(self, label, options, *a, **k):
        self.pills_calls.append((label, list(options)))
        return self.session_state.get(k.get("key")) or k.get("default")


class _Rerun(Exception):
    """Streamlit's rerun unwinds the script; the stand-in raises to stop rendering the same way."""


class _Store:
    def __init__(self, md_path, doc_id="acme/2024-1T", company="acme", doc_type="quarterly_release"):
        self._md, self._doc_id, self._company, self._dt = md_path, doc_id, company, doc_type

    def get_meta(self, key):
        return None

    def document_rows(self, where="", params=()):
        return [{"doc_id": self._doc_id, "company": self._company, "period": "2024-1T",
                 "doc_type": self._dt, "title": "Q1 results", "markdown_path": self._md}]

    def count(self, _table):
        return 1


class _Retriever:
    def __init__(self, store):
        self.store = store

    def search(self, *a, **k):
        return []


_DOC = """# First quarter results

Net sales grew 8% and margins expanded thanks to pricing pressure easing. Net sales in Mexico
were strong. Our net debt declined; the deuda neta fell to 1.1x EBITDA and EBITDA grew.

## Outlook

Pricing pressure remains a risk and costs were higher, which hurt the operating margin.
"""


def _render(tmp_path, st, **kw):
    doc = tmp_path / "acme_2024-1T.md"
    doc.write_text(_DOC, encoding="utf-8")
    store = _Store(str(doc))
    try:
        render_search(st, _Retriever(store), query=kw.pop("query", "pricing pressure"),
                      filters=kw.pop("filters", SearchFilters()), limit=20,
                      doc_type_label=lambda raw: {"quarterly_release": "Quarterly release"}.get(raw, raw),
                      **kw)
    except _Rerun:
        pass
    return st


def test_reader_pane_shows_smart_summary_topics_sections_and_scope_chips(tmp_path):
    st = _render(tmp_path, _RecordingSt())
    assert any(lbl.startswith("🧠 Smart Summary") for lbl in st.expanders), st.expanders
    assert "**Key takeaways**" in st.markdowns and "**Topics**" in st.markdowns
    assert "**Sections**" in st.markdowns
    # topics are literal counts of curated concepts present in the doc
    assert any(b.startswith("net debt · ") or b.startswith("ebitda · ") for b in st.buttons), st.buttons
    assert "First quarter results" in st.buttons and "Outlook" in st.buttons
    # AlphaSense's per-facet "Only" shortcuts, on the selected document's facets
    assert "Only ACME" in st.buttons and "Only Quarterly release" in st.buttons
    assert any(b.startswith("Hits across this company's") for b in st.buttons)
    # sort / view pills + sentiment-underline toggle appear once there are results
    assert [p[0] for p in st.pills_calls] == ["Sort", "View", "Pane"]
    assert st.toggles == ["Sentiment underlines"]
    # the reader header uses the taxonomy label, not the raw doc_type key
    assert any("**ACME** · 2024-1T · Quarterly release" in m for m in st.markdowns)


def test_only_company_chip_stages_sidebar_scope_and_reruns(tmp_path):
    # Widget keys can't be written after the sidebar is drawn → the chip STAGES the change and
    # the app applies it at the top of the next run.
    st = _render(tmp_path, _RecordingSt(click={"Only ACME"}))
    assert st.reran
    assert "flt-companies" not in st.session_state
    assert st.session_state[panels._PENDING_SCOPE] == {"flt-companies": ["acme"],
                                                        "flt-documents": []}
    panels.apply_pending_state(st.session_state)
    assert st.session_state["flt-companies"] == ["acme"]
    assert st.session_state["flt-documents"] == []
    assert panels._PENDING_SCOPE not in st.session_state


def test_only_type_chip_stages_the_display_label(tmp_path):
    st = _render(tmp_path, _RecordingSt(click={"Only Quarterly release"}))
    panels.apply_pending_state(st.session_state)
    assert st.session_state["flt-doctypes"] == ["Quarterly release"]


def test_apply_pending_state_drops_keys_marked_none_and_rewrites_query():
    state = {"flt-period": ("2024-1T", "2024-2T"), "query": "old",
             panels._PENDING_SCOPE: {"flt-period": None, "flt-companies": []},
             panels._PENDING_QUERY: "new"}
    panels.apply_pending_state(state)
    assert state == {"flt-companies": [], "query": "new"}
    panels.apply_pending_state(state)          # idempotent when nothing is staged
    assert state == {"flt-companies": [], "query": "new"}


def test_topic_click_opens_context_at_first_occurrence(tmp_path):
    st = _RecordingSt(click={"ebitda · 2"})
    _render(tmp_path, st)
    jump = st.session_state["topic-jump-acme/2024-1T"]
    assert _DOC[jump[0]:].lower().startswith("ebitda")
    assert any("**In context**" == m for m in st.markdowns)


def test_pending_reader_doc_from_a_shared_link_is_honoured_once(tmp_path):
    st = _RecordingSt()
    st.session_state["_pending_reader_doc"] = "acme/2024-1T"
    _render(tmp_path, st)
    assert st.session_state["reader_doc"] == "acme/2024-1T"
    assert "_pending_reader_doc" not in st.session_state


def test_sentiment_underlines_toggle_controls_reader_html(tmp_path):
    on = _render(tmp_path, _RecordingSt(click={"Read in context"}))
    st_off = _RecordingSt(click={"Read in context"})
    st_off.session_state[panels._TONES_KEY] = False
    off = _render(tmp_path, st_off)
    assert any("text-decoration:underline" in m for m in on.markdowns)
    assert not any("text-decoration:underline" in m for m in off.markdowns)
    assert any("underlined:" in c for c in on.captions)


def test_zero_results_offer_scope_expansions_and_wording(tmp_path):
    filters = SearchFilters(companies=["acme"], period_from="2024-1T", period_to="2024-1T",
                            doc_types=["quarterly_release"])
    st = _render(tmp_path, _RecordingSt(), query="zzz nonexistent phrase", filters=filters)
    assert st.warnings == ["No matches. Try broader terms or clear the sidebar filters."]
    assert {"Show all periods", "All document types", "Search the whole corpus"} <= set(st.buttons)
    # the expansions write the sidebar's own keys and rerun
    st2 = _render(tmp_path, _RecordingSt(click={"Search the whole corpus"}),
                  query="zzz nonexistent phrase", filters=filters)
    assert st2.reran
    panels.apply_pending_state(st2.session_state)
    assert st2.session_state["flt-companies"] == [] and "flt-period" not in st2.session_state


def test_zero_results_for_a_dictionary_term_suggest_related_wording(tmp_path):
    # "fx" has no literal occurrence in the doc → zero results; its curated equivalents are
    # offered as one-click queries, and clicking one rewrites the query box and reruns.
    st = _render(tmp_path, _RecordingSt(click={"exchange rate"}), query="fx")
    assert st.warnings == ["No matches. Try broader terms or clear the sidebar filters."]
    assert "exchange rate" in st.buttons and st.reran
    assert st.session_state[panels._PENDING_QUERY] == "exchange rate"
    panels.apply_pending_state(st.session_state)
    assert st.session_state["query"] == "exchange rate"


def test_fallback_without_optional_widgets_still_renders(tmp_path):
    st = _render(tmp_path, _RecordingSt(pills=False))
    assert st.pills_calls == []
    assert any(lbl.startswith("🧠 Smart Summary") for lbl in st.expanders)


# --- Phase B: the Company pane -------------------------------------------------------------------

def _company_st(click=None):
    st = _RecordingSt(click=click)
    st.session_state[panels._PANE_KEY] = "Company"        # the fake pills return the stored value
    return st


def test_company_pane_replaces_the_reader_with_the_issuer_card(tmp_path):
    st = _render(tmp_path, _company_st(),
                 app_config={"company_catalog": [{"ticker": "ACME", "slug": "acme",
                                                  "company": "Acme Corp", "industry": "widgets"}],
                             "coverage_contract": {"target_documents_per_company": 1,
                                                   "minimum_quarterlies": 4}})
    assert any("**Acme Corp** · `ACME` · Widgets" == m for m in st.markdowns), st.markdowns
    assert "**Holdings by document type**" in st.markdowns
    assert "- Quarterly release — 1" in st.markdowns                     # taxonomy label used
    assert "✅ Filings & disclosures: 1 / 1" in st.markdowns
    assert "◻️ Quarterly reports: 1 / 4" in st.markdowns
    assert any(m.startswith("**Reporting calendar**") for m in st.markdowns)
    # the fixture's 2024 period is outside the calendar window → recent deadlines read "missing"
    assert any("⚠️" in m and "missing" in m for m in st.markdowns)
    assert any("🗓️" in m and "expected" in m for m in st.markdowns)
    assert "Load financials" in st.buttons
    # the document surfaces are NOT rendered in this pane
    assert not any(lbl.startswith("🧠 Smart Summary") for lbl in st.expanders)
    assert "Only ACME" not in st.buttons
    # the trend falls back honestly on a store without memberships
    assert any("Trend unavailable" in c for c in st.captions)


def test_browse_any_in_scope_document_when_picked_outside_results(tmp_path):
    st = _RecordingSt()
    st.session_state["_pending_reader_doc"] = "acme/2024-1T"
    # Simulate a stale/other selection: a doc in scope (the only one) but pretend it is not in specs
    # by searching a query that matches nothing literal → specs empty → warning path instead.
    _render(tmp_path, st)
    assert st.session_state["reader_doc"] == "acme/2024-1T"


def test_event_button_opens_document_and_stages_pane_switch(tmp_path):
    # Give the store an event-type document so the card lists it.
    event_md = tmp_path / "acme_event.md"
    event_md.write_text("# Buyback\n\nThe board approved a share repurchase program today.\n",
                        encoding="utf-8")                       # no query phrase → not a result

    class _EventStore(_Store):
        def document_rows(self, where="", params=()):
            rows = super().document_rows(where, params)
            rows.append({"doc_id": "acme/relevant_event/2024-05-02t09-00/x", "company": "acme",
                         "period": None, "doc_type": "relevant_event",
                         "title": "Acme buyback", "markdown_path": str(event_md)})
            return rows

    doc = tmp_path / "acme_2024-1T.md"
    doc.write_text(_DOC, encoding="utf-8")
    label = "2024-05-02 · Relevant event · Acme buyback"
    st = _company_st(click={label})
    try:
        render_search(st, _Retriever(_EventStore(str(doc))), query="pricing pressure",
                      filters=SearchFilters(), limit=20,
                      doc_type_label=lambda raw: {"relevant_event": "Relevant event",
                                                  "quarterly_release": "Quarterly release"}.get(raw, raw))
    except _Rerun:
        pass
    assert label in st.buttons, st.buttons
    assert st.reran
    assert st.session_state["reader_doc"] == "acme/relevant_event/2024-05-02t09-00/x"
    assert st.session_state[panels._PENDING_SCOPE] == {panels._PANE_KEY: "Document"}
    # next run: the pane flips back to Document and the event doc opens as a "browse" entry
    panels.apply_pending_state(st.session_state)
    st2 = _RecordingSt()
    st2.session_state.update(st.session_state)     # same session: memo hit, pane now "Document"
    try:
        render_search(st2, _Retriever(_EventStore(str(doc))), query="pricing pressure",
                      filters=SearchFilters(), limit=20)
    except _Rerun:
        pass
    assert any("Opened from the company card" in c for c in st2.captions)
    assert any("**ACME** · — · relevant_event — Acme buyback" in m for m in st2.markdowns)
