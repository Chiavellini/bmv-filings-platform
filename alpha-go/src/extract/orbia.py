"""Deterministic extractor for Orbia (ex-Mexichem) English earnings releases.

Releases print small header-anchored blocks whose header line is
``<Label>  <year1>  <year2>  %Var.`` — the consolidated "Financial Highlights"
(and, 2022+, an identical "Income Statement" table) plus one block per business
group. Header years give column orientation directly (year1 column is current);
Q2–Q4 releases append YTD/FY columns AFTER the quarterly pair, so the first two
values of a row are always (current quarter, prior quarter).

Business-group label eras (block header names):
    2018-1T→2019-4T  Vinyl / Fluent / Fluor    — Mexichem perimeters, NOT
                     mapped: they do not equal the modern five groups.
    2020-1T→2021-4T  Vestolit / Wavin / Netafim / Dura-Line / Koura
                     (Polymer Solutions header appears from 2020-4T)
    2022-1T→2022-2T  Polymer Solutions / Building & Infrastructure /
                     Precision Agriculture / Data Communications /
                     Fluorinated Solutions
    2022-3T→2023-3T  … Data Communications → Connectivity Solutions
    2023-4T→today    … Fluorinated Solutions → Fluor & Energy Materials

Values are US$ millions as printed (company.unit == millions, currency USD).
Rows are tagged ``[statement]`` and carry the prior column so
``apply_restated_priors`` can consume them if ever needed.
"""

from __future__ import annotations

import re

from src.extract.extract_metrics import MetricRow, parse_number
from src.extract.statement_utils import looks_like_year
from src.model.financial_model import MetricDef

# Block-header label → segment key prefix. Consolidated blocks are handled
# separately. Order matters only for documentation; matching is exact per line.
_SEGMENT_LABELS: dict[str, str] = {
    "polymer solutions": "polymer",
    "vestolit": "polymer",
    "building & infrastructure": "building",
    "building and infrastructure": "building",
    "wavin": "building",
    "precision agriculture": "precision_ag",
    "netafim": "precision_ag",
    "connectivity solutions": "connectivity",
    "data communications": "connectivity",
    "dura-line": "connectivity",
    "duraline": "connectivity",
    "fluor & energy materials": "fluor",
    "fluorinated solutions": "fluor",
    "koura": "fluor",
}

_CONSOLIDATED_LABELS = {
    "financial highlights",
    "consolidated financial highlights",   # 2021-1T
    "income statement",
    "selected financial results",          # 2018-era header
}

# Row label regex → metric key, per block kind. First match on a line wins;
# a key already found is not overwritten (Financial Highlights precedes the
# Income Statement table, and both print the same quarter).
_CONSOLIDATED_ROWS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^net (?:sales|revenues?)\b"), "revenue"),
    (re.compile(r"^cost of sales\b"), "cogs"),
    (re.compile(r"^gross profit\b"), "gross_profit"),
    # _norm strips punctuation: "selling general and administrative", "sg a"
    (re.compile(r"^(?:selling,? general and administrative|operating expenses\b|sg\s?&?\s?a\b)"), "operating_expense"),
    (re.compile(r"^operating income\b"), "operating_income"),
    (re.compile(r"^ebitda\b(?! margin)"), "ebitda"),
    (re.compile(r"^financial cost"), "interest_expense"),
    (re.compile(r"^(?:ebt\b|earnings (?:\(loss\) )?before tax(?:es)?|income \(?loss\)? from continuing operations before income tax)"), "ebt"),
    (re.compile(r"^income tax\b"), "tax_expense"),
    (re.compile(r"^(?:net majority (?:\(loss\) )?income|majority net (?:\(loss\) )?income|net (?:income|\(?loss\)?) to majority|net income \(loss\)(?! to))"), "net_income"),
    (re.compile(r"^minority stockholders\b"), "minority_interest"),
    (re.compile(r"^(?:total )?cap\s?ex\b|^capital expenditures?\b"), "capex"),
    (re.compile(r"^free cash (?:flow|inflow|outflow)"), "free_cash_flow"),
    (re.compile(r"^(?:operating cash (?:flow|inflow|outflow)|cash generation)\b"), "cfo"),
    (re.compile(r"^cash (?:balance|and temporary investments|and cash equivalents)\b"), "cash"),
    (re.compile(r"^total debt\b"), "total_debt"),
    (re.compile(r"^net debt\b(?!.?-?to)"), "net_debt"),
]

