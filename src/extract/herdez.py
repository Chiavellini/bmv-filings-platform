"""
herdez.py — custom cascade extractor for Grupo Herdez quarterly reports.

Ported from the standalone archive extractor (archive/herdez_reportes/extract_herdez.py),
which is battle-tested across 2016–2026 reports for the hard parts: narrative-vs-table
rejection, ES/EN dual format, annual-vs-quarterly disambiguation, and the 2025 layout
shift (Conservas/Impulso → Domestic/Export segments). Here the file-loop + CSV writer are
replaced by ``extract_herdez(text, metric_defs, period) -> {canonical_key: MetricRow}`` so
the cascade can merge Herdez segment/equity/MegaMex facts at the [statement] tier.

Segment naming: Herdez reported Conservas/Impulso/Exportación through 4Q24 and switched to
Nacional (Domestic)/Exportación (Export) from 2025. The archive 'nacional' key == Domestic;
'conservas'/'impulso' are the legacy (≤4Q24) breakdown — both are surfaced.
"""
from __future__ import annotations

import re
import statistics  # noqa: F401  (kept for parity with archive helpers)

from src.extract.extract_metrics import MetricRow
from src.model.financial_model import MetricDef


def parse_num(s: str) -> float | None:
    """Parse a single numeric token string: (123) / -45 / 1,234.5 / NM → None."""
    s = s.strip()
    if not s or s in ('-', '—', 'NM', 'nm', 'N/A', 'n/a'):
        return None
    negative = s.startswith('(') and s.endswith(')')
    s = s.strip('()').replace(',', '').replace(' ', '')
    if not s:
        return None
    try:
        v = float(s)
        return -v if negative else v
    except ValueError:
        return None


def row_numbers(line: str) -> list[float]:
    """Return all financial numbers from a line using whitespace tokenization.

    Each whitespace-delimited token is accepted only if it contains NO letters
    (so "2Q18", "NM", "3Q22" are silently dropped).
    """
    result = []
    for tok in line.split():
        if re.search(r'[A-Za-z]', tok):
            continue
        v = parse_num(tok)
        if v is not None:
            result.append(v)
    return result


def first_num(line: str) -> float | None:
    nums = row_numbers(line)
    return nums[0] if nums else None


# ---------------------------------------------------------------------------
# Row label matching — alias must appear at START of the stripped line
# ---------------------------------------------------------------------------

def _compile_aliases(aliases: list[str]) -> list[re.Pattern]:
    return [re.compile(rf'(?:{a})\b', re.IGNORECASE) for a in aliases]


def matches_row_start(line: str, compiled_aliases: list[re.Pattern]) -> bool:
    """True iff any alias matches at the very start of the stripped line."""
    stripped = line.lstrip()
    for pat in compiled_aliases:
        if pat.match(stripped):
            return True
    return False


# ---------------------------------------------------------------------------
# Segment alias definitions
# ---------------------------------------------------------------------------

# Row aliases per canonical key  (compiled separately)
_ROW_DEFS: dict[str, list[str]] = {
    # 'consolidated' is the row label in English-format tables; Spanish tables embed the value on the header line
    'consolidated': [r'Consolidated\b'],
    'conservas':    [r'Conservas\b', r'Preserves\b', r'Preservas\b', r'Mexico\s+Core\b'],
    'impulso':      [r'Impulso\b', r'Impulse\b', r'Frozen\b', r'Congelados\b'],
    'exports':      [r'Exportaciones\b', r'Exportaci[oó]n\b', r'Exports\b', r'Export\s+Sales\b', r'Foreign\s+Sales\b'],
    'nacional':     [r'Nacional\b'],
    'eliminations': [r'Eliminaciones\b', r'Eliminations\b', r'Rounding\b'],
}

_ROW_PATS: dict[str, list[re.Pattern]] = {
    k: _compile_aliases(v) for k, v in _ROW_DEFS.items()
}

_EQUITY_DEFS: dict[str, list[str]] = {
    'equity_total':     [r'Consolidated\b', r'Total\b'],
    'equity_megamex':   [r'MegaMex\b', r'Mega\s*Mex\b'],
    'equity_mccormick': [r'McCormick\b'],
    'equity_others':    [r'Others?\b', r'Otras?\b'],
}
_EQUITY_PATS = {k: _compile_aliases(v) for k, v in _EQUITY_DEFS.items()}

