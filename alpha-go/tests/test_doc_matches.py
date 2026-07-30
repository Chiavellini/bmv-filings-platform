"""Doc-viewer match navigation — pure helpers (find/merge/window) + active-span styling."""
from __future__ import annotations

from app.components.doc_matches import active_index, find_matches, merge_spans, window_view
from app.components.highlight import spans_to_html


def test_find_matches_case_insensitive_all_occurrences():
    text = "Revenue grew. REVENUE margin fell. revenue!"
    spans = find_matches(text, ["revenue"])
    assert [text[s:e].lower() for s, e in spans] == ["revenue"] * 3
    assert spans == sorted(spans)


def test_find_matches_merges_overlaps():
    text = "operating margin expanded"
    spans = find_matches(text, ["operating margin", "margin"])
    assert spans == [(0, 16)]  # "margin" is swallowed by the longer overlapping match


def test_find_matches_ignores_empty_terms():
    assert find_matches("some text", ["", None if False else ""]) == []


def test_merge_spans_same_discipline_as_highlight():
    assert merge_spans([(5, 10), (0, 6), (20, 25)]) == [(0, 10), (20, 25)]


def test_active_index_lands_on_clicked_span():
    spans = [(0, 5), (10, 15), (30, 40)]
    assert active_index(spans, 10) == 1
    assert active_index(spans, 12) == 1   # inside the span
    assert active_index(spans, 99) == 2   # past everything → last


def test_window_view_offsets_round_trip():
    text = "x" * 5000 + " TARGET " + "y" * 5000
    spans = find_matches(text, ["target"])
    win_text, win_spans, win_active, win_start = window_view(text, spans, 0,
                                                             before=100, after=200)
    assert win_active == 0
    s, e = win_spans[0]
    # exact offset discipline: window span maps back to the source text
    assert win_text[s:e] == text[s + win_start:e + win_start] == "TARGET"
    assert len(win_text) <= 100 + 200 + len("TARGET")


def test_window_view_drops_out_of_window_spans():
    text = "a" * 100 + " targ " + "b" * 5000 + " targ " + "c" * 100
    spans = find_matches(text, ["targ"])
    assert len(spans) == 2
    win_text, win_spans, win_active, _ = window_view(text, spans, 0, before=50, after=50)
    assert len(win_spans) == 1 and win_active == 0


def test_window_view_no_spans():
    win_text, win_spans, win_active, win_start = window_view("hello world", [], 0)
    assert win_text == "hello world" and win_spans == [] and win_active is None
    assert win_start == 0


def test_spans_to_html_active_span_distinct():
    html = spans_to_html("aa bb cc", [(0, 2), (3, 5)], active=1)
    assert html.count("<mark") == 2
    assert "<mark>aa</mark>" in html
    assert "<mark style=" in html and ">bb</mark>" in html


def test_spans_to_html_no_active_unchanged():
    assert spans_to_html("aa bb", [(0, 2)]) == "<mark>aa</mark> bb"


# --- phrase mode (Track A) -----------------------------------------------------------------

def test_phrase_matches_contiguous_not_or():
    text = "Net sales rose, but net income fell while sales lagged."
    spans = find_matches(text, ["net sales"])
    assert [text[s:e].lower() for s, e in spans] == ["net sales"]   # the phrase, not net OR sales


def test_phrase_tolerates_interword_whitespace_and_newlines():
    text = "the net\n   sales line"                 # phrase split across a line break
    spans = find_matches(text, ["net sales"])
    assert len(spans) == 1 and text[spans[0][0]:spans[0][1]] == "net\n   sales"


def test_phrase_with_interior_stopword():
    text = "a return of capital was declared"
    spans = find_matches(text, ["return of capital"])
    assert [text[s:e] for s, e in spans] == ["return of capital"]


# --- accent folding (Track A) --------------------------------------------------------------

def test_accents_folded_on_both_sides():
    text = "Ventas en México y en Mexico crecieron"
    # unaccented query term counts the accented occurrence too (FTS folds accents when surfacing)
    assert [text[s:e] for s, e in find_matches(text, ["mexico"])] == ["México", "Mexico"]
    # and an accented query term still finds the unaccented occurrence
    assert len(find_matches(text, ["méxico"])) == 2


def test_accent_folding_preserves_offsets():
    text = "un café, otro café, y un café"          # precomposed accents, separated
    spans = find_matches(text, ["cafe"])
    assert len(spans) == 3
    for s, e in spans:
        assert text[s:e] == "café"                   # offsets index the ORIGINAL accented text


def test_accent_folding_can_be_disabled():
    text = "México"
    assert find_matches(text, ["mexico"], fold_accents=False) == []
    assert find_matches(text, ["mexico"], fold_accents=True)


def test_short_term_boundary_rule_still_holds_after_folding():
    # ≤3-char rule preserved: "año" (3 chars) folds to "ano" and must sit on a word boundary.
    text = "el año pasado, no el planoaño"
    spans = find_matches(text, ["ano"])
    assert [text[s:e] for s, e in spans] == ["año"]  # standalone only, not inside "planoaño"


def test_curated_expansion_can_require_whole_words():
    text = "Aprovechamos oportunidades y abrimos nuevas unidades."
    # Neither a literal search nor a synonym expansion may manufacture a cross-word match.
    assert len(find_matches(text, ["unidades"])) == 1
    spans = find_matches(text, ["unidades"])
    assert [text[s:e] for s, e in spans] == ["unidades"]