# Rows printed as cash OUTFLOWS in parens but modeled positive.
_ABS_KEYS = {"capex"}

# Printed leverage ratio, prose variants across eras:
#   "reduces net debt to EBITDA at 1.98x" (2018) / "Leverage ratio (net debt-
#   to-EBITDA) decreased to 1.34x" (2021) / "net debt-to-EBITDA ratio decreased
#   from 3.70x to 3.64x" (2026 — the LAST "to X.XXx" is the current value).
_ND_EBITDA = r"net debt\s*(?:[\-–\s]*to[\-–\s]*|/\s*)ebitda"
_LEVERAGE_PROSE_RES = [
    # table row "Net Debt/EBITDA 12 M   2.31x   2.05x" — 12 M = twelve months,
    # NOT the value (the x suffix is what disambiguates)
    re.compile(_ND_EBITDA + r"(?:\s+12\s*m)?\s+([\d.]+)\s*x\b", re.IGNORECASE),
    # "ratio decreased from 3.70x to 3.64x" — current is the SECOND figure
    re.compile(_ND_EBITDA + r"(?:\s*ratio)?\)?\s*(?:decreased|increased|improved)\s+from\s+[\d.]+\s*x\s+to\s+([\d.]+)\s*x", re.IGNORECASE),
    # gap may contain numbers ("3.70x") but not a sentence end (". " boundary)
    re.compile(_ND_EBITDA + r"(?:\s*ratio)?\)?(?:[^.\n]|\.(?=\d)){0,80}?(?:to|at|of|reached|was)\s+([\d.]+)\s*x", re.IGNORECASE),
    re.compile(r"([\d.]+)\s*x\s+" + _ND_EBITDA, re.IGNORECASE),
]

# 2018-2019 releases carry a "FINANCIAL DEBT" table ("Net Debt USD million
# 2,748  1,356") and a balance row for cash outside any recognized block.
_LOOSE_ROWS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^net debt usd million\b"), "net_debt"),
    (re.compile(r"^cash and temporary investments\b"), "cash"),
]

# Billion-precision debt prose (2018-2020): group(1) value, group(2) unit.
_TOTAL_DEBT_PROSE_RE = re.compile(
    r"total (?:financial )?debt(?: as of [^.]{0,40}?)?\s+(?:was|of|totaled)\s+\$([\d,.]+)\s*(billion|million)",
    re.IGNORECASE,
)
_NET_DEBT_BN_PROSE_RE = re.compile(
    r"net (?:financial )?debt(?: for covenant purposes)?(?: as of [^.]{0,40}?)?\s+(?:was|of|totaled)\s+\$([\d,.]+)\s*(billion|million)",
    re.IGNORECASE,
)

# Leverage decomposition prose ("Net debt of $3,937 million included total debt
# of $4,821 million, less cash and cash equivalents of $884 million") — the only
# place some quarters print cash / total debt.
_NET_DEBT_PROSE_RE = re.compile(
    r"net debt (?:of|was) \$([\d,]+(?:\.\d+)?)\s*(?:million|mm)[^.]{0,120}?"
    r"total debt of \$([\d,]+(?:\.\d+)?)\s*(?:million|mm)[^.]{0,120}?"
    r"cash(?: and cash equivalents| and temporary investments)? of \$([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

_SEGMENT_ROWS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(?:total )?(?:net )?(?:sales|revenues?)\*?\b"), "ns_{g}"),
    (re.compile(r"^ebitda\b(?! margin)"), "ebitda_{g}"),
]