_MEGAMEX_DEFS: dict[str, list[str]] = {
    'megamex_net_sales':    [r'Net\s+Sales\b', r'Ventas\s+Netas\b'],
    'megamex_gross_profit': [r'Gross\s+[Pp]rofit\b', r'Utilidad\s+Bruta\b'],
    'megamex_ebit':         [r'EBIT\b(?!DA)', r'Utilidad\s+de\s+[Oo]peraci'],
    'megamex_ebitda':       [r'EBITDA\b', r'UAFIDA\b'],
    'megamex_net_income':   [r'Net\s+Income\b', r'Utilidad\s+Neta\b'],
}
_MEGAMEX_PATS = {k: _compile_aliases(v) for k, v in _MEGAMEX_DEFS.items()}

# Section headers used to locate each metric block
_NET_SALES_HDR  = re.compile(
    r'(?:Ventas\s+Netas|NET\s+SALES|Total\s+(?:de\s+)?(?:ventas|sales))', re.IGNORECASE)
_GP_HDR         = re.compile(
    r'(?:Utilidad\s+Bruta|GROSS\s+PROFIT|Gross\s+Profit)\b', re.IGNORECASE)
_EBIT_HDR       = re.compile(
    # standalone "EBIT" must be followed by column-header digits or EOL (not narrative words)
    r'(?:Utilidad\s+de\s+operaci[oó]n|EARNINGS\s+BEFORE\s+INTEREST'
    r'|EBIT\b(?!DA)(?:\s+\d|\s*$|\s+[(\[]))',
    re.IGNORECASE)
_EBITDA_HDR     = re.compile(r'(?:UAFIDA|EBITDA|UAFIRA)\b', re.IGNORECASE)
_EQUITY_HDR     = re.compile(
    r'(?:EQUITY\s+INVEST|Participaci[oó]n\s+en\s+los\s+Resultados|Inversi[oó]n\s+en\s+asociadas)',
    re.IGNORECASE)
_MEGAMEX_HDR    = re.compile(
    r'(?:MEGAMEX\s+CONSOLIDATED|MEGAMEX\s+INCOME|MEGAMEX\s+RESULTADOS|ESTADO\s+DE\s+RESULTADOS\s+MEGAMEX)',
    re.IGNORECASE)

# Lines that signal the END of a segment table (must appear at line start + have big numbers)
_STOP_STARTS = re.compile(
    r'(?:Costo\s+de\s+Ventas|Cost\s+of\s+(?:sales|goods)\b|COGS\b'
    r'|Utilidad\s+Bruta\b|Gross\s+(?:Profit|Margin)\b'
    r'|Gastos\s+Generales\b|SG&A\b|Operating\s+Expenses\b'
    r'|Utilidad\s+(?:antes|de\s+operaci)'
    r'|EARNINGS\s+BEFORE\s+INTEREST'
    r'|UAFIDA\b|EBITDA\b'
    r'|Participaci[oó]n\s+en\s+los'
    r'|MegaMex\s+(?:Result|Consol)'
    r'|GROSS\s+PROFIT\b|GROSS\s+MARGIN\b'
    r'|NET\s+INCOME\b|Cifras|Figur)',
    re.IGNORECASE)


def _is_stop_line(line: str) -> bool:
    stripped = line.lstrip()
    if not _STOP_STARTS.match(stripped):
        return False
    big_nums = [v for v in row_numbers(line) if abs(v) >= 50]
    return bool(big_nums)


# Stacked column-header filler between a table header and its first data row
# ("%", "change", "var."), produced by three-line headers in the 2024+ layout.
_COLUMN_FILLER_RE = re.compile(r'^\s*(?:%|change|var(?:iaci[oó]n)?\.?|Δ)\s*$', re.IGNORECASE)

# Cumulative companion tables ("9M24", "6M23") that follow the quarterly table in
# press-release pages. Kept separate from _ANNUAL_TABLE_RE: appendix statement
# headers legitimately carry 9M column labels next to the quarterly columns.
_CUMULATIVE_HDR_RE = re.compile(r'\b[69]M\d{2,4}\b')


def _is_year(v: float) -> bool:
    return v == int(v) and 1990 <= int(v) <= 2030


