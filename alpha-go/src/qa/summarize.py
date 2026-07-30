"""Smart Summaries — extractive "key takeaways" of a document or a theme (optional LLM tier).

AlphaSense-style document summaries, built to the same two-tier shape as :mod:`src.qa.sentiment`
and :mod:`src.qa.rag`:

- **extractive** (default, deterministic, offline): split the source into sentences, drop
  boilerplate (reusing the corpus-frequency model from :mod:`src.search.fusion`), score each
  sentence for *salience* (semantic centrality + figure/financial signal + tonal magnitude +
  a lead-position prior), then greedily pick a non-redundant top-N (MMR). Every picked point
  carries its source ``doc_id``/offsets so the dashboard's "Read in context" viewer works
  exactly as it does for a search hit. Runs with no API key and no network.
- **llm** (optional): hand the extractively-selected sentences (numbered) to the provider and
  ask for a tight bulleted "key takeaways", mapping each ``[n]`` marker back to a real point —
  the same citation contract as :func:`src.qa.rag.answer`. Gated on the ``summary`` config
  block + a resolvable provider key; disk-cached; any failure degrades to the extractive summary.

Two pools feed the SAME extractive core: a single document's markdown (per-document summary) or
the passages of retrieved :class:`~src.search.retriever.Hit`\\ s (thematic / query-scoped summary).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

import numpy as np

from src.qa import llm, llm_cache
from src.qa.sentiment import score_sentiment
from src.search import fusion

# --- salience weights (sum to 1.0) --------------------------------------------------------
# Semantic centrality is primary (a takeaway sits near the document's/theme's center of mass),
# but a bare centroid over-selects fluent boilerplate — so a figure/financial-term signal and
# tonal magnitude co-drive, and a small lead prior reflects earnings releases front-loading the
# headline. Tuned on the eval TUNING split only (never the held-out set); see scripts/eval_summary.py.
_W_CENTRAL = 0.45
_W_FIGURE = 0.25
_W_TONE = 0.15
_W_LEAD = 0.15
_MMR_LAMBDA = 0.5        # redundancy penalty in the greedy selection (0 = ignore, 1 = pure novelty)

_MIN_SENT_CHARS = 25     # drop headers/fragments; a takeaway is a full clause
_MAX_POINT_CHARS = 400   # cap a single thematic candidate (a snippet window can be long)

_FIGURE_RE = re.compile(r"\d")
# A small bilingual set of "this sentence is about results" terms. Presence lifts a sentence over
# equally-central prose that carries no financial substance. Accent-folded to match _normalize_text.
_FINANCIAL_TERMS = frozenset(
    """revenue revenues sales margin margins ebitda income profit profits earnings growth net gross
    operating cash flow flows dividend dividends guidance outlook debt leverage volume volumes
    ventas ingresos margen utilidad utilidades crecimiento flujo deuda apalancamiento volumen
    ganancia ganancias resultado resultados rentabilidad""".split()
)

# Sentence boundary: terminal .!?… followed by whitespace/close-quote/end (so "1.5" and "S.A"
# mid-token dots don't split), OR one-or-more newlines (markdown paragraph/line breaks).
_SENT_END = re.compile(r"[.!?…]+(?=[\s\"')\]]|$)|\n+")


@dataclass
class SummaryPoint:
    """One salient sentence, with the provenance the doc viewer needs."""

    text: str
    doc_id: str
    title: str
    company: str
    period: str | None
    char_start: int              # offset of ``text`` in the source markdown (for the viewer)
    char_end: int
    markdown_path: str
    sentiment: str               # lexicon label of the point ("positive"/"neutral"/"negative")
    score: float                 # salience score (higher = more central/important)
    marker: int = 0              # the [n] this point carries in an LLM narrative (0 = extractive)


@dataclass
class Summary:
    points: list[SummaryPoint] = field(default_factory=list)
    engine: str = "extractive"   # "extractive" | "llm"
    scope: str = ""              # "document:<company>/<period>" | "thematic:<query>"
    narrative: str = ""          # LLM bullet text (empty for the extractive summary)


@dataclass
class _Candidate:
    text: str
    doc_id: str
    title: str
    company: str
    period: str | None
    char_start: int
    char_end: int
    markdown_path: str
    position: float              # 0.0 at the top of its document, 1.0 at the end (lead prior)


# --------------------------------------------------------------------------------------------
# Sentence splitting (offset-preserving: markdown[char_start:char_end] == sentence, exactly).
# --------------------------------------------------------------------------------------------
def split_sentences(text: str) -> "list[tuple[str, int, int]]":
    """``(sentence, char_start, char_end)`` triples over ``text``, offsets into ``text``.

    The round-trip ``text[char_start:char_end] == sentence`` holds for every triple (the slice is
    the stripped span, so the doc viewer can anchor on it). Fragments shorter than
    ``_MIN_SENT_CHARS`` are dropped.
    """
    out: "list[tuple[str, int, int]]" = []
    start = 0
    for m in _SENT_END.finditer(text):
        if m.group().startswith("\n"):
            seg_end, next_start = m.start(), m.end()   # newline delimiter: excluded from the sentence
        else:
            seg_end = next_start = m.end()             # keep the terminal punctuation
        _emit(out, text, start, seg_end)
        start = next_start
    _emit(out, text, start, len(text))
    return out


def _emit(out: list, text: str, lo: int, hi: int) -> None:
    seg = text[lo:hi]
    stripped = seg.strip()
    if len(stripped) < _MIN_SENT_CHARS:
        return
    cs = lo + (len(seg) - len(seg.lstrip()))
    out.append((stripped, cs, cs + len(stripped)))


# --------------------------------------------------------------------------------------------
# Candidate builders — the two pools that feed the shared extractive core.
# --------------------------------------------------------------------------------------------
def _candidates_from_text(text: str, *, doc_id: str, title: str, company: str,
                          period: "str | None", markdown_path: str) -> "list[_Candidate]":
    sents = split_sentences(text)
    n = max(len(sents), 1)
    return [
        _Candidate(text=s, doc_id=doc_id, title=title, company=company, period=period,
                   char_start=cs, char_end=ce, markdown_path=markdown_path, position=i / n)
        for i, (s, cs, ce) in enumerate(sents)
    ]


def _candidates_from_hits(hits: list) -> "list[_Candidate]":
    """One candidate per hit — the hit's snippet text, anchored at its chunk offsets.

    Thematic mode summarizes across documents, so a point's provenance is the hit's chunk (its
    ``markdown_path`` + ``char_start/char_end`` drive the same doc viewer); the display text is the
    hit's snippet, whitespace-collapsed and length-capped. Near-duplicate snippets across
    periods/filers are collapsed downstream by the shared dedupe signature.
    """
    out: "list[_Candidate]" = []
    for h in hits:
        text = re.sub(r"\s+", " ", (h.snippet.text or "").strip())[:_MAX_POINT_CHARS]
        if len(text) < _MIN_SENT_CHARS:
            continue
        out.append(_Candidate(
            text=text, doc_id=h.doc_id, title=h.title, company=h.company, period=h.period,
            char_start=h.char_start, char_end=h.char_end, markdown_path=h.markdown_path,
            position=0.0,   # ranked pool has no within-doc position; lead prior is neutral here
        ))
    return out


# --------------------------------------------------------------------------------------------
# Scoring + selection.
# --------------------------------------------------------------------------------------------
def _is_boilerplate(cand: "_Candidate", model) -> bool:
    if model is not None:
        try:
            return model.is_boilerplate(cand.text)
        except Exception:  # noqa: BLE001 — precision aid, never fatal
            return False
    return fusion._is_boilerplate(cand.text)   # curated-signature fallback


def _figure_signal(text: str) -> float:
    """0..1 "is this about results" signal: a figure present, plus up to two financial terms."""
    toks = fusion._normalize_text(text).split()
    fin = sum(1 for t in toks if t in _FINANCIAL_TERMS)
    return min(1.0, 0.5 * (1 if _FIGURE_RE.search(text) else 0) + 0.25 * min(fin, 2))


def _embed_normalized(embedder, texts: list) -> "np.ndarray | None":
    """L2-normalized embedding matrix for ``texts`` (or ``None`` when no embedder)."""
    if embedder is None or not texts:
        return None
    try:
        vecs = np.asarray(embedder.encode(texts), dtype="float32")
    except Exception:  # noqa: BLE001 — semantic centrality is optional
        return None
    if vecs.ndim != 2 or vecs.shape[0] != len(texts):
        return None
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def _score(cands: list, mat, anchor) -> list:
    scores = []
    for i, c in enumerate(cands):
        central = (float(mat[i] @ anchor) + 1.0) / 2.0 if (mat is not None and anchor is not None) else 0.0
        s = (_W_CENTRAL * central + _W_FIGURE * _figure_signal(c.text)
             + _W_TONE * abs(score_sentiment(c.text).score) + _W_LEAD * (1.0 - c.position))
        scores.append(s)
    return scores


def _select(cands: list, mat, scores: list, k: int) -> list:
    """Greedy MMR pick of ``k`` indices: highest salience, penalized by redundancy to picks.

    Redundancy is semantic cosine to already-selected points (when an embedding matrix is
    available) plus an exact near-duplicate guard on the leading-token signature (so the same
    boilerplate line repeated across a document never appears twice). Fully deterministic:
    ties break toward the earlier sentence, then the earlier candidate index.
    """
    remaining = list(range(len(cands)))
    selected: list = []
    seen_sigs: set = set()
    while remaining and len(selected) < k:
        best_i = None
        best_key = None
        for i in remaining:
            redundancy = max((float(mat[i] @ mat[j]) for j in selected), default=0.0) if mat is not None else 0.0
            val = scores[i] - _MMR_LAMBDA * redundancy
            key = (val, -cands[i].char_start, -i)
            if best_key is None or key > best_key:
                best_key, best_i = key, i
        remaining.remove(best_i)
        sig = fusion._dedupe_signature(cands[best_i].text)
        if sig and sig in seen_sigs:
            continue                       # redundant copy of an already-picked passage
        if sig:
            seen_sigs.add(sig)
        selected.append(best_i)
    return selected


def _summarize_core(cands: list, *, embedder, boilerplate, anchor_vec, max_sentences: int,
                    scope: str, doc_order: bool) -> Summary:
    cands = [c for c in cands if not _is_boilerplate(c, boilerplate)]
    if not cands:
        return Summary(points=[], engine="extractive", scope=scope)

    mat = _embed_normalized(embedder, [c.text for c in cands])
    if anchor_vec is not None and mat is not None:
        anchor = np.asarray(anchor_vec, dtype="float32")
        nrm = float(np.linalg.norm(anchor))
        anchor = anchor / nrm if nrm else anchor
        anchor = anchor if anchor.shape[0] == mat.shape[1] else None   # dim mismatch → skip centrality
    elif mat is not None:
        anchor = mat.mean(axis=0)          # per-document centroid
        nrm = float(np.linalg.norm(anchor))
        anchor = anchor / nrm if nrm else anchor
    else:
        anchor = None

    scores = _score(cands, mat, anchor)
    picked = _select(cands, mat, scores, max_sentences)
    # Per-document summary reads top-to-bottom; a thematic summary reads most-salient first.
    picked.sort(key=lambda i: cands[i].char_start if doc_order else -scores[i])

    points = [
        SummaryPoint(
            text=cands[i].text, doc_id=cands[i].doc_id, title=cands[i].title,
            company=cands[i].company, period=cands[i].period, char_start=cands[i].char_start,
            char_end=cands[i].char_end, markdown_path=cands[i].markdown_path,
            sentiment=score_sentiment(cands[i].text).label, score=round(scores[i], 4),
        )
        for i in picked
    ]
    return Summary(points=points, engine="extractive", scope=scope)


# --------------------------------------------------------------------------------------------
# Public extractive entrypoints.
# --------------------------------------------------------------------------------------------
def summarize_document(text: str, *, doc_id: str = "", title: str = "", company: str = "",
                       period: "str | None" = None, markdown_path: str = "", embedder=None,
                       boilerplate=None, max_sentences: int = 6) -> Summary:
    """Extractive key-takeaways of a single document's markdown (offline, deterministic)."""
    cands = _candidates_from_text(text, doc_id=doc_id, title=title, company=company,
                                  period=period, markdown_path=markdown_path)
    return _summarize_core(cands, embedder=embedder, boilerplate=boilerplate, anchor_vec=None,
                           max_sentences=max_sentences,
                           scope=f"document:{company}/{period or '—'}", doc_order=True)


