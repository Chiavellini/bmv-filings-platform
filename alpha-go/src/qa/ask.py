"""Ask — the research-plan + cited-answer pipeline behind the Ask panel.

AlphaSense's Generative Search shows *how* it looked before it shows what it found: the scope
it searched, the sub-queries it ran, the documents it read, and then an answer whose every
claim carries a citation chip. This module produces exactly that, deterministically where it
can and with the LLM only for the final synthesis:

1. :func:`decompose` — the question → a few literal sub-queries (the question's key phrase,
   its vetted ES/EN equivalents and curated metric wording). No LLM: the plan must be
   reproducible and auditable, and the corpus is bilingual so the equivalents matter more
   than paraphrases.
2. :func:`retrieve` — run every sub-query through :class:`~src.search.retriever.HybridRetriever`
   under the same :class:`~src.search.filters.SearchFilters` the sidebar holds, then merge by
   reciprocal rank so a chunk found by two sub-queries outranks one found by one.
3. :func:`run_ask` — build the :class:`ResearchPlan`, retrieve, and either synthesize a cited
   answer through :func:`src.qa.rag.answer` (when ``qa.enabled`` and a provider key resolve;
   disk-cached under ``.llm_cache/qa/``) or return the numbered **evidence pack** — the same
   snippets the LLM would have read, so the panel is useful with no key at all.

The ``[n]`` citation contract is :mod:`src.qa.rag`'s; :func:`answer_segments` splits an answer
into text/marker runs so the UI can render each marker as a clickable chip.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.qa import llm, llm_cache, rag
from src.qa.prompts import NOT_FOUND, SYSTEM_PROMPT, build_user_prompt
from src.search.fusion import reciprocal_rank_fusion

_MARKER_RE = re.compile(r"\[(\d+)\]")
_MAX_SUBQUERIES = 4
_DEFAULT_TOP_K = 8

# Interrogative / connective words dropped when reducing a question to its key phrase. Both
# languages, since analysts here ask in either.
_QUESTION_WORDS = frozenset("""
what which who whom whose when where why how did does do has have had is are was were will would
should could can may might about regarding on of the a an and or to in for from with by at as
its their our management say said saying mention mentioned mentions discuss discussed discussion
comment commented commentary tone changed change over last past year years quarter quarters recent
recently latest affect affected affects impact impacted impacts drive drove driven happened happen
evolve evolved describe explain compare
que cual cuales quien quienes cuando donde por como ha han hubo es son fue fueron sera sobre del de
la el los las un una y o a en para con al su sus dijo dice dicen menciona mencionan comenta
comentan cambio cambios ultimo ultima ultimos ultimas reciente recientes trimestre trimestres ano anos
afecto afectaron impacto impactaron explica compara
""".split())


@dataclass
class ScopeChip:
    kind: str        # "companies" | "industries" | "doc_types" | "period" | "documents"
    label: str


@dataclass
class DocRead:
    doc_id: str
    company: str
    period: str | None
    doc_type: str
    title: str
    hits: int        # how many retrieved chunks came from this document


@dataclass
class ResearchPlan:
    question: str
    scope: list = field(default_factory=list)          # ScopeChip
    sub_queries: list = field(default_factory=list)    # str, in the order they ran
    docs_read: list = field(default_factory=list)      # DocRead, most-hit first
    chunks_read: int = 0
    mode: str = "evidence"                             # "llm" | "evidence"
    provider: str | None = None                        # "deepseek · deepseek-chat" when llm


@dataclass
class AskResult:
    plan: ResearchPlan
    hits: list                                         # retriever Hits, fused order
    answer: "rag.CitedAnswer | None"                   # None in evidence mode
    cached: bool = False

    @property
    def not_found(self) -> bool:
        return bool(self.answer and self.answer.text.strip() == NOT_FOUND)


# --------------------------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------------------------
def key_phrase(question: str) -> str:
    """The question minus its interrogative scaffolding — the literal thing to look for."""
    import unicodedata

    from src.search.fusion import _normalize_text

    folded = "".join(ch for ch in unicodedata.normalize("NFKD", question or "")
                     if not unicodedata.combining(ch))
    toks = [t for t in _normalize_text(folded).split() if t and t not in _QUESTION_WORDS]
    return " ".join(toks).strip()


def decompose(question: str, *, max_subqueries: int = _MAX_SUBQUERIES) -> list[str]:
    """Deterministic sub-queries for ``question``: key phrase, ES/EN equivalents, curated wording.

    Order matters (it is the order the plan shows): the question's own key phrase first, then
    vetted direct translations, then curated metric equivalents. Empty/stopword-only questions
    yield the question itself so retrieval still runs.
    """
    from src.index.keyword_index import bilingual_equivalent_phrases, metric_synonym_phrases

    q = " ".join((question or "").split())
    if not q:
        return []
    phrase = key_phrase(q)
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = " ".join(s.split())
        if s and s.casefold() not in seen and len(out) < max_subqueries:
            seen.add(s.casefold())
            out.append(s)

    _add(phrase or q)
    for extra in bilingual_equivalent_phrases(phrase or q):
        _add(extra)
    for extra in metric_synonym_phrases(phrase or q):
        _add(extra)
    return out


def scope_chips(filters, *, company_label=None, doc_type_label=None) -> list[ScopeChip]:
    """Human-readable chips for the scope a question ran under (the sidebar's filters)."""
    chips: list[ScopeChip] = []
    if filters is None:
        return chips
    cl = company_label if callable(company_label) else (lambda c: str(c).upper())
    dl = doc_type_label if callable(doc_type_label) else (lambda d: str(d))
    for c in getattr(filters, "companies", None) or []:
        chips.append(ScopeChip("companies", cl(c)))
    for i in getattr(filters, "industries", None) or []:
        chips.append(ScopeChip("industries", str(i).replace("_", " ").title()))
    for d in getattr(filters, "doc_types", None) or []:
        chips.append(ScopeChip("doc_types", dl(d)))
    lo, hi = getattr(filters, "period_from", None), getattr(filters, "period_to", None)
    if lo or hi:
        chips.append(ScopeChip("period", f"{lo or '…'} – {hi or '…'}"))
    n_docs = len(getattr(filters, "doc_ids", None) or [])
    if n_docs:
        chips.append(ScopeChip("documents", f"{n_docs} selected document(s)"))
    if not chips:
        chips.append(ScopeChip("companies", "Whole corpus"))
    return chips


# --------------------------------------------------------------------------------------------
# Retrieve
# --------------------------------------------------------------------------------------------
def retrieve(sub_queries: list[str], retriever, *, filters=None, top_k: int = _DEFAULT_TOP_K,
             per_query: "int | None" = None) -> list:
    """Run each sub-query and fuse the ranked lists by reciprocal rank (dedup by chunk)."""
    if not sub_queries:
        return []
    per_query = per_query or max(top_k, 8)
    ranked: list[list[str]] = []
    by_id: dict[str, object] = {}
    for sq in sub_queries:
        try:
            hits = retriever.search(sq, filters=filters, limit=per_query)
        except Exception:  # noqa: BLE001 — one bad sub-query must not sink the plan
            hits = []
        lst: list[str] = []
        for h in hits:
            by_id.setdefault(h.chunk_id, h)
            if h.chunk_id not in lst:
                lst.append(h.chunk_id)
        if lst:
            ranked.append(lst)
    if not ranked:
        return []
    fused = reciprocal_rank_fusion(ranked)
    return [by_id[cid] for cid, _score in fused[:top_k]]


def docs_read(hits: list) -> list[DocRead]:
    agg: dict[str, DocRead] = {}
    for h in hits:
        d = agg.get(h.doc_id)
        if d is None:
            agg[h.doc_id] = DocRead(doc_id=h.doc_id, company=h.company, period=h.period,
                                    doc_type=h.doc_type, title=" ".join((h.title or "").split()),
                                    hits=1)
        else:
            d.hits += 1
    return sorted(agg.values(), key=lambda d: (-d.hits, d.company, d.period or ""))


# --------------------------------------------------------------------------------------------
# Answer
# --------------------------------------------------------------------------------------------
def llm_ready(config: "dict | None") -> "llm.ProviderConfig | None":
    """The resolved provider when ``qa.enabled`` and its key is present, else ``None``."""
    qa = (config or {}).get("qa", {}) or {}
    if not qa.get("enabled"):
        return None
    return llm.resolve_provider(qa)


def thread_question(question: str, history: "list[tuple[str, str]] | None") -> str:
    """The synthesis question with earlier turns folded in (retrieval still uses the raw one)."""
    if not history:
        return question
    ctx = "\n".join(f"Q: {q}\nA: {a[:600]}" for q, a in history[-3:] if q)
    return f"{question}\n\n(Earlier in this thread, for context only:\n{ctx})"


def run_ask(question: str, retriever, *, config: "dict | None" = None, filters=None,
            history: "list[tuple[str, str]] | None" = None, company_label=None,
            doc_type_label=None, use_cache: bool = True) -> AskResult:
    """The whole pipeline: plan → retrieve → (LLM answer | evidence pack)."""
    qa = (config or {}).get("qa", {}) or {}
    top_k = int(qa.get("top_k", _DEFAULT_TOP_K))
    subs = decompose(question)
    plan = ResearchPlan(question=question,
                        scope=scope_chips(filters, company_label=company_label,
                                          doc_type_label=doc_type_label),
                        sub_queries=list(subs))
    hits = retrieve(subs, retriever, filters=filters, top_k=top_k)
    plan.docs_read = docs_read(hits)
    plan.chunks_read = len(hits)

    provider = llm_ready(config)
    if provider is None:
        plan.mode = "evidence"
        return AskResult(plan=plan, hits=hits, answer=None)

    plan.mode = "llm"
    plan.provider = f"{provider.provider} · {provider.model}"
    if not hits:
        return AskResult(plan=plan, hits=[], answer=rag.CitedAnswer(text=NOT_FOUND))
    q_for_llm = thread_question(question, history)
    prompt = build_user_prompt(q_for_llm, rag._number_snippets(hits))
    cache_on = bool(qa.get("cache", True)) and use_cache
    if cache_on:
        cached = llm_cache.get("qa", provider.model, SYSTEM_PROMPT + "\n" + prompt)
        if cached is not None and cached.get("text"):
            text = cached["text"]
            return AskResult(plan=plan, hits=hits, cached=True,
                             answer=rag.CitedAnswer(text=text,
                                                    citations=rag._extract_citations(text, hits)))
    try:
        answer = rag.answer(q_for_llm, hits, model=provider.model, provider=provider)
    except Exception as exc:  # noqa: BLE001 — network/SDK failure → evidence pack, say why
        plan.mode = "evidence"
        plan.provider = f"{plan.provider} (unreachable: {type(exc).__name__})"
        return AskResult(plan=plan, hits=hits, answer=None)
    if cache_on and answer.text.strip():
        llm_cache.put("qa", provider.model, SYSTEM_PROMPT + "\n" + prompt, {"text": answer.text})
    return AskResult(plan=plan, hits=hits, answer=answer)


def answer_segments(text: str) -> "list[tuple[str, int | None]]":
    """Split an answer into ``(text, None)`` runs and ``("", n)`` citation markers, in order."""
    out: list[tuple[str, int | None]] = []
    pos = 0
    for m in _MARKER_RE.finditer(text or ""):
        if m.start() > pos:
            out.append((text[pos:m.start()], None))
        out.append(("", int(m.group(1))))
        pos = m.end()
    if pos < len(text or ""):
        out.append((text[pos:], None))
    return out
