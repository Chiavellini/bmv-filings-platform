"""Deterministic extractors for Genomma Lab reports."""

from __future__ import annotations

import re
import unicodedata

from src.extract.extract_metrics import MetricRow, parse_number
from src.model.financial_model import MetricDef


_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\(?-?\$?\s*\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?%?\)?"
    r"|\(?-?\$?\s*\d+(?:\.\d+)?%?\)?"
    r")(?![A-Za-z0-9])"
)


def extract_lab_release(
    text: str,
    metric_defs: list[MetricDef],
    period: str | None = None,
) -> dict[str, MetricRow]:
    defs = {m.key: m for m in metric_defs}
    out: dict[str, MetricRow] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    revenue = _extract_revenue(lines, period)
    if revenue is not None and "revenue" in defs:
        out["revenue"] = _row(defs["revenue"], revenue, "[search] LAB Ventas Netas")

    ebitda = _extract_ebitda(lines, period)
    if ebitda is not None and "ebitda" in defs:
        out["ebitda"] = _row(defs["ebitda"], ebitda, "[search] LAB EBITDA Ajustado")

    product = _extract_product_revenue(lines, period)
    if product.get("revenue_otc") is not None and "revenue_otc" in defs:
        out["revenue_otc"] = _row(
            defs["revenue_otc"], product["revenue_otc"], "[search] LAB OTC + Formula Infantil"
        )
    if product.get("revenue_personal_care") is not None and "revenue_personal_care" in defs:
        out["revenue_personal_care"] = _row(
            defs["revenue_personal_care"], product["revenue_personal_care"], "[search] LAB Cuidado Personal + Bebidas"
        )
    return out


def _row(mdef: MetricDef, value: float, source_line: str) -> MetricRow:
    return MetricRow(
        metric=mdef.key,
        label_es=mdef.label_es,
        current=round(value, 4),
        prior=None,
        var_pct=None,
        unit=mdef.unit,
        source_line=source_line,
    )


def _extract_revenue(lines: list[str], period: str | None) -> float | None:
    quarter_re = _quarter_phrase_re(period)
    if quarter_re is not None:
        for line in lines:
            if quarter_re.search(line):
                m = re.search(
                    r"Ventas\s+Netas[^.\n]{0,180}?(?:alcanzaron?|alcanz[oó])\s+Ps\.?\s*([\d,]+\.?\d*)\s+millones",
                    line,
                    re.IGNORECASE,
                )
                if m:
                    return parse_number(m.group(1))
    for line in lines:
        m = re.search(
            r"Ventas\s+Netas[^.\n]{0,160}?(?:alcanzaron?|alcanz[oó])\s+Ps\.?\s*([\d,]+\.?\d*)\s+millones",
            line,
            re.IGNORECASE,
        )
        if m:
            return parse_number(m.group(1))
    for line in lines[:120]:
        norm = _norm(line)
        if norm.startswith("ventas netas"):
            nums = _non_percent_numbers(line)
            if nums:
                return nums[0]
        if norm.startswith("total venta neta"):
            nums = _non_percent_numbers(line)
            if nums:
                return nums[0]
        m = re.search(r"Ventas\s+Netas\s+([\d,]+\.?\d*)\s+100", line, re.IGNORECASE)
        if m:
            value = parse_number(m.group(1))
            if value is not None:
                return value
    return None


