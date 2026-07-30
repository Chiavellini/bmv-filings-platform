"""Real-PDF hit viewer — the AlphaSense-style in-app document reader.

Search results carry character offsets into the parsed *markdown*, not the PDF. This module
bridges that gap at render time so a hit can be shown ON the original document:

  1. recover the physical PDF page from the parser's ``===== Página N =====`` markers
     (``src/parse/parse_pdf.py`` writes one before each page's text, 1:1 with physical pages);
  2. locate the found phrase on that page via a case-insensitive text search on the real PDF
     (PyMuPDF ``page.search_for`` — robust to the parser's whitespace/case normalisation);
  3. embed the whole PDF inline (``streamlit-pdf-viewer``) with every hit highlighted, scrolled
     to the first — no new tab.

The char offset only *picks the page*; the on-PDF search finds the highlight box, so we never
need a markdown→PDF coordinate map. Docs with no usable PDF (HTML-sourced MD&A, or a pdf_path
that isn't on disk) return ``False`` from :func:`render` so the caller can fall back to the
markdown context view. The search core (retriever/store/linear_results) is untouched — the
PDF path is looked up from the corpus manifest here, at render time.
"""
from __future__ import annotations

import functools
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

_PAGE_MARKER = re.compile(r"===== Página (\d+) =====")
_HIGHLIGHT = "#f5c518"          # AlphaSense-yellow highlight box
_MENTION_HIGHLIGHTS = {
    "positive": "#dcfce7",     # light green
    "neutral": "#fef3c7",      # light yellow
    "negative": "#fee2e2",     # light red
}
_MAX_PHRASE_CHARS = 80          # search_for matches on-line text; a long span won't be found whole
_CROP_SCALE = 1.5               # crisp enough for an evidence card without oversized PNGs
_CROP_MARGIN = 42               # PDF points around the located mention
_MIN_CROP_WIDTH = 210
_MIN_CROP_HEIGHT = 130


@dataclass(frozen=True)
class MentionPreview:
    """A source-verifiable visual excerpt around one located PDF mention."""

    png: bytes
    page: int
    highlights: int
    filename: str


@functools.lru_cache(maxsize=1)
def _manifest_pdf_paths() -> "dict[str, str]":
    """``doc_id -> pdf_path`` from the corpus manifest (loaded once per process)."""
    root = Path(__file__).resolve().parents[2]        # .../alpha-go
    try:
        docs = json.loads((root / "data" / "corpus" / "manifest.json").read_text(
            encoding="utf-8"))["documents"]
    except (OSError, ValueError, KeyError):
        return {}
    out = {}
    for d in docs:
        raw = (d.get("original_path") if d.get("original_format") == "pdf" else None)
        raw = raw or d.get("pdf_path") or (d.get("source_path") if d.get("source_format") == "pdf" else None)
        if not raw:
            continue
        p = Path(raw)
        if p.is_absolute():
            resolved = p
        else:
            candidates = (p, root / p, root / "data" / "corpus" / p)
            resolved = next((candidate for candidate in candidates if candidate.exists()), root / p)
        out[d["doc_id"]] = str(resolved)
    return out


def pdf_path_for(doc_id: str) -> "str | None":
    """On-disk PDF for a document, or ``None`` (no pdf_path, or the file is not present).

    The manifest stores absolute paths into the sibling ``data/reports`` tree; guard on
    ``os.path.exists`` so a moved/missing file degrades to the markdown viewer instead of crashing.
    """
    p = _manifest_pdf_paths().get(doc_id)
    return p if p and os.path.exists(p) else None


def invalidate_manifest_cache() -> None:
    """Refresh source-path discovery after a live dashboard upload.

    The manifest lookup is intentionally cached for normal result rendering, but an uploaded PDF
    mutates that manifest while Streamlit stays alive. Without explicit invalidation the new file
    is searchable immediately yet its original appears unavailable until the process restarts.
    """
    _manifest_pdf_paths.cache_clear()


def page_for_offset(markdown_text: str, char_start: int) -> "int | None":
    """Physical PDF page (1-based) for a markdown offset, via the parser's page markers.

    The nearest ``===== Página N =====`` marker at or before ``char_start`` names the page.
    Returns ``None`` when the markdown carries no markers (HTML-sourced docs).
    """
    page = None
    for m in _PAGE_MARKER.finditer(markdown_text, 0, max(0, char_start) + 1):
        page = int(m.group(1))
    return page


