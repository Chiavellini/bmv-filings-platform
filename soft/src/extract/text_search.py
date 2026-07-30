"""Command-F style text extraction.

This tier is deliberately simple and auditable: find a metric alias in the
parsed report text, read the numeric cells on that same logical line, and use
nearby header/context text to choose the quarterly value when the row carries
both accumulated and quarter columns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.extract.extract_metrics import MetricRow, parse_number
from src.extract.semantic_search import (
    SemanticMatcher,
    build_metric_profile,
    load_search_dictionary,
    normalize_label,
)
from src.extract.table_periods import normalize_target_period, parse_header_token, select_value_for_period
from src.model.financial_model import MetricDef


_MONETARY_UNITS = {"currency", "miles_mxn"}
_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\(?-?\$?\s*\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?%?\)?"
    r"|\(?-?\$?\s*\d+(?:\.\d+)?%?\)?"
    r")(?![A-Za-z0-9])"
)
_CURRENCY_TAIL_RE = re.compile(
    r"(?:\b(?:ps|mxn|p\$|mps|mp|millones|millions|million|miles|thousands?)\.?|\$)\s*$",
    re.IGNORECASE,
)
_HEADER_SPLIT_RE = re.compile(r"\s{2,}|\s+\|\s+|\t+")
_ACCUM_AND_QUARTER_RE = re.compile(
    r"(?:acum|acumulado|years?\s+ended|for\s+the\s+years?|a[nñ]o)"
    r"[\s\S]{0,240}?"
    r"(?:trim|trimestre|quarter|three\s+months?|three\s+month\s+period|tres\s+meses)"
    r"|(?:trim|trimestre|quarter|three\s+months?|three\s+month\s+period|tres\s+meses)"
    r"[\s\S]{0,240}?"
    r"(?:acum|acumulado|years?\s+ended|for\s+the\s+years?|a[nñ]o)",
    re.IGNORECASE,
)
# Full-year / cumulative (YTD) markers — used to demote annual figures so the
# QUARTERLY figure wins when a Q-report states both (e.g. a Q4 release that gives
# both "fourth quarter ... P$13,164M" and "full year 2023 ... P$44,355M").
_FULL_YEAR_RE = re.compile(
    r"\b(?:full[\s-]*year|fiscal\s+year|annual|anual|"
    r"(?:twelve|nine|six)\s+months?|year[\s-]*to[\s-]*date|"
    r"a[nñ]o\s+(?:completo|fiscal)|del\s+a[nñ]o|acumulad[oa]|"
    r"primer\s+semestre|(?:doce|nueve|seis)\s+meses)\b"
    r"|\b(?:1[02]|9|6)M\d{2}\b|\bFY\d{2,4}\b",
    re.IGNORECASE,
)
# Volume statements (unit cases / MUC / hectolitres) must never satisfy a
# currency metric like revenue — "Total Sales Volume grew 3% to 556.8 MUC"
# loosely matches the "sales" alias but is not money.
_VOLUME_UNIT_RE = re.compile(
    r"\b(?:MUC|UC|unit\s+cases?|cajas\s+unidad|"
    r"(?:nine|9)[\s-]*l(?:iter|itre)?s?\s+cases?|9L\s+cases?|"
    r"hectol|hectolitre?s?|million\s+cases?)\b",
    re.IGNORECASE,
)
_CURRENCY_SYMBOL_RE = re.compile(r"(?:\$|Ps\.?|P\$|MXN|US\$|USD|EUR|€)", re.IGNORECASE)
# Quarter anchors on the data line itself (strongest quarterly signal).
_QUARTER_ANCHOR_RE = re.compile(
    r"\b(?:first|second|third|fourth)\s+quarter\b"
    r"|\b(?:primer|segundo|tercer|cuarto)\s+trimestre\b"
    r"|\b[1-4]\s*[QT]\s*['’]?\d{2}\b"
    r"|\b(?:three\s+months?|tres\s+meses)\b",
    re.IGNORECASE,
)
_MILLION_HINT_RE = re.compile(r"\b(?:millones|million|millions|p\$mn|ps\.?\s*mn)\b", re.IGNORECASE)
_THOUSAND_HINT_RE = re.compile(r"\b(?:miles|thousand|thousands)\b", re.IGNORECASE)
# English prose states quarterly figures as "$5.421 billion pesos"; Spanish
# equivalents are "mil/miles de millones". (Spanish "billón" = 10^12, excluded.)
_BILLION_HINT_RE = re.compile(r"\b(?:billion|billions|mil(?:es)?\s+de\s+millones|mil\s+millones)\b", re.IGNORECASE)
_DEFINITION_RE = re.compile(
    r"\b(?:we\s+measure|when\s+calculating|under\s+consideration|operational\s+for\s+at\s+least|definition)\b",
    re.IGNORECASE,
)
_BOILERPLATE_RE = re.compile(
    r"\b(?:will\s+host\s+a\s+call|conference\s+call|webcast|dial[-\s]?in|investor\s+relations\s+contact)\b",
    re.IGNORECASE,
)
_MAX_LOGICAL_LINE_CHARS = 2400


@dataclass(frozen=True)
class _NumberToken:
    raw: str
    value: float
    start: int
    is_percent: bool


def extract_from_text_search(
    text: str,
    metric_defs: list[MetricDef],
    cfg: dict | None = None,
    *,
    period: str | None = None,
) -> dict[str, MetricRow]:
    """Return metric rows found by alias search over parsed report text.

    Config hook, all optional:

      text_search:
        currency_scale: 1000
        scale_only_below: 1000000
        skip_metrics: [revenue_otc]   # keys this tier must never answer
        metrics:
          revenue: {currency_scale: 1000, scale_only_below: 1000000}

    The hook is intentionally generic and documented in YAML where used. Without
    it, the tier falls back to unit hints and magnitude-based scaling.
    """
    if not text:
        return {}
    cfg = cfg or {}
    search_cfg = cfg.get("text_search") or cfg.get("command_f") or {}
    if isinstance(search_cfg, dict) and search_cfg.get("disabled"):
        return {}
    max_line_chars = (
        int(search_cfg.get("max_line_chars", _MAX_LOGICAL_LINE_CHARS))
        if isinstance(search_cfg, dict)
        else _MAX_LOGICAL_LINE_CHARS
    )
    lines = [line.strip() for line in text.splitlines()]
    dictionary = load_search_dictionary()
    matcher = SemanticMatcher(metric_defs)
    defs_by_key = {m.key: m for m in metric_defs}
    alias_index = _build_alias_index(metric_defs, dictionary)
    found: dict[str, MetricRow] = {}
    chosen_rank: dict[str, tuple[int, float]] = {}
    match_cache: dict[tuple[str, str], tuple[str, float] | None] = {}

    for idx, line in enumerate(lines):
        if not line or _is_separator(line):
            continue
        context_lines = _context_lines(lines, idx)
        for logical in _logical_lines(lines, idx):
            if len(logical) > max_line_chars:
                continue
            if _DEFINITION_RE.search(logical) or _BOILERPLATE_RE.search(logical):
                continue
            if _skip_annual_row(logical, context_lines, search_cfg):
                continue
            tokens = _number_tokens(logical)
            if not tokens:
                continue
            label = _label_before_first_number(logical, tokens[0].start)
            if not label:
                continue

            fallback_score = 0.0
            cache_key = (label, normalize_label(logical[:160]))
            if cache_key in match_cache:
                cached = match_cache[cache_key]
                if cached is None:
                    continue
                key, fallback_score = cached
            else:
                match = matcher.best_match(label, threshold=0.82)
                if match is None:
                    fallback = _alias_contains_match(label, logical, alias_index, dictionary.negative_labels)
                    if fallback is None:
                        match_cache[cache_key] = None
                        continue
                    key, fallback_score = fallback
                else:
                    key, fallback_score = match.metric_key, match.score
                match_cache[cache_key] = (key, fallback_score)

            mdef = defs_by_key.get(key)
            if mdef is None or (mdef.calc and not mdef.patterns and not mdef.aliases):
                continue
            # Metrics a company reports only in dedicated tables/extractors:
            # alias hits in prose (TOC lines, facility blurbs) are noise there.
            if isinstance(search_cfg, dict) and key in set(search_cfg.get("skip_metrics") or []):
                continue

            # A currency metric must not be filled from a pure volume statement
            # (e.g. "Total Sales Volume grew 3% in 4Q23 to 556.8 MUC").
            if (mdef.unit == "currency"
                    and _VOLUME_UNIT_RE.search(logical)
                    and not _CURRENCY_SYMBOL_RE.search(logical)):
                continue

            selected = _select_values(mdef, tokens, context_lines, period)
            if selected is None:
                continue
            current, prior = selected
            current = _scale_value(current, mdef, logical, context_lines, cfg)
            prior = _scale_value(prior, mdef, logical, context_lines, cfg) if prior is not None else None

            rank = _row_rank(label, logical, context_lines)
            score = fallback_score
            existing_rank = chosen_rank.get(key)
            if key in found and existing_rank is not None and existing_rank >= (rank, score):
                continue
            found[key] = MetricRow(
                metric=key,
                label_es=mdef.label_es,
                current=current,
                prior=prior,
                var_pct=None,
                unit=mdef.unit,
                source_line=f"[search] {logical.strip()}"[:120],
            )
            chosen_rank[key] = (rank, score)
    return found


def _is_separator(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and set(stripped) <= {"-", "|", ":", " "}


def _context_lines(lines: list[str], idx: int) -> list[str]:
    start = max(0, idx - 8)
    return [line for line in lines[start:idx] if line]


def _logical_lines(lines: list[str], idx: int) -> list[str]:
    """Same-line first, then one-line continuation for wrapped numeric rows."""
    line = lines[idx].strip()
    yield line
    if idx + 1 < len(lines):
        nxt = lines[idx + 1].strip()
        if nxt and not _is_separator(nxt):
            # Common PDF markdown artifact: row label on one line, values on the next.
            if not any(ch.isdigit() for ch in line):
                yield f"{line} {nxt}"


def _skip_annual_row(logical: str, context_lines: list[str], search_cfg: dict) -> bool:
    if not isinstance(search_cfg, dict) or not search_cfg.get("skip_annual_rows"):
        return False
    context = "\n".join(context_lines[-5:])
    combined = f"{context}\n{logical}"
    acc_hits = list(re.finditer(r"\bacumulad", context, re.IGNORECASE))
    if acc_hits:
        tri_hits = list(re.finditer(
            r"\b(?:trim|trimestre|quarter|three\s+months?|tres\s+meses)\b",
            context,
            re.IGNORECASE,
        ))
        last_tri = tri_hits[-1].start() if tri_hits else -1
        if acc_hits[-1].start() > last_tri:
            return True
    if re.search(r"resultados\s+acumulad", combined, re.IGNORECASE) and re.search(
        r"\b(?:19|20)\d{2}\s+(?:19|20)\d{2}\b", combined
    ):
        return True
    if re.match(r"\s*(?:19|20)\d{2}\s+(?:19|20)\d{2}\b", logical):
        return True
    if re.search(r"\b(?:[1-4]\s*[QT]|quarter|trimestre)\b", context, re.IGNORECASE):
        return False
    year_hits = re.findall(r"\b(?:19|20)\d{2}\b", context)
    if len(year_hits) >= 2:
        return True
    # Spanish/English releases sometimes write compact headers as "2018 2017"
    # and the logical data row itself has no period marker.
    return bool(re.search(r"\b(?:19|20)\d{2}\s+(?:19|20)\d{2}\b", combined))


def _number_tokens(line: str) -> list[_NumberToken]:
    out: list[_NumberToken] = []
    for m in _NUM_RE.finditer(line):
        raw = m.group(0).strip()
        value = parse_number(raw)
        if value is None:
            continue
        if _looks_like_year(raw, value):
            continue
        is_percent = "%" in raw or (m.end() < len(line) and line[m.end()] == "%")
        out.append(_NumberToken(raw=raw, value=value, start=m.start(), is_percent=is_percent))
    return out


def _looks_like_year(raw: str, value: float) -> bool:
    cleaned = raw.strip().strip("()").replace("$", "").replace(" ", "")
    if not cleaned.isdigit() or "," in raw or "." in raw or "%" in raw:
        return False
    return 1900 <= int(value) <= 2099


def _label_before_first_number(line: str, first_number_start: int) -> str:
    label = line[:first_number_start].strip(" \t:-|")
    while True:
        new = _CURRENCY_TAIL_RE.sub("", label).strip(" \t:-|")
        if new == label:
            break
        label = new
    return label


def _alias_contains_match(
    label: str,
    line: str,
    alias_index: list[tuple[str, str]],
    negative_labels: set[str],
) -> tuple[str, float] | None:
    """Fallback for long/dirty labels that SemanticMatcher rejects.

    Chooses the longest exact alias present before/near the value. Generic
    one-token aliases are intentionally weak so specific custom aliases win.
    """
    norm_label = normalize_label(label)
    norm_line = normalize_label(line)
    if not norm_label or any(norm_label.startswith(neg) for neg in negative_labels):
        return None

    best: tuple[str, float, int, int] | None = None
    for key, alias in alias_index:
        if len(alias) < 3:
            continue
        if alias in norm_label or alias in norm_line[:160]:
            token_count = len(alias.split())
            if token_count == 1:
                continue
            score = 0.86 if token_count > 1 else 0.80
            rank = (token_count, len(alias))
            if best is None or (score, rank[0], rank[1], key) > (best[1], best[2], best[3], best[0]):
                best = (key, score, rank[0], rank[1])
    return (best[0], best[1]) if best else None


def _build_alias_index(metric_defs: list[MetricDef], dictionary) -> list[tuple[str, str]]:
    """Precompute fallback aliases once per extraction call."""
    out: list[tuple[str, str]] = []
    for mdef in metric_defs:
        profile = build_metric_profile(mdef, dictionary)
        for alias in profile.aliases:
            out.append((mdef.key, alias))
    return out


def _select_values(
    mdef: MetricDef,
    tokens: list[_NumberToken],
    context_lines: list[str],
    period: str | None,
) -> tuple[float, float | None] | None:
    candidates = _tokens_for_unit(mdef, tokens)
    if not candidates:
        return None

    # Four-column BMV/regulatory rows commonly print accumulated current/prior
    # first and quarterly current/prior next. Command-F users visually skip the
    # accumulated pair; this heuristic does the same only when nearby headers say
    # both accumulated/year and quarter are present.
    context = "\n".join(context_lines[-6:])
    if len(candidates) >= 4 and _ACCUM_AND_QUARTER_RE.search(context):
        return candidates[2].value, candidates[3].value

    target = normalize_target_period(period)
    headers = _header_tokens(context_lines)
    if target is not None and headers and len(headers) == len(candidates):
        cells = [(i, t.raw) for i, t in enumerate(candidates)]
        cur, prior = select_value_for_period({i: h for i, h in enumerate(headers)}, cells, target)
        if cur is not None:
            current = parse_number(cur)
            prior_val = parse_number(prior) if prior is not None else None
            if current is not None:
                return current, prior_val

    return candidates[0].value, candidates[1].value if len(candidates) > 1 else None


def _tokens_for_unit(mdef: MetricDef, tokens: list[_NumberToken]) -> list[_NumberToken]:
    if mdef.unit == "pct":
        pct = [t for t in tokens if t.is_percent and abs(t.value) <= 1000]
        if pct:
            return pct
        return [t for t in tokens if not t.is_percent and abs(t.value) <= 100]

    non_pct = [t for t in tokens if not t.is_percent]
    if mdef.unit in _MONETARY_UNITS:
        if any(abs(t.value) >= 1000 for t in non_pct):
            large = [t for t in non_pct if abs(t.value) >= 1000]
            if large:
                return large
        return non_pct

    if mdef.unit in {"count", "area"}:
        return non_pct
    if mdef.unit == "ratio":
        return [t for t in non_pct if abs(t.value) <= 1000]
    return non_pct


def _header_tokens(context_lines: list[str]) -> list[str]:
    for line in reversed(context_lines[-8:]):
        parts = [p.strip(" |") for p in _HEADER_SPLIT_RE.split(line) if p.strip(" |")]
        parsed = [p for p in parts if parse_header_token(p) is not None]
        if len(parsed) >= 2:
            return parsed
    return []


def _scale_value(
    value: float,
    mdef: MetricDef,
    line: str,
    context_lines: list[str],
    cfg: dict,
) -> float:
    if mdef.unit not in _MONETARY_UNITS:
        return value

    configured = _configured_scale(mdef.key, value, cfg)
    if configured is not None:
        return value * configured
    search_cfg = cfg.get("text_search") or cfg.get("command_f") or {}
    if isinstance(search_cfg, dict) and search_cfg.get("disable_auto_scale"):
        return value

    context = f"{' '.join(context_lines[-4:])} {line}"
    company_unit = str((cfg.get("company") or {}).get("unit", "")).lower()
    abs_value = abs(value)
    if "miles" in company_unit or "thousand" in company_unit:
        if _MILLION_HINT_RE.search(context) and abs_value < 1_000_000:
            return value * 1000.0
        if abs_value >= 1_000_000:
            return value * 0.001
        return value
    if "million" in company_unit or "millones" in company_unit:
        if _THOUSAND_HINT_RE.search(context) and abs_value >= 1000:
            return value * 0.001
        if _BILLION_HINT_RE.search(context) and abs_value < 1000:
            return value * 1000.0
        if abs_value >= 1_000_000:
            return value * 0.000001
        return value
    return value


def _configured_scale(key: str, value: float, cfg: dict) -> float | None:
    spec = cfg.get("text_search") or cfg.get("command_f") or {}
    if isinstance(spec, (int, float)):
        return float(spec)
    if not isinstance(spec, dict):
        return None

    metric_spec = (spec.get("metrics") or {}).get(key) or spec.get(key) or spec
    if isinstance(metric_spec, (int, float)):
        return float(metric_spec)
    if not isinstance(metric_spec, dict):
        return None
    scale = metric_spec.get("currency_scale", metric_spec.get("scale"))
    if scale is None:
        return None
    abs_value = abs(value)
    min_abs = metric_spec.get("scale_only_above")
    max_abs = metric_spec.get("scale_only_below")
    if min_abs is not None and abs_value <= float(min_abs):
        return None
    if max_abs is not None and abs_value >= float(max_abs):
        return None
    return float(scale)


def _row_rank(label: str, logical: str, context_lines: list[str]) -> int:
    norm = normalize_label(f"{' '.join(context_lines[-2:])} {label}")
    rank = 0
    if any(token in norm for token in ("consolidated", "consolidado", "consolidada", "total")):
        rank += 1
    if _ACCUM_AND_QUARTER_RE.search(" ".join(context_lines[-6:])):
        rank += 1
    # Quarterly-vs-annual disambiguation. When a Q-report states both the quarter
    # and the full-year/cumulative figure for a metric, prefer the quarterly one so
    # the annual row cannot overwrite it (or vice-versa) on equal rank. Judge the
    # data line itself for a quarter anchor (strongest), and the line plus its
    # immediate context for full-year/YTD markers.
    # Demote full-year / cumulative rows ONLY (do not reward quarter anchors —
    # that wrongly promotes quarter-stamped non-revenue lines like
    # "Total Sales Volume grew 3% in 4Q23 to 556.8 MUC"). A quarter anchor on the
    # data line itself protects it from being mistaken for an annual row.
    ctx = " ".join(context_lines[-4:])
    quarter_anchored = bool(_QUARTER_ANCHOR_RE.search(logical))
    full_year = bool(_FULL_YEAR_RE.search(logical) or _FULL_YEAR_RE.search(ctx))
    if full_year and not quarter_anchored:
        rank -= 2
    return rank
