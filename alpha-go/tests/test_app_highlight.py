"""Tests for the highlight helper (Phase 4)."""
from __future__ import annotations

from app.components.highlight import snippet_html, spans_to_html
from src.search.snippets import make_snippet


def test_spans_become_mark():
    html = spans_to_html("pricing pressure here", [(0, 7)])
    assert "<mark>pricing</mark>" in html
    assert "pressure here" in html


def test_surrounding_text_is_escaped():
    # Angle brackets in corpus text must not inject markup.
    html = spans_to_html("a <b>x</b> z", [(0, 1)])
    assert "<b>" not in html
    assert "&lt;b&gt;" in html
    assert html.startswith("<mark>a</mark>")


def test_overlapping_spans_merge():
    html = spans_to_html("EBITDA", [(0, 6), (0, 4)])
    assert html == "<mark>EBITDA</mark>"


def test_out_of_range_spans_are_clamped():
    html = spans_to_html("hi", [(0, 99)])
    assert html == "<mark>hi</mark>"


def test_no_spans_passthrough_escaped():
    assert spans_to_html("plain & simple", []) == "plain &amp; simple"


def test_snippet_html_integration():
    snip = make_snippet("Management cited pricing pressure.", ["pricing"], window=80)
    html = snippet_html(snip)
    assert "<mark>pricing</mark>" in html


# --- sentence-tone underlines (second, independent visual channel) ---------------------------

def test_tone_spans_underline_and_nest_marks_inside():
    text = "Sales grew. Costs hurt."
    html = spans_to_html(text, [(0, 5)], tones=[(0, 11, "positive"), (12, 23, "negative")])
    assert html.startswith("<span style='text-decoration:underline;text-decoration-color:#3fb950")
    assert "<mark>Sales</mark> grew.</span>" in html
    assert "text-decoration-color:#f85149" in html and html.endswith("Costs hurt.</span>")
    assert html.count("<span") == html.count("</span>") == 2


def test_mark_straddling_a_tone_boundary_is_split_not_broken():
    text = "ab cd"
    html = spans_to_html(text, [(1, 4)], tones=[(0, 2, "negative")])
    # the mark [1,4) crosses the tone end at 2 → two marks, well-nested
    assert html == ("<span style='" + __import__("app.components.highlight", fromlist=["TONE_STYLES"])
                    .TONE_STYLES["negative"] + "'>a<mark>b</mark></span><mark> c</mark>d")


def test_neutral_tones_and_none_leave_output_unchanged():
    text = "plain & simple"
    assert spans_to_html(text, [(0, 5)], tones=[(0, 14, "neutral")]) == spans_to_html(text, [(0, 5)])
    assert spans_to_html(text, [(0, 5)], tones=[]) == spans_to_html(text, [(0, 5)])


def test_active_mark_kept_with_tones():
    html = spans_to_html("x y", [(0, 1), (2, 3)], active=1, tones=[(0, 3, "positive")])
    assert html.count("background:#ffb300") == 1 and "<mark style=" in html
