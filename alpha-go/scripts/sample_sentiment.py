"""sample_sentiment — build an UNLABELED, unbiased sentiment eval sample from the corpus.

    python3 scripts/sample_sentiment.py                 # ~200 snippets → eval/sentiment_sample_unlabeled.yaml
    python3 scripts/sample_sentiment.py --n 300 --out eval/foo.yaml

Why this exists: the original gold set (``eval/sentiment_labels.yaml``) was authored AND tuned
against by the same hand, with no held-out split — an inflated number. This sampler draws a
FRESH, SEEDED random sample of real corpus prose for an INDEPENDENT labeler to annotate. To
remove the cherry-picking bias, snippets are selected WITHOUT any reference to sentiment cue
words: we stratify only by company and detected language for coverage, and never look at
polarity. Boilerplate, headers, and pure-number table rows are skipped as non-prose.

The emitted file has NO ``label`` field — labeling happens downstream, by a different agent.
"""
from __future__ import annotations

import argparse
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reuse the ENGINE's own language router so the sample's lang slices match how the scorer
# actually routes each snippet at eval time. Import only — no sentiment logic is modified here,
# and language detection is orthogonal to polarity (no cue-word peeking).
from src.qa.sentiment import _detect_lang, _tokenize  # noqa: E402

SEED = 20260707
CORPUS_DIR = ROOT / "data" / "corpus"
DEFAULT_OUT = ROOT / "eval" / "sentiment_sample_unlabeled.yaml"
DEFAULT_N = 200

_MIN_WORDS = 6           # a self-contained clause, not a fragment
_MAX_WORDS = 60          # ~1–3 sentences, not a whole paragraph dump
_MIN_CHARS = 40
_MAX_CHARS = 360
_MAX_DIGIT_FRAC = 0.25   # above this a "sentence" is really a table row / figure list

# Repeated legal/formatting boilerplate — skipped so the sample is earnings PROSE, not fine
# print. Matched case-insensitively as substrings; none of these are sentiment-bearing.
_BOILERPLATE = (
    "free translation",
    "good faith estimates",
    "future events, risks and uncertainties",
    "risks and uncertainties",
    "forward-looking statements",
    "safe harbor",
    "earnings per share",
    "eps=",
    "not audited",
    "conference call",
    "webcast",
    "investor relations",
    "about the company",
    "all rights reserved",
    "this document may contain",
    "this report may contain",
    "should be considered as good faith",
    "actual results",
)

# A line is structural noise (page marker / markdown header / underline rule) — never prose.
_NOISE_LINE = re.compile(r"^\s*(#|=====|-{3,}|\*{3,}|página\b|page\b)", re.IGNORECASE)

# Sentence break: end punctuation preceded by a lowercase letter / % / ) and followed by an
# uppercase (incl. Spanish) opener. The lowercase-before guard avoids splitting inside
# abbreviations ("S.A.B.", "Ps.") and decimals ("3.08"); imperfect but conservative.
_SENT_SPLIT = re.compile(r'(?<=[a-záéíóúñ%\)])[.!?]+\s+(?=[A-ZÁÉÍÓÚÑ¿¡"“])')

_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
_ALPHA_TOKEN_RE = re.compile(r"[^\W\d_]+", re.UNICODE)   # incl. single letters, for junk checks
_NUMBER_RE = re.compile(r"[<>]?\d[\d.,]*")               # standalone figure groups (table cells)
# Trailing close-quote/paren then terminal punctuation → a self-contained sentence, not a
# fragment cut off by an interrupting table row.
_ENDS_SENTENCE = re.compile(r'[.!?]["”’\')]?$')


def _iter_prose_paragraphs(text: str):
    """Yield paragraphs of consecutive prose lines, table/header/blank lines acting as breaks."""
    buf: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _NOISE_LINE.match(line) or not _is_prose_line(line):
            if buf:
                yield " ".join(buf)
                buf = []
            continue
        buf.append(line)
    if buf:
        yield " ".join(buf)


def _is_prose_line(line: str) -> bool:
    """Prose = enough words and not dominated by digits (a table row is digit-heavy)."""
    words = _WORD_RE.findall(line)
    if len(words) < 4:
        return False
    non_space = [c for c in line if not c.isspace()]
    if not non_space:
        return False
    digit_frac = sum(c.isdigit() for c in non_space) / len(non_space)
    return digit_frac <= 0.35


