"""KeywordIndex — BM25 keyword search over chunks via SQLite FTS5 (stdlib only).

Query expansion can reuse the vendored deterministic synonym dictionary
(src.extract.semantic_search + configs/metric_search.yaml) so a search for "EBITDA"
also matches "operating cash flow proxy" aliases, AlphaSense "Smart Synonyms" style.
"""
from __future__ import annotations

import functools
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from src.index.store import IndexStore

_log = logging.getLogger(__name__)

# Analog expansion phrases at/above this weight are "strong"; below it, "weak". The retriever
# fuses the two bands at different weights, so a per-alias weight in analogs.yaml (e.g. a bare
# risky token demoted to 0.5) materially changes ranking instead of flooding one flat OR list.
# Unweighted metric/analog aliases default to 1.0 (strong).
_STRONG_MIN = 0.75

_WORD = re.compile(r"\w+", re.UNICODE)
# Spanish + English function words: dropped from FTS queries (BM25 idf already down-weights
# them, but excluding them keeps the OR query tight and avoids zero-signal terms).
_STOPWORDS = frozenset({
    "de", "del", "la", "las", "el", "los", "un", "una", "y", "o", "en", "por", "para",
    "con", "se", "su", "sus", "al", "lo", "que", "a",
    "the", "of", "and", "or", "in", "on", "for", "to", "with", "by", "is", "are",
    "it", "its", "at", "as", "an", "was", "were",
})


@functools.lru_cache(maxsize=1)
def _bilingual_equivalences() -> tuple[tuple[str, ...], ...]:
    """Load the small, reviewable ES/EN exact-equivalence registry.

    This intentionally lives apart from the broader metric/analog dictionaries: an entry here
    means that a phrase in either language is a direct search equivalent and may be counted in
    the bilingual Command-F lane.  It is not a corpus-learned or embedding-derived expansion.
    """
    from src.extract.semantic_search import load_search_dictionary
    from src.shared.paths import CONFIGS_DIR

    path = Path(CONFIGS_DIR) / "bilingual_search.yaml"
    if not path.exists():
        return ()
    try:
        registry = load_search_dictionary(path)
    except Exception as exc:  # noqa: BLE001 — a bad optional registry must not break search
        _log.warning("bilingual_search.yaml failed to load: %s", exc)
        return ()
    return tuple(aliases for aliases in registry.concept_aliases.values() if aliases)


def query_terms(query: str) -> list[str]:
    """Content tokens of a free-text query (lowercased, stopwords/length-1 removed)."""
    return [t for t in _WORD.findall((query or "").lower()) if len(t) > 1 and t not in _STOPWORDS]


def query_phrase(query: str) -> str:
    """Normalized literal query phrase, or ``""`` for empty/stopword-only input."""
    phrase = re.sub(r"\s+", " ", re.sub(r"[^\w%\s]+", " ", (query or "").lower())).strip()
    return phrase if phrase and query_terms(query) else ""


def bilingual_equivalent_phrases(query: str) -> list[str]:
    """Direct ES/EN equivalents of a query, excluding its literal wording.

    Equivalence only fires when the complete normalized query is an approved registry alias;
    this prevents a generic word embedded in a longer request from unexpectedly widening it.
    """
    literal = query_phrase(query)
    if not literal:
        return []
    folded = literal.casefold()
    for aliases in _bilingual_equivalences():
        if folded in {alias.casefold() for alias in aliases}:
            return [alias for alias in aliases if alias.casefold() != folded]
    return []


def bilingual_search_phrases(query: str) -> list[str]:
    """Literal query plus its approved direct Spanish/English equivalents."""
    literal = query_phrase(query)
    if not literal:
        return []
    return list(dict.fromkeys([literal, *bilingual_equivalent_phrases(query)]))


