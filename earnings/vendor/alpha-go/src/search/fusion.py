"""Rank fusion — combine keyword (BM25) and semantic (cosine) result lists.

Default: Reciprocal Rank Fusion (RRF), which is robust to the two scorers being on
different scales (BM25 magnitude vs cosine in [-1, 1]). A tunable ``weights`` lets the UI
bias toward keyword or semantic.

    rrf_score(chunk) = sum_over_lists( weight_l / (k + rank_l(chunk)) )

Status: SCAFFOLD. ``reciprocal_rank_fusion`` is stubbed (Phase 3).
"""
from __future__ import annotations

import re
from collections import defaultdict

RRF_K = 60

# --------------------------------------------------------------------------------------------
# Analog-band precision: near-duplicate + boilerplate suppression and topical rerank.
#
# The independent precision judge found analog synonym-expansion noise is dominated by two
# failure modes (NOT wrong-sense — the polysemy gate already handles that):
#   1. NEAR-DUPLICATES — the same passage repeated across periods/filings (e.g. FEMSA's
#      "Water = Still bottled water in 5.0/19.0/20.0-liter packaging presentations" glossary line
#      surfacing 4x, or the "About Grupo Bimbo … bakeries and plants" blurb 5x). One is enough.
#   2. COMPANY BOILERPLATE / FOOTERS — self-description, conference-call logistics, share-unit
#      definitions, DJSI-membership blurbs — that share ONE fired alias token but carry no topical
#      substance for the query.
# Both are demoted/dropped ONLY within the analog (strong/weak) bands — never for literal-query
# or semantic hits — so expansion breadth is kept while coincidental token matches sink or drop.
# --------------------------------------------------------------------------------------------
_WS = re.compile(r"\s+")
_NONALNUM = re.compile(r"[^a-z0-9 ]+")
_SIM_FLOOR = -1.0        # below any valid cosine; floors a candidate that has no embedding

# Footer/boilerplate signatures (matched against normalized text: lowercased, punctuation
# stripped, whitespace collapsed). Curated from the judged noise pool. A boilerplate chunk is
# only DROPPED when it ALSO has zero topical overlap with the query (see
# :func:`analyze_analog_candidates`), so a self-description that genuinely answers the query
# (e.g. the "bakeries and plants" blurb for a bakery/plant-closure query) is kept — a signature
# match alone never drops a hit.
_BOILERPLATE_PATTERNS = tuple(re.compile(p) for p in (
    r"\babout grupo bimbo\b",
    r"grupo bimbo is the leader and largest baking company",
    r"\bgrupo bimbo s a b de c v\b",
    r"\bconference call\b",
    r"\breplay of the\b",
    r"united packaging",
    r"penn paper supply",
    r"delta packaging",
    r"member of the dow jones sustainability",
    r"\bfemsa units?\b",
    r"each femsa bd unit",
    r"forward looking statements",
    r"\bsafe harbor\b",
    r"\binvestor relations\b",
))