def summarize_hits(hits: list, *, query: str = "", embedder=None, boilerplate=None,
                   query_vec=None, max_sentences: int = 6) -> Summary:
    """Extractive thematic summary over retrieved hits, anchored on the query's meaning."""
    if query_vec is None and query and embedder is not None:
        query_vec = _embed_normalized(embedder, [query])
        query_vec = query_vec[0] if query_vec is not None else None
    cands = _candidates_from_hits(hits)
    return _summarize_core(cands, embedder=embedder, boilerplate=boilerplate, anchor_vec=query_vec,
                           max_sentences=max_sentences, scope=f"thematic:{query}", doc_order=False)


# --------------------------------------------------------------------------------------------
# Opt-in LLM tier — same citation contract as rag.answer.
# --------------------------------------------------------------------------------------------
_LLM_SYSTEM = (
    "You write a concise KEY TAKEAWAYS summary of a company earnings document. You are given "
    "numbered source passages. Produce 3-6 short bullets capturing the most important facts "
    "(results, drivers, guidance, notable events). Each bullet MUST cite the passage(s) it draws "
    "from with their [n] marker(s). Use ONLY the given passages — never invent figures or claims. "
    "Spanish and English passages are summarized in the passages' own language. Answer with the "
    "bullets only, one per line beginning with '- '."
)
_LLM_MAX_TOKENS = 512


