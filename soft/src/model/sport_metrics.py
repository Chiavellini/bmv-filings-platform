"""
sport_metrics.py — Comprehensive SPORT-specific metric regex dictionary.

This module extends financial_model.METRICS with all real (directly extractable)
financial and operational metrics from Grupo Sports World quarterly reports.

CLASSIFICATION GUIDE
====================
REAL   = value appears verbatim in the report text/tables → extractable by regex
DERIVED = computed from other extracted values → NOT regex targets

DERIVED metrics (do NOT add regex for these):
  - YoY / QoQ % change               → (current / prior - 1) × 100
  - As % of revenues/consolidated     → metric / revenue × 100
  - bps change YoY                    → (current_margin - prior_margin) × 100
  - Check rows                        → sum verification (e.g., col sums)
  - Churn difference (gross − net)    → gross_churn - net_churn
  - Delta                             → difference between two metrics
  - Active Clients per Club           → clientes_activos / clubs_count
  - Same Club Sales (revenue)         → not reported per revenue line
  - Marginal Contribution (%)         → marginal_contrib / revenue × 100

IFRS 16 COLUMN ORDER NOTE
==========================
The 7-column income statement (con IFRS | $ | % | sin IFRS) REVERSES in 2026:
  2019–2025:  Col 1 = con IFRS 16,  Col 5 = sin IFRS 16
  2026+:      Col 1 = sin IFRS 16,  Col 5 = con IFRS 16

Prose patterns (multiplier=1000) are always year-safe because they explicitly
name "con IFRS 16" or "sin IFRS 16". Table patterns work for 2019–2025 only
and are documented accordingly.

Usage
=====
    from src.model.financial_model import METRICS, apply_config, load_config
    from src.model.sport_metrics import SPORT_EXTENDED_METRICS, get_sport_metrics

    # Option A: use the combined list directly
    all_metrics = get_sport_metrics()

    # Option B: merge into a config-driven pipeline
    cfg = load_config("configs/sport.yaml")
    base = apply_config(METRICS, cfg)
    combined = base + SPORT_EXTENDED_METRICS
"""

from __future__ import annotations

from src.model.financial_model import MetricDef, PatternSpec, METRICS, _N, _NL, _FN


def _p(regex: str, mult: float = 1.0, src: str = "table") -> PatternSpec:
    return PatternSpec(regex=regex, multiplier=mult, source=src)


# ---------------------------------------------------------------------------
# Shared regex fragments
# ---------------------------------------------------------------------------

# Skip to after the first "X.X%" in a row (used to find sin-IFRS columns in
# 2019-2025 tables where con-IFRS columns appear first).
# CAUTION: In 2026+ tables the order is reversed (sin-IFRS appears first).
# [^\s]* after % handles suffixes like ")" in "(185.2%) " or " pp" in "5.0 pp"
_SKIP_TO_AFTER_PCT = r"[^\n]*?[\d.]+%[^\s]*\s+"


# ---------------------------------------------------------------------------
# SPORT_EXTENDED_METRICS
# All metrics below are REAL (extractable by regex).
# ---------------------------------------------------------------------------

