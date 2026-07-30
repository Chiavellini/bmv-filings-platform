"""statement_utils.py — shared primitives for deterministic statement extractors.

The per-company custom extractors (gmexico, soriana, herdez, orbia, lab, liverpool)
each walk printed financial statements and pull numbers out of noisy, OCR-parsed
lines. Most of the *scalar* parsing is already centralised in
:func:`src.extract.extract_metrics.parse_number` — the canonical
"Mexican-format string → float" primitive (parens = negative; strips ``,`` / ``%`` /
``$`` / spaces; recognises n/a sentinels). New extractors should import it **from here**.

This module adds the two *line-level* helpers that were genuinely duplicated (and are
unit-testable in isolation):

- :func:`repair_split_digits` — rejoin a lone leading digit split from its number by
  OCR noise, without merging adjacent columns.
- :func:`looks_like_year` — recognise a stray 4-digit column-header year that leaked
  into a value row.

Deliberately NOT centralised (they are genuinely company-specific — see
docs/EXTRACTION_ROADMAP.md P0 #3): soriana's one-number-per-line vertical de-spacing,
herdez's ``$``/``%``-rejecting ``parse_num``, liverpool's magnitude-filter + currency
scaling, gmexico's sign-dropping tokenizer, lab's space-as-thousands regex.
"""
from __future__ import annotations

import re

# Re-export the canonical scalar parser so extractors can depend on this module as the
# single home. (It still physically lives in extract_metrics to avoid churning ~5
# importers + tests/test_parse_number.py; a later cleanup can flip the location.)
from src.extract.extract_metrics import parse_number  # noqa: F401

__all__ = ["parse_number", "repair_split_digits", "looks_like_year"]

_SPLIT_DIGIT_RE = re.compile(r"(?<![\d.,])(\d)\s+(?=[\d,])")


def repair_split_digits(line: str) -> str:
    """Rejoin a lone leading digit that OCR split from the rest of its number.

    BMV/OCR parses sometimes insert a space after the first digit of a number
    (``"7 48,239"`` → should be ``"748,239"``; ``"1 ,050,272"`` → ``"1,050,272"``;
    ``"9 3,501"`` → ``"93,501"``). The negative lookbehind means only a *lone leading*
    digit is rejoined — a genuine column boundary is never merged
    (``"527,501 18.7"`` stays two numbers, because the ``1`` is preceded by ``0``).
    """
    return _SPLIT_DIGIT_RE.sub(r"\1", line)


def looks_like_year(raw: str, value: float, *, lo: int = 1900) -> bool:
    """True when ``raw``/``value`` is a bare 4-digit column-header year that leaked
    into a value row (so the caller should drop it).

    A stray year is a plain integer in ``[lo, 2099]`` with no comma, decimal or ``%``
    — a real monetary figure of that magnitude is written ``2,024`` (comma) or
    ``2024.0`` (decimal), which are kept. ``lo`` defaults to 1900 (lab's bound); pass
    ``lo=2000`` for orbia's tighter bound.
    """
    cleaned = raw.strip().strip("()").replace("$", "").replace(" ", "")
    if not cleaned.isdigit() or "," in raw or "." in raw or "%" in raw:
        return False
    return lo <= int(value) <= 2099