def _big_financial_nums(line: str, threshold: float = 100.0) -> list[float]:
    """Numbers ≥ threshold that are not calendar years."""
    return [v for v in row_numbers(line) if abs(v) >= threshold and not _is_year(v)]


def _has_table_data_on_next_line(lines: list[str], header_idx: int, all_row_pats) -> bool:
    """Check whether the first non-blank line after header_idx looks like table data.

    True if:
    - a known segment alias appears at line-start AND there are big non-year numbers, or
    - the line starts with a digit/( (unlabeled data row) AND has big non-year numbers.

    Stacked column headers ("NET SALES 4Q24 4Q23" with "%" above and "change" below)
    put filler between the header and the first data row, and the two-column page
    layout can interleave unrelated narrative lines — skip both. An alias row
    WITHOUT numbers stays a hard reject (narrative about the metric itself).
    """
    allow_unlabeled = True  # unlabeled rows only confirm as the FIRST real candidate
    for j in range(header_idx + 1, min(header_idx + 8, len(lines))):
        candidate = lines[j]
        if not candidate.strip():
            continue
        if _COLUMN_FILLER_RE.match(candidate):
            continue
        # Labeled row: alias at start + big number
        for pats in all_row_pats:
            if matches_row_start(candidate, pats):
                if _big_financial_nums(candidate):
                    return True
                return False  # alias matched but no big num → narrative line (e.g. "Consolidated margin reached 40%")
        # No alias matched — check unlabeled numeric row (e.g. 2025 EBIT continuation).
        # Restricted to the first candidate so bar-chart number stacks under a
        # section title ("9,809" / "9,297 9,345") can never confirm a table.
        stripped = candidate.lstrip()
        if allow_unlabeled and stripped and (stripped[0].isdigit() or stripped[0] == '('):
            if len(_big_financial_nums(candidate)) >= 2:
                return True
        allow_unlabeled = False
        # Neutral narrative interleave from the two-column layout — keep looking
    return False


def _narrative_suffix(content: str, header_re: re.Pattern) -> bool:
    """Return True if the text after the header match starts with a letter word.

    Detects narrative sentences like "Net sales in the quarter increased 10.4%..."
    vs. legitimate table headers like "NET SALES  1Q20  1Q19" or "Ventas Netas  9,281".
    """
    m = header_re.match(content)
    if not m:
        return False
    rest = content[m.end():].lstrip()
    if not rest or not rest[0].isalpha():
        return False
    # Quarter/period tokens like "Q19", "T21" after a partial digit match are not narrative words
    # e.g. "EBIT  1Q19  1Q18  % change" → after matching "EBIT  1", rest="Q19…" → allow
    if re.match(r'^[QT]\d', rest, re.IGNORECASE):
        return False
    return True


def _label_has_narrative_suffix(line: str, pats: list[re.Pattern]) -> bool:
    """True if the row label is followed by letter words (narrative, not a table row).

    Example: "Consolidated gross margin for the quarter was 36.2%, 130 basis points lower"
    has "Consolidated" at start but is NOT a data row — "gross" follows immediately.
    """
    stripped = line.lstrip()
    for pat in pats:
        m = pat.match(stripped)
        if m:
            rest = stripped[m.end():].lstrip()
            return bool(rest) and rest[0].isalpha()
    return False


_ANNUAL_TABLE_RE = re.compile(
    r'\b12M\d{2,4}\b'          # "12M23", "12M2024"
    r'|\b12\s*[Mm]eses?\b'     # "12 meses"
    r'|\bFull[.\s-]?Year\b'    # "Full Year", "Full-Year"
    r'|\bAnual\b',              # Spanish "Anual"
    re.IGNORECASE)
_ANNUAL_YEAR_ONLY_RE = re.compile(r'^\s*20\d{2}\s*$')  # line with just "2023"


def _is_annual_table_context(lines: list[str], header_idx: int, lookback: int = 5) -> bool:
    """Return True if this section header likely belongs to a full-year (12-month) table.

    Detected by "12M23", "12 meses", "Full Year", or a standalone year-only label
    appearing in the few lines immediately around the header.
    """
    for j in range(max(0, header_idx - lookback), min(header_idx + 2, len(lines))):
        line = lines[j]
        if _ANNUAL_TABLE_RE.search(line):
            return True
        if _ANNUAL_YEAR_ONLY_RE.match(line):
            return True
    return False


# ---------------------------------------------------------------------------
# Core table extractor
# ---------------------------------------------------------------------------

