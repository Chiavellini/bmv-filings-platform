"""Deterministic extractors for Liverpool KPI rows."""

from __future__ import annotations

import re

from src.extract.extract_metrics import MetricRow, parse_number
from src.extract.table_periods import normalize_target_period, parse_header_token
from src.model.financial_model import MetricDef


def extract_liverpool_release(
    text: str,
    metric_defs: list[MetricDef],
    period: str | None = None,
) -> dict[str, MetricRow]:
    defs = {m.key: m for m in metric_defs}
    out: dict[str, MetricRow] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    financials = _extract_release_financials(lines)
    for key, value in financials.items():
        if key in defs:
            out[key] = _row(defs[key], value, f"[search] Liverpool release {key}")
    if "revenue" in defs and "revenue" not in out:
        parts = [
            financials.get("revenue_commercial"),
            financials.get("revenue_financial"),
            financials.get("revenue_real_estate"),
        ]
        if all(v is not None for v in parts):
            out["revenue"] = _row(defs["revenue"], sum(parts), "[search] Liverpool release revenue sum")

    cards = _extract_cards(lines, period)
    if cards is not None and "cards_total" in defs:
        out["cards_total"] = _row(defs["cards_total"], cards, "[search] Liverpool card table")

    stores = _extract_liverpool_stores(lines)
    if stores is not None and "stores_liverpool" in defs:
        out["stores_liverpool"] = _row(defs["stores_liverpool"], stores, "[search] Liverpool profile table")

    sss_liverpool = _extract_sss(lines, "liverpool")
    if sss_liverpool is not None and "sss_liverpool" in defs:
        out["sss_liverpool"] = _row(defs["sss_liverpool"], sss_liverpool, "[search] Liverpool SSS")

    sss_suburbia = _extract_sss(lines, "suburbia")
    if sss_suburbia is not None and "sss_suburbia" in defs:
        out["sss_suburbia"] = _row(defs["sss_suburbia"], sss_suburbia, "[search] Suburbia SSS")

    ecommerce = _extract_ecommerce(lines)
    if ecommerce is not None and "ecommerce_penetration" in defs:
        out["ecommerce_penetration"] = _row(defs["ecommerce_penetration"], ecommerce, "[search] Liverpool digital penetration")

    npl = _extract_group_npl(lines)
    if npl is not None and "npl" in defs:
        out["npl"] = _row(defs["npl"], npl, "[search] Liverpool Group NPL")

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


def _extract_cards(lines: list[str], period: str | None) -> float | None:
    target = normalize_target_period(period)
    for idx, line in enumerate(lines):
        if "Número Total de Tarjetas" not in line and "Numero Total de Tarjetas" not in line:
            continue
        context = " ".join(lines[max(0, idx - 3):idx + 1])
        if not re.search(r"EL\s+PUERTO\s+DE\s+LIVERPOOL", context, re.IGNORECASE):
            continue
        vals = [v for v in _numbers(line) if v >= 100]
        if vals:
            return round(vals[0])

    for idx, line in enumerate(lines):
        if not re.search(r"tarjetas\s+liverpool|liverpool\s+cards", line, re.IGNORECASE):
            continue
        has_suburbia_next = idx + 1 < len(lines) and re.search(
            r"tarjetas\s+suburbia|suburbia\s+cards", lines[idx + 1], re.IGNORECASE
        )
        liverpool = _numbers(line)
        suburbia = _numbers(lines[idx + 1]) if has_suburbia_next else []
        if not liverpool:
            continue
        if "participacion" in _squash(line) or max(liverpool) < 100_000:
            continue
        headers = _period_headers(lines[max(0, idx - 4):idx])
        col = 0
        if target is not None and headers:
            for pos, header_text in enumerate(headers[:len(liverpool)]):
                parsed = parse_header_token(header_text)
                if parsed and parsed.year == target.year and parsed.quarter == target.quarter:
                    col = pos
                    break
        if col >= len(liverpool):
            col = 0
        total = liverpool[col]
        if col < len(suburbia):
            return round(liverpool[col] / 1000.0) + round(suburbia[col] / 1000.0)
        return round(total / 1000.0)
    return None


