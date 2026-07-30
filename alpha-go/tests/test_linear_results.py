"""Linear occurrence view — per-document grouping, document-order windows, chronological sort."""
from __future__ import annotations

from types import SimpleNamespace

from src.search.snippets import Snippet

from app.components.doc_matches import find_matches
from app.components.linear_results import (DocGroup, doc_sort_key, group_hits,
                                           match_ranges, occurrence_snippets, sort_groups)


def _hit(doc_id="d1", company="walmex", period="2023Q1", markdown_path="p.md",
         char_start=0, char_end=6, snippet=None):
    return SimpleNamespace(
        doc_id=doc_id, company=company, period=period, doc_type="quarterly_report",
        title=f"{company} {period}", markdown_path=markdown_path,
        char_start=char_start, char_end=char_end,
        snippet=snippet or Snippet(text="chunk fallback", spans=[(0, 5)]),
    )


# --- occurrence_snippets -------------------------------------------------------------------

def test_isolated_occurrences_yield_ordered_windows():
    text = ("margin pressure eased. " + "x" * 500
            + " later the margin recovered. " + "y" * 500 + " end.")
    spans = find_matches(text, ["margin"])
    occs = occurrence_snippets(text, spans)

    assert len(occs) == 2
    assert [o.spans[0] for o in occs] == spans          # document order preserved
    for occ in occs:
        for s, e in occ.snippet.spans:                  # re-based spans round-trip
            assert occ.snippet.text[s:e].lower() == "margin"


def test_nearby_occurrences_merge_into_one_window():
    text = "the margin and the margin again" + " z" * 300
    spans = find_matches(text, ["margin"])
    occs = occurrence_snippets(text, spans)

    assert len(occs) == 1
    assert occs[0].spans == spans                       # both matches, one snippet
    assert len(occs[0].snippet.spans) == 2


def test_truncation_flags_at_document_edges():
    text = "margin at the head " + "x" * 800 + " and margin at the tail"
    occs = occurrence_snippets(text, find_matches(text, ["margin"]))

    assert occs[0].snippet.truncated_start is False
    assert occs[0].snippet.truncated_end is True
    assert occs[-1].snippet.truncated_start is True
    assert occs[-1].snippet.truncated_end is False


def test_windows_snap_to_whitespace():
    text = "aaaa " * 100 + "margin" + " bbbb" * 100
    occs = occurrence_snippets(text, find_matches(text, ["margin"]))
    snip = occs[0].snippet.text
    assert not snip.startswith("aaa ")                  # no mid-word cut at either edge
    assert text[occs[0].snippet.char_start - 1].isspace()


# --- group_hits ----------------------------------------------------------------------------

def test_chunks_of_same_doc_collapse_and_enumerate_all_occurrences():
    text = "margin one. " + "x" * 900 + " margin two. " + "y" * 900 + " margin three."
    hits = [_hit(doc_id="d1", char_start=0, char_end=6),
            _hit(doc_id="d1", char_start=950, char_end=956)]
    groups = group_hits(hits, terms=["margin"], doc_text_fn=lambda p: text)

    assert len(groups) == 1
    assert groups[0].total_matches == 3                 # full-doc scan beats chunk hits
    starts = [occ.spans[0][0] for occ in groups[0].occurrences]
    assert starts == sorted(starts)


def test_groups_sort_chronologically_with_undated_last():
    hits = [_hit(doc_id="d1", company="walmex", period="2024Q2", markdown_path="a.md"),
            _hit(doc_id="d2", company="bimbo", period="2023Q4", markdown_path="b.md"),
            _hit(doc_id="d3", company="walmex", period=None, markdown_path="c.md"),
            _hit(doc_id="d4", company="walmex", period="2019Q1", markdown_path="d.md")]
    groups = group_hits(hits, terms=["margin"], doc_text_fn=lambda p: "a margin here")

    assert [(g.company, g.period) for g in groups] == [
        ("bimbo", "2023Q4"), ("walmex", "2019Q1"), ("walmex", "2024Q2"), ("walmex", None)]