_LEADING_NON_WORD = re.compile(r'^[^\w(]+')

# Broader MegaMex section marker (also catches column-header lines like "MEGAMEX  change  %")
_MEGAMEX_SECTION = re.compile(r'MEGAMEX', re.IGNORECASE)
# Detects annual-only MegaMex sections (Q4 reports without a quarterly breakdown)
_MEGAMEX_ANNUAL_RE = re.compile(r'\bfor\s+the\s+year\b', re.IGNORECASE)


def _in_megamex_section(lines: list[str], idx: int, lookback: int = 50) -> bool:
    """Return True if idx is within a MegaMex standalone results section."""
    start = max(0, idx - lookback)
    for j in range(idx - 1, start - 1, -1):
        if _MEGAMEX_HDR.search(lines[j]):
            return True
    return False


def extract_segment_table(
    lines: list[str],
    *,
    header_re: re.Pattern,
    row_pats: dict[str, list[re.Pattern]],
    window: int = 25,
) -> dict[str, float | None]:
    """Find section headers and extract labeled segment rows under them.

    Every plausible table occurrence is collected, then the MOST COMPLETE one wins
    (appendix statement tables carry intact quarterly digits; the press-release
    tables in the 2024+ layout often have digit-destroyed cells and stray "Net
    sales" currency-block titles match the header too). Remaining unset keys are
    supplemented from the other occurrences, earliest first.
    """
    all_pats = list(row_pats.values())
    candidates: list[tuple[int, dict[str, float]]] = []

    for i, line in enumerate(lines):
        # Quick search to avoid processing every line with match()
        if not header_re.search(line):
            continue

        # Require header label at the START of the line content
        # (strips leading whitespace then leading non-word/non-( chars like bullet symbols)
        content = _LEADING_NON_WORD.sub('', line.lstrip())
        if not header_re.match(content):
            continue  # header appears mid-sentence (e.g. "Consolidated net sales grew...")

        # Reject narrative sentences: "Net sales in the quarter increased..." has a letter word
        # right after the metric name — real table headers have numbers or end-of-line next.
        if _narrative_suffix(content, header_re):
            continue

        # Skip rows that appear inside the MegaMex standalone results section
        if _in_megamex_section(lines, i):
            continue

        # Skip annual (12-month) summary tables — quarterly reports want standalone quarter values
        if _is_annual_table_context(lines, i):
            continue

        # Skip cumulative companion tables ("NET SALES 9M24 9M23")
        if _CUMULATIVE_HDR_RE.search(line):
            continue

        res: dict[str, float] = {}
        nums_on_header = row_numbers(line)
        # Spanish format: header line has a big non-year financial value inline
        # (e.g. "Ventas Netas  9,281  100.0  ...").  Year-only column headers like
        # "NET SALES  2017  2016  % Change" are treated as English-format tables.
        big_inline = [v for v in nums_on_header if abs(v) >= 200 and not _is_year(v)]
        if big_inline:
            res['consolidated'] = big_inline[0]
        else:
            # English format (no big inline value): confirm the next non-blank line is a data row
            if not _has_table_data_on_next_line(lines, i, all_pats):
                continue  # standalone section title with narrative after it

        # Scan forward for segment rows.
        for j in range(i + 1, min(i + 1 + window, len(lines))):
            seg = lines[j]
            if not seg.strip():
                continue
            if _ANNUAL_TABLE_RE.search(seg) or _CUMULATIVE_HDR_RE.search(seg):
                break  # 12M/9M/6M companion table begins — quarterly rows end here
            if _is_stop_line(seg):
                break
            matched_seg = False
            for key, pats in row_pats.items():
                if key == 'consolidated':
                    continue
                if matches_row_start(seg, pats):
                    matched_seg = True
                    if _label_has_narrative_suffix(seg, pats):
                        break  # alias matched but narrative follows — skip
                    v = first_num(seg)
                    if v is not None and key not in res:
                        res[key] = v
                    break
            if not matched_seg:
                # Try English-format labeled consolidated row
                cons_pats = row_pats.get('consolidated', [])
                if 'consolidated' not in res and matches_row_start(seg, cons_pats):
                    if not _label_has_narrative_suffix(seg, cons_pats):
                        v = first_num(seg)
                        if v is not None:
                            res['consolidated'] = v
                # Try unlabeled number row (2025 two-line headers like "Utilidad de operación")
                elif 'consolidated' not in res:
                    seg_stripped = seg.lstrip()
                    if seg_stripped and (seg_stripped[0].isdigit() or seg_stripped[0] == '('):
                        big = _big_financial_nums(seg, threshold=50.0)
                        if big:
                            res['consolidated'] = big[0]

        if res:
            candidates.append((i, res))

    # Most complete table first; ties resolved by document order. Then supplement
    # still-missing keys from the remaining occurrences.
    candidates.sort(key=lambda t: (-len(t[1]), t[0]))
    result: dict[str, float] = {}
    for _, res in candidates:
        for k, v in res.items():
            result.setdefault(k, v)

    return {k: result.get(k) for k in row_pats}


