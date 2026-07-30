"""RAG — retrieval-augmented Q&A over the corpus (optional, LLM-backed).

Flow (Phase 5):
    1. HybridRetriever.search(question, filters)  -> top Hits
    2. number the Hit snippets, build_user_prompt(...)
    3. call the LLM (anthropic, lazily imported) with SYSTEM_PROMPT
    4. return a CitedAnswer linking each [n] back to its Hit

``answer`` is the pure synthesis step (hits in, cited answer out) so it can be tested with a
monkeypatched ``_call_llm``; ``ask`` wraps it with retrieval and the same enable/API-key gate
as ``sentiment.refine_with_llm`` — LLM code never runs unless explicitly enabled.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from src.qa import llm
from src.qa.prompts import NOT_FOUND, SYSTEM_PROMPT, build_user_prompt
from src.search.retriever import Hit

_MARKER_RE = re.compile(r"\[(\d+)\]")
_DEFAULT_MODEL = "claude-haiku-4-5"
_MAX_TOKENS = 1024


@dataclass
class Citation:
    marker: int          # the [n] used in the answer text
    chunk_id: str
    doc_id: str
    title: str
    char_start: int = 0  # chunk location in the source doc (for the doc viewer)
    char_end: int = 0


@dataclass
class CitedAnswer:
    text: str
    citations: list[Citation] = field(default_factory=list)


def _number_snippets(hits: list[Hit]) -> list[str]:
    """One ``[n] (COMPANY · period · doc_type) text`` block per hit, in rank order."""
    return [
        f"[{i}] ({h.company.upper()} · {h.period or '—'} · {h.doc_type}) {h.snippet.text.strip()}"
        for i, h in enumerate(hits, start=1)
    ]


def _call_llm(*, model: str, system: str, user: str) -> str:
    """Anthropic chokepoint (tests monkeypatch this). Delegates to the shared provider layer.

    Kept for the default Anthropic path and its existing test seam; the OpenAI-compatible
    (DeepSeek) path is taken directly in :func:`answer` via a resolved ``ProviderConfig``.
    """
    provider = llm.ProviderConfig(
        kind="anthropic", provider="anthropic", model=model, base_url=None,
        api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    )
    return llm.complete(provider, system=system, user=user, max_tokens=_MAX_TOKENS)


def _extract_citations(text: str, hits: list[Hit]) -> list[Citation]:
    """Map each in-range ``[n]`` marker in ``text`` to its Hit; out-of-range markers dropped."""
    markers = sorted({int(m) for m in _MARKER_RE.findall(text)})
    out: list[Citation] = []
    for m in markers:
        if 1 <= m <= len(hits):
            h = hits[m - 1]
            out.append(Citation(
                marker=m, chunk_id=h.chunk_id, doc_id=h.doc_id, title=h.title,
                char_start=h.char_start, char_end=h.char_end,
            ))
    return out


def answer(question: str, hits: list[Hit], *, model: str | None = None,
           provider: "llm.ProviderConfig | None" = None) -> CitedAnswer:
    """Synthesize a cited answer from retrieved hits (pure synthesis — no gating, no retrieval).

    Zero hits short-circuits to the NOT_FOUND sentinel without an API call. Citation markers in
    the reply that don't correspond to a provided snippet are dropped. The default Anthropic path
    goes through :func:`_call_llm` (the test seam); a non-Anthropic ``provider`` (e.g. DeepSeek)
    is called directly via the shared layer.
    """
    if not hits:
        return CitedAnswer(text=NOT_FOUND, citations=[])
    user = build_user_prompt(question, _number_snippets(hits))
    if provider is not None and provider.kind != "anthropic":
        text = llm.complete(provider, system=SYSTEM_PROMPT, user=user, max_tokens=_MAX_TOKENS)
    else:
        text = _call_llm(model=model or _DEFAULT_MODEL, system=SYSTEM_PROMPT, user=user)
    return CitedAnswer(text=text, citations=_extract_citations(text, hits))


def ask(
    question: str,
    retriever,
    *,
    config: dict | None = None,
    filters=None,
) -> CitedAnswer | None:
    """Retrieve → answer. Returns ``None`` unless ``qa.enabled`` and a provider key is present.

    Gating: disabled config or an unresolvable provider (no API key for the configured provider)
    yields ``None`` (callers show the "enable Q&A" hint). The provider defaults to Anthropic but
    can be switched to DeepSeek via ``qa.provider``. API/network failures also degrade to ``None``.
    """
    qa = (config or {}).get("qa", {})
    if not qa.get("enabled"):
        return None
    provider = llm.resolve_provider(qa)
    if provider is None:
        return None

    hits = retriever.search(question, filters=filters, limit=qa.get("top_k", 8))
    try:
        return answer(question, hits, model=provider.model, provider=provider)
    except Exception:  # noqa: BLE001 — network/SDK failures degrade like sentiment's refine
        return None
