"""Tiendas 3B (BBB Foods Inc., NYSE: TBBB) quarterly earnings-release extractor.

TBBB is the only company on the panel that is NOT a BMV filer: it listed on the
NYSE in February 2024, files nothing with CNBV, and its 6-K filings carry no
XBRL.  The English earnings release furnished with each 6-K is therefore the
ONLY quarterly source that exists for this company, so this reader has to be
right about a document that nothing else can cross-check.

Three properties of that document drive the design:

* **It is cell-per-line.**  The corpus is built by ``edgar.html_to_text``, which
  puts every HTML text node on its own line, so a statement row arrives as a
  label line followed by its value cells.  Values are read forward from the
  label rather than off the label line (which is how the BMV extractors work).

* **Every figure is printed twice — quarter and year-to-date.**  The release
  carries a "For the Three Months Ended" income statement and a "For the Six
  Months Ended" one with identical labels.  Reading the wrong block silently
  yields a YTD number that is plausible, roughly double, and wrong.  The
  quarterly window is therefore bounded explicitly by the next cumulative
  anchor, never by a fixed line count.

* **Two scales coexist.**  The front "Consolidated Results" summary is in
  millions ROUNDED to whole pesos; the statements at the back are in thousands.
  The model tracks the thousands figures — its 2Q25 operating income is
  393.09299, the statement's ``393,093`` and not the summary's ``393`` — so
  everything monetary here is read from the statements and scaled thousands ->
  millions to match ``company.unit``.

EBITDA is the one metric where the model does arithmetic of its own, and getting
it wrong is a 60%+ error, so see ``_read_share_based_payment`` and
``_derive_ebitda`` below for the rule and the evidence.
"""
from __future__ import annotations

import re

from src.extract.custom_registry import STATEMENT_TIER
from src.extract.extract_metrics import MetricRow
from src.extract.segment_tables import collapse, document_guard, emit, norm, numbers
from src.model.financial_model import MetricDef

# --- section anchors -------------------------------------------------------

# The quarterly income statement. Q4 releases word the header "three and twelve
# months ended", so the quarter block is still the one this matches first.
_THREE_MONTH_RE = re.compile(r"FOR THE THREE\b.*\bMONTHS ENDED")
# Anything cumulative closes the quarterly window: six/nine/twelve months, or a
# full year. Without this bound the six-month block's identical labels win.
_CUMULATIVE_RE = re.compile(
    r"FOR THE (?:SIX|NINE|TWELVE)\b.*\bMONTHS ENDED|FOR THE YEARS? ENDED")
_BALANCE_RE = re.compile(r"CONSOLIDATED BALANCE SHEET")
# Q4 releases title this "key ANNUAL operating metric" and fill it with FULL-YEAR
# figures, which is why the per-quarter store counts are read from prose instead.
_KPI_RE = re.compile(r"KEY\b.*\bOPERATING METRIC")
_KPI_ANNUAL_RE = re.compile(r"KEY ANNUAL\b")

# --- statement row labels --------------------------------------------------
#
# Label wording drifts across eras — "Operating Profit" became "Operating
# (Loss) Profit" once the company started posting quarterly losses, and the net
# line has appeared as "Net (Loss) Profit for the Period", "Net Profit (Loss)
# for the Period" and plain "Net Loss for the Period". These patterns are
# deliberately loose about the loss parenthetical and strict about everything
# else, and every one is anchored so a Margin row can never satisfy it.
_TOTAL_REVENUE_RE = re.compile(r"^TOTAL REVENUE$")
_MERCHANDISE_RE = re.compile(r"^REVENUE FROM SALES OF MERCHANDISE$")
_GROSS_PROFIT_RE = re.compile(r"^GROSS PROFIT$")
_OPERATING_RE = re.compile(r"^OPERATING (?:\(LOSS\) )?(?:LOSS )?PROFIT(?: \(LOSS\))?$")
_NET_PROFIT_RE = re.compile(r"^NET .{0,20}?FOR THE PERIOD$")
_EBITDA_RE = re.compile(r"^EBITDA$")
_TOTAL_ASSETS_RE = re.compile(r"^TOTAL ASSETS$")