def _clip(phrase: str) -> str:
    """Normalise a found phrase for on-PDF search (collapse whitespace, cap length)."""
    return re.sub(r"\s+", " ", phrase or "").strip()[:_MAX_PHRASE_CHARS]


def highlight_color(sentiment: str) -> str:
    """Light, readable PDF-marker tint for one independently scored mention."""
    return _MENTION_HIGHLIGHTS.get(sentiment, _MENTION_HIGHLIGHTS["neutral"])


def _rgb(color: str) -> "tuple[float, float, float]":
    """Convert a CSS ``#rrggbb`` tint into the RGB floats required by PyMuPDF."""
    value = color.lstrip("#")
    if len(value) != 6:
        value = _MENTION_HIGHLIGHTS["neutral"].lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


@functools.lru_cache(maxsize=128)
def _page_count(pdf_path: str) -> int:
    """Total pages in the PDF (cached); 0 on any error."""
    import fitz

    try:
        with fitz.open(pdf_path) as doc:
            return doc.page_count
    except Exception:
        return 0


@functools.lru_cache(maxsize=256)
def _search_page(pdf_path: str, page_no: int, phrase: str) -> "tuple[tuple[float, float, float, float], ...]":
    """Highlight rects ``(x, y, w, h)`` in PDF points for a phrase on one page (cached).

    ``search_for`` is case-insensitive and tolerant of the parser's text tidying. A phrase the
    layout wraps across lines won't match whole; fall back to its longest word so the hit is
    still boxed. Any PDF error degrades to no box (the page is still shown/scrolled to).
    """
    import fitz

    try:
        with fitz.open(pdf_path) as doc:
            if not 1 <= page_no <= doc.page_count:
                return ()
            page = doc[page_no - 1]
            rects = page.search_for(phrase)
            if not rects and " " in phrase:
                word = max(phrase.split(), key=len)
                rects = page.search_for(word) if len(word) >= 3 else []
            return tuple((r.x0, r.y0, r.width, r.height) for r in rects)
    except Exception:
        return ()


def _annotations(pdf_path: str, offset_pages: "list[tuple[int, list[str], str]]"):
    """Build viewer annotations + the scroll-to page from ``[(page, [phrases], color)]`` in order.

    Coordinates map 1:1 — PyMuPDF rects and streamlit-pdf-viewer annotations are both in PDF
    points with a top-left origin. Each entry carries its own box color (sentiment of the mention).
    Scroll target is the first page that actually got a highlight, else the first hit page.
    """
    annotations: list[dict] = []
    for page_no, phrases, color in offset_pages:
        for ph in phrases:
            for (x, y, w, h) in _search_page(pdf_path, page_no, ph):
                annotations.append({"page": page_no, "x": x, "y": y,
                                    "width": w, "height": h, "color": color})
    scroll_page = (annotations[0]["page"] if annotations
                   else offset_pages[0][0] if offset_pages else 1)
    return annotations, scroll_page


def _crop_rect(page_rect, hit_rects):
    """Bounded, legible page region around one or more hit rectangles."""
    import fitz

    target = hit_rects[0]
    for rect in hit_rects[1:]:
        target |= rect
    # Give a short line of context on each side.  Minimum dimensions stop a one-word hit from
    # becoming an unreadably narrow strip, while intersection keeps the crop on the real page.
    target = fitz.Rect(
        target.x0 - _CROP_MARGIN, target.y0 - _CROP_MARGIN,
        target.x1 + _CROP_MARGIN, target.y1 + _CROP_MARGIN,
    )
    if target.width < _MIN_CROP_WIDTH:
        pad = (_MIN_CROP_WIDTH - target.width) / 2
        target.x0 -= pad
        target.x1 += pad
    if target.height < _MIN_CROP_HEIGHT:
        pad = (_MIN_CROP_HEIGHT - target.height) / 2
        target.y0 -= pad
        target.y1 += pad
    return target & page_rect