_HEADER_RE = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z&,\-\. ]{2,45}?)\s+(?P<y1>20\d{2})\s+(?P<y2>20\d{2})\s+%(?:\s*var)?",
    re.IGNORECASE,
)

# 2024+ releases print the consolidated block with a bare year header
# ("2024   2023   % Var") — the label lives lines above. Treated as
# consolidated only when the block's first data row is "Net sales".
_BARE_HEADER_RE = re.compile(r"^(?P<y1>20\d{2})\s+(?P<y2>20\d{2})\s+%(?:\s*var)?", re.IGNORECASE)

# Some quarters (e.g. 2023-3T Building & Infrastructure / Connectivity) publish
# a group's figures ONLY in prose under a "<Group> (<Brand>), N% of Revenues"
# heading: "Revenues of $694 million decreased 1%, EBITDA of $79 million…".
_PROSE_HEADING_RE = re.compile(
    r"^(?P<label>[A-Za-z][A-Za-z&\- ]{2,45}?)\s*\([A-Za-z\-,\. ]+\),\s*~?\d+(?:\.\d+)?%\s*of\s+revenues",
    re.IGNORECASE,
)
_PROSE_REV_RE = re.compile(r"\brevenues?\s+of\s+\$([\d,]+(?:\.\d+)?)\s+million", re.IGNORECASE)
_PROSE_EBITDA_RE = re.compile(r"\bebitda\s+of\s+\$([\d,]+(?:\.\d+)?)\s+million", re.IGNORECASE)

# Prose sentences ("Net sales of $1,976 million increased…") must never be read
# as table rows — their trailing numbers are unrelated figures.
_PROSE_LINE_RE = re.compile(r"\bof\s+\$|\bincreased\b|\bdecreased\b|\bcompared\b|\bdriven\b", re.IGNORECASE)

# 2018-era income statement splits the EBT row label across two lines:
#   "Income (loss) from continuing operations before" / <values> / "income tax"
_EBT_SPLIT_RE = re.compile(r"^income \(?loss\)? from continuing operations before$")

_NUM_TOKEN_RE = re.compile(r"\(?-?\$?\d[\d,]*(?:\.\d+)?\)?%?")

_BLOCK_MAX_LINES = 40


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _row_values(line: str, label_end: int) -> list[float]:
    """Non-percent numeric tokens after the row label (bps/percent excluded)."""
    values: list[float] = []
    tail = line[label_end:]
    for m in _NUM_TOKEN_RE.finditer(tail):
        raw = m.group(0)
        after = tail[m.end():m.end() + 4]
        if raw.endswith("%") or after.strip().lower().startswith("bps"):
            continue
        v = parse_number(raw.rstrip("%"))
        if v is None:
            continue
        # Column-header years leak into some rows; values here are US$mn and
        # the header regex already consumed the real year pair.
        if looks_like_year(raw, v, lo=2000):
            continue
        values.append(v)
    return values


