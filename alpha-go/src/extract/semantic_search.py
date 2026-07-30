"""Deterministic semantic matching for extraction search.

This module centralizes row-label matching for table and evidence extraction.
It deliberately avoids external embedding providers; the public API is small so
true vector matching can be added behind it later without changing callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import unicodedata

import yaml

from src.model.financial_model import MetricDef
from src.shared.paths import CONFIGS_DIR


DEFAULT_SEARCH_DICTIONARY = CONFIGS_DIR / "metric_search.yaml"
DEFAULT_MATCH_THRESHOLD = 0.85
MAX_MATCH_LABEL_CHARS = 120
MAX_MATCH_LABEL_TOKENS = 16
_PROSE_CONTEXT_RE = re.compile(
    r"\b(?:finalizo|finalizaron|finalizaron?|ascendio|ascendieron|"
    r"fue|fueron|alcanzo|alcanzaron|cerro|cerraron|se ubico|se ubicaron|"
    r"sumo|sumaron|totalizo|totalizaron|termino|terminaron|"
    r"opera|operan|operaba|operaban|aproximadamente)\b"
)
_SUBLINE_CONTEXT_RE = re.compile(
    r"\b(?:u12m|mismos clubes|same clubs|mensual|trimestral|monthly|quarterly)\b"
)
_STOPWORDS = frozenset({"de", "del", "la", "las", "el", "los", "y", "and", "of", "the"})


@dataclass(frozen=True)
class SearchDictionary:
    version: int
    concept_aliases: dict[str, tuple[str, ...]]
    concept_metric_keys: dict[str, tuple[str, ...]]
    negative_labels: tuple[str, ...]


@dataclass(frozen=True)
class MetricProfile:
    key: str
    aliases: tuple[str, ...]
    alias_tokens: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class MatchResult:
    metric_key: str
    score: float
    matched_alias: str
    method: str
    raw_label: str
    is_negative: bool = False

    @property
    def reason(self) -> str:
        alias = self.matched_alias or ""
        if alias:
            return f"semantic_score={self.score:.2f} method={self.method} alias={alias}"
        return f"semantic_score={self.score:.2f} method={self.method}"


_DICTIONARY_CACHE: SearchDictionary | None = None


def normalize_label(text: str) -> str:
    """Normalize labels for accent/case/punctuation-insensitive matching."""
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = "".join(c for c in normalized if not unicodedata.combining(c))
    ascii_text = ascii_text.lower()
    ascii_text = re.sub(r"[_/\\-]+", " ", ascii_text)
    ascii_text = re.sub(r"[^a-z0-9% ]+", " ", ascii_text)
    ascii_text = re.sub(r"\s+", " ", ascii_text).strip()
    return ascii_text


# Generic consolidation cues (language-mixed, BMV filings are ES/EN). Used to
# prefer the consolidated/total row over a same-metric segment row when several
# table rows map to one metric — without per-company `sections` config.
_CONSOLIDATED_CUES = (
    "including eliminations", "incluyendo eliminaciones", "consolidated",
    "consolidado", "consolidada", "grupo", "group",
)
_SEGMENT_CUES = (
    "mexico", "north america", "norteamerica", "eaa", "latam", "latinoamerica",
    "suburbia", "central america", "centroamerica", "u s ", "united states",
    "estados unidos", "commercial", "boutiques", "self service", "autoservicio",
)


def consolidation_rank(raw_label: str) -> int:
    """How 'consolidated' a row label looks: higher beats lower for the same metric.

    +2 explicit consolidation cue ("including eliminations", "consolidated",
    "grupo"); +1 a leading "total"; −1 a generic region/segment cue. This is a
    generic tiebreak (a small built-in token list, not per-company config) so a
    segmented filer's consolidated total wins over its segment rows.
    """
    norm = normalize_label(raw_label)
    if not norm:
        return 0
    rank = 0
    if any(cue in norm for cue in _CONSOLIDATED_CUES):
        rank += 2
    if norm.startswith("total"):
        rank += 1
    if any(cue in norm for cue in _SEGMENT_CUES):
        rank -= 1
    return rank


def load_search_dictionary(path: str | Path | None = None) -> SearchDictionary:
    """Load the deterministic search dictionary, cached for the default path."""
    global _DICTIONARY_CACHE
    dictionary_path = Path(path) if path is not None else DEFAULT_SEARCH_DICTIONARY
    if path is None and _DICTIONARY_CACHE is not None:
        return _DICTIONARY_CACHE

    data = yaml.safe_load(dictionary_path.read_text(encoding="utf-8")) if dictionary_path.exists() else {}
    concepts = (data or {}).get("concepts", {})
    if not isinstance(concepts, dict):
        raise ValueError(f"{dictionary_path}: concepts must be a mapping")

    concept_aliases: dict[str, tuple[str, ...]] = {}
    concept_metric_keys: dict[str, tuple[str, ...]] = {}
    for concept, raw in concepts.items():
        if not isinstance(raw, dict):
            raise ValueError(f"{dictionary_path}: concept {concept!r} must be a mapping")
        aliases = _normalized_unique(raw.get("aliases", []))
        metric_keys = tuple(str(k) for k in raw.get("metric_keys", []))
        concept_aliases[str(concept)] = aliases
        concept_metric_keys[str(concept)] = metric_keys

    dictionary = SearchDictionary(
        version=int((data or {}).get("version", 1)),
        concept_aliases=concept_aliases,
        concept_metric_keys=concept_metric_keys,
        negative_labels=_normalized_unique((data or {}).get("negative_labels", [])),
    )
    if path is None:
        _DICTIONARY_CACHE = dictionary
    return dictionary


def build_metric_profile(
    metric_def: MetricDef,
    dictionary: SearchDictionary | None = None,
) -> MetricProfile:
    """Build the search profile for one metric from labels, aliases, and concepts."""
    dictionary = dictionary or load_search_dictionary()
    raw_aliases = [
        metric_def.key.replace("_", " "),
        metric_def.label,
        metric_def.label_es,
        *metric_def.aliases,
    ]
    for concept, metric_keys in dictionary.concept_metric_keys.items():
        if metric_def.key in metric_keys:
            raw_aliases.extend(dictionary.concept_aliases.get(concept, ()))
    aliases = _normalized_unique(raw_aliases)
    return MetricProfile(
        key=metric_def.key,
        aliases=aliases,
        alias_tokens=tuple((alias, tuple(alias.split())) for alias in aliases),
    )


class SemanticMatcher:
    """Reusable matcher for a fixed metric set.

    Constructing profiles is cheap for one call but expensive across thousands
    of PDF row labels. This class precomputes them once per extraction pass.
    """

    def __init__(
        self,
        metric_defs: list[MetricDef],
        dictionary: SearchDictionary | None = None,
        *,
        skip_keys: frozenset = frozenset(),
    ) -> None:
        self.dictionary = dictionary or load_search_dictionary()
        self.metric_defs = [
            m
            for m in metric_defs
            if m.key not in skip_keys
            and not (m.calc and not m.patterns and not m.aliases)
        ]
        self.profiles = {
            metric_def.key: build_metric_profile(metric_def, self.dictionary)
            for metric_def in self.metric_defs
        }

    def score_metric_label(self, raw_label: str, metric_def: MetricDef) -> MatchResult:
        norm_label = normalize_label(raw_label)
        rejected = _rejected_label_result(norm_label, raw_label, metric_def.key, self.dictionary)
        if rejected:
            return rejected

        profile = self.profiles.get(metric_def.key) or build_metric_profile(metric_def, self.dictionary)
        label_tokens = tuple(norm_label.split())
        return _score_profile(norm_label, label_tokens, profile, raw_label)

    def best_match(
        self,
        raw_label: str,
        *,
        threshold: float = DEFAULT_MATCH_THRESHOLD,
    ) -> MatchResult | None:
        norm_label = normalize_label(raw_label)
        if _label_is_rejected(norm_label, self.dictionary):
            return None

        label_tokens = tuple(norm_label.split())
        best: MatchResult | None = None
        for metric_def in self.metric_defs:
            profile = self.profiles[metric_def.key]
            result = _score_profile(norm_label, label_tokens, profile, raw_label)
            if result.score < threshold:
                continue
            if best is None or (result.score, result.metric_key) > (best.score, best.metric_key):
                best = result
        return best


def _score_profile(
    norm_label: str,
    label_tokens: tuple[str, ...],
    profile: MetricProfile,
    raw_label: str,
) -> MatchResult:
    best_score = 0.0
    best_alias = ""
    best_method = "none"
    for alias, alias_tokens in profile.alias_tokens:
        score, method = _alias_score(norm_label, label_tokens, alias, alias_tokens)
        if score > best_score:
            best_score, best_alias, best_method = score, alias, method

    return MatchResult(
        metric_key=profile.key,
        score=best_score,
        matched_alias=best_alias,
        method=best_method,
        raw_label=raw_label,
    )


def score_metric_label(
    raw_label: str,
    metric_def: MetricDef,
    dictionary: SearchDictionary | None = None,
) -> MatchResult:
    """Score a raw row label against a metric profile."""
    return SemanticMatcher([metric_def], dictionary).score_metric_label(raw_label, metric_def)


def best_metric_match(
    raw_label: str,
    metric_defs: list[MetricDef],
    dictionary: SearchDictionary | None = None,
    *,
    skip_keys: frozenset = frozenset(),
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> MatchResult | None:
    """Return the best metric match for one row label, or None below threshold."""
    matcher = SemanticMatcher(metric_defs, dictionary, skip_keys=skip_keys)
    return matcher.best_match(raw_label, threshold=threshold)


def _alias_score(
    norm_label: str,
    label_tokens: tuple[str, ...],
    norm_alias: str,
    alias_tokens: tuple[str, ...],
) -> tuple[float, str]:
    if not norm_label or not norm_alias:
        return 0.0, "none"
    if norm_label == norm_alias:
        return 1.0, "exact"
    if (
        len(alias_tokens) == 1
        and len(label_tokens) > 1
        and label_tokens[1] in {"por", "from", "de"}
        and _same_simple_word(label_tokens[0], norm_alias)
    ):
        return 0.0, "single_token_subline"
    if norm_label.startswith(norm_alias):
        if (
            len(alias_tokens) == 1
            and len(label_tokens) > 1
            and label_tokens[1] in {"por", "from", "de"}
        ):
            return 0.82, "single_token_subline"
        return 0.98, "prefix"
    if norm_alias in norm_label:
        if len(norm_alias.split()) == 1:
            return 0.80, "single_token_contains"
        return 0.84, "contains"
    if len(alias_tokens) > 1:
        overlap = len(set(label_tokens) & set(alias_tokens))
        coverage = overlap / len(set(alias_tokens))
        if (
            coverage == 1.0
            and len(label_tokens) <= len(alias_tokens) + 2
            and _meaningful_alias_is_contiguous(label_tokens, alias_tokens)
        ):
            return 0.88, "token_overlap"
        if coverage >= 0.75 and len(label_tokens) <= len(alias_tokens) + 2:
            return 0.84, "token_overlap"
    return 0.0, "none"


def _negative_score(norm_label: str, dictionary: SearchDictionary) -> float:
    for negative in dictionary.negative_labels:
        if norm_label == negative or norm_label.startswith(negative):
            return 1.0
    return 0.0


def _looks_like_prose_statement(norm_label: str) -> bool:
    return bool(_PROSE_CONTEXT_RE.search(norm_label))


def _label_is_rejected(norm_label: str, dictionary: SearchDictionary) -> bool:
    if not norm_label:
        return True
    if len(norm_label) > MAX_MATCH_LABEL_CHARS:
        return True
    if len(norm_label.split()) > MAX_MATCH_LABEL_TOKENS:
        return True
    if _negative_score(norm_label, dictionary) >= 1.0:
        return True
    return _looks_like_prose_statement(norm_label) or _looks_like_subline_context(norm_label)


def _rejected_label_result(
    norm_label: str,
    raw_label: str,
    metric_key: str,
    dictionary: SearchDictionary,
) -> MatchResult | None:
    if _negative_score(norm_label, dictionary) >= 1.0:
        return MatchResult(
            metric_key=metric_key,
            score=0.0,
            matched_alias="",
            method="negative",
            raw_label=raw_label,
            is_negative=True,
        )
    if _looks_like_prose_statement(norm_label):
        return MatchResult(
            metric_key=metric_key,
            score=0.0,
            matched_alias="",
            method="prose_context",
            raw_label=raw_label,
        )
    if _looks_like_subline_context(norm_label):
        return MatchResult(
            metric_key=metric_key,
            score=0.0,
            matched_alias="",
            method="subline_context",
            raw_label=raw_label,
        )
    if len(norm_label) > MAX_MATCH_LABEL_CHARS or len(norm_label.split()) > MAX_MATCH_LABEL_TOKENS:
        return MatchResult(
            metric_key=metric_key,
            score=0.0,
            matched_alias="",
            method="long_label",
            raw_label=raw_label,
        )
    return None


def _same_simple_word(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.rstrip("s") == right.rstrip("s")


def _looks_like_subline_context(norm_label: str) -> bool:
    return bool(_SUBLINE_CONTEXT_RE.search(norm_label))


def _meaningful_alias_is_contiguous(
    label_tokens: tuple[str, ...],
    alias_tokens: tuple[str, ...],
) -> bool:
    label_core = [token for token in label_tokens if token not in _STOPWORDS]
    alias_core = [token for token in alias_tokens if token not in _STOPWORDS]
    if not alias_core or len(alias_core) > len(label_core):
        return False
    width = len(alias_core)
    return any(label_core[i:i + width] == alias_core for i in range(len(label_core) - width + 1))


def _normalized_unique(values) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        norm = normalize_label(str(value))
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return tuple(out)
