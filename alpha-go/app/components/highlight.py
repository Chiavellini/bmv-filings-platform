"""Render text with highlighted spans as safe HTML — shared by the search + doc panels.

Pure and unit-testable (no Streamlit import). Turns a piece of text plus ``(start, end)``
offset spans into HTML where the spanned regions are wrapped in ``<mark>`` and everything
else is HTML-escaped, so arbitrary corpus text can't inject markup.
"""
from __future__ import annotations

import html


_ACTIVE_MARK_STYLE = "background:#ffb300;outline:1px solid #b26a00"


def spans_to_html(
    text: str,
    spans: "list[tuple[int, int]]",
    *,
    newline_br: bool = True,
    active: "int | None" = None,
) -> str:
    """Wrap each ``(start, end)`` region of ``text`` in ``<mark>``; escape the rest.

    Spans are clamped to the text bounds, sorted, and merged when they overlap, so callers
    needn't pre-clean them. Offsets are character indices into ``text``. When ``newline_br``,
    newlines in the (escaped) output become ``<br>`` for HTML rendering. ``active`` is an index
    into the merged span list; that span gets a visually distinct ``<mark>`` (the doc viewer's
    "current match").
    """
    n = len(text)
    clean: list[tuple[int, int]] = []
    for s, e in spans:
        s, e = max(0, min(s, n)), max(0, min(e, n))
        if e > s:
            clean.append((s, e))
    clean.sort()

    merged: list[tuple[int, int]] = []
    for s, e in clean:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    def esc(chunk: str) -> str:
        out = html.escape(chunk)
        return out.replace("\n", "<br>") if newline_br else out

    parts: list[str] = []
    cursor = 0
    for i, (s, e) in enumerate(merged):
        if s > cursor:
            parts.append(esc(text[cursor:s]))
        attrs = f" style='{_ACTIVE_MARK_STYLE}'" if i == active else ""
        parts.append(f"<mark{attrs}>{esc(text[s:e])}</mark>")
        cursor = e
    if cursor < n:
        parts.append(esc(text[cursor:]))
    return "".join(parts)


def snippet_html(snippet) -> str:
    """Convenience: render a search ``Snippet`` (``.text`` + ``.spans``) as highlighted HTML."""
    return spans_to_html(snippet.text, list(snippet.spans))