def _period_headers(context: list[str]) -> list[str]:
    joined = " ".join(context)
    return re.findall(r"\b[1-4][QT]\s*'?[\d]{2,4}\b", joined, flags=re.IGNORECASE)


def _extract_group_npl(lines: list[str]) -> float | None:
    patterns = [
        r"The\s+Group.?s\s+NPLs[^%\n]{0,120}?(?:(?:were|stood|closed)\s+at|were)\s+([\d.]+)%",
        r"total\s+NPL\s+portfolio\s+closed\s+at\s+([\d.]+)%",
        r"cuentas\s+vencidas\s+a\s+m[aá]s\s+de\s+90\s+d[ií]as\s+([\d.]+)%",
        r"cartera\s+vencida\s+a\s+m[aá]s\s+de\s+90\s+d[ií]as\s+del\s+Grupo[^%\n]{0,80}?es\s+de\s+([\d.]+)%",
        r"cartera\s+vencida\s+a\s+m[aá]s\s+de\s+90\s+d[ií]as\s+se\s+ubic[oó]\s+en\s+([\d.]+)%",
        r"cartera\s+vencida\s+total[^%\n]{0,80}?(\d\.\d+)%",
    ]
    for line in lines:
        for pattern in patterns:
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                return parse_number(m.group(1))
    return None


