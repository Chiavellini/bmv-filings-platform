"""
financial_model.py — Comprehensive financial metric registry for any company.

Defines 60+ metrics spanning income statement, balance sheet, cash flow, and
derived ratios, each with multi-language (EN/ES) regex patterns. Company-specific
overrides and custom KPIs are loaded from YAML configs via load_config().
"""

from __future__ import annotations

import ast
import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Building blocks shared with extract_metrics.py
# ---------------------------------------------------------------------------

# Generic signed number: 1,234.5 | (1,234.5) | ( 94,052) | -94,052 | 14.3%
_N = r"[\(\-]?\s?[\d,]+\.?\d*\%?\)?"

# Large number — must have at least one thousands-separator comma.
# Rejects: year-numbers ("2019"), single-digit split artifacts ("5"),
#          and small footnote markers.
_NL = r"[\(\-]?\s?[\d]{1,3}(?:,[\d]{3})+\.?\d*\)?"

# Footnote marker: pdfplumber renders PDF superscripts as a lone digit between
# numbers (e.g. "192,119 1 39,062"). Matches: optional (spaces + single digit
# NOT followed by another digit, dot, or comma).
_FN = r"(?:\s+\d(?![\d.,]))?"


@dataclass
class PatternSpec:
    regex: str
    multiplier: float = 1.0   # 1000 for prose "millones", 1_000_000 for "billions"
    source: str = "table"     # "table" | "prose" — informs confidence scoring


AGGREGATIONS = frozenset({"sum", "ending", "average", "none"})

# Count metrics are normally period-end stocks.  These exceptions are period
# flows and therefore belong in a guarded FY sum instead.  Company configs may
# override either default explicitly with ``aggregation``.
_FLOW_COUNT_KEYS = {
    "net_new_stores", "stores_opened", "stores_closed", "openings", "closures",
    "clubs_sw_openings", "clubs_sw_closures", "club_openings", "club_closures",
}

# Stock-like metrics sometimes live in a custom ``kpi`` section rather than the
# balance sheet.  Key-level defaults keep those rows from being accidentally
# summed when a caller only has a key/unit map available.
_ENDING_KEYS = {
    "cash", "accounts_receivable", "inventory", "prepaid", "current_assets",
    "ppe_net", "intangibles", "goodwill", "right_of_use", "non_current_assets",
    "total_assets", "accounts_payable", "short_term_debt", "current_liabilities",
    "long_term_debt", "lease_liabilities", "total_debt", "non_current_liabilities",
    "total_liabilities", "equity", "minority_interest", "retained_earnings",
    "net_debt", "shares_outstanding", "employees", "total_units",
    "total_sales_floor", "clubs_count", "plants", "dist_centers",
    "capex_annual",
}


def default_metric_aggregation(key: str, section: str | None, unit: str | None) -> str:
    """Return the safe FY aggregation for a metric.

    ``sum`` is reserved for flows, ``ending`` uses the Q4/period-end value,
    ``average`` requires all four quarters, and ``none`` leaves FY blank unless
    a derived formula can be evaluated against other FY rows.
    """
    key = str(key or "").strip().lower()
    section = str(section or "").strip().lower()
    unit = str(unit or "").strip().lower()
    if key in _FLOW_COUNT_KEYS:
        return "sum"
    if section == "balance" or key in _ENDING_KEYS or unit == "count":
        return "ending"
    if section == "ratio" or unit in {"pct", "ratio", "per_share"}:
        return "none"
    if section in {"income", "cashflow"} or unit in {"currency", "volume"}:
        return "sum"
    return "none"


@dataclass
class MetricDef:
    key: str                           # canonical snake_case identifier
    label: str                         # English display label
    label_es: str                      # Spanish display label
    section: str                       # "income" | "balance" | "cashflow" | "ratio" | "kpi"
    unit: str                          # "currency" | "pct" | "count" | "ratio" | "per_share"
    patterns: list[PatternSpec]        # tried in order; first match wins
    calc: str | None = None            # formula for derived metrics ("gross_profit / revenue")
    validate: list[str] = field(default_factory=list)  # rules from validator.py to apply
    xbrl_concepts: list[str] = field(default_factory=list)  # Tier 1: ordered IFRS concept keys (first present wins)
    aliases: list[str] = field(default_factory=list)        # Tier 2: row labels for table-cell matching
    aggregation: str | None = None      # FY: sum | ending | average | none

    def __post_init__(self) -> None:
        aggregation = self.aggregation or default_metric_aggregation(
            self.key, self.section, self.unit,
        )
        aggregation = str(aggregation).strip().lower()
        if aggregation not in AGGREGATIONS:
            allowed = ", ".join(sorted(AGGREGATIONS))
            raise ValueError(
                f"Invalid aggregation {self.aggregation!r} for {self.key}; "
                f"expected one of: {allowed}"
            )
        self.aggregation = aggregation


def _p(regex: str, mult: float = 1.0, src: str = "table") -> PatternSpec:
    return PatternSpec(regex=regex, multiplier=mult, source=src)


# ---------------------------------------------------------------------------
# Helper: prose pattern builder
# Constructs patterns for  "metric ... reached/totaled ... $X million/billion"
# ---------------------------------------------------------------------------

def _prose(en_verbs: str, es_verbs: str, scale_hint: str = "millones") -> list[PatternSpec]:
    """Return prose PatternSpecs for EN + ES with correct multipliers."""
    mult = {"millones": 1_000, "billions": 1_000_000, "miles": 1.0}.get(scale_hint, 1_000)
    return [
        _p(
            rf"(?:{en_verbs})\s+(?:\S+\s+){{0,5}}\$?\s*([\d.]+)\s+(?:billion|million)s?",
            mult if scale_hint != "billions" else 1_000_000,
            "prose",
        ),
        _p(
            rf"(?:{es_verbs})\s+(?:\S+\s+){{0,5}}\$\s*([\d.]+)\s+{scale_hint}",
            mult,
            "prose",
        ),
    ]


# ---------------------------------------------------------------------------
# Master metric catalog
# ---------------------------------------------------------------------------