def _split_sentences(paragraph: str) -> list[str]:
    paragraph = re.sub(r"\s+", " ", paragraph).strip()
    return [s.strip() for s in _SENT_SPLIT.split(paragraph) if s.strip()]


def _is_candidate(sentence: str) -> bool:
    if not (_MIN_CHARS <= len(sentence) <= _MAX_CHARS):
        return False
    # Self-contained: must both open like a sentence (capital / ¿¡) and terminate as one — not a
    # mid-clause fragment left by an interrupting table row.
    head = sentence.lstrip()
    if not head or not (head[0].isupper() or head[0] in "¿¡"):
        return False
    if not _ENDS_SENTENCE.search(sentence.rstrip()):
        return False
    if "@" in sentence:                                 # contact blocks / email lists
        return False
    words = _WORD_RE.findall(sentence)
    if not (_MIN_WORDS <= len(words) <= _MAX_WORDS):
        return False
    non_space = [c for c in sentence if not c.isspace()]
    if sum(c.isdigit() for c in non_space) / len(non_space) > _MAX_DIGIT_FRAC:
        return False
    if len(_NUMBER_RE.findall(sentence)) > 3:           # >3 figure groups ⇒ a table row
        return False
    # Spaced-out caps ("CON FER EN CE CA LL") and stray-letter noise leave many 1-char tokens.
    if sum(1 for t in _ALPHA_TOKEN_RE.findall(sentence) if len(t) == 1) >= 4:
        return False
    alpha = [c for c in non_space if c.isalpha()]
    if not alpha or sum(c.isupper() for c in alpha) / len(alpha) > 0.5:   # ALL-CAPS headers
        return False
    low = sentence.lower()
    if any(b in low for b in _BOILERPLATE):
        return False
    # Must read as a real clause: a decent share of alphabetic characters.
    if sum(c.isalpha() for c in non_space) / len(non_space) < 0.6:
        return False
    return True


def _norm_text(text: str) -> str:
    """Normalize a snippet for overlap comparison: lowercase + whitespace-collapsed.

    Matches the dedup key used in ``collect_candidates`` so an ``--exclude`` file drops not just
    byte-identical picks but near-identical ones (differing only in case/spacing).
    """
    return re.sub(r"\s+", " ", text.lower()).strip()


