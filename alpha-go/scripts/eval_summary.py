"""eval_summary — accuracy eval for the Smart Summaries extractive selector.

A summary is right when it SURFACES the facts a reader would want and does NOT waste slots on
boilerplate. Two metrics, computed against independently-authored gold:

  - COVERAGE@N — of a target's ``salient_facts`` (must-include facts a reader deemed important,
    authored from the SOURCE document, never from the extractor's output), what fraction appear in
    the extractive top-N sentences. This is recall of the important facts.
  - BOILERPLATE-RATE — of the N selected sentences, what fraction the corpus-frequency model flags
    as boilerplate (disclaimers/glossaries/footers). Target ~0.

Honesty rules (same as eval_analogs / eval_sentiment):
  - ``salient_facts`` are authored WITHOUT looking at the extractor output — never tuned to it.
  - Rows carry ``split: tuning|heldout``; the held-out split must NEVER be tuned against. Mint a
    fresh held-out set each round and report it separately.

Run:
    python3 scripts/eval_summary.py --score            # score the gold
    python3 scripts/eval_summary.py --collect          # dump extractor picks to help AUTHOR gold

``--collect`` writes the raw document sentences + the current extractor picks per target so a
labeler can author ``salient_facts`` with context — it never writes labels itself.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLD_PATH = ROOT / "eval" / "summary_gold.yaml"
COLLECT_PATH = ROOT / "eval" / "summary_candidates_unlabeled.yaml"

# Coverage stopwords (EN/ES function words) so a fact matches on its content, not "the/de/con".
_STOP = frozenset(
    """the a an of and to for in on with by is was were are be as at from that this it its our we
    una uno los las del con por para que como fue son era este esta sus y en el la de un lo al se
    reached with year over""".split()
)
_COVERAGE_THRESHOLD = 0.6      # a fact is covered when >=60% of its content tokens are present


def _content_tokens(s: str) -> set:
    """Salient tokens of a string: numbers (comma/dot-stripped) + content words (len>=4)."""
    low = s.lower()
    nums = {re.sub(r"[.,]", "", n) for n in re.findall(r"\d[\d.,]*", low)}
    words = {w for w in re.findall(r"[a-záéíóúñ]{4,}", low) if w not in _STOP}
    return {t for t in (nums | words) if t}


def _covered(fact: str, hay_tokens: set) -> bool:
    ft = _content_tokens(fact)
    if not ft:
        return False
    present = sum(1 for t in ft if t in hay_tokens)
    return present >= max(1, math.ceil(_COVERAGE_THRESHOLD * len(ft)))


def _embedder_and_boiler(config: dict, corpus_root: Path):
    """A deterministic embedder + a corpus-boilerplate model over the eval corpus (best-effort)."""
    from src.index.embeddings import get_embedder
    from src.search.fusion import build_corpus_boilerplate

    try:
        embedder, _ = get_embedder(config)
    except Exception:  # noqa: BLE001 — centrality is optional; scorer still runs
        embedder = None
    docs = []
    for md in sorted(corpus_root.rglob("*.md")):
        try:
            docs.append((str(md), md.read_text(encoding="utf-8")))
        except OSError:
            continue
    boiler = build_corpus_boilerplate(docs) if docs else None
    return embedder, boiler


def _resolve_text(target: dict, corpus_root: Path) -> "tuple[str, str]":
    """(text, markdown_path) for a target, from its ``file`` (relative to the corpus root)."""
    path = corpus_root / target["file"]
    return path.read_text(encoding="utf-8"), str(path)


def score(gold_path: Path) -> int:
    if not gold_path.exists():
        print(f"no gold yet: {gold_path} is missing. Author salient_facts first (see the module "
              "docstring), or run --collect to dump extractor picks for context.")
        return 0

    from src.qa import summarize

    doc = yaml.safe_load(gold_path.read_text(encoding="utf-8")) or {}
    corpus_root = ROOT / doc.get("corpus", "tests/fixtures/sample_corpus")
    max_sentences = int(doc.get("max_sentences", 6))
    targets = doc.get("targets", [])
    config = {"index": {"embedding_backend": "auto"}}
    embedder, boiler = _embedder_and_boiler(config, corpus_root)

    print(f"scoring {len(targets)} target(s) from {gold_path.name} "
          f"(corpus={corpus_root.relative_to(ROOT)}, N={max_sentences})")
    print(f"  embedder: {'ST/hashing' if embedder is not None else 'none (lexical only)'}; "
          f"boilerplate model: {'built' if boiler is not None else 'none'}\n")

    buckets: dict[str, list] = {}     # split -> [facts_covered, facts_total, boiler_selected, selected]
    for t in targets:
        text, md_path = _resolve_text(t, corpus_root)
        summ = summarize.summarize_document(
            text, doc_id=f"{t.get('slug')}/{t.get('period')}", company=t.get("slug", ""),
            period=t.get("period"), markdown_path=md_path, embedder=embedder,
            boilerplate=boiler, max_sentences=max_sentences,
        )
        hay = set()
        for p in summ.points:
            hay |= _content_tokens(p.text)
        facts = t.get("salient_facts", [])
        covered = sum(1 for f in facts if _covered(f, hay))
        boiler_hits = sum(1 for p in summ.points
                          if boiler is not None and boiler.is_boilerplate(p.text))

        for key in (t.get("split", "?"), "overall"):
            b = buckets.setdefault(key, [0, 0, 0, 0])
            b[0] += covered
            b[1] += len(facts)
            b[2] += boiler_hits
            b[3] += len(summ.points)

        miss = [f for f in facts if not _covered(f, hay)]
        print(f"  [{t.get('split','?'):<7}] {t.get('slug')}/{t.get('period')}: "
              f"coverage {covered}/{len(facts)}"
              + (f"  MISS: {miss}" if miss else "")
              + (f"  boilerplate {boiler_hits}/{len(summ.points)}" if boiler_hits else ""))

    print()
    for key in ("heldout", "tuning", "overall"):
        if key not in buckets:
            continue
        cov, tot, bhit, sel = buckets[key]
        cov_pct = f"{cov / tot:.1%}" if tot else "n/a"
        boiler_pct = f"{bhit / sel:.1%}" if sel else "n/a"
        print(f"=== {key:<8} coverage@N: {cov_pct} ({cov}/{tot} facts) · "
              f"boilerplate-rate: {boiler_pct} ({bhit}/{sel} selected)")
    return 0


def collect(gold_path: Path) -> int:
    """Dump each target's raw sentences + current extractor picks so a labeler can author gold."""
    from src.qa import summarize

    doc = yaml.safe_load(gold_path.read_text(encoding="utf-8")) if gold_path.exists() else {}
    corpus_root = ROOT / doc.get("corpus", "tests/fixtures/sample_corpus")
    max_sentences = int(doc.get("max_sentences", 6))
    embedder, boiler = _embedder_and_boiler({"index": {"embedding_backend": "auto"}}, corpus_root)

    records = []
    for md in sorted(corpus_root.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        summ = summarize.summarize_document(text, markdown_path=str(md), embedder=embedder,
                                            boilerplate=boiler, max_sentences=max_sentences)
        records.append({
            "file": str(md.relative_to(corpus_root)),
            "all_sentences": [s for s, _, _ in summarize.split_sentences(text)],
            "extractor_picks": [p.text for p in summ.points],
            # salient_facts intentionally OMITTED — a human authors them from all_sentences.
        })
    out = {
        "note": ("UNLABELED. Author eval/summary_gold.yaml by writing per-target `salient_facts` "
                 "from `all_sentences` (NOT from `extractor_picks`), with a `split: tuning|heldout`."),
        "corpus": doc.get("corpus", "tests/fixtures/sample_corpus"),
        "records": records,
    }
    COLLECT_PATH.write_text(yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=100),
                            encoding="utf-8")
    print(f"wrote {COLLECT_PATH.relative_to(ROOT)} ({len(records)} document(s))")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", default=str(GOLD_PATH))
    ap.add_argument("--score", action="store_true", help="score coverage@N + boilerplate-rate")
    ap.add_argument("--collect", action="store_true",
                    help="dump raw sentences + extractor picks to help author gold")
    args = ap.parse_args()
    if not args.score and not args.collect:
        ap.error("choose --score or --collect")
    gold = Path(args.gold)
    if not gold.is_absolute():
        gold = ROOT / gold
    if args.collect:
        collect(gold)
    if args.score:
        score(gold)


if __name__ == "__main__":
    main()