def extract_orbia_release(
    text: str,
    metric_defs: list[MetricDef],
    period: str | None = None,
) -> dict[str, MetricRow]:
    defs = {m.key: m for m in metric_defs}
    out: dict[str, MetricRow] = {}
    if not text:
        return out
    lines = [ln.strip() for ln in text.splitlines()]

    i = 0
    while i < len(lines):
        header = _HEADER_RE.match(lines[i])
        header_label = header.group("label") if header else ""
        if not header:
            bare = _BARE_HEADER_RE.match(lines[i])
            if bare:
                nxt = [_norm(x) for x in lines[i + 1:i + 5] if x.strip()]
                if nxt and nxt[0].startswith("net sales"):
                    header, header_label = bare, "Financial Highlights"
        if not header:
            i += 1
            continue
        label = _norm(header_label)
        group = _SEGMENT_LABELS.get(label)
        consolidated = label in _CONSOLIDATED_LABELS
        if group is None and not consolidated:
            i += 1
            continue
        rows = (
            _CONSOLIDATED_ROWS
            if consolidated
            else [(rx, key.format(g=group)) for rx, key in _SEGMENT_ROWS]
        )
        j = i + 1
        end = min(len(lines), i + 1 + _BLOCK_MAX_LINES)
        while j < end:
            if _HEADER_RE.match(lines[j]) or _BARE_HEADER_RE.match(lines[j]):
                break            # next block starts — do not consume its rows
            norm_line = _norm(lines[j])
            if _PROSE_LINE_RE.search(norm_line):
                j += 1
                continue
            if (consolidated and "ebt" not in out and "ebt" in defs
                    and _EBT_SPLIT_RE.match(norm_line) and j + 1 < end):
                values = _row_values(lines[j + 1], 0)
                if len(values) >= 2:
                    out["ebt"] = MetricRow(
                        metric="ebt", label_es=defs["ebt"].label_es,
                        current=round(values[0], 4), prior=round(values[1], 4),
                        var_pct=None, unit=defs["ebt"].unit,
                        source_line=f"[statement] Orbia {header_label.strip()} — {lines[j][:50]} / {lines[j + 1][:30]}",
                    )
                    j += 2
                    continue
            for rx, key in rows:
                m = rx.match(norm_line)
                if not m or key in out or key not in defs:
                    continue
                values = _row_values(lines[j], 0)
                if len(values) < 2 and j + 1 < end:
                    # 2019-era rows wrap the label ("Operating cash flow before
                    # capex, buy-back shares" / values on the next line).
                    nxt = lines[j + 1]
                    if not re.search(r"[a-z]{3,}", _norm(nxt).replace("bps", "")):
                        values = _row_values(nxt, 0)
                if len(values) < 2:
                    continue
                current, prior = values[0], values[1]
                if key in _ABS_KEYS:
                    current, prior = abs(current), abs(prior)
                out[key] = MetricRow(
                    metric=key,
                    label_es=defs[key].label_es,
                    current=round(current, 4),
                    prior=round(prior, 4),
                    var_pct=None,
                    unit=defs[key].unit,
                    source_line=f"[statement] Orbia {header_label.strip()} — {lines[j][:70]}",
                )
                break
            j += 1
        i = j

    # 2018-2019 "FINANCIAL DEBT" table + balance rows outside recognized blocks.
    for rx, key in _LOOSE_ROWS:
        if key in out or key not in defs:
            continue
        for ln in lines:
            if not rx.match(_norm(ln)) or _PROSE_LINE_RE.search(_norm(ln)):
                continue
            values = _row_values(ln, 0)
            if not values:
                continue
            out[key] = MetricRow(
                metric=key, label_es=defs[key].label_es,
                current=round(values[0], 4),
                prior=round(values[1], 4) if len(values) > 1 else None,
                var_pct=None, unit=defs[key].unit,
                source_line=f"[statement] Orbia balance row — {ln.strip()[:60]}",
            )
            break

    # Leverage decomposition prose — fill cash/total_debt/net_debt gaps.
    if not {"cash", "total_debt", "net_debt"} <= out.keys():
        m = _NET_DEBT_PROSE_RE.search(re.sub(r"\s+", " ", text))
        if m:
            for key, raw in (("net_debt", m.group(1)), ("total_debt", m.group(2)), ("cash", m.group(3))):
                if key in out or key not in defs:
                    continue
                v = parse_number(raw)
                if v is None:
                    continue
                out[key] = MetricRow(
                    metric=key, label_es=defs[key].label_es,
                    current=round(v, 4), prior=None, var_pct=None,
                    unit=defs[key].unit,
                    source_line=f"[statement] Orbia net-debt prose — {m.group(0)[:70]}",
                )

    # 2018-2020 prose states debt at billion precision ("total financial debt
    # of $3.6 billion", "Net Debt was $2.9 billion") — last-resort fill.
    if "total_debt" not in out or "net_debt" not in out:
        flat = re.sub(r"\s+", " ", text)
        for key, rx in (("total_debt", _TOTAL_DEBT_PROSE_RE), ("net_debt", _NET_DEBT_BN_PROSE_RE)):
            if key in out or key not in defs:
                continue
            m = rx.search(flat)
            if not m:
                continue
            v = parse_number(m.group(1))
            if v is None:
                continue
            if m.group(2).lower().startswith("billion"):
                v *= 1000.0
            # debt is $bn-scale; a small figure is a delta ("reduction in total
            # net debt of $114 million"), not the level
            if v < 500:
                continue
            out[key] = MetricRow(
                metric=key, label_es=defs[key].label_es,
                current=round(v, 4), prior=None, var_pct=None,
                unit=defs[key].unit,
                source_line=f"[statement] Orbia debt prose — {m.group(0)[:60]}",
            )

    # Printed leverage ratio (calc from quarterly EBITDA would be wrong basis).
    if "net_debt_to_ebitda" not in out and "net_debt_to_ebitda" in defs:
        flat = re.sub(r"\s+", " ", text)
        for rx in _LEVERAGE_PROSE_RES:
            matches = rx.findall(flat)
            if not matches:
                continue
            v = parse_number(matches[-1] if "from" in rx.pattern else matches[0])
            if v is not None and 0 < v < 8:
                out["net_debt_to_ebitda"] = MetricRow(
                    metric="net_debt_to_ebitda", label_es=defs["net_debt_to_ebitda"].label_es,
                    current=round(v, 4), prior=None, var_pct=None,
                    unit=defs["net_debt_to_ebitda"].unit,
                    source_line="[statement] Orbia leverage prose — net debt-to-EBITDA",
                )
                break

    # Minority interest fallback: consolidated − majority when no row printed.
    if "minority_interest" not in out and "minority_interest" in defs and "net_income" in out:
        cons = re.search(
            r"^\s*consolidated net (?:\(loss\) )?income(?: \(loss\))?\s+(\(?-?[\d,]+(?:\.\d+)?\)?)\s+[\d(]",
            text, re.IGNORECASE | re.MULTILINE)
        if cons:
            cv = parse_number(cons.group(1))
            if cv is not None:
                out["minority_interest"] = MetricRow(
                    metric="minority_interest", label_es=defs["minority_interest"].label_es,
                    current=round(cv - out["net_income"].current, 4), prior=None, var_pct=None,
                    unit=defs["minority_interest"].unit,
                    source_line="[statement] Orbia consolidated − majority net income",
                )

    # Prose fallback: fill segment keys whose table is absent this quarter.
    for idx, line in enumerate(lines):
        m = _PROSE_HEADING_RE.match(_norm(line))
        if not m:
            continue
        group = _SEGMENT_LABELS.get(_norm(m.group("label")))
        if group is None:
            continue
        window = " ".join(lines[idx + 1:idx + 14])
        for rx, key_tpl in ((_PROSE_REV_RE, "ns_{g}"), (_PROSE_EBITDA_RE, "ebitda_{g}")):
            key = key_tpl.format(g=group)
            if key in out or key not in defs:
                continue
            vm = rx.search(window)
            if not vm:
                continue
            value = parse_number(vm.group(1))
            if value is None:
                continue
            out[key] = MetricRow(
                metric=key,
                label_es=defs[key].label_es,
                current=round(value, 4),
                prior=None,
                var_pct=None,
                unit=defs[key].unit,
                source_line=f"[statement] Orbia prose {line[:60]} — {vm.group(0)[:50]}",
            )
    return out
