"""Tests for make_snippet (Phase 3)."""
from __future__ import annotations

from src.search.snippets import expand_passage, make_snippet


def test_expand_passage_reads_complete_context():
    doc = " ".join(f"tok{i}" for i in range(500))
    # Pretend a chunk covers tok100..tok140 (find their offsets).
    start = doc.index("tok100")
    end = doc.index("tok140") + len("tok140")
    p = expand_passage(doc, start, end, before=50, after=120)
    assert "tok100" in p.text and "tok140" in p.text
    assert p.truncated_start and p.truncated_end
    assert not p.text.startswith(" ") and not p.text.endswith(" ")
    assert doc[p.char_start:p.char_start + len(p.text)] == p.text


def test_expand_passage_clamps_to_document():
    doc = "short document text"
    p = expand_passage(doc, 0, len(doc), before=100, after=100)
    assert p.text == doc
    assert not p.truncated_start and not p.truncated_end


def test_snippet_highlights_match_and_offsets_are_valid():
    text = "Management highlighted pricing pressure from e-commerce competitors in Mexico."
    snip = make_snippet(text, ["pricing", "pressure"], window=80)
    # Every span lands on the query term within the returned window text.
    for s, e in snip.spans:
        assert snip.text[s:e].lower() in {"pricing", "pressure"}
    assert snip.spans, "expected at least one highlight span"
    # char_start maps the window back into the source text.
    assert text[snip.char_start:snip.char_start + len(snip.text)] == snip.text


def test_snippet_no_match_returns_head_without_spans():
    text = "EBITDA finalized at 230.0 with a margin of 18.4%."
    snip = make_snippet(text, ["nonexistentterm"], window=20)
    assert snip.spans == []
    # Head of the text, snapped back to a whitespace boundary (never cut mid-word).
    assert snip.text == "EBITDA finalized at"
    assert snip.char_start == 0
    assert not snip.truncated_start and snip.truncated_end


def test_snippet_edges_never_cut_words():
    words = " ".join(f"word{i}" for i in range(400))
    snip = make_snippet(words, ["word200"], window=300)
    assert snip.truncated_start and snip.truncated_end
    # Both edges land on whole words.
    assert not snip.text.startswith(" ") and not snip.text.endswith(" ")
    assert snip.text.split()[0].startswith("word")
    assert snip.text.split()[-1].startswith("word")
    # Offset discipline holds after snapping.
    assert words[snip.char_start:snip.char_start + len(snip.text)] == snip.text


def test_snippet_default_window_is_generous():
    text = "revenue " + "x" * 2000
    snip = make_snippet(text, ["revenue"])
    assert len(snip.text) >= 800


def test_snippet_untruncated_when_text_fits():
    text = "Revenue grew nicely."
    snip = make_snippet(text, ["revenue"])
    assert snip.text == text
    assert not snip.truncated_start and not snip.truncated_end


def test_snippet_merges_overlapping_spans():
    # Overlapping terms ('ebit' inside 'ebitda') must merge, not double-count.
    text = "EBITDA grew."
    snip = make_snippet(text, ["ebitda", "ebit"], window=80)
    assert snip.spans == [(0, 6)]


def test_snippet_empty_text():
    snip = make_snippet("", ["x"])
    assert snip.text == "" and snip.spans == []