METRICS: list[MetricDef] = [

    # ===================================================================
    # INCOME STATEMENT
    # ===================================================================

    MetricDef(
        key="revenue",
        label="Total Revenue",
        label_es="Ingresos Totales",
        section="income",
        unit="currency",
        validate=["sanity_revenue_positive", "sanity_yoy_change", "revenue_segment_sum"],
        patterns=[
            # English table
            _p(r"^\s*(?:Total\s+Revenue|Net\s+Revenue|Net\s+Sales|Total\s+Net\s+Revenue)\s+"
               r"({NL}){FN}\s+({NL}){FN}\s+(?:{N}\s+)?({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Total\s+Revenue|Net\s+Revenue|Net\s+Sales)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            # Spanish table
            _p(r"^\s*(?:Total\s+de\s+[Ii]ngresos|[Ii]ngresos\s+[Tt]otales|"
               r"[Ii]ngresos\s+[Nn]etos|[Vv]entas\s+[Nn]etas)\s+"
               r"({NL}){FN}\s+({NL}){FN}\s+(?:{N}\s+)?({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Total\s+de\s+[Ii]ngresos|[Ii]ngresos\s+[Tt]otales|"
               r"[Ii]ngresos\s+[Nn]etos)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            # 2016-2018 SPORT legacy label
            _p(r"^\s*Total\s+de\s+[Ii]ngresos\s+[Nn]etos\s+"
               r"({NL})\s+({NL})".format(NL=_NL)),
            # English prose
            _p(r"[Rr]evenue[s]?\s+(?:\S+\s+){0,5}"
               r"(?:reached|totaled?|was|were|of)\s+\$\s*([\d.]+)\s+(?:billion|million)s?",
               1_000, "prose"),
            # Spanish prose
            _p(r"[Ii]ngresos\s+[Tt]otales\s+(?:\S+\s+){0,5}"
               r"(?:alcanzaron?|finalizaron?|ascendieron?)\s+(?:\S+\s+){0,4}"
               r"\$\s*([\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="cogs",
        label="Cost of Revenue",
        label_es="Costo de Ventas",
        section="income",
        unit="currency",
        validate=["gross_profit_identity"],
        patterns=[
            _p(r"^\s*(?:Cost\s+of\s+(?:Revenue|Goods\s+Sold|Sales)|COGS)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Costo\s+de\s+(?:Ventas|Productos\s+Vendidos|Servicios))\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="gross_profit",
        label="Gross Profit",
        label_es="Utilidad Bruta",
        section="income",
        unit="currency",
        calc="revenue - cogs",
        validate=["gross_profit_identity"],
        patterns=[
            _p(r"^\s*(?:Gross\s+Profit|Gross\s+Income)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Utilidad\s+Bruta|Contribuci[oó]n\s+Marginal\s+(?:de\s+clubes)?)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="gross_margin",
        label="Gross Margin %",
        label_es="Margen Bruto",
        section="income",
        unit="pct",
        calc="gross_profit / revenue * 100",
        validate=["margin_consistency"],
        patterns=[
            _p(r"^\s*(?:Gross\s+(?:Profit\s+)?Margin|Gross\s+Margin\s+[%]?)\s+({N})\s+({N})".format(N=_N)),
            _p(r"^\s*(?:Margen\s+(?:de\s+)?(?:Utilidad\s+)?Bruta?|"
               r"Contribuci[oó]n\s+[Mm]arginal\s+de\s+clubes\s+\(%\))\s+({N})\s+({N})".format(N=_N)),
        ],
    ),

    MetricDef(
        key="sga",
        label="SG&A Expenses",
        label_es="Gastos de Venta y Administración",
        section="income",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Selling[\s,]+General\s+(?:and\s+)?Administrative|SG&?A|"
               r"Operating\s+Expenses)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Gastos\s+(?:de\s+Venta\s+y\s+Administraci[oó]n|Corporativos|"
               r"Administrativos))\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="rd_expense",
        label="R&D Expense",
        label_es="Gastos de I+D",
        section="income",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Research\s+(?:and\s+)?Development|R&?D\s+Expense)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Gastos\s+de\s+Investigaci[oó]n\s+y\s+Desarrollo)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="operating_expense",
        label="Total Operating Expenses",
        label_es="Gastos de Operación (excl. D&A, sin IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Total\s+Operating\s+Expenses?|Operating\s+Costs?)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            # Spanish prose (cross-line supported via \n?)
            _p(r"[Gg]astos\s+(?:[Tt]otales\s+de\s+)?[Oo]peraci[oó]n\s*\d?"
               r"[^\n]*?excluyendo[^\n]*?\n?[^\n]*?\$\s*([\d.]+)\s+millones",
               1_000, "prose"),
            _p(r"[Ss]i\s+excluimos\s+(?:el\s+efecto\s+de\s+)?IFRS\s*16"
               r"[^\n]*?\$\s*([\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="operating_income",
        label="Operating Income (EBIT)",
        label_es="Utilidad de Operación",
        section="income",
        unit="currency",
        validate=["ebitda_derivation"],
        patterns=[
            _p(r"^\s*(?:Operating\s+(?:Income|Profit|Earnings?)|EBIT)\s+"
               r"({NL}){FN}\s+({N}){FN}\s+(?:{N}\s+)?({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Operating\s+(?:Income|Profit|Earnings?)|EBIT)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:\(?P[eé]rdida\)?\s+)?[Uu]tilidad\s+de\s+[Oo]peraci[oó]n\s+"
               r"({NL}){FN}\s+({N}){FN}\s+(?:{N}\s+)?({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:\(?P[eé]rdida\)?\s+)?[Uu]tilidad\s+de\s+[Oo]peraci[oó]n\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"[Uu]tilidad\s+de\s+[Oo]peraci[oó]n[\s\S]{0,1000}?"
               r"Considerando\s+el\s+IFRS\s*16[\s\S]{0,240}?"
               r"(?:llegando|terminando|finalizando|alcanzando|ascendiendo)"
               r"\s+(?:a|en)?\s*\$?\s*([\d.]+)\s+millones", 1_000, "prose"),
            _p(r"[Uu]tilidad\s+de\s+[Oo]peraci[oó]n[\s\S]{0,80}?"
               r"en\s+el\s+(?:trimestre|\d+T\d{2,4})(?![\s\S]{0,80}?sin\s+considerar)[\s\S]{0,80}?"
               r"(?:fue|alcanz[oó]|finaliz[oó]|termin[oó]|cerr[oó])\s+"
               r"(?:de\s+|en\s+)?\$?\s*([\d.]+)\s+millones", 1_000, "prose"),
            _p(r"[Uu]tilidad\s+de\s+[Oo]peraci[oó]n\s+"
               r"(?:fue|alcanz[oó]|finaliz[oó]|termin[oó]|cerr[oó])\s+"
               r"(?:de\s+|en\s+)?\$?\s*([\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="operating_margin",
        label="Operating Margin %",
        label_es="Margen de Utilidad de Operación",
        section="income",
        unit="pct",
        calc="operating_income / revenue * 100",
        validate=["margin_consistency"],
        patterns=[
            _p(r"^\s*(?:Operating\s+(?:Profit\s+)?Margin|EBIT\s+Margin)\s+({N})\s+({N})".format(N=_N)),
            _p(r"^\s*[Mm]argen\s+de\s+[Uu]tilidad\s+de\s+[Oo]peraci[oó]n\s+({N})\s+({N})".format(N=_N)),
        ],
    ),

    MetricDef(
        key="ebitda",
        label="EBITDA",
        label_es="EBITDA / UAFIDA (con IFRS 16)",
        section="income",
        unit="currency",
        validate=["ebitda_derivation", "margin_consistency", "sanity_yoy_change"],
        patterns=[
            # Table (handles superscript artifacts via _FN)
            _p(r"^\s*(?:EBITDA|UAFIDA)\s+"
               r"({NL}){FN}\s+({N}){FN}\s+(?:{N}\s+)?({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:EBITDA|UAFIDA)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            # EN prose
            _p(r"EBITDA\s+(?:was|totaled?|reached|of)\s+\$\s*([\d.]+)\s+(?:billion|million)s?",
               1_000, "prose"),
            # ES prose — "finalizó/cerró el trimestre en $41.2 millones"
            _p(r"[Ee][Bb][Ii][Tt][Dd][Aa]\s+(?:finalizó|cerró)\s+(?:el\s+trimestre\s+en|en)\s+"
               r"\$\s*([\d.]+)\s+millones", 1_000, "prose"),
            # ES prose — "EBITDA (IFRS 16) alcanzó $181.9 millones" (2019+ format with parenthetical)
            _p(r"[Ee][Bb][Ii][Tt][Dd][Aa]\s+\(?(?:IFRS\s*16|con\s+IFRS\s*16)\)?"
               r"\s+alcanzó\s+\$?\s*([\d.]+)\s+millones", 1_000, "prose"),
            # ES prose — "EBITDA alcanzó $97.3 millones" (2018 no qualifier)
            _p(r"[Ee][Bb][Ii][Tt][Dd][Aa]\s+alcanzó\s+\$\s*([\d.]+)\s+millones", 1_000, "prose"),
            # ES prose — "EBITDA considerando IFRS 16 alcanzó los $213.9 millones"
            _p(r"[Ee][Bb][Ii][Tt][Dd][Aa]\s+(?:considerando|con|incluyendo)\s+"
               r"(?:el\s+efecto\s+de\s+)?IFRS\s*16\s+(?:alcanzó|finalizó|cerró|fue)[^\n]*?"
               r"\$\s*([\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="ebitda_sin_ifrs",
        label="EBITDA ex-IFRS 16",
        label_es="EBITDA sin IFRS 16",
        section="income",
        unit="currency",
        patterns=[
            # NOTE: number must immediately follow the verb (tight \s*, no [\s\S] jump). In SPORT's
            # two-column PDFs a loose jump skipped the quarterly figure and grabbed an interleaved
            # number (cash) or the acumulado/YTD figure. Tight adjacency avoids both; SPORT-specific
            # anchored patterns (configs/sport.yaml) handle the clean quarterly cases first.
            _p(r"[Ee][Bb][Ii][Tt][Dd][Aa]\s+sin\s+(?:considerar\s+)?(?:el\s+efecto\s+de\s+)?IFRS\s*16"
               r"\s+(?:cerró|fue|finalizó|alcanzó|totalizó|terminó|terminando|llegó|llegando)"
               r"(?:\s+el\s+trimestre)?(?:\s+en|\s+a|\s+de)?\s*"
               r"(-?\$?\s?[\d.]+)\s+millones",
               1_000, "prose"),
            _p(r"[Ee][Bb][Ii][Tt][Dd][Aa]\s+\(sin\s+considerar\s+(?:el\s+efecto\s+de\s+)?IFRS\s*16\)"
               r"\s+(?:fue|totalizó|finalizó|alcanzó|terminó|cerró)\s+"
               r"(?:el\s+trimestre\s+en|en|de|a)?\s*"
               r"(-?\$?\s?[\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="ebitda_margin",
        label="EBITDA Margin %",
        label_es="Margen EBITDA (con IFRS 16)",
        section="income",
        unit="pct",
        calc="ebitda / revenue * 100",
        validate=["margin_consistency"],
        patterns=[
            # ES prose 2026: "sin IFRS ... 16.5% y con IFRS ... 36.3%" — capture CON IFRS only
            _p(r"[Mm]argen\s+(?:EBITDA|UAFIDA)[^\n]*?sin\s+IFRS[^\n]*?[\d.]+%"
               r"[^\n]*?con\s+IFRS[^\n]*?([\d.]+)%"),
            # ES prose 2024: "margen EBITDA para el trimestre fue del 37.1%"
            _p(r"[Mm]argen\s+(?:EBITDA|UAFIDA)\s+para\s+el\s+trimestre\s+fue\s+del?\s+([\d.]+)%"),
            # Table (first column = CON IFRS 16 for 2016-2025)
            _p(r"^\s*(?:Margen\s+EBITDA|Margen\s+UAFIDA|Margen\s+de\s+UAFIDA|EBITDA\s+Margin)\s+"
               r"({N})\s+({N})".format(N=_N)),
            # EN prose
            _p(r"EBITDA\s+[Mm]argin\s+(?:was|of|reached)\s+([\d.]+)%"),
            # ES generic
            _p(r"[Mm]argen\s+(?:EBITDA|UAFIDA)\s+(?:fue|es)\s+(?:del?\s+)?([\d.]+)%"),
        ],
    ),

    MetricDef(
        key="ebitda_margin_sin_ifrs",
        label="EBITDA Margin ex-IFRS 16 %",
        label_es="Margen EBITDA sin IFRS 16",
        section="income",
        unit="pct",
        patterns=[
            _p(r"[Mm]argen\s+(?:EBITDA|UAFIDA)[^\n]*?sin\s+(?:considerar\s+)?IFRS\s*16"
               r"[^\n]*?pas[oó]\s+de[\s\S]{0,120}?\b(?:a|en)\s+([\d.]+)%"),
            _p(r"[Mm]argen\s+(?:EBITDA|UAFIDA)[^\n]*?sin\s+(?:considerar\s+)?IFRS\s*16"
               r"[\s\S]{0,120}?(?:fue|es|del?|de)\s+([\d.]+)%"),
        ],
    ),

    MetricDef(
        key="depreciation",
        label="Depreciation & Amortization",
        label_es="Depreciación y Amortización",
        section="income",
        unit="currency",
        validate=["ebitda_derivation"],
        patterns=[
            _p(r"^\s*(?:Depreciation\s+(?:and\s+)?Amortization|D&?A)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*[Dd]epreciaci[oó]n\s+y\s+[Aa]mortizaci[oó]n\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="interest_expense",
        label="Interest Expense",
        label_es="Gastos por Intereses",
        section="income",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Interest\s+Expense|Finance\s+Costs?)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Gastos\s+(?:por\s+)?[Ii]ntereses|[Cc]osto\s+[Ff]inanciero\s*[-–]\s*[Nn]eto)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="interest_income",
        label="Interest Income",
        label_es="Ingresos por Intereses",
        section="income",
        unit="currency",
        patterns=[
            _p(r"^\s*Interest\s+Income\s+({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*[Ii]ngresos\s+por\s+[Ii]ntereses\s*\d?\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="ebt",
        label="Earnings Before Tax",
        label_es="Utilidad Antes de Impuestos",
        section="income",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Earnings?\s+Before\s+(?:Income\s+)?Tax|EBT|Pre-?[Tt]ax\s+(?:Income|Profit))\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:\(?P[eé]rdida\)?\s+)?[Uu]tilidad\s+[Aa]ntes\s+de\s+[Ii]mpuestos\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="tax_expense",
        label="Income Tax Expense",
        label_es="Impuestos a la Utilidad",
        section="income",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Income\s+Tax(?:\s+Expense)?|Tax\s+(?:Provision|Expense))\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*[Ii]mpuestos\s+a\s+la\s+[Uu]tilidad\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="net_income",
        label="Net Income",
        label_es="Utilidad Neta",
        section="income",
        unit="currency",
        validate=["sanity_yoy_change"],
        patterns=[
            _p(r"^\s*(?:Net\s+(?:Income|Earnings?|Profit)|Net\s+Loss)\s+"
               r"({NL}){FN}\s+({N}){FN}\s+(?:{N}\s+)?({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Net\s+(?:Income|Earnings?|Profit)|Net\s+Loss)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:\(?P[eé]rdida\)?\s+)?[Uu]tilidad\s+(?:del\s+[Ee]jercicio|[Nn]eta)\s+"
               r"({NL}){FN}\s+({N}){FN}\s+(?:{N}\s+)?({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:\(?P[eé]rdida\)?\s+)?[Uu]tilidad\s+(?:del\s+[Ee]jercicio|[Nn]eta)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="net_margin",
        label="Net Margin %",
        label_es="Margen de Utilidad Neta",
        section="income",
        unit="pct",
        calc="net_income / revenue * 100",
        validate=["margin_consistency"],
        patterns=[
            _p(r"^\s*(?:Net\s+(?:Profit\s+)?Margin|Net\s+Income\s+Margin)\s+({N})\s+({N})".format(N=_N)),
            _p(r"^\s*[Mm]argen\s+de\s+[Uu]tilidad\s+(?:del\s+[Ee]jercicio|[Nn]eta?)\s+"
               r"({N})\s+({N})".format(N=_N)),
        ],
    ),

    MetricDef(
        key="eps",
        label="Earnings Per Share",
        label_es="Utilidad Por Acción (UPA)",
        section="income",
        unit="per_share",
        patterns=[
            _p(r"^\s*(?:Earnings?\s+Per\s+Share|EPS|Basic\s+EPS)\s+({N})\s+({N})".format(N=_N)),
            _p(r"^\s*(?:Utilidad\s+[Pp]or\s+[Aa]cci[oó]n|UPA)\s*\d?\s+({N})\s+({N})".format(N=_N)),
        ],
    ),

    MetricDef(
        key="shares_outstanding",
        label="Shares Outstanding",
        label_es="Acciones en Circulación",
        section="income",
        unit="count",
        patterns=[
            _p(r"^\s*(?:(?:Weighted\s+)?Average\s+)?[Ss]hares?\s+[Oo]utstanding\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Aa]cciones\s+en\s+[Cc]irculaci[oó]n\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="dividends_per_share",
        label="Dividends Per Share",
        label_es="Dividendos Por Acción",
        section="income",
        unit="per_share",
        patterns=[
            _p(r"^\s*(?:Dividends?\s+(?:Per\s+Share|Declared|Paid))\s+({N})\s+({N})".format(N=_N)),
            _p(r"^\s*[Dd]ividendos\s+[Pp]or\s+[Aa]cci[oó]n\s+({N})\s+({N})".format(N=_N)),
        ],
    ),

    # ===================================================================
    # BALANCE SHEET
    # ===================================================================

    MetricDef(
        key="cash",
        label="Cash & Equivalents",
        label_es="Efectivo y Equivalentes",
        section="balance",
        unit="currency",
        validate=["net_debt_identity"],
        patterns=[
            _p(r"^\s*(?:Cash\s+(?:and\s+)?(?:Cash\s+)?Equivalents?|"
               r"Cash\s+and\s+Short-?[Tt]erm\s+Investments?)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Ee]fectivo\s+y\s+[Ee]quivalentes?\s+(?:de\s+[Ee]fectivo\s+)?"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"[Ee]fectivo\s+y\s+[Ee]quivalentes?\s+(?:\S+\s+){0,6}"
               r"(?:fue|finaliz[oó]|alcanz[oó]|cerr[oó]|registr[oó])[\s\S]{0,120}?"
               r"\$\s*([\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="accounts_receivable",
        label="Accounts Receivable",
        label_es="Cuentas por Cobrar",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Accounts?\s+Receivable|Trade\s+Receivables?)\s*,?\s*(?:net)?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Cc]uentas?\s+por\s+[Cc]obrar\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="inventory",
        label="Inventory",
        label_es="Inventarios",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*Inventories?(?:\s*,\s*net)?\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Ii]nventarios?\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="prepaid",
        label="Prepaid Expenses",
        label_es="Pagos Anticipados",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*Prepaid\s+(?:Expenses?|Assets?)\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Pp]agos?\s+[Aa]nticipados?\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="current_assets",
        label="Total Current Assets",
        label_es="Activo Circulante Total",
        section="balance",
        unit="currency",
        validate=["current_ratio"],
        patterns=[
            _p(r"^\s*(?:Total\s+)?Current\s+Assets?\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Total\s+)?[Aa]ctivo\s+[Cc]irculante\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="ppe_net",
        label="Property, Plant & Equipment (net)",
        label_es="Propiedades, Planta y Equipo (neto)",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Property,?\s+Plant\s+(?:and\s+)?Equipment|PP&E),?\s*(?:net)?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Pp]ropiedades,?\s+[Pp]lanta\s+y\s+[Ee]quipo\s*(?:,\s*neto)?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="intangibles",
        label="Intangible Assets",
        label_es="Activos Intangibles",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Intangible\s+Assets?|Other\s+Intangibles?)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Aa]ctivos?\s+[Ii]ntangibles?\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="goodwill",
        label="Goodwill",
        label_es="Crédito Mercantil",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*Goodwill\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Cc]r[eé]dito\s+[Mm]ercantil\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="right_of_use",
        label="Right-of-Use Assets (IFRS 16)",
        label_es="Activos por Derecho de Uso",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*Right-?of-?[Uu]se\s+Assets?\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Aa]ctivos?\s+por\s+[Dd]erecho\s+de\s+[Uu]so\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="non_current_assets",
        label="Non-Current Assets",
        label_es="Activo No Circulante",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Total\s+)?Non-?[Cc]urrent\s+Assets?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Total\s+)?[Aa]ctivo\s+[Nn]o\s+[Cc]irculante\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="total_assets",
        label="Total Assets",
        label_es="Total de Activos",
        section="balance",
        unit="currency",
        validate=["balance_sheet_identity"],
        patterns=[
            _p(r"^\s*Total\s+Assets?\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Total\s+de\s+)?[Aa]ctivos?\s+[Tt]otales?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="accounts_payable",
        label="Accounts Payable",
        label_es="Cuentas por Pagar",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Accounts?\s+Payable|Trade\s+Payables?)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Cc]uentas?\s+por\s+[Pp]agar\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="short_term_debt",
        label="Short-Term Debt",
        label_es="Deuda a Corto Plazo",
        section="balance",
        unit="currency",
        validate=["net_debt_identity"],
        patterns=[
            _p(r"^\s*(?:Short-?[Tt]erm\s+(?:Debt|Borrowings?)|Current\s+Portion\s+of\s+(?:Long-?[Tt]erm\s+)?Debt)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Dd]euda\s+a\s+[Cc]orto\s+[Pp]lazo\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="current_liabilities",
        label="Total Current Liabilities",
        label_es="Pasivos Circulantes",
        section="balance",
        unit="currency",
        validate=["current_ratio"],
        patterns=[
            _p(r"^\s*(?:Total\s+)?Current\s+Liabilities?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Total\s+)?[Pp]asivos?\s+[Cc]irculantes?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="long_term_debt",
        label="Long-Term Debt",
        label_es="Deuda a Largo Plazo",
        section="balance",
        unit="currency",
        validate=["net_debt_identity"],
        patterns=[
            _p(r"^\s*Long-?[Tt]erm\s+(?:Debt|Borrowings?)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Dd]euda\s+(?:a\s+)?[Ll]argo\s+[Pp]lazo\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="lease_liabilities",
        label="Lease Liabilities (IFRS 16)",
        label_es="Pasivos por Arrendamiento",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Total\s+)?Lease\s+Liabilities?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Pp]asivos?\s+(?:por\s+)?[Aa]rrendamiento\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="total_debt",
        label="Total Financial Debt",
        label_es="Deuda Financiera Total",
        section="balance",
        unit="currency",
        calc="short_term_debt + long_term_debt",
        validate=["net_debt_identity"],
        patterns=[
            _p(r"^\s*(?:Total\s+)?(?:Financial\s+)?Debt\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Dd]euda\s+[Ff]inanciera\s+[Tt]otal\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="non_current_liabilities",
        label="Non-Current Liabilities",
        label_es="Pasivo No Circulante",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Total\s+)?Non-?[Cc]urrent\s+Liabilities?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Total\s+)?[Pp]asivo\s+[Nn]o\s+[Cc]irculante\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="total_liabilities",
        label="Total Liabilities",
        label_es="Total de Pasivos",
        section="balance",
        unit="currency",
        validate=["balance_sheet_identity"],
        patterns=[
            _p(r"^\s*Total\s+Liabilities?\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Total\s+de\s+)?[Pp]asivos?\s+[Tt]otales?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="equity",
        label="Total Shareholders' Equity",
        label_es="Capital Contable Total",
        section="balance",
        unit="currency",
        validate=["balance_sheet_identity"],
        patterns=[
            _p(r"^\s*(?:Total\s+)?(?:Shareholders?'?\s+)?Equity\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Total\s+)?[Cc]apital\s+[Cc]ontable\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="minority_interest",
        label="Non-controlling Interest",
        label_es="Participación No Controladora",
        section="balance",
        unit="currency",
        # Tier-1 from XBRL (ifrs-full_NoncontrollingInterests, wired via configs/xbrl_concepts.yaml);
        # prose/table fallbacks for filings that print it on the equity face.
        # (Back-ported from the soft fork — its native EV bridge consumes this key.)
        patterns=[
            _p(r"^\s*(?:Total\s+)?Non-?controlling\s+[Ii]nterests?\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*Participaci[oó]n\s+(?:no\s+controladora|minoritaria)\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="retained_earnings",
        label="Retained Earnings",
        label_es="Utilidades Retenidas",
        section="balance",
        unit="currency",
        patterns=[
            _p(r"^\s*Retained\s+Earnings?\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Uu]tilidades?\s+[Rr]etenidas?\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="net_debt",
        label="Net Debt",
        label_es="Deuda Financiera Neta",
        section="balance",
        unit="currency",
        calc="total_debt - cash",
        validate=["net_debt_identity"],
        patterns=[
            _p(r"^\s*Net\s+Debt\s+({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"^\s*[Dd]euda\s+[Ff]inanciera\s+[Nn]eta\s+"
               r"({NL}){FN}\s+({NL})".format(NL=_NL, FN=_FN)),
            _p(r"[Ii]ncluyendo\s+el\s+IFRS\s*16[\s\S]{0,120}?"
               r"[Dd]euda\s+[Ff]inanciera\s+[Nn]eta[\s\S]{0,120}?"
               r"(?:ascendió|fue|alcanzó|finalizó)\s+(?:a|en)?\s*\$?\s*([\d,.]+)\s+millones",
               1_000, "prose"),
            _p(r"[Aa]l\s+mes\s+de\s+[^\n,]+,\s*la\s+[Dd]euda\s+[Ff]inanciera\s+[Nn]eta\s+ascendió\s+a?\s*\$?\s*([\d,.]+)\s+millones",
               1_000, "prose"),
            _p(r"[Dd]euda\s+[Ff]inanciera\s+[Nn]eta\s+al\s+cierre[\s\S]{0,120}?"
               r"(?:ascendió|fue|alcanzó|finalizó)\s+(?:a|en)?\s*\$?\s*([\d,.]+)\s+millones",
               1_000, "prose"),
            _p(r"[Aa]l\s+cierre[\s\S]{0,120}?la\s+[Dd]euda\s+[Ff]inanciera\s+[Nn]eta[\s\S]{0,120}?"
               r"(?:ascendió|fue|alcanzó|finalizó)\s+(?:a|en)?\s*\$?\s*([\d,.]+)\s+millones",
               1_000, "prose"),
        ],
    ),

    # ===================================================================
    # CASH FLOW STATEMENT
    # ===================================================================

    MetricDef(
        key="cfo",
        label="Operating Cash Flow",
        label_es="Flujo de Efectivo de Operación",
        section="cashflow",
        unit="currency",
        validate=["fcf_derivation"],
        patterns=[
            _p(r"^\s*(?:(?:Net\s+)?Cash\s+(?:Provided\s+by|[Ff]rom)\s+Operating\s+Activities?|"
               r"Operating\s+Cash\s+Flow)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*[Ff]lujo\s+de\s+[Ee]fectivo\s+(?:de|generado\s+en)\s+[Aa]ctividades?\s+"
               r"de\s+[Oo]peraci[oó]n\s+({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="capex",
        label="Capital Expenditures",
        label_es="CAPEX / Inversión en Activos Fijos",
        section="cashflow",
        unit="currency",
        validate=["fcf_derivation"],
        patterns=[
            _p(r"^\s*(?:Capital\s+Expenditures?|CAPEX|Purchases?\s+of\s+(?:PP&E|Property))\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:CAPEX|[Ii]nversi[oó]n\s+en\s+[Aa]ctivos?\s+[Ff]ijos?)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"(?:CAPEX|capital\s+expenditures?|[Ii]nversi[oó]n\s+en\s+activos?\s+fijos?)"
               r"[^\n]*?\$\s*([\d.]+)\s+(?:million|millon|millones)", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="free_cash_flow",
        label="Free Cash Flow",
        label_es="Flujo de Caja Libre",
        section="cashflow",
        unit="currency",
        calc="cfo - capex",
        validate=["fcf_derivation"],
        patterns=[
            _p(r"^\s*Free\s+Cash\s+Flow\s+({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:Flujo\s+de\s+(?:Caja|Efectivo)\s+Libre)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="cfi",
        label="Investing Cash Flow",
        label_es="Flujo de Efectivo de Inversión",
        section="cashflow",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:(?:Net\s+)?Cash\s+(?:Used\s+in|[Ff]rom)\s+Investing\s+Activities?)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*[Ff]lujo\s+de\s+[Ee]fectivo\s+(?:de|usado\s+en)\s+[Aa]ctividades?\s+"
               r"de\s+[Ii]nversi[oó]n\s+({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="cff",
        label="Financing Cash Flow",
        label_es="Flujo de Efectivo de Financiamiento",
        section="cashflow",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:(?:Net\s+)?Cash\s+(?:Used\s+in|[Ff]rom)\s+Financing\s+Activities?)\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*[Ff]lujo\s+de\s+[Ee]fectivo\s+(?:de|usado\s+en)\s+[Aa]ctividades?\s+"
               r"de\s+[Ff]inanciamiento\s+({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="dividends_paid",
        label="Dividends Paid",
        label_es="Dividendos Pagados",
        section="cashflow",
        unit="currency",
        patterns=[
            _p(r"^\s*Dividends?\s+Paid\s+({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*[Dd]ividendos?\s+[Pp]agados?\s+({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="share_repurchases",
        label="Share Repurchases",
        label_es="Recompra de Acciones",
        section="cashflow",
        unit="currency",
        patterns=[
            _p(r"^\s*(?:Share|Stock)\s+(?:Re)?[Pp]urchases?\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*[Rr]ecompra\s+de\s+[Aa]cciones\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="net_change_cash",
        label="Net Change in Cash",
        label_es="Variación Neta de Efectivo",
        section="cashflow",
        unit="currency",
        patterns=[
            _p(r"^\s*Net\s+(?:Increase|Decrease|Change)\s+in\s+Cash\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*(?:[Ii]ncremento|[Vv]ariaci[oó]n)\s+[Nn]eto\s+de\s+[Ee]fectivo\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    # ===================================================================
    # DERIVED RATIOS (calc-only; no regex patterns needed)
    # These are computed after extraction via compute_derived_metrics()
    # ===================================================================

    MetricDef(
        key="net_debt_to_ebitda",
        label="Net Debt / EBITDA",
        label_es="Deuda Neta / EBITDA",
        section="ratio",
        unit="ratio",
        calc="net_debt / ebitda",
        patterns=[
            _p(r"(?:[Dd]euda\s+[Nn]eta|[Nn]et\s+[Dd]ebt)\s*/\s*EBITDA\s+"
               r"([\d.]+)\s*[xX]?", 1.0, "prose"),
        ],
    ),

    MetricDef(
        key="debt_to_equity",
        label="Debt / Equity",
        label_es="Deuda / Capital",
        section="ratio",
        unit="ratio",
        calc="total_debt / equity",
        patterns=[],
    ),

    MetricDef(
        key="current_ratio",
        label="Current Ratio",
        label_es="Razón Circulante",
        section="ratio",
        unit="ratio",
        calc="current_assets / current_liabilities",
        patterns=[
            _p(r"^\s*Current\s+Ratio\s+([\d.]+)", 1.0, "table"),
            _p(r"^\s*[Rr]az[oó]n\s+[Cc]irculante\s+([\d.]+)", 1.0, "table"),
        ],
    ),

    MetricDef(
        key="interest_coverage",
        label="Interest Coverage (EBITDA/Interest)",
        label_es="Cobertura de Intereses",
        section="ratio",
        unit="ratio",
        calc="ebitda / interest_expense",
        patterns=[],
    ),

    MetricDef(
        key="return_on_equity",
        label="Return on Equity (ROE)",
        label_es="Retorno sobre Capital",
        section="ratio",
        unit="pct",
        calc="net_income / equity * 100",
        patterns=[
            _p(r"(?:Return\s+on\s+Equity|ROE)\s+([\d.]+)%?", 1.0, "prose"),
        ],
    ),

    MetricDef(
        key="return_on_assets",
        label="Return on Assets (ROA)",
        label_es="Retorno sobre Activos",
        section="ratio",
        unit="pct",
        calc="net_income / total_assets * 100",
        patterns=[
            _p(r"(?:Return\s+on\s+Assets|ROA)\s+([\d.]+)%?", 1.0, "prose"),
        ],
    ),

    MetricDef(
        key="asset_turnover",
        label="Asset Turnover",
        label_es="Rotación de Activos",
        section="ratio",
        unit="ratio",
        calc="revenue / total_assets",
        patterns=[],
    ),
]

# Quick lookup by key
METRIC_BY_KEY: dict[str, MetricDef] = {m.key: m for m in METRICS}


# ---------------------------------------------------------------------------
# Config loading — adds company-specific overrides and custom metrics
# ---------------------------------------------------------------------------

def load_config(config_path: str | Path) -> dict:
    """Load a YAML company config; returns dict with company info and merged metrics."""
    cfg = yaml.safe_load(Path(config_path).read_text())
    return cfg


def apply_config(base_metrics: list[MetricDef], config: dict) -> list[MetricDef]:
    """Merge company-specific alias overrides and custom KPI metrics into base list."""
    metrics = list(base_metrics)
    overrides = config.get("metric_overrides", {})

    for key, override in overrides.items():
        if key not in METRIC_BY_KEY:
            continue
        idx = next(i for i, m in enumerate(metrics) if m.key == key)
        m = metrics[idx]
        extra_pats: list[PatternSpec] = []

        extra_aliases = override.get("extra_aliases", [])
        if extra_aliases:
            alias_pat = "|".join(re.escape(a) for a in extra_aliases)
            extra_pats.append(_p(
                # Concatenate to avoid .format() misreading {1,3} quantifiers in _NL/_N
                r"^\s*(?:" + alias_pat + r")\s+(" + _NL + r")" + _FN + r"\s+(" + _N + r")"
            ))

        for ep in override.get("patterns", []):
            extra_pats.append(PatternSpec(
                regex=ep["regex"],
                multiplier=ep.get("multiplier", 1.0),
                source=ep.get("source", "table"),
            ))

        if extra_pats or override.get("calc") or "aggregation" in override:
            # Insert at front so company-specific patterns take priority. A config
            # may also set `calc` to enable an identity-derived fallback on a base
            # metric (e.g. operating_income = ebitda - depreciation for one company).
            metrics[idx] = MetricDef(
                key=m.key, label=m.label, label_es=m.label_es,
                section=m.section, unit=m.unit,
                patterns=extra_pats + m.patterns,
                calc=override.get("calc") or m.calc, validate=m.validate,
                xbrl_concepts=list(m.xbrl_concepts) + override.get("xbrl_concepts", []),
                aliases=list(m.aliases) + override.get("aliases", []),
                aggregation=override.get("aggregation", m.aggregation),
            )

    for cm in config.get("custom_metrics", []):
        patterns = [
            PatternSpec(
                regex=p["regex"],
                multiplier=p.get("multiplier", 1.0),
                source=p.get("source", "table"),
            )
            for p in cm.get("patterns", [])
        ]
        metrics.append(MetricDef(
            key=cm["key"],
            label=cm.get("label", cm["key"]),
            label_es=cm.get("label_es", cm["key"]),
            section=cm.get("section", "kpi"),
            unit=cm.get("unit", "currency"),
            patterns=patterns,
            calc=cm.get("calc"),
            validate=cm.get("validate", []),
            xbrl_concepts=cm.get("xbrl_concepts", []),
            aliases=cm.get("aliases", []),
            aggregation=cm.get("aggregation"),
        ))

    # Drop base metrics a company never wants extracted (e.g. margins that are
    # formula-derived in the deliverable). Keeping them only invites mis-grabs that
    # poison cross-checks (a stray "80" read as ebitda_margin breaks margin_consistency
    # for revenue/ebitda). `exclude_metrics: [key, ...]` removes them outright.
    excluded = set(config.get("exclude_metrics", []))
    if excluded:
        metrics = [m for m in metrics if m.key not in excluded]

    # Attach Tier 1 IFRS concept maps + Tier 2 row-label aliases from the shared
    # registry (configs/xbrl_concepts.yaml), unless a metric already carries them.
    metrics = attach_concept_map(metrics)
    return metrics


# ---------------------------------------------------------------------------
# Tier 1/2 metadata: IFRS concept map + table row-label aliases
# ---------------------------------------------------------------------------

from src.shared.paths import CONFIGS_DIR
_CONCEPT_MAP_PATH = CONFIGS_DIR / "xbrl_concepts.yaml"
_CONCEPT_MAP_CACHE: dict | None = None


def load_concept_map(path: str | Path | None = None) -> dict:
    """Load the metric_key → {xbrl_concepts, aliases} registry (cached)."""
    global _CONCEPT_MAP_CACHE
    if path is None:
        if _CONCEPT_MAP_CACHE is not None:
            return _CONCEPT_MAP_CACHE
        path = _CONCEPT_MAP_PATH
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    mapping = (data or {}).get("metrics", {})
    if path == _CONCEPT_MAP_PATH or str(path) == str(_CONCEPT_MAP_PATH):
        _CONCEPT_MAP_CACHE = mapping
    return mapping


def attach_concept_map(
    metrics: list[MetricDef], mapping: dict | None = None,
) -> list[MetricDef]:
    """Return metrics with xbrl_concepts/aliases filled from the registry.

    A metric that already declares concepts/aliases (e.g. from a company config)
    keeps its own; the registry only fills empty fields. Pure-default — never
    overwrites and never errors when the registry is absent.
    """
    if mapping is None:
        mapping = load_concept_map()
    if not mapping:
        return metrics
    out: list[MetricDef] = []
    for m in metrics:
        entry = mapping.get(m.key)
        if entry and (not m.xbrl_concepts or not m.aliases):
            m = MetricDef(
                key=m.key, label=m.label, label_es=m.label_es,
                section=m.section, unit=m.unit, patterns=m.patterns,
                calc=m.calc, validate=m.validate,
                xbrl_concepts=m.xbrl_concepts or list(entry.get("xbrl_concepts", [])),
                aliases=m.aliases or list(entry.get("aliases", [])),
                aggregation=m.aggregation,
            )
        out.append(m)
    return out


_CALC_NODE_TYPES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.UAdd, ast.USub,
)
_CALC_OPERATORS = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


def parse_metric_calc(expression: str) -> ast.Expression:
    """Parse and validate a declarative metric calculation.

    Only metric names, numeric literals, and ``+ - * /`` (including unary
    signs) are accepted.  This
    AST is shared by the Python fallback evaluator and the Excel compiler, so a
    config formula cannot execute functions, access attributes, or import code.
    """
    try:
        tree = ast.parse(str(expression), mode="eval")
    except (SyntaxError, TypeError) as exc:
        raise ValueError(f"Invalid metric calculation: {expression!r}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _CALC_NODE_TYPES):
            raise ValueError(
                f"Unsupported expression in metric calculation: {expression!r}"
            )
        if isinstance(node, ast.Constant) and (
            isinstance(node.value, bool) or not isinstance(node.value, (int, float))
        ):
            raise ValueError(
                f"Only numeric literals are allowed in metric calculations: {expression!r}"
            )
    return tree


def metric_calc_names(expression: str) -> tuple[str, ...]:
    """Return metric operands in first-use order after validating ``expression``."""
    tree = parse_metric_calc(expression)
    names: list[str] = []

    class _NameVisitor(ast.NodeVisitor):
        def visit_Name(self, node):  # noqa: N802 - ast visitor API name
            if node.id not in names:
                names.append(node.id)

    _NameVisitor().visit(tree)
    return tuple(names)


def evaluate_metric_calc(expression: str, values: dict[str, float]) -> float:
    """Safely evaluate a validated metric calculation against numeric values."""
    tree = parse_metric_calc(expression)

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name):
            return float(values[node.id])
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.UnaryOp):
            value = visit(node.operand)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value
            raise ValueError(f"Unsupported metric unary operator: {type(node.op).__name__}")
        if isinstance(node, ast.BinOp):
            op = _CALC_OPERATORS.get(type(node.op))
            if op is None:  # defensive; parse_metric_calc already rejects it
                raise ValueError(f"Unsupported metric operator: {type(node.op).__name__}")
            return op(visit(node.left), visit(node.right))
        raise ValueError(f"Unsupported metric calculation node: {type(node).__name__}")

    return float(visit(tree))


def compute_derived_metrics(
    extracted: dict,  # dict[str, MetricRow]
    metric_defs: list[MetricDef],
    *,
    include_existing: bool = False,
) -> dict:
    """Compute formula-based ratio metrics from already-extracted values."""
    from src.extract.extract_metrics import MetricRow

    derived = {}
    for mdef in metric_defs:
        if not mdef.calc or (mdef.key in extracted and not include_existing):
            continue
        try:
            values = {
                m.key: extracted[m.key].current
                for m in metric_defs
                if m.key in extracted and extracted[m.key].current is not None
            }
            val = evaluate_metric_calc(mdef.calc, values)
            derived[mdef.key] = MetricRow(
                metric=mdef.key,
                label_es=mdef.label_es,
                current=float(val),
                prior=None,
                var_pct=None,
                unit=mdef.unit,
                source_line="[calculated]",
            )
        except (KeyError, ZeroDivisionError, TypeError, ValueError):
            pass
    return derived


def list_metrics(section: str | None = None) -> None:
    """Print all available metric keys to stdout."""
    sections = {}
    for m in METRICS:
        sections.setdefault(m.section, []).append(m)
    order = ["income", "balance", "cashflow", "ratio", "kpi"]
    for sec in order:
        if section and sec != section:
            continue
        if sec not in sections:
            continue
        print(f"\n  {'─' * 58}")
        print(f"  {sec.upper()}")
        print(f"  {'─' * 58}")
        for m in sections[sec]:
            calc_note = f"  [calc: {m.calc}]" if m.calc else ""
            print(f"  {m.key:<30}  {m.label}{calc_note}")