def test_max_docs_keeps_first_n_by_relevance_not_chronology():
    hits = [_hit(doc_id="d1", period="2024Q4"),        # most relevant, latest period
            _hit(doc_id="d2", period="2019Q1"),
            _hit(doc_id="d3", period="2018Q1")]        # least relevant, earliest period
    groups = group_hits(hits, terms=["margin"], max_docs=2,
                        doc_text_fn=lambda p: "the margin line")

    assert {g.doc_id for g in groups} == {"d1", "d2"}   # relevance picked the docs
    assert [g.period for g in groups] == ["2019Q1", "2024Q4"]   # chronology ordered them


def test_unreadable_doc_falls_back_to_chunk_snippet():
    snippet = Snippet(text="from the chunk", spans=[(0, 4)])
    hits = [_hit(char_start=10, char_end=16, snippet=snippet)]
    groups = group_hits(hits, terms=["margin"], doc_text_fn=lambda p: "")

    assert groups[0].total_matches == 1
    assert groups[0].occurrences[0].snippet is snippet
    assert groups[0].occurrences[0].spans == [(10, 16)]


def test_no_substring_match_falls_back_to_anchor_span():
    hits = [_hit(char_start=4, char_end=10)]
    groups = group_hits(hits, terms=["absent phrase"],
                        doc_text_fn=lambda p: "the anchor text stands in")

    assert groups[0].total_matches == 1
    assert groups[0].occurrences[0].spans == [(4, 10)]


def test_max_per_doc_caps_windows_but_counts_all():
    text = " ".join(["margin"] * 40)
    groups = group_hits([_hit()], terms=["margin"], max_per_doc=5,
                        doc_text_fn=lambda p: text)

    g = groups[0]
    assert g.total_matches == 40
    assert sum(len(o.spans) for o in g.occurrences) == 5


def test_match_ranges_number_by_match_not_window():
    text = "the margin and the margin again" + " z" * 300 + " final margin here"
    occs = occurrence_snippets(text, find_matches(text, ["margin"]))

    assert len(occs) == 2                               # first two matches merged
    assert match_ranges(occs) == [(1, 2), (3, 3)]       # ranges cover all 3 matches


def test_dense_matches_split_at_max_window():
    text = ("margin " * 400).strip()                    # a match every 7 chars, ~2800 chars
    occs = occurrence_snippets(text, find_matches(text, ["margin"]), max_window=1200)

    assert len(occs) > 1                                # chained merge is bounded
    assert all(len(o.snippet.text) <= 1200 + 40 for o in occs)   # + snap slack
    covered = [s for o in occs for s in o.spans]
    assert covered == sorted(covered)                   # nothing lost, order kept


def _group(company, period, path="p.md"):
    return DocGroup(doc_id=path, company=company, period=period, doc_type="q",
                    title="t", markdown_path=path)


def test_sort_groups_newest_first_flips_periods_within_company():
    groups = [_group("bimbo", "2020-1T", "a.md"), _group("bimbo", "2024-1T", "b.md"),
              _group("walmex", None, "c.md"), _group("walmex", "2019-1T", "d.md")]
    newest = sort_groups(groups, newest_first=True)

    assert [(g.company, g.period) for g in newest] == [
        ("bimbo", "2024-1T"), ("bimbo", "2020-1T"), ("walmex", "2019-1T"), ("walmex", None)]
    oldest = sort_groups(groups, newest_first=False)
    assert [(g.company, g.period) for g in oldest] == [
        ("bimbo", "2020-1T"), ("bimbo", "2024-1T"), ("walmex", "2019-1T"), ("walmex", None)]


def test_short_terms_require_word_boundaries():
    text = "profitability with it it-self bit"
    spans = find_matches(text, ["it"])
    matched = [text[s:e] for s, e in spans]

    assert matched == ["it", "it"]                      # standalone + hyphenated, not inside words
    assert find_matches("andthemargin", ["margin"]) == []   # no cross-word false positives


def test_accent_folding_preserves_offsets_with_spanish_and_ligatures():
    text = "México y presión; oﬃce margin."
    spans = find_matches(text, ["mexico", "presion", "margin"])
    assert [text[s:e] for s, e in spans] == ["México", "presión", "margin"]


def test_doc_sort_key_is_stable_on_path():
    a = DocGroup(doc_id="1", company="c", period="2020Q1", doc_type="q", title="t",
                 markdown_path="a.md")
    b = DocGroup(doc_id="2", company="c", period="2020Q1", doc_type="q", title="t",
                 markdown_path="b.md")
    assert doc_sort_key(a) < doc_sort_key(b)