SPORT_EXTENDED_METRICS: list[MetricDef] = [

    # ===================================================================
    # REVENUE BREAKDOWN
    # Sub-lines of Total Revenue that appear in the income statement.
    # ===================================================================

    MetricDef(
        key="rev_membership",
        label="Membership Revenue",
        label_es="Ingresos por Membresías",
        section="income",
        unit="currency",
        patterns=[
            # Table: "Ingresos por Membresías  13,850  15,377  (1,526)  (9.9%)"
            _p(r"^\s*[Ii]ngresos\s+por\s+[Mm]embres[ií]as\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
        ],
    ),

    MetricDef(
        key="rev_maintenance",
        label="Maintenance Revenue",
        label_es="Ingresos por Mantenimiento",
        section="income",
        unit="currency",
        patterns=[
            # Table: "Ingresos por Mantenimiento  385,809  318,749  67,059  21.0%"
            _p(r"^\s*[Ii]ngresos\s+por\s+[Mm]antenimiento\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
        ],
    ),

    MetricDef(
        key="rev_sports_only",
        label="Sports Revenue",
        label_es="Ingresos Deportivos",
        section="income",
        unit="currency",
        patterns=[
            # Table: "Ingresos deportivos  34,705  34,034  671  2.0%"
            # Note: must NOT match "Ingresos deportivos y Otros" (the combined line)
            _p(r"^\s*[Ii]ngresos\s+[Dd]eportivos\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
        ],
    ),

    MetricDef(
        key="rev_other_business",
        label="Other Business Revenue",
        label_es="Otros Ingresos del Negocio",
        section="income",
        unit="currency",
        patterns=[
            # Table: "Otros Ingresos del negocio  47,864  33,897  13,967  41.2%"
            _p(r"^\s*[Oo]tros\s+[Ii]ngresos\s+del\s+[Nn]egocio\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
        ],
    ),

    MetricDef(
        key="rev_total_other",
        label="Total Other Income",
        label_es="Total Otros Ingresos",
        section="income",
        unit="currency",
        patterns=[
            # Table: "Total Otros Ingresos  118,049  79,094  38,956  49.3%"
            _p(r"^\s*Total\s+[Oo]tros?\s+[Ii]ngresos\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
        ],
    ),

    # ===================================================================
    # CLUB COUNT DETAIL
    # From the rolling multi-period club count table.
    # Pattern: take the LAST number in the row (most recent quarter).
    # The (\d{2})\d* trick strips pdfplumber superscript artifacts (49¹→"491").
    # ===================================================================

    MetricDef(
        key="clubs_sw_period_start",
        label="SW Clubs at Start of Period",
        label_es="Inicio del Periodo Clubes SW",
        section="kpi",
        unit="count",
        patterns=[
            # 2022-2024: "Inicio del periodo clubes SW  55  56  56 ... 50"
            _p(r"[Ii]nicio\s+del\s+[Pp]eriodo\s+(?:clubes?\s+)?SW"
               r"(?:\s+[-\d]+)+\s+(\d{2})\d*\s*$"),
            # 2016: "Inicio del periodo  35  36  37 ... 46"  (no SW suffix)
            _p(r"[Ii]nicio\s+del\s+[Pp]eriodo\s+(?:\d+\s+)+(\d{2})\d*\s*$"),
        ],
    ),

    MetricDef(
        key="clubs_sw_openings",
        label="SW Club Openings in Period",
        label_es="Aperturas SW",
        section="kpi",
        unit="count",
        patterns=[
            # "Aperturas SW   1   0   0  0  0  2  0  0  0  0  0  0  0"
            _p(r"[Aa]perturas?\s+SW(?:\s+[-\d]+)+\s+(\d{1,2})\d*\s*$"),
            # 2016: "Aperturas   1   1   4   1b   0   2c  1   1   1"
            _p(r"^\s*[Aa]perturas?\s+(?:\d+[a-z]?\s+)+(\d{1,2})[a-z]?\s*$"),
        ],
    ),

    MetricDef(
        key="clubs_sw_closures",
        label="SW Club Closures in Period",
        label_es="Cierres SW",
        section="kpi",
        unit="count",
        patterns=[
            # "Cierres SW  0  0  1  0  0  3  1  1  0  0  2  0  0"
            # Note: closures are typically positive integers (even though they reduce count)
            _p(r"[Cc]ierres?\s+SW(?:\s+[-\d]+)+\s+(-?\d{1,2})\d*\s*$"),
        ],
    ),

    MetricDef(
        key="clubs_sw_total",
        label="Total SW Clubs in Operation (end of period)",
        label_es="Total de Clubes SW en Operación al Final del Periodo",
        section="kpi",
        unit="count",
        patterns=[
            # "Total de clubes SW en operación al final del periodo  56  56  55 ... 50"
            _p(r"Total\s+de\s+clubes?\s+SW\s+en\s+operaci[oó]n\s+al\s+final\s+del\s+periodo"
               r"(?:\s+\d+)+\s+(\d{2})\d*\s*$"),
        ],
    ),

    MetricDef(
        key="clubs_other_total",
        label="Other Clubs Total (non-SW brands)",
        label_es="Total Otros Clubes",
        section="kpi",
        unit="count",
        patterns=[
            # "Total LOAD  2  2  2  2  2  0  0 ... 50"
            # Covers LOAD and any other non-SW brands
            _p(r"Total\s+(?:LOAD|[Oo]tros?)(?:\s+[-\d]+)+\s+(\d{1,2})\d*\s*$"),
        ],
    ),

    # ===================================================================
    # OPERATING METRICS — extended
    # ===================================================================

    MetricDef(
        key="gross_churn",
        label="Average Gross Churn Rate",
        label_es="Deserción Bruta Promedio",
        section="kpi",
        unit="pct",
        patterns=[
            # Summary table: "Deserción bruta promedio  7.2%  7.8%  -0.6 pp"
            _p(r"^\s*[Dd]eserci[oó]n\s+bruta\s+promedio\s+"
               r"({N})\s+({N})".format(N=_N)),
            # Alternative labels
            _p(r"^\s*[Dd]escerci[oó]n\s+bruta\s+promedio\s+"  # typo variant in some reports
               r"({N})\s+({N})".format(N=_N)),
        ],
    ),

    MetricDef(
        key="active_clients_per_club",
        label="Active Clients per Club",
        label_es="Clientes Activos por Club",
        section="kpi",
        unit="ratio",
        # NOTE: This is often DERIVED (active_clients / clubs_count).
        # Included here in case it appears in a report table.
        calc="clientes_activos / clubs_count",
        patterns=[
            _p(r"^\s*[Cc]lientes\s+(?:[Aa]ctivos\s+)?por\s+[Cc]lub\s+"
               r"({N})\s+({N})".format(N=_N)),
            _p(r"[Cc]lientes\s+activos\s+por\s+club[:\s]+([0-9,.]+)", 1.0, "prose"),
        ],
    ),

    # ===================================================================
    # INCOME STATEMENT — CON IFRS 16 (Post IFRS 16)
    # These capture the "con IFRS 16" (post-IFRS) variant of P&L lines.
    #
    # TABLE PATTERNS:  Work for 2019–2025 (con IFRS = columns 1 & 2).
    #                  In 2026, columns are REVERSED — use prose patterns instead.
    # PROSE PATTERNS:  Always year-safe; explicitly state "con IFRS 16".
    # ===================================================================

    MetricDef(
        key="club_opex_con_ifrs",
        label="Club Operating Costs (post IFRS 16)",
        label_es="Gastos de Operación de Clubes (con IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table 2019-2025: "Gastos de Operación de clubes¹  286,975 249,623 ..."
            _p(r"^\s*[Gg]astos\s+de\s+[Oo]peraci[oó]n\s+de\s+clubes?\s*\d?\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
            # 2016 label: "Gastos de Operación Clubes¹"
            _p(r"^\s*[Gg]astos\s+de\s+[Oo]peraci[oó]n\s+[Cc]lubes?\s*\d?\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
        ],
    ),

    MetricDef(
        key="club_sales_expense",
        label="Club Sales Expenses",
        label_es="Gastos de Venta",
        section="income",
        unit="currency",
        patterns=[
            # Table: "Gastos de Venta  18,636  16,234  2,402  14.8%  -  -  -"
            # This line has no sin-IFRS variant (marked "-")
            _p(r"^\s*[Gg]astos\s+de\s+[Vv]enta\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
        ],
    ),

    MetricDef(
        key="marginal_contrib_con_ifrs",
        label="Marginal Contribution of Clubs (post IFRS 16)",
        label_es="Contribución Marginal de Clubes (con IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table 2019-2025: "Contribución Marginal de clubes  230,734 163,597 ..."
            _p(r"^\s*[Cc]ontribuci[oó]n\s+[Mm]arginal\s+de\s+clubes?\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
            # 2016 label: "Contribución Marginal" (no "de clubes")
            _p(r"^\s*[Cc]ontribuci[oó]n\s+[Mm]arginal\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
        ],
    ),

    MetricDef(
        key="admin_costs",
        label="Administrative Costs",
        label_es="Costo Administrativo",
        section="income",
        unit="currency",
        # NOTE: Admin costs have the SAME value in both con-IFRS and sin-IFRS columns
        # (no IFRS 16 adjustment to admin costs), so no need for two separate metrics.
        patterns=[
            # 2019+: "Costo Administrativo  38,615  24,535  14,080  57.4%"
            _p(r"^\s*[Cc]osto\s+[Aa]dministrativo\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
            # 2016: "Gastos Corporativos  20,687  19,253  7.4%"
            _p(r"^\s*[Gg]astos\s+[Cc]orporativos\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
        ],
    ),

    MetricDef(
        key="total_opex_con_ifrs",
        label="Total Operating Expenses excl. D&A (post IFRS 16)",
        label_es="Gastos de Operación Total excl. D&A (con IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table 2019-2025: "Gastos de Operación²  325,589 274,158 ... 439,680 ..."
            # Column 1 = con IFRS (2019-2025). ² is the footnote: "excl. D&A"
            _p(r"^\s*[Gg]astos\s+de\s+[Oo]peraci[oó]n\s*2\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
            # Prose 2024: "Los Gastos de Operación ... alcanzando $325.6 millones"
            _p(r"[Gg]astos\s+(?:de\s+|[Tt]otales\s+de\s+)?[Oo]peraci[oó]n\s*\d?"
               r"[^\n]*?(?:alcanzaron?|finalizaron?|llegaron?)[^\n]*?\$\s*([\d.]+)\s+millones",
               1_000, "prose"),
        ],
    ),

    MetricDef(
        key="da_con_ifrs",
        label="D&A (post IFRS 16)",
        label_es="Depreciación y Amortización (con IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table 2019-2025: "Depreciación y Amortización  91,444 108,246 ..."
            # Column 1 = con IFRS (2019-2025), which includes right-of-use asset D&A
            _p(r"^\s*[Dd]epreciaci[oó]n\s+y\s+[Aa]mortizaci[oó]n\s+"
               r"({NL}){FN}\s+({N}){FN}\s+(?:{N}\s+)?({N})".format(N=_N, NL=_NL, FN=_FN)),
            _p(r"^\s*[Dd]epreciaci[oó]n\s+y\s+[Aa]mortizaci[oó]n\s+"
               r"({NL}){FN}\s+({N})".format(N=_N, NL=_NL, FN=_FN)),
        ],
    ),

    MetricDef(
        key="total_costs_con_ifrs",
        label="Total Operating Costs incl. D&A (post IFRS 16)",
        label_es="Gastos Totales de Operación (con IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table: "Gastos Totales de Operación  417,033 382,404 34,629 9.1% 472,338 ..."
            _p(r"^\s*[Gg]astos\s+[Tt]otales\s+de\s+[Oo]peraci[oó]n\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
        ],
    ),

    MetricDef(
        key="operating_profit_con_ifrs",
        label="Operating Profit (post IFRS 16)",
        label_es="Utilidad de Operación (con IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table: "(Pérdida) Utilidad de operación  100,675 30,816 ... 45,371 ..."
            # Column 1 = con IFRS (2019-2025)
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
        ],
    ),

    # ===================================================================
    # INCOME STATEMENT — SIN IFRS 16 (Pre IFRS 16)
    # These capture the "sin IFRS 16" variant. Prose patterns are the primary
    # method since the table column ordering varies by year.
    # ===================================================================

    MetricDef(
        key="club_opex_sin_ifrs",
        label="Club Operating Costs (pre IFRS 16)",
        label_es="Gastos de Operación de Clubes (sin IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table 2019-2025: skip 4 cols (con IFRS cur, prior, var, %var) → sin IFRS current
            # Pattern: LABEL ... firstlargenum ... X% ... secondlargenum
            _p(r"^\s*[Gg]astos\s+de\s+[Oo]peraci[oó]n\s+de\s+clubes?\s*\d?\s+"
               + _NL + _SKIP_TO_AFTER_PCT + "(" + _NL + ")" + _FN + r"\s+(" + _N + ")"),
            # Prose: "Sin considerar el efecto del IFRS 16 estos gastos aumentaron ... $315.8 millones"
            _p(r"[Gg]astos\s+de\s+[Oo]peraci[oó]n[^.]*?"
               r"sin\s+(?:considerar\s+)?(?:el\s+efecto\s+(?:del?|de\s+))?IFRS\s*16[^\n]*?"
               r"\$\s*([\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="marginal_contrib_sin_ifrs",
        label="Marginal Contribution of Clubs (pre IFRS 16)",
        label_es="Contribución Marginal de Clubes (sin IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table 2019-2025: skip to after %var → sin IFRS columns
            _p(r"^\s*[Cc]ontribuci[oó]n\s+[Mm]arginal\s+de\s+clubes?\s+"
               + _NL + _SKIP_TO_AFTER_PCT + "(" + _NL + ")" + _FN + r"\s+(" + _N + ")"),
        ],
    ),

    MetricDef(
        key="total_opex_sin_ifrs",
        label="Total Operating Expenses excl. D&A (pre IFRS 16)",
        label_es="Gastos de Operación Total excl. D&A (sin IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table 2019-2025: "Gastos de Operación²  325,589 ... 18.8% 439,680 ..."
            # Skip to after first %var → sin IFRS current
            _p(r"^\s*[Gg]astos\s+de\s+[Oo]peraci[oó]n\s*2\s+"
               + _NL + _SKIP_TO_AFTER_PCT + "(" + _NL + ")" + _FN + r"\s+(" + _N + ")"),
            # Prose (multi-line): "Sin IFRS 16 ... $439.7 millones"
            _p(r"[Ss]i\s+excluimos\s+(?:el\s+efecto\s+de\s+)?IFRS\s*16"
               r"[^\n]*\$\s*([\d.]+)\s+millones", 1_000, "prose"),
            _p(r"[Gg]astos\s+de\s+[Oo]peraci[oó]n[^\n]*"
               r"excluyendo[^\n]*?\n?[^\n]*?\$\s*([\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="da_sin_ifrs",
        label="D&A excl. Leases (pre IFRS 16)",
        label_es="Depreciación y Amortización (sin IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table 2019-2025: skip to sin IFRS columns
            _p(r"^\s*[Dd]epreciaci[oó]n\s+y\s+[Aa]mortizaci[oó]n\s+"
               + _NL + _SKIP_TO_AFTER_PCT + "(" + _NL + ")" + _FN + r"\s+(" + _N + ")"),
            # Prose: "Sin el efecto contable de IFRS 16 la D&A fue $45.1 millones"
            _p(r"[Ss]in\s+(?:el\s+efecto\s+(?:contable\s+)?de\s+)?IFRS\s*16[^.]*?"
               r"[Dd]epreciaci[oó]n[^.]*?\$\s*([\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="operating_profit_sin_ifrs",
        label="Operating Profit (pre IFRS 16)",
        label_es="Utilidad de Operación (sin IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table 2019-2025: skip to sin IFRS columns
            _p(r"^\s*(?:\(?P[eé]rdida\)?\s+)?[Uu]tilidad\s+de\s+[Oo]peraci[oó]n\s+"
               + _NL + _SKIP_TO_AFTER_PCT + "(" + _NL + ")" + _FN + r"\s+(" + _N + ")"),
            # Prose: "Sin el efecto IFRS 16, la Pérdida de Operación fue -$111.2 millones"
            # Capture full signed value (parse_number strips $ and handles sign)
            _p(r"[Ss]in\s+(?:considerar\s+)?(?:el\s+efecto\s+)?IFRS\s*16[^.]*?"
               r"[Uu]tilidad\s+de\s+[Oo]peraci[oó]n[^.]*?(-?\$?\s?[\d.]+)\s+millones", 1_000, "prose"),
            _p(r"[Ss]in\s+(?:considerar\s+)?(?:el\s+efecto\s+)?IFRS\s*16[^.]*?"
               r"[Pp][eé]rdida\s+de\s+[Oo]peraci[oó]n[^.]*?(-?\$?\s?[\d.]+)\s+millones", 1_000, "prose"),
            _p(r"[Uu]tilidad\s+de\s+[Oo]peraci[oó]n[\s\S]{0,120}?"
               r"en\s+el\s+(?:trimestre|\d+T\d{2,4})[\s\S]{0,80}?"
               r"sin\s+(?:considerar\s+)?(?:el\s+efecto\s+)?IFRS\s*16[\s\S]{0,120}?"
               r"(?:fue|finaliz[oó]|termin[oó]|cerr[oó])(?:\s+el\s+trimestre\s+en|\s+en|\s+de|\s+a)?"
               r"[\s\S]{0,120}?(-?\$?\s?[\d,.]+)\s+millones", 1_000, "prose"),
            _p(r"[Uu]tilidad\s+de\s+[Oo]peraci[oó]n\s+\(sin\s+considerar\s+"
               r"(?:el\s+efecto\s+)?IFRS\s*16\)[\s\S]{0,120}?"
               r"(?:fue|finaliz[oó]|termin[oó]|cerr[oó])(?:\s+el\s+trimestre\s+en|\s+en|\s+de|\s+a)?"
               r"[\s\S]{0,120}?(-?\$?\s?[\d,.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="total_costs_sin_ifrs",
        label="Total Operating Costs incl. D&A (pre IFRS 16)",
        label_es="Gastos Totales de Operación (sin IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Table: "Gastos Totales de Operación  417,033 ... 9.1% 472,338 ..."
            # Skip to sin IFRS columns
            _p(r"^\s*[Gg]astos\s+[Tt]otales\s+de\s+[Oo]peraci[oó]n\s+"
               + _NL + _SKIP_TO_AFTER_PCT + "(" + _NL + ")" + _FN + r"\s+(" + _N + ")"),
        ],
    ),

    # ===================================================================
    # EBITDA EXTENDED METRICS
    # ===================================================================

    MetricDef(
        key="same_club_ebitda",
        label="Same Club EBITDA (pre IFRS 16)",
        label_es="EBITDA Mismos Clubes (sin IFRS 16)",
        section="income",
        unit="currency",
        patterns=[
            # Prose 2022: "El EBITDA Mismos Clubes (clubes con 12 meses...) en el 1T22 fue -$66.4 millones"
            # Uses flexible match: parenthetical clause + "en el PERIOD fue"
            _p(r"[Ee][Bb][Ii][Tt][Dd][Aa]\s+[Mm]ismos?\s+[Cc]lubes?"
               r"(?:\s*\([^)]+\))?"                       # optional parenthetical
               r"(?:\s+en\s+(?:\S+\s+){1,3})?"            # optional "en el 1T22 ..."
               r"\s*(?:fue|finalizó|alcanzó|cerró)\s+"
               r"[^\n]*?(-?\$?\s?[\d.]+)\s+millones", 1_000, "prose"),
            # Prose alternative: "EBITDA de Mismos Clubes fue de $X millones" (any form)
            _p(r"[Ee][Bb][Ii][Tt][Dd][Aa]\s+(?:de\s+)?[Mm]ismos?\s+[Cc]lubes?"
               r"[^\n]*?(-?\$?\s?[\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="same_club_ebitda_margin",
        label="Same Club EBITDA Margin (pre IFRS 16)",
        label_es="Margen EBITDA Mismos Clubes (sin IFRS 16)",
        section="income",
        unit="pct",
        # NOTE: Often not directly stated — typically derived as same_club_ebitda / revenue.
        # Included here for completeness; rarely appears as an explicit figure.
        calc="same_club_ebitda / revenue * 100",
        patterns=[
            _p(r"[Mm]argen\s+EBITDA\s+[Mm]ismos?\s+[Cc]lubes?[^\n]*?([\d.]+)%"),
        ],
    ),

    MetricDef(
        key="lease_payments_is",
        label="Lease Payments (Income Statement, IFRS 16 add-back)",
        label_es="Pagos de Arrendamiento (Estado de Resultados)",
        section="income",
        unit="currency",
        # This is the difference in operating costs between con and sin IFRS 16:
        # lease_payments_is ≈ total_opex_sin_ifrs - total_opex_con_ifrs
        # Reported explicitly only in some years (2019-2020 transition period).
        calc="total_opex_sin_ifrs - total_opex_con_ifrs",
        patterns=[
            _p(r"^\s*[Pp]agos?\s+(?:de\s+)?[Aa]rrendamiento\s+"
               r"({NL}){FN}\s+({N})".format(NL=_NL, N=_N, FN=_FN)),
            _p(r"[Pp]agos?\s+(?:de\s+)?[Aa]rrendamiento[^\n]*?\$\s*([\d.]+)\s+millones",
               1_000, "prose"),
        ],
    ),

    MetricDef(
        key="da_ex_leases_sin_ifrs",
        label="D&A excl. Right-of-Use Asset Depreciation (pre IFRS 16)",
        label_es="D&A excluyendo Arrendamientos (sin IFRS 16)",
        section="income",
        unit="currency",
        # This IS the da_sin_ifrs value — same metric, different label.
        # Pre IFRS 16 D&A excludes the right-of-use asset depreciation added by IFRS 16.
        calc="da_sin_ifrs",   # alias
        patterns=[
            # Same patterns as da_sin_ifrs — this metric is identical
            _p(r"^\s*[Dd]epreciaci[oó]n\s+y\s+[Aa]mortizaci[oó]n\s+"
               + _NL + _SKIP_TO_AFTER_PCT + "(" + _NL + ")" + _FN + r"\s+(" + _N + ")"),
            _p(r"[Ss]in\s+el\s+efecto\s+(?:contable\s+)?de\s+IFRS\s*16[^.]*?"
               r"[Dd]epreciaci[oó]n[^.]*?\$\s*([\d.]+)\s+millones", 1_000, "prose"),
        ],
    ),

    MetricDef(
        key="ebitda_margin_con_ifrs",
        label="EBITDA Margin (post IFRS 16)",
        label_es="Margen EBITDA (con IFRS 16)",
        section="income",
        unit="pct",
        calc="ebitda / revenue * 100",
        patterns=[
            # Prose 2026: "... y con IFRS 16 de 36.3%"
            _p(r"[Mm]argen\s+(?:EBITDA|UAFIDA)[^\n]*?sin\s+IFRS[^\n]*?[\d.]+%"
               r"[^\n]*?con\s+IFRS[^\n]*?([\d.]+)%"),
            # Prose 2024: "margen EBITDA para el trimestre fue del 37.1%"
            _p(r"[Mm]argen\s+(?:EBITDA|UAFIDA)\s+para\s+el\s+trimestre\s+fue\s+del?\s+([\d.]+)%"),
            # Table (first col = con IFRS for 2022-2025):
            _p(r"^\s*[Mm]argen\s+(?:EBITDA|UAFIDA)\s+({N})\s+({N})".format(N=_N)),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Complete combined list
# ---------------------------------------------------------------------------

def get_sport_metrics() -> list[MetricDef]:
    """Return the full SPORT metric list: generic + SPORT-extended.

    Deduplicates by key — SPORT_EXTENDED_METRICS takes precedence over
    financial_model.METRICS if the same key appears in both.
    """
    extended_keys = {m.key for m in SPORT_EXTENDED_METRICS}
    base = [m for m in METRICS if m.key not in extended_keys]
    return base + SPORT_EXTENDED_METRICS


# ---------------------------------------------------------------------------
# Derived-metrics reference (for documentation / Excel model planning)
# ---------------------------------------------------------------------------

DERIVED_METRICS = {
    # key: formula (uses extracted metric keys as variables)
    "yoy_revenue":                 "revenue / revenue_prior - 1",
    "qoq_revenue":                 "revenue / revenue_qprior - 1",
    "rev_maintenance_membership_pct": "rev_maintenance_membership / revenue",
    "rev_sports_pct":              "rev_sports_only / revenue",
    "rev_sponsorships_pct":        "rev_sponsorships / revenue",
    "club_opex_pct_con":           "club_opex_con_ifrs / revenue",
    "club_opex_pct_sin":           "club_opex_sin_ifrs / revenue",
    "marginal_contrib_pct_con":    "marginal_contrib_con_ifrs / revenue",
    "marginal_contrib_pct_sin":    "marginal_contrib_sin_ifrs / revenue",
    "admin_costs_pct":             "admin_costs / revenue",
    "da_pct_con":                  "da_con_ifrs / revenue",
    "da_pct_sin":                  "da_sin_ifrs / revenue",
    "operating_profit_pct_con":    "operating_profit_con_ifrs / revenue",
    "operating_profit_pct_sin":    "operating_profit_sin_ifrs / revenue",
    "ebitda_margin_sin":           "ebitda_sin_ifrs / revenue",
    "ebitda_margin_con":           "ebitda / revenue",
    "same_club_ebitda_margin":     "same_club_ebitda / revenue",
    "churn_difference":            "gross_churn - net_churn",
    "active_clients_per_club":     "clientes_activos / clubs_count",
    "lease_payments_is":           "total_opex_sin_ifrs - total_opex_con_ifrs",
    "yoy_ebitda_bps":              "(ebitda_margin_con - ebitda_margin_con_prior) * 100",
    "net_debt_to_ebitda":          "net_debt / ebitda",
}


# ---------------------------------------------------------------------------
# Quick print
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"\n{'SPORT EXTENDED METRICS':=^60}")
    print(f"{'(all are REAL / directly extractable)':^60}\n")
    sections = {}
    for m in SPORT_EXTENDED_METRICS:
        sections.setdefault(m.section, []).append(m)
    for sec, mlist in sections.items():
        print(f"\n  {sec.upper()}")
        print(f"  {'─' * 56}")
        for m in mlist:
            tag = ""
            if m.calc and not any(p.source == "table" for p in m.patterns):
                tag = " [calc/prose only]"
            print(f"  {m.key:<36}  {m.label_es[:40]}{tag}")

    print(f"\n\n{'DERIVED METRICS (NOT regex targets)':=^60}")
    print(f"{'These are computed in Excel / pandas, not extracted':^60}\n")
    for key, formula in DERIVED_METRICS.items():
        print(f"  {key:<36}  = {formula}")
