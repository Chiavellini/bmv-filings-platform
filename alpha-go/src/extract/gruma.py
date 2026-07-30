"""GRUMA-specific extraction from the subsidiary appendix table."""

from __future__ import annotations

import re
import unicodedata

from src.extract.extract_metrics import MetricRow, parse_number
from src.model.financial_model import MetricDef


_SEGMENTS = (
    ("usa", r"\bGRUMA\s+ESTADOS\s+UNIDOS\b|\bGRUMA\s+USA\b"),
    ("gimsa", r"\bGIMSA\b"),
    ("europe", r"\bGRUMA\s+EUROPA\b"),
    ("ao", r"\bGRUMA\s+ASIA\s+Y\s+OCEANIA\b"),
    ("cam", r"\bGRUMA\s+CENTROAMERICA\b"),
    ("other", r"\bOTRAS\s+SUBSIDIARIAS\b.*\bELIMINACIONES\b|\bOTRAS\s+SUBSIDIARIAS\b"),
    ("consolidated", r"\bCONSOLIDADO\b"),
)

_METRIC_ROWS = (
    ("volume", r"\bVOLUMEN\s+DE\s+VENTAS\b"),
    ("net_sales", r"\bVENTAS\s+NETAS\b"),
    ("gross_profit", r"\bUTILIDAD\s+BRUTA\b"),
    ("operating_income", r"\bUTILIDAD\s+DE\s+OPERACION\b"),
    ("ebitda", r"\b(?:UAFIRDA|UAFIDA|EBITDA)\b"),
)

_SEGMENT_SUFFIX = {
    "usa": "usa",
    "gimsa": "gimsa",
    "europe": "europe",
    "ao": "ao",
    "cam": "cam",
    "other": "other",
}

_CONSOLIDATED_KEY = {
    "volume": "volume",
    "net_sales": "revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "ebitda": "ebitda",
}

_NUM_RE = re.compile(r"\(?-?\$?\s*[\d,]+(?:\.\d+)?%?\)?")
_NUMERIC_TAIL_RE = re.compile(r"^(?:USD\s+MILLONES\s+)?[\(\-$]?\s*\d")


def extract_gruma_appendix(
    text: str,
    metric_defs: list[MetricDef],
) -> dict[str, MetricRow]:
    """Extract GRUMA consolidated and segment rows from layout-preserved text."""
    if "GRUMA" not in text.upper() or "VENTAS NETAS" not in _norm(text):
        return {}

    defs = {m.key: m for m in metric_defs}
    allowed = set(defs)
    found: dict[str, MetricRow] = {}
    current_segment: str | None = None

    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        norm_line = _norm(line)
        segment, stripped_norm = _detect_segment(norm_line)
        if segment:
            current_segment = segment
            norm_for_metric = stripped_norm or norm_line
        else:
            norm_for_metric = norm_line

        row_metric = _detect_metric(norm_for_metric)
        if not row_metric or not current_segment:
            continue

        key = _metric_key(current_segment, row_metric)
        if not key or key not in allowed:
            continue

        values = [_parse_token(token) for token in _NUM_RE.findall(line)]
        values = [v for v in values if v is not None]
        if row_metric == "volume":
            if len(values) < 3:
                continue
            current, prior = values[0], values[1]
            var_pct = values[-1] if len(values) >= 3 else None
        else:
            if len(values) < 3:
                continue
            if _has_percentage_columns(values):
                current, prior = values[0], values[2]
                var_pct = values[-1] if len(values) >= 5 else None
            else:
                current, prior = values[0], values[1]
                var_pct = values[2] if len(values) >= 3 else None

        mdef = defs[key]
        found[key] = MetricRow(
            metric=key,
            label_es=mdef.label_es,
            current=current,
            prior=prior,
            var_pct=var_pct,
            unit=mdef.unit,
            source_line=f"[gruma_table] {line[:100]}",
        )

    return found


def _metric_key(segment: str, metric: str) -> str | None:
    if segment == "consolidated":
        return _CONSOLIDATED_KEY.get(metric)
    suffix = _SEGMENT_SUFFIX.get(segment)
    if not suffix:
        return None
    return f"{metric}_{suffix}"


def _detect_segment(norm_line: str) -> tuple[str | None, str]:
    for segment, pattern in _SEGMENTS:
        match = re.search(pattern, norm_line)
        if not match:
            continue
        if match.start() > 35:
            continue
        return segment, norm_line[match.end():].strip()
    return None, norm_line


def _detect_metric(norm_line: str) -> str | None:
    for metric, pattern in _METRIC_ROWS:
        match = re.search(pattern, norm_line)
        if match and _NUMERIC_TAIL_RE.match(norm_line[match.end():].lstrip()):
            return metric
    return None


def _parse_token(token: str) -> float | None:
    cleaned = token.replace("$", "").strip()
    if not re.search(r"\d", cleaned):
        return None
    return parse_number(cleaned)


def _has_percentage_columns(values: list[float]) -> bool:
    """True for rows shaped current, current %, prior, prior %, var abs, var %."""
    return len(values) >= 4 and abs(values[1]) <= 150 and abs(values[3]) <= 150


def _norm(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return ascii_text.upper()