def _extract_ebitda(lines: list[str], period: str | None) -> float | None:
    quarter_re = _quarter_phrase_re(period)
    if quarter_re is not None:
        for line in lines:
            if quarter_re.search(line):
                m = re.search(
                    r"EBITDA\s+Ajustado[^.\n]{0,180}?(?:alcanz[oó]|increment[oó]|fue|cerr[oó])\s+Ps\.?\s*([\d,]+\.?\d*)\s+millones",
                    line,
                    re.IGNORECASE,
                )
                if m:
                    return parse_number(m.group(1))
    for line in lines:
        m = re.search(
            r"EBITDA\s+Ajustado[^.\n]{0,160}?(?:alcanz[oó]|increment[oó]|fue|cerr[oó])\s+Ps\.?\s*([\d,]+\.?\d*)\s+millones",
            line,
            re.IGNORECASE,
        )
        if m:
            return parse_number(m.group(1))
    row_candidates: list[tuple[int, float]] = []
    quarter_re = _quarter_phrase_re(period)
    for idx, line in enumerate(lines[:260]):
        norm = _norm(line)
        context = " ".join(lines[max(0, idx - 5):idx + 1])
        if norm.startswith("ebitda") or norm.startswith("total ebitda"):
            if "ventas netas" in norm or re.search(r"\b12m\b", norm):
                continue
            oriented = _current_from_value_margin_row(line)
            if oriented is not None:
                score = 2 if quarter_re is not None and quarter_re.search(context) else 0
                if re.search(r"\b12m\b|a[nñ]o\s+completo", _norm(context)):
                    score -= 1
                row_candidates.append((score, oriented))
                continue
            nums = _non_percent_numbers(line)
            if nums:
                row_candidates.append((0, nums[0]))
    if row_candidates:
        return max(enumerate(row_candidates), key=lambda item: (item[1][0], -item[0]))[1][1]
    return None


def _extract_product_revenue(lines: list[str], period: str | None) -> dict[str, float | None]:
    separate = _extract_recent_product_rows(lines)
    if separate:
        otc = _sum_present(separate.get("otc"), separate.get("infant"))
        pc = _sum_present(separate.get("personal_care"), separate.get("beverage"))
        return {"revenue_otc": otc, "revenue_personal_care": pc}

    old = _extract_legacy_product_total(lines, period)
    return {
        "revenue_otc": old[0] if old else None,
        "revenue_personal_care": old[1] if old else None,
    }


def _extract_recent_product_rows(lines: list[str]) -> dict[str, float]:
    values: dict[str, float] = {}
    for idx, line in enumerate(lines[:220]):
        norm = _norm(line)
        nums = _non_percent_numbers(line)
        if not nums and idx + 1 < len(lines):
            if re.search(r"\d", norm):
                continue
            nums = _non_percent_numbers(f"{line} {lines[idx + 1]}")
        if not nums:
            continue
        if re.match(r"^otc\b", norm):
            values["otc"] = nums[0]
        elif re.match(r"^(?:medicina\s+venta\s+libre|medicamentos?\s+de\s+libre\s+venta)\b", norm):
            values["otc"] = _current_total_from_business_unit_row(nums)
        elif re.match(r"^(?:formula|formula)\s+infantil\b", norm):
            values["infant"] = _current_total_from_business_unit_row(nums)
        elif re.match(r"^cuidado\s+personal\b", norm):
            values["personal_care"] = _current_total_from_business_unit_row(nums)
        elif re.match(r"^(?:bebidas|beverage)\b", norm):
            values["beverage"] = _current_total_from_business_unit_row(nums)
    return values if {"otc", "personal_care"} & set(values) else {}


def _extract_legacy_product_total(lines: list[str], period: str | None) -> tuple[float, float] | None:
    candidates: list[tuple[int, float, float]] = []
    quarter_re = _quarter_phrase_re(period)
    for idx, line in enumerate(lines):
        norm = _norm(line)
        if not (norm.startswith("total ") or " total " in f" {norm} "):
            continue
        context = _norm(" ".join(lines[max(0, idx - 12):idx]))
        if not (
            "libre venta" in context
            or "otc" in context and ("cuidado personal" in context or "pc" in context)
            or "farma" in context and " pc " in f" {context} "
        ):
            continue
        grouped = _legacy_value_groups(line)
        if grouped is not None:
            score = 0
            if quarter_re is not None and quarter_re.search(context):
                score += 3
            if re.search(r"\b(?:12m|12M|a[nñ]o|year|2020\s+2021|2021\s+2022|2022\s+2023)\b", context):
                score -= 1
            candidates.append((score, grouped[0], grouped[1]))
            continue
        nums = _non_percent_numbers(line)
        if len(nums) < 6:
            continue
        # 2016-2017 layout: OTC, PC, Total, prior OTC, prior PC, prior Total.
        if abs((nums[0] + nums[1]) - nums[2]) <= 2.0:
            candidates.append((0, nums[0], nums[1]))
            continue
        # 2018-2023 layout: OTC current/prior, PC current/prior, Total current/prior.
        if len(nums) >= 6 and abs((nums[0] + nums[2]) - nums[4]) <= 2.0:
            candidates.append((0, nums[0], nums[2]))
    if not candidates:
        return None
    best = max(enumerate(candidates), key=lambda item: (item[1][0], -item[0]))[1]
    return best[1], best[2]


