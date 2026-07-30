"""The Bloomberg field registry + the ingested ``BloombergPack`` contract.

soft/ has no terminal, so these are the fields the user fills in from a Bloomberg terminal.
The registry is data-driven: :func:`required_rows` expands it against a coverage spec (which
blocks are enabled, how many peers/segments/macro rows) into the exact set of cells the
template must ask for. Every cell carries a ``[bbg]`` source tag downstream.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------------------
# Field registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    unit: str          # price | shares | currency | pct | x | index | ratio
    bbg_hint: str      # the Bloomberg field/override that produces it
    scope: str         # company | subject_scalar | segment | macro | history
    blocks: tuple[str, ...]
    entities: tuple[str, ...] = ("subject", "peer")  # only meaningful for scope=="company"


# Company-level market structure — needed for every company (subject + peers) so EV and the
# equity multiples can be computed uniformly.
_MARKET = [
    FieldSpec("px_last", "Last price", "price", "PX_LAST", "company",
              ("snapshot_multiples", "historical_multiples", "sum_of_the_parts", "replacement_value",
               "bank_snapshot", "bank_historical", "reit_snapshot", "reit_historical")),
    FieldSpec("shares_out", "Diluted shares out (mn)", "shares", "EQY_SH_OUT (diluted)", "company",
              ("snapshot_multiples", "sum_of_the_parts", "replacement_value",
               "bank_snapshot", "bank_historical", "reit_snapshot", "reit_historical")),
    FieldSpec("net_debt", "Net debt (mn)", "currency", "NET_DEBT", "company",
              ("snapshot_multiples", "sum_of_the_parts", "replacement_value", "reit_snapshot")),
    FieldSpec("minority_interest", "Minority interest (mn)", "currency", "MINORITY_NONCONTROLLING_INTEREST", "company",
              ("snapshot_multiples", "sum_of_the_parts", "reit_snapshot")),
]

# Consensus estimates — subject + peers.
_ESTIMATES = [
    FieldSpec("dvd_yield", "Dividend yield, %", "pct", "EQY_DVD_YLD_IND", "company",
              ("snapshot_multiples", "bank_snapshot")),
]

# Peer fundamentals — PEERS ONLY. The subject's fundamentals come from its filings ([filing]),
# but we do not extract peer filings, so peers supply these from Bloomberg.
_PEER_FUNDAMENTALS = [
    FieldSpec("sales_ltm", "Sales, LTM (mn)", "currency", "SALES_REV_TURN (LTM)", "company",
              ("snapshot_multiples", "financial_analysis"), entities=("peer",)),
    # EBITDA is SHARED (subject + peer): EBITDA is NOT an XBRL concept, so the subject falls back
    # to Bloomberg here (the valuation engine uses it whenever the filing EBITDA is absent).
    FieldSpec("ebitda_ltm", "EBITDA, LTM (mn)", "currency", "EBITDA (LTM)", "company",
              ("snapshot_multiples", "financial_analysis", "reit_snapshot"), entities=("subject", "peer")),
    FieldSpec("net_income_ltm", "Net income, LTM (mn)", "currency", "NET_INCOME (LTM)", "company",
              ("snapshot_multiples", "financial_analysis"), entities=("peer",)),
    # Book value is SHARED (subject + peer): WALMEX quarterly *releases* omit total equity,
    # so the subject falls back to Bloomberg here (the valuation engine prefers a filing value
    # when one is present). Peers always come from Bloomberg.
    FieldSpec("total_equity", "Total equity / book value (mn)", "currency", "TOTAL_EQUITY", "company",
              ("snapshot_multiples", "financial_analysis"), entities=("subject", "peer")),
    # FCF is SHARED: WALMEX releases don't carry a clean cash-flow statement, so the subject's
    # FCF falls back to Bloomberg when CFO−capex can't be computed reliably from filings.
    FieldSpec("fcf_ltm", "Free cash flow, LTM (mn)", "currency", "FREE_CASH_FLOW (LTM)", "company",
              ("snapshot_multiples", "financial_analysis"), entities=("subject", "peer")),
    # Total assets — subject fallback for ROA (releases carry only a partial balance sheet).
    FieldSpec("total_assets", "Total assets (mn)", "currency", "BS_TOT_ASSET", "company",
              ("financial_analysis",), entities=("subject",)),
]

# Sum-of-the-parts — one comp-implied EV/EBITDA per segment.
_SOTP = [
    FieldSpec("seg_ev_ebitda", "Peer-implied EV/EBITDA for the segment", "x", "peer comp median EV/EBITDA", "segment",
              ("sum_of_the_parts",)),
]

# Replacement value — subject scalar assumptions.
_REPLACEMENT = [
    FieldSpec("replacement_cost_per_sqm", "Replacement cost per m2 of sales floor", "currency", "analyst assumption / build cost", "subject_scalar",
              ("replacement_value",)),
]

# Historical multiples — subject price at each fiscal-year close (industrial EV/EBITDA & bank P/BV).
_HISTORY = [
    FieldSpec("px_fy_close", "Price at fiscal-year close", "price", "PX_LAST @ FY close", "history",
              ("historical_multiples", "bank_historical", "reit_historical")),
]

# Financials (bank) template fields. Fundamentals XBRL supplies (equity/NI) get a subject fallback;
# ratios (NIM/CET1/ROTE/efficiency/cost of risk) and loan/deposit growth are Bloomberg-only.
_FINANCIALS = [
    FieldSpec("book_value", "Book value / total equity (mn)", "currency", "TOTAL_EQUITY", "company",
              ("bank_snapshot", "bank_returns", "bank_historical"), entities=("subject", "peer")),
    FieldSpec("tangible_book", "Tangible book value (mn)", "currency", "TANGIBLE_BOOK_VALUE", "company",
              ("bank_snapshot", "bank_returns"), entities=("subject", "peer")),
    FieldSpec("net_income_ltm", "Net income, LTM (mn)", "currency", "NET_INCOME (LTM)", "company",
              ("bank_snapshot", "bank_returns"), entities=("subject", "peer")),
    # subject-only bank ratios (Bloomberg-only)
    FieldSpec("roe", "Return on equity, %", "pct", "RETURN_COM_EQY", "company",
              ("bank_returns",), entities=("subject",)),
    FieldSpec("rote", "Return on tangible equity, %", "pct", "RETURN_ON_TANG_COM_EQY", "company",
              ("bank_returns",), entities=("subject",)),
    FieldSpec("nim", "Net interest margin, %", "pct", "NET_INT_MARGIN", "company",
              ("bank_returns",), entities=("subject",)),
    FieldSpec("efficiency_ratio", "Efficiency ratio (cost/income), %", "pct", "EFFICIENCY_RATIO", "company",
              ("bank_returns",), entities=("subject",)),
    FieldSpec("cost_of_risk", "Cost of risk, %", "pct", "provisions / avg loans", "company",
              ("bank_returns",), entities=("subject",)),
    FieldSpec("cet1", "CET1 capital ratio, %", "pct", "CET1_RATIO", "company",
              ("bank_returns",), entities=("subject",)),
]

# Macro/sector — one value per macro row declared in the spec.
_MACRO = [
    FieldSpec("value", "Macro/sector series value", "index", "per the spec row", "macro",
              ("macro_sector",)),
]

# REIT / FIBRA template fields. FFO/AFFO/NAV/distribution aren't XBRL concepts → Bloomberg;
# revenue/net_income/assets/equity come from XBRL.
_REIT = [
    FieldSpec("ffo", "Funds from operations, LTM (mn)", "currency", "FFO", "company",
              ("reit_snapshot", "reit_historical"), entities=("subject", "peer")),
    FieldSpec("affo", "Adjusted FFO, LTM (mn)", "currency", "AFFO", "company",
              ("reit_snapshot",), entities=("subject", "peer")),
    FieldSpec("distribution_yield", "Distribution yield, %", "pct", "DVD_YLD / distribution", "company",
              ("reit_snapshot",), entities=("subject", "peer")),
    # subject-only portfolio metrics
    FieldSpec("noi", "Net operating income, LTM (mn)", "currency", "NOI", "company",
              ("reit_metrics",), entities=("subject",)),
    FieldSpec("occupancy", "Occupancy, %", "pct", "occupancy", "company",
              ("reit_metrics",), entities=("subject",)),
]

# Opt-in annual time-series (A1) — a terminal user CAN supply per-FY fundamentals to gap-fill the
# analysis blocks where cached XBRL is thin. Gated behind `spec.timeseries_years` (default 0 →
# emits NOTHING, so the 138 residual templates get no new blank cells). ``ebit`` maps to the
# engine's ``operating_income`` key on ingest.
_TIMESERIES_FIELDS = [
    ("revenue", "Revenue"), ("ebit", "EBIT"), ("ebitda", "EBITDA"),
    ("gross_profit", "Gross profit"), ("net_income", "Net income"),
    ("cfo", "CFO"), ("capex", "Capex"), ("fcf", "FCF"),
    ("inventory", "Inventory"), ("current_assets", "Current assets"),
    ("current_liabilities", "Current liabilities"),
]
_TIMESERIES = [
    FieldSpec(key, f"{label}, annual (mn)", "currency", f"{label} per FY", "timeseries",
              ("profitability", "fcf_liquidity", "temporal_ebit", "growth"))
    for key, label in _TIMESERIES_FIELDS
]

REGISTRY: list[FieldSpec] = (
    _MARKET + _ESTIMATES + _PEER_FUNDAMENTALS + _SOTP + _REPLACEMENT + _HISTORY + _MACRO
    + _FINANCIALS + _REIT + _TIMESERIES
)


# ---------------------------------------------------------------------------
# Template rows (expansion of the registry against a spec)
# ---------------------------------------------------------------------------


@dataclass
class TemplateRow:
    entity: str        # slug of subject/peer, segment key, macro key, or subject slug for history
    entity_kind: str   # subject | peer | segment | macro | history
    field: str
    period: str        # "" except history rows (the FY, e.g. "2024")
    label: str
    unit: str
    bbg_hint: str


def history_years(spec, base_year: int | None = None) -> list[int]:
    """The fiscal years used for the historical-multiples band (most recent first)."""
    if base_year is None:
        base_year = _dt.date.today().year
    # Most-recent completed FY assumed to be base_year - 1.
    top = base_year - 1
    n = max(1, int(getattr(spec, "history_years", 5)))
    return list(range(top, top - n, -1))


def cell_id(row: "TemplateRow") -> str:
    """Stable id for a template cell, matching src.coverage.native.filled_cells."""
    if row.entity_kind == "history":
        return f"history:{row.entity}:{row.field}@{row.period}"
    if row.entity_kind == "timeseries":
        return f"timeseries:{row.entity}:{row.field}@{row.period}"
    if row.entity_kind == "macro":
        return f"macro:{row.entity}:value"
    return f"{row.entity_kind}:{row.entity}:{row.field}"


def required_rows(spec, base_year: int | None = None, filled: set | None = None) -> list[TemplateRow]:
    """Expand the registry against a coverage spec → the template cells to request. When ``filled``
    is given (cell ids the native layer already covers), those cells are omitted → the emitted
    template is the Bloomberg RESIDUAL only."""
    enabled = set(spec.blocks)
    out: list[TemplateRow] = []

    def add(row: TemplateRow):
        if filled is None or cell_id(row) not in filled:
            out.append(row)

    for f in REGISTRY:
        if not (set(f.blocks) & enabled):
            continue
        if f.scope == "company":
            if "subject" in f.entities:
                add(TemplateRow(spec.slug, "subject", f.key, "", f.label, f.unit, f.bbg_hint))
            if "peer" in f.entities:
                for p in spec.peers:
                    add(TemplateRow(p.slug, "peer", f.key, "",
                                    f"{p.name} — {f.label}", f.unit, f.bbg_hint))
        elif f.scope == "subject_scalar":
            add(TemplateRow(spec.slug, "subject", f.key, "", f.label, f.unit, f.bbg_hint))
        elif f.scope == "segment":
            for seg in spec.segments:
                add(TemplateRow(seg.key, "segment", f.key, "",
                                f"{seg.label} — {f.label}", f.unit, f.bbg_hint))
        elif f.scope == "macro":
            for m in spec.macro:
                add(TemplateRow(m.key, "macro", "value", "", m.label, f.unit, f.bbg_hint))
        elif f.scope == "history":
            for yr in history_years(spec, base_year):
                add(TemplateRow(spec.slug, "history", f.key, str(yr),
                                f"{f.label} FY{yr}", f.unit, f.bbg_hint))
        elif f.scope == "timeseries":
            # Opt-in only: emit nothing unless the spec asks for ≥1 year of annual series.
            n = max(0, int(getattr(spec, "timeseries_years", 0) or 0))
            if n <= 0:
                continue
            base = base_year if base_year is not None else _dt.date.today().year
            top = base - 1
            for yr in range(top, top - n, -1):
                add(TemplateRow(spec.slug, "timeseries", f.key, str(yr),
                                f"{f.label} FY{yr}", f.unit, f.bbg_hint))
    return out


# ---------------------------------------------------------------------------
# The ingested pack
# ---------------------------------------------------------------------------


@dataclass
class BloombergPack:
    """Structured, validated view of the filled template."""
    slug: str
    subject: dict[str, float] = field(default_factory=dict)          # field -> value
    peers: dict[str, dict[str, float]] = field(default_factory=dict)  # peer_slug -> {field: value}
    segment_multiples: dict[str, float] = field(default_factory=dict)  # segment_key -> ev/ebitda
    history: dict[int, float] = field(default_factory=dict)           # FY -> px_fy_close
    macro: dict[str, float] = field(default_factory=dict)             # macro_key -> value
    # Opt-in annual time-series gap-filler (A1): FY -> {engine_field -> value}. Empty by default;
    # the analysis blocks read fund.annual first, then fall back to this.
    timeseries: dict[int, dict[str, float]] = field(default_factory=dict)
    # Set when the ingested residual pack looks like hand-typed PLACEHOLDER data (dummy round
    # numbers) rather than real Bloomberg values — so the gate can refuse to call it complete.
    suspect_placeholder: bool = False
    placeholder_reasons: list = field(default_factory=list)
    # Subject fields filled from the Yahoo key-stats fallback (not the filing/XBRL path) — carried so
    # the workbook/validation can label those cells honestly by provenance.
    yahoo_filled: set = field(default_factory=set)
    # Yahoo's own computed statistics (shares_out, price_to_book, trailing_pe, ev_ebitda, …) — kept as
    # an independent oracle so the validator can corroborate a suspiciously-cheap multiple against a
    # second source before flagging it (a real sub-book name vs a share-count artifact).
    yahoo_stats: dict = field(default_factory=dict)

    def get_subject(self, field_key: str) -> float | None:
        return self.subject.get(field_key)
