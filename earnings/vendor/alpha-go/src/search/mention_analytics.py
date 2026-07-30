"""Passage-level sentiment for literal mention spans.

Search and Trends both operate on exact character spans in the indexed document.  This module
turns each span into one independently scored mention so the UI never hides a positive/negative
split behind a single document- or period-level average.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.qa.sentiment import score_sentiment


@dataclass(frozen=True)
class MentionTone:
    start: int
    end: int
    context: str
    label: str
    score: float
    rationale: str


def mention_tone(
    text: str,
    start: int,
    end: int,
    *,
    before: int = 240,
    after: int = 360,
) -> MentionTone:
    """Score one mention using a bounded passage around its exact document span."""
    start = max(0, min(int(start), len(text)))
    end = max(start, min(int(end), len(text)))
    lo = max(0, start - before)
    hi = min(len(text), end + after)
    context = text[lo:hi]
    result = score_sentiment(context)
    return MentionTone(
        start=start,
        end=end,
        context=context,
        label=result.label,
        score=result.score,
        rationale=result.rationale,
    )


def mention_tones(text: str, spans: "list[tuple[int, int]]") -> list[MentionTone]:
    """Score every supplied match independently, preserving document order."""
    return [mention_tone(text, start, end) for start, end in spans]


def tone_counts(tones: "list[MentionTone]") -> dict[str, int]:
    """Stable positive/neutral/negative counts for charts and compact summaries."""
    counts = {"positive": 0, "neutral": 0, "negative": 0}
    for tone in tones:
        if tone.label in counts:
            counts[tone.label] += 1
    return counts
