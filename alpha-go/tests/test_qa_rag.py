"""Phase-5 RAG Q&A — gating, prompt assembly, citation mapping, not-found path.

All offline: the LLM chokepoint (``rag._call_llm``) is monkeypatched; the ``anthropic`` SDK is
never touched (a poisoned stub module asserts that).
"""
from __future__ import annotations

import sys
import types

import pytest

from src.qa import rag
from src.qa.prompts import NOT_FOUND, build_user_prompt
from src.search.retriever import HybridRetriever


def _poison_anthropic(monkeypatch):
    """Install an ``anthropic`` stub whose client constructor fails the test if touched."""
    stub = types.ModuleType("anthropic")

    def _boom(*a, **k):  # pragma: no cover — reaching this IS the failure
        raise AssertionError("anthropic client must not be constructed")

    stub.Anthropic = _boom
    monkeypatch.setitem(sys.modules, "anthropic", stub)
    return stub


@pytest.mark.parametrize("config", [None, {}, {"qa": {"enabled": False}}])
def test_ask_disabled_by_default(monkeypatch, config):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _poison_anthropic(monkeypatch)
    assert rag.ask("q", retriever=None, config=config) is None


def test_ask_needs_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _poison_anthropic(monkeypatch)
    assert rag.ask("q", retriever=None, config={"qa": {"enabled": True}}) is None


def test_build_user_prompt_numbers_and_question():
    prompt = build_user_prompt("How did revenue evolve?",
                               ["[1] (ACME · 2024-1T · report) revenue grew",
                                "[2] (ACME · 2024-2T · report) margins fell"])
    assert "[1] (ACME · 2024-1T · report) revenue grew" in prompt
    assert "[2] (ACME · 2024-2T · report) margins fell" in prompt
    assert "Question: How did revenue evolve?" in prompt
    assert NOT_FOUND in prompt


def test_answer_not_found_without_hits(monkeypatch):
    def _no_call(**kwargs):  # pragma: no cover
        raise AssertionError("LLM must not be called with zero hits")

    monkeypatch.setattr(rag, "_call_llm", _no_call)
    result = rag.answer("anything", [])
    assert result.text == NOT_FOUND
    assert result.citations == []


def test_answer_citations_map_to_real_hits(built_index, monkeypatch):
    store, embedder = built_index
    retriever = HybridRetriever(store, embedder=embedder)
    hits = retriever.search("revenue", limit=2)
    assert len(hits) == 2

    seen_prompts: dict = {}

    def _fake_llm(*, model, system, user):
        seen_prompts.update(model=model, system=system, user=user)
        return "Revenue grew [1] while margins fell [2] [9]."

    monkeypatch.setattr(rag, "_call_llm", _fake_llm)
    result = rag.answer("How did revenue evolve?", hits)

    # markers 1 and 2 map to the hits in rank order; the out-of-range [9] is dropped
    assert [c.marker for c in result.citations] == [1, 2]
    assert result.citations[0].chunk_id == hits[0].chunk_id
    assert result.citations[1].chunk_id == hits[1].chunk_id
    assert result.citations[0].doc_id == hits[0].doc_id
    assert (result.citations[0].char_start, result.citations[0].char_end) == (
        hits[0].char_start, hits[0].char_end)
    # the numbered snippets made it into the user prompt
    assert "[1] (ACME ·" in seen_prompts["user"]
    assert seen_prompts["model"] == rag._DEFAULT_MODEL


def test_ask_end_to_end_gated_on(built_index, monkeypatch):
    store, embedder = built_index
    retriever = HybridRetriever(store, embedder=embedder)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "anthropic", types.ModuleType("anthropic"))
    monkeypatch.setattr(rag, "_call_llm", lambda **k: "Growth was strong [1].")

    result = rag.ask("revenue growth", retriever,
                     config={"qa": {"enabled": True, "top_k": 3}})
    assert result is not None
    assert result.text == "Growth was strong [1]."
    assert len(result.citations) == 1 and result.citations[0].marker == 1
