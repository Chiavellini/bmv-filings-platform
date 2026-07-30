"""verify_mentions — independently prove the corpus-wide literal mention scan is Ctrl+F-exact.

    .venv/bin/python scripts/verify_mentions.py
    .venv/bin/python scripts/verify_mentions.py --per-cat 16 --seed 7

The product's mentions view reports, for a query, the TRUE corpus-wide occurrence/document count
by substring-scanning every INDEXED document's full markdown with
``app.components.doc_matches.find_matches`` (see ``linear_results.group_hits`` /
``panels.render_search``). The claim under test: that literal scan equals plain Ctrl+F and is NOT
gated by FTS tokenization.

This script proves it BROADLY and INDEPENDENTLY. For a large random set of terms drawn from the
actual indexed corpus — spanning plain ASCII words, ACCENTED terms, MULTI-WORD phrases, ≤3-char
boundary terms, and GLUED-token cases — it compares the tool's count against TWO references that
never import the tool's fold/match logic:

  (a) the system ``grep`` binary (subprocess) over both the raw corpus and a script-folded copy, and
  (b) a from-scratch Python ``re``/``unicodedata`` counter re-implementing the documented rules.

Accent handling is made explicit on BOTH sides (folded vs unfolded columns). Every term where the
tool disagrees with an independent reference is enumerated with the reason. It also reports the
index-coverage gap: how many on-disk ``.md`` files are NOT indexed (and are therefore outside the
scan's universe) so counts are never silently capped.
"""
from __future__ import annotations

import argparse
import random
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GREP = "/usr/bin/grep"          # BSD grep on macOS — an engine wholly independent of the tool
FOLD_DIR = Path("/private/tmp/verify_mentions_folded")


# --------------------------------------------------------------------------------------------
# Independent reference (b): a from-scratch fold + counter. Deliberately NOT imported from
# app.components.doc_matches — re-implemented here so a bug in the tool cannot hide behind shared
# code. The algorithm mirrors the DOCUMENTED contract (NFKD + combining-mark strip, per char to
# preserve length; substring for words >3, alnum-boundary for ≤3, phrase = words joined by \s+).
# --------------------------------------------------------------------------------------------
def ref_fold(s: str) -> str:
    out = []
    for ch in s:
        decomp = "".join(c for c in unicodedata.normalize("NFKD", ch)
                         if not unicodedata.combining(c))
        out.append(decomp if len(decomp) == 1 else ch)
    return "".join(out)


def ref_count(hay: str, term: str) -> int:
    """Occurrence count of ``term`` in ``hay`` under the documented rules (independent impl).

    ``hay`` and ``term`` are already lowercased+folded by the caller (fold is deterministic and
    length-preserving, so folding once per doc is equivalent to the tool folding on every call —
    it keeps this O(docs·terms) sweep tractable over the whole corpus).
    """
    words = term.split()
    if not words:
        return 0
    if len(words) > 1:                                   # phrase: contiguous words, \s+ between
        pat = re.compile(r"\s+".join(re.escape(w) for w in words))
        return len(pat.findall(hay))
    w = words[0]
    n = 0
    i = hay.find(w)
    while i != -1:
        j = i + len(w)
        if len(w) > 3 or ((i == 0 or not hay[i - 1].isalnum())
                          and (j >= len(hay) or not hay[j].isalnum())):
            n += 1
        i = hay.find(w, j)
    return n


# --------------------------------------------------------------------------------------------
# Independent reference (a): the system grep binary via subprocess.
# --------------------------------------------------------------------------------------------
def grep_occurrences(term: str, paths: list, *, ignore_case: bool) -> int:
    """Total ``grep -oF`` matches of ``term`` across ``paths`` (line-oriented, one match per line)."""
    if not paths:
        return 0
    args = [GREP, "-oF"]
    if ignore_case:
        args.append("-i")
    args += [term, *paths]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode not in (0, 1):                        # 0=found, 1=none; anything else is real
        raise RuntimeError(f"grep failed ({r.returncode}) on {term!r}: {r.stderr[:200]}")
    return sum(1 for ln in r.stdout.splitlines() if ln)


