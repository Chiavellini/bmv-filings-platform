"""Locate every query-term match in a document — pure helpers for the doc viewer.

Streamlit cannot scroll-to-anchor inside an ``st.markdown`` HTML block, so the viewer's
"match n of N" navigation instead re-renders a window of text around the active match. These
helpers keep that logic pure and unit-testable (no Streamlit import), mirroring
``highlight.py``: character-offset spans, same overlap-merge discipline.

``find_matches`` is also the corpus-wide *occurrence counter* behind the linear mentions view:
  * **Phrase mode** — a multi-word term (e.g. ``"net sales"``) is matched as a contiguous
    phrase, NOT as ``net`` OR ``sales``, so its count equals what Ctrl+F reports. Inter-word
    whitespace is flexible (``\\s+``) because the corpus glues words across line breaks.
  * **Accent folding** — diacritics are folded on BOTH sides so the substring counter agrees
    with the FTS5 tokenizer (which folds accents when surfacing docs): a search for ``mexico``
    counts ``México``. The fold mirrors ``semantic_search.normalize_label``'s NFKD +
    combining-mark strip, but is applied per character so it is length- (offset-) preserving.

NOTE (known gap, out of scope here): scanned/image PDF pages carry no extractable text — the
parser emits a ``requiere OCR`` placeholder (see ``src/parse/parse_pdf.py``). Those pages are
therefore invisible to this literal scan and are a follow-up (OCR) item, not silently counted.
"""
from __future__ import annotations

import functools
import unicodedata


@functools.lru_cache(maxsize=2048)
def _fold_char(ch: str) -> str:
    """One-character, offset-safe accent fold (compatibility expansions stay unchanged)."""
    base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                   if not unicodedata.combining(c))
    return base if len(base) == 1 else ch


def _latin_translation() -> dict[int, str]:
    """Offset-safe translation table for Latin diacritics used by the corpus."""
    out: dict[int, str] = {}
    for lo, hi in ((0x00C0, 0x024F), (0x1E00, 0x1EFF)):
        for codepoint in range(lo, hi + 1):
            ch = chr(codepoint)
            base = _fold_char(ch)
            if base != ch and len(base) == 1:
                out[codepoint] = base
    return out


_LATIN_TRANSLATION = _latin_translation()


@functools.lru_cache(maxsize=1024)
def _fold_accents(s: str) -> str:
    """Diacritic-fold ``s`` length-preservingly (so match offsets stay valid).

    Same NFKD + combining-mark strip as ``semantic_search.normalize_label``, but per character:
    a precomposed accent (``é``→``e``, ``ñ``→``n``) decomposes to base+mark and yields exactly
    one base char, so offsets are preserved; any character whose fold is not a single char
    (e.g. a ligature) is kept as-is to keep length constant.
    """
    if s.isascii():
        return s
    # ``str.translate`` runs in C and every replacement is exactly one character, preserving all
    # source offsets. The LRU lets the exact/metric/analog bands reuse one folded document during
    # a search instead of normalizing the same 87 MB corpus three times.
    return s.translate(_LATIN_TRANSLATION)


def merge_spans(spans: "list[tuple[int, int]]") -> "list[tuple[int, int]]":
    """Sort spans and merge overlapping/touching ones (same rule as ``spans_to_html``)."""
    merged: list[tuple[int, int]] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def find_matches(text: str, terms: "list[str]", *,
                 fold_accents: bool = True) -> "list[tuple[int, int]]":
    """All case-insensitive ``(start, end)`` occurrences of ``terms`` in ``text``, merged.

    ``str.lower`` preserves offsets (the same trick ``make_snippet`` relies on) and ``_fold_accents``
    is length-preserving, so spans index into the original ``text``. Empty terms contribute nothing.

    Matching rules (precise lexical Command-F — see module docstring):
      * Every term must occupy a whole lexical word/phrase. This prevents cross-language false
        positives such as English ``stores`` inside Spanish ``gestores`` and ``unidades`` inside
        ``oportunidades``. Parser glue should be fixed at ingest instead of inflating mentions.
      * A **multi-word** term is a phrase: its words must appear contiguously, separated only
        by whitespace (``\\s+`` — tolerant of the corpus's mid-phrase line breaks).
      * Diacritics are folded on both text and terms when ``fold_accents`` (default) so the
        counter agrees with the accent-folding FTS surfacer.
    """
    if not any((term or "").strip() for term in terms):
        return []
    hay = text.lower()
    # The common Spanish/English diacritics that affect search equivalence. A consonant-only
    # acronym such as "FX" cannot gain an accent, so skip the corpus-sized translation entirely.
    accent_relevant = any(ch in "aeiouncy" for term in terms for ch in (term or "").lower())
    if fold_accents and accent_relevant:
        hay = _fold_accents(hay)
    spans: list[tuple[int, int]] = []
    for term in terms:
        t = (term or "").lower().strip()
        if fold_accents and accent_relevant:
            t = _fold_accents(t)
        if not t.strip():
            continue
        words = t.split()
        first = words[0]
        start = 0
        while True:
            i = hay.find(first, start)
            if i < 0:
                break
            end = i + len(first)
            matched = True
            for word in words[1:]:
                ws_start = end
                while end < len(hay) and hay[end].isspace():
                    end += 1
                if end == ws_start or not hay.startswith(word, end):
                    matched = False
                    break
                end += len(word)
            if matched:
                boundary_ok = ((i == 0 or not hay[i - 1].isalnum())
                               and (end >= len(hay) or not hay[end].isalnum()))
                if boundary_ok:
                    spans.append((i, end))
            # Match each term non-overlapping, like ``str.find``/Ctrl+F. Different terms are
            # unioned by ``merge_spans`` below.
            start = i + max(1, len(first))
    return merge_spans(spans)


def active_index(spans: "list[tuple[int, int]]", char_start: int) -> int:
    """Index of the span at/after ``char_start`` — where the viewer opens from a search hit.

    Falls back to the last span when the click lands past every match (can't happen when the
    clicked span itself is included, but keeps the helper total).
    """
    for i, (s, e) in enumerate(spans):
        if e > char_start:
            return i
    return max(0, len(spans) - 1)


def window_view(
    text: str,
    spans: "list[tuple[int, int]]",
    active: int,
    *,
    before: int = 2000,
    after: int = 4000,
) -> "tuple[str, list[tuple[int, int]], int | None, int]":
    """Slice a window of ``text`` around the active span, shifting spans into window offsets.

    Returns ``(window_text, window_spans, window_active, window_start)`` where ``window_spans``
    are the input spans clipped to the window and re-based so
    ``window_text[s:e] == text[s + window_start : e + window_start]``, and ``window_active`` is
    the active span's index within ``window_spans`` (``None`` if there are no spans).
    """
    if not spans:
        return text[:before + after], [], None, 0

    active = max(0, min(active, len(spans) - 1))
    a_start, a_end = spans[active]
    win_start = max(0, a_start - before)
    win_end = min(len(text), a_end + after)

    window_spans: list[tuple[int, int]] = []
    window_active: int | None = None
    for i, (s, e) in enumerate(spans):
        cs, ce = max(s, win_start), min(e, win_end)
        if ce <= cs:
            continue
        if i == active:
            window_active = len(window_spans)
        window_spans.append((cs - win_start, ce - win_start))
    return text[win_start:win_end], window_spans, window_active, win_start
