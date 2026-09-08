"""Render text with highlighted spans as safe HTML — shared by the search + doc panels.

Pure and unit-testable (no Streamlit import). Turns a piece of text plus ``(start, end)``
offset spans into HTML where the spanned regions are wrapped in ``<mark>`` and everything
else is HTML-escaped, so arbitrary corpus text can't inject markup.

A second, independent layer — ``tones`` — underlines sentence-level sentiment (green for
positive, red for negative) the way AlphaSense's transcript reader does. Underlines and
``<mark>`` highlights are separate visual channels: a match inside a negative sentence renders
as a yellow mark on a red-underlined run, never as one colour overwriting the other.
"""
from __future__ import annotations

import html


_ACTIVE_MARK_STYLE = "background:#ffb300;outline:1px solid #b26a00"

# Sentence-tone underline styles keyed by lexicon label. Neutral is deliberately absent: an
# underline under every sentence would carry no information.
TONE_STYLES = {
    "positive": "text-decoration:underline;text-decoration-color:#3fb950;"
                "text-decoration-thickness:2px;text-underline-offset:3px",
    "negative": "text-decoration:underline;text-decoration-color:#f85149;"
                "text-decoration-thickness:2px;text-underline-offset:3px",
}


def _merge(spans: "list[tuple[int, int]]", n: int) -> "list[tuple[int, int]]":
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
    return merged


def spans_to_html(
    text: str,
    spans: "list[tuple[int, int]]",
    *,
    newline_br: bool = True,
    active: "int | None" = None,
    tones: "list[tuple[int, int, str]] | None" = None,
) -> str:
    """Wrap each ``(start, end)`` region of ``text`` in ``<mark>``; escape the rest.

    Spans are clamped to the text bounds, sorted, and merged when they overlap, so callers
    needn't pre-clean them. Offsets are character indices into ``text``. When ``newline_br``,
    newlines in the (escaped) output become ``<br>`` for HTML rendering. ``active`` is an index
    into the merged span list; that span gets a visually distinct ``<mark>`` (the doc viewer's
    "current match").

    ``tones`` is an optional list of ``(start, end, label)`` sentence spans; runs labelled
    ``positive``/``negative`` are wrapped in an underlined ``<span>`` (see :data:`TONE_STYLES`).
    Tone spans never overlap each other (they come from a sentence splitter); a ``<mark>`` that
    straddles a tone boundary is split so the HTML stays well-nested.
    """
    n = len(text)
    merged = _merge(spans, n)

    def esc(chunk: str) -> str:
        out = html.escape(chunk)
        return out.replace("\n", "<br>") if newline_br else out

    if not tones:
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

    # Two layers → segment the text at every boundary of either layer, then emit each segment
    # with its (tone, mark) state. Tone spans are the outer element so a mark never straddles one.
    tone_runs = sorted(
        (max(0, min(s, n)), max(0, min(e, n)), label)
        for s, e, label in tones
        if label in TONE_STYLES and min(e, n) > max(0, s)
    )
    cuts = {0, n}
    for s, e in merged:
        cuts.update((s, e))
    for s, e, _ in tone_runs:
        cuts.update((s, e))
    points = sorted(cuts)

    def mark_index(pos: int) -> "int | None":
        for i, (s, e) in enumerate(merged):
            if s <= pos < e:
                return i
        return None

    def tone_at(pos: int) -> "str | None":
        for s, e, label in tone_runs:
            if s <= pos < e:
                return label
        return None

    out: list[str] = []
    open_tone: "str | None" = None
    for a, b in zip(points, points[1:]):
        if b <= a:
            continue
        tone = tone_at(a)
        if tone != open_tone:
            if open_tone is not None:
                out.append("</span>")
            if tone is not None:
                out.append(f"<span style='{TONE_STYLES[tone]}'>")
            open_tone = tone
        seg = esc(text[a:b])
        mi = mark_index(a)
        if mi is not None:
            attrs = f" style='{_ACTIVE_MARK_STYLE}'" if mi == active else ""
            out.append(f"<mark{attrs}>{seg}</mark>")
        else:
            out.append(seg)
    if open_tone is not None:
        out.append("</span>")
    return "".join(out)


def snippet_html(snippet) -> str:
    """Convenience: render a search ``Snippet`` (``.text`` + ``.spans``) as highlighted HTML."""
    return spans_to_html(snippet.text, list(snippet.spans))