def _legacy_value_groups(line: str) -> tuple[float, float] | None:
    tokens = _number_tokens(line)
    if len(tokens) < 9:
        return None
    triples = []
    i = 0
    while i + 2 < len(tokens) and len(triples) < 3:
        first, second, pct = tokens[i], tokens[i + 1], tokens[i + 2]
        if first[1] or second[1] or not pct[1]:
            return None
        triples.append((first[0], second[0], pct[0]))
        i += 3
    if len(triples) < 3:
        return None

    total_first, total_second, total_growth = triples[2]
    err_first_current = abs(((total_first / total_second) - 1.0) * 100.0 - total_growth) if total_second else 999
    err_second_current = abs(((total_second / total_first) - 1.0) * 100.0 - total_growth) if total_first else 999
    current_idx = 0 if err_first_current <= err_second_current else 1
    return triples[0][current_idx], triples[1][current_idx]


def _current_from_value_margin_row(line: str) -> float | None:
    tokens = _number_tokens(line)
    if len(tokens) < 5:
        return None
    values = [v for v, is_pct in tokens if not is_pct]
    pcts = [v for v, is_pct in tokens if is_pct]
    if len(values) < 2 or not pcts:
        return None
    first, second, growth = values[0], values[1], pcts[-1]
    err_first_current = abs(((first / second) - 1.0) * 100.0 - growth) if second else 999
    err_second_current = abs(((second / first) - 1.0) * 100.0 - growth) if first else 999
    return first if err_first_current <= err_second_current else second


def _current_total_from_business_unit_row(nums: list[float]) -> float:
    return nums[-2] if len(nums) >= 4 else nums[0]


def _number_tokens(line: str) -> list[tuple[float, bool]]:
    tokens: list[tuple[float, bool]] = []
    for m in _NUM_RE.finditer(line):
        raw = m.group(0)
        value = parse_number(raw)
        if value is None or _looks_like_year(raw, value):
            continue
        is_percent = "%" in raw or (m.end() < len(line) and line[m.end()] == "%")
        tokens.append((value, is_percent))
    return tokens


def _non_percent_numbers(line: str) -> list[float]:
    vals: list[float] = []
    for m in _NUM_RE.finditer(line):
        raw = m.group(0)
        if "%" in raw or (m.end() < len(line) and line[m.end()] == "%"):
            continue
        value = parse_number(raw)
        if value is not None and not _looks_like_year(raw, value):
            vals.append(value)
    return vals


def _sum_present(*values: float | None) -> float | None:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present)


def _norm(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = "".join(c for c in normalized if not unicodedata.combining(c))
    ascii_text = ascii_text.lower()
    ascii_text = re.sub(r"[^a-z0-9% ]+", " ", ascii_text)
    return re.sub(r"\s+", " ", ascii_text).strip()


def _looks_like_year(raw: str, value: float) -> bool:
    cleaned = raw.strip().strip("()").replace("$", "").replace(" ", "")
    if not cleaned.isdigit() or "," in raw or "." in raw or "%" in raw:
        return False
    return 1900 <= int(value) <= 2099


def _quarter_phrase_re(period: str | None) -> re.Pattern | None:
    if not period:
        return None
    m = re.search(r"([1-4])[QT](\d{2})", period, re.IGNORECASE)
    if not m:
        return None
    q, yy = m.group(1), m.group(2)
    names = {
        "1": r"primer\s+trimestre|1T[-\s]?\d{2,4}",
        "2": r"segundo\s+trimestre|2T[-\s]?\d{2,4}",
        "3": r"tercer\s+trimestre|3T[-\s]?\d{2,4}",
        "4": r"cuarto\s+trimestre|4T[-\s]?\d{2,4}",
    }
    return re.compile(rf"(?:{names[q]}|{q}Q{yy})", re.IGNORECASE)