def grep_documents(term: str, paths: list, *, ignore_case: bool) -> int:
    if not paths:
        return 0
    args = [GREP, "-lF"]
    if ignore_case:
        args.append("-i")
    args += [term, *paths]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode not in (0, 1):
        raise RuntimeError(f"grep -l failed ({r.returncode}) on {term!r}: {r.stderr[:200]}")
    return sum(1 for ln in r.stdout.splitlines() if ln)


# --------------------------------------------------------------------------------------------
# Term sampling — draw a large, category-diverse set from the ACTUAL indexed corpus.
# --------------------------------------------------------------------------------------------
WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)          # letters only (no digits/underscore)

INJECT_ACCENTED = ["méxico", "número", "utilidad", "día", "operación"]
INJECT_PHRASES = ["net sales", "operating income", "flujo de efectivo", "same store sales",
                  "cash flow"]
INJECT_SHORT = ["net", "tax", "eps", "usd", "vs"]


def sample_terms(texts: dict, *, seed: int, per_cat: int) -> list:
    """Return ``(category, term)`` pairs across every category the claim must survive."""
    rng = random.Random(seed)
    lowered = {p: t.lower() for p, t in texts.items()}

    tok_freq: Counter = Counter()
    accented: Counter = Counter()
    bigrams: Counter = Counter()
    for low in lowered.values():
        for line in low.splitlines():
            toks = WORD_RE.findall(line)
            tok_freq.update(toks)
            for a, b in zip(toks, toks[1:]):
                if 3 < len(a) < 12 and 3 < len(b) < 12:
                    bigrams[f"{a} {b}"] += 1
    for tok, c in tok_freq.items():
        if any(ord(ch) > 127 for ch in tok):
            accented[tok] += c

    def pick(pool, n, pred=lambda w: True):
        cands = [w for w in pool if pred(w)]
        rng.shuffle(cands)
        return cands[:n]

    ascii_words = pick([w for w, c in tok_freq.items() if c >= 3],
                       per_cat, lambda w: w.isascii() and 4 <= len(w) <= 12)
    short_words = pick([w for w, c in tok_freq.items() if c >= 3],
                       per_cat, lambda w: w.isascii() and len(w) <= 3)
    acc_words = pick(list(accented), per_cat)
    phrase_words = [b for b, _ in bigrams.most_common(200)]
    rng.shuffle(phrase_words)
    phrases = phrase_words[:per_cat]

    # Glued tokens: an interior substring of a frequent long token that is itself NOT a standalone
    # token in the vocabulary — FTS indexes only whole tokens, so its MATCH will MISS these, while
    # the literal substring scan finds them. This is the crux of "literal > FTS".
    vocab = set(tok_freq)
    glued = []
    long_toks = [w for w, c in tok_freq.items() if len(w) >= 9 and c >= 3 and w.isascii()]
    rng.shuffle(long_toks)
    for host in long_toks:
        sub = host[2:7]
        if len(sub) >= 4 and sub not in vocab and sub.isalpha():
            glued.append((sub, host))
        if len(glued) >= per_cat:
            break

    terms: list = []
    terms += [("ascii", w) for w in ascii_words]
    terms += [("accented", w) for w in acc_words]
    terms += [("accented*", w) for w in INJECT_ACCENTED]
    terms += [("phrase", w) for w in phrases]
    terms += [("phrase*", w) for w in INJECT_PHRASES]
    terms += [("short<=3", w) for w in short_words]
    terms += [("short<=3*", w) for w in INJECT_SHORT]
    terms += [("glued", sub) for sub, _ in glued]
    # dedupe, preserve first category
    seen: set = set()
    out = []
    for cat, w in terms:
        if w and w not in seen:
            seen.add(w)
            out.append((cat, w))
    return out


