"""Sentiment eval — accuracy / macro-F1 / per-class P·R / confusion over a labeled gold set.

Parallels ``src/search/evaluate.py``: a cheap, deterministic harness that makes "did the
lexicon get better?" measurable instead of vibes. A snippet is scored correctly when
``score_sentiment(text).label`` equals its hand-read gold label. Pure service layer —
``scripts/eval_sentiment.py`` is the CLI.

HONEST-EVAL upgrades (Track C, 2026-07): the original gold set was self-authored AND tuned
against with no held-out split, so its 97% is inflated. This harness now supports:
  - a ``split`` field per record (dev | test) so a headline number can be read on held-out data;
  - a 95% Wilson score interval on accuracy (a point estimate on ~200 snippets is not a fact);
  - per-slice breakdowns by language, company, and tag (negation / direction / mixed), so a
    strong overall number can't hide a weak hard-case slice.
The ``score(scorer, snippets)`` signature is unchanged — the report just carries more.

Kept offline: the scorer defaults to the lexicon engine and never touches the network.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

LABELS = ("positive", "neutral", "negative")
_Z_95 = 1.959963984540054            # standard normal 97.5th percentile


@dataclass
class LabeledSnippet:
    text: str
    label: str                      # gold: positive | neutral | negative
    lang: str | None = None         # es | en (metadata only; engine auto-detects)
    company: str | None = None      # slice key (optional)
    split: str = "dev"              # dev | test — unspecified records default to dev
    tags: list = field(default_factory=list)   # e.g. ["negation", "direction"]


@dataclass
class SnippetResult:
    text: str
    gold: str
    predicted: str
    lang: str | None = None
    company: str | None = None
    tags: list = field(default_factory=list)

    @property
    def correct(self) -> bool:
        return self.gold == self.predicted


@dataclass
class ClassMetrics:
    precision: float
    recall: float
    f1: float
    support: int                    # gold count for this class


@dataclass
class SliceMetrics:
    key: str                        # the slice value (e.g. "es", "walmex", "negation")
    n: int
    accuracy: float
    macro_f1: float
    accuracy_ci: tuple             # (lo, hi) 95% Wilson interval on accuracy


@dataclass
class SentimentReport:
    n: int
    accuracy: float
    accuracy_ci: tuple              # (lo, hi) 95% Wilson interval on accuracy
    macro_f1: float
    per_class: dict                 # label -> ClassMetrics
    confusion: dict                 # gold_label -> {pred_label -> count}
    per_snippet: list               # SnippetResult, misses first
    by_lang: dict = field(default_factory=dict)     # lang -> SliceMetrics
    by_company: dict = field(default_factory=dict)  # company -> SliceMetrics
    by_tag: dict = field(default_factory=dict)      # tag -> SliceMetrics
    split: str = "all"              # which split this report was computed over

    def misses(self) -> list:
        return [r for r in self.per_snippet if not r.correct]


def load_labels(path: "Path | str", split: str | None = None) -> list[LabeledSnippet]:
    """Read the gold set. ``split`` filters records: None/"all" keeps everything; "dev"/"test"
    keeps only matching records (a record with no ``split`` field counts as dev)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[LabeledSnippet] = []
    for item in raw.get("snippets", []):
        text, label = item.get("text"), item.get("label")
        if not text or label not in LABELS:
            continue
        rec_split = (item.get("split") or "dev").lower()
        out.append(LabeledSnippet(
            text=text, label=label, lang=item.get("lang"),
            company=item.get("company"), split=rec_split,
            tags=list(item.get("tags") or []),
        ))
    return select_split(out, split)


def select_split(snippets: list[LabeledSnippet], split: str | None) -> list[LabeledSnippet]:
    if split is None or split == "all":
        return snippets
    return [s for s in snippets if s.split == split]


def _f1(precision: float, recall: float) -> float:
    return 0.0 if (precision + recall) == 0 else 2 * precision * recall / (precision + recall)


def _wilson_ci(k: int, n: int, z: float = _Z_95) -> tuple:
    """95% Wilson score interval for a binomial proportion k/n. Returns (lo, hi) in [0, 1].

    Preferred over the normal approximation for the small samples (~200) and near-1 rates this
    harness sees: it stays inside [0, 1] and doesn't collapse to a zero-width interval at k==n.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


def _macro_f1_and_classes(results: list[SnippetResult]) -> tuple[float, dict, dict]:
    confusion = {g: {p: 0 for p in LABELS} for g in LABELS}
    for r in results:
        confusion[r.gold][r.predicted] += 1
    per_class: dict = {}
    f1s: list[float] = []
    for lbl in LABELS:
        tp = confusion[lbl][lbl]
        fp = sum(confusion[g][lbl] for g in LABELS if g != lbl)
        support = sum(confusion[lbl].values())
        fn = support - tp
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = _f1(precision, recall)
        per_class[lbl] = ClassMetrics(precision, recall, f1, support)
        f1s.append(f1)
    macro_f1 = sum(f1s) / len(f1s) if f1s else 0.0
    return macro_f1, per_class, confusion


def _slice_metrics(key: str, results: list[SnippetResult]) -> SliceMetrics:
    n = len(results)
    correct = sum(1 for r in results if r.correct)
    accuracy = correct / n if n else 0.0
    macro_f1, _, _ = _macro_f1_and_classes(results)
    return SliceMetrics(key=key, n=n, accuracy=round(accuracy, 4),
                        macro_f1=round(macro_f1, 4), accuracy_ci=_wilson_ci(correct, n))


def _breakdown(results: list[SnippetResult], keyfn) -> dict:
    """Group results by keyfn (which may return a scalar or a list of keys) → SliceMetrics."""
    groups: dict = {}
    for r in results:
        keys = keyfn(r)
        if keys is None:
            continue
        if not isinstance(keys, (list, tuple, set)):
            keys = [keys]
        for k in keys:
            groups.setdefault(k, []).append(r)
    return {k: _slice_metrics(str(k), groups[k]) for k in sorted(groups, key=str)}


def score(scorer, snippets: list[LabeledSnippet], split: str = "all") -> SentimentReport:
    """Run ``scorer(text).label`` over the gold set and compute the metrics.

    ``scorer`` is any callable text -> object with a ``.label`` attribute (i.e.
    ``score_sentiment``), so an LLM engine can be swapped in for an apples-to-apples number.
    """
    results = [
        SnippetResult(text=s.text, gold=s.label, predicted=scorer(s.text).label,
                      lang=s.lang, company=s.company, tags=s.tags)
        for s in snippets
    ]
    n = len(results)
    correct = sum(1 for r in results if r.correct)
    macro_f1, per_class, confusion = _macro_f1_and_classes(results)
    accuracy = correct / n if n else 0.0

    return SentimentReport(
        n=n, accuracy=round(accuracy, 4), accuracy_ci=_wilson_ci(correct, n),
        macro_f1=round(macro_f1, 4), per_class=per_class, confusion=confusion,
        per_snippet=sorted(results, key=lambda r: r.correct),   # misses (False) first
        by_lang=_breakdown(results, lambda r: r.lang or "unknown"),
        by_company=_breakdown(results, lambda r: r.company or "unknown"),
        by_tag=_breakdown(results, lambda r: list(r.tags) or None),   # untagged excluded
        split=split,
    )
