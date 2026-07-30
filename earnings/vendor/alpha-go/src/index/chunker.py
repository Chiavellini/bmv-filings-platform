"""Chunker — split a parsed document's markdown into retrievable chunks.

Chunks are the unit of retrieval: a chunk carries enough text to be a meaningful search
hit and snippet, plus a char offset back into the source markdown so the doc viewer can
highlight the exact span. Tables (preserved by parse_pdf's layout mode) should chunk as
whole rows/blocks rather than be split mid-row.

Round-trip invariant (guaranteed): for every returned chunk,
``markdown[chunk.char_start:chunk.char_end] == chunk.text``. Chunks are exact contiguous
slices of the source — no normalization — so the doc viewer can highlight precisely.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Paragraph boundary: a blank line (optionally whitespace-only) between blocks.
_PARA_BOUNDARY = re.compile(r"\n[ \t]*\n")


@dataclass
class Chunk:
    """One retrievable unit of text from a document."""

    chunk_id: str        # f"{doc_id}#{ordinal}"
    doc_id: str
    ordinal: int         # position within the document
    text: str            # the chunk text (what gets indexed/embedded)
    char_start: int      # offset into the source markdown (for highlight)
    char_end: int
    heading: str | None = None   # nearest markdown heading, for context/snippet labels


def _paragraph_spans(markdown: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of non-empty paragraphs, in order.

    Spans cover the paragraph text only (boundary whitespace is excluded), but each span is
    still an exact slice of ``markdown``.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    n = len(markdown)
    for m in _PARA_BOUNDARY.finditer(markdown):
        seg = markdown[pos:m.start()]
        if seg.strip():
            spans.append((pos, m.start()))
        pos = m.end()
    if pos < n and markdown[pos:].strip():
        spans.append((pos, n))
    return spans


def _heading_before(markdown: str, offset: int) -> str | None:
    """Nearest markdown heading line (starts with '#') at or before ``offset``."""
    heading: str | None = None
    for line_match in re.finditer(r"(?m)^#{1,6}[ \t]+(.+)$", markdown):
        if line_match.start() <= offset:
            heading = line_match.group(1).strip()
        else:
            break
    return heading


def chunk_document(
    doc_id: str,
    markdown: str,
    *,
    target_chars: int = 1200,
    overlap_chars: int = 150,
) -> list[Chunk]:
    """Split ``markdown`` into heading-aware, paragraph-packed chunks with overlap.

    - Packs whole paragraphs up to ~``target_chars``; a single oversized paragraph is
      hard-split at ``target_chars``.
    - Adds ``overlap_chars`` of trailing context to the next chunk by snapping its start back
      to a paragraph boundary.
    - Each chunk's ``text`` is the exact slice ``markdown[char_start:char_end]``.
    """
    spans = _paragraph_spans(markdown)
    if not spans:
        return []

    # Flatten oversized paragraphs into <= target_chars pieces (still exact slices).
    pieces: list[tuple[int, int]] = []
    for start, end in spans:
        if end - start <= target_chars:
            pieces.append((start, end))
            continue
        cur = start
        while cur < end:
            pieces.append((cur, min(cur + target_chars, end)))
            cur += target_chars

    chunks: list[Chunk] = []
    ordinal = 0
    i = 0
    prev_start = -1
    while i < len(pieces):
        chunk_start = pieces[i][0]
        # Overlap: pull the window start back toward the previous chunk's tail.
        if chunks:
            target_back = chunks[-1].char_end - overlap_chars
            j = i
            while j > 0 and pieces[j - 1][0] >= target_back:
                j -= 1
            chunk_start = pieces[j][0]
        chunk_start = max(chunk_start, prev_start + 1)

        # Grow the window to ~target_chars worth of paragraphs.
        chunk_end = pieces[i][1]
        k = i
        while k + 1 < len(pieces) and pieces[k + 1][1] - chunk_start <= target_chars:
            k += 1
            chunk_end = pieces[k][1]

        text = markdown[chunk_start:chunk_end]
        chunks.append(Chunk(
            chunk_id=f"{doc_id}#{ordinal}",
            doc_id=doc_id,
            ordinal=ordinal,
            text=text,
            char_start=chunk_start,
            char_end=chunk_end,
            heading=_heading_before(markdown, chunk_start),
        ))
        ordinal += 1
        prev_start = chunk_start
        i = k + 1

    return chunks