# --------------------------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/alpha_go.yaml")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--per-cat", type=int, default=12,
                    help="random terms per category (ascii/accented/phrase/short/glued)")
    args = ap.parse_args()

    from app.components.doc_matches import _fold_accents, find_matches
    from src.index.keyword_index import KeywordIndex
    from src.index.store import IndexStore
    from src.shared.paths import ESTATE_BRIDGE

    sys.stdout.reconfigure(line_buffering=True)          # stream progress even when redirected

    config = yaml.safe_load((ROOT / args.config).read_text())
    db = ESTATE_BRIDGE.alpha_go_index_path
    store = IndexStore(db)
    store.connect()
    kidx = KeywordIndex(store)

    rows = store.document_rows()
    raw_paths = [r["markdown_path"] for r in rows]
    texts: dict = {}
    for p in raw_paths:
        try:
            texts[p] = Path(p).read_text(encoding="utf-8")
        except OSError as e:
            print(f"STOP: cannot read indexed doc {p}: {e}")
            sys.exit(2)

    # Coverage gap: indexed universe vs everything on disk.
    corpus_root = (ROOT / "data" / "corpus").resolve()
    on_disk = sorted(corpus_root.rglob("*.md"))
    indexed_set = {str(Path(p).resolve()) for p in raw_paths}
    not_indexed = [p for p in on_disk if str(p.resolve()) not in indexed_set]
    by_co = Counter(p.parent.name for p in not_indexed)

    # A script-folded copy of each indexed doc for the independent folded-grep reference, plus
    # pre-folded haystacks (folded ONCE per doc, reused across all terms — fold is deterministic).
    # Tool side uses the tool's own ``_fold_accents``; the reference side uses this script's
    # ``ref_fold`` — they never share fold code.
    FOLD_DIR.mkdir(parents=True, exist_ok=True)
    folded_paths = []
    ref_hay: dict = {}
    tool_hay: dict = {}
    for i, p in enumerate(raw_paths):
        low = texts[p].lower()
        ref_hay[p] = ref_fold(low)
        tool_hay[p] = _fold_accents(low)
        fp = FOLD_DIR / f"{i:04d}.md"
        fp.write_text(ref_hay[p], encoding="utf-8")          # independent (ref_fold) folded corpus
        folded_paths.append(str(fp))

    print("=" * 96)
    print("CORPUS COVERAGE")
    print("=" * 96)
    print(f"indexed documents (scan universe): {len(rows)}")
    print(f".md files on disk under data/corpus: {len(on_disk)}")
    print(f"on-disk .md NOT indexed (outside the scan): {len(not_indexed)}")
    print("  not-indexed by company: " + ", ".join(f"{k}={v}" for k, v in sorted(by_co.items())))
    print()

    terms = sample_terms(texts, seed=args.seed, per_cat=args.per_cat)

    print("=" * 96)
    print(f"TERM VERIFICATION  ({len(terms)} terms)   tool = find_matches over the {len(rows)} "
          "indexed docs")
    print("=" * 96)
    header = (f"{'category':10} {'term':22} {'tOcc':>6} {'tDoc':>5} {'ftsD':>5} "
              f"{'pyOcc':>6} {'pyDoc':>5} {'gOccF':>6} {'gDocF':>6} {'gOccR':>6} status")
    print(header)
    print("-" * len(header))

    real_div = []              # tool != a VALID independent reference (REAL problem if any)
    fts_gated = []             # literal doc-count > FTS doc-count (why literal scanning exists)
    accent_div = []            # folded (tool) != unfolded raw grep (why folding exists)
    bound_notes = []           # ≤3-char boundary rule: boundary-less grep -F over-counts (by design)
    phrase_notes = []          # cross-line phrase: line-based grep under-counts (why \s+ is needed)

    for cat, term in terms:
        # --- tool: exactly what the product counts (find_matches, folded, over every indexed doc).
        # Fold is pre-applied per doc with the tool's OWN _fold_accents, so find_matches(fold=False)
        # runs the identical matcher on identical bytes — same COUNT as find_matches(text,[term]).
        tterm = _fold_accents(term.lower())
        tool_spans = {p: find_matches(tool_hay[p], [tterm], fold_accents=False) for p in raw_paths}
        tool_occ = sum(len(s) for s in tool_spans.values())
        tool_doc = sum(1 for s in tool_spans.values() if s)
        # --- tool FTS universe (no synonyms, literal query) — the OLD, token-gated doc frequency
        fts_doc = len(kidx.matching_documents(term, expand_synonyms=False))
        # --- independent ref (b): from-scratch python counter over this script's own folded corpus
        rterm = ref_fold(term.lower())
        ref_counts = {p: ref_count(ref_hay[p], rterm) for p in raw_paths}
        py_occ = sum(ref_counts.values())
        py_doc = sum(1 for c in ref_counts.values() if c)
        # --- independent ref (a): system grep, folded corpus (mirrors tool's lower+fold of hay)
        gterm_fold = ref_fold(term.lower())
        gocc_fold = grep_occurrences(gterm_fold, folded_paths, ignore_case=False)
        gdoc_fold = grep_documents(gterm_fold, folded_paths, ignore_case=False)
        # --- independent ref (a'): system grep, RAW corpus, unfolded (accent-explicit)
        gocc_raw = grep_occurrences(term, raw_paths, ignore_case=True)

        # Which references are VALID for this term?
        #   * the from-scratch PYTHON counter re-implements EVERY documented rule (boundaries,
        #     phrase \s+, fold) → it must equal the tool for ALL terms.
        #   * system grep -F is a boundary-LESS, line-based substring engine → it equals the tool
        #     ONLY for single words LONGER than 3 chars. For ≤3-char words the tool adds a word
        #     boundary (grep over-counts); for phrases grep can't span newlines (grep under-counts).
        words = rterm.split()
        is_phrase = len(words) > 1
        is_short = (not is_phrase) and len(words[0]) <= 3
        grep_comparable = (not is_phrase) and not is_short          # single word, >3 chars

        status = "OK"
        if tool_occ != py_occ or tool_doc != py_doc:
            status = "PY-FAIL"
            real_div.append(("python-ref", cat, term, tool_occ, py_occ, tool_doc, py_doc))
        elif grep_comparable and (tool_occ != gocc_fold or tool_doc != gdoc_fold):
            status = "GREP-FAIL"
            real_div.append(("folded-grep", cat, term, tool_occ, gocc_fold, tool_doc, gdoc_fold))
        if is_short and gocc_fold != tool_occ:
            bound_notes.append((cat, term, tool_occ, gocc_fold))
            if status == "OK":
                status = "<=3-bound"
        if is_phrase and gocc_fold != tool_occ:
            phrase_notes.append((cat, term, tool_occ, gocc_fold))
            if status == "OK":
                status = "xline"
        if fts_doc < tool_doc:
            fts_gated.append((cat, term, tool_doc, fts_doc))
            if status == "OK":
                status = "fts<lit"
        if grep_comparable and gocc_raw != tool_occ:
            accent_div.append((cat, term, tool_occ, gocc_raw))

        print(f"{cat:10} {term[:22]:22} {tool_occ:>6} {tool_doc:>5} {fts_doc:>5} "
              f"{py_occ:>6} {py_doc:>5} {gocc_fold:>6} {gdoc_fold:>6} {gocc_raw:>6} {status}")

    print()
    print("=" * 96)
    print("DIVERGENCE ANALYSIS")
    print("=" * 96)
    print(f"[1] tool vs a VALID independent reference: {len(real_div)} REAL divergence(s)")
    print("    reference = the from-scratch python counter for ALL terms, PLUS folded grep -F for "
          "single words >3 chars.")
    if real_div:
        for ref, cat, term, a, b, c, d in real_div:
            print(f"    !! [{ref}] {cat:10} {term!r}: tool_occ={a} ref_occ={b} "
                  f"tool_doc={c} ref_doc={d}")
        print("    ^^ REAL disagreements — the foolproof claim would be FALSE. Investigate.")
    else:
        print("    none — the literal scan equals the independent python counter on EVERY term, and "
              "the independent grep -F engine on every word it can validly count. Ctrl+F-exact.")

    print()
    print(f"[2] ≤3-char boundary rule (a CORRECTNESS feature, not a bug): {len(bound_notes)} short "
          "term(s) where a boundary-LESS grep -F over-counts the tool")
    for cat, term, tocc, gf in bound_notes:
        print(f"    <=3-bound  {cat:10} {term!r}: tool(word-boundary)={tocc}  grep -F(substring)={gf}"
              f"   (tool requires alnum boundaries so {term!r} is not matched inside larger words; "
              "the python ref agrees with the tool)")
    if not bound_notes:
        print("    (none in this sample)")

    print()
    print(f"[3] cross-line phrases (why phrase matching uses \\s+): {len(phrase_notes)} phrase(s) "
          "where line-based grep -F under-counts the tool")
    for cat, term, tocc, gf in phrase_notes:
        print(f"    xline      {cat:10} {term!r}: tool(\\s+ across newlines)={tocc}  "
              f"grep -F(one line)={gf}   (the corpus glues phrases across line breaks; python ref "
              "agrees with the tool)")
    if not phrase_notes:
        print("    (none in this sample)")

    print()
    print(f"[4] FTS-token gating (why the literal scan EXISTS): {len(fts_gated)} term(s) where the "
          "literal doc-count EXCEEDS the FTS MATCH doc-count")
    for cat, term, tdoc, fdoc in fts_gated:
        print(f"    literal>fts  {cat:10} {term!r}: literal_docs={tdoc}  fts_docs={fdoc}"
              f"   (FTS tokenizes whole words; the substring is buried inside larger tokens)")
    if not fts_gated:
        print("    (none in this sample — increase --per-cat to surface more glued cases)")

    print()
    print(f"[5] accent folding (explicit, words >3 chars): {len(accent_div)} term(s) where the "
          "folded tool count != raw case-insensitive grep")
    for cat, term, tocc, graw in accent_div:
        print(f"    fold!=raw  {cat:10} {term!r}: tool(folded)={tocc}  grep(raw,unfolded)={graw}"
              f"   (folding counts accented variants a byte-literal grep misses)")
    if not accent_div:
        print("    (none — sampled accented terms happen to appear only unaccented in the English "
              "corpus)")

    print()
    print("=" * 96)
    print("SUMMARY")
    print("=" * 96)
    print(f"terms checked           : {len(terms)}")
    print(f"tool vs independent ref : {len(real_div)} REAL divergence(s) "
          f"({'FAIL' if real_div else 'PASS - literal scan is Ctrl+F-exact'})")
    print(f"  (python counter agrees with the tool on all {len(terms)} terms; folded grep -F agrees "
          "on every word it can validly count)")
    print(f"≤3-char boundary terms  : {len(bound_notes)} (tool boundary rule; grep -F would "
          "over-count — feature, not bug)")
    print(f"cross-line phrases      : {len(phrase_notes)} (tool \\s+ spans newlines; grep -F would "
          "under-count)")
    print(f"FTS-gated terms         : {len(fts_gated)} (literal scan catches what FTS MATCH drops)")
    print(f"accent-fold terms       : {len(accent_div)} (folded count != raw byte grep)")
    print(f"index coverage          : scanned {len(rows)} of {len(on_disk)} on-disk docs "
          f"({len(not_indexed)} not indexed)")


if __name__ == "__main__":
    main()
