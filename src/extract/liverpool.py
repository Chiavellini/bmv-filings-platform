"""Deterministic extractors for Liverpool KPI rows."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

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
    # Modern (2025+) filings print a LIVERPOOL / SUBURBIA / EL PUERTO trio of
    # "Número Total de Tarjetas (Miles)" rows. GT sums the two component rows;
    # the filing's own total row can disagree by 1 from rounding (4Q25 prints
    # 6,325 + 2,052 = 8,377 components but an 8,378 total), so prefer the
    # component sum whenever it reconciles with the printed total within 1.
    miles = []
    for line in lines:
        if re.search(r"n[uú]mero\s+total\s+de\s+tarjetas\s*\(miles\)", _squash(line)):
            vals = _numbers(line)
            if vals:
                miles.append(vals[0])
    if len(miles) >= 3 and abs(miles[0] + miles[1] - miles[2]) <= 1:
        return round(miles[0] + miles[1])
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
        # The Suburbia row can land past a page break (page footer + "===== Página
        # N =====" + filing header), so scan ahead rather than only the next line.
        suburbia_line = None
        for nxt in lines[idx + 1:idx + 8]:
            if re.search(r"tarjetas\s+suburbia|suburbia\s+cards", nxt, re.IGNORECASE):
                suburbia_line = nxt
                break
            if _numbers(nxt) and not re.search(
                    r"p[aá]gina|clave de cotizaci[oó]n|\d+\s+de\s+\d+", nxt, re.IGNORECASE):
                break   # a different data row intervenes — no Suburbia split here
        liverpool = _numbers(line)
        suburbia = _numbers(suburbia_line) if suburbia_line else []
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
            return round((liverpool[col] + suburbia[col]) / 1000.0)
        return round(total / 1000.0)
    return None


def _period_headers(context: list[str]) -> list[str]:
    joined = " ".join(context)
    return re.findall(r"\b[1-4][QT]\s*'?[\d]{2,4}\b", joined, flags=re.IGNORECASE)


def _extract_group_npl(lines: list[str]) -> float | None:
    patterns = [
        r"The\s+Group.?s\s+NPLs[^%\n]{0,120}?(?:(?:were|stood|closed)\s+at|were)\s+([\d.]+)%",
        r"total\s+NPL\s+portfolio\s+closed\s+at\s+([\d.]+)%",
        r"\bNPL\s+levels?[^%\n]{0,120}?clos(?:ed|ing)\s+at\s+(\d{1,2}\.\d+)%",
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
    # Corporate-deck slide tiles. 2024 era: a header row names the three credit
    # KPIs and the value row prints them in the same order, the reserve tagged
    # "cobertura": "Portafolio Neto de Crédito Cartera Vencida Reserva de
    # Incobrables" → "+22.2%  3.1%  9.9% cobertura".
    for idx, line in enumerate(lines):
        norm = _squash(line)
        if "portafolio neto de credito" in norm and "cartera vencida" in norm:
            # The value row can land past a page break (footer + página marker +
            # filing header), e.g. 2024-3T header line 87 → values line 97.
            for nxt in lines[idx + 1:idx + 8]:
                m = re.search(r"[-+]?[\d.,]+%\s+(\d{1,2}\.\d)%\s+[\d.]+%\s*cobertura",
                              nxt, re.IGNORECASE)
                if m:
                    return parse_number(m.group(1))
    # 2025+ deck: the value row sits ABOVE its label row ("129  4.0%  +1,800" /
    # "Boutiques NPLs Rate Tenants"); the NPL is the sole percentage on the value
    # row whose label row says "NPLs Rate".
    for idx, line in enumerate(lines[1:], start=1):
        if not re.search(r"\bNPLs?\s+Rate\b", line):
            continue
        for prev in reversed(lines[max(0, idx - 3):idx]):
            m = re.search(r"(?<![\d.])(\d{1,2}\.\d)%", prev)
            if m:
                return parse_number(m.group(1))
    return None


def _extract_release_financials(lines: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    commercial_parts: dict[str, float] = {}
    direct_rows: set[str] = set()
    for idx, line in enumerate(lines):
        norm = _squash(line)
        values = _money_values(line)
        if not values:
            continue
        if _is_header_only_row(line, norm, values):
            continue
        if re.search(r"^(?:ingresos\s+)?segmento comercial\b", norm):
            if "revenue_commercial" not in direct_rows:
                out["revenue_commercial"] = values[0]
                direct_rows.add("revenue_commercial")
        elif norm.startswith("ingresos comerciales"):
            out.setdefault("revenue_commercial", values[0])
        elif norm.startswith("ventas comercial"):
            # 2018-era release table: "Ventas Comercial <Q-cur> <Q-prior> <var%>
            # <YTD-cur> <YTD-prior> <var%>" (quarter-current first).
            out.setdefault("revenue_commercial", values[0])
        elif "commercial revenues" not in norm and norm.startswith("total") and _recent_header(lines, idx, r"ingresos comerciales|commercial revenues"):
            out.setdefault("revenue_commercial", values[0])
        elif re.search(r"\bliverpool\b", norm) and _recent_header(lines, idx, r"ingresos comerciales|commercial revenues"):
            commercial_parts["liverpool"] = values[0]
        elif "suburbia" in norm and _recent_header(lines, idx, r"ingresos comerciales|commercial revenues"):
            commercial_parts["suburbia"] = values[0]
        elif re.search(r"^(?:ingresos\s+)?segmento negocios financieros\b", norm):
            if "revenue_financial" not in direct_rows:
                out["revenue_financial"] = values[0]
                direct_rows.add("revenue_financial")
        elif norm.startswith("intereses") and not re.search(r"ganados|gasto|expense|recibidos|pagados|cobrados", norm):
            if line.strip().startswith("INTERESES"):
                continue
            val = _income_note_value(values)
            if val is not None:
                out.setdefault("revenue_financial", val)
        elif re.search(r"^(?:ingresos\s+)?segmento inmobiliaria\b", norm):
            if "revenue_real_estate" not in direct_rows:
                out["revenue_real_estate"] = values[0]
                direct_rows.add("revenue_real_estate")
        elif norm.startswith("arrendamiento"):
            if line.strip().startswith("ARRENDAMIENTO"):
                continue
            val = _income_note_value(values)
            if val is not None:
                out.setdefault("revenue_real_estate", val)
        elif "ingresos totales" in norm or norm.startswith("total de ingresos"):
            val = values[0] if "%" in line and values else _income_note_value(values)
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


def _is_header_only_row(line: str, norm: str, values: list[float]) -> bool:
    if "var" not in norm:
        return False
    if "," in line or any("." in token for token in _NUMBER_RE.findall(line)):
        return False
    return all(1900 <= abs(v) <= 2099 for v in values)


def _extract_liverpool_stores(lines: list[str]) -> float | None:
    counts: dict[str, int] = {}
    # Fallback tallies that are never reset by a later section trigger: the
    # "Compañía operaba un total de…" company-profile sentence and the English
    # release's store table ("Liverpool Stores: 122 … Boutiques: 116").
    fallback: dict[str, int] = {}
    profile_remaining = 0
    for idx, line in enumerate(lines):
        norm = _squash(line)
        m = re.match(r"liverpool\s+stores:?\s+(\d{2,3})\b", norm)
        if m:
            fallback.setdefault("stores", int(m.group(1)))
        m = re.match(r"boutiques:?\s+(\d{2,3})(?:\b|(?=\d{1,3},\d{3}\b))", norm)
        if m:
            fallback.setdefault("boutiques", int(m.group(1)))
        if "compania operaba" in norm or "company operated" in norm:
            window = " ".join(lines[idx:idx + 3])
            m = (re.search(r"(\d+)\s+con\s+el\s+nombre\s+de\s+Liverpool\b", window, re.IGNORECASE)
                 or re.search(r"total\s+de\s+(\d+)\s+tiendas\s+departamentales\s+con\s+el\s+nombre\s+de\s+Liverpool",
                              window, re.IGNORECASE))
            if m:
                fallback.setdefault("stores", int(m.group(1)))
            # "\b(?:de)?" tolerates the OCR-glued "además de108 boutiques" (2017-1T).
            m = re.search(r"\b(?:de)?(\d{2,3})\s+boutiques", window, re.IGNORECASE)
            if m:
                fallback.setdefault("boutiques", int(m.group(1)))
            m = re.search(r"(\d+)\s+(?:tiendas\s+)?Liverpool\s+express", window, re.IGNORECASE)
            if m:
                fallback.setdefault("express", int(m.group(1)))
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
            # OCR can glue the count to the area column ("Boutiques: 11975,093 m2"
            # = 119 + 75,093 m²); the count is the 2-3 digits before the comma'd area.
            m = re.search(r"boutiques:?\s*(\d{2,3})(?=\d{1,3},\d{3}\b)", norm)
            value = int(m.group(1)) if m else _first_count(line)
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
    for tallies in (counts, fallback):
        if "stores" in tallies and "boutiques" in tallies:
            total = tallies["stores"] + tallies["boutiques"] + tallies.get("express", 0)
            if total >= 180:
                return float(total)
    return _extract_deck_stores(lines)


def _extract_deck_stores(lines: list[str]) -> float | None:
    """Corporate-deck (2025+) 'at a Glance' tiles: each count sits 1-3 lines ABOVE
    its label line ("125 5.1 million 30" / "Liverpool Stores Liverpool Credit
    Cards …"). GT "Total Liverpool" = Liverpool stores + Express + Boutiques."""
    stores = express = boutiques = None
    for idx, line in enumerate(lines):
        norm = _squash(line)
        if stores is None and norm.startswith("liverpool stores"):
            stores = _count_above(lines, idx, 300)
        elif stores is None and norm == "liverpool" and any(
                _squash(nx).startswith("stores") for nx in lines[idx + 1:idx + 3]):
            # 2022 English deck fragments: "122" / "Liverpool" / (stray tile text)
            # / "stores nationwide".
            stores = _count_above(lines, idx, 300)
        elif express is None and norm.startswith("liverpool express"):
            express = _count_above(lines, idx, 200)
        elif boutiques is None and norm.startswith("boutiques") and (
                "npls rate" in norm or "tenants" in norm):
            boutiques = _count_above(lines, idx, 400)
        elif boutiques is None and "boutiques" in norm and "across" in norm:
            # "113" / "boutiques across" / "21 Mexican states" (2022 English deck).
            boutiques = _count_above(lines, idx, 400)
    if stores and boutiques:
        total = stores + boutiques + (express or 0)
        if total >= 180:
            return float(total)
    return None


def _count_above(lines: list[str], idx: int, cap: int) -> int | None:
    """Nearest integer count above a deck label line (skipping prose tile text)."""
    for prev in reversed(lines[max(0, idx - 3):idx]):
        for v in _numbers(prev):
            if float(v).is_integer() and 0 < v < cap:
                return int(v)
    return None


def _extract_sss(lines: list[str], brand: str) -> float | None:
    brand_pat = "suburbia" if brand == "suburbia" else "liverpool"
    compact = _extract_compact_vmt(lines, brand)
    if compact is not None:
        return compact
    patterns = [
        rf"vmt\s+{brand_pat}[^-\d(]{{0,80}}({_PCT_TOKEN})",
        rf"same-store\s+growth\s+{brand_pat}[^-\d(]{{0,80}}({_PCT_TOKEN})",
        rf"crecimiento\s+mismas\s+tiendas\s+{brand_pat}[^-\d(]{{0,80}}({_PCT_TOKEN})",
        rf"ventas\s+a\s+(?:mismas|tiendas)\s+iguales\s+en\s+{brand_pat}[^-\d(]{{0,80}}(?:crecen|crecieron|es\s+de|registraron\s+un)?\s*({_PCT_TOKEN})",
        # English releases (2022): "Liverpool's same-store sales increased 27.6%
        # in the quarter." / "Suburbia's same-store sales increased 24.1%…".
        # "(?<!and )" keeps the dual sentence "Liverpool and Suburbia's same store
        # sales grew 27.6% and 24.1%" from crediting Liverpool's figure to Suburbia.
        rf"(?<!and ){brand_pat}.{{0,2}}s\s+same[\s-]stores?\s+sales\s+(?:increased|decreased|grew|declined|fell)(?:\s+by)?\s+({_PCT_TOKEN})",
        # "En Suburbia, las ventas a tiendas iguales tuvieron una disminución de
        # 31.4%" (2020-21 bullet form; also the Liverpool variant).
        rf"en\s+{brand_pat}[^\n]{{0,6}}\s+las\s+ventas\s+a\s+(?:mismas\s+tiendas|tiendas\s+iguales)[^%\d]{{0,80}}?({_PCT_TOKEN})",
        # 2017-18 Suburbia aside: "las ventas a mismas tiendas Suburbia (no
        # incluidas en el indicador anterior) se incrementaron 4.7%". The sentence
        # sometimes wraps right before "Suburbia", so the parenthetical alone is
        # also accepted when it opens with the "no incluidas" marker.
        rf"ventas\s+a\s+mismas\s+tiendas\s+{brand_pat}\s*(?:\([^)]*\))?[^%\d]{{0,60}}?({_PCT_TOKEN})",
    ]
    if brand == "suburbia":
        patterns.append(
            rf"suburbia\s*\(no\s+incluidas[^)]*\)\s*se\s+incrementaron\s+({_PCT_TOKEN})")
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


def _extract_compact_vmt(lines: list[str], brand: str) -> float | None:
    for idx, line in enumerate(lines):
        if not re.search(r"\bvmt\b", line, re.IGNORECASE):
            continue
        pct_values = _pct_values(line)
        if len(pct_values) < 2:
            continue
        norm = _squash(line)
        if brand == "liverpool" and re.search(r"vmt\s+liverpool", norm):
            return pct_values[0]
        if brand == "suburbia" and re.search(r"vmt\s+suburbia", norm):
            return pct_values[0]
        context = _squash(" ".join(lines[max(0, idx - 4):idx + 1]))
        if "liverpool" in context and "suburbia" in context:
            if brand == "liverpool":
                return pct_values[0]
            if len(pct_values) >= 3:
                return pct_values[2]
    return None


def _extract_ecommerce(lines: list[str]) -> float | None:
    # Corporate-deck tile (2025+): percentages sit one line above the label row
    # "Digital Share  Increase in digital sales"; the share is the first one.
    for idx, line in enumerate(lines[1:], start=1):
        if _squash(line).startswith("digital share"):
            for prev in reversed(lines[max(0, idx - 3):idx]):
                m = re.search(r"(\d{1,2}\.\d)%", prev)
                if m:
                    return parse_number(m.group(1))
    for idx, line in enumerate(lines):
        text = " ".join(lines[idx:idx + 2])
        norm = _squash(text)
        if "suburbia" in norm:
            continue
        patterns = [
            # NOTE: "participación digital" (a broader omnichannel figure, e.g. 28.7%) is
            # NOT the GT "E-Commerce Penetration" (e.g. 21.4% for 1Q25); matching flexible
            # "participacion digital" forms grabbed the wrong metric, so patterns are kept
            # narrow to the exact e-commerce-penetration phrasings only.
            rf"participacion\s+digital\s+(?:de\s+liverpool\s+)?(?:para\s+liverpool\s+)?(?:durante\s+el\s+(?:primer|segundo|tercer|cuarto)?\s*trimestre\s+)?(?:ha\s+sido\s+del|alcanzo|alcanzando\s+el|fue\s+de)\s+({_PCT_TOKEN})",
            # Share OF total/commercial sales only. A "…de las ventas digitales"
            # tail is the inverse ratio (e.g. Click & Collect as 19% of DIGITAL
            # sales, 2020-3T:100) and must not match.
            rf"({_PCT_TOKEN})\s+de\s+(?:las\s+)?ventas\s+(?:total(?:es)?|comerciales)",
            rf"({_PCT_TOKEN})\s+en\s+el\s+canal\s+digital",
            # Quarterly digital share stated inline with growth: "…crecieron 26.5%
            # y representaron el 9.3% de las ventas" (2020 era).
            rf"y\s+representaron\s+el\s+({_PCT_TOKEN})\s+del?\s+(?:total\s+de\s+las\s+|las\s+)?ventas",
            # English releases (2022): "The digital channel's share reached 21.8%
            # during the quarter." / "digital sales achieved a participation of
            # 21.8% of commercial sales".
            rf"digital\s+channel.{{0,2}}s\s+share\s+reached\s+({_PCT_TOKEN})",
            rf"digital\s+sales\s+achieved\s+a\s+participation\s+of\s+({_PCT_TOKEN})",
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
    squashed = _squash(text)
    raw_norm = _squash(raw).replace(" ", "")
    compact = squashed.replace(" ", "")
    pos = compact.find(raw_norm)
    if pos >= 0:
        # Only use nearby wording for sign. Liverpool/Suburbia SSS sentences often
        # mention ticket or transaction declines after the SSS percentage; that
        # should not flip the SSS sign.
        sign_context = compact[max(0, pos - 70):pos + len(raw_norm) + 20]
    else:
        sign_context = compact
    if value > 0 and re.search(r"decrec|decrement|disminu|reducc|declin|declined|decreased|fell", sign_context):
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
        # Some legacy press releases print FULL PESOS inside a %-bearing table
        # (e.g. 2016-3T "Ingresos Totales: 21,767,113,000 ... 10.9%"). Those must
        # be divided by 1e6 → millions, not 1e3 (which left 3Q16 revenue ×1000 high).
        if abs_val >= 1_000_000_000:
            return value / 1_000_000.0
        if abs_val >= 100_000:           # thousands of pesos
            return value / 1000.0
        return value
    if abs_val >= 1_000_000:
        return value / 1_000_000.0
    return value


@lru_cache(maxsize=8192)
def _squash(text: str) -> str:
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
