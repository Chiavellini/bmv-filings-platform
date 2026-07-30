"""Canonical master-matrix column contract — the single source of truth shared by
``scripts/build_master.py`` (rendering) and ``src/coverage/applicability.py`` (the N/A map).

Kept in its own tiny module so both importers share the column identity without an import
cycle (build_master → applicability → build_master).

A column is ``(band, block_id, label, header, unit, good_direction)``; a *colkey* is the
``(block_id, label)`` tuple used to index a row's ``values`` dict. Labels are unique across
the whole list, so ``COLKEY_BY_LABEL`` lets callers name columns by their (human) label.
See ``docs/ANALYSIS_METRICS.md`` §C for the metric definitions.
"""
from __future__ import annotations

# (band, block_id, label, header, unit, good) — the MEDIUM cross-comparable set: metrics
# computable from cached BMV XBRL + live price for industrials, banks, and REITs alike.
# CET1 (banks-only, free from CNBV) is appended as its own band; applicability.py routes it.
COLUMNS = [
    ("Valuation", "snapshot_multiples", "P/E (LTM)", "P/E", "x", "low"),
    ("Valuation", "snapshot_multiples", "EV/EBITDA", "EV/EBITDA", "x", "low"),
    ("Valuation", "financial_analysis", "P/BV", "P/BV", "x", "low"),
    ("Valuation", "snapshot_multiples", "Dividend yield", "Div yield", "pct", "high"),
    # REIT-native: sector convention reads P/FFO where P/E is na_template (fair-value distorted
    # earnings). FFO from XBRL (src/coverage/reit_ffo.py) when no Bloomberg pack; N/A elsewhere.
    ("Valuation", "reit_snapshot", "P/FFO", "P/FFO", "x", "low"),

    ("Profitability", "financial_analysis", "ROE", "ROE", "pct", "high"),
    ("Profitability", "financial_analysis", "ROIC", "ROIC", "pct", "high"),
    ("Profitability", "financial_analysis", "EBITDA margin", "EBITDA mgn", "pct", "high"),
    ("Profitability", "financial_analysis", "Net margin", "Net mgn", "pct", "high"),

    ("Growth", "growth", "Revenue CAGR 1y", "Rev CAGR 1y", "pct", "high"),
    ("Growth", "growth", "Revenue CAGR 5y", "Rev CAGR 5y", "pct", "high"),
    ("Growth", "growth", "Net income CAGR 1y", "NI CAGR 1y", "pct", "high"),
    ("Growth", "growth", "Net income CAGR 5y", "NI CAGR 5y", "pct", "high"),
    ("Growth", "temporal_ebit", "EBITDA YoY (latest)", "EBITDA YoY", "pct", "high"),
    ("Growth", "growth", "Revenue growth stability (σ)", "Rev σ", "pct", "low"),

    ("Leverage & Liquidity", "financial_analysis", "Net debt / EBITDA", "NetDebt/EBITDA", "x", "low"),
    ("Leverage & Liquidity", "fcf_liquidity", "Current ratio", "Current", "x", "high"),
    ("Leverage & Liquidity", "fcf_liquidity", "FCF yield", "FCF yield", "pct", "high"),

    # Free, monthly-refreshed from CNBV (ICAP_BM_<YYYYMM>.pdf) — banks-sector only; N/A elsewhere.
    ("Capital (financials)", "bank_returns", "CET1 ratio", "CET1", "pct", "high"),
    # Price / tangible book — already computed in block_bank_snapshot (peers.py MULTIPLES_BANK) for
    # the whole financials template (banks, brokers, insurers), never previously surfaced in the
    # master grid. No new extraction: verified 18/22 financials already fill it via cached XBRL +
    # live price.
    ("Capital (financials)", "bank_snapshot", "P/TBV", "P/TBV", "x", "low"),

    # ---- Bank-only operating metrics (financials template; N/A for industrial + reit). Pass-throughs
    # of bank_returns (CET1/ICAP from CNBV; NIM/efficiency/cost-of-risk from the annual-report parse —
    # honest ceiling: sparse today). Surfaced so the Banks section shows a bank's own metrics instead
    # of the EV/EBITDA / FCF columns that are meaningless for a bank.
    ("Risk (banks)", "bank_returns", "Net interest margin (NIM)", "NIM", "pct", "high"),
    ("Risk (banks)", "bank_returns", "Efficiency ratio", "Efficiency", "pct", "low"),
    ("Risk (banks)", "bank_returns", "Cost of risk", "Cost of risk", "pct", "low"),

    # ---- REIT-only property metrics (reit template; N/A for industrial + financials). From
    # reit_metrics (occupancy + NOI margin, MD&A-parsed — honest ceiling: sparse today). Surfaced so
    # the REITs section shows property operating metrics alongside P/FFO.
    ("Property (REITs)", "reit_metrics", "Occupancy", "Occupancy", "pct", "high"),
    ("Property (REITs)", "reit_metrics", "NOI margin", "NOI mgn", "pct", "high"),
]

FIXED_HEADERS = ["Company", "Sector", "Template", "Ticker"]
N_FIXED = len(FIXED_HEADERS)


def colkey(col) -> tuple[str, str]:
    """(block_id, label) for a COLUMNS entry — the key used to index a row's ``values``."""
    return (col[1], col[2])


COLKEYS = [colkey(c) for c in COLUMNS]
COLKEY_BY_LABEL = {c[2]: colkey(c) for c in COLUMNS}

# --------------------------------------------------------------------------------------------------
# Semantic label groups — used by applicability.py (source overlays) and certify_master.py (cause).
# --------------------------------------------------------------------------------------------------
# The metrics Yahoo key-stats can compute for a NO-XBRL (ABSENT) company — the only cells such a
# company can fill. Mirrors _yahoo_fallback_vmap() in build_master.py.
YAHOO_FILLABLE_LABELS = {
    "P/E (LTM)", "EV/EBITDA", "P/BV", "ROE", "Net margin", "Dividend yield",
}
# Columns that need multi-year annual history (≥5 annual filings) to compute.
HISTORY_LABELS = {
    "Revenue CAGR 5y", "Net income CAGR 5y", "Revenue growth stability (σ)",
}
# Columns that need only a short history (≥2 annual filings) — a single YoY step. Applicable far
# more broadly than the 5y CAGRs, so gated separately.
SHORT_HISTORY_LABELS = {
    "Revenue CAGR 1y", "Net income CAGR 1y",
}
# Columns whose value moves with live price → a blank here on a company with a stale/missing price
# is a *recoverable* (throttled-price) gap, not a source hole.
PRICE_DERIVED_LABELS = {
    "P/E (LTM)", "EV/EBITDA", "P/BV", "Dividend yield", "FCF yield",
}
