"""HybridRetriever — the public search entrypoint.

Runs the keyword index and the semantic index in parallel, fuses their rankings (RRF),
applies filters, and returns ranked ``Hit`` objects each carrying a highlighted snippet
and full document provenance. This is the stable interface the dashboard and the Q&A layer
both depend on.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.index.keyword_index import KeywordIndex, metric_expansion_concepts, query_terms
from src.index.store import IndexStore
from src.index import vector_index
from src.search.filters import SearchFilters
from src.search.fusion import (
    analyze_analog_candidates,
    build_corpus_boilerplate,
    cap_analog_floods,
    reciprocal_rank_fusion,
    rerank_sort_key,
)
from src.search.snippets import Snippet, make_snippet
from src.search.query_understanding import semantic_query_text

# How many fused candidates to hydrate before applying filters + limit. Generous so selective
# filters still return a full page, bounded so hydration stays cheap.
_CANDIDATE_CAP = 1000
_SQLITE_MAX_VARS = 400

# Analog/synonym-surfaced hits are fused BELOW the literal query (as a fraction of the keyword
# weight), so an expansion match can lift a related passage but never outrank a literal hit —
# the "boost, not OR-flood" principle. The strong band (unambiguous aliases) outweighs the weak
# band (risky bare tokens, per-alias-weighted down in analogs.yaml).
_ANALOG_STRONG_WEIGHT = 0.5
_ANALOG_WEAK_WEIGHT = 0.25
# Diversity caps: no single fired concept or company may contribute more than this many
# analog-ONLY hits to a page before the rest sink to the tail (keeps synonym hits from crowding
# out literal answers). Generous relative to a typical page so only genuine floods are trimmed.
_ANALOG_CONCEPT_CAP = 6
_ANALOG_COMPANY_CAP = 10


@dataclass
class Hit:
    """One ranked search result."""

    chunk_id: str
    doc_id: str
    company: str
    period: str | None
    doc_type: str
    title: str
    score: float
    snippet: Snippet
    markdown_path: str
    char_start: int          # location of the chunk in the source doc (for the viewer)
    char_end: int
    keyword_match: bool = True   # False = surfaced only by the semantic half ("related")
    companies: list[str] = field(default_factory=list)   # all corpora the doc belongs to
    match_kind: str = "exact"    # exact | expanded | semantic
    semantic_score: float | None = None


class HybridRetriever:
    """Hybrid keyword + semantic retrieval over an :class:`IndexStore`.

    The semantic half needs to embed the query with the *same* embedder used to build the
    index. Supply it either directly (``embedder=``, best for tests / DI) or via the alpha_go
    ``config`` (the embedder is then built lazily through ``get_embedder``). With neither, the
    retriever degrades gracefully to keyword-only search.
    """

    def __init__(
        self,
        store: IndexStore,
        *,
        embedder=None,
        config: dict | None = None,
        expand_synonyms: bool = True,
        embedding_model: str | None = None,  # accepted for back-compat; prefer embedder/config
    ):
        self.store = store
        self.config = config
        self.expand_synonyms = expand_synonyms
        self.embedding_model = embedding_model
        self._embedder = embedder
        self._embedder_resolved = embedder is not None
        self._vector_cache: "tuple[list[str], object] | None" = None  # (ids, normalized matrix)
        self._id_index: "dict[str, int] | None" = None      # chunk_id -> matrix row (built with cache)
        self._boiler_cache = None                            # CorpusBoilerplate, built once per index
        # SQLite increments ``data_version`` on this connection whenever a
        # *different* connection commits.  The estate worker updates Alpha's
        # index in another process, so this is a cheap, token-free invalidation
        # signal for long-lived dashboard/retriever instances.
        self._index_data_version = self._read_index_data_version()

    def get_embedder(self):
        """The resolved embedder (loaded once), or ``None`` when unavailable.

        Exposed so callers that add documents to the live index (e.g. the upload tab) reuse the
        already-loaded model instead of loading a second copy.
        """
        if not self._embedder_resolved:
            self._embedder_resolved = True
            if self.config is not None:
                from src.index.embeddings import get_embedder
                try:
                    self._embedder, _ = get_embedder(self.config)
                except Exception:
                    if (self.config.get("index", {}) or {}).get("strict_runtime", False):
                        raise
                    self._embedder = None
        if self._embedder is not None and (self.config or {}).get("index", {}).get("strict_runtime"):
            expected = self.store.get_meta("embedding_dim")
            actual = getattr(self._embedder, "dim", None)
            if expected and actual is not None and int(expected) != int(actual):
                raise ValueError(
                    "Embedding dimension mismatch: index has "
                    f"{expected}, runtime embedder has {actual}. Rebuild the index with "
                    "the configured local model."
                )
        return self._embedder

    @property
    def semantic_available(self) -> bool:
        """Whether a genuine semantic model—not the lexical hashing fallback—is available."""
        embedder = self.get_embedder()
        return bool(embedder is not None
                    and getattr(embedder, "semantic_quality", "semantic") != "lexical")

    def invalidate_cache(self) -> None:
        """Drop the cached vector matrix so the next search reflects newly added embeddings."""
        self._vector_cache = None
        self._id_index = None
        self._boiler_cache = None
        self._index_data_version = self._read_index_data_version()

    def _read_index_data_version(self) -> int:
        row = self.store.connect().execute("PRAGMA data_version").fetchone()
        return int(row[0])

    def _refresh_external_index(self) -> None:
        """Invalidate process-local caches after another process updates SQLite."""

        current = self._read_index_data_version()
        if current == self._index_data_version:
            return
        self._vector_cache = None
        self._id_index = None
        self._boiler_cache = None
        self._index_data_version = current

    def _boilerplate_model(self):
        """The corpus-frequency boilerplate model (built once per index, then cached).

        Learns recurring boilerplate (disclaimers, corporate headers, glossaries) from the whole
        chunk corpus so the analog reranker can drop substanceless-but-fired passages without a
        curated signature list. A build failure degrades to the curated fallback (returns None).
        """
        if self._boiler_cache is None:
            try:
                rows = self.store.connect().execute(
                    "SELECT doc_id, text FROM chunks"
                ).fetchall()
                self._boiler_cache = build_corpus_boilerplate(
                    [(r["doc_id"], r["text"]) for r in rows]
                )
            except Exception:  # noqa: BLE001 — precision aid is optional, never fatal
                self._boiler_cache = None
        return self._boiler_cache

    def _embed_query(self, query: str):
        """Embed the query only with a genuine semantic model.

        ``HashingEmbedder`` is a useful deterministic index/build fallback, but its vectors are
        lexical token overlap—not conceptual meaning. Running an 85k-row vector scan with those
        vectors both mislabels the result and makes the first interactive search unnecessarily
        expensive. In fallback mode, FTS + the exhaustive literal scanner are the complete search
        path; semantic retrieval resumes automatically when a real multilingual model is active.
        """
        embedder = self.get_embedder()
        if (embedder is None
                or getattr(embedder, "semantic_quality", "semantic") == "lexical"):
            return None
        return embedder.encode([semantic_query_text(query)])[0]

    def search(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
        limit: int = 20,
        keyword_weight: float = 0.5,
        semantic_weight: float = 0.5,
    ) -> list[Hit]:
        """Hybrid search: keyword + semantic, fused and filtered, best first.

        1. keyword (FTS5/BM25) + semantic (cosine over embeddings) candidate lists
        2. ``reciprocal_rank_fusion`` of the two rank lists, weighted
        3. apply ``SearchFilters`` via the documents join
        4. hydrate ``Hit`` rows, each with a highlighted ``make_snippet``
        """
        if not query or not query.strip():
            return []

        self._refresh_external_index()

        # Push the active scope INTO candidate retrieval (not just the post-hoc hydrate filter),
        # so a narrow company/industry/period scope draws its top-`pool` from the scoped subset.
        where, params = (filters.to_sql() if filters and not filters.is_empty() else ("", []))

        pool = max(limit * 5, 50)
        # Keyword hits split by provenance: the literal query, and the strong/weak analog bands.
        # Fusing the bands separately (below) lets an expansion match boost a passage without
        # letting it outrank a literal hit, and carries per-concept provenance for the cap.
        split = KeywordIndex(self.store).search_split(
            query, limit=pool, expand_synonyms=self.expand_synonyms,
            filter_sql=where, filter_params=params,
        )
        literal_ids = [h.chunk_id for h in split.literal]
        strong_ids = [h.chunk_id for h in split.strong]
        weak_ids = [h.chunk_id for h in split.weak]
        literal_set = set(literal_ids)

        terms = query_terms(query)

        # Semantic half FIRST (and its cached matrix), so the analog reranker below can score each
        # analog chunk by MiniLM cosine to the FULL query — reusing the very same cached matrix.
        query_vec = self._embed_query(query)
        sem_ids: list[str] = []
        sem_scores: dict[str, float] = {}
        if query_vec is not None:
            if self._vector_cache is None:                      # build once, reuse across queries
                self._vector_cache = vector_index.load_matrix(self.store)
                ids_c, _m = self._vector_cache
                self._id_index = {cid: i for i, cid in enumerate(ids_c)}
            ids, matrix = self._vector_cache
            allowed = self._allowed_chunk_ids(where, params) if where else None
            sem_hits = vector_index.search_matrix(
                ids, matrix, query_vec, limit=pool, allowed=allowed,
                id_index=self._id_index,
            )
            sem_ids = [h.chunk_id for h in sem_hits]
            sem_scores = {h.chunk_id: h.score for h in sem_hits}
        sem_set = set(sem_ids)

        # PRECISION: dedupe near-duplicate + drop substanceless boilerplate/footers in the analog
        # bands, and reorder survivors by SEMANTIC cosine to the query (lexical fit as tiebreak),
        # BEFORE fusion. Applied only to analog-ONLY chunks (a chunk also surfaced literally/
        # semantically is left untouched here and still fuses via those lists), so expansion breadth
        # and recall are preserved while coincidental single-alias token matches sink or drop.
        # See fusion.dedupe_rerank_analog / rerank_sort_key.
        if self.expand_synonyms and (strong_ids or weak_ids):
            original_strong, original_weak = strong_ids, weak_ids
            strong_ids, weak_ids = self._clean_analog_bands(
                strong_ids, weak_ids, literal_set, terms, query_vec,
            )
            # Curated metric aliases are precise equivalents/translations, not speculative
            # analogies. Restore any such candidates that the generic analog boilerplate pass
            # rejected (for example REIT "unidades" for a "stores" query), preserving the
            # original BM25 order. This is the exhaustive, unknown-document-safe path.
            metric_concepts = metric_expansion_concepts(query)
            if metric_concepts:
                cleaned_strong, cleaned_weak = set(strong_ids), set(weak_ids)
                strong_ids = [
                    cid for cid in original_strong
                    if cid in cleaned_strong
                    or split.concept_of.get(cid, set()).intersection(metric_concepts)
                ]
                weak_ids = [
                    cid for cid in original_weak
                    if cid in cleaned_weak
                    or split.concept_of.get(cid, set()).intersection(metric_concepts)
                ]

        fused = reciprocal_rank_fusion(
            [literal_ids, strong_ids, weak_ids, sem_ids],
            weights=[
                keyword_weight,
                keyword_weight * _ANALOG_STRONG_WEIGHT,
                keyword_weight * _ANALOG_WEAK_WEIGHT,
                semantic_weight,
            ],
        )
        if not fused:
            return []

        candidates = [cid for cid, _ in fused][:_CANDIDATE_CAP]
        scores = dict(fused)
        rows = self._hydrate(candidates, filters)

        # Cap per-concept / per-company synonym floods: reorder so analog-ONLY hits that over-
        # represent one concept or filer sink below the literal/semantic answers (kept for the
        # tail, not dropped). Hits missing from `rows` (filtered out) are excluded up front.
        present = [cid for cid in candidates if cid in rows]
        reordered = cap_analog_floods(
            [(cid, rows[cid]["company"],
              split.concept_of.get(cid, set()),
              cid not in literal_set and cid not in sem_set)
             for cid in present],
            concept_cap=_ANALOG_CONCEPT_CAP, company_cap=_ANALOG_COMPANY_CAP,
        )

        kw_id_set = literal_set | set(strong_ids) | set(weak_ids)
        expanded_set = set(strong_ids) | set(weak_ids)
        hits: list[Hit] = []
        for cid in reordered:
            row = rows[cid]
            hits.append(Hit(
                chunk_id=cid,
                doc_id=row["doc_id"],
                company=row["company"],
                period=row["period"],
                doc_type=row["doc_type"],
                title=row["title"],
                score=scores[cid],
                snippet=make_snippet(row["text"], terms),
                markdown_path=row["markdown_path"],
                char_start=row["char_start"],
                char_end=row["char_end"],
                keyword_match=cid in kw_id_set,
                companies=([c for c in (row["companies"] or "").split(",") if c]
                           or [row["company"]]),
                match_kind=("exact" if cid in literal_set else
                            "expanded" if cid in expanded_set else "semantic"),
                semantic_score=sem_scores.get(cid),
            ))
            if len(hits) >= limit:
                break
        return hits

    def _hydrate(self, chunk_ids: list[str], filters: SearchFilters | None) -> dict:
        """Fetch chunk + document fields for ``chunk_ids``, honoring ``filters``.

        Returns a ``{chunk_id: row}`` map (rows missing the filter are absent). Queries in
        batches to stay under SQLite's bound-variable limit.
        """
        if not chunk_ids:
            return {}
        where, params = (filters.to_sql() if filters and not filters.is_empty() else ("", []))
        conn = self.store.connect()
        out: dict = {}
        base = (
            "SELECT c.chunk_id, c.doc_id, c.text, c.char_start, c.char_end, "
            "       documents.company, documents.period, documents.doc_type, "
            "       documents.title, documents.markdown_path, "
            "       (SELECT GROUP_CONCAT(dc.company) FROM document_companies dc "
            "        WHERE dc.doc_id = c.doc_id) AS companies "
            "FROM chunks c JOIN documents ON documents.doc_id = c.doc_id WHERE "
        )
        for i in range(0, len(chunk_ids), _SQLITE_MAX_VARS):
            batch = chunk_ids[i:i + _SQLITE_MAX_VARS]
            placeholders = ", ".join("?" for _ in batch)
            sql = base + f"c.chunk_id IN ({placeholders})"
            if where:
                sql += f" AND ({where})"
            for row in conn.execute(sql, [*batch, *params]).fetchall():
                out[row["chunk_id"]] = row
        return out

    def _clean_analog_bands(
        self, strong_ids: list, weak_ids: list, literal_set: set, terms: list,
        query_vec=None,
    ) -> "tuple[list, list]":
        """Dedupe + boilerplate-drop + semantic-rerank the analog-only chunks of each band.

        Only chunks surfaced SOLELY by expansion (not in ``literal_set``) are eligible: they are
        analyzed together (strong first, then weak, best-first) so a near-duplicate that appears
        in both bands is caught once, then each band is rebuilt as ``[literal-overlap ids in
        original order] + [analog-only survivors, reordered by semantic cosine to the query]``.
        Boilerplate is judged by the corpus-frequency model; survivors are ordered by MiniLM
        cosine (``query_vec`` vs the cached chunk matrix) with lexical fit as tiebreak. Literal-
        overlap ids keep their band contribution untouched. Returns ``(strong_ids, weak_ids)``.
        """
        strong_set = set(strong_ids)
        analog_ids = [c for c in strong_ids if c not in literal_set]
        analog_ids += [c for c in weak_ids if c not in literal_set and c not in strong_set]
        if not analog_ids:
            return strong_ids, weak_ids

        texts = self._chunk_texts(analog_ids)

        # Semantic cosine of each analog chunk to the FULL query (reuse the cached matrix + index).
        sim_scores = None
        if query_vec is not None and self._vector_cache is not None:
            ids, matrix = self._vector_cache
            sim_scores = vector_index.similarities(
                ids, matrix, query_vec, analog_ids, id_index=self._id_index,
            )

        # Keep interactive search independent of corpus-learned rules. Building a whole-corpus
        # shingle model on the first query made large indexes stall for minutes and—more
        # importantly—made ranking depend on the already-known corpus. Candidate-local duplicate
        # suppression and topical/semantic ordering generalize to newly uploaded documents without
        # a per-company or per-corpus learning pass. ``False`` also bypasses the legacy curated
        # company boilerplate signatures in ``analyze_analog_candidates``.
        meta = analyze_analog_candidates(
            [(c, texts.get(c, "")) for c in analog_ids], terms,
            sim_scores=sim_scores, is_boilerplate=lambda _key, _text: False,
        )

        def clean(band: list) -> list:
            lit = [c for c in band if c in literal_set]
            ana = [c for c in band
                   if c in meta and not meta[c]["drop"]]     # analog-only survivors of THIS band
            ana.sort(key=lambda c: rerank_sort_key(meta[c]))
            return lit + ana

        return clean(strong_ids), clean(weak_ids)

    def _chunk_texts(self, chunk_ids: list) -> dict:
        """``{chunk_id: text}`` for ``chunk_ids`` (batched under SQLite's bound-variable limit)."""
        if not chunk_ids:
            return {}
        conn = self.store.connect()
        out: dict = {}
        for i in range(0, len(chunk_ids), _SQLITE_MAX_VARS):
            batch = chunk_ids[i:i + _SQLITE_MAX_VARS]
            placeholders = ", ".join("?" for _ in batch)
            sql = f"SELECT chunk_id, text FROM chunks WHERE chunk_id IN ({placeholders})"
            for row in conn.execute(sql, batch).fetchall():
                out[row["chunk_id"]] = row["text"]
        return out

    def _allowed_chunk_ids(self, where: str, params: list) -> set:
        """Chunk-ids whose document satisfies ``where`` — the scoped subset for semantic search."""
        sql = ("SELECT c.chunk_id FROM chunks c "
               "JOIN documents ON documents.doc_id = c.doc_id "
               f"WHERE {where}")
        return {r["chunk_id"] for r in self.store.connect().execute(sql, params).fetchall()}
