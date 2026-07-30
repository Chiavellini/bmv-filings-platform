"""test_statement_utils.py — shared statement-extraction primitives.

Characterization tests that LOCK the behaviour custom extractors depend on, so the
gmexico/lab/orbia migrations onto these helpers are provably behaviour-preserving
(the full ground-truth certs are iCloud-eviction-blocked, so equivalence is proven
here at the unit level instead).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.extract.statement_utils import (
    looks_like_year,
    parse_number,
    repair_split_digits,
)


# ── repair_split_digits (was gmexico._repair) ─────────────────────────────
@pytest.mark.parametrize("raw,expect", [
    ("Ventas 7 48,239", "Ventas 748,239"),          # lone leading digit
    ("EBITDA 1 ,050,272 900", "EBITDA 1,050,272 900"),
    ("EBITDA 9 3,501 8 9,911", "EBITDA 93,501 89,911"),
    ("Ventas 3 ,345,585 2,818,084", "Ventas 3,345,585 2,818,084"),
    ("Costo 2 57,469", "Costo 257,469"),
])
def test_repair_rejoins_split_leading_digit(raw, expect):
    assert repair_split_digits(raw) == expect


def test_repair_preserves_real_column_boundary():
    # "527,501 18.7" must NOT merge — the 1 is preceded by 0, not a lone digit.
    assert repair_split_digits("Ventas 527,501 18.7") == "Ventas 527,501 18.7"
    # two clean adjacent numbers stay separate
    assert repair_split_digits("4,195,519 3,799,097") == "4,195,519 3,799,097"


# ── looks_like_year (was lab._looks_like_year / orbia inline guard) ────────
@pytest.mark.parametrize("raw,value,lo,expect", [
    ("2024", 2024.0, 1900, True),      # bare 4-digit year
    ("2024", 2024.0, 2000, True),
    ("2,024", 2024.0, 1900, False),    # comma ⇒ a real figure, not a year
    ("2024.0", 2024.0, 1900, False),   # decimal ⇒ real figure
    ("12.3%", 12.3, 1900, False),      # percent
    ("1998", 1998.0, 1900, True),      # lab keeps 1900s as year…
    ("1998", 1998.0, 2000, False),     # …orbia's tighter bound does not
    ("2100", 2100.0, 1900, False),     # above the 2099 ceiling
    ("(2024)", -2024.0, 2000, False),  # negative ⇒ not a year
    ("1,234", 1234.0, 1900, False),    # ordinary money figure
])
def test_looks_like_year(raw, value, lo, expect):
    assert looks_like_year(raw, value, lo=lo) is expect


# ── equivalence to the OLD inline guards the migrations replaced ──────────
_PROBES = [
    ("2024", 2024.0), ("2,024", 2024.0), ("2024.0", 2024.0), ("12.3%", 12.3),
    ("1998", 1998.0), ("2100", 2100.0), ("(2024)", -2024.0), ("1,234", 1234.0),
    ("$2024", 2024.0), ("2099", 2099.0), ("1999", 1999.0),
]


def _old_lab(raw, value):  # verbatim pre-migration lab._looks_like_year
    cleaned = raw.strip().strip("()").replace("$", "").replace(" ", "")
    if not cleaned.isdigit() or "," in raw or "." in raw or "%" in raw:
        return False
    return 1900 <= int(value) <= 2099


def _old_orbia(raw, value):  # verbatim pre-migration orbia inline guard
    return 2000 <= value <= 2099 and "." not in raw and "," not in raw


def test_matches_old_lab_guard():
    for raw, v in _PROBES:
        assert looks_like_year(raw, v, lo=1900) == _old_lab(raw, v), raw


def test_matches_old_orbia_guard():
    # orbia only ever feeds non-%, positive number tokens here (its `%` tokens are
    # skipped upstream), so the probe set excludes "12.3%".
    for raw, v in _PROBES:
        if raw.endswith("%"):
            continue
        assert looks_like_year(raw, v, lo=2000) == _old_orbia(raw, v), raw


# ── parse_number re-export smoke ──────────────────────────────────────────
def test_parse_number_reexported():
    assert parse_number("(1,234)") == -1234.0
    assert parse_number("$1,234.5") == 1234.5
    assert parse_number("12.3%") == 12.3
    assert parse_number("—") is None