@functools.lru_cache(maxsize=1)
def _dictionary():
    """Synonym/analog dictionary, parsed once per process.

    Unions two sources under one ``concept_aliases`` map:
    - the vendored **metric** dictionary (``configs/metric_search.yaml``) — financial concepts;
    - the search-only **analog** dictionary (``configs/analogs.yaml``, optional) — domain/material
      vocabulary (e.g. pet ↔ plastic ↔ resin ↔ packaging) kept OUT of the extraction dictionary so
      it never affects metric matching. Both share the same YAML shape, so the same loader parses
      both; analog concepts are namespaced (``analog::<name>``) to avoid clobbering metric concepts.

    Reused by keyword FTS expansion (``synonym_phrases``/``fts_match_query``), Trends
    (``mention_trend``), and the UI highlighter — so an analog added to ``analogs.yaml`` flows to
    all three with no further code change.
    """
    import dataclasses

    from src.extract.semantic_search import load_search_dictionary
    from src.shared.paths import CONFIGS_DIR

    base = load_search_dictionary()
    analog_path = CONFIGS_DIR / "analogs.yaml"
    if not analog_path.exists():
        return base
    try:
        extra = load_search_dictionary(analog_path)
    except Exception as exc:  # noqa: BLE001 — a malformed analog file must not break search
        _log.warning(
            "analogs.yaml failed to load (%s); analog query expansion is DISABLED "
            "until it parses. Fix configs/analogs.yaml.", exc,
        )
        return base
    merged = dict(base.concept_aliases)
    for name, aliases in extra.concept_aliases.items():
        merged[f"analog::{name}"] = aliases
    return dataclasses.replace(base, concept_aliases=merged)


@dataclass(frozen=True)
class _Gate:
    """Polysemy guard for one analog concept (from ``analogs.yaml``'s ``gate:`` block).

    All terms are normalized (accent/case/punctuation-insensitive) on load.

    - ``guarded``: aliases that must NOT trigger expansion on their own — a bare risky token
      (e.g. "pet", "sugar", "planta"). They fire only when corroborated by ``require_any``.
    - ``require_any``: co-terms that corroborate a guarded alias (e.g. "pet" only expands the
      resin/plastic family when "bottle"/"resin"/"packaging" is also in the query).
    - ``suppress_if_any``: other-sense signals that VETO the concept entirely (e.g. "pet food",
      "sugar-free", "water treatment") — the veto wins even if a corroborator is present.
    """

    guarded: frozenset
    require_any: tuple
    suppress_if_any: tuple


@dataclass(frozen=True)
class _AnalogMeta:
    default_weight: float
    alias_weights: dict          # normalized alias -> weight
    gate: "_Gate | None"


def _as_weight(value, concept: str, default: float = 1.0) -> float:
    try:
        w = float(value)
    except (TypeError, ValueError):
        _log.warning("analogs.yaml: concept %r has non-numeric weight %r; using %.2f",
                     concept, value, default)
        return default
    if w < 0:
        _log.warning("analogs.yaml: concept %r has negative weight %r; clamping to 0", concept, value)
        return 0.0
    return w


def _norm_terms(values, *, concept: str, field: str) -> tuple:
    from src.extract.semantic_search import normalize_label

    if values is None:
        return ()
    if not isinstance(values, (list, tuple)):
        _log.warning("analogs.yaml: concept %r gate.%s is not a list; ignored", concept, field)
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        n = normalize_label(str(v))
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return tuple(out)


