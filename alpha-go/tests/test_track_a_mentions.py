"""Track A — corpus-wide literal mention scan (foolproof Ctrl+F).

Two guarantees:
  * ``KeywordIndex.matching_documents`` is a doc-level FTS scan with NO candidate-pool cap —
    it returns every document containing the term (grep-equivalent set).
  * ``group_hits(doc_rows=…)`` counts occurrences across that whole set, including documents the
    retriever never surfaced, so the reported total equals a literal grep of the corpus.
"""
from __future__ import annotations

import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from src.index.keyword_index import KeywordIndex
from src.search.snippets import Snippet

from app.components.linear_results import group_hits


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


# --- doc-level FTS: the matching-document universe ------------------------------------------

def test_matching_documents_equals_grep_of_the_corpus(built_index):
    store, _ = built_index
    rows = KeywordIndex(store).matching_documents("ebitda")
    got = sorted(r["markdown_path"] for r in rows)

    all_docs = store.connect().execute("SELECT markdown_path FROM documents").fetchall()
    expected = sorted(d["markdown_path"] for d in all_docs
                      if re.search("ebitda", _read(d["markdown_path"]), re.I))
    assert got == expected and got                    # every matching doc, none missing


def test_matching_doc_ids_is_a_distinct_set(built_index):
    store, _ = built_index
    ids = KeywordIndex(store).matching_documents("revenues")
    assert KeywordIndex(store).matching_doc_ids("revenues") == {r["doc_id"] for r in ids}


def test_matching_documents_honors_filters(built_index):
    store, _ = built_index
    ki = KeywordIndex(store)
    assert ki.matching_documents("revenues", filter_sql="documents.company IN (?)",
                                 filter_params=["acme"])
    assert ki.matching_documents("revenues", filter_sql="documents.company IN (?)",
                                 filter_params=["nobody"]) == []


def test_matching_documents_unmatched_term_empty(built_index):
    store, _ = built_index
    assert KeywordIndex(store).matching_documents("zzzqqqxxx") == []


# --- mention completeness: scan total == literal grep total ---------------------------------

def test_corpus_scan_total_equals_grep_total(built_index):
    # The foolproof path: universe = EVERY document (store.document_rows), substring-scanned.
    store, _ = built_index
    term = "revenues"
    rows = store.document_rows()
    groups = group_hits([], terms=[term], doc_text_fn=_read, doc_rows=rows)

    scan_total = sum(g.total_matches for g in groups)
    grep_total = sum(len(re.findall(re.escape(term), _read(r["markdown_path"]), re.I))
                     for r in rows)
    assert scan_total == grep_total > 0               # Ctrl+F equivalence


def test_substring_inside_larger_token_is_not_counted_as_exact(built_index):
    # Exact mentions are lexical words; morphological variants belong in related wording.
    store, _ = built_index
    rows = store.document_rows()
    groups = group_hits([], terms=["revenue"], doc_text_fn=_read, doc_rows=rows)
    scan_total = sum(g.total_matches for g in groups)
    grep_total = sum(len(re.findall(r"\brevenue\b", _read(r["markdown_path"]), re.I)) for r in rows)
    assert scan_total == grep_total == 0
    # and FTS-token counting would MISS these (docs have "Revenues", token != "revenue");
    # synonyms off to isolate the tokenizer gap from the synonym dictionary.
    assert KeywordIndex(store).matching_documents("revenue", expand_synonyms=False) == []


# --- low-ranked docs are counted (the actual bug) ------------------------------------------

def _rows(*paths):
    return [{"doc_id": p, "company": "c", "period": None, "doc_type": "q",
             "title": p, "markdown_path": p} for p in paths]


def _hit(path):
    return SimpleNamespace(doc_id=path, company="c", period=None, doc_type="q", title=path,
                           markdown_path=path, char_start=0, char_end=10,
                           snippet=Snippet(text="x", spans=[(0, 1)]))


_TEXT = {
    "hi.md": "returnable A. returnable B.",                       # surfaced, 2 occurrences
    "lo1.md": "some buried returnable here.",                     # NOT surfaced, 1 occurrence
    "lo2.md": "returnable and returnable and returnable.",        # NOT surfaced, 3 occurrences
}


def test_unsurfaced_matching_docs_are_counted():
    groups = group_hits([_hit("hi.md")], terms=["returnable"],
                        doc_rows=_rows("hi.md", "lo1.md", "lo2.md"),
                        doc_text_fn=lambda p: _TEXT[p])
    assert {g.doc_id for g in groups} == {"hi.md", "lo1.md", "lo2.md"}   # low-ranked included
    assert sum(g.total_matches for g in groups) == 6                     # 2 + 1 + 3


