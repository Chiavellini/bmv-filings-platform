"""Becle (José Cuervo) segment tables — deterministic extractor.

Becle publishes the same four region labels and the same six category labels twice
each: once for net sales and once for volume. The label therefore does not identify a
figure, and `U.S. & Canada` legitimately resolves to 6,717 (sales) and 3,782 (volume)
in one document — which is precisely why a label-keyed approach could not be certified.

On top of that, the release is laid out around donut charts, so labels wrap *around*
their own numbers and stray chart percentages land on their own lines:

    Rest of the                                  29.6%      <- chart annotation
               1,603  1,877 -14.6% -14.6% -4.1%             <- the data
    World                                                   <- rest of the label

So the row label is not reliably adjacent to the row. What IS reliable is the order of
rows inside each table, which is fixed across every vintage checked. This extractor
latches the table from its caption and then walks its data rows in order.

A data row is a line carrying at least two NON-percentage numbers — the current and
prior columns. That single test discards the chart annotations (percentages alone) and
the wrapped label fragments (no numbers) without needing to understand either.
"""
from __future__ import annotations

import re
import unicodedata

from src.extract.extract_metrics import MetricRow, parse_number
from src.model.financial_model import MetricDef

# Table caption -> (metric prefix, row sequence). The sequences include rows the model
# does not use (`subtotal_spirits`) because position is what identifies a row here;
# dropping one would shift every row beneath it.
_REGION_ROWS = ("us_canada", "mexico", "row", "total")
_CATEGORY_ROWS = ("jose_cuervo", "other_tequilas", "other_spirits",
                  "subtotal_spirits", "non_alcoholic", "rtd", "total")

_TABLES = (
    (r"^NET\s+SALES\s+BY\s+REGION\b", "revenue", _REGION_ROWS),
    (r"^VOLUME\s+BY\s+REGION\b", "volume", _REGION_ROWS),
    (r"^NET\s+SALES\s+BY\s+CATEGORY\b", "revenue", _CATEGORY_ROWS),
    (r"^VOLUME\s+BY\s+CATEGORY\b", "volume", _CATEGORY_ROWS),
)

# The consolidated total is the plain metric name, not a `_total` suffix.
_TOTAL_KEY = {"revenue": "revenue", "volume": "volume"}

_NUM_RE = re.compile(r"\(?-?\$?\s*[\d,]+(?:\.\d+)?%?\)?")
# `2Q26`, `4T25`, `2Q 26` — a period label, never a figure.
_PERIOD_TOKEN_RE = re.compile(r"\b\d\s?[QT]\s?\d{2,4}\b", re.I)
# A caption line ends the previous table and starts a new one; anything that is not a
# caption and carries no values is skipped rather than counted as a row.
_CAPTION_RE = re.compile(r"\b(?:BY\s+REGION|BY\s+CATEGORY)\b")
# "Full Year 2025 results" / "... for Full Year 2025" — the annual block in a Q4 release.
_FULL_YEAR_RE = re.compile(r"\bFULL\s+YEAR\b")


def _norm(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c)).upper()


def _values(line: str) -> list[float]:
    """Numbers on the line that are NOT percentages — i.e. the value columns.

    Period tokens are removed first. `2Q26` otherwise contributes a bare `2`, which
    made the column header `Region 2Q26 2Q25 % ∆` look like a two-value data row and
    shifted every row in the table down by one.
    """
    out = []
    for tok in _NUM_RE.findall(_PERIOD_TOKEN_RE.sub(" ", line)):
        if "%" in tok:
            continue
        v = parse_number(tok)
        if v is not None:
            out.append(v)
    return out


def _detect_table(norm_line: str):
    for pattern, prefix, rows in _TABLES:
        if re.search(pattern, norm_line):
            return prefix, rows
    return None, None


def extract_becle_segments(text: str, metric_defs: list[MetricDef],
                           period=None, pdf_path=None) -> dict[str, MetricRow]:
    """Region and category net sales / volume, read positionally within each table."""
    norm_all = _norm(text)
    if "BECLE" not in norm_all and "CUERVO" not in norm_all:
        return {}

    defs = {m.key: m for m in metric_defs}
    found: dict[str, MetricRow] = {}
    prefix: str | None = None
    rows: tuple[str, ...] = ()
    idx = 0
    annual = False

    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        norm_line = _norm(line)

        # A Q4 release carries the SAME four captions twice: once for the quarter and
        # again under "Full Year 2025 results", whose tables are headed by bare years
        # ("Region 2025 2024 % ∆"). Both matched, the annual table came second, and it
        # overwrote every quarterly figure — BECLE's 4Q rows were wrong in every year
        # from 2022 on. The annual block is terminal in the document, so once it starts
        # nothing after it is a quarterly table.
        if _FULL_YEAR_RE.search(norm_line):
            prefix, rows = None, ()
            annual = True
            continue
        if annual:
            continue
        if _CAPTION_RE.search(norm_line):
            prefix, rows = _detect_table(norm_line)
            idx = 0
            continue
        if prefix is None or idx >= len(rows):
            continue

        vals = _values(line)
        if len(vals) < 2:
            continue                      # chart annotation or wrapped label fragment

        row_name = rows[idx]
        idx += 1
        key = _TOTAL_KEY[prefix] if row_name == "total" else f"{prefix}_{row_name}"
        # The config decides which metrics exist; never invent one.
        if key not in defs:
            continue

        mdef = defs[key]
        found[key] = MetricRow(
            metric=key,
            label_es=mdef.label_es,
            current=vals[0],
            prior=vals[1],
            var_pct=None,
            unit=mdef.unit,
            source_line=f"[statement] becle {line[:100]}",
        )

    return found