# --- operating metrics -----------------------------------------------------

_STORES_OPENED_RE = re.compile(r"^NUMBER OF STORES OPENED$")
# "Number of Distribution Centers" is the running TOTAL; the 2024-era
# "Number Distribution Centers Opened" is the per-quarter ADDITION. Same table,
# same position, opposite meaning — so the total must never match the "Opened"
# wording, and the optional "of" has to be tolerated (1Q24 omits it).
_TOTAL_DCS_RE = re.compile(r"^NUMBER (?:OF )?DISTRIBUTION CENTERS$")
_SSS_RE = re.compile(r"^SAME STORE SALES GROWTH")
# "Same Store Sales grew 16.6% compared to 4Q24" / "grew by 16.6% for 4Q25".
# Anchored on the quarter's own sentence, which every era states before the
# annual one, so a Q4 release cannot hand back its full-year figure.
_SSS_PROSE_RE = re.compile(r"SAME STORE SALES GREW (?:BY )?([\d.]+)%")

# Prose is the primary source for store counts: it is the only place a Q4
# release states the QUARTER's openings (its table reports the full year).
_STORES_PROSE_RE = re.compile(
    r"OPENED ([\d,]+) NET NEW STORES[^.]{0,60}?REACHING (?:A TOTAL OF )?([\d,]+) (?:STORES|UNITS)")
_TOTAL_STORES_RE = re.compile(r"REACHING (?:A TOTAL OF )?([\d,]+) (?:STORES|UNITS) AS OF")
# Two wordings for the running distribution-centre total, both anchored on the
# phrase "distribution center" so the store-count sentence ("bringing the total
# to 2,043 stores") can never satisfy them.
_DCS_PROSE_RE = re.compile(
    r"DISTRIBUTION CENTERS?[^.]{0,60}?(?:REACHING|BRINGING THE TOTAL TO) ([\d,]+)")

# Share-based payment expense, in the two forms the release presents it.
_SBP_ROW_RE = re.compile(r"^SHARE-BASED PAYMENT EXPENSES?$")
_SBP_PROSE_RE = re.compile(
    r"SHARE-BASED PAYMENT EXPENSE (?:REACHED|OF|WAS) PS\.? ([\d,]+) MILLION")

# Appendix 1 — fully diluted share count.  These point-in-time inputs change
# independently of the income statement and therefore cannot be rolled from the
# prior-year model column.  The release prints them as one value per label (not
# current/prior pairs), so they have their own bounded single-cell reader.
_SHARE_COUNT_ROWS = (
    (re.compile(r"^CLASS A COMMON SHARES \(PUBLICLY TRADED AND REGISTERED\)$"),
     "shares_class_a"),
    (re.compile(r"^CLASS B COMMON SHARES \(HIGH-VOTE SHARES\)$"),
     "shares_class_b"),
    (re.compile(r"^CLASS C COMMON SHARES$"), "shares_class_c"),
    (re.compile(r"^LIQUIDITY EVENT(?: PLAN)? CLASS C (?:COMMON )?SHARES$"),
     "shares_liquidity_event"),
    (re.compile(r"^BOLTON PARTNERS CLASS C SHARE ALLOCATION$"),
     "shares_bolton_allocation"),
    (re.compile(r"^NET SHARES SUBJECT TO EQUITY-BASED COMPENSATION PLANS$"),
     "shares_equity_compensation"),
)
_SHARE_APPENDIX_RE = re.compile(r"APPENDIX 1: FULLY DILUTED SHARES")
_SHARE_APPENDIX_STOP_RE = re.compile(r"APPENDIX 2: SHARE-BASED PAYMENT")

_THOUSANDS = 0.001          # statements are in thousands of pesos; model is millions
_PCT = 0.01                 # "20.0%" -> 0.200, the fraction the Segments sheet stores
_MAX_CELLS = 5              # cells scanned after a label before giving up
_BALANCE_WINDOW = 120
_KPI_WINDOW = 30
_FOOTNOTE_RE = re.compile(r"^\(\d\)$")


