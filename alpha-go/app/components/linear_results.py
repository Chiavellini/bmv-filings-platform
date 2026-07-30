"""Linear occurrence view — group hits per document, every keyword match in document order.

The retriever stays the *document selector* (relevance decides which docs surface); this
module is the *occurrence enumerator*: for each surfaced document it re-reads the full text,
finds every match with ``doc_matches.find_matches`` (character-position order), and windows
each into a small whitespace-snapped snippet. Documents are then ordered chronologically
(company, then period), so the results read linearly — occurrence 1..N per document, oldest
period first. Pure helpers, no Streamlit import, same char-offset span discipline as
``doc_matches.py``.

Corpus-wide completeness (the former "upgrade path"): the document universe is no longer the
retriever's capped candidate pool. When ``group_hits`` is given ``doc_rows`` — the full (scoped)
document set from ``IndexStore.document_rows`` — EVERY document is lexical-scanned and any with a
literal match is counted, including ones ranked far below the pool and ones the FTS tokenizer would
miss. This is the foolproof multi-document Command-F: it does not depend on ranking. The
retriever's ranked ``hits`` are still used, but only to order the
surfaced documents first (for display) and to supply per-doc anchor fallbacks; they no longer bound
the occurrence total. Documents with zero literal matches are dropped; only the number of documents
*windowed for rendering* is capped — the reported totals are corpus-wide.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.components.doc_matches import find_matches
from src.search.snippets import Snippet, _snap_end, _snap_start


@dataclass
class Occurrence:
    """One rendered occurrence window — may cover several nearby match spans."""

    spans: list[tuple[int, int]]                # doc-absolute merged match spans in this window
    snippet: Snippet                            # window text; .spans are window-relative


@dataclass
class DocGroup:
    """All occurrences of the query terms within one document, in document order."""

    doc_id: str
    company: str                                # primary corpus (owns the on-disk copy)
    period: str | None
    doc_type: str
    title: str
    markdown_path: str
    occurrences: list[Occurrence] = field(default_factory=list)
    total_matches: int = 0                      # all matches in the doc (>= windowed ones)
    companies: list[str] = field(default_factory=list)   # all corpora this doc belongs to


def match_ranges(occurrences: "list[Occurrence]") -> "list[tuple[int, int]]":
    """1-based ``(first, last)`` match numbers per occurrence window.

    Windows can cover several merged matches, so the renderer labels them by match range
    ("4–6 / 16") instead of a window index that would silently skip numbers.
    """
    ranges: list[tuple[int, int]] = []
    pos = 1
    for occ in occurrences:
        ranges.append((pos, pos + len(occ.spans) - 1))
        pos += len(occ.spans)
    return ranges


def doc_sort_key(g: DocGroup) -> tuple:
    """Chronological within company; undated docs sort after dated ones.

    Period labels like ``2023Q1`` sort lexically-chronologically; the path is a stable
    tie-break for same-period docs.
    """
    return (g.company, g.period is None, g.period or "", g.markdown_path)


def occurrence_snippets(
    doc_text: str,
    spans: "list[tuple[int, int]]",
    *,
    before: int = 120,
    after: int = 200,
    max_window: int = 1200,
) -> "list[Occurrence]":
    """Window each match span into an :class:`Occurrence`, merging overlapping windows.

    ``spans`` must be sorted and merged (``find_matches`` output). Each span gets a
    whitespace-snapped window ``[s - before, e + after]``; when the next span's window would
    overlap the current one, the window is extended instead so nearby matches share one
    snippet (with multiple highlight spans) — but never past ``max_window`` chars, so a dense
    term (every row of a table) can't chain hundreds of matches into one giant card.
    Snippet spans are re-based to window offsets.
    """
    occurrences: list[Occurrence] = []
    win_lo = win_hi = -1
    covered: list[tuple[int, int]] = []

    def _emit() -> None:
        if not covered:
            return
        occurrences.append(Occurrence(
            spans=list(covered),
            snippet=Snippet(
                text=doc_text[win_lo:win_hi],
                spans=[(s - win_lo, e - win_lo) for s, e in covered],
                char_start=win_lo,
                truncated_start=win_lo > 0,
                truncated_end=win_hi < len(doc_text),
            ),
        ))

    for s, e in spans:
        lo = _snap_start(doc_text, max(0, s - before))
        hi = _snap_end(doc_text, min(len(doc_text), e + after))
        if covered and lo <= win_hi and max(win_hi, hi) - win_lo <= max_window:
            win_hi = max(win_hi, hi)            # windows touch/overlap — same snippet
            covered.append((s, e))
        else:
            _emit()
            win_lo, win_hi = lo, hi
            covered = [(s, e)]
    _emit()
    return occurrences


def sort_groups(groups: "list[DocGroup]", *, newest_first: bool = False) -> "list[DocGroup]":
    """Groups in chronological order per company; ``newest_first`` reverses the period order.

    Company grouping is stable either way — only the within-company period direction flips
    (undated docs stay last).
    """
    if not newest_first:
        return sorted(groups, key=doc_sort_key)
    by_period = sorted(groups, key=lambda g: (g.period is None, g.period or "", g.markdown_path),
                       reverse=True)
    return sorted(by_period, key=lambda g: (g.company, g.period is None))


def _row_companies(r) -> list[str]:
    """All corpora for a doc-level row: split the ``companies`` GROUP_CONCAT, else the primary."""
    try:
        raw = r["companies"]
    except (IndexError, KeyError):
        raw = None
    slugs = [c for c in str(raw).split(",") if c] if raw else []
    return slugs or [r["company"]]


def _hit_group(h) -> DocGroup:
    return DocGroup(doc_id=h.doc_id, company=h.company, period=h.period,
                    doc_type=h.doc_type, title=h.title, markdown_path=h.markdown_path,
                    companies=(getattr(h, "companies", None) or [h.company]))


def _row_group(r) -> DocGroup:
    """A ``DocGroup`` shell from a doc-level FTS row (mapping access — a sqlite ``Row`` or dict)."""
    return DocGroup(doc_id=r["doc_id"], company=r["company"], period=r["period"],
                    doc_type=r["doc_type"], title=r["title"], markdown_path=r["markdown_path"],
                    companies=_row_companies(r))


def _fill_group(group: DocGroup, *, terms, doc_text_fn, anchor, window: bool,
                before: int, after: int, max_per_doc: int) -> bool:
    """Scan a document's FULL text into ``group`` (total_matches always; windows iff ``window``).

    Returns ``True`` if the document holds ≥1 (literal or anchor-fallback) match — i.e. it counts.
    ``anchor`` is the retriever hit for this doc (or ``None`` for an unsurfaced doc); it supplies
    the fallbacks that keep a *surfaced* doc visible when the literal scan finds nothing (an FTS
    phrase split across a newline, or unreadable text). An unsurfaced doc with no literal match is
    an FTS-only artifact for a literal Ctrl+F count → it does not count.
    """
    text = doc_text_fn(group.markdown_path)
    if text:
        spans = find_matches(text, terms)
        if not spans and anchor is not None:
            spans = [(anchor.char_start, anchor.char_end)]
        if not spans:
            return False
        group.total_matches = len(spans)
        if window:
            group.occurrences = occurrence_snippets(
                text, spans[:max_per_doc], before=before, after=after)
        return True
    if anchor is not None:                          # unreadable but surfaced → chunk-snippet fallback
        group.total_matches = 1
        if window:
            group.occurrences = [
                Occurrence(spans=[(anchor.char_start, anchor.char_end)], snippet=anchor.snippet)]
        return True
    return False                                    # unreadable and unsurfaced → cannot verify


def group_hits(
    hits: list,
    *,
    terms: "list[str]",
    doc_text_fn,
    doc_rows: "list | None" = None,
    max_docs: "int | None" = None,
    before: int = 120,
    after: int = 200,
    max_per_doc: int = 100,
) -> "list[DocGroup]":
    """Collapse the matching documents into per-document groups holding EVERY occurrence, in order.

    Two universes:
      * ``doc_rows=None`` (legacy) — the documents are exactly those among the ranked ``hits``,
        deduped and capped at ``max_docs``. Kept for callers/tests that only have chunk hits.
      * ``doc_rows`` given — the full (scoped) document set (``IndexStore.document_rows``) is the
        universe: every document is lexical-scanned, so a term buried in a low-ranked document is
        still COUNTED. ``hits`` order the surfaced documents
        first (relevance) and provide anchor fallbacks; ``max_docs`` becomes a *display* cap (only
        that many docs are windowed for rendering) — every matching document is still returned with
        its ``total_matches`` so the caller can report corpus-wide totals.

    Either way the FULL text is scanned per document with ``find_matches`` (occurrences invisible to
    the chunk hits still show), and the output is ordered chronologically (``doc_sort_key``).
    """
    groups: list[DocGroup] = []
    seen: set[str] = set()

    if doc_rows is None:
        for h in hits:
            if h.doc_id in seen:
                continue
            if max_docs is not None and len(groups) >= max_docs:
                break
            seen.add(h.doc_id)
            group = _hit_group(h)
            _fill_group(group, terms=terms, doc_text_fn=doc_text_fn, anchor=h, window=True,
                        before=before, after=after, max_per_doc=max_per_doc)
            groups.append(group)
        groups.sort(key=doc_sort_key)
        return groups

    # Corpus-wide: surfaced docs first (relevance), then the rest of the FTS scan. ``max_docs``
    # windows only the first N *counted* docs; all counted docs are returned for the true totals.
    anchors: dict = {}
    order: list = []
    for h in hits:
        if h.doc_id not in anchors:
            anchors[h.doc_id] = h
            order.append(_hit_group(h))
            seen.add(h.doc_id)
    for r in doc_rows:
        if r["doc_id"] not in seen:
            seen.add(r["doc_id"])
            order.append(_row_group(r))

    windowed = 0
    for group in order:
        window = max_docs is None or windowed < max_docs
        if _fill_group(group, terms=terms, doc_text_fn=doc_text_fn,
                       anchor=anchors.get(group.doc_id), window=window,
                       before=before, after=after, max_per_doc=max_per_doc):
            groups.append(group)
            if window:
                windowed += 1

    groups.sort(key=doc_sort_key)
    return groups
