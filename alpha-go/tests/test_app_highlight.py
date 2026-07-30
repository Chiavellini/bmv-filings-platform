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
