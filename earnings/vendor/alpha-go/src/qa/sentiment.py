"""Light sentiment of a mention — hybrid (offline lexicon by default, optional LLM refine).

The search UI shows a quick tonal read of each matched passage ("is this a positive or a
cautious mention?"). Two engines, same ``SentimentResult`` shape:

- **lexicon** (default, deterministic, offline): score the snippet from the curated financial
  polarity word lists in ``configs/sentiment_lexicon.yaml``, with negators flipping polarity
  within a short window ("no pressure" → positive; "not strong" → negative). Runs on every hit.
- **llm** (optional): when the ``sentiment`` engine is ``hybrid``/``llm`` and the configured
  provider resolves (DeepSeek by default), refine a passage for a context-aware read of
  "fluff"/negation the lexicon misses. Goes through the shared :mod:`src.qa.llm` provider layer
  and never runs unless explicitly enabled.

Keep this pure/offline by default so the test suite and dashboard stay hermetic.
"""
from __future__ import annotations

import functools
import re
import unicodedata
from dataclasses import dataclass

import yaml

from src.shared.paths import CONFIGS_DIR

_LEXICON_PATH = CONFIGS_DIR / "sentiment_lexicon.yaml"
_NEG_WINDOW = 3          # a negator flips a polarity word up to this many tokens later
_LABEL_THRESHOLD = 0.15  # |score| below this reads as neutral
# A polar label needs real evidence: long passages with a single stray cue read as neutral.
# Short texts (a sentence or two) may be labeled from one clear cue.
_MIN_CUES_LONG = 2
_LONG_TEXT_TOKENS = 60

# Deleveraging is good news the bag-of-cues scorer misreads: "reduction" reads down/negative and
# the "debt" object often sits outside the small object window. These proximity patterns catch a
# debt term paired (either order, within ~60 chars) with a down/up move on the accent-folded text.
# NB: "apalancamiento operativo" (operating leverage) is not debt — keep only debt/indebtedness.
_DEBT_TERM = r"(?:net debt|debt net|total debt|deuda neta|deuda total|la deuda|de deuda|" \
             r"endeudamiento|deuda financiera)"
_DEBT_DOWN = r"(?:reduc\w*|lower|decreas\w*|redujo|disminu\w*|menor\w*|baja\w*|desapalanca\w*)"
_DEBT_UP = r"(?:increas\w*|higher|increment\w*|aument\w*|mayor\w*|creci\w*|elev\w*)"
_DEBT_GAP = r".{0,60}?"
_DELEVERAGE_RE = re.compile(
    rf"(?:deleverag\w*|desapalanca\w*|{_DEBT_DOWN}{_DEBT_GAP}{_DEBT_TERM}|"
    rf"{_DEBT_TERM}{_DEBT_GAP}{_DEBT_DOWN})")
_DEBT_INCREASE_RE = re.compile(
    rf"(?:{_DEBT_UP}{_DEBT_GAP}{_DEBT_TERM}|{_DEBT_TERM}{_DEBT_GAP}{_DEBT_UP})")

# A forward-looking passage with no concrete evidence (no figures, no strong result word) is vague
# optimism → neutral per the gold rubric; concrete optimism ("grew 10%", "record", "saved $500M")
# keeps its polarity. These are the "concrete" signals that veto the demotion.
_STRONG_POSITIVE = frozenset((
    "record", "outstanding", "exceeded", "beat", "historic", "historico", "destacado",
    "sobresaliente", "all-time", "accretive",
))
_DIGIT_RE = re.compile(r"\d")

# Language-exclusive function words used to pick the lexicon. These barely overlap between the
# two languages, so even a short earnings sentence lands on the right side; ties default to en.
_ES_MARKERS = frozenset((
    "de la el que los las del una por con para se su al y en un lo "
    "nuestro nuestros nuestra nuestras fue fueron ano anos trimestre durante "
    "resultados ingresos ventas millones respecto periodo cierre ademas mismo "
    "este esta"
).split())
_EN_MARKERS = frozenset(
    "the of and to for our with was were is this an by".split())