# ---------------------------------------------------------------------------
# Equity in associates — handled separately (unlabeled total row in 2025)
# ---------------------------------------------------------------------------

def _equity_has_table_data(lines: list[str], header_idx: int, max_nonblank: int = 10) -> bool:
    """Look ahead up to max_nonblank non-blank lines for equity table data.

    Returns True if we find a row starting with a known equity alias AND big numbers,
    OR an unlabeled digit-start line with big numbers (≥10).
    """
    nonblank = 0
    for j in range(header_idx + 1, min(header_idx + 50, len(lines))):
        seg = lines[j]
        if not seg.strip():
            continue
        nonblank += 1
        if nonblank > max_nonblank:
            break
        for pats in _EQUITY_PATS.values():
            if matches_row_start(seg, pats):
                if [v for v in row_numbers(seg) if abs(v) >= 10]:
                    return True
                break
        else:
            stripped = seg.lstrip()
            if stripped and (stripped[0].isdigit() or stripped[0] == '('):
                if [v for v in row_numbers(seg) if abs(v) >= 10]:
                    return True
    return False


def _extract_equity(lines: list[str]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}

    for i, line in enumerate(lines):
        if not _EQUITY_HDR.search(line):
            continue

        # Require equity header at the very start of line content (no mid-sentence matches)
        content = _LEADING_NON_WORD.sub('', line.lstrip())
        if not _EQUITY_HDR.match(content):
            continue

        # A wrapped boilerplate sentence can put "Equity Investments in Associated
        # Companies." at line start; real section headers never end with a period.
        if content.rstrip().endswith('.'):
            continue

        # Confirm a real table follows within the next 10 non-blank lines.
        # This rejects narrative section titles far from any actual data.
        if not _equity_has_table_data(lines, i):
            continue

        # Check for total value on header line itself.
        nums = row_numbers(line)
        if nums and abs(nums[0]) >= 10:
            result['equity_total'] = nums[0]

        # Scan next 25 lines for labeled/unlabeled equity rows.
        for j in range(i + 1, min(i + 26, len(lines))):
            seg = lines[j]
            if not seg.strip():
                continue
            if _is_stop_line(seg):
                break
            # Try labeled rows.
            for key, pats in _EQUITY_PATS.items():
                if matches_row_start(seg, pats):
                    v = first_num(seg)
                    if v is not None and key not in result:
                        result[key] = v
                    break
            else:
                # Unlabeled digit-start line of numbers → equity total (2025 format).
                # Require digit/( start to avoid extracting numbers from narrative sentences.
                if 'equity_total' not in result:
                    seg_stripped = seg.lstrip()
                    if seg_stripped and (seg_stripped[0].isdigit() or seg_stripped[0] == '('):
                        seg_nums = [v for v in row_numbers(seg) if abs(v) >= 10 and not _is_year(v)]
                        if len(seg_nums) >= 2:
                            result['equity_total'] = seg_nums[0]

        break

    return {
        'equity_total':     result.get('equity_total'),
        'equity_megamex':   result.get('equity_megamex'),
        'equity_mccormick': result.get('equity_mccormick'),
        'equity_others':    result.get('equity_others'),
    }


# ---------------------------------------------------------------------------
# MegaMex standalone extraction
# ---------------------------------------------------------------------------