@functools.lru_cache(maxsize=1)
def _analog_meta() -> dict:
    """Per-concept weight + polysemy-gate metadata parsed from ``analogs.yaml``.

    This is the *data-driven* disambiguation spec: the ``risk:`` notes documented per concept
    are encoded here as ``gate:`` blocks and per-alias ``weight``. Kept separate from
    :func:`load_search_dictionary` (which only reads ``aliases``) so extra keys never touch the
    extraction dictionary. Returns ``{concept_name: _AnalogMeta}``; a malformed file logs a
    warning and degrades to no gating (aliases still expand at default weight).
    """
    from src.extract.semantic_search import normalize_label
    from src.shared.paths import CONFIGS_DIR

    path = CONFIGS_DIR / "analogs.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        _log.warning("analogs.yaml unreadable (%s); analog gating/weighting DISABLED", exc)
        return {}
    concepts = data.get("concepts") if isinstance(data, dict) else None
    if not isinstance(concepts, dict):
        _log.warning("analogs.yaml: 'concepts' missing or not a mapping; analog gating DISABLED")
        return {}

    out: dict[str, _AnalogMeta] = {}
    for name, raw in concepts.items():
        if not isinstance(raw, dict):
            _log.warning("analogs.yaml: concept %r is not a mapping; skipped", name)
            continue
        default_w = _as_weight(raw.get("weight", 1.0), str(name))
        alias_weights: dict[str, float] = {}
        aw_raw = raw.get("alias_weights") or {}
        if isinstance(aw_raw, dict):
            for alias, w in aw_raw.items():
                na = normalize_label(str(alias))
                if na:
                    alias_weights[na] = _as_weight(w, str(name), default_w)
        elif aw_raw:
            _log.warning("analogs.yaml: concept %r alias_weights is not a mapping; ignored", name)

        gate = None
        graw = raw.get("gate")
        if isinstance(graw, dict):
            gate = _Gate(
                guarded=frozenset(_norm_terms(graw.get("guarded_aliases"),
                                              concept=str(name), field="guarded_aliases")),
                require_any=_norm_terms(graw.get("require_any"),
                                        concept=str(name), field="require_any"),
                suppress_if_any=_norm_terms(graw.get("suppress_if_any"),
                                            concept=str(name), field="suppress_if_any"),
            )
        elif graw is not None:
            _log.warning("analogs.yaml: concept %r gate is not a mapping; ignored", name)
        out[str(name)] = _AnalogMeta(default_w, alias_weights, gate)
    return out


def _fires(term_norm: str, query_norm: str) -> bool:
    """True if ``term_norm`` appears as a whole word/phrase in the normalized query."""
    return bool(term_norm) and bool(
        re.search(rf"(?:^|\s){re.escape(term_norm)}(?:\s|$)", query_norm)
    )


def _gate_allows(gate: _Gate, fired: list[str], query_norm: str) -> bool:
    """Whether an analog concept may expand given which of its aliases fired.

    Veto first (``suppress_if_any`` = the other sense present) → never expand. Otherwise a
    non-guarded alias fires freely; a guarded-only match needs a ``require_any`` corroborator.
    """
    if any(_fires(s, query_norm) for s in gate.suppress_if_any):
        return False
    if any(a not in gate.guarded for a in fired):    # an unambiguous alias fired
        return True
    if not gate.require_any:                          # guarded alias, but no corroboration required
        return True
    return any(_fires(c, query_norm) for c in gate.require_any)


def _weighted_synonyms_with_source(query: str) -> list[tuple[str, float, str, str]]:
    """Internal synonym expansion including ``metric``/``analog`` provenance.

    Unions the vendored **metric** dictionary and the search-only **analog** dictionary
    (AlphaSense "Smart Synonyms" style). For each concept whose alias appears as a whole phrase
    in the query, every alias is offered as an extra term — but analog concepts are first run
    through their ``gate`` (polysemy guard) and each alias carries its per-alias ``weight`` so a
    risky bare token contributes less than an unambiguous phrase. Metric concepts and un-gated
    analogs expand as before (default weight 1.0). Best-effort: any failure yields no expansion.
    """
    try:
        from src.extract.semantic_search import normalize_label
    except Exception as exc:  # noqa: BLE001 — expansion is optional, never fatal
        _log.warning("normalize_label unavailable (%s); no query expansion", exc)
        return []
    norm = normalize_label(query)
    if not norm:
        return []
    try:
        dictionary = _dictionary()
    except Exception as exc:  # noqa: BLE001
        _log.warning("synonym dictionary unavailable (%s); no query expansion", exc)
        return []
    meta = _analog_meta()

    best: dict[str, tuple[float, str, str]] = {}  # phrase -> (weight, concept, source)
    for concept, aliases in dictionary.concept_aliases.items():
        fired = [a for a in aliases if _fires(a, norm)]
        if not fired:
            continue
        source = "analog" if concept.startswith("analog::") else "metric"
        display = concept[len("analog::"):] if source == "analog" else concept
        cmeta = meta.get(display)
        if cmeta and cmeta.gate and not _gate_allows(cmeta.gate, fired, norm):
            continue
        default_w = cmeta.default_weight if cmeta else 1.0
        alias_weights = cmeta.alias_weights if cmeta else {}
        for alias in aliases:
            w = alias_weights.get(alias, default_w)
            prev = best.get(alias)
            # Equal-weight collisions prefer the metric dictionary: metric aliases are direct
            # translations/names for a financial concept, while analog aliases are deliberately
            # kept in the separately labelled Analogs lane.
            if (prev is None or w > prev[0]
                    or (w == prev[0] and source == "metric" and prev[2] == "analog")):
                best[alias] = (w, display, source)
    return [(phrase, w, c, source) for phrase, (w, c, source) in best.items()]


