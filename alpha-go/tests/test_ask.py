"""Ask pipeline: plan (decompose, scope chips), fused retrieval, evidence vs LLM modes, chips."""
from __future__ import annotations

from types import SimpleNamespace

from src.qa import ask, rag
from src.qa.prompts import NOT_FOUND
from src.search.filters import SearchFilters
from src.search.snippets import Snippet


def _hit(chunk_id, doc_id="acme/2024-1T", text="Total revenues grew 8%.", score=1.0,
         company="acme", period="2024-1T"):
    return SimpleNamespace(chunk_id=chunk_id, doc_id=doc_id, company=company, period=period,
                           doc_type="quarterly_release", title="Q1", score=score,
                           snippet=Snippet(text=text, spans=[(0, 5)]), markdown_path="/x.md",
                           char_start=10, char_end=40)


class _Retriever:
    """Returns a different ranked list per sub-query so fusion has something to merge."""

    def __init__(self, by_query):
        self.by_query = by_query
        self.calls = []

    def search(self, q, *, filters=None, limit=8, **k):
        self.calls.append((q, filters, limit))
        return list(self.by_query.get(q, self.by_query.get("*", [])))[:limit]


def test_key_phrase_drops_interrogatives_in_both_languages():
    assert ask.key_phrase("What has management said about pricing pressure over the last year?") \
        == "pricing pressure"
    assert ask.key_phrase("¿Qué dijo la administración sobre la presión de precios?") \
        == "administracion presion precios"
    assert ask.key_phrase("") == ""


def test_decompose_starts_with_key_phrase_then_equivalents_deduped_and_capped():
    subs = ask.decompose("How did fx affect margins?")
    assert subs[0] == "fx margins"
    assert len(subs) <= ask._MAX_SUBQUERIES
    assert len({s.casefold() for s in subs}) == len(subs)
    assert ask.decompose("   ") == []
    assert ask.decompose("the of and") == ["the of and"]        # stopword-only → the question itself


def test_scope_chips_reflect_filters_or_whole_corpus():
    f = SearchFilters(companies=["walmex"], doc_types=["quarterly_release"], period_from="2025-1T",
                      period_to="2026-2T", doc_ids=["a", "b"])
    chips = ask.scope_chips(f, company_label=lambda c: c.title(),
                            doc_type_label=lambda d: "Quarterly")
    assert [(c.kind, c.label) for c in chips] == [
        ("companies", "Walmex"), ("doc_types", "Quarterly"), ("period", "2025-1T – 2026-2T"),
        ("documents", "2 selected document(s)")]
    assert [c.label for c in ask.scope_chips(SearchFilters())] == ["Whole corpus"]


def test_retrieve_fuses_sub_queries_by_reciprocal_rank():
    a, b, c = _hit("a"), _hit("b"), _hit("c", doc_id="acme/2024-2T", period="2024-2T")
    r = _Retriever({"q1": [a, b], "q2": [b, c]})
    hits = ask.retrieve(["q1", "q2"], r, filters=SearchFilters(), top_k=3)
    assert [h.chunk_id for h in hits][0] == "b"           # found by both → first
    assert {h.chunk_id for h in hits} == {"a", "b", "c"}
    assert len(r.calls) == 2 and all(isinstance(c[1], SearchFilters) for c in r.calls)
    assert ask.retrieve([], r) == []


def test_docs_read_aggregates_per_document_most_hits_first():
    hits = [_hit("a"), _hit("b"), _hit("c", doc_id="acme/2024-2T", period="2024-2T")]
    docs = ask.docs_read(hits)
    assert [(d.doc_id, d.hits) for d in docs] == [("acme/2024-1T", 2), ("acme/2024-2T", 1)]


def test_run_ask_evidence_mode_without_llm(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = _Retriever({"*": [_hit("a"), _hit("b")]})
    res = ask.run_ask("What happened to revenues?", r, config={"qa": {"enabled": False}},
                      filters=SearchFilters(companies=["acme"]))
    assert res.plan.mode == "evidence" and res.answer is None
    assert res.plan.sub_queries[0] == "revenues"
    assert res.plan.scope[0].label == "ACME"
    assert res.plan.chunks_read == 2 and res.plan.docs_read[0].hits == 2
    assert not res.not_found


def test_run_ask_llm_mode_uses_rag_with_thread_context_and_caches(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(ask.llm_cache, "_CACHE_ROOT", tmp_path / "cache")
    seen = {}

    def fake_complete(provider, *, system, user, max_tokens):
        seen["user"] = user
        return "Revenues grew 8% [1]. Not in snippets [9]."

    monkeypatch.setattr(ask.llm, "complete", fake_complete)
    cfg = {"qa": {"enabled": True, "provider": "deepseek"}}
    r = _Retriever({"*": [_hit("a"), _hit("b")]})
    res = ask.run_ask("Did revenues grow?", r, config=cfg, filters=SearchFilters(),
                      history=[("earlier q", "earlier a")])
    assert res.plan.mode == "llm" and res.plan.provider == "deepseek · deepseek-chat"
    assert [c.marker for c in res.answer.citations] == [1]        # out-of-range [9] dropped
    assert "earlier q" in seen["user"] and "Did revenues grow?" in seen["user"]
    assert not res.cached
    # second run → disk cache, no provider call
    monkeypatch.setattr(ask.llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    res2 = ask.run_ask("Did revenues grow?", r, config=cfg, filters=SearchFilters(),
                       history=[("earlier q", "earlier a")])
    assert res2.cached and res2.answer.text == res.answer.text


def test_run_ask_llm_failure_degrades_to_evidence(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")

    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(ask.llm, "complete", boom)
    res = ask.run_ask("q revenues", _Retriever({"*": [_hit("a")]}),
                      config={"qa": {"enabled": True, "provider": "deepseek", "cache": False}})
    assert res.plan.mode == "evidence" and "unreachable" in res.plan.provider
    assert res.answer is None and len(res.hits) == 1


def test_run_ask_llm_mode_with_no_hits_is_not_found_without_a_call(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
    monkeypatch.setattr(ask.llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    res = ask.run_ask("q", _Retriever({}), config={"qa": {"enabled": True, "provider": "deepseek"}})
    assert res.not_found and res.answer.text == NOT_FOUND and res.plan.mode == "llm"


def test_answer_segments_split_markers():
    segs = ask.answer_segments("Grew [1] and fell [12].")
    assert segs == [("Grew ", None), ("", 1), (" and fell ", None), ("", 12), (".", None)]
    assert ask.answer_segments("") == []
    assert isinstance(rag.CitedAnswer(text="x"), rag.CitedAnswer)