def _extract_megamex(lines: list[str], text: str) -> dict[str, float | None]:
    result: dict[str, float | None] = {}

    # Try clean table first (2016-2024 English reports).
    for i, line in enumerate(lines):
        if not _MEGAMEX_HDR.search(line):
            continue
        # Skip annual-only sections (Q4 reports where MegaMex shows full-year data only)
        if any(_MEGAMEX_ANNUAL_RE.search(lines[k]) for k in range(i, min(i + 5, len(lines)))):
            continue
        for j in range(i + 1, min(i + 50, len(lines))):
            seg = lines[j]
            if not seg.strip():
                continue
            for key, pats in _MEGAMEX_PATS.items():
                if matches_row_start(seg, pats):
                    if _label_has_narrative_suffix(seg, pats):
                        break  # narrative sentence, not a table row
                    # Require ≥50 to skip narrative prose values like "3.4 billion" or "19.4 percent"
                    big = _big_financial_nums(seg, threshold=50.0)
                    if big and key not in result:
                        result[key] = big[0]
                    break
        if result:
            break

    # Prose fallback for MegaMex Net Sales (2025 Spanish narrative reports).
    if 'megamex_net_sales' not in result:
        for pat in [
            r'MegaMex[^.]*?alcanzaron?\s+\$\s*([\d,]+)',
            r'MegaMex[^.]*?net\s+sales[^.]*?MXN\s+([\d,]+)',
            r'ventas\s+netas[^.]*?MegaMex[^.]*?([\d,]+)\s*millon',
        ]:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                v = parse_num(m.group(1))
                if v is not None and v >= 100:  # reject "3.4 billion" captured as "3"
                    result['megamex_net_sales'] = v
                    break

    return {
        'megamex_net_sales':    result.get('megamex_net_sales'),
        'megamex_gross_profit': result.get('megamex_gross_profit'),
        'megamex_ebit':         result.get('megamex_ebit'),
        'megamex_ebitda':       result.get('megamex_ebitda'),
        'megamex_net_income':   result.get('megamex_net_income'),
    }


# ---------------------------------------------------------------------------
# Prose KPI extraction
# ---------------------------------------------------------------------------

_PROSE_PATTERNS: dict[str, list[str]] = {
    'plants': [
        r'(\d+)\s+plantas?\s+de\s+producci[oó]n',
        r'(\d+)\s+production\s+plants?',
        r'has\s+(\d+)\s+plants?(?:\s*,|\s+\d)',  # "has 15 plants, 9 dist..." (2016-2019 boilerplate)
    ],
    'dist_centers': [
        r'(\d+)\s+centros?\s+de\s+distribuci[oó]n',
        r'(\d+)\s+distribution\s+cent',
    ],
}


def _extract_prose_kpis(text: str) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for key, patterns in _PROSE_PATTERNS.items():
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                v = parse_num(m.group(1))
                if v is not None:
                    result[key] = v
                    break
    return {k: result.get(k) for k in _PROSE_PATTERNS}



# ---------------------------------------------------------------------------
# Archive-key -> canonical-outline-key mapping  (+ unit per canonical key)
# ---------------------------------------------------------------------------
# Consolidated + segment monetary lines are MXN millions as reported; plants/DCs are counts.
_KEY_MAP: dict[str, str] = {
    # consolidated
    "net_sales":            "revenue",
    "gross_profit":         "gross_profit",
    "ebit":                 "operating_income",
    "ebitda":               "ebitda",
    # net sales segments  (nacional == Domestic)
    "ns_nacional":          "ns_domestic",
    "ns_exports":           "ns_export",
    "ns_elim":              "ns_elim",
    "ns_conservas":         "ns_conservas",   # legacy ≤4Q24
    "ns_impulso":           "ns_impulso",     # legacy ≤4Q24
    # gross profit segments
    "gp_nacional":          "gp_domestic",
    "gp_exports":           "gp_export",
    "gp_elim":              "gp_elim",
    "gp_conservas":         "gp_conservas",
    "gp_impulso":           "gp_impulso",
    # EBIT segments
    "ebit_nacional":        "ebit_domestic",
    "ebit_exports":         "ebit_export",
    "ebit_elim":            "ebit_elim",
    "ebit_conservas":       "ebit_conservas",
    "ebit_impulso":         "ebit_impulso",
    # EBITDA segments
    "ebitda_nacional":      "ebitda_domestic",
    "ebitda_exports":       "ebitda_export",
    "ebitda_elim":          "ebitda_elim",
    "ebitda_conservas":     "ebitda_conservas",
    "ebitda_impulso":       "ebitda_impulso",
    # equity in associates
    "equity_total":         "equity_associates",
    "equity_megamex":       "equity_megamex",
    "equity_mccormick":     "equity_mccormick",
    "equity_others":        "equity_others",
    # MegaMex standalone (100% of JV)
    "megamex_net_sales":    "megamex_net_sales",
    "megamex_gross_profit": "megamex_gross_profit",
    "megamex_ebit":         "megamex_ebit",
    "megamex_ebitda":       "megamex_ebitda",
    "megamex_net_income":   "megamex_net_income",
    # operating KPIs
    "plants":               "plants",
    "dist_centers":         "dist_centers",
}