def weighted_synonyms(query: str) -> list[tuple[str, float, str]]:
    """Gated, weighted synonym expansion for ``query``: ``(phrase, weight, concept)`` tuples."""
    return [(phrase, weight, concept)
            for phrase, weight, concept, _source in _weighted_synonyms_with_source(query)]


def synonym_phrases(query: str) -> list[str]:
    """Gated synonym phrases for ``query`` (weights dropped), highest-weight first.

    Backward-compatible flat list used by :func:`fts_match_query`, Trends and the UI highlighter.
    Now honors the analog polysemy gates, so e.g. "pet food" no longer surfaces resin/plastic.
    """
    exps = sorted(weighted_synonyms(query), key=lambda t: (-t[1], t[0]))
    return [phrase for phrase, _w, _c in exps]


def metric_synonym_phrases(query: str) -> list[str]:
    """Metric aliases for ``query``, excluding search-only domain analogs.

    These are equivalent names/translations of the requested financial concept (for example
    ``fx`` → ``tipo de cambio``), so the corpus-wide Mentions finder should count them as
    mentions instead of relegating them to the looser Analogs lane.
    """
    literal = query_phrase(query).casefold()
    exps = sorted(
        (e for e in _weighted_synonyms_with_source(query)
         if e[3] == "metric" and e[0].casefold() != literal),
        key=lambda t: (-t[1], t[0]),
    )
    return [phrase for phrase, _w, _c, _source in exps]


def metric_expansion_concepts(query: str) -> set[str]:
    """Concept names whose fired expansions are curated metric equivalents.

    Retrieval uses this provenance to keep direct translations/equivalent financial labels out
    of the looser analog-only boilerplate pruning stage. A metric alias is part of the precise
    search contract and must not disappear merely because its source wording is repetitive.
    """
    return {
        concept for _phrase, _weight, concept, source in _weighted_synonyms_with_source(query)
        if source == "metric"
    }


def analog_synonym_phrases(query: str) -> list[str]:
    """Search-only domain analogs for ``query``, kept in the labelled Analogs lane."""
    exps = sorted(
        (e for e in _weighted_synonyms_with_source(query) if e[3] == "analog"),
        key=lambda t: (-t[1], t[0]),
    )
    return [phrase for phrase, _w, _c, _source in exps]


def search_phrases(query: str) -> list[str]:
    """Literal phrase plus every gated metric/analog expansion, de-duplicated in order.

    This is the shared exhaustive-finder contract used by Search highlighting and Trends
    counting. Keeping one phrase builder prevents a term from working in one panel but not the
    other.
    """
    literal = query_phrase(query)
    if not literal:
        return []
    return list(dict.fromkeys([literal] + synonym_phrases(query)))


def concept_phrase_bands(query: str, *, threshold: float = _STRONG_MIN) -> dict:
    """Gated expansion grouped by concept and split into strong/weak weight bands.

    Returns ``{concept: (strong_phrases, weak_phrases)}`` — the retriever runs each band as its
    own FTS pass and fuses them at different weights (and caps per-concept contribution).
    """
    groups: dict[str, tuple[list[str], list[str]]] = {}
    for phrase, weight, concept in weighted_synonyms(query):
        strong, weak = groups.setdefault(concept, ([], []))
        (strong if weight >= threshold else weak).append(phrase)
    return groups