def load_exclude_keys(paths: list[str]) -> set[str]:
    """Collect NORMALIZED snippet ``text`` values from one or more YAML files to skip in the draw.

    Each file is a mapping with a ``snippets`` list of records carrying a ``text`` field (the
    same shape this script emits, and the shape of the labeled gold). Never inspects labels.
    """
    keys: set[str] = set()
    for p in paths:
        path = Path(p)
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            sys.exit(f"--exclude file not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for rec in data.get("snippets", []) or []:
            text = rec.get("text") if isinstance(rec, dict) else None
            if text:
                keys.add(_norm_text(str(text)))
    return keys


def _company_of(path: Path) -> str:
    rel = path.relative_to(CORPUS_DIR)
    return rel.parts[0] if len(rel.parts) > 1 else "unknown"


def collect_candidates(exclude: set[str] | None = None) -> list[dict]:
    """Every unique prose snippet in the corpus, tagged with company + detected language.

    Selection never inspects sentiment — only structure (prose vs table) and length. Snippets
    whose NORMALIZED text is in ``exclude`` (e.g. the existing gold set) are dropped so a fresh
    draw never re-samples them.
    """
    exclude = exclude or set()
    seen: set[str] = set()
    out: list[dict] = []
    for md in sorted(CORPUS_DIR.rglob("*.md")):
        company = _company_of(md)
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # iCloud eviction etc. — fail loudly, don't silently under-sample
            sys.exit(f"cannot read {md}: {exc} (materialize the corpus first)")
        for para in _iter_prose_paragraphs(text):
            for sent in _split_sentences(para):
                if not _is_candidate(sent):
                    continue
                key = re.sub(r"\s+", " ", sent.lower()).strip()
                if key in seen or key in exclude:
                    continue
                seen.add(key)
                lang = _detect_lang(_tokenize(sent))
                out.append({"text": sent, "company": company, "lang": lang})
    return out


def stratified_sample(candidates: list[dict], n: int, rng: random.Random) -> list[dict]:
    """Round-robin over (company, lang) strata so coverage is balanced, not size-weighted.

    Each stratum is shuffled; we pull one snippet per stratum per pass until we hit ``n`` or
    exhaust the pool. This deliberately equalizes company/lang weight instead of letting the
    biggest corpus dirs dominate — the sample is for coverage, not frequency estimation.
    """
    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in candidates:
        strata[(c["company"], c["lang"])].append(c)
    for items in strata.values():
        rng.shuffle(items)

    order = sorted(strata)                      # deterministic stratum order
    picked: list[dict] = []
    cursor = {k: 0 for k in order}
    progressed = True
    while len(picked) < n and progressed:
        progressed = False
        for k in order:
            if len(picked) >= n:
                break
            i = cursor[k]
            if i < len(strata[k]):
                picked.append(strata[k][i])
                cursor[k] = i + 1
                progressed = True
    return picked


def _print_distribution(sample: list[dict]) -> None:
    by_company = Counter(s["company"] for s in sample)
    by_lang = Counter(s["lang"] for s in sample)
    by_pair = Counter((s["company"], s["lang"]) for s in sample)

    print(f"\nsampled {len(sample)} snippets (class-agnostic — NO sentiment labels)")
    print("\n  per language:")
    for lang, k in sorted(by_lang.items()):
        print(f"    {lang:>4}  {k:>4}")
    print("\n  per company · lang:")
    print(f"    {'company':<12}{'es':>5}{'en':>5}{'total':>7}")
    for company in sorted(by_company):
        es = by_pair.get((company, "es"), 0)
        en = by_pair.get((company, "en"), 0)
        print(f"    {company:<12}{es:>5}{en:>5}{by_company[company]:>7}")
    print(f"    {'TOTAL':<12}{by_lang.get('es', 0):>5}{by_lang.get('en', 0):>5}{len(sample):>7}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n", type=int, default=DEFAULT_N, help="target sample size (default 200)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output YAML path")
    ap.add_argument("--seed", type=int, default=SEED,
                    help=f"RNG seed override for the draw (default {SEED})")
    ap.add_argument("--exclude", action="append", default=[], metavar="YAML",
                    help="YAML file(s) whose snippet `text` values are excluded from the draw "
                         "(matched by normalized text). Repeatable.")
    ap.add_argument("--id-prefix", default="s", metavar="P",
                    help="prefix for record ids (default 's'; use e.g. 't' for a fresh set that "
                         "does not collide with the gold's s0001..).")
    args = ap.parse_args()

    if not CORPUS_DIR.exists():
        sys.exit(f"corpus not found at {CORPUS_DIR}")

    exclude_keys = load_exclude_keys(args.exclude)
    if args.exclude:
        print(f"excluding {len(exclude_keys)} normalized snippet(s) from "
              f"{len(args.exclude)} file(s)")

    rng = random.Random(args.seed)
    candidates = collect_candidates(exclude_keys)
    if not candidates:
        sys.exit("no prose candidates found — is the corpus materialized?")
    print(f"{len(candidates)} unique prose candidates across the corpus")

    sample = stratified_sample(candidates, args.n, rng)
    # Stable id order: sort the drawn sample by (company, lang, text) so the file is diff-friendly
    # and the ids are reproducible run to run.
    sample.sort(key=lambda s: (s["company"], s["lang"], s["text"]))
    records = [
        {"id": f"{args.id_prefix}{i:04d}", "text": s["text"],
         "company": s["company"], "lang": s["lang"]}
        for i, s in enumerate(sample, 1)
    ]

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    header = (
        "# UNLABELED sentiment eval sample — seeded random draw from data/corpus/**/*.md.\n"
        f"# Generated by scripts/sample_sentiment.py (seed={args.seed}); DO NOT hand-edit.\n"
        "#\n"
        "# Selection is sentiment-AGNOSTIC: snippets are chosen by structure (prose vs table)\n"
        "# and stratified only by company + detected language — NEVER by polarity cue words.\n"
        "# This is the de-biased replacement for the self-authored eval/sentiment_labels.yaml.\n"
        "#\n"
        "# An INDEPENDENT labeler adds a `label:` (positive|negative|neutral) and optional\n"
        "# `tags:`/`split:` (dev|test) field to each record. There is intentionally NO label here.\n"
        "# Records: {id, text, company, lang}\n"
    )
    out_path.write_text(
        header + yaml.safe_dump({"snippets": records}, allow_unicode=True, sort_keys=False,
                                width=1000, default_flow_style=False),
        encoding="utf-8",
    )
    print(f"\nwrote {len(records)} records → {out_path.relative_to(ROOT)}")
    _print_distribution(sample)


if __name__ == "__main__":
    main()
