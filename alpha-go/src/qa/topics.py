"""Reader intelligence — document Topics and sentence-level tone spans (offline, deterministic).

Two small building blocks for the AlphaSense-style reader pane:

* :func:`document_topics` — the "Topics" list that sits under a Smart Summary: which curated
  concepts (from the shared ``metric_search.yaml`` dictionary) this document actually talks
  about, ranked by how often, each anchored to its first occurrence so the reader can jump to
  it. Counting reuses the Ctrl-F-faithful :func:`app.components.doc_matches.find_matches`
  (whole-word, accent-insensitive, phrase-aware), so a topic count is a literal count — never a
  semantic guess. Markdown headings are returned alongside as ``sections`` for documents that
  carry a real outline.
* :func:`sentence_tones` — ``(start, end, label)`` spans for every sentence in a text window
  scored by the offline lexicon, so the reader can underline positive/negative sentences inline
  (the same visual channel AlphaSense uses in transcripts). Neutral sentences are omitted.

No LLM, no network, no model: both run on the lexicon and the synonym dictionary already in
the repo and degrade to empty results when either is unavailable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.qa.sentiment import score_sentiment
from src.qa.summarize import split_sentences

_HEADING_LINE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
_PAGE_MARKER = re.compile(r"^=+\s*p[aá]gina\s+\d+\s*=+$", re.IGNORECASE)

_MIN_TOPIC_COUNT = 2         # a concept mentioned once is a passing reference, not a topic
_MAX_SECTIONS = 12
_MAX_SENTENCES = 400         # tone scoring is per sentence; cap for pathological windows


@dataclass
class Topic:
    label: str                       # humanized concept name ("net debt", "same store sales")
    count: int                       # literal occurrences of the concept's phrases in the doc
    first_offset: int                # char offset of the first occurrence (reader jump target)
    spans: list = field(default_factory=list)   # all (start, end) occurrences, merged
    phrases: list = field(default_factory=list)  # the phrases that actually fired


@dataclass
class Section:
    title: str
    offset: int                      # char offset of the heading line


def _concept_phrases() -> "dict[str, list[str]]":
    """Concept → phrases from the shared synonym dictionary (best effort; ``{}`` on failure)."""
    try:
        from src.extract.semantic_search import load_search_dictionary
        d = load_search_dictionary()
    except Exception:  # noqa: BLE001 — topics are an aid, never fatal
        return {}
    out: dict[str, list[str]] = {}
    for concept, aliases in d.concept_aliases.items():
        label = concept.replace("_", " ")
        phrases = [label] + [a for a in aliases if a and a.casefold() != label.casefold()]
        # Very short aliases (2 chars: "fx", "ac") fire on unrelated tokens; keep them only when
        # the concept has no longer alias to stand on.
        long = [p for p in phrases if len(p) >= 3]
        out[label] = long or phrases
    return out


def document_sections(text: str, *, max_sections: int = _MAX_SECTIONS) -> "list[Section]":
    """Markdown headings in document order (parser page markers are not headings)."""
    out: list[Section] = []
    for m in _HEADING_LINE.finditer(text):
        title = " ".join(m.group(2).split())
        if not title or _PAGE_MARKER.match(title):
            continue
        out.append(Section(title=title[:120], offset=m.start()))
        if len(out) >= max_sections:
            break
    return out


def document_topics(text: str, *, concepts: "dict[str, list[str]] | None" = None,
                    max_topics: int = 8, min_count: int = _MIN_TOPIC_COUNT) -> "list[Topic]":
    """Curated concepts this document discusses, ranked by literal occurrence count.

    ``concepts`` maps a topic label to the phrases that count toward it; defaults to the shared
    ``metric_search.yaml`` dictionary. Ties break alphabetically so the list is deterministic.
    """
    if not text or not text.strip():
        return []
    from app.components.doc_matches import find_matches

    concepts = _concept_phrases() if concepts is None else concepts
    topics: list[Topic] = []
    for label, phrases in concepts.items():
        phrases = [p for p in phrases if p and p.strip()]
        if not phrases:
            continue
        spans = find_matches(text, phrases)
        if len(spans) < min_count:
            continue
        fired = []
        lowered = text.lower()
        for p in phrases:
            if p.lower() in lowered:
                fired.append(p)
        topics.append(Topic(label=label, count=len(spans), first_offset=spans[0][0],
                            spans=list(spans), phrases=fired[:4]))
    topics.sort(key=lambda t: (-t.count, t.label))
    return topics[:max_topics]


def sentence_tones(text: str, *, base: int = 0,
                   max_sentences: int = _MAX_SENTENCES) -> "list[tuple[int, int, str]]":
    """``(start, end, label)`` for each positive/negative sentence in ``text``.

    Offsets are relative to ``text`` plus ``base`` (pass the window start to get document
    offsets). Neutral sentences are skipped so the caller underlines only what carries tone.
    """
    if not text:
        return []
    out: list[tuple[int, int, str]] = []
    for sent, cs, ce in split_sentences(text)[:max_sentences]:
        label = score_sentiment(sent).label
        if label in ("positive", "negative"):
            out.append((base + cs, base + ce, label))
    return out