def fts_match_query(query: str, *, expand_synonyms: bool) -> str:
    """Build an FTS5 MATCH expression: OR of quoted content terms (+ synonym phrases)."""
    # Direct bilingual equivalents belong to the normal finder contract, even when optional
    # broader synonym discovery is disabled.  Build each as a phrase so multi-word entries
    # (for example ``trafico de pasajeros``) retain their meaning.
    # Preserve the established token-wise finder for arbitrary multi-word user queries, then
    # append the exact translated phrases.  (Replacing tokens with one quoted full query would
    # accidentally make ordinary searches much narrower.)
    pieces = [f'"{t}"' for t in query_terms(query)]
    pieces.extend(f'"{phrase}"' for phrase in bilingual_equivalent_phrases(query))
    if expand_synonyms:
        for phrase in synonym_phrases(query):
            cleaned = phrase.replace('"', " ").strip()
            if cleaned:
                pieces.append(f'"{cleaned}"')
    seen: set[str] = set()
    uniq = [p for p in pieces if not (p in seen or seen.add(p))]
    return " OR ".join(uniq)


def _phrases_or(phrases: list[str]) -> str:
    """OR of quoted, de-duplicated phrases — an FTS5 MATCH fragment (empty if no phrases)."""
    pieces: list[str] = []
    seen: set[str] = set()
    for phrase in phrases:
        cleaned = phrase.replace('"', " ").strip()
        quoted = f'"{cleaned}"'
        if cleaned and quoted not in seen:
            seen.add(quoted)
            pieces.append(quoted)
    return " OR ".join(pieces)


@dataclass
class KeywordHit:
    chunk_id: str
    score: float          # BM25 (lower rank = better; normalized at fusion time)


@dataclass
class SplitHits:
    """Keyword hits partitioned by provenance so the retriever can weight/cap each source.

    ``literal`` = matches of the query's own terms; ``strong``/``weak`` = matches surfaced only
    through analog/synonym expansion, split by the phrase's weight band. ``concept_of`` maps each
    expansion-surfaced chunk to the analog concept(s) that surfaced it (for per-concept caps).
    """

    literal: list          # list[KeywordHit]
    strong: list           # list[KeywordHit] — high-weight expansion band
    weak: list             # list[KeywordHit] — low-weight expansion band
    concept_of: dict       # chunk_id -> set[concept]