def extract_tbbb(
    text: str,
    metric_defs: list[MetricDef],
    period: str | None = None,
    pdf_path=None,
) -> dict[str, MetricRow]:
    """Read Tiendas 3B's quarterly consolidated figures and operating metrics."""
    if not text or not document_guard(text, "BBB FOODS", "TIENDAS 3B"):
        return {}

    raw = [collapse(line) for line in text.splitlines() if line.strip()]
    lines = [norm(line) for line in raw]
    # Prose is matched against the FLATTENED document, not line by line. The
    # HTML-to-text conversion splits a highlights bullet across several text
    # nodes ("Non-cash share-based payment expense reached" / "Ps. 213" /
    # "million"), so a per-line search sees only fragments of the sentence.
    flat = norm(collapse(text))
    defs = {m.key: m for m in metric_defs}
    found: dict[str, MetricRow] = {}

    _read_quarterly_statement(lines, raw, defs, found)
    _read_balance_sheet(lines, raw, defs, found)
    _read_share_based_payment(lines, raw, flat, defs, found)
    _read_fully_diluted_shares(lines, raw, defs, found)
    _read_store_counts(flat, defs, found)
    _read_operating_metrics(lines, raw, flat, defs, found)
    _derive_ebitda(defs, found)
    return found


def _emit_prose(found, defs, key, pattern, flat, *, group: int = 1,
                scale: float = 1.0) -> bool:
    """Emit a metric from a prose sentence, if the document states one."""
    match = pattern.search(flat)
    if not match:
        return False
    value = float(match.group(group).replace(",", "")) * scale
    return emit(found, defs, key, current=value, prior=None,
                line=match.group(0)[:100], tag=STATEMENT_TIER)


# ---------------------------------------------------------------------------
# cell reading
# ---------------------------------------------------------------------------

# "(Ps. 386,336)" — a negative whose currency prefix sits inside the parentheses —
# parses to nothing unless the prefix is removed first. That cell is the net loss,
# so dropping it would silently lose the metric on every loss-making quarter.
_CURRENCY_RE = re.compile(r"PS\.?\s*|\$")


def _cell(line: str) -> float | None:
    """The numeric value of one table cell, or None if it holds no number."""
    vals = numbers(_CURRENCY_RE.sub("", line))
    return vals[0] if vals else None


def _pair(lines: list[str], start: int, stop: int, *,
          skip_pct: bool = True) -> tuple[float, float] | None:
    """The (current, prior) pair following a label line.

    A row is ``label, current, prior, %change``. Percent cells are skipped so a
    variance column cannot shift the reading — except on a row that is ITSELF a
    percentage, where ``skip_pct=False`` keeps them. Footnote markers ("(1)")
    sit between label and value on the same-store-sales row and parse as the
    number -1, so they are dropped explicitly rather than ending the scan.
    """
    out: list[float] = []
    for i in range(start + 1, min(start + 1 + _MAX_CELLS, stop)):
        if _FOOTNOTE_RE.match(lines[i]):
            continue
        if skip_pct and "%" in lines[i]:
            continue
        value = _cell(lines[i])
        if value is None:
            break
        out.append(value)
        if len(out) == 2:
            return out[0], out[1]
    return None


def _emit_scaled(found, defs, key, pair, line, scale) -> None:
    if pair is None:
        return
    current, prior = pair
    emit(found, defs, key, current=current * scale, prior=prior * scale,
         line=line, tag=STATEMENT_TIER)


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------

def _quarterly_bounds(lines: list[str]) -> tuple[int, int] | None:
    """(start, stop) of the three-month income statement, or None.

    ``stop`` is the first cumulative-period header after the start — the guard
    that keeps the six-month block's identical labels out of the reading.
    """
    start = next((i for i, line in enumerate(lines) if _THREE_MONTH_RE.search(line)), None)
    if start is None:
        return None
    stop = next((i for i in range(start + 1, len(lines))
                 if _CUMULATIVE_RE.search(lines[i])), len(lines))
    return start, stop


