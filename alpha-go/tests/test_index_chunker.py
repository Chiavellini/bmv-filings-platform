"""Tests for the chunker (Phase 2). The round-trip invariant is the load-bearing one."""
from __future__ import annotations

from src.index.chunker import chunk_document

SAMPLE = (
    "# Acme Corp — Q1 2024\n\n"
    "Total Revenues reached 1,250.0, up 8.3% year over year. Pricing pressure persisted.\n\n"
    "## Profitability\n\n"
    "EBITDA finalized at 230.0 with a margin of 18.4%.\n\n"
    "Net income was 90.0, reflecting higher financing costs.\n"
)


def _assert_round_trip(markdown, chunks):
    for c in chunks:
        assert markdown[c.char_start:c.char_end] == c.text


def test_round_trip_invariant_small_target():
    chunks = chunk_document("acme/2024-1T", SAMPLE, target_chars=80, overlap_chars=20)
    assert len(chunks) > 1
    _assert_round_trip(SAMPLE, chunks)


def test_round_trip_invariant_single_chunk():
    chunks = chunk_document("d", SAMPLE, target_chars=10_000)
    assert len(chunks) == 1
    _assert_round_trip(SAMPLE, chunks)


def test_ordinals_and_ids_sequential():
    chunks = chunk_document("acme/2024-1T", SAMPLE, target_chars=80, overlap_chars=10)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    assert [c.chunk_id for c in chunks] == [f"acme/2024-1T#{i}" for i in range(len(chunks))]


def test_covers_all_non_whitespace():
    chunks = chunk_document("d", SAMPLE, target_chars=70, overlap_chars=15)
    covered = set()
    for c in chunks:
        covered.update(range(c.char_start, c.char_end))
    for i, ch in enumerate(SAMPLE):
        if not ch.isspace():
            assert i in covered, f"char {i!r} at {i} not covered"


def test_heading_captured():
    chunks = chunk_document("d", SAMPLE, target_chars=60, overlap_chars=0)
    headings = {c.heading for c in chunks}
    assert "Acme Corp — Q1 2024" in headings
    assert any(h == "Profitability" for h in headings)


def test_oversized_paragraph_hard_split():
    big = "# H\n\n" + ("x" * 500)
    chunks = chunk_document("d", big, target_chars=100, overlap_chars=0)
    assert len(chunks) >= 5
    _assert_round_trip(big, chunks)


def test_empty_and_whitespace():
    assert chunk_document("d", "") == []
    assert chunk_document("d", "   \n\n   ") == []


def test_large_heading_lookup_is_precomputed(monkeypatch):
    import src.index.chunker as chunker

    # chunk_document must not call the prefix-scanning compatibility helper for
    # every output chunk. Heading lookup is precomputed and binary-searched.
    monkeypatch.setattr(
        chunker,
        "_heading_before",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("quadratic heading rescan")
        ),
    )
    markdown = "\n\n".join(
        f"## Heading {i}\n\n" + ("x" * 400) for i in range(1_000)
    )

    chunks = chunker.chunk_document(
        "large", markdown, target_chars=200, overlap_chars=20
    )

    assert len(chunks) >= 2_000
    assert chunks[0].heading == "Heading 0"
    assert chunks[-1].heading == "Heading 999"
