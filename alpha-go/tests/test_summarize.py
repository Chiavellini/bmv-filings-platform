"""Smart Summaries — extractive salience/offsets/determinism + the opt-in LLM tier's gating.

All offline: the extractive path needs no network, and the LLM tier's provider call
(``summarize.llm.complete``) is monkeypatched so no SDK is imported and nothing is cached to disk
unless a test opts in.
"""
from __future__ import annotations

import pytest

from src.qa import summarize
from src.search.retriever import HybridRetriever


# --------------------------------------------------------------------------------------------
# Sentence splitting — the offset round-trip invariant.
# --------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "Revenue grew 8.3% in Q1. EBITDA reached 230.0 with an 18.4% margin.\nGuidance was raised.",
    "First line about sales.\n\nSecond paragraph mentions margins of 20.0%! And a final clause?",
    "Net debt fell to 1.5x. S.A. de C.V. filed the report. Volumes rose across formats.",
])
def test_split_sentences_offsets_round_trip(text):
    for sent, cs, ce in summarize.split_sentences(text):
        assert text[cs:ce] == sent
        assert len(sent) >= summarize._MIN_SENT_CHARS


def test_split_sentences_drops_short_fragments():
    # "# Title" and "OK." are below the min-sentence length and must not appear.
    sents = [s for s, _, _ in summarize.split_sentences("# Title\nOK.\nThis is a full sentence about revenue growth.")]
    assert sents == ["This is a full sentence about revenue growth."]


# --------------------------------------------------------------------------------------------
# Extractive per-document summary.
# --------------------------------------------------------------------------------------------
def _doc_text(store, company="acme"):
    rows = store.document_rows()
    row = next(r for r in rows if r["company"] == company)
    return open(row["markdown_path"], encoding="utf-8").read(), row


def test_document_summary_points_have_valid_offsets(built_index):
    store, embedder = built_index
    text, row = _doc_text(store)
    summ = summarize.summarize_document(
        text, doc_id=row["doc_id"], title=row["title"], company=row["company"],
        period=row["period"], markdown_path=row["markdown_path"], embedder=embedder,
    )
    assert summ.engine == "extractive"
    assert summ.points, "expected at least one takeaway"
    for p in summ.points:
        assert text[p.char_start:p.char_end] == p.text          # round-trip into the source doc
        assert p.markdown_path == row["markdown_path"]
        assert p.sentiment in {"positive", "neutral", "negative"}


def test_document_summary_surfaces_the_headline_result(built_index):
    store, embedder = built_index
    text, row = _doc_text(store)
    summ = summarize.summarize_document(text, company=row["company"], embedder=embedder,
                                        markdown_path=row["markdown_path"], max_sentences=3)
    joined = " ".join(p.text for p in summ.points)
    assert "Revenue" in joined or "Revenues" in joined       # the figure-bearing headline is salient


def test_document_summary_respects_max_sentences(built_index):
    store, embedder = built_index
    text, _ = _doc_text(store)
    summ = summarize.summarize_document(text, embedder=embedder, max_sentences=2)
    assert len(summ.points) <= 2


def test_document_summary_is_deterministic(built_index):
    store, embedder = built_index
    text, _ = _doc_text(store)
    a = summarize.summarize_document(text, embedder=embedder)
    b = summarize.summarize_document(text, embedder=embedder)
    assert [(p.text, p.score) for p in a.points] == [(p.text, p.score) for p in b.points]


def test_boilerplate_sentences_are_excluded(built_index):
    store, embedder = built_index
    text, row = _doc_text(store)

    class _FlagModel:
        """Stand-in corpus-boilerplate model that flags any sentence mentioning EBITDA."""
        def is_boilerplate(self, t):
            return "EBITDA" in t

    summ = summarize.summarize_document(text, company=row["company"], embedder=embedder,
                                        boilerplate=_FlagModel())
    assert summ.points
    assert all("EBITDA" not in p.text for p in summ.points)


# --------------------------------------------------------------------------------------------
# Thematic (query-scoped) summary over retrieved hits.
# --------------------------------------------------------------------------------------------
def test_thematic_summary_over_hits(built_index):
    store, embedder = built_index
    retriever = HybridRetriever(store, embedder=embedder)
    hits = retriever.search("revenue", limit=4)
    assert hits
    summ = summarize.summarize_hits(hits, query="revenue", embedder=embedder, max_sentences=3)
    assert summ.scope == "thematic:revenue"
    assert summ.points and len(summ.points) <= 3
    for p in summ.points:                                    # provenance is carried for the viewer
        assert p.markdown_path and (p.char_end > p.char_start)


# --------------------------------------------------------------------------------------------
# LLM tier — gating, citation mapping, cache. No network.
# --------------------------------------------------------------------------------------------
def _base_summary(store, embedder):
    text, row = _doc_text(store)
    return summarize.summarize_document(text, doc_id=row["doc_id"], company=row["company"],
                                        period=row["period"], markdown_path=row["markdown_path"],
                                        embedder=embedder)


@pytest.mark.parametrize("config", [None, {}, {"summary": {"engine": "extractive"}}])
def test_llm_disabled_returns_extractive(built_index, monkeypatch, config):
    store, embedder = built_index
    base = _base_summary(store, embedder)

    def _boom(*a, **k):  # pragma: no cover — must not be reached when the tier is off
        raise AssertionError("provider must not be called when the LLM tier is disabled")

    monkeypatch.setattr(summarize.llm, "complete", _boom)
    out = summarize.summarize_llm(base, config)
    assert out is base                                       # untouched extractive summary
    assert summarize.llm_enabled(config) is False


def test_llm_tier_maps_citations(built_index, monkeypatch):
    store, embedder = built_index
    base = _base_summary(store, embedder)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def _fake_complete(provider, *, system, user, max_tokens):
        assert "[1]" in user                                # the numbered passages reached the prompt
        return "- Revenue rose [1]\n- Margins held [2] [9]"  # [9] is out of range

    monkeypatch.setattr(summarize.llm, "complete", _fake_complete)
    out = summarize.summarize_llm(
        base,
        {"summary": {"engine": "llm", "provider": "deepseek", "cache": False}},
    )
    assert out.engine == "llm"
    assert out.narrative.startswith("- Revenue rose")
    assert [p.marker for p in out.points] == [1, 2]         # in-range markers kept, [9] dropped
    assert out.points[0] is not base.points[0]              # replace(), not mutation


def test_llm_tier_uses_cache_without_calling_provider(built_index, monkeypatch):
    store, embedder = built_index
    base = _base_summary(store, embedder)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr(summarize.llm_cache, "get",
                        lambda ns, model, text: {"text": "- Cached takeaway [1]"})

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("cache hit must short-circuit the provider call")

    monkeypatch.setattr(summarize.llm, "complete", _boom)
    out = summarize.summarize_llm(
        base,
        {"summary": {"engine": "hybrid", "provider": "deepseek", "cache": True}},
    )
    assert out.engine == "llm" and out.narrative == "- Cached takeaway [1]"
    assert [p.marker for p in out.points] == [1]
