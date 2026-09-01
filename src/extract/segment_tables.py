"""Shared primitives for the per-company segment extractors.

Every company on the panel gets a dedicated extractor, because segment tables cannot
be addressed by label alone: Bimbo prints the same four region labels under three
different metrics, Becle prints the same six category labels for both sales and
volume, KOF repeats `Total` once per division. What identifies a figure is the table
it sits in, so each extractor latches a section and then reads rows inside it.

That shape is the same everywhere; only the latch axis and the column rule differ.
This module holds the parts that were being copied verbatim — `_norm` was
byte-identical in gruma.py, bimbo.py and becle.py, as was the number regex — so a fix
lands once instead of nine times.

What deliberately stays per-company: which heading starts a section, which label names
a row, and WHICH COLUMN is the current quarter. That last one is where the money is
(Bimbo's trailing margin columns, Gruma's optional percentage block, Becle's period
token that parses as a bare `2`) and it must be written down per company, not guessed.
"""
from __future__ import annotations

import re
import unicodedata

from src.extract.extract_metrics import MetricRow, parse_number
from src.model.financial_model import MetricDef

# A numeric token: optional parens (negative), optional sign, optional currency, digits
# with thousands separators, optional decimals, optional trailing percent.
#
# The sign must be ATTACHED to the number. Tables use a bare dash for "none" —
# Alsea prints `Mexico 175 - 175` for a wholly-corporate brand — and a pattern that
# allowed whitespace after the minus read that as `-175`, flipping the sign of the
# total. `-18.0` still parses; `- 175` now yields `175`.
NUM_RE = re.compile(r"\(?-?(?:\$\s*)?\d[\d,]*(?:\.\d+)?%?\)?")

# `2Q26`, `4T25`, `2Q 26` — a period label, never a figure. Strip these before reading
# a row: `2Q26` otherwise contributes a bare `2`, which makes a column header look like
# a two-value data row and shifts every row under it by one.
PERIOD_TOKEN_RE = re.compile(r"\b\d\s?[QT]\s?\d{2,4}\b", re.I)

# A row label must be followed immediately by a number. This is what separates a table
# row from a sentence that merely opens with the same word ("North America delivered…").
NUMERIC_TAIL_RE = re.compile(r"^\(?-?\$?\s*\d")


def norm(text: str) -> str:
    """Uppercase, accent-stripped text — so `México` and `MEXICO` are one label."""
    stripped = unicodedata.normalize("NFKD", text)
    return "".join(c for c in stripped if not unicodedata.combining(c)).upper()


def collapse(raw: str) -> str:
    """One line with runs of whitespace collapsed to single spaces."""
    return " ".join(raw.split())


# A single stray character at the start of a line, left by a rotated sidebar. Alsea
# prints "BRAND" vertically down the margin, one glyph per text line, and pdfplumber
# emits those glyphs on whatever data row shares their baseline:
#     A    Belgium    2   31   33
# The row then no longer STARTS with its label, so an anchored match misses it —
# which silently dropped Starbucks Belgium, Portugal and Uruguay from 3Q25.
_MARGIN_GLYPH_RE = re.compile(r"^[A-Z](?:\s+[A-Z])*\s+(?=[A-Z][a-zA-Z])")


def strip_margin_glyph(line: str) -> str:
    """Drop a leading rotated-sidebar glyph, if the rest still looks like a label."""
    return _MARGIN_GLYPH_RE.sub("", line, count=1)


def numbers(line: str, *, drop_pct: bool = False,
            drop_periods: bool = True) -> list[float]:
    """Numeric tokens on the line, left to right.

    `drop_pct` removes percentage columns, which is what you want when the value
    columns and the variance columns are interleaved. `drop_periods` removes period
    labels, which are not data.
    """
    text = PERIOD_TOKEN_RE.sub(" ", line) if drop_periods else line
    out: list[float] = []
    for tok in NUM_RE.findall(text):
        if drop_pct and "%" in tok:
            continue
        v = parse_number(tok)
        if v is not None:
            out.append(v)
    return out


def match_label(patterns, norm_line: str) -> tuple[str | None, str]:
    """First (name, pattern) whose regex matches at the START of the line.

    Returns (name, the rest of the line after the label) so the caller can apply the
    numeric-tail guard. Order the patterns longest-first: `NORTH AMERICA` has to be
    tested before anything that could prefix-match it.
    """
    for name, pattern in patterns:
        m = re.match(pattern, norm_line)
        if m:
            return name, norm_line[m.end():].strip()
    return None, norm_line


def match_section(patterns, norm_line: str) -> str | None:
    """First (name, pattern) whose regex is found anywhere on the line."""
    for name, pattern in patterns:
        if re.search(pattern, norm_line):
            return name
    return None


def emit(found: dict[str, MetricRow], defs: dict[str, MetricDef], key: str, *,
         current: float, prior: float | None = None, var_pct: float | None = None,
         line: str, tag: str) -> bool:
    """Record one row, or do nothing if the config never declared the key.

    The whitelist is the contract: the config decides which metrics exist, so an
    extractor change can never introduce a metric the company has not declared. Label
    and unit are copied from the MetricDef, never invented here.
    """
    mdef = defs.get(key)
    if mdef is None:
        return False
    found[key] = MetricRow(
        metric=key,
        label_es=mdef.label_es,
        current=current,
        prior=prior,
        var_pct=var_pct,
        unit=mdef.unit,
        source_line=f"[{tag}] {line[:100]}",
    )
    return True


def document_guard(text: str, *tokens: str, whole_word: bool = False) -> bool:
    """True when the document mentions any of `tokens` (accent-insensitive).

    Cheap first statement in every extractor: a company's extractor must return `{}`
    on someone else's document rather than pattern-match its way into nonsense.

    `whole_word=True` requires the token to stand alone. Pass it whenever a
    token is a prefix of an ordinary Spanish word — "LA COMER" otherwise
    matches "la comercialización", which appears in most issuers' filings and
    turns the guard into a no-op.
    """
    flat = norm(text)
    if not whole_word:
        return any(norm(t) in flat for t in tokens)
    return any(re.search(rf"\b{re.escape(norm(t))}\b", flat) for t in tokens)