def _extract_release_financials(lines: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    commercial_parts: dict[str, float] = {}
    for idx, line in enumerate(lines):
        norm = _squash(line)
        values = _money_values(line)
        if not values:
            continue
        if "ingresos segmento comercial" in norm:
            out.setdefault("revenue_commercial", values[0])
        elif "commercial revenues" not in norm and norm.startswith("total") and _recent_header(lines, idx, r"ingresos comerciales|commercial revenues"):
            out.setdefault("revenue_commercial", values[0])
        elif re.search(r"\bliverpool\b", norm) and _recent_header(lines, idx, r"ingresos comerciales|commercial revenues"):
            commercial_parts["liverpool"] = values[0]
        elif "suburbia" in norm and _recent_header(lines, idx, r"ingresos comerciales|commercial revenues"):
            commercial_parts["suburbia"] = values[0]
        elif "ingresos segmento negocios financieros" in norm:
            out.setdefault("revenue_financial", values[0])
        elif norm.startswith("intereses") and not re.search(r"ganados|gasto|expense", norm):
            if line.strip().startswith("INTERESES"):
                continue
            val = _income_note_value(values)
            if val is not None:
                out.setdefault("revenue_financial", val)
        elif "ingresos segmento inmobiliaria" in norm:
            out.setdefault("revenue_real_estate", values[0])
        elif norm.startswith("arrendamiento"):
            if line.strip().startswith("ARRENDAMIENTO"):
                continue
            val = _income_note_value(values)
            if val is not None:
                out.setdefault("revenue_real_estate", val)
        elif "ingresos totales" in norm or norm.startswith("total de ingresos"):
            val = _income_note_value(values)
            if val is not None:
                out.setdefault("revenue", val)
    if "revenue_commercial" not in out and commercial_parts:
        # Legacy releases break commercial sales into Liverpool and Suburbia
        # rows, then print a Total row. When the Total row is lost to OCR,
        # these two rows are still the same reported commercial segment.
        if {"liverpool", "suburbia"} <= commercial_parts.keys():
            out["revenue_commercial"] = commercial_parts["liverpool"] + commercial_parts["suburbia"]
    return out


def _income_note_value(values: list[float]) -> float | None:
    positives = [v for v in values if 100 < v < 100_000]
    if not positives:
        return None
    # For BMV note rows:
    #   Q1: current, prior
    #   Q2-Q4 legacy: acum current, acum prior, quarter current, quarter prior
    #   2025+ some filings: quarter current, acum current, quarter prior, acum prior
    # Prefer the first value when it is already a quarter-scale number; otherwise
    # use the third value from the legacy accumulated-first layout.
    if len(positives) >= 4 and positives[0] > positives[2] * 1.2 and positives[1] > positives[3] * 1.2:
        return positives[2]
    return positives[0]


def _extract_liverpool_stores(lines: list[str]) -> float | None:
    counts: dict[str, int] = {}
    profile_remaining = 0
    for idx, line in enumerate(lines):
        norm = _squash(line)
        if re.search(r"perfil de la empresa|about the company|tiendas y centros comerciales|compania cuenta con las siguientes tiendas", norm):
            counts = {}
            profile_remaining = 25
            continue
        profile_context = profile_remaining > 0
        if profile_remaining > 0:
            profile_remaining -= 1
        if "suburbia" in norm or "fabrica" in norm or "duty free" in norm:
            continue
        if profile_context and re.search(r"liverpool\s+express", norm):
            value = _first_count(line)
            if value is not None and value < 150:
                counts["express"] = value
            continue
        if profile_context and re.search(r"almacenes?\s+liverpool|liverpool stores|^liverpool\b", norm):
            value = _first_count(line)
            if value is not None and value < 250:
                counts["stores"] = value
        if profile_context and "boutiques" in norm:
            value = _first_count(line)
            if value is not None and value < 250:
                counts["boutiques"] = value
        if profile_context and "stores" in counts and "boutiques" in counts:
            return float(counts["stores"] + counts["boutiques"] + counts.get("express", 0))
        if "compania operaba" in norm or "company operated" in norm:
            window = " ".join(lines[idx:idx + 3])
            m = re.search(r"(\d+)\s+con\s+el\s+nombre\s+de\s+Liverpool", window, re.IGNORECASE)
            if m:
                counts.setdefault("stores", int(m.group(1)))
            m = re.search(r"(\d+)\s+boutiques", window, re.IGNORECASE)
            if m:
                counts.setdefault("boutiques", int(m.group(1)))
    if "stores" in counts and "boutiques" in counts:
        total = counts["stores"] + counts["boutiques"] + counts.get("express", 0)
        if total >= 180:
            return float(total)
    return None


def _extract_sss(lines: list[str], brand: str) -> float | None:
    brand_pat = "suburbia" if brand == "suburbia" else "liverpool"
    patterns = [
        rf"vmt\s+{brand_pat}[^-\d(]{{0,80}}({_PCT_TOKEN})",
        rf"same-store\s+growth\s+{brand_pat}[^-\d(]{{0,80}}({_PCT_TOKEN})",
        rf"crecimiento\s+mismas\s+tiendas\s+{brand_pat}[^-\d(]{{0,80}}({_PCT_TOKEN})",
        rf"ventas\s+a\s+(?:mismas|tiendas)\s+iguales\s+en\s+{brand_pat}[^-\d(]{{0,80}}(?:crecen|crecieron|es\s+de|registraron\s+un)?\s*({_PCT_TOKEN})",
    ]
    for idx, line in enumerate(lines):
        if not re.search(brand_pat, line, re.IGNORECASE):
            continue
        text = " ".join(lines[idx:idx + 2])
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                return _signed_pct(text, m.group(1))
    # Section form: a heading line "Liverpool" / "Suburbia" followed by the
    # same-store-sales sentence on the next line.
    for idx, line in enumerate(lines[:-1]):
        if _squash(line) != brand_pat:
            continue
        text = " ".join(lines[idx + 1:idx + 3])
        m = re.search(rf"ventas\s+a\s+mismas\s+tiendas[^-\d(]{{0,80}}(?:incremento|crecieron|decrecen|decremento)?\s*(?:de\s+)?({_PCT_TOKEN})", text, re.IGNORECASE)
        if m:
            return _signed_pct(text, m.group(1))
    for idx, line in enumerate(lines[:-1]):
        norm = _squash(line)
        if brand_pat not in norm or _pct_values(line):
            continue
        text = " ".join(lines[idx + 1:idx + 4])
        m = re.search(rf"\bvmt\s+({_PCT_TOKEN})", text, re.IGNORECASE)
        if m:
            return _signed_pct(text, m.group(1))
    return None


def _extract_ecommerce(lines: list[str]) -> float | None:
    for idx, line in enumerate(lines):
        text = " ".join(lines[idx:idx + 2])
        norm = _squash(text)
        if "suburbia" in norm:
            continue
        patterns = [
            rf"participacion\s+digital\s+(?:de\s+liverpool\s+)?(?:durante\s+el\s+trimestre\s+)?(?:ha\s+sido\s+del|alcanzo|alcanzando\s+el)\s+({_PCT_TOKEN})",
            rf"({_PCT_TOKEN})\s+de\s+(?:las\s+)?ventas\s+(?:comerciales\s+)?(?:en\s+el\s+canal\s+digital|digitales)",
        ]
        for pattern in patterns:
            m = re.search(pattern, norm, re.IGNORECASE)
            if m:
                return parse_number(m.group(1))
    return None


def _signed_pct(text: str, raw: str) -> float | None:
    value = parse_number(raw)
    if value is None:
        return None
    if value > 0 and re.search(r"decrec|decrement|disminu|declin|declined|decreased", _squash(text)):
        return -value
    return value


def _recent_header(lines: list[str], idx: int, pattern: str) -> bool:
    context = " ".join(lines[max(0, idx - 5):idx + 1])
    return re.search(pattern, context, re.IGNORECASE) is not None


def _first_count(line: str) -> int | None:
    for val in _numbers(line):
        if float(val).is_integer() and 0 < val < 1000:
            return int(val)
    return None


def _money_values(line: str) -> list[float]:
    line = _remove_note_markers(line)
    line = _clean_ocr_number_spaces(line)
    release_scale = "%" in line
    vals: list[float] = []
    for raw in _NUMBER_RE.findall(line):
        if "%" in raw:
            continue
        value = parse_number(raw)
        if value is None or abs(value) < 100:
            continue
        vals.append(_normalize_currency(value, release_scale=release_scale))
    return vals


def _remove_note_markers(line: str) -> str:
    labels = (
        r"Ingresos Segmento Comercial",
        r"Ingresos Segmento Negocios Financieros",
        r"Ingresos Segmento Inmobiliaria",
        r"Ventas Liverpool[^0-9]*",
    )
    for label in labels:
        line = re.sub(rf"({label})\s+[1-4]\s+(?=\d{{1,3}}\s*,)", r"\1 ", line, flags=re.IGNORECASE)
    return line


def _clean_ocr_number_spaces(line: str) -> str:
    line = re.sub(r"(\d)\s+,", r"\1,", line)
    prev = None
    while prev != line:
        prev = line
        line = re.sub(r"(?<![\d,.])(\d)\s+(?=\d{1,2}\s*,)", r"\1", line)
    return line


def _pct_values(line: str) -> list[float]:
    vals: list[float] = []
    for raw in re.findall(_PCT_TOKEN, line):
        if "%" not in raw and "(" not in raw:
            continue
        value = parse_number(raw)
        if value is not None:
            vals.append(value)
    return vals


def _normalize_currency(value: float, *, release_scale: bool) -> float:
    abs_val = abs(value)
    if release_scale:
        if abs_val >= 100_000:
            return value / 1000.0
        return value
    if abs_val >= 1_000_000:
        return value / 1_000_000.0
    return value


def _squash(text: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip().lower()


def _numbers(line: str) -> list[float]:
    line = _clean_ocr_number_spaces(line)
    vals: list[float] = []
    for raw in re.findall(r"(?:N/A|n\.a\.|[\d]{1,3}(?:\s*,\s*[\d]{3})+(?:\.\d+)?|[\d]+(?:\.\d+)?)", line):
        if raw.lower().startswith("n"):
            vals.append(0.0)
            continue
        value = parse_number(raw)
        if value is not None:
            vals.append(value)
    return vals


_PCT_TOKEN = r"\(?\s*[-+]?\d+(?:\.\d+)?%?\s*\)?"
_NUMBER_RE = re.compile(r"\(?\s*[-+]?\$?\s*\d{1,3}(?:\s*,\s*\d{3})+(?:\.\d+)?%?\s*\)?|\(?\s*[-+]?\$?\s*\d+(?:\.\d+)?%?\s*\)?")