@functools.lru_cache(maxsize=96)
def _render_crop(pdf_path: str, page_no: int, phrases: "tuple[str, ...]",
                 highlight: str = _MENTION_HIGHLIGHTS["neutral"]) -> "tuple[bytes, int]":
    """Render a highlighted PNG crop from one PDF page (cached by source/page/phrase)."""
    import fitz

    try:
        with fitz.open(pdf_path) as doc:
            if not 1 <= page_no <= doc.page_count:
                return b"", 0
            page = doc[page_no - 1]
            rects = []
            for phrase in phrases:
                found = page.search_for(phrase)
                if not found and " " in phrase:
                    word = max(phrase.split(), key=len)
                    found = page.search_for(word) if len(word) >= 3 else []
                rects.extend(found)
            if not rects:
                return b"", 0
            clip = _crop_rect(page.rect, rects)
            # The document is opened afresh for this generated image; drawing does not modify the
            # original file.  PDF coordinates are retained, so the visual marker is auditable.
            tint = _rgb(highlight)
            # Pale sentiment tints leave the original PDF text and surrounding evidence legible.
            for rect in rects:
                page.draw_rect(rect, color=tint, fill=tint,
                               fill_opacity=0.54, width=1.25, overlay=True)
            pix = page.get_pixmap(matrix=fitz.Matrix(_CROP_SCALE, _CROP_SCALE), clip=clip,
                                  alpha=False)
            return pix.tobytes("png"), len(rects)
    except Exception:
        return b"", 0


def mention_preview(doc_id: str, markdown_path: str, char_start: int,
                    phrases: "list[str] | tuple[str, ...]", *,
                    sentiment: str = "neutral") -> "MentionPreview | None":
    """Return a highlighted page crop for one mention, or ``None`` when it cannot be verified.

    The caller must render a text-evidence card when this returns ``None``.  This strict fallback
    is intentional: a plausible-looking image that is not tied to the exact source occurrence is
    worse than no image at all.
    """
    pdf_path = pdf_path_for(doc_id)
    if not pdf_path:
        return None
    from app.components.panels import _doc_text  # avoid a second uncached read of large filings

    page = page_for_offset(_doc_text(markdown_path), char_start)
    cleaned = tuple(c for c in (_clip(p) for p in phrases) if c)
    if page is None or not cleaned:
        return None
    png, highlights = _render_crop(pdf_path, page, cleaned, highlight_color(sentiment))
    if not png:
        return None
    return MentionPreview(png=png, page=page, highlights=highlights,
                          filename=Path(pdf_path).name)


def render(st, *, doc_id: str, markdown_path: str,
           offset_phrases: "list[tuple]", key: str) -> bool:
    """Embed the real PDF inline, hits highlighted, scrolled to the first. ``False`` if no PDF.

    ``offset_phrases`` is ``[(markdown_char_offset, [found phrases], color?)]`` — one entry per hit;
    the offset picks the page, the phrases are searched on that page for the highlight boxes, and the
    optional color (sentiment of the mention) tints the box (defaults to the neutral gold).
    """
    pdf_path = pdf_path_for(doc_id)
    if not pdf_path:
        return False

    from app.components.panels import _doc_text          # reuse the cached markdown reader

    md = _doc_text(markdown_path)
    offset_pages: list[tuple] = []
    for entry in offset_phrases:
        char_start, phrases = entry[0], entry[1]
        color = entry[2] if len(entry) > 2 else _HIGHLIGHT
        page = page_for_offset(md, char_start) if md else None
        if page is None:
            continue
        cleaned = [c for c in (_clip(p) for p in phrases) if c]
        offset_pages.append((page, cleaned, color))
    if not offset_pages:                                  # markers absent → still open the doc
        offset_pages = [(1, [], _HIGHLIGHT)]

    annotations, _ = _annotations(pdf_path, offset_pages)

    from streamlit_pdf_viewer import pdf_viewer

    # Land on the hit. streamlit-pdf-viewer's scroll_to_page/scroll_to_annotation don't fire on
    # first mount under Streamlit ≥1.41 (a known regression), so instead of rendering the whole
    # PDF and scrolling, we render it FROM the earliest hit page onward — that page is then the
    # first thing shown, and the rest of the report stays scrollable below it. Start from the
    # earliest of ALL hit pages (not just the first *located* highlight) so no occurrence whose
    # phrase we couldn't box is ever hidden. (scroll_to_page is passed too as a harmless
    # belt-and-suspenders for versions where it does work.)
    total = _page_count(pdf_path)
    start_page = min(pg for pg, _, _ in offset_pages)
    pages_to_render = list(range(start_page, total + 1)) if total else []
    pdf_viewer(pdf_path, annotations=annotations, height=800, render_text=True,
               annotation_outline_size=2, pages_to_render=pages_to_render,
               scroll_to_page=start_page, scroll_behavior="instant", key=f"pdfv-{key}")
    name = Path(pdf_path).name
    tail = "" if start_page == 1 else " (earlier pages hidden — open the file for the full doc)"
    if annotations:
        st.caption(f"{len(annotations)} highlight(s) · opened at page {start_page} · {name}{tail}")
    else:
        st.caption(f"Opened at page {start_page} — exact match not located on the page · {name}")
    return True