def test_display_cap_windows_top_docs_but_counts_all():
    groups = group_hits([_hit("hi.md")], terms=["returnable"], max_docs=1,
                        doc_rows=_rows("hi.md", "lo1.md", "lo2.md"),
                        doc_text_fn=lambda p: _TEXT[p])
    windowed = [g for g in groups if g.occurrences]
    assert len(windowed) == 1 and windowed[0].doc_id == "hi.md"   # surfaced doc windowed first
    assert len(groups) == 3                                       # every matching doc returned
    assert sum(g.total_matches for g in groups) == 6             # true total regardless of cap


def test_fts_only_doc_without_literal_match_is_dropped():
    # An FTS-universe doc whose text lacks the literal term (e.g. surfaced by synonym expansion)
    # and has no anchor hit does not inflate the literal count.
    groups = group_hits([], terms=["returnable"], doc_rows=_rows("x.md"),
                        doc_text_fn=lambda p: "no literal term here")
    assert groups == []


def test_legacy_hits_only_path_unchanged():
    # doc_rows=None keeps the pre-Track-A behavior (universe = surfaced hits, capped by max_docs).
    groups = group_hits([_hit("hi.md")], terms=["returnable"],
                        doc_text_fn=lambda p: _TEXT[p])
    assert [g.doc_id for g in groups] == ["hi.md"] and groups[0].total_matches == 2


# --- phrase-aware term builder -------------------------------------------------------------

def test_highlight_terms_multiword_is_one_phrase():
    from app.components.panels import _highlight_terms
    terms = _highlight_terms("net sales")
    assert "net sales" in terms and "net" not in terms and "sales" not in terms


def test_highlight_terms_single_word_and_stopword_only():
    from app.components.panels import _highlight_terms
    # The literal query term is always highlighted; with the analog dictionary loaded it may
    # also carry synonym phrases (Track B), so assert membership rather than exact equality.
    assert "returnable" in _highlight_terms("returnable")
    assert _highlight_terms("the of and") == []


def test_literal_query_is_not_repeated_in_curated_discovery_terms():
    from src.index.keyword_index import metric_synonym_phrases

    assert "fx" not in [term.casefold() for term in metric_synonym_phrases("fx")]


# --- unconditional literal scan: runs even when the retriever returns ZERO hits ------------

class _FakeCol:
    """A two-pane column stand-in — usable as a context manager (`with left:`)."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeSt:
    """Minimal Streamlit stand-in that records captions/warnings/markdowns for assertions."""

    def __init__(self):
        self.session_state: dict = {}
        self.captions: list = []
        self.warnings: list = []
        self.markdowns: list = []
        self.infos: list = []

    def caption(self, msg, *a, **k):
        self.captions.append(msg)

    def warning(self, msg, *a, **k):
        self.warnings.append(msg)

    def info(self, msg, *a, **k):
        self.infos.append(msg)

    def markdown(self, msg="", *a, **k):
        self.markdowns.append(msg)

    def divider(self, *a, **k):
        return None

    def toggle(self, *a, **k):
        return False

    def button(self, *a, **k):
        return False

    def columns(self, spec, *a, **k):
        n = len(spec) if hasattr(spec, "__len__") else int(spec)
        return [_FakeCol() for _ in range(n)]

    def expander(self, *a, **k):
        return nullcontext()


class _ZeroHitRetriever:
    """A retriever that ALWAYS returns zero hits — isolates the unconditional literal scan from
    the ranker: if the mentions view still reports the true corpus count, the scan is not gated on
    the retriever surfacing anything."""

    def __init__(self, store):
        self.store = store

    def search(self, *a, **k):
        return []


def test_render_search_rejects_midword_fragment_when_retriever_returns_zero_hits(built_index):
    from app.components.panels import _highlight_terms, render_search
    from src.search.filters import SearchFilters

    store, _ = built_index
    # "margi" is only a fragment of "margin" and must not become a suspicious exact mention.
    term = "margi"
    assert KeywordIndex(store).matching_documents(term, expand_synonyms=False) == []
    assert _highlight_terms(term) == [term]                       # no synonym dilution

    st = _FakeSt()
    render_search(st, _ZeroHitRetriever(store), query=term, filters=SearchFilters(), limit=20)

    assert st.warnings == ["No matches. Try broader terms or clear the sidebar filters."]