_COUNT_KEYS = {"plants", "dist_centers"}


# From 2026 Herdez abandons the segment TABLE for Spanish bullet prose
# ("Nacional: Las ventas netas del segmento ascendieron a $4,815 millones"). The
# archive table engine returns nothing for these, so the cascade would fall to the
# regulatory [bmv] "Ingresos" line — a DIFFERENT basis (IFRS total revenue in full
# pesos) than the management "ventas netas" the rest of the series uses. These patterns
# recover the management-basis figures (in MXN millions) directly from the prose.
_RE_2026 = {
    "net_sales":         r'ventas\s+netas\s+ascendieron\s+a\s+\$?\s*([\d,]+)\s*millones',
    "gross_profit":      r'utilidad\s+bruta[^.]{0,60}?(?:alcanz[oó]|totaliz[oó])\s+\$?\s*([\d,]+)\s*millones',
    "equity_total":      r'participaci[oó]n\s+en\s+los\s+resultados\s+de\s+asociadas[^.]{0,60}?totaliz[oó]\s+\$?\s*([\d,]+)\s*millones',
    "equity_megamex":    r'MegaMex:\s*La\s+participaci[oó]n\s+se\s+situ[oó]\s+en\s+\$?\s*([\d,]+)\s*millones',
    "megamex_net_sales": r'ventas\s+netas\s+de\s+MegaMex\s+alcanzaron\s+\$?\s*([\d,]+)\s*millones',
    "ns_nacional":       r'Nacional:\s*Las\s+ventas\s+netas\s+del\s+segmento\s+ascendieron\s+a\s+\$?\s*([\d,]+)\s*millones',
    "ns_exports":        r'Exportaci[oó]n:\s*Las\s+ventas\s+netas\s+fueron\s+de\s+\$?\s*([\d,]+)\s*millones',
    "ebit_nacional":     r'Nacional:\s*La\s+utilidad\s+de\s+operaci[oó]n\s+alcanz[oó]\s+\$?\s*([\d,]+)\s*millones',
    "ebitda_nacional":   r'Nacional:\s*Report[oó]\s+una\s+UAFIDA\s+de\s+\$?\s*([\d,]+)\s*millones',
    "ebitda_exports":    r'Exportaci[oó]n:\s*El\s+flujo\s+operativo\s+registr[oó]\s+\$?\s*([\d,]+)\s*millones',
}
# Consolidated operating income + EBITDA are stated together: "La utilidad de operación
# y la UAFIDA … a $620 millones y $810 millones".
_RE_2026_OI_EBITDA = re.compile(
    r'utilidad\s+de\s+operaci[oó]n\s+y\s+la\s+UAFIDA[^.]*?a\s+\$?\s*([\d,]+)\s*millones\s+y\s+\$?\s*([\d,]+)\s*millones',
    re.IGNORECASE)
_RE_2026_EBITDA_FALLBACK = re.compile(r'UAFIDA\s+totaliz[oó]\s+\$?\s*([\d,]+)\s*millones', re.IGNORECASE)
# Export operating result may be a loss ("reportó una pérdida de operación de $6 millones").
_RE_2026_EBIT_EXPORT_LOSS = re.compile(
    r'Exportaci[oó]n:\s*Este\s+segmento\s+report[oó]\s+una\s+p[ée]rdida\s+de\s+operaci[oó]n\s+de\s+\$?\s*([\d,]+)\s*millones',
    re.IGNORECASE)