def _read_quarterly_statement(lines, raw, defs, found) -> None:
    bounds = _quarterly_bounds(lines)
    if bounds is None:
        return
    start, stop = bounds
    rows = (
        (_TOTAL_REVENUE_RE, "revenue"),
        (_MERCHANDISE_RE, "revenue_merchandise"),
        (_GROSS_PROFIT_RE, "gross_profit"),
        (_OPERATING_RE, "operating_income"),
        (_NET_PROFIT_RE, "net_income"),
        (_EBITDA_RE, "ebitda_reported"),
    )
    for i in range(start, stop):
        for pattern, key in rows:
            if key in found or not pattern.match(lines[i]):
                continue
            _emit_scaled(found, defs, key, _pair(lines, i, stop), raw[i], _THOUSANDS)
            break


def _read_balance_sheet(lines, raw, defs, found) -> None:
    start = next((i for i, line in enumerate(lines) if _BALANCE_RE.search(line)), None)
    if start is None:
        return
    stop = min(start + _BALANCE_WINDOW, len(lines))
    for i in range(start, stop):
        if _TOTAL_ASSETS_RE.match(lines[i]):
            _emit_scaled(found, defs, "total_assets", _pair(lines, i, stop),
                         raw[i], _THOUSANDS)
            return


def _read_share_based_payment(lines, raw, flat, defs, found) -> None:
    """Share-based payment expense, in WHOLE MILLIONS as the release presents it.

    The scale here is not a detail — it is the value the model uses. From 2Q25
    the front summary carries an SBP row in rounded millions; earlier releases
    state it only in prose ("Non-cash share-based payment expense reached Ps.
    129 million"). Both are rounded, and the model's history is built on the
    rounded figure: 1Q26's EBITDA hardcode is 1276.199 = 554.199 + 722, and
    2Q25's is 1095.5219 = 843.521 + 252.

    The cash-flow statement also carries an SBP line, in thousands and often
    year-to-date. It is deliberately NOT used: on a Q4 release it is the annual
    figure, which would overstate the quarter several-fold.
    """
    for i, line in enumerate(lines):
        if not _SBP_ROW_RE.match(line):
            continue
        # The front-summary row is the one whose value is followed by a margin
        # percentage; the cash-flow row is bare thousands. That shape is what
        # separates them, not their position.
        window = lines[i + 1:i + 4]
        if not any("%" in cell for cell in window):
            continue
        pair = _pair(lines, i, len(lines))
        if pair is not None:
            _emit_scaled(found, defs, "share_based_payment", pair, raw[i], 1.0)
            return

    _emit_prose(found, defs, "share_based_payment", _SBP_PROSE_RE, flat)


def _read_fully_diluted_shares(lines, raw, defs, found) -> None:
    """Read Appendix 1 share-count inputs as full share counts.

    The model stores these in thousands, but extraction deliberately preserves
    the document unit.  The company mapping owns the 0.001 conversion at the
    workbook boundary, making the provenance and scale explicit.
    """
    start = next((i for i, line in enumerate(lines)
                  if _SHARE_APPENDIX_RE.search(line)), None)
    if start is None:
        return
    stop = next((i for i in range(start + 1, len(lines))
                 if _SHARE_APPENDIX_STOP_RE.search(lines[i])), len(lines))
    for i in range(start, stop):
        match = next(((pattern, key) for pattern, key in _SHARE_COUNT_ROWS
                      if pattern.match(lines[i])), None)
        if match is None:
            continue
        _pattern, key = match
        for j in range(i + 1, min(i + 1 + _MAX_CELLS, stop)):
            if _FOOTNOTE_RE.match(lines[j]):
                continue
            value = _cell(lines[j])
            if value is None:
                break
            emit(found, defs, key, current=value, prior=None,
                 line=raw[i], tag=STATEMENT_TIER)
            break