def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — basis for dup signatures + matching."""
    return _WS.sub(" ", _NONALNUM.sub(" ", (text or "").lower())).strip()


def _dedupe_signature(text: str, n_tokens: int = 16) -> str:
    """A near-duplicate key: the first ``n_tokens`` normalized tokens joined.

    Two chunks whose leading window is identical after normalization are treated as the same
    passage (period/OCR variants collapse). Empty text yields an empty signature (never dupes).
    """
    return " ".join(_normalize_text(text).split()[:n_tokens])


def _is_boilerplate(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(p.search(normalized) for p in _BOILERPLATE_PATTERNS)


# --------------------------------------------------------------------------------------------
# STATISTICAL boilerplate — a corpus-frequency detector that supersedes the curated signatures.
#
# The curated `_BOILERPLATE_PATTERNS` above are a hand-maintained fallback. The live retriever
# instead learns boilerplate FROM THE CORPUS: a passage whose word-shingles recur verbatim across
# MANY documents (forward-looking-statement disclaimers, corporate-name safe-harbor headers,
# glossary/definition blocks, conference-call logistics) is boilerplate — no signature list needed
# and it generalizes to any filer. Built once over the whole corpus, then a chunk is boilerplate
# when a large fraction of its shingles are "common" (document-frequency >= ``min_docs``). As with
# the curated path, a boilerplate chunk is only DROPPED when it ALSO has zero topical overlap with
# the query, so a recurring block that genuinely answers the query still survives.
# --------------------------------------------------------------------------------------------
_BOILERPLATE_SHINGLE_N = 8          # window length; long enough that content rarely collides
_BOILERPLATE_MIN_DOCS = 10          # a shingle in >= this many distinct docs is "common"
_BOILERPLATE_MIN_FRAC = 0.5         # chunk is boilerplate when >= half its shingles are common


def _shingles(text: str, n: int) -> "list[str]":
    """The ``n``-token normalized shingles of ``text`` (empty when shorter than ``n`` tokens)."""
    toks = _normalize_text(text).split()
    if len(toks) < n:
        return []
    return [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]


class CorpusBoilerplate:
    """Corpus-frequency boilerplate model: flags chunks built mostly of high-document-frequency
    shingles. Constructed via :func:`build_corpus_boilerplate`; ``is_boilerplate`` classifies any
    text (a corpus chunk or a fresh snippet) with no further corpus access."""

    __slots__ = ("common", "n", "min_frac")

    def __init__(self, common: "set[str]", n: int, min_frac: float):
        self.common = common
        self.n = n
        self.min_frac = min_frac

    def is_boilerplate(self, text: str) -> bool:
        shs = _shingles(text, self.n)
        if not shs:
            return False
        common_hits = sum(1 for sh in shs if sh in self.common)
        return (common_hits / len(shs)) >= self.min_frac


def build_corpus_boilerplate(
    doc_texts: "list[tuple[str, str]]",
    *,
    shingle_n: int = _BOILERPLATE_SHINGLE_N,
    min_docs: int = _BOILERPLATE_MIN_DOCS,
    min_frac: float = _BOILERPLATE_MIN_FRAC,
) -> "CorpusBoilerplate":
    """Learn corpus boilerplate from ``(doc_id, text)`` chunk rows (pure; no DB dependency).

    Computes each shingle's DOCUMENT frequency (distinct doc_ids it appears in) and keeps those
    at/above ``min_docs`` as the "common" set. The returned model flags a chunk whose shingles are
    ``min_frac``-or-more common. Deterministic and reusable across queries — the retriever builds
    it once per index and caches it.
    """
    df: "dict[str, set[str]]" = defaultdict(set)
    for doc_id, text in doc_texts:
        for sh in set(_shingles(text, shingle_n)):
            df[sh].add(doc_id)
    common = {sh for sh, docs in df.items() if len(docs) >= min_docs}
    return CorpusBoilerplate(common, shingle_n, min_frac)


def topical_fit(text: str, query_terms: "list[str]") -> int:
    """Count of DISTINCT query content terms present in ``text`` (prefix-5 stem match).

    Measures "topical substance beyond the single fired alias": a coincidental one-token analog
    match scores ~0-1, while a passage echoing several query words scores higher. Matching is
    exact for short terms and prefix-5 for terms of length >=5 (so bakery<->bakeries,
    plant<->plants, sweetener<->sweeteners align without a full stemmer).
    """
    toks = set(_normalize_text(text).split())
    prefixes = {t[:5] for t in toks if len(t) >= 5}
    hits = 0
    for q in query_terms:
        if q in toks or (len(q) >= 5 and q[:5] in prefixes):
            hits += 1
    return hits


def rerank_sort_key(m: dict) -> tuple:
    """The shared analog-rerank ordering (best first), used by BOTH the retriever and the scorer.

    Lexical topical ``fit`` (distinct query terms echoed) is the PRIMARY signal, AUGMENTED by the
    MiniLM semantic cosine (``sim``, chunk vs the FULL query) as the tiebreak that separates
    on-topic from coincidental passages sharing the same lexical fit; boilerplate is demoted, and
    original best-first order breaks final ties. Without a sim (embedder unavailable) it degrades
    to the pure lexical order. This ordering was selected on the eval TUNING split: lexical-primary
    + semantic tiebreak beat both pure-lexical and semantic-primary at precision@5 (see
    scripts/eval_analogs.py --score and the track-b-improve2 report).
    """
    sim = m.get("sim")
    if sim is None:
        return (-m["fit"], m["boiler"], m["order"])
    return (-m["fit"], -sim, m["boiler"], m["order"])


def analyze_analog_candidates(
    items: "list[tuple]",
    query_terms: "list[str]",
    *,
    sim_scores: "dict | None" = None,
    is_boilerplate=None,
) -> dict:
    """Per-candidate dedupe / boilerplate / topical / semantic metadata for analog-only hits.

    ``items`` = ``(key, text)`` tuples in best-first (bm25/fused) order. Returns
    ``{key: {"drop", "fit", "boiler", "order", "sim"}}`` where ``drop`` is True for a later
    near-duplicate of an already-kept passage, or a boilerplate/footer chunk with zero topical
    overlap. ``fit`` (lexical overlap) and ``sim`` (semantic cosine to the full query, from
    ``sim_scores[key]`` when supplied) drive the rerank; ``boiler`` demotes on ties. The first
    occurrence of a signature is always kept (a relevant passage is never fully lost); only its
    redundant copies drop.

    ``is_boilerplate`` is an optional ``(key, text) -> bool`` predicate (the corpus-frequency
    model the retriever builds); when omitted the curated-signature fallback is used.
    """
    seen_sigs: set = set()
    out: dict = {}
    for i, (key, text) in enumerate(items):
        sig = _dedupe_signature(text)
        fit = topical_fit(text, query_terms)
        boiler = is_boilerplate(key, text) if is_boilerplate is not None else _is_boilerplate(text)
        drop = False
        if sig and sig in seen_sigs:
            drop = True                        # redundant copy of an earlier, better-ranked hit
        elif boiler and fit == 0:
            drop = True                        # pure footer/boilerplate, no query substance
        if not drop and sig:
            seen_sigs.add(sig)
        out[key] = {
            "drop": drop, "fit": fit, "boiler": boiler, "order": i,
            # When sims are supplied, a candidate missing an embedding floors to _SIM_FLOOR so the
            # sort keys stay homogeneous floats (never mixed None/float within one rerank call).
            "sim": (sim_scores.get(key, _SIM_FLOOR) if sim_scores is not None else None),
        }
    return out


def dedupe_rerank_analog(
    items: "list[tuple]",
    query_terms: "list[str]",
    *,
    sim_scores: "dict | None" = None,
    is_boilerplate=None,
) -> "tuple[list, list]":
    """Dedupe + semantic/topical rerank of analog-only ``(key, text)`` items → ``(kept, dropped)``.

    Survivors are reordered by :func:`rerank_sort_key` (semantic cosine primary when ``sim_scores``
    is supplied, else lexical topical fit), boilerplate demoted, original best-first order as the
    final tiebreak. Thin wrapper over :func:`analyze_analog_candidates` for callers that just want
    the reordered key lists.
    """
    meta = analyze_analog_candidates(
        items, query_terms, sim_scores=sim_scores, is_boilerplate=is_boilerplate,
    )
    kept = [k for k, _ in items if not meta[k]["drop"]]
    kept.sort(key=lambda k: rerank_sort_key(meta[k]))
    dropped = [k for k, _ in items if meta[k]["drop"]]
    return kept, dropped


def reciprocal_rank_fusion(
    ranked_lists: "list[list[str]]",
    *,
    weights: "list[float] | None" = None,
    k: int = RRF_K,
) -> "list[tuple[str, float]]":
    """Fuse several ranked id-lists into one ``(chunk_id, score)`` list, best first.

    Each list is assumed already ordered best-first. An id at 0-based rank ``r`` in list
    ``l`` contributes ``weights[l] / (k + r + 1)`` to its fused score; contributions across
    lists sum. Ties break by ``chunk_id`` (ascending) so the ordering is fully deterministic.
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    if len(weights) != len(ranked_lists):
        raise ValueError("weights length must match ranked_lists length")

    scores: dict[str, float] = {}
    for weight, ids in zip(weights, ranked_lists):
        for rank, chunk_id in enumerate(ids):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank + 1)

    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def cap_analog_floods(
    candidates: "list[tuple]",
    *,
    concept_cap: int,
    company_cap: int,
) -> "list":
    """Demote synonym-only hits that over-represent one concept or company to the result tail.

    A pure reranker guarding against the classic analog failure mode: one fired concept (or one
    verbose filer) flooding the page with loosely-related passages that bury the literal-query
    answer. Only *analog-only* hits — surfaced by synonym expansion but NOT by the literal query
    or the semantic half — are subject to caps; those keep their fused order until a concept or
    company hits its cap, after which further ones sink to the tail (kept, not dropped — the
    caller truncates to the page limit). Literal/semantic hits are never demoted.

    ``candidates`` are ``(key, company, concepts, analog_only)`` tuples in fused order, where
    ``concepts`` is a set (empty for non-analog hits). Returns the reordered list of ``key``\\ s.
    """
    kept: list = []
    demoted: list = []
    per_concept: dict = {}
    per_company: dict = {}
    for key, company, concepts, analog_only in candidates:
        if not analog_only:
            kept.append(key)
            continue
        over = per_company.get(company, 0) >= company_cap or any(
            per_concept.get(c, 0) >= concept_cap for c in concepts
        )
        if over:
            demoted.append(key)
            continue
        per_company[company] = per_company.get(company, 0) + 1
        for c in concepts:
            per_concept[c] = per_concept.get(c, 0) + 1
        kept.append(key)
    return kept + demoted