def _extract_2026_prose(text: str) -> dict[str, float]:
    """Recover management-basis figures (MXN millions) from the 2026 Spanish bullet format."""
    out: dict[str, float] = {}
    for key, pat in _RE_2026.items():
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            v = parse_num(m.group(1))
            if v is not None:
                out[key] = v
    m = _RE_2026_OI_EBITDA.search(text)
    if m:
        oi, eb = parse_num(m.group(1)), parse_num(m.group(2))
        if oi is not None:
            out["ebit"] = oi
        if eb is not None:
            out["ebitda"] = eb
    if "ebitda" not in out:
        m = _RE_2026_EBITDA_FALLBACK.search(text)
        if m and (v := parse_num(m.group(1))) is not None:
            out["ebitda"] = v
    m = _RE_2026_EBIT_EXPORT_LOSS.search(text)
    if m and (v := parse_num(m.group(1))) is not None:
        out["ebit_exports"] = -v
    return out


def extract_herdez(text: str, metric_defs: list[MetricDef], period: str | None = None,
                   pdf_path=None) -> dict[str, MetricRow]:
    """Run the Herdez segment/equity/MegaMex/KPI engine on one report's markdown.

    Returns ``{canonical_key: MetricRow}`` for every archive field that resolved to a
    value, mapped to the outline's canonical keys. Source tagged ``[statement]`` so the
    cascade prefers it over generic prose/table tiers (see tier_precedence in the config).
    """
    lines = text.splitlines()
    raw: dict[str, float | None] = {}

    # Four income-statement metric blocks (consolidated + per-segment rows).
    for metric, col_pfx, hdr in _METRIC_HDRS:
        seg = extract_segment_table(lines, header_re=hdr, row_pats=_ROW_PATS)
        raw[metric]                 = seg.get("consolidated")
        raw[f"{col_pfx}_conservas"] = seg.get("conservas")
        raw[f"{col_pfx}_impulso"]   = seg.get("impulso")
        raw[f"{col_pfx}_exports"]   = seg.get("exports")
        raw[f"{col_pfx}_nacional"]  = seg.get("nacional")
        raw[f"{col_pfx}_elim"]      = seg.get("eliminations")

    raw.update(_extract_equity(lines))
    raw.update(_extract_megamex(lines, text))
    raw.update(_extract_prose_kpis(text))

    # 2026+ bullet-prose format. The "Nacional:" segment bullet is the unambiguous
    # marker of that layout; when present, its management-basis prose figures OVERRIDE
    # the table engine (which otherwise grabs the regulatory IFRS full-pesos lines from
    # the 2026 statement, a different basis/scale). For non-2026 reports the patterns
    # don't match, so this only ever fills genuine gaps.
    prose2026 = _extract_2026_prose(text)
    is_2026 = "ns_nacional" in prose2026
    for key, val in prose2026.items():
        if is_2026 or raw.get(key) is None:
            raw[key] = val

    # Reconstruct the Domestic segment across the labeling eras. Herdez disclosed
    # Conservas+Impulso (≤2024) → Preserves (2025, archive maps Preserves→conservas)
    # → Nacional (2026+). Domestic = the explicit Nacional line if present, else the
    # sum of the two domestic sub-segments, else the single Preserves/Conservas line.
    for pfx in ("ns", "gp", "ebit", "ebitda"):
        nac = raw.get(f"{pfx}_nacional")
        con = raw.get(f"{pfx}_conservas")
        imp = raw.get(f"{pfx}_impulso")
        if nac is not None:
            domestic = nac
        elif con is not None and imp is not None:
            domestic = con + imp
        elif con is not None:
            domestic = con
        else:
            domestic = None
        raw[f"{pfx}_nacional"] = domestic   # _KEY_MAP routes *_nacional → *_domestic

    out: dict[str, MetricRow] = {}
    for akey, val in raw.items():
        if val is None:
            continue
        ckey = _KEY_MAP.get(akey)
        if ckey is None:
            continue
        unit = "count" if ckey in _COUNT_KEYS else "currency"
        out[ckey] = MetricRow(
            metric=ckey, label_es=akey, current=float(val), prior=None,
            var_pct=None, unit=unit, source_line="[statement] herdez segment table",
        )
    return out


# Header table needed by extract_herdez (consolidated + segment prefixes).
_METRIC_HDRS = [
    ("net_sales",    "ns",     _NET_SALES_HDR),
    ("gross_profit", "gp",     _GP_HDR),
    ("ebit",         "ebit",   _EBIT_HDR),
    ("ebitda",       "ebitda", _EBITDA_HDR),
]