def _read_store_counts(flat, defs, found) -> None:
    """Net new stores and the period-end total, from the highlights prose.

    Prose is preferred over the operating-metrics table because a Q4 release
    tables the FULL YEAR's openings (4Q25: 574) while the prose still states the
    quarter's (184) — and the model tracks the quarter.
    """
    match = _STORES_PROSE_RE.search(flat)
    if match:
        for key, group in (("net_new_stores", 1), ("total_stores", 2)):
            emit(found, defs, key,
                 current=float(match.group(group).replace(",", "")), prior=None,
                 line=match.group(0)[:100], tag=STATEMENT_TIER)
        return
    _emit_prose(found, defs, "total_stores", _TOTAL_STORES_RE, flat)


def _read_operating_metrics(lines, raw, flat, defs, found) -> None:
    """Distribution-centre count and same-store sales, from the metrics table.

    These are counts and percentages, not thousands of pesos, so nothing here is
    rescaled except the percentage.
    """
    start = next((i for i, line in enumerate(lines) if _KPI_RE.search(line)), None)
    if start is not None:
        annual = bool(_KPI_ANNUAL_RE.search(lines[start]))
        stop = min(start + _KPI_WINDOW, len(lines))
        for i in range(start, stop):
            if "total_dcs" not in found and _TOTAL_DCS_RE.match(lines[i]):
                # A period-end count, so the annual table's value is the same
                # figure as the quarter's — safe to read either way.
                _emit_scaled(found, defs, "total_dcs", _pair(lines, i, stop), raw[i], 1.0)
            elif "sss_yoy" not in found and not annual and _SSS_RE.match(lines[i]):
                _emit_scaled(found, defs, "sss_yoy",
                             _pair(lines, i, stop, skip_pct=False), raw[i], _PCT)
            elif ("net_new_stores" not in found and not annual
                    and _STORES_OPENED_RE.match(lines[i])):
                _emit_scaled(found, defs, "net_new_stores",
                             _pair(lines, i, stop), raw[i], 1.0)

    # A Q4 release's metrics table is full-year, so its same-store-sales figure
    # is the annual one (4Q25: 18.3% annual against 16.6% for the quarter). The
    # quarter is stated only in prose.
    if "sss_yoy" not in found:
        _emit_prose(found, defs, "sss_yoy", _SSS_PROSE_RE, flat, scale=_PCT)
    # Pre-2025 releases table only the DCs OPENED in the quarter, never the
    # running total; the total appears in the highlights prose instead.
    if "total_dcs" not in found:
        _emit_prose(found, defs, "total_dcs", _DCS_PROSE_RE, flat)


def _derive_ebitda(defs, found) -> None:
    """EBITDA as the model defines it: reported EBITDA PLUS share-based payment.

    Two EBITDAs are printed and they diverge enormously — 2Q26 shows Ps. 960mn
    reported against Ps. 1,575mn ex-SBP — so this is the most consequential
    choice in the file. The model tracks the ex-SBP measure, and it computes it
    rather than lifting the printed line. That distinction is not cosmetic: in
    4Q25 the release's own "EBITDA ex. SBP & One-Time Account Receivable
    Write-Off" line reads 1,200, while the model's hardcode is 970.442 — which
    is 79.442 + 891, reported EBITDA plus share-based payment and nothing else.
    Deriving therefore reproduces the analyst in every quarter checked
    (1Q24 754.771, 2Q25 1095.5219, 4Q25 970.442, 1Q26 1276.199), while the
    printed line does not.

    Reported EBITDA is kept as ``ebitda_reported`` so the alternative never
    requires a re-extraction.
    """
    reported, sbp = found.get("ebitda_reported"), found.get("share_based_payment")
    if "ebitda" in found or reported is None or sbp is None:
        return
    prior = (None if reported.prior is None or sbp.prior is None
             else reported.prior + sbp.prior)
    emit(found, defs, "ebitda", current=reported.current + sbp.current, prior=prior,
         line=f"derived: reported EBITDA + share-based payment ({reported.source_line})",
         tag=STATEMENT_TIER)
