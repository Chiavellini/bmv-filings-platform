"""Tests for the Grupo México custom extractor (src/extract/gmexico.py).

Grupo México's reports exercise several tricky behaviours the extractor must get
right, verified here against the real cached corpus:
  * column orientation FLIPS by era — 2018 prints prior-year first, 2020 current
    first, and a single report (2025-2T) can flip back to prior-first;
  * the standardized "(GM)" statement header labels columns with quarter tags
    ("2T24 2T25") OR plain years ("2025 2026");
  * Q4 reports carry the full-year columns after the standalone quarter — only the
    quarter is taken;
  * USD-thousands tables scale to US$ millions (÷1000);
  * BMV split-digit noise ("7 48,239", "9 3,501") is repaired without merging real
    column boundaries;
  * a division block is anchored by its own "División X" header, and a subsidiary's
    ESTADO DE RESULTADOS is never read as the consolidated one.
"""
from pathlib import Path

import pytest

from src.excel.segments_sheet import load_metric_defs
from src.extract.gmexico import extract_gmexico, _tokens
from src.extract.statement_utils import repair_split_digits as _repair

REPORTS = Path(__file__).resolve().parents[1] / "data" / "reports" / "grupo_mexico"
CONFIG = Path(__file__).resolve().parents[1] / "configs" / "grupo_mexico.yaml"

pytestmark = pytest.mark.skipif(
    not (REPORTS / "2025-1T.md").exists(),
    reason="grupo_mexico corpus not present",
)


@pytest.fixture(scope="module")
def defs():
    return load_metric_defs(str(CONFIG))


def _run(defs, period):
    return extract_gmexico((REPORTS / f"{period}.md").read_text(encoding="utf-8"),
                           defs, period=period)


def _cur(rows, key):
    return rows[key].current if key in rows else None


# ── split-digit repair (unit) ─────────────────────────────────────────────
@pytest.mark.parametrize("raw,expect_tokens", [
    ("Ventas 3 ,345,585 2,818,084 527,501 18.7", [3345585.0, 2818084.0, 527501.0, 18.7]),
    ("Costo de Ventas 7 48,239 9 20,328", [748239.0, 920328.0]),
    ("EBITDA 9 3,501 8 9,911 3,591 4.0", [93501.0, 89911.0, 3591.0, 4.0]),
])
def test_repair_rejoins_split_digits(raw, expect_tokens):
    assert _tokens(_repair(raw)) == expect_tokens


def test_repair_keeps_real_column_boundary():
    # "527,501 18.7" must stay two numbers (the 1 is preceded by 0, not a lone digit).
    assert _tokens(_repair("Ventas 527,501 18.7")) == [527501.0, 18.7]


# ── consolidated P&L (USD millions, standalone quarter) ────────────────────
def test_consolidated_current_first_era(defs):
    r = _run(defs, "2025-1T")   # header "2025 2024" — current first
    assert _cur(r, "revenue") == pytest.approx(4195.5, abs=0.1)
    assert _cur(r, "ebitda") == pytest.approx(2216.7, abs=0.1)
    assert _cur(r, "net_income") == pytest.approx(1009.2, abs=0.1)  # MAJORITY


def test_statement_prior_first_flip(defs):
    # 2025-2T statement prints "2024 2025" (prior first): current 2Q25 is col 2.
    r = _run(defs, "2025-2T")
    assert _cur(r, "revenue") == pytest.approx(4239.2, abs=0.1)   # NOT 4397.0 (2Q24)


def test_statement_year_labeled_header(defs):
    # 2026-1T header labels columns with plain years "2025 2026" (no quarter tags).
    r = _run(defs, "2026-1T")
    assert _cur(r, "revenue") == pytest.approx(5565.3, abs=0.1)   # 1Q26, col 2


def test_q4_takes_standalone_quarter_not_fy(defs):
    r = _run(defs, "2024-4T")   # standalone 4Q24 = 3,847.3, NOT FY 16,169.9
    assert _cur(r, "revenue") == pytest.approx(3847.3, abs=0.1)
    assert _cur(r, "ebitda") == pytest.approx(1905.6, abs=0.1)


# ── divisions & era-flip orientation ──────────────────────────────────────
def test_divisions_prior_first_2018(defs):
    # 2018-2T tables print prior year first; Minera current (2018) is col 2.
    r = _run(defs, "2018-2T")
    assert _cur(r, "ns_minera") == pytest.approx(1984.8, abs=0.1)
    assert _cur(r, "ns_infraestructura") == pytest.approx(169.3, abs=0.1)


def test_divisions_never_exceed_consolidated(defs):
    for period in ("2025-1T", "2026-1T", "2024-4T", "2022-2T"):
        r = _run(defs, period)
        rev = _cur(r, "revenue")
        for k in ("ns_minera", "ns_transportes", "ns_infraestructura"):
            v = _cur(r, k)
            if rev and v:
                assert v <= rev * 1.02, f"{period} {k}={v} > revenue {rev}"


def test_infraestructura_not_mislabeled_consolidated(defs):
    # A stray "División Infraestructura" prose mention must not tag the consolidated
    # block as infra (regression: ns_infraestructura once == revenue).
    r = _run(defs, "2024-1T")
    assert _cur(r, "ns_infraestructura") == pytest.approx(189.9, abs=0.2)
    assert _cur(r, "ns_infraestructura") < _cur(r, "revenue") / 2


# ── balance sheet cash + debt ─────────────────────────────────────────────
def test_balance_cash_and_debt(defs):
    r = _run(defs, "2024-4T")
    assert _cur(r, "cash") == pytest.approx(8162.9, abs=0.5)
    # total debt = deuda cp + lp (757.3 + 7,691.3)
    assert _cur(r, "total_debt") == pytest.approx(8448.6, abs=1.0)


# ── subsidiary-only report yields no consolidated statement ───────────────
def test_subsidiary_only_report_no_consolidated_statement(defs):
    # 2019-3T prints only subsidiary (GMXT/MPD) statements — the consolidated P&L
    # comes from the page-2 table, never a subsidiary's 642.9 figure.
    r = _run(defs, "2019-3T")
    rev = _cur(r, "revenue")
    assert rev is None or rev > 2000  # consolidated ~2,794, never the GMXT 642.9