class KeywordIndex:
    def __init__(self, store: IndexStore):
        self.store = store

    def rebuild(self) -> None:
        """(Re)populate the FTS5 table from the chunks table (standard FTS5, wholesale)."""
        conn = self.store.connect()
        conn.execute("DELETE FROM chunks_fts")
        conn.execute("INSERT INTO chunks_fts (chunk_id, text) SELECT chunk_id, text FROM chunks")
        conn.commit()

    def _run(
        self, match: str, limit: int, filter_sql: str, filter_params: "list | tuple",
    ) -> list[KeywordHit]:
        """Execute one FTS5 MATCH, best-first (ascending ``bm25()``), honoring the doc filter.

        ``filter_sql``/``filter_params`` (from :meth:`SearchFilters.to_sql`) push document-level
        scoping INTO the ranked retrieval — joining ``chunks_fts → chunks → documents`` — so a
        narrow scope draws its top-``limit`` from the scoped subset instead of being filtered out
        of a global pool afterward. ``documents`` is left unaliased to match ``to_sql``'s clauses.
        """
        if not match:
            return []
        if filter_sql:
            sql = (
                "SELECT chunks_fts.chunk_id AS chunk_id, bm25(chunks_fts) AS score "
                "FROM chunks_fts "
                "JOIN chunks ON chunks.chunk_id = chunks_fts.chunk_id "
                "JOIN documents ON documents.doc_id = chunks.doc_id "
                f"WHERE chunks_fts MATCH ? AND ({filter_sql}) ORDER BY score LIMIT ?"
            )
            params = [match, *filter_params, limit]
        else:
            sql = (
                "SELECT chunk_id, bm25(chunks_fts) AS score FROM chunks_fts "
                "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?"
            )
            params = [match, limit]
        rows = self.store.connect().execute(sql, params).fetchall()
        return [KeywordHit(chunk_id=r["chunk_id"], score=float(r["score"])) for r in rows]

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
        expand_synonyms: bool = True,
        filter_sql: str = "",
        filter_params: "list | tuple" = (),
    ) -> list[KeywordHit]:
        """Run an FTS5 MATCH query, optionally expanding query terms with the synonym dict.

        Returns hits ordered best-first (ascending ``bm25()``). An empty/stopword-only query
        yields no hits. (Flat single-MATCH form kept for callers that don't need the banded
        provenance :meth:`search_split` provides.)
        """
        return self._run(
            fts_match_query(query, expand_synonyms=expand_synonyms),
            limit, filter_sql, filter_params,
        )

    def search_split(
        self,
        query: str,
        *,
        limit: int = 50,
        expand_synonyms: bool = True,
        filter_sql: str = "",
        filter_params: "list | tuple" = (),
    ) -> SplitHits:
        """Ranked keyword hits partitioned into literal / strong-analog / weak-analog bands.

        The literal band runs the query's own terms only. Each fired analog concept runs its
        strong and weak phrase bands as separate MATCHes, tagging every surfaced chunk with the
        concept — so the retriever can fuse the bands at different weights and cap any one
        concept's synonym contribution, instead of dumping one equal-weight OR flood.
        """
        literal_match = fts_match_query(query, expand_synonyms=False)
        literal = self._run(literal_match, limit, filter_sql, filter_params)
        if not expand_synonyms:
            return SplitHits(literal, [], [], {})

        strong_map: dict[str, KeywordHit] = {}
        weak_map: dict[str, KeywordHit] = {}
        concept_of: dict[str, set] = {}
        for concept, (strong_phrases, weak_phrases) in concept_phrase_bands(query).items():
            for phrases, bucket in ((strong_phrases, strong_map), (weak_phrases, weak_map)):
                match = _phrases_or(phrases)
                if not match:
                    continue
                for hit in self._run(match, limit, filter_sql, filter_params):
                    kept = bucket.get(hit.chunk_id)
                    if kept is None or hit.score < kept.score:   # keep the better bm25
                        bucket[hit.chunk_id] = hit
                    concept_of.setdefault(hit.chunk_id, set()).add(concept)

        strong = sorted(strong_map.values(), key=lambda h: h.score)
        weak = sorted(weak_map.values(), key=lambda h: h.score)
        return SplitHits(literal, strong, weak, concept_of)

    def matching_documents(
        self,
        query: str,
        *,
        expand_synonyms: bool = True,
        filter_sql: str = "",
        filter_params: "list | tuple" = (),
    ) -> list:
        """Corpus-wide document frequency: EVERY document with ≥1 matching chunk, no pool cap.

        Where :meth:`search` ranks and truncates a *capped* pool of chunks (``max(limit*5, 50)``),
        this GROUPs the same FTS5 MATCH by document — so a term buried in a document that ranks
        far outside that pool is still returned. This is the "corpus-wide Ctrl+F" universe: the
        linear occurrence view uses it to report the TRUE matching-document count instead of the
        surfaced undercount. Reuses :func:`fts_match_query` (identical synonym expansion), and
        accepts the same ``filter_sql``/``filter_params`` scoping as :meth:`search`.

        Returns ``documents`` rows (``doc_id, company, period, doc_type, title, markdown_path``),
        one per matching document, so callers can hydrate per-doc groups without re-querying.
        """
        match = fts_match_query(query, expand_synonyms=expand_synonyms)
        if not match:
            return []
        sql = (
            "SELECT DISTINCT documents.doc_id AS doc_id, documents.company AS company, "
            "       documents.period AS period, documents.doc_type AS doc_type, "
            "       documents.title AS title, documents.markdown_path AS markdown_path "
            "FROM chunks_fts "
            "JOIN chunks ON chunks.chunk_id = chunks_fts.chunk_id "
            "JOIN documents ON documents.doc_id = chunks.doc_id "
            "WHERE chunks_fts MATCH ?"
        )
        params: list = [match]
        if filter_sql:
            sql += f" AND ({filter_sql})"
            params.extend(filter_params)
        return self.store.connect().execute(sql, params).fetchall()

    def matching_doc_ids(
        self,
        query: str,
        *,
        expand_synonyms: bool = True,
        filter_sql: str = "",
        filter_params: "list | tuple" = (),
    ) -> "set[str]":
        """The DISTINCT set of ``document_id``s whose ANY chunk matches — true corpus-wide
        document frequency (see :meth:`matching_documents`)."""
        return {r["doc_id"] for r in self.matching_documents(
            query, expand_synonyms=expand_synonyms,
            filter_sql=filter_sql, filter_params=filter_params)}
