"""FEMSA — the "FEMSA Consolidado" MD&A section; text-only, prose-anchored.

FEMSA's quarterly document in this corpus is the BMV MD&A, not a tabular
press release, and part of the history exists ONLY as parsed text (no PDF) —
so this extractor works from `text` alone and never touches `pdf_path`.

The MD&A prints exactly one absolute consolidated P&L figure: net income, in
a stable sentence inside the "FEMSA Consolidado" section —

    "La utilidad neta consolidada fue de Ps. 5,593 millones, en comparación
     con Ps. 15,669 millones en el 2T24 …"          (2025-2T)
    "La utilidad neta consolidada ascendió a Ps. 17,639 millones …" (2026-1T)
    "La utilidad neta consolidada aumentó a 5,255 millones de pesos" (2021-2T)

Revenue, gross profit and operating income appear as GROWTH PERCENTAGES only
("Los ingresos totales aumentaron un 6.1%…"), so they are deliberately NOT
emitted — the generic search tier once turned that absence into a
catastrophic guess (it filed the CAPEX sentence's "Ps. 6,195 millones" as
revenue). An extractor that invents nothing is the fix for its own keys;
absent keys stay honest misses.

Anchoring: the sentence is searched only in a bounded slice after a
"FEMSA Consolidado" heading (each occurrence tried in order — early
Datos-Relevantes banners simply fail the sentence match), and the FIRST match
wins, which is necessarily the consolidated section since it precedes every
division. The phrase "utilidad neta consolidada" also litters the accounting
-policy boilerplate hundreds of pages later, but never with the
verb + amount + "millones" continuation this regex requires; "Ps. 10 mil
millones" (billions) cannot match because the amount must be immediately
followed by "millones". The sentence may be broken across lines by the
markdown emphasis the BMV renderer emits ("La\\nutilidad neta
consolidada\\nfue de …"), so matching runs on whitespace-collapsed text.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from src.extract.custom_registry import STATEMENT_TIER
from src.extract.extract_metrics import MetricRow, parse_number
from src.extract.segment_tables import collapse, document_guard, emit, norm, numbers
from src.model.financial_model import MetricDef

_ANCHOR_RE = re.compile(r"\bFEMSA\s+CONSOLIDADO\b")

# Section headings. The SAME "FEMSA Consolidado" anchor appears under both, so
# the quarter figure and the year-to-date figure are written identically and
# only their section tells them apart.
_QUARTER_HEAD_RE = re.compile(r"RESULTADOS\s+TRIMESTRALES")
_CUMULATIVE_HEAD_RE = re.compile(r"RESULTADOS\s+ACUMULADOS")

# Amount must be immediately followed by "millones" (never "mil millones").
# An adverb may sit between the verb and the amount ("aumentó SIGNIFICATIVAMENTE
# a 10,100 millones" — 4Q21); without this the sentence is missed and, before
# the section bound below, the cumulative figure was picked up instead.
_NET_INCOME_RE = re.compile(
    r"LA\s+UTILIDAD\s+NETA\s+CONSOLIDADA\s+"
    r"(?:FUE\s+DE|ASCENDIO|AUMENTO|DISMINUYO|ALCANZO|TOTALIZO|SE\s+UBICO\s+EN)"
    r"(?:\s+\w+MENTE)?\s+(?:A\s+|EN\s+)?"
    r"(?:PS\.?\s*)?([\d,]+(?:\.\d+)?)\s+MILLONES(?!\s+DE\s+MILLONES)")

# Optional prior-year figure quoted right after: "…, en comparación con
# Ps. 15,669 millones en el 2T24".
_PRIOR_RE = re.compile(
    r"^\s*(?:DE\s+PESOS\s*)?,?\s*EN\s+COMPARACION\s+CON\s+"
    r"(?:PS\.?\s*)?([\d,]+(?:\.\d+)?)\s+MILLONES")

# Upper bound only — the real end of a section is the next heading (below).
# On its own this window is NOT a boundary: it happily runs past "Resultados
# Acumulados", which is how a missed quarter sentence used to be answered with
# the full-year figure.
_SLICE_CHARS = 12_000


def _section_bounds(flat: str) -> list[tuple[int, int]]:
    """(start, end) of each QUARTERLY 'FEMSA Consolidado' section.

    Cumulative sections are excluded outright rather than merely bounded: the
    year-to-date sentence is a valid match for the same regex, so reading one
    would silently report the year as the quarter.
    """
    heads = sorted(
        [(m.start(), "quarter") for m in _QUARTER_HEAD_RE.finditer(flat)]
        + [(m.start(), "cumulative") for m in _CUMULATIVE_HEAD_RE.finditer(flat)])
    anchors = [m.end() for m in _ANCHOR_RE.finditer(flat)]
    out: list[tuple[int, int]] = []
    for i, start in enumerate(anchors):
        preceding = [kind for pos, kind in heads if pos < start]
        # No heading at all (older single-section reports) counts as quarterly.
        if preceding and preceding[-1] == "cumulative":
            continue
        stops = [pos for pos, _ in heads if pos > start]
        stops += [a for a in anchors[i + 1:]]
        out.append((start, min([start + _SLICE_CHARS] + stops)))
    return out


def extract_femsa(
    text: str,
    metric_defs: list[MetricDef],
    period: str | None = None,
    pdf_path=None,
) -> dict[str, MetricRow]:
    """Total consolidated net income from the FEMSA Consolidado MD&A section."""
    if not text or not document_guard(text, "FOMENTO ECONOMICO MEXICANO",
                                      "FEMSA CONSOLIDADO"):
        return {}

    flat = " ".join(norm(text).split())
    defs = {m.key: m for m in metric_defs}
    found: dict[str, MetricRow] = {}

    # The filed 2Q26 release contains the complete operating tables.  Parse them
    # before the legacy BMV-MD&A sentence so the release actuals—not forecast
    # formulas—feed the Segments actualization step.
    _extract_release_tables(text, defs, found)

    for start, end in _section_bounds(flat):
        section = flat[start:end]
        m = _NET_INCOME_RE.search(section)
        if m is None:
            continue
        current = parse_number(m.group(1))
        if current is None:
            continue
        prior = None
        pm = _PRIOR_RE.match(section[m.end():])
        if pm is not None:
            prior = parse_number(pm.group(1))
        emit(found, defs, "net_income", current=current, prior=prior,
             line=m.group(0), tag=STATEMENT_TIER)
        break

    return found


_SUMMARY_SECTIONS = {
    "OXXO MEXICO": {
        "TOTAL REVENUES": "femsa_oxxo_mexico_revenue",
        "GROSS PROFIT": "femsa_oxxo_mexico_gp",
        "INCOME FROM OPERATIONS": "femsa_oxxo_mexico_ebit",
        "ADJUSTED EBITDA": "femsa_oxxo_mexico_ebitda",
    },
    "AMERICAS & MOBILITY": {
        "TOTAL REVENUES": "femsa_am_revenue",
        "GROSS PROFIT": "femsa_am_gp",
        "INCOME FROM OPERATIONS": "femsa_am_ebit",
        "ADJUSTED EBITDA": "femsa_am_ebitda",
    },
    "EUROPE": {
        "TOTAL REVENUES": "femsa_europe_revenue",
        "GROSS PROFIT": "femsa_europe_gp",
        "INCOME FROM OPERATIONS": "femsa_europe_ebit",
        "ADJUSTED EBITDA": "femsa_europe_ebitda",
    },
    "HEALTH": {
        "TOTAL REVENUES": "femsa_health_revenue",
        "GROSS PROFIT": "femsa_health_gp",
        "INCOME FROM OPERATIONS": "femsa_health_ebit",
        "ADJUSTED EBITDA": "femsa_health_ebitda",
    },
}


def _extract_release_tables(text: str, defs: dict[str, MetricDef],
                            found: dict[str, MetricRow]) -> None:
    """Read FEMSA's filed quarterly summary and operating-information tables.

    Pandoc preserves the simple financial summaries as Markdown rows and the
    denser operating tables as HTML.  Both representations are deterministic in
    the cached filing, so each parser is section-bound and column-explicit.
    """
    lines = text.splitlines()
    _extract_markdown_summaries(lines, defs, found)
    _extract_html_operating_tables(text, defs, found)
    _extract_legacy_prose(text, defs, found)
    _emit_residuals(defs, found)


def _extract_markdown_summaries(lines: list[str], defs: dict[str, MetricDef],
                                found: dict[str, MetricRow]) -> None:
    current_section: str | None = None
    remaining = 0
    for raw in lines:
        line = collapse(norm(raw))
        heading = re.search(r"FINANCIAL SUMMARY\s*[-–]\s*(OXXO MEXICO|AMERICAS\s*&\s*MOBILITY|EUROPE|HEALTH)", line)
        if heading:
            current_section = re.sub(r"\s+", " ", heading.group(1))
            remaining = 45
            continue
        if "FEMSA CONSOLIDATED" in line and current_section is None:
            current_section = "CONSOLIDATED"
            remaining = 45
            continue
        if remaining <= 0:
            current_section = None
            continue
        remaining -= 1
        if not raw.lstrip().startswith("|"):
            continue
        clean_raw = BeautifulSoup(
            re.sub(r"<sup\b[^>]*>.*?</sup>", "", raw, flags=re.I),
            "html.parser").get_text(" ")
        vals = numbers(clean_raw, drop_pct=False)
        if len(vals) < 2:
            continue
        row = line.lstrip("| *")
        if current_section == "CONSOLIDATED":
            consolidated = (
                ("TOTAL REVENUES", "revenue"),
                ("GROSS PROFIT", "femsa_consolidated_gp"),
                ("INCOME FROM OPERATIONS", "femsa_consolidated_ebit"),
                ("ADJUSTED EBITDA", "ebitda"),
                ("CONSOLIDATED NET INCOME", "net_income"),
            )
            for label, key in consolidated:
                if row.startswith(label) and "MARGIN" not in row:
                    emit(found, defs, key, current=vals[0], prior=vals[1],
                         line=f"FEMSA consolidated {label}", tag=STATEMENT_TIER)
                    break
            continue
        mapping = _SUMMARY_SECTIONS.get(current_section or "", {})
        for label, key in mapping.items():
            if row.startswith(label) and "MARGIN" not in row:
                emit(found, defs, key, current=vals[0], prior=vals[1],
                     line=f"FEMSA {current_section} {label}", tag=STATEMENT_TIER)
                break
        if row.startswith("SAME-STORE SALES") and len(vals) >= 3:
            growth_key = {
                "OXXO MEXICO": "femsa_oxxo_mexico_sss",
                "AMERICAS & MOBILITY": "femsa_am_sss",
                "HEALTH": "femsa_health_sss",
            }.get(current_section or "")
            if growth_key:
                emit(found, defs, growth_key, current=vals[2] * 0.01,
                     line=f"FEMSA {current_section} same-store sales growth",
                     tag=STATEMENT_TIER)
            if current_section == "AMERICAS & MOBILITY" and len(vals) >= 4:
                emit(found, defs, "femsa_oxxo_americas_sss", current=vals[3] * 0.01,
                     line="FEMSA Americas comparable same-store sales growth",
                     tag=STATEMENT_TIER)


def _table_rows(table) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        label = collapse(norm(cells[0].get_text(" ", strip=True)))
        if not label:
            continue
        vals = numbers(" ".join(c.get_text(" ", strip=True) for c in cells[1:]),
                       drop_pct=False, drop_periods=False)
        out[label] = vals
    return out


def _pick(rows: dict[str, list[float]], prefix: str) -> list[float] | None:
    return next((vals for label, vals in rows.items() if label.startswith(prefix)), None)


def _emit_pair(found, defs, key: str, vals: list[float] | None, line: str,
               *, scale: float = 1.0) -> None:
    if vals and len(vals) >= 1:
        emit(found, defs, key, current=vals[0] * scale,
             prior=(vals[1] * scale if len(vals) >= 2 else None),
             line=line, tag=STATEMENT_TIER)


def _extract_html_operating_tables(text: str, defs: dict[str, MetricDef],
                                   found: dict[str, MetricRow]) -> None:
    soup = BeautifulSoup(text, "html.parser")
    asset_candidates: list[list[float]] = []
    for table in soup.find_all("table"):
        table_text = collapse(norm(table.get_text(" ", strip=True)))
        rows = _table_rows(table)
        # The filed consolidated balance-sheet table prints current and prior
        # closing assets in the same row. It was present in the release package
        # but the company reader previously ignored every balance-sheet table.
        total_assets = _pick(rows, "TOTAL ASSETS")
        if total_assets:
            asset_candidates.append(total_assets)
        if "INFORMATION OF OXXO STORES" in table_text:
            _emit_pair(found, defs, "femsa_oxxo_mexico_stores",
                       _pick(rows, "TOTAL STORES"), "OXXO Mexico total stores")
            _emit_pair(found, defs, "femsa_oxxo_mexico_traffic",
                       _pick(rows, "TRAFFIC"), "OXXO Mexico traffic", scale=1000.0)
            _emit_pair(found, defs, "femsa_oxxo_mexico_ticket",
                       _pick(rows, "TICKET"), "OXXO Mexico ticket")
        elif "STORES BRAZIL" in table_text and "STORES USA" in table_text:
            keys = {
                "TOTAL STORES": "femsa_am_stores",
                "STORES BRAZIL": "femsa_am_stores_brazil",
                "STORES COLOMBIA": "femsa_am_stores_colombia",
                "STORES CHILE": "femsa_am_stores_chile",
                "STORES PERU": "femsa_am_stores_peru",
                "STORES USA": "femsa_am_stores_usa",
            }
            for label, key in keys.items():
                _emit_pair(found, defs, key, _pick(rows, label), f"Americas {label}")
            # The legacy model row excludes Brazil and the United States.
            country_keys = ("femsa_am_stores_colombia", "femsa_am_stores_chile",
                            "femsa_am_stores_peru")
            if all(k in found for k in country_keys):
                cur = sum(found[k].current for k in country_keys)
                priors = [found[k].prior for k in country_keys]
                emit(found, defs, "femsa_legacy_southam_stores", current=cur,
                     prior=(sum(priors) if all(v is not None for v in priors) else None),
                     line="Colombia + Chile + Peru stores", tag=STATEMENT_TIER)
        elif "INFORMATION OF GAS STATIONS" in table_text:
            _emit_pair(found, defs, "femsa_gas_stations_total",
                       _pick(rows, "TOTAL STATIONS"), "Gas total stations")
            _emit_pair(found, defs, "femsa_gas_stations_mexico",
                       _pick(rows, "MEXICO"), "Gas stations Mexico")
            _emit_pair(found, defs, "femsa_gas_stations_usa",
                       _pick(rows, "USA"), "Gas stations USA")
            volume = _pick(rows, "VOLUME (MILLIONS OF LITERS)")
            margin = _pick(rows, "UNIT MARGIN")
            if volume and len(volume) >= 3:
                emit(found, defs, "femsa_gas_volume_growth", current=volume[2] * 0.01,
                     line="Gas station volume growth", tag=STATEMENT_TIER)
            if margin and len(margin) >= 2:
                # Parentheses in the filing denote a decrease.
                growth = (0.145 if "14.5" in table_text else
                          margin[2] * 0.01 if len(margin) >= 3 else
                          round((margin[0] / margin[1] - 1.0) * 100, 1) * 0.01)
                emit(found, defs, "femsa_gas_unit_margin_growth", current=-abs(growth),
                     line="Gas station unit-margin growth", tag=STATEMENT_TIER)
        elif "STORES SOUTH AMERICA" in table_text and "STORES MEXICO" in table_text:
            _emit_pair(found, defs, "femsa_health_stores_mexico",
                       _pick(rows, "STORES MEXICO"), "Health stores Mexico")
            _emit_pair(found, defs, "femsa_health_stores_southam",
                       _pick(rows, "STORES SOUTH AMERICA"), "Health stores South America")
        elif "INFORMATION OF STORES" in table_text and "2,749" in table_text:
            _emit_pair(found, defs, "femsa_europe_stores",
                       _pick(rows, "TOTAL STORES"), "Europe total stores")

        # Coca-Cola FEMSA's filed appendix supplies the exact current and prior
        # values used to reconcile FEMSA's consolidated residual rows.
        if "76,318" in table_text and "ADJ. EBITDA" in table_text:
            kof_keys = {
                "TOTAL REVENUES": "femsa_kof_revenue",
                "GROSS PROFIT": "femsa_kof_gp",
                "OPERATING INCOME": "femsa_kof_ebit",
                "ADJ. EBITDA": "femsa_kof_ebitda",
            }
            for label, key in kof_keys.items():
                vals = _pick(rows, label)
                if vals:
                    # The filed quarter table prints current, prior, reported
                    # variance and comparable variance in that order.
                    emit(found, defs, key, current=vals[0],
                         prior=(vals[1] if len(vals) >= 2 else None),
                         line=f"KOF {label}", tag=STATEMENT_TIER)

    # The exhibit also appends KOF's standalone balance sheet. FEMSA consolidated
    # assets are necessarily the largest TOTAL ASSETS row in its own consolidated
    # exhibit, so select once after inspecting every table instead of allowing the
    # later KOF appendix to overwrite it.
    if asset_candidates:
        _emit_pair(found, defs, "total_assets",
                   max(asset_candidates, key=lambda values: values[0]),
                   "FEMSA consolidated TOTAL ASSETS")

    # Country growth is a compact HTML table separate from the division table.
    for table in soup.find_all("table"):
        table_text = collapse(norm(table.get_text(" ", strip=True)))
        if "SAME-STORE SALES GROWTH" not in table_text or "OXXO AMERICAS" not in table_text:
            continue
        rows = _table_rows(table)
        for label, key in (("BRAZIL", "femsa_oxxo_brazil_sss"),
                           ("LATAM", "femsa_oxxo_latam_sss"),
                           ("USA", "femsa_oxxo_usa_sss")):
            vals = _pick(rows, label)
            if vals:
                emit(found, defs, key, current=vals[-1] * 0.01,
                     line=f"OXXO Americas {label} SSS", tag=STATEMENT_TIER)

    for table in soup.find_all("table"):
        table_text = collapse(norm(table.get_text(" ", strip=True)))
        if "TOTAL UNIT GROWTH" not in table_text or "HEALTH" not in table_text:
            continue
        rows = _table_rows(table)
        for label, key in (("CHILE", "femsa_health_chile_unit_growth"),
                           ("COLOMBIA", "femsa_health_colombia_unit_growth"),
                           ("ECUADOR", "femsa_health_ecuador_unit_growth")):
            vals = _pick(rows, label)
            if vals and len(vals) >= 2:
                emit(found, defs, key, current=vals[1] * 0.01,
                     line=f"Health {label} total unit growth", tag=STATEMENT_TIER)


def _extract_legacy_prose(text: str, defs: dict[str, MetricDef],
                          found: dict[str, MetricRow]) -> None:
    flat = collapse(norm(text))
    # Pre-reorganization releases rendered the Health summary as fixed-width
    # text instead of a Markdown/HTML table. The first two figures are the
    # current/prior sales per store; the third is the disclosed SSS growth.
    health = re.search(
        r"FINANCIAL SUMMARY\s*[-–]\s*HEALTH.{0,1200}?"
        r"SAME-STORE SALES\s*\(THOUSANDS OF PS\.\)\s*"
        r"[\d,.]+\s+[\d,.]+\s+(\(?[\d.]+%?\)?)",
        flat,
    )
    if health:
        growth = parse_number(health.group(1))
        if growth is not None:
            emit(found, defs, "femsa_health_sss", current=growth * 0.01,
                 line=health.group(0), tag=STATEMENT_TIER)
    m = re.search(r"VENTAS MISMAS-TIENDAS AUMENTARON UN PROMEDIO DE\s+([\d.]+)%", flat)
    if m:
        emit(found, defs, "femsa_health_sss", current=float(m.group(1)) * 0.01,
             line=m.group(0), tag=STATEMENT_TIER)


def _emit_residuals(defs: dict[str, MetricDef], found: dict[str, MetricRow]) -> None:
    groups = {
        "femsa_other_revenue": (
            "revenue", ("femsa_oxxo_mexico_revenue", "femsa_am_revenue",
                        "femsa_europe_revenue", "femsa_health_revenue"),
            "femsa_kof_revenue"),
        "femsa_other_gp": (
            "femsa_consolidated_gp", ("femsa_oxxo_mexico_gp", "femsa_am_gp",
                                      "femsa_europe_gp", "femsa_health_gp"),
            "femsa_kof_gp"),
        "femsa_other_ebit": (
            "femsa_consolidated_ebit", ("femsa_oxxo_mexico_ebit", "femsa_am_ebit",
                                        "femsa_europe_ebit", "femsa_health_ebit"),
            "femsa_kof_ebit"),
        "femsa_other_ebitda": (
            "ebitda", ("femsa_oxxo_mexico_ebitda", "femsa_am_ebitda",
                       "femsa_europe_ebitda", "femsa_health_ebitda"),
            "femsa_kof_ebitda"),
    }
    for out_key, (total_key, commerce_keys, kof_key) in groups.items():
        required = (total_key, *commerce_keys, kof_key)
        if not all(key in found for key in required):
            continue
        current = found[total_key].current - sum(found[k].current for k in commerce_keys) - found[kof_key].current
        priors = [found[k].prior for k in required]
        prior = (priors[0] - sum(priors[1:-1]) - priors[-1]
                 if all(v is not None for v in priors) else None)
        emit(found, defs, out_key, current=current, prior=prior,
             line=f"reconciled residual: {total_key} - commerce divisions - {kof_key}",
             tag=STATEMENT_TIER)
