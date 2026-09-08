"""Reader intelligence: document Topics, Sections and sentence tone spans (offline)."""
from __future__ import annotations

from src.qa.topics import document_sections, document_topics, sentence_tones

_DOC = """# Results of operations

Net sales grew 8% in the quarter. Net sales in Mexico were driven by traffic.
EBITDA margin expanded 40 bps; EBITDA reached a record.

## Balance sheet

Net debt declined to 1.2x. The deuda neta fell again.
Costs were higher due to inflation, which hurt the operating margin.
===== Página 3 =====
Revenue and revenue again.
"""

_CONCEPTS = {
    "net sales": ["net sales", "ventas netas"],
    "ebitda": ["ebitda"],
    "net debt": ["net debt", "deuda neta"],
    "capex": ["capex", "capital expenditures"],            # absent → not a topic
    "revenue": ["revenue"],
}


def test_topics_are_literal_counts_ranked_desc_with_first_offset():
    topics = document_topics(_DOC, concepts=_CONCEPTS)
    by_label = {t.label: t for t in topics}
    # every present concept occurs exactly twice → deterministic alphabetical tiebreak
    assert [t.label for t in topics] == ["ebitda", "net debt", "net sales", "revenue"]
    assert by_label["net sales"].count == 2
    assert by_label["net debt"].count == 2 and "deuda neta" in by_label["net debt"].phrases
    assert "capex" not in by_label
    # first_offset points at the literal first occurrence (case-insensitive)
    assert _DOC[by_label["net sales"].first_offset:].lower().startswith("net sales")


def test_single_mention_is_not_a_topic_but_min_count_is_tunable():
    doc = "We mention capex once here, and nothing else of note in this text."
    assert document_topics(doc, concepts={"capex": ["capex"]}) == []
    assert [t.label for t in document_topics(doc, concepts={"capex": ["capex"]}, min_count=1)] == ["capex"]


def test_max_topics_cap_and_empty_text():
    topics = document_topics(_DOC, concepts=_CONCEPTS, max_topics=2)
    assert len(topics) == 2
    assert document_topics("", concepts=_CONCEPTS) == []


def test_sections_skip_parser_page_markers_and_keep_offsets():
    sections = document_sections(_DOC)
    assert [s.title for s in sections] == ["Results of operations", "Balance sheet"]
    assert _DOC[sections[1].offset:].startswith("## Balance sheet")


def test_sentence_tones_offsets_round_trip_and_skip_neutral():
    text = "Net sales grew strongly and margins expanded. The table below lists the segments. Costs were higher and hurt the margin."
    tones = sentence_tones(text)
    labels = {label for _, _, label in tones}
    assert "positive" in labels and "negative" in labels
    for s, e, label in tones:
        assert text[s:e].strip() == text[s:e]           # exact stripped sentence slice
        assert label in ("positive", "negative")
    # base offset shifts every span
    shifted = sentence_tones(text, base=100)
    assert [(s - 100, e - 100, l) for s, e, l in shifted] == tones


def test_sentence_tones_empty():
    assert sentence_tones("") == []
