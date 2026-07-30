"""eval_analogs — PRECISION eval infrastructure for the analog synonym-expansion feature.

scripts/eval_retrieval.py is a RECALL guard (right-company @k). It cannot see the two risks
analog expansion adds: NOISE (irrelevant passages an analog concept drags in) and POLYSEMY
FALSE-FIRES (a wrong-sense query expanding into the domain sense). This tool measures those.

Two phases, deliberately split so no self-judging leaks into the query/candidate authoring:

  1. COLLECT (this agent, no labels):
        python3 scripts/eval_analogs.py --collect
     For each query in eval/analog_queries.yaml it gathers the ANALOG-ONLY candidate hits —
     chunks surfaced by the strong/weak expansion bands of KeywordIndex.search_split that the
     literal band did NOT already surface — plus the fused HybridRetriever top-k, and writes
     eval/analog_candidates_unlabeled.yaml with NO relevance field. A separate independent
     judge copies that file to eval/analog_relevance.yaml and fills in `relevant` / `wrong_sense`.

  2. SCORE (run later, once judgments exist):
        python3 scripts/eval_analogs.py --score
     Computes precision@k / noise-rate over analog-only hits and the polysemy false-fire rate
     (fraction of trap_wrong queries surfacing ANY wrong-sense analog hit; target ~0). Guarded:
     if eval/analog_relevance.yaml is absent it explains what to do and exits cleanly.

INVARIANT: this script never edits configs/analogs.yaml, retriever.py, or fusion.py; it only
observes. Keep eval/queries.yaml + scripts/eval_retrieval.py as the untouched recall guard.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

QUERIES_PATH = ROOT / "eval" / "analog_queries.yaml"
CANDIDATES_PATH = ROOT / "eval" / "analog_candidates_unlabeled.yaml"
# Second, held-out-inclusive candidate batch collected for the track-b-expand2 additions
# (queries carrying `added_in: track-b-expand2`); written by `--collect --only-new`.
CANDIDATES2_PATH = ROOT / "eval" / "analog_candidates2_unlabeled.yaml"
RELEVANCE_PATH = ROOT / "eval" / "analog_relevance.yaml"

_SNIPPET_CHARS = 240

# The expansion bands can surface dozens of chunks per concept; a human/independent judge cannot
# label hundreds. Cap the analog-only candidates carried per query to the best-first head (strong
# band before weak, each already bm25-sorted) — enough to compute precision@5 and to expose any
# wrong-sense leakage — while `n_analog_candidates_total` records the true, uncapped count.
_PER_QUERY_CAP = 12


# --------------------------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------------------------
def _short_snippet(text: str, terms: list[str]) -> str:
    """A compact, single-line preview anchored near the first matched term (display only)."""
    if not text:
        return ""
    flat = " ".join(text.split())
    low = flat.lower()
    first = None
    for t in terms:
        i = low.find(t.lower())
        if i != -1 and (first is None or i < first):
            first = i
    anchor = 0 if first is None else max(0, first - 60)
    out = flat[anchor:anchor + _SNIPPET_CHARS]
    if anchor > 0:
        out = "…" + out
    if anchor + _SNIPPET_CHARS < len(flat):
        out = out + "…"
    return out


def collect(config_path: Path, db_path: Path | None,
            queries: "list[dict] | None" = None) -> dict:
    """Gather analog-only candidates + fused top-k for every query. Returns the YAML doc dict.

    An ANALOG-ONLY candidate is a chunk in the strong/weak expansion bands that the literal band
    did NOT surface — i.e. it exists in the results ONLY because an analog concept fired. Those
    are exactly the hits whose relevance the judge must vet (a literal hit is not the analog's
    fault). Each record carries the concept(s) that surfaced it and its weight band.
    """
    from src.index.keyword_index import KeywordIndex, query_terms, weighted_synonyms
    from src.index.store import IndexStore
    from src.search.retriever import HybridRetriever
    from src.shared.paths import ESTATE_BRIDGE

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    db = db_path if db_path else ESTATE_BRIDGE.alpha_go_index_path
    store = IndexStore(db)
    store.connect()

    ki = KeywordIndex(store)
    # Keyword-only retriever keeps the collector deterministic and offline (no embedder load);
    # the fused top-k here is the BM25/analog fusion, which is what analog changes actually move.
    retriever = HybridRetriever(store, expand_synonyms=True)

    conn = store.connect()

    def meta_of(chunk_id: str) -> dict:
        row = conn.execute(
            "SELECT d.company AS company, d.period AS period, d.title AS title, "
            "d.doc_id AS doc_id, c.text AS text "
            "FROM chunks c JOIN documents d ON d.doc_id = c.doc_id WHERE c.chunk_id = ?",
            (chunk_id,),
        ).fetchone()
        return dict(row) if row else {}

    if queries is None:
        spec = yaml.safe_load(QUERIES_PATH.read_text(encoding="utf-8"))
        queries = spec.get("queries", []) if isinstance(spec, dict) else []

    records: list[dict] = []
    n_candidates = 0
    for q in queries:
        query = q["query"]
        kind = q.get("kind", "")
        concept = q.get("concept", "")
        terms = query_terms(query)

        # Which analog concepts the gate lets fire for this query (label-free provenance).
        fired = sorted({c for _, _, c in weighted_synonyms(query)})

        # Literal band (no expansion) — the base system's reach. base_literal_miss records whether
        # the base misses the expected company entirely (the base_miss precondition, verified here).
        base = ki.search_split(query, limit=50, expand_synonyms=False)
        literal_ids = {h.chunk_id for h in base.literal}
        expect_company = q.get("expect_company")
        base_companies = {meta_of(h.chunk_id).get("company") for h in base.literal}
        base_literal_miss = (
            bool(expect_company) and expect_company not in base_companies
        )

        # Expansion bands — the analog-only surface.
        split = ki.search_split(query, limit=50, expand_synonyms=True)
        analog_candidates: list[dict] = []
        for band_name, band in (("strong", split.strong), ("weak", split.weak)):
            for hit in band:
                if hit.chunk_id in literal_ids:
                    continue  # literal already had it; not the analog's doing
                m = meta_of(hit.chunk_id)
                fired_concepts = sorted(split.concept_of.get(hit.chunk_id, set()))
                analog_candidates.append({
                    "chunk_id": hit.chunk_id,
                    "doc": m.get("doc_id"),
                    "company": m.get("company"),
                    "period": m.get("period"),
                    "title": m.get("title"),
                    "band": band_name,
                    "fired_concepts": fired_concepts,
                    "snippet": _short_snippet(m.get("text", ""), terms),
                    # relevance intentionally OMITTED — the independent judge fills it in.
                })

        # De-dup a chunk that appears in both bands: keep the strong-band record.
        seen: dict[str, dict] = {}
        for c in analog_candidates:
            prev = seen.get(c["chunk_id"])
            if prev is None or (prev["band"] == "weak" and c["band"] == "strong"):
                seen[c["chunk_id"]] = c
        # Best-first: strong band before weak, preserving each band's bm25 order.
        deduped = sorted(seen.values(), key=lambda c: 0 if c["band"] == "strong" else 1)
        n_total = len(deduped)
        analog_candidates = deduped[:_PER_QUERY_CAP]
        n_candidates += len(analog_candidates)

        # Fused top-k the user actually sees (for the judge's context; not itself scored here).
        top_k = []
        for hit in retriever.search(query, limit=10):
            top_k.append({
                "chunk_id": hit.chunk_id,
                "company": hit.company,
                "period": hit.period,
                "keyword_match": hit.keyword_match,
                "snippet": _short_snippet(hit.snippet.text if hasattr(hit.snippet, "text")
                                          else str(hit.snippet), terms),
            })

        records.append({
            "query": query,
            "kind": kind,
            "concept": concept,
            "expect_company": expect_company,
            "fired_concepts": fired,
            "base_literal_miss": base_literal_miss,
            "n_analog_candidates_total": n_total,
            "analog_candidates": analog_candidates,
            "fused_top_k": top_k,
        })

    return {
        "note": (
            "UNLABELED analog-eval candidates. An independent judge copies this to "
            "eval/analog_relevance.yaml and adds, per analog_candidate, `relevant: true|false` "
            "and (for trap_wrong queries) `wrong_sense: true|false`. Do NOT self-judge here."
        ),
        "db": str(db),
        "n_queries": len(records),
        "n_analog_candidates": n_candidates,
        "records": records,
    }


# --------------------------------------------------------------------------------------------
# Metrics (run AFTER eval/analog_relevance.yaml exists)
# --------------------------------------------------------------------------------------------
def precision_and_noise(labeled_records: list[dict], k: int | None = None) -> dict:
    """Precision / noise-rate over ANALOG-ONLY hits, from judge `relevant` labels.

    precision = relevant analog-only hits / all labeled analog-only hits (optionally capped to
    the top-`k` analog candidates per query). noise_rate = 1 - precision. Unlabeled candidates
    are skipped (and counted) so a partial judging pass still yields an honest partial number.
    """
    relevant = total = unlabeled = 0
    for rec in labeled_records:
        cands = rec.get("analog_candidates", [])
        if k is not None:
            cands = cands[:k]
        for c in cands:
            if "relevant" not in c or c["relevant"] is None:
                unlabeled += 1
                continue
            total += 1
            if c["relevant"]:
                relevant += 1
    precision = (relevant / total) if total else None
    return {
        "k": k,
        "labeled_hits": total,
        "relevant_hits": relevant,
        "unlabeled_hits": unlabeled,
        "precision": precision,
        "noise_rate": (1 - precision) if precision is not None else None,
    }


class _ScoreContext:
    """Reproduces the retriever's LIVE analog transform for the scorer, over the fixed judged pool.

    Three faithful pieces, so the reported "after" precision matches what actually ships:
      - GATE — a judged candidate is only surfaced live if one of the analog concept(s) that
        produced it still fires for the query under the current ``configs/analogs.yaml`` gate
        (``keyword_index.weighted_synonyms``). A gate veto (the wrong-sense fix) removes it.
      - SEMANTIC RERANK — each candidate chunk is scored by MiniLM cosine to the FULL query, via
        the SAME stored embeddings + ``vector_index.similarities`` the retriever uses.
      - CORPUS BOILERPLATE — the corpus-frequency model (``fusion.build_corpus_boilerplate``),
        applied to each candidate's FULL chunk text.

    Degrades gracefully: if the index/embedder is unavailable, ``sims`` returns None (lexical
    rerank) and ``boiler`` is None (curated fallback), and ``score()`` prints which path ran.
    """

    def __init__(self, config_path: Path | None = None, db_path: Path | None = None):
        self.ok = False
        self.err = None
        self.embedder = None
        self.boiler = None
        self._text: dict[str, str] = {}
        self.ids: list[str] = []
        self.matrix = None
        self.id_index: dict[str, int] = {}
        try:
            from src.index.store import IndexStore
            from src.index import vector_index
            from src.index.embeddings import get_embedder
            from src.search.fusion import build_corpus_boilerplate

            cfg = yaml.safe_load((config_path or (ROOT / "configs/alpha_go.yaml"))
                                 .read_text(encoding="utf-8"))
            db = db_path if db_path else ROOT / cfg["index"]["db_path"]
            store = IndexStore(db)
            rows = store.connect().execute("SELECT chunk_id, doc_id, text FROM chunks").fetchall()
            self._text = {r["chunk_id"]: r["text"] for r in rows}
            self.boiler = build_corpus_boilerplate([(r["doc_id"], r["text"]) for r in rows])
            self.ids, self.matrix = vector_index.load_matrix(store)
            self.id_index = {c: i for i, c in enumerate(self.ids)}
            self.embedder, _ = get_embedder(cfg)
            self.ok = True
        except Exception as exc:  # noqa: BLE001 — scorer must still run lexically
            self.err = exc

    def sims(self, query: str, chunk_ids: "list[str]") -> "dict | None":
        if not self.ok or self.embedder is None or self.matrix is None:
            return None
        from src.index import vector_index
        qv = self.embedder.encode([query])[0]
        return vector_index.similarities(self.ids, self.matrix, qv, chunk_ids,
                                         id_index=self.id_index)

    def chunk_text(self, chunk_id: str) -> str:
        return self._text.get(chunk_id, "")


def _live_fired_concepts(query: str) -> set:
    """Analog concepts that still fire for ``query`` under the current gate (label-free)."""
    from src.index.keyword_index import weighted_synonyms
    return {c for _, _, c in weighted_synonyms(query)}


def _gate_survivors(rec: dict) -> list[dict]:
    """Judged candidates still surfaced after the LIVE polysemy gate.

    A candidate survives if any concept that produced it (``fired_concepts``) still fires for the
    query; a candidate with no recorded provenance is kept (can't prove it was gated out). This is
    how the scorer reflects the wrong-sense fix on the fixed judged pool without re-judging.
    """
    live = _live_fired_concepts(rec.get("query", ""))
    out = []
    for c in rec.get("analog_candidates", []):
        fc = c.get("fired_concepts") or []
        if not fc or (set(fc) & live):
            out.append(c)
    return out


def _rerank_record(rec: dict, ctx: "_ScoreContext | None" = None, *, gate: bool = True) -> dict:
    """Return a COPY of ``rec`` whose candidates reflect the retriever's live analog transform.

    Order of operations mirrors the retriever: LIVE gate (``gate=True``) → dedupe → corpus-
    boilerplate drop → SEMANTIC rerank (MiniLM cosine, lexical fit tiebreak). Only reorders/drops
    candidates that already carry a judgment; never invents or promotes an unjudged candidate, so
    no relevance is ever assumed.
    """
    from src.index.keyword_index import query_terms
    from src.search.fusion import dedupe_rerank_analog

    cands = _gate_survivors(rec) if gate else list(rec.get("analog_candidates", []))
    query = rec.get("query", "")
    items = [(i, c.get("snippet", "")) for i, c in enumerate(cands)]

    sim_scores = None
    if ctx is not None:
        by_cid = ctx.sims(query, [c.get("chunk_id") for c in cands])
        if by_cid is not None:
            sim_scores = {i: by_cid.get(c.get("chunk_id")) for i, c in enumerate(cands)}

    is_boiler = None
    if ctx is not None and ctx.boiler is not None:
        is_boiler = lambda key, text: ctx.boiler.is_boilerplate(  # noqa: E731
            ctx.chunk_text(cands[key].get("chunk_id")))

    kept_idx, _dropped_idx = dedupe_rerank_analog(
        items, query_terms(query), sim_scores=sim_scores, is_boilerplate=is_boiler)
    out = dict(rec)
    out["analog_candidates"] = [cands[i] for i in kept_idx]
    return out


def _dropped_candidates(rec: dict, ctx: "_ScoreContext | None" = None) -> list[dict]:
    """Judged analog candidates the live transform removes from ``rec`` (gate + dedupe/boilerplate)."""
    kept = {id(c) for c in _rerank_record(rec, ctx).get("analog_candidates", [])}
    return [c for c in rec.get("analog_candidates", []) if id(c) not in kept]


def precision_by_kind(labeled_records: list[dict], k: int | None = None,
                      *, rerank: bool = False, ctx: "_ScoreContext | None" = None) -> dict:
    """Precision over analog-only hits, split by query ``kind`` (+ overall).

    With ``rerank=True`` each record's candidates are first put through the retriever's live
    transform (gate → dedupe → corpus-boilerplate drop → semantic rerank via ``ctx``), so
    precision@k sees the reordered head and precision(all) sees the post-drop pool. Returns
    ``{kind_or_'overall': {relevant, total, precision}}``.
    """
    buckets: dict[str, list[int]] = {}
    for rec in labeled_records:
        r = _rerank_record(rec, ctx) if rerank else rec
        cands = r.get("analog_candidates", [])
        if k is not None:
            cands = cands[:k]
        kind = r.get("kind", "")
        rel_tot = buckets.setdefault(kind, [0, 0])
        overall = buckets.setdefault("overall", [0, 0])
        for c in cands:
            if c.get("relevant") is None:
                continue
            rel_tot[1] += 1
            overall[1] += 1
            if c["relevant"]:
                rel_tot[0] += 1
                overall[0] += 1
    return {kind: {"relevant": rel, "total": tot,
                   "precision": (rel / tot) if tot else None}
            for kind, (rel, tot) in buckets.items()}


def polysemy_false_fire_rate(labeled_records: list[dict], *, gate: bool = False) -> dict:
    """Fraction of trap_wrong queries that surface ANY wrong-sense analog hit (target ~0).

    A query counts as a false-fire if it has >=1 analog-only candidate marked `wrong_sense: true`
    by the judge. With ``gate=True`` the candidate set is first passed through the LIVE polysemy
    gate (:func:`_gate_survivors`), so a wrong-sense hit whose concept is now vetoed no longer
    counts — this is how the wrong-sense fix is verified on the fixed judged pool without
    re-judging. As a LABEL-FREE proxy, also report how many trap_wrong queries fire any analog
    concept at all under the current gate — the gate's own claim is that this should be 0.
    """
    traps = [r for r in labeled_records if r.get("kind") == "trap_wrong"]
    judged_false_fire = 0
    judged_any = 0
    proxy_fired = 0
    for r in traps:
        # Prefer the CURRENT gate (live) so the proxy reflects the shipped analogs.yaml; fall back
        # to the record's stored provenance only when there is no query to evaluate live.
        q = r.get("query", "")
        fired_now = _live_fired_concepts(q) if q else set(r.get("fired_concepts") or [])
        if fired_now:
            proxy_fired += 1
        cands = _gate_survivors(r) if gate else r.get("analog_candidates", [])
        labeled = [c for c in cands if c.get("wrong_sense") is not None]
        if labeled:
            judged_any += 1
            if any(c.get("wrong_sense") for c in labeled):
                judged_false_fire += 1
    return {
        "trap_wrong_queries": len(traps),
        "judged_queries": judged_any,
        "judged_false_fire_queries": judged_false_fire,
        "judged_false_fire_rate": (judged_false_fire / judged_any) if judged_any else None,
        "proxy_fired_any_concept": proxy_fired,      # label-free: gate says this should be 0
    }


def _split_map() -> dict:
    """``{query_text: split}`` from eval/analog_queries.yaml (tuning|heldout)."""
    spec = yaml.safe_load(QUERIES_PATH.read_text(encoding="utf-8"))
    qs = spec.get("queries", []) if isinstance(spec, dict) else []
    return {q["query"]: q.get("split", "?") for q in qs}


def _records_for_split(records: list[dict], split: str | None) -> list[dict]:
    """Records whose query is in ``split`` (``None`` → all). Split read from analog_queries.yaml."""
    if split is None:
        return records
    smap = _split_map()
    return [r for r in records if smap.get(r.get("query")) == split]


def score() -> int:
    """Run the metrics against eval/analog_relevance.yaml, or explain how to produce it."""
    if not RELEVANCE_PATH.exists():
        print(f"no judgments yet: {RELEVANCE_PATH} is missing.")
        print("An independent judge must copy eval/analog_candidates_unlabeled.yaml to that path")
        print("and fill `relevant` (all) + `wrong_sense` (trap_wrong) before --score can run.")
        # Still surface the label-free proxy so the gate claim is at least visible now.
        if CANDIDATES_PATH.exists():
            doc = yaml.safe_load(CANDIDATES_PATH.read_text(encoding="utf-8")) or {}
            proxy = polysemy_false_fire_rate(doc.get("records", []))
            print(f"\nlabel-free proxy from {CANDIDATES_PATH.name}:")
            print(f"  trap_wrong queries firing any analog concept: "
                  f"{proxy['proxy_fired_any_concept']}/{proxy['trap_wrong_queries']} "
                  f"(gate target: 0)")
        return 0

    doc = yaml.safe_load(RELEVANCE_PATH.read_text(encoding="utf-8")) or {}
    records = doc.get("records", [])
    print(f"scoring {len(records)} judged records from {RELEVANCE_PATH.name}\n")

    # Build the live-transform context once (semantic sims + corpus boilerplate + gate). Heavy
    # (loads MiniLM); degrades to lexical rerank if unavailable.
    ctx = _ScoreContext()
    if ctx.ok:
        print("  transform: LIVE (gate + corpus-boilerplate + MiniLM semantic rerank)\n")
    else:
        print(f"  transform: LEXICAL fallback (semantic/index unavailable: {ctx.err})\n")

    order = ["overall", "trap_right", "base_miss", "trap_wrong"]

    def report_split(label: str, split: str | None) -> None:
        recs = _records_for_split(records, split)
        n_traps = sum(1 for r in recs if r.get("kind") == "trap_wrong")
        print(f"=== SPLIT: {label} ({len(recs)} queries) ===")
        # Raw precision (no transform) for context.
        for k in (5, None):
            m = precision_and_noise(recs, k=k)
            klabel = f"@{k}" if k else " (all)"
            if m["precision"] is None:
                print(f"  raw precision{klabel}: n/a")
            else:
                print(f"  raw precision{klabel}: {m['precision']:.2%}  "
                      f"({m['relevant_hits']}/{m['labeled_hits']} hits)")
        # Before (raw pool) -> after (live transform), overall + by kind, @5 and (all).
        print("  before (raw) -> after (live transform):")
        for k in (5, None):
            klabel = f"@{k}" if k else "(all)"
            base = precision_by_kind(recs, k=k, rerank=False)
            rr = precision_by_kind(recs, k=k, rerank=True, ctx=ctx)
            print(f"    precision {klabel}:")
            for kind in order:
                b, a = base.get(kind), rr.get(kind)
                if not b or b["precision"] is None:
                    continue
                bp = b["precision"]
                ap = a["precision"] if a and a["precision"] is not None else None
                aptxt = f"{ap:.2%} ({a['relevant']}/{a['total']})" if ap is not None else "n/a"
                print(f"      {kind:<11} {bp:.2%} ({b['relevant']}/{b['total']})  ->  {aptxt}")
        # Wrong-sense (gate-aware): the shipped number, target 0.
        ff = polysemy_false_fire_rate(recs, gate=True)
        if ff["judged_false_fire_rate"] is None:
            print(f"  wrong-sense (gated): n/a (no judged trap_wrong); "
                  f"proxy fired={ff['proxy_fired_any_concept']}/{n_traps}")
        else:
            print(f"  wrong-sense (gated) false-fire: {ff['judged_false_fire_rate']:.2%} "
                  f"({ff['judged_false_fire_queries']}/{ff['judged_queries']} trap_wrong; target 0); "
                  f"proxy fired-any-concept={ff['proxy_fired_any_concept']}/{n_traps}")
        # Ungated comparison so the gate's effect is visible.
        ff0 = polysemy_false_fire_rate(recs, gate=False)
        if ff0["judged_false_fire_rate"] is not None:
            print(f"  wrong-sense (pre-gate, raw pool): {ff0['judged_false_fire_rate']:.2%} "
                  f"({ff0['judged_false_fire_queries']}/{ff0['judged_queries']})")
        print()

    report_split("heldout", "heldout")
    report_split("tuning", "tuning")
    report_split("overall (both splits)", None)

    # Honest coverage: the transform only reorders/drops candidates that already carry a judgment.
    kept_unlabeled = sum(
        1 for rec in records for c in _rerank_record(rec, ctx).get("analog_candidates", [])
        if c.get("relevant") is None
    )
    print(f"  coverage: kept candidates with NO judgment after transform: {kept_unlabeled} "
          f"(transform never promotes an unjudged candidate; precision on judged pool only)")

    # A few concrete hits the transform now suppresses (gate veto / near-dup / boilerplate).
    print("\n  sample suppressed analog hits (gate / near-dup / boilerplate):")
    shown = 0
    for rec in records:
        for c in _dropped_candidates(rec, ctx):
            if shown >= 6:
                break
            print(f"    [{rec.get('kind')}] q={rec.get('query')[:32]!r} {c.get('company')} "
                  f"rel={c.get('relevant')} ws={c.get('wrong_sense')} :: {c.get('snippet', '')[:70]}")
            shown += 1
        if shown >= 6:
            break
    return 0


# --------------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="configs/alpha_go.yaml")
    ap.add_argument("--db", default=None, help="index db (default: the config's index.db_path)")
    ap.add_argument("--collect", action="store_true",
                    help="gather analog-only candidates → eval/analog_candidates_unlabeled.yaml")
    ap.add_argument("--only-new", action="store_true",
                    help="collect ONLY queries tagged `added_in: track-b-expand2` (the new batch); "
                         "defaults the output to eval/analog_candidates2_unlabeled.yaml")
    ap.add_argument("--out", default=None,
                    help="output path for --collect (default: analog_candidates_unlabeled.yaml, "
                         "or analog_candidates2_unlabeled.yaml when --only-new)")
    ap.add_argument("--score", action="store_true",
                    help="compute precision/noise + polysemy false-fire from analog_relevance.yaml")
    args = ap.parse_args()

    if not args.collect and not args.score:
        ap.error("choose --collect (build candidates) or --score (run metrics on judgments)")

    if args.collect:
        config_path = ROOT / args.config
        db = Path(args.db) if args.db else None
        # Select which queries to collect: the whole set, or only the new (added_in) batch.
        spec = yaml.safe_load(QUERIES_PATH.read_text(encoding="utf-8"))
        all_queries = spec.get("queries", []) if isinstance(spec, dict) else []
        if args.only_new:
            queries = [q for q in all_queries if q.get("added_in")]
        else:
            queries = all_queries
        out_path = (Path(args.out) if args.out
                    else (CANDIDATES2_PATH if args.only_new else CANDIDATES_PATH))
        if not out_path.is_absolute():
            out_path = ROOT / out_path
        doc = collect(config_path, db, queries=queries)
        out_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=100),
            encoding="utf-8",
        )
        n_traps_w = sum(1 for r in doc["records"] if r["kind"] == "trap_wrong")
        n_traps_r = sum(1 for r in doc["records"] if r["kind"] == "trap_right")
        n_base = sum(1 for r in doc["records"] if r["kind"] == "base_miss")
        base_verified = sum(1 for r in doc["records"]
                            if r["kind"] == "base_miss" and r["base_literal_miss"])
        traps_fired = sum(1 for r in doc["records"]
                          if r["kind"] == "trap_wrong" and r["fired_concepts"])
        print(f"wrote {out_path.relative_to(ROOT)}")
        print(f"  queries: {doc['n_queries']}  "
              f"(trap_wrong={n_traps_w}, trap_right={n_traps_r}, base_miss={n_base})")
        print(f"  analog-only candidates collected: {doc['n_analog_candidates']}")
        print(f"  base_miss verified to miss without expansion: {base_verified}/{n_base}")
        print(f"  trap_wrong queries that fired any analog concept: {traps_fired}/{n_traps_w} "
              f"(gate target: 0; NOT tuned here)")

    if args.score:
        score()


if __name__ == "__main__":
    main()