def llm_enabled(config: "dict | None") -> bool:
    """True when the ``summary`` config opts into the LLM tier AND the provider key resolves."""
    block = (config or {}).get("summary", {})
    if block.get("engine") not in ("llm", "hybrid"):
        return False
    return llm.resolve_provider(block) is not None


def summarize_llm(base: Summary, config: "dict | None", *,
                  provider: "llm.ProviderConfig | None" = None) -> Summary:
    """Upgrade an extractive ``base`` to an LLM "key takeaways" narrative, or return ``base``.

    Gated on ``summary.engine`` being ``llm``/``hybrid`` and a resolvable provider. The base
    points are numbered and sent to the provider; ``[n]`` markers in the reply map back to the
    real points (out-of-range dropped, exactly as ``rag.answer``). Result disk-cached under the
    ``summary`` namespace. Any import/API failure degrades to the extractive ``base``.
    """
    block = (config or {}).get("summary", {})
    if block.get("engine") not in ("llm", "hybrid"):
        return base
    provider = provider or llm.resolve_provider(block)
    if provider is None or not base.points:
        return base

    numbered = [f"[{i}] ({p.company.upper()} · {p.period or '—'}) {p.text}"
                for i, p in enumerate(base.points, start=1)]
    excerpt = "\n".join(numbered)
    use_cache = block.get("cache", True)
    if use_cache:
        cached = llm_cache.get("summary", provider.model, excerpt)
        if cached is not None:
            return _from_narrative(base, cached.get("text", ""))
    try:
        text = llm.complete(provider, system=_LLM_SYSTEM,
                            user="Passages:\n" + excerpt + "\n\nWrite the key takeaways.",
                            max_tokens=_LLM_MAX_TOKENS)
    except Exception:  # noqa: BLE001 — network/SDK failure falls back to extractive
        return base
    if not (text or "").strip():
        return base
    if use_cache:
        llm_cache.put("summary", provider.model, excerpt, {"text": text})
    return _from_narrative(base, text)


_MARKER_RE = re.compile(r"\[(\d+)\]")


def _from_narrative(base: Summary, narrative: str) -> Summary:
    """Build an LLM Summary: keep only the cited points, tagged with their [n] marker."""
    markers = sorted({int(m) for m in _MARKER_RE.findall(narrative)})
    pts = [replace(base.points[m - 1], marker=m) for m in markers if 1 <= m <= len(base.points)]
    return Summary(points=pts or base.points, engine="llm", scope=base.scope,
                   narrative=narrative.strip())
