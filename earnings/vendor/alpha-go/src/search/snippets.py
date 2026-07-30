"""Snippets — build a highlighted preview around the query match within a chunk.

A ``Snippet`` carries the windowed text plus the highlight spans (relative to that window)
so both the results list and the full doc viewer can render the same highlights. Windows are
generous (~900 chars) and snap to whitespace so a result reads as a complete passage, and the
``truncated_*`` flags tell the renderer whether ellipses are warranted.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Snippet:
    text: str                                   # the windowed preview text
    spans: list[tuple[int, int]] = field(default_factory=list)  # highlight offsets within `text`
    char_start: int = 0                         # offset of `text` within the source chunk/doc
    truncated_start: bool = False               # text was cut before the window
    truncated_end: bool = False                 # text was cut after the window


# How far a window edge may move to reach a whitespace boundary. Beyond this (e.g. a huge
# unbroken table row) we cut hard rather than degenerate to a tiny window.
_SNAP_LIMIT = 40


def _snap_start(text: str, pos: int) -> int:
    """Move ``pos`` forward to the next whitespace boundary so the window never starts mid-word."""
    if pos <= 0:
        return 0
    limit = min(len(text), pos + _SNAP_LIMIT)
    p = pos
    while p < limit and not text[p - 1].isspace():
        p += 1
    return p if p < limit or (p > 0 and text[p - 1].isspace()) else pos


def _snap_end(text: str, pos: int) -> int:
    """Move ``pos`` back to the previous whitespace boundary so the window never ends mid-word."""
    if pos >= len(text):
        return len(text)
    limit = max(0, pos - _SNAP_LIMIT)
    p = pos
    while p > limit and not text[p].isspace():
        p -= 1
    return p if text[p].isspace() else pos


def expand_passage(
    doc_text: str,
    chunk_start: int,
    chunk_end: int,
    *,
    before: int = 200,
    after: int = 700,
) -> Snippet:
    """A display passage from the SOURCE DOCUMENT around a chunk's location.

    Chunks are sliced with overlap and can begin/end mid-word or mid-sentence; for a readable
    result card we re-read the surrounding document text instead: the chunk's span plus some
    context, snapped to whitespace. Highlight spans are NOT computed here (the caller matches
    query terms against the returned text).
    """
    lo = _snap_start(doc_text, max(0, chunk_start - before))
    hi = _snap_end(doc_text, min(len(doc_text), chunk_end + after))
    return Snippet(
        text=doc_text[lo:hi],
        spans=[],
        char_start=lo,
        truncated_start=lo > 0,
        truncated_end=hi < len(doc_text),
    )


def make_snippet(text: str, query_terms: list[str], *, window: int = 900) -> Snippet:
    """Find the best-matching window in ``text`` for ``query_terms`` and mark highlight spans.

    Matching is case-insensitive (``str.lower`` preserves offsets). The window is anchored a
    little before the first matched term, sized ``window`` chars, and snapped to whitespace at
    both cut edges; highlight ``spans`` are ``(start, end)`` offsets *within the returned window
    text* and are merged when they overlap. ``char_start`` records where the window begins
    inside ``text`` so callers can map back to the source chunk/doc. When no term matches, the
    head of the text is returned with no spans.
    """
    if not text:
        return Snippet(text="", spans=[], char_start=0)

    low = text.lower()
    terms = [t.lower() for t in query_terms if t]

    first: int | None = None
    for t in terms:
        i = low.find(t)
        if i != -1 and (first is None or i < first):
            first = i

    anchor = 0 if first is None else max(0, first - window // 4)
    start = _snap_start(text, anchor)
    end = _snap_end(text, min(len(text), start + window))
    snippet_text = text[start:end]

    spans: list[tuple[int, int]] = []
    if first is not None:
        low_win = snippet_text.lower()
        for t in terms:
            i = low_win.find(t)
            while i != -1:
                spans.append((i, i + len(t)))
                i = low_win.find(t, i + len(t))
        spans.sort()

    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    return Snippet(
        text=snippet_text,
        spans=merged,
        char_start=start,
        truncated_start=start > 0,
        truncated_end=end < len(text),
    )
