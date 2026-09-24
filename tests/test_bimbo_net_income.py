"""Bimbo's income statement has three lines containing "NET INCOME".

Only one of them is Utilidad Neta. Extracting 1Q26 through the Analyst Console
returned `net_income = 4,443` -- NET INCOME BEFORE TAXES -- against a true
2,362, an 88% overstatement that shipped as validated=True, flagged=False.

The generic patterns in `src/model/financial_model.py` require a number
immediately after the label, so they correctly decline all three lines; the
looser positional `table` tier then matched the first one by label similarity.
`configs/bimbo.yaml` now pins the metric to NET MAJORITY INCOME, the headline
figure Bimbo reports in its own highlights table.

No PDF is needed: these are the literal lines from the 1Q26 filing.
"""

from __future__ import annotations

from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Verbatim from the 1Q26 income statement (page 8 of the filing).
INCOME_STATEMENT = """\
OPERATING INCOME 7,911 6,741 17.4
Comprehensive Financing Cost 3,462 3,247 6.6
NET INCOME BEFORE TAXES 4,443 3,544 25.4
Income Taxes 1,644 1,315 25.0
INCOME (LOSS) FROM CONTINUED OPERATIONS 2,799 2,229 25.6
NET MAJORITY INCOME 2,362 1,785 32.3
ADJUSTED EBITDA 14,036 12,797 9.7
"""

TRUE_NET_INCOME = ("2,362", "1,785")
PRE_TAX_INCOME = "4,443"


def _net_income_patterns() -> list[str]:
    config = yaml.safe_load((ROOT / "configs" / "bimbo.yaml").read_text(encoding="utf-8"))
    overrides = config["metric_overrides"]["net_income"]["patterns"]
    return [entry["regex"] for entry in overrides]


def test_bimbo_pins_net_income_to_the_majority_line() -> None:
    matches = [
        match
        for pattern in _net_income_patterns()
        for match in re.finditer(pattern, INCOME_STATEMENT)
    ]
    assert matches, "no configured pattern matched the income statement"
    assert all(m.groups() == TRUE_NET_INCOME for m in matches), (
        f"expected every match to be {TRUE_NET_INCOME}, got "
        f"{[m.groups() for m in matches]}"
    )


def test_bimbo_net_income_never_matches_the_pre_tax_line() -> None:
    """The specific 88% error this override exists to prevent."""
    for pattern in _net_income_patterns():
        for match in re.finditer(pattern, INCOME_STATEMENT):
            assert PRE_TAX_INCOME not in match.groups(), (
                f"pattern {pattern!r} captured pre-tax income {PRE_TAX_INCOME}"
            )


def test_bimbo_net_income_handles_the_spanish_filing_layout() -> None:
    spanish = "Utilidad Neta Mayoritaria 2,362 1,785 32.3\n"
    assert any(
        re.search(pattern, spanish) for pattern in _net_income_patterns()
    ), "no pattern covers the Spanish-language layout"