@dataclass
class SentimentResult:
    label: str        # "positive" | "neutral" | "negative"
    score: float      # net polarity in [-1, 1]
    rationale: str    # short human-readable explanation
    engine: str       # "lexicon" | "llm"


@dataclass(frozen=True)
class _Lexicon:
    positive: frozenset
    negative: frozenset
    negators: frozenset
    directions_up: frozenset
    directions_down: frozenset
    negative_objects: frozenset
    positive_objects: frozenset
    # Context guards (accent-folded phrase lists, matched on the full text). See the lexicon YAML.
    neutral_domains: tuple = ()      # boilerplate domains → force neutral
    positive_domains: tuple = ()     # award/recognition/expansion PR → force positive
    offset_markers: tuple = ()       # "offset by" → trailing (offsetting) clause is subordinate
    concession_markers: tuple = ()   # "despite X, Y" → leading (concession) clause is subordinate
    forward_markers: tuple = ()      # future-modal optimism → vague forward-looking reads neutral


def _fold(word: str) -> str:
    """Lowercase and strip accents so 'presión'/'caída' match ASCII lexicon keys."""
    nfkd = unicodedata.normalize("NFKD", word.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _lexicon_from(raw: dict, guards: dict | None = None) -> _Lexicon:
    def _set(key: str) -> frozenset:
        return frozenset(_fold(w) for w in raw.get(key, []) or [])

    def _phrases(key: str) -> tuple:
        return tuple(_fold(p) for p in (guards or {}).get(key, []) or [])

    return _Lexicon(
        positive=_set("positive"), negative=_set("negative"), negators=_set("negators"),
        directions_up=_set("directions_up"), directions_down=_set("directions_down"),
        negative_objects=_set("negative_objects"), positive_objects=_set("positive_objects"),
        neutral_domains=_phrases("neutral_domains"),
        positive_domains=_phrases("positive_domains"),
        offset_markers=_phrases("offset_markers"),
        concession_markers=_phrases("concession_markers"),
        forward_markers=_phrases("forward_markers"),
    )


@functools.lru_cache(maxsize=1)
def _load_lexicons() -> dict:
    """Return {"en": _Lexicon, "es": _Lexicon}.

    Supports both the sectioned schema ({en: {...}, es: {...}}) and a legacy flat schema
    (top-level word lists), which is loaded as English so old configs keep working. A separate
    top-level ``guards`` section carries per-language context-guard phrase lists.
    """
    raw = yaml.safe_load(_LEXICON_PATH.read_text(encoding="utf-8")) or {}
    guards = raw.get("guards", {}) or {}
    if "en" in raw or "es" in raw:
        return {lang: _lexicon_from(raw.get(lang, {}) or {}, guards.get(lang, {}) or {})
                for lang in ("en", "es")}
    flat = _lexicon_from(raw)                        # legacy: one flat English lexicon
    return {"en": flat, "es": flat}


def _tokenize(text: str) -> list[str]:
    # Keep apostrophes so "n't" survives as part of contractions (don't → don, 't); we match the
    # "n't" negator against a trailing-token check below. Accented letters are kept then folded
    # so Spanish words survive tokenization.
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", text.lower(), flags=re.UNICODE)
    return [_fold(w) for w in words]


def _fold_text(text: str) -> str:
    """Accent-fold and lowercase the WHOLE text (punctuation kept) for phrase-guard matching."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _detect_lang(tokens: list[str]) -> str:
    es = sum(1 for t in tokens if t in _ES_MARKERS)
    en = sum(1 for t in tokens if t in _EN_MARKERS)
    return "es" if es > en else "en"


def _is_negated(tokens: list[str], i: int, negators: frozenset) -> bool:
    lo = max(0, i - _NEG_WINDOW)
    return any(t in negators or t.endswith("n't") for t in tokens[lo:i])


_OBJECT_WINDOW = 4       # a directional word looks this many tokens around it for its object


def _direction_polarity(tokens: list[str], i: int, direction: int, lex: _Lexicon) -> int:
    """Polarity of a directional word given what is moving.

    "increase in the excise tax" → up × negative_object → −1; "reduced costs" → down ×
    negative_object → +1; "revenue increased" → up × positive_object → +1. With no known
    object in the window, the direction's own sign applies ("momentum accelerated" → +1).

    The NEAREST object wins, so "higher costs" is not hijacked by a "sales" further away.
    """
    lo, hi = max(0, i - _OBJECT_WINDOW), min(len(tokens), i + _OBJECT_WINDOW + 1)
    for j in sorted(range(lo, hi), key=lambda j: (abs(j - i), j)):
        if j == i:
            continue
        if tokens[j] in lex.negative_objects:
            return -direction
        if tokens[j] in lex.positive_objects:
            return direction
    return direction


def _count_cues(tokens: list[str], lex: _Lexicon) -> tuple[int, int, list[str], list[str]]:
    """Tally positive/negative cues over ``tokens`` (with negation + direction/object logic)."""
    pos = neg = 0
    pos_terms: list[str] = []
    neg_terms: list[str] = []
    for i, tok in enumerate(tokens):
        if tok in lex.directions_up:
            polarity = _direction_polarity(tokens, i, 1, lex)
        elif tok in lex.directions_down:
            polarity = _direction_polarity(tokens, i, -1, lex)
        else:
            polarity = 1 if tok in lex.positive else (-1 if tok in lex.negative else 0)
        if polarity == 0:
            continue
        if _is_negated(tokens, i, lex.negators):
            polarity = -polarity
        if polarity > 0:
            pos += 1
            pos_terms.append(tok)
        else:
            neg += 1
            neg_terms.append(tok)
    return pos, neg, pos_terms, neg_terms


def _first_marker_pos(folded: str, markers: tuple) -> int:
    """Character index of the earliest offset marker in ``folded`` (or -1 if none present)."""
    best = -1
    for m in markers:
        idx = folded.find(m)
        if idx != -1 and (best == -1 or idx < best):
            best = idx
    return best


def _has_marker(folded: str, markers: tuple) -> bool:
    return any(m in folded for m in markers)


def score_sentiment(text: str) -> SentimentResult:
    """Offline lexicon sentiment for a passage. Always available; never touches the network.

    Language is auto-detected (Spanish vs English) so the matching lexicon section applies —
    Spanish prose is scored, not silently read neutral. Before bag-of-cues scoring, context
    guards catch the systematic false-fires of a keyword scorer on earnings prose:
    valence-free boilerplate domains read neutral, deleveraging reads positive, an offsetting
    clause defers to the dominant prior clause, and vague forward-looking optimism reads neutral.
    """
    tokens = _tokenize(text)
    lex = _load_lexicons()[_detect_lang(tokens)]
    folded = _fold_text(text)

    # Guard 1 — valence-free boilerplate domains (accounting policy, notes/methodology, hedging &
    # risk-management objectives, share/dividend mechanics, education/training CSR) read neutral:
    # their cue words ("perdida", "crecimiento", "higher") are terminology, not tone.
    if _has_marker(folded, lex.neutral_domains):
        return SentimentResult("neutral", 0.0, "valence-free boilerplate domain", "lexicon")

    # Guard 1b — award / recognition / expansion PR reads positive even without a lexicon cue
    # ("was recognized as the retailer of the year", "opened new distribution centers"): these are
    # unambiguously good-news domains that otherwise land neutral (no polarity word) or negative
    # (a stray "reduction"). Checked after neutral_domains so accounting/CSR-neutral prose wins.
    if _has_marker(folded, lex.positive_domains):
        return SentimentResult("positive", 0.5, "award / recognition / expansion", "lexicon")

    # Guard 2 — deleveraging is good news ("net debt reduced 45%" → positive), a rising debt load
    # is bad ("net debt increased"). These override the misleading direction-word reading.
    if _DELEVERAGE_RE.search(folded) and not _DEBT_INCREASE_RE.search(folded):
        return SentimentResult("positive", 0.6, "deleveraging / debt reduction", "lexicon")
    if _DEBT_INCREASE_RE.search(folded) and not _DELEVERAGE_RE.search(folded):
        return SentimentResult("negative", -0.6, "rising debt load", "lexicon")

    # Guard 3 — two-clause DOMINANCE: a subordinate clause defers to the dominant one, so a passage
    # with both a positive and a negative clause is read on its primary clause, not net cue-count.
    #  (a) concession — "Despite/A pesar de X, Y": the LEADING concession clause X is subordinate;
    #      score the MAIN clause Y (after the concession's comma) when it carries a cue.
    #  (b) offset — "... partially offset by Z": the TRAILING clause Z is subordinate; score the
    #      dominant prior clause when it already carries a cue ("favorable prices ... offset by FX").
    scoring_tokens = tokens
    conc_pos = _first_marker_pos(folded, lex.concession_markers)
    if conc_pos != -1:
        comma = text.find(",", conc_pos)
        if comma != -1:
            main_tokens = _tokenize(text[comma + 1:])
            if any(_count_cues(main_tokens, lex)[:2]):
                scoring_tokens = main_tokens
    marker_pos = _first_marker_pos(folded, lex.offset_markers)
    if marker_pos > 0:
        head_tokens = _tokenize(text[:marker_pos])
        if any(_count_cues(head_tokens, lex)[:2]):
            scoring_tokens = head_tokens

    pos, neg, pos_terms, neg_terms = _count_cues(scoring_tokens, lex)

    total = pos + neg
    if total == 0:
        return SentimentResult("neutral", 0.0, "no tonal cues", "lexicon")
    score = (pos - neg) / total
    label = ("positive" if score > _LABEL_THRESHOLD
             else "negative" if score < -_LABEL_THRESHOLD else "neutral")

    # Guard 4 — vague forward-looking optimism reads neutral: a positive lean carried only by
    # future-modal language, with no concrete evidence (no figures, no strong result word), is not
    # a polar read ("confident we will drive sustainable growth"). Concrete optimism is untouched.
    if (label == "positive" and _has_marker(folded, lex.forward_markers)
            and not _DIGIT_RE.search(text)
            and not any(t in _STRONG_POSITIVE for t in tokens)):
        return SentimentResult(
            "neutral", round(score, 3),
            "vague forward-looking optimism — treated as neutral", "lexicon")

    # Neutral is the honest default: a long passage with one stray cue is not a polar read.
    if label != "neutral" and total < _MIN_CUES_LONG and len(scoring_tokens) > _LONG_TEXT_TOKENS:
        return SentimentResult(
            "neutral", round(score, 3),
            f"only {total} tonal cue in a long passage — treated as neutral", "lexicon")
    drivers = list(dict.fromkeys(pos_terms if score >= 0 else neg_terms))[:4]
    rationale = f"{pos} positive / {neg} negative cues"
    if drivers:
        rationale += f" (e.g. {', '.join(drivers)})"
    return SentimentResult(label, round(score, 3), rationale, "lexicon")


# Highlight palette — the color a MENTION's highlight box takes so tone reads at a glance:
# green = good news, red = bad, yellow = neutral (the AlphaSense-gold, kept for the neutral case).
HIGHLIGHT_COLORS = {"positive": "#3fb950", "negative": "#f85149", "neutral": "#f5c518"}


def sentiment_color(text: str) -> str:
    """Highlight color for a passage's tone (green/red/yellow), via the offline lexicon."""
    return HIGHLIGHT_COLORS.get(score_sentiment(text).label, HIGHLIGHT_COLORS["neutral"])


# Opt-in LLM tier. This rubric-aligned zero-shot prompt scored acc 0.910 / macro-F1 0.915 on the
# 200-snippet held-out test (2026-07-08, DeepSeek deepseek-chat) vs the lexicon's 0.755 / 0.693 —
# a decisive, non-overlapping-CI win, driven by the hard cases the bag-of-cues lexicon can't reach
# (cross-sentence anaphora, numeric comparison, context-inverted directionals). OFF by default:
# enabled only when ``sentiment.engine`` is ``llm``/``hybrid`` AND the provider key is present, so
# the offline lexicon stays the hermetic default and nothing here touches the network under pytest.
_LLM_SYSTEM = (
    "You classify the TONAL THRUST of a sentence from a company earnings report as exactly one of: "
    "positive, neutral, or negative. positive = dominant good news (revenue/margin/EBITDA up, costs "
    "down, records, strength) OR optimism tied to a CONCRETE realized result. negative = dominant "
    "bad news (declines, losses, margin/volume down, cost/FX/inflation pressure, impairments). "
    "neutral = bare factual/boilerplate (accounting-policy notes, store/share/debt figures stated "
    "flatly, bios, CSR, definitions) OR vague forward-looking optimism with no concrete result OR "
    "genuinely mixed where cues cancel. Rules: for 'up but down' pick the DOMINANT clause; "
    "'reduction in net debt'=positive; an 'offset by' clause is subordinate. Spanish and English "
    "use the same rubric. Answer with ONLY one word: positive, neutral, or negative."
)


def llm_enabled(config: dict | None) -> bool:
    """True when the config opts into the LLM tier AND the provider key resolves."""
    from src.qa import llm

    block = (config or {}).get("sentiment", {})
    if block.get("engine") not in ("llm", "hybrid"):
        return False
    return llm.resolve_provider(block) is not None


def refine_with_llm(text: str, config: dict | None) -> SentimentResult | None:
    """Context-aware sentiment via the opt-in LLM tier. Returns None unless enabled.

    Gated on ``config['sentiment']['engine']`` being ``llm``/``hybrid`` AND a resolvable provider
    key (DeepSeek by default). Results are disk-cached (namespace ``sentiment``) so a passage is
    scored once. Any import/API failure degrades to ``None`` → the caller falls back to the lexicon.
    """
    from src.qa import llm, llm_cache

    block = (config or {}).get("sentiment", {})
    if block.get("engine") not in ("llm", "hybrid"):
        return None
    provider = llm.resolve_provider(block)
    if provider is None:
        return None
    excerpt = text.strip()[:1500]
    use_cache = block.get("cache", True)
    if use_cache:
        hit = llm_cache.get("sentiment", provider.model, excerpt)
        if hit is not None:
            return SentimentResult(hit["label"], hit["score"],
                                   hit.get("rationale", "LLM-assessed tone"), "llm")
    try:
        out = llm.complete(provider, system=_LLM_SYSTEM, user=excerpt, max_tokens=4)
    except Exception:  # noqa: BLE001 — network/SDK failures fall back to lexicon
        return None
    out = (out or "").strip().lower()
    label = next((w for w in ("positive", "negative", "neutral") if w in out), None)
    if label is None:
        return None
    score = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}[label]
    if use_cache:
        llm_cache.put("sentiment", provider.model, excerpt,
                      {"label": label, "score": score, "rationale": "LLM-assessed tone"})
    return SentimentResult(label, score, "LLM-assessed tone", "llm")


def analyze(text: str, config: dict | None = None, *, refine: bool = False) -> SentimentResult:
    """Lexicon sentiment, optionally upgraded to the LLM read when ``refine`` and enabled."""
    if refine:
        llm = refine_with_llm(text, config)
        if llm is not None:
            return llm
    return score_sentiment(text)
