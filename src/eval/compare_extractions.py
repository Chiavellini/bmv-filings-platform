#!/usr/bin/env python3
"""
compare_extractions.py — Compare extracted metrics vs. actuales ground truth.

Usage:
    python3 compare_extractions.py lacomer
    python3 compare_extractions.py walmex
    python3 compare_extractions.py all
    python3 compare_extractions.py walmex --tiers xbrl,table,prose   # ablation
"""
from __future__ import annotations

import csv
import re
import sys
import yaml
from collections import defaultdict
from pathlib import Path

from src.model.financial_model import METRICS, apply_config
from src.extract.extract_metrics import extract_metrics_segmented
from src.extract.tiered_extract import PeriodSource, extract_metrics_tiered, period_end_from_label

# Extraction tiers, in precision order. Default replicates the historical
# behavior (regex + derived); pass --tiers to measure the structured tiers'
# marginal contribution (table reads sibling PDFs and is slower).
ALL_TIERS = ["xbrl", "bmv", "note", "search", "table", "prose", "llm"]
DEFAULT_TIERS = {"prose"}


def _tier_of(source_line: str) -> str:
    """Map a MetricRow.source_line prefix back to the tier that produced it."""
    for tag in ("xbrl", "bmv", "note", "search", "table", "regex_table", "prose", "llm"):
        if f"[{tag}]" in source_line:
            return tag
    if "[calc" in source_line:   # [calc] / [calculated]
        return "calc"
    return "other"

# ── Tolerance thresholds ──────────────────────────────────────────────────────
# currency/area: relative error; pct: absolute pp; count: exact units
TOLERANCE = {
    'currency': 0.02,
    'pct':      0.5,
    'count':    0,
    'area':     0.005,
}

# Absolute-tolerance floor (in the metric's own units), applied on top of the
# relative TOLERANCE above. BMV ground-truth spreadsheets routinely round
# monetary figures to whole millions, so a value the PDF prints as -20,514
# (thousands) → -20.51M is recorded as -21M. On small-magnitude figures that
# half-million rounding exceeds the 2% relative bar even though the extraction
# is exact. The floor admits "rounded to the nearest million" and nothing more:
# a row PASSes if it is within EITHER the relative tolerance OR this absolute
# amount. Currency is in millions, so 0.5 == ±half a million. Genuine errors
# (wrong row/scale/period) miss by far more and still FAIL.
ABS_FLOOR = {
    'currency': 0.5,
    'pct':      0.0,
    'count':    0.0,
    'area':     0.0,
}

# ── Company configs ───────────────────────────────────────────────────────────
COMPANIES = {
    'soriana': {
        'config':      'configs/soriana.yaml',
        'source_dir':  'data/reports/soriana',
        'actual_file': 'data/ground_truth/soriana-actual.csv',
        # Consolidated P&L + capex. Reports are prose-first (the income-statement
        # table OCR-parses to garbage); Q4 GT rows hold the QUARTERLY figure.
        'metric_map': {
            'revenue':           ('P&L', 'Total Income',       'currency'),
            'cogs':              ('P&L', 'Cost of Sales',      'currency'),
            'gross_profit':      ('P&L', 'Gross Income',       'currency'),
            'operating_expense': ('P&L', 'Operating Expenses', 'currency'),
            'ebitda':            ('P&L', 'EBITDA',              'currency'),
            'depreciation':      ('P&L', 'D&A',                 'currency'),
            'operating_income':  ('P&L', 'Operating Income',   'currency'),
            'interest_expense':  ('P&L', 'Net Financial Cost', 'currency'),
            'ebt':               ('P&L', 'Pre-tax Income',      'currency'),
            'tax_expense':       ('P&L', 'Tax',                 'currency'),
            'net_income':        ('P&L', 'Net Income',          'currency'),
            'capex':             ('P&L', 'Capex',               'currency'),
            # Rich store/Sodimac KPIs (per-format units & sales-floor m²). Tests the
            # rotated-PDF store reconstruction against independent visual reads.
            'units_hiper':       ('Store Units',    'Hiper',       'count'),
            'units_super':       ('Store Units',    'Super',       'count'),
            'units_mercado':     ('Store Units',    'Mercado',     'count'),
            'units_express':     ('Store Units',    'Express',     'count'),
            'units_cityclub':    ('Store Units',    'City Club',   'count'),
            'total_units':       ('Store Units',    'Total Units', 'count'),
            'area_hiper':        ('Sales Floor m2', 'Hiper',       'area'),
            'area_super':        ('Sales Floor m2', 'Super',       'area'),
            'area_mercado':      ('Sales Floor m2', 'Mercado',     'area'),
            'area_express':      ('Sales Floor m2', 'Express',     'area'),
            'area_cityclub':     ('Sales Floor m2', 'City Club',   'area'),
            'total_sales_floor': ('Sales Floor m2', 'Total Area',  'area'),
            'sodimac_units':     ('Sodimac',        'Units',       'count'),
            'sodimac_area':      ('Sodimac',        'Area',        'area'),
        },
    },
    'herdez': {
        'config':      'configs/herdez.yaml',
        'source_dir':  'data/reports/herdez',
        'actual_file': 'data/ground_truth/herdez-actual.csv',
        # Consolidated P&L + per-segment + equity-in-associates + MegaMex standalone +
        # operations, all in MXN millions (config unit: millions → no currency_scale).
        # Net Sales = management press-release basis (matches the [statement] extractor).
        'metric_map': {
            'revenue':           ('P&L',                  'Net Sales',            'currency'),
            'gross_profit':      ('P&L',                  'Gross Profit',         'currency'),
            'operating_income':  ('P&L',                  'EBIT',                 'currency'),
            'ebitda':            ('P&L',                  'EBITDA',               'currency'),
            'ns_domestic':       ('Segments',             'Domestic',             'currency'),
            'ns_export':         ('Segments',             'Export',               'currency'),
            'ns_conservas':      ('Segments',             'Conservas',            'currency'),
            'ns_impulso':        ('Segments',             'Impulso',              'currency'),
            'equity_associates': ('Equity in Associates', 'Total',                'currency'),
            'equity_megamex':    ('Equity in Associates', 'MegaMex',              'currency'),
            'megamex_net_sales': ('MegaMex Standalone',   'Net Sales',            'currency'),
            'megamex_ebitda':    ('MegaMex Standalone',   'EBITDA',               'currency'),
            'plants':            ('Operations',           'Plants',               'count'),
            'dist_centers':      ('Operations',           'Distribution Centers', 'count'),
        },
    },
    'orbia': {
        'config':      'configs/orbia.yaml',
        'source_dir':  'data/reports/orbia',
        'actual_file': 'data/ground_truth/orbia-actual.csv',
        # USD millions as printed in the English releases (config unit: millions,
        # currency USD → no currency_scale). net_income = MAJORITY line; EBITDA =
        # plain printed EBITDA (not Adjusted). Segment keys exist from 2020-1T
        # (era_gates) — Vinyl/Fluent/Fluor Mexichem chains are different perimeters.
        'metric_map': {
            'revenue':             ('P&L',                       'Net Sales',                       'currency'),
            'cogs':                ('P&L',                       'Cost of Sales',                   'currency'),
            'gross_profit':        ('P&L',                       'Gross Profit',                    'currency'),
            'operating_expense':   ('P&L',                       'Operating Expenses',              'currency'),
            'operating_income':    ('P&L',                       'Operating Income',                'currency'),
            'ebitda':              ('P&L',                       'EBITDA',                          'currency'),
            'interest_expense':    ('P&L',                       'Financial Cost',                  'currency'),
            'ebt':                 ('P&L',                       'Pre-tax Income',                  'currency'),
            'tax_expense':         ('P&L',                       'Tax',                             'currency'),
            'net_income':          ('P&L',                       'Net Majority Income',             'currency'),
            'ns_polymer':          ('Revenues by Business Group', 'Polymer Solutions',              'currency'),
            'ns_building':         ('Revenues by Business Group', 'Building & Infrastructure',      'currency'),
            'ns_precision_ag':     ('Revenues by Business Group', 'Precision Agriculture',          'currency'),
            'ns_connectivity':     ('Revenues by Business Group', 'Connectivity Solutions',         'currency'),
            'ns_fluor':            ('Revenues by Business Group', 'Fluor Solutions',                'currency'),
            'ebitda_polymer':      ('EBITDA by Business Group',  'EBITDA Polymer Solutions',        'currency'),
            'ebitda_building':     ('EBITDA by Business Group',  'EBITDA Building & Infrastructure', 'currency'),
            'ebitda_precision_ag': ('EBITDA by Business Group',  'EBITDA Precision Agriculture',    'currency'),
            'ebitda_connectivity': ('EBITDA by Business Group',  'EBITDA Connectivity Solutions',   'currency'),
            'ebitda_fluor':        ('EBITDA by Business Group',  'EBITDA Fluor Solutions',          'currency'),
            'cfo':                 ('Cash Flow & Leverage',      'Operating Cash Flow',             'currency'),
            'capex':               ('Cash Flow & Leverage',      'Capex',                           'currency'),
            'free_cash_flow':      ('Cash Flow & Leverage',      'Free Cash Flow',                  'currency'),
            'cash':                ('Cash Flow & Leverage',      'Cash',                            'currency'),
            'total_debt':          ('Cash Flow & Leverage',      'Total Debt',                      'currency'),
            'net_debt':            ('Cash Flow & Leverage',      'Net Debt',                        'currency'),
            # 'pct' semantics = absolute tolerance (±0.5) — right scale for a ~2-4x ratio
            'net_debt_to_ebitda':  ('Cash Flow & Leverage',      'Net Debt to EBITDA',              'pct'),
        },
    },
    'grupo_mexico': {
        'config':      'configs/grupo_mexico.yaml',
        'source_dir':  'data/reports/grupo_mexico',
        'actual_file': 'data/ground_truth/grupo_mexico-actual.csv',
        # USD millions as printed (config currency USD, unit millions). Consolidated
        # P&L from the standardized "(GM)" statement / page-2 table; net_income =
        # MAJORITY ("Utilidad Neta Controladora"). Divisions = Minera/Transportes/
        # Infraestructura net sales + EBITDA. Cash/debt from the balance sheet.
        'metric_map': {
            'revenue':                 ('P&L',                 'Net Sales',        'currency'),
            'cogs':                    ('P&L',                 'Cost of Sales',    'currency'),
            'operating_income':        ('P&L',                 'Operating Income', 'currency'),
            'ebitda':                  ('P&L',                 'EBITDA',           'currency'),
            'net_income':              ('P&L',                 'Net Income',       'currency'),
            'ns_minera':               ('Net Sales by Division', 'Minera',           'currency'),
            'ns_transportes':          ('Net Sales by Division', 'Transportes',      'currency'),
            'ns_infraestructura':      ('Net Sales by Division', 'Infraestructura',  'currency'),
            'ebitda_minera':           ('EBITDA by Division',  'Minera EBITDA',           'currency'),
            'ebitda_transportes':      ('EBITDA by Division',  'Transportes EBITDA',      'currency'),
            'ebitda_infraestructura':  ('EBITDA by Division',  'Infraestructura EBITDA',  'currency'),
            'cash':                    ('Balance & Leverage',  'Cash',             'currency'),
            'total_debt':              ('Balance & Leverage',  'Total Debt',       'currency'),
            'net_debt':                ('Balance & Leverage',  'Net Debt',         'currency'),
        },
    },
    'lacomer': {
        'config':      'configs/lacomer.yaml',
        'source_dir':  'data/reports/lacomer',
        'actual_file': 'data/ground_truth/lacomer-actual.csv',
        # key: (section_header_label, row_label, tolerance_type)
        'metric_map': {
            'revenue':                 ('Revenues',       'La Comer',         'currency'),
            'sss_lacomer':             ('Revenues',       'SSS YoY',          'pct'),
            'total_stores':            ('Stores',         'Total Stores',     'count'),
            'stores_la_comer':         ('Stores',         'La Comer',         'count'),
            'stores_fresko':           ('Stores',         'Fresko',           'count'),
            'stores_city_market':      ('Stores',         'City Market',      'count'),
            'stores_sumesa':           ('Stores',         'Sumesa',           'count'),
            'stores_city_market_cafe': ('Stores',         'City Market Café', 'count'),
            'sales_floor_total':       ('Sales Floor m2', 'Total Area',       'area'),
            'sales_floor_la_comer':    ('Sales Floor m2', 'La Comer',         'area'),
            'sales_floor_fresko':      ('Sales Floor m2', 'Fresko',           'area'),
            'sales_floor_city_market': ('Sales Floor m2', 'City Market',      'area'),
            'sales_floor_sumesa':      ('Sales Floor m2', 'Sumesa',           'area'),
        },
    },
    'sport': {
        'config':      'configs/sport.yaml',
        'source_dir':  'data/reports/sport',
        'actual_file': 'data/ground_truth/sports-pream.csv',
        # SPORT is extracted in miles_mxn (thousands); ground truth is P$mn.
        # Scale currency/area extractions by 0.001 before comparison.
        'currency_scale': 0.001,
        'metric_map': {
            'revenue':              ('Revenues',                       'Total Revenue',                            'currency'),
            'revenue_memberships':  ('Consolidated Income Statement',  'Maintenance Fees and Membership Revenue',  'currency'),
            'revenue_sports':       ('Sports and Other Revenue',       'Sports Revenue and Other Business Revenue', 'currency'),
            'revenue_sponsorships': ('Other Income',                   'Sponsorship and Other Activities Revenue', 'currency'),
            # Headline EBITDA is reported WITH IFRS 16; the pre-IFRS-16 figure is a
            # separate metric (ebitda_sin_ifrs). Map each to the matching GT row.
            'ebitda':               ('Financial metrics',              'EBITDA (post IFRS 16)',                    'currency'),
            'ebitda_sin_ifrs':      ('Financial metrics',              'EBITDA (pre IFRS 16)',                     'currency'),
            'clientes_activos':     ('Operating Metrics',              'Total Active Clients',                     'count'),
            'clubs_count':          ('Number of Clubs',                'Total Clubs in Operation',                 'count'),
            'net_churn':            ('Operating Metrics',              'Average Net Churn',                        'pct'),
            'gross_churn':          ('Operating Metrics',              'Average Gross Churn',                      'pct'),
            'visits_per_member':    ('Operating Metrics',              'Monthly Visits Per Client',                'pct'),
        },
    },
    'walmex': {
        'config':      'configs/walmex.yaml',
        'source_dir':  'data/reports/walmex',
        'actual_file': 'data/ground_truth/walmex-actual.csv',
        'metric_map': {
            'revenue':              ('Revenues',         'Walmart de México y CAM',      'currency'),
            'gross_profit':         ('Gross Profit',     'Consolidated',                 'currency'),
            'operating_income':     ('Operating Income', 'Consolidated Operating Income', 'currency'),
            'revenue_mexico':       ('Mexico',           'Total MX Sales',               'currency'),
            'revenue_cam':          ('Central America',  'Total CAM Sales',              'currency'),
            'sss_mexico':           ('Mexico',           'MX SSS YoY',                   'pct'),
            'sss_cam':              ('Central America',  'CAM SSS YoY',                  'pct'),
            'total_stores':         ('Stores',           'Total Stores',                 'count'),
            'total_stores_mexico':  ('Stores',           'Total Stores Mexico',          'count'),
            'total_stores_cam':     ('Stores',           'Total Stores CAM',             'count'),
            'sales_floor_mexico':   ('Sales Floor (m2)', 'Area Mexico',                  'area'),
            'sales_floor_cam':      ('Sales Floor (m2)', 'Area Central America',         'area'),
            'ebitda':               ('EBITDA',           'Consolidated',                 'currency'),
            'ticket_mexico':        ('Mexico',           'Ticket',                       'pct'),
            'traffic_mexico':       ('Mexico',           'Traffic',                      'pct'),
        },
    },
    # ── New companies — mapped to the REPORTED-FIGURE rows in each GT CSV
    # (consolidated + segment revenue/profitability + operational KPIs the
    # company prints). Analyst-derived rows (YoY, "As % of", 2-year comp,
    # "Check", FX effect, unit-growth) are intentionally excluded. ──
    'liverpool': {
        'config':      'configs/liverpool.yaml',
        'source_dir':  'data/reports/liverpool',
        'actual_file': 'data/ground_truth/liverpool-actual.csv',
        'metric_map': {
            'revenue':               ('Revenues',                    'Consolidated',                         'currency'),
            'revenue_commercial':    ('Sale of goods and services',  'Commercial Income',                    'currency'),
            'revenue_suburbia':      ('Sale of goods and services',  'Suburbia',                             'currency'),
            'revenue_financial':     ('Financial Business',          'Financial Business income',            'currency'),
            'revenue_real_estate':   ('Real Estate',                 'Real Estate income',                   'currency'),
            'sss_liverpool':         ('Sale of goods and services',  'Liverpool SSS YoY',                    'pct'),
            'sss_suburbia':          ('Sale of goods and services',  'Suburbia SSS YoY',                     'pct'),
            'ecommerce_penetration': ('Sale of goods and services',  'E-Commerce Penetration',               'pct'),
            'npl':                   ('Financial Business',          'NPL',                                  'pct'),
            'cards_total':           ('Financial Business',          'Total number of cards (in thousands)', 'count'),
            'total_stores':          ('Stores',                      'Total Stores',                         'count'),
            'stores_liverpool':      ('Stores',                      'Total Liverpool',                      'count'),
            'sales_floor_total':     ('Sales Floor m2',              'Total Area',                           'area'),
        },
    },
    'bimbo': {
        'config':      'configs/bimbo.yaml',
        'source_dir':  'data/reports/bimbo',
        'actual_file': 'data/ground_truth/bimbo-actual.csv',
        'metric_map': {
            'revenue':          ('Revenues',         'Total Net sales (Including Eliminations and others)',   'currency'),
            'revenue_na':       ('Revenues',         'Net Sales North America',                               'currency'),
            'revenue_mexico':   ('Revenues',         'Net Sales Mexico',                                      'currency'),
            'revenue_eaa':      ('Revenues',         'Net Sales EAA',                                         'currency'),
            'revenue_latam':    ('Revenues',         'Net Sales LatAm',                                       'currency'),
            'gross_profit':     ('Gross Profit',     'Total Gross Profit (Including Eliminations and others)', 'currency'),
            'gross_margin':     ('Gross Profit',     'Margin',                                                'pct'),
            'operating_income': ('Operating Income', 'Total EBIT (Including Eliminations and others)',         'currency'),
            'operating_margin': ('Operating Income', 'Margin',                                                'pct'),
            'ebitda':           ('Adj. EBITDA',      'Total Adj. EBITDA (Including Eliminations and others)',  'currency'),
            'ebitda_margin':    ('Adj. EBITDA',      'Margin',                                                'pct'),
        },
    },
    'chedraui': {
        'config':      'configs/chedraui.yaml',
        'source_dir':  'data/reports/chedraui',
        'actual_file': 'data/ground_truth/chedraui-actual.csv',
        'metric_map': {
            'revenue':             ('Revenues',         'Grupo Comercial Chedraui',     'currency'),
            'revenue_mexico':      ('Mexico',           'Total MX Sales',               'currency'),
            'revenue_us':          ('United States',    'Total U.S. Sales',             'currency'),
            'sss_mexico':          ('Mexico',           'MX SSS YoY',                   'pct'),
            'sss_us':              ('United States',    'U.S. SSS YoY',                 'pct'),
            'ebitda':              ('EBITDA',           'Consolidated',                 'currency'),
            'ebitda_mexico':       ('bps change',       'Mexico (Self-Service)',        'currency'),
            'ebitda_us':           ('bps change',       'United States (Self-Service)', 'currency'),
            'total_stores':        ('Stores',           'Total Stores',                 'count'),
            'total_stores_mexico': ('Stores',           'Total Stores Mexico',          'count'),
            'total_stores_us':     ('Stores',           'Total Stores U.S.',            'count'),
            'sales_floor_total':   ('Sales Floor (m2)', 'Total Area',                   'area'),
            'sales_floor_mexico':  ('Sales Floor (m2)', 'Area Mexico',                  'area'),
            'sales_floor_us':      ('Sales Floor (m2)', 'Area United States',           'area'),
        },
    },
    # New analyst models (added 2026-06-22). Source: BMV XBRL only (IR sites are
    # bot-protected), so consolidated financials rebuild via the XBRL tier; segment
    # rows that XBRL doesn't break out MISS until report PDFs are obtained.
    'kimber': {
        'config':      'configs/kimber.yaml',
        'source_dir':  'data/reports/kimber',
        'actual_file': 'data/ground_truth/KIMBER_Model_post1Q26_Segments.csv',
        'metric_map': {
            'revenue':          ('Revenues', 'Kimberly-Clark de México', 'currency'),
            'revenue_consumer': ('Revenues', 'Total Consumer',           'currency'),
            'revenue_exports':  ('Revenues', 'Exports',                  'currency'),
        },
    },
    'kof': {
        'config':      'configs/kof.yaml',
        'source_dir':  'data/reports/kof',
        'actual_file': 'data/ground_truth/KOF_Model_post1Q26_Segments.csv',
        'metric_map': {
            'revenue': ('Revenues', 'Consolidated (Excluding Restatement)', 'currency'),
        },
    },
    'femsa': {
        'config':      'configs/femsa.yaml',
        'source_dir':  'data/reports/femsa',
        'actual_file': 'data/ground_truth/FEMSA_Model_post1Q26_Segments.csv',
        'metric_map': {
            'revenue': ('Revenues', 'Fomento Económico Mexicano (Including Others)', 'currency'),
        },
    },
    'lab': {
        'config':      'configs/lab.yaml',
        'source_dir':  'data/reports/lab',
        'actual_file': 'data/ground_truth/LAB_Model_post1Q26_Segments.csv',
        'metric_map': {
            'revenue':              ('Revenues',    'Genomma Lab Internacional', 'currency'),
            'revenue_otc':          ('Revenues',    'Over-the-Counter (OTC)',    'currency'),
            'revenue_personal_care':('Revenues',    'Personal Care',             'currency'),
            'ebitda':               ('Adj. EBITDA', 'Consolidated',              'currency'),
        },
    },
    'tbbb': {
        'config':      'configs/tbbb.yaml',
        'source_dir':  'data/reports/tbbb',
        'actual_file': 'data/ground_truth/TBBB_Model_post1Q26_Segments.csv',
        # Report statements are in thousands; GT model is in P$mn → scale by 1e-3.
        'currency_scale': 0.001,
        'metric_map': {
            'revenue':             ('Revenues', 'TBBB',         'currency'),
            'revenue_merchandise': ('Revenues', 'Merchandise',  'currency'),
            'total_stores':        ('Stores',   'Total Stores', 'count'),
        },
    },
    # ── Extractor-efficiency test companies (no custom_metrics; as-is cascade) ──
    # Ground truth lives in holdout-pdfs/. Only consolidated keys can resolve
    # without bespoke patterns; segment-revenue keys are wired to document intent
    # and quantify the coverage gap (they MISS until custom_metrics are authored).
    'becle': {
        'config':      'configs/becle.yaml',
        'source_dir':  'data/reports/becle',
        'actual_file': 'data/ground_truth/Becle_Model_Post1Q26_Segments.csv',
        'metric_map': {
            'revenue':                ('Revenues',          'Total sales',         'currency'),
            'revenue_us_canada':      ('U.S. & Canada',     'U.S. & Canada sales', 'currency'),
            'revenue_mexico':         ('Mexico',            'Mexico sales',        'currency'),
            'revenue_row':            ('Rest of the World', 'RoW sales',           'currency'),
            'revenue_jose_cuervo':    ('Categories',        'Jose Cuervo',         'currency'),
            'revenue_other_tequilas': ('Categories',        'Other tequilas',      'currency'),
            'revenue_other_spirits':  ('Categories',        'Other Spirits',       'currency'),
            'revenue_rtd':            ('Categories',        'RTD',                 'currency'),
        },
    },
    'ac': {
        'config':      'configs/ac.yaml',
        'source_dir':  'data/reports/ac',
        'actual_file': 'data/ground_truth/AC_Model_post1Q26_Segments.csv',
        'metric_map': {
            'revenue':               ('Revenues',         'Total sales',                  'currency'),
            'operating_income':      ('Operating Income', 'Total EBIT',                   'currency'),
            'ebitda':                ('EBITDA',           'Total EBITDA',                 'currency'),
            'revenue_beverages':     ('Revenues',         'Beverages',                    'currency'),
            'revenue_food_snacks':   ('Revenues',         'Others (incl. Food & Snacks)', 'currency'),
            'revenue_mexico':        ('Mexico',           'Total MX sales',               'currency'),
            'revenue_us':            ('United States',    'Total US sales',               'currency'),
            'revenue_south_america': ('South America',    'Total South Am sales',         'currency'),
        },
    },
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_period(stem: str) -> str | None:
    """Map MD filename stem → 'NQ{YY}A' quarterly period label."""
    # SPORT-style "YYYY-NT" / "YYYY-NQ" (e.g. "2016-1T" → "1Q16A").
    m = re.search(r'(\d{4})-(\d)[TtQq]', stem)
    if m:
        return f"{m.group(2)}Q{m.group(1)[2:]}A"
    m = re.search(r'(\d)[Qq](\d{2})', stem)
    if m:
        return f"{m.group(1)}Q{m.group(2)}A"
    # LACOMER T-format: "4T25", "1T20", "1t16"
    m = re.search(r'(\d)[Tt](\d{2})', stem)
    if m:
        return f"{m.group(1)}Q{m.group(2)}A"
    # Spanish ordinal quarter: "1er-Trimestre-2018", "4to-Trimestre-2018",
    # "4_Trimestre_2023", "La-Comer-2do-Trimestre-2022".
    m = re.search(r'(\d)(?:er|do|to)?[-_ ]+trimestre[-_ ]+(\d{4})', stem, re.IGNORECASE)
    if m:
        return f"{m.group(1)}Q{m.group(2)[2:]}A"
    return None


def _parse_value(cell: str) -> float | None:
    """
    Parse cell strings like '$ 3,347', '(5.6%)', '6.4%', '-2.6%', '4,124'.
    Returns None for blank / dash cells.
    """
    cell = cell.strip()
    if not cell or cell.replace('-', '').replace(' ', '') in ('', '—', 'N/A', 'n/a'):
        return None
    # Parenthetical negatives: "(1.0%)" → -1.0
    neg = cell.startswith('(') and cell.endswith(')')
    cell = cell.strip('()')
    cell = cell.replace('$', '').replace(',', '').replace('%', '').replace(' ', '')
    if not cell:
        return None
    try:
        v = float(cell)
        return -v if neg else v
    except ValueError:
        return None


def parse_actuales(csv_path: str) -> dict:
    """
    Parse wide-format actuales CSV.
    Returns {(section, label): {period: float}}
    - section/label are stripped of whitespace
    - only quarterly periods ('NQ{YY}A' normalised from both A and E tags)
    - rows with a non-empty label but all-blank period values → section headers
    """
    with open(csv_path, newline='', encoding='utf-8-sig') as f:
        rows = list(csv.reader(f))

    # Find the header row: first row containing a quarterly period label like '1Q16A'
    header_row = None
    header_idx = None
    for i, row in enumerate(rows):
        if any(re.match(r'^\d[Qq]\d{2}[AE]$', c.strip()) for c in row):
            header_row = row
            header_idx = i
            break
    if header_row is None:
        raise ValueError(f"No period header found in {csv_path}")

    # Build col_index → normalised period label (quarterly only)
    period_cols: dict[int, str] = {}
    for col_i, cell in enumerate(header_row):
        m = re.match(r'^(\d)[Qq](\d{2})[AE]$', cell.strip())
        if m:
            period_cols[col_i] = f"{m.group(1)}Q{m.group(2)}A"

    lookup: dict = defaultdict(dict)
    current_section = ''

    for row in rows[header_idx + 1:]:
        if len(row) < 2:
            continue
        label = row[1].strip() if len(row) > 1 else ''
        if not label:
            continue  # skip blank-label rows (note/calculation rows)

        # Collect parsed period values for this row
        period_values = {}
        for col_i, period in period_cols.items():
            val = _parse_value(row[col_i]) if col_i < len(row) else None
            period_values[period] = val

        # Section header: non-empty label + all period values blank
        if all(v is None for v in period_values.values()):
            current_section = label
            continue

        # Data row — first-occurrence-wins: don't overwrite a period already set
        # (avoids duplicate-label collision, e.g. 'La Comer' totals vs per-store)
        for period, val in period_values.items():
            if val is not None and period not in lookup[(current_section, label)]:
                lookup[(current_section, label)][period] = val

    return dict(lookup)


def _period_year_quarter(period: str) -> tuple[int, int] | None:
    """'1Q26A' -> (2026, 1). Returns None if unparseable."""
    m = re.match(r'^(\d)[Qq](\d{2})A?$', period)
    if not m:
        return None
    return (2000 + int(m.group(2)), int(m.group(1)))


def excluded_reason(cfg: dict, key: str, period: str) -> str | None:
    """Return a human-readable reason if (key, period) is a documented
    data-unavailability cell that must be EXCLUDED from the accuracy denominator
    (rather than counted as a MISS), else None.

    Two sources, both kept in the company config:
      1. `era_gates: {metric: {available_from_year: YYYY}}` — the metric is not
         reported before that year (extraction MISSes by design).
      2. `unavailable: {metric: {...}}` — documented absence with a proof citation.
         Supports whole-metric, `before_year: YYYY`, or `periods: [..]` scoping.
    Every exclusion is auditable: the reason/proof string is surfaced by the
    EXCLUDED report so a reviewer can confirm the value really is absent.
    """
    yq = _period_year_quarter(period)
    year = yq[0] if yq else None

    gate = (cfg.get('era_gates') or {}).get(key)
    if gate and year is not None:
        afy = gate.get('available_from_year')
        if afy is not None and year < afy:
            return f"era_gate: not reported before {afy}"

    spec = (cfg.get('unavailable') or {}).get(key)
    if spec is None:
        return None
    proof = spec.get('proof', '')
    reason = spec.get('reason', 'documented as unavailable in source')
    tag = '[parse_gap] ' if spec.get('parse_gap') else ''
    suffix = f" | proof: {proof}" if proof else ''

    if 'before_year' in spec:
        if year is not None and year < spec['before_year']:
            return f"{tag}{reason}{suffix}"
        if 'periods' not in spec:
            return None
        # both scopes present → fall through to the explicit period list
    if 'periods' in spec:
        if period in set(spec['periods']):
            return f"{tag}{reason}{suffix}"
        return None
    # No scoping qualifier → the whole metric is unavailable.
    return f"{tag}{reason}{suffix}"


def compute_error(extracted: float, actual: float, tol_type: str) -> float | None:
    if tol_type in ('currency', 'area'):
        return None if actual == 0 else abs(extracted - actual) / abs(actual)
    if tol_type == 'pct':
        return abs(extracted - actual)
    if tol_type == 'count':
        return abs(extracted - actual)
    return None


def classify(
    err: float | None,
    tol_type: str,
    extracted: float | None = None,
    actual: float | None = None,
) -> str:
    if err is None:
        return 'unknown'
    if err <= TOLERANCE[tol_type]:
        return 'PASS'
    # Absolute-floor rescue: pass if the raw gap is within the rounding floor
    # (e.g. GT rounded to whole millions). Only applies where ABS_FLOOR > 0.
    floor = ABS_FLOOR.get(tol_type, 0.0)
    if floor and extracted is not None and actual is not None:
        if abs(extracted - actual) <= floor:
            return 'PASS'
    return 'FAIL'


def fmt_err(err: float | None, tol_type: str) -> str:
    if err is None:
        return '—'
    if tol_type == 'currency':
        return f"{err * 100:.2f}%"
    if tol_type == 'area':
        return f"{err * 100:.3f}%"
    if tol_type == 'pct':
        return f"{err:.2f}pp"
    return f"{err:.0f}"


# ── Main comparison logic ─────────────────────────────────────────────────────

def _fallback_facts_file(md_file: Path, period: str) -> Path | None:
    """Locate (or regenerate) the period's facts artifact under ``<dir>/xbrl/``.

    The ``<stem>_facts.json`` sidecar layout was wiped once in a bulk cleanup
    (2026-07-28, docs/SWEEP_LOG.md sweep #1) and Tier 1 silently died fleet-wide.
    The raw instance JSONs under ``xbrl/`` survived, so the sidecars are no longer
    a single point of failure: prefer an existing ``*_facts.json`` there, else
    regenerate one offline from the raw instance via ``extract_artifacts``.
    """
    xbrl_dir = md_file.parent / 'xbrl'
    if not xbrl_dir.is_dir():
        return None
    from src.download.bmv_xbrl import _logical_stem, extract_artifacts
    for candidate in sorted(xbrl_dir.glob('*_facts.json')):
        if parse_period(_logical_stem(candidate)) == period:
            return candidate
    for raw in sorted(list(xbrl_dir.glob('*.json')) + list(xbrl_dir.glob('*.json.gz'))):
        stem = _logical_stem(raw)
        if raw.name.endswith('_facts.json') or stem.endswith('_facts'):
            continue
        if parse_period(stem) != period:
            continue
        try:
            return extract_artifacts(raw)['facts']
        except Exception:
            return None
    return None


def _build_source(md_file: Path, period: str, enabled_tiers: set) -> PeriodSource:
    """Assemble a PeriodSource, gating each input so disabled tiers can't fire."""
    text = ''
    # The BMV, income-note, command-F search, and regex (prose) tiers read the markdown.
    if {'prose', 'bmv', 'note', 'search'} & enabled_tiers:
        try:
            text = md_file.read_text(encoding='utf-8', errors='replace')
        except Exception:
            text = ''
    facts = None
    if 'xbrl' in enabled_tiers:
        facts_file = md_file.with_name(md_file.stem + '_facts.json')
        if not facts_file.exists():
            facts_file = _fallback_facts_file(md_file, period)
        if facts_file is not None and facts_file.exists():
            import json
            try:
                facts = json.loads(facts_file.read_text(encoding='utf-8')).get('facts')
            except Exception:
                facts = None
    pdf_path = None
    if 'table' in enabled_tiers:
        sibling = md_file.with_suffix('.pdf')
        if sibling.exists():
            pdf_path = sibling
    # NB: DocMeta (parse_pdf with_meta) is intentionally NOT built here. It is
    # diagnostic-only — tiered_extract treats the per-company config table_scale
    # as authoritative (see tiered_extract.py) and nothing consumes PeriodSource.doc.
    # Calling parse_pdf a second time (Tier 2 already opens the PDF) also hung on
    # large BMV filings (liverpool/chedraui), stalling the scorecard. doc=None.
    return PeriodSource(period=period, text=text, facts=facts, pdf_path=pdf_path,
                        period_end=period_end_from_label(period), doc=None)


def _markdown_by_period(source_dir: Path) -> dict:
    """Period → Markdown file for certification.

    The configured directory is authoritative. The estate view is consulted
    **only when that directory yields no Markdown at all** — the genuinely
    broken case: ``data/reports/gruma`` holds 41 PDFs and zero Markdown, so
    gruma certified at zero coverage while its 41 parsed reports sat in
    ``views/reports/gruma``.

    Deliberately not a full union. Merging the two corpora per-period is the
    more principled fix (it is what `build_segments` reads), but it *adds*
    observations — it took `chedraui` from 36 to 43 source files and `femsa`
    from 20 to 31 — and uncertified observations move pinned regression
    baselines. Widening certification's corpus is a decision to make per
    company, with re-certification, not a silent side effect.
    """
    from src.shared.paths import SHARED_PARSED_REPORTS_DIR, SHARED_REPORTS_DIR

    def collect(directory: Path) -> dict:
        found: dict = {}
        if not directory.is_dir():
            return found
        for md_file in sorted(directory.glob('*.md')):
            period = parse_period(md_file.stem)
            if period is not None:
                found.setdefault(period, md_file)
        return found

    configured = collect(source_dir)
    if configured:
        return configured

    slug = source_dir.name
    for fallback in (SHARED_REPORTS_DIR / slug, SHARED_PARSED_REPORTS_DIR / slug):
        recovered = collect(fallback)
        if recovered:
            return recovered
    return {}


def run_comparison(company_name: str, *, enabled_tiers: set | None = None,
                   use_llm: bool = False) -> list:
    comp = COMPANIES[company_name]
    cfg = yaml.safe_load(open(comp['config']))
    metric_defs = apply_config(METRICS, cfg)
    actuales = parse_actuales(comp['actual_file'])
    source_dir = Path(comp['source_dir'])
    md_by_period = _markdown_by_period(source_dir)
    metric_map = comp['metric_map']
    currency_scale = comp.get('currency_scale', 1.0)
    enabled_tiers = enabled_tiers if enabled_tiers is not None else set(DEFAULT_TIERS)

    n_files = n_mapped = 0
    results = []

    # ── Pass 1: extract every period (kept so YTD→quarterly differencing below
    #            can see neighbouring quarters) ────────────────────────────────
    extracted_by_period: dict = {}
    file_by_period: dict = {}
    for period, md_file in sorted(md_by_period.items()):
        n_files += 1
        n_mapped += 1

        src = _build_source(md_file, period, enabled_tiers)
        if not (src.text or src.facts or src.pdf_path):
            continue
        extracted_by_period[period] = extract_metrics_tiered(
            src, metric_defs, cfg,
            use_llm=use_llm and 'llm' in enabled_tiers, tiers=enabled_tiers)
        file_by_period[period] = md_file.stem

    # ── Pass 1.5: acumulado → trimestre (opt-in via cfg['ytd_quarterly']) ──────
    # Many BMV filers report income/segment lines year-to-date. For listed metric
    # keys, the single quarter = YTD(this Q) − YTD(prior Q same year); Q1 YTD IS
    # the quarter. Snapshot YTD first so chained quarters don't subtract an
    # already-differenced value. If the prior quarter is missing, drop to None
    # (honest MISS) rather than pass a YTD value off as the quarter.
    ytd_keys = set(cfg.get('ytd_quarterly') or [])
    ytd_periods = {
        key: set(periods or [])
        for key, periods in (cfg.get('ytd_quarterly_periods') or {}).items()
    }
    ytd_snap_keys = ytd_keys | set(ytd_periods)
    if ytd_snap_keys:
        snap = {p: {k: (extracted_by_period[p][k].current
                        if extracted_by_period[p].get(k) else None)
                    for k in ytd_snap_keys}
                for p in extracted_by_period}
        for period, extracted in extracted_by_period.items():
            yq = _period_year_quarter(period)
            if not yq:
                continue
            year, q = yq
            for key in ytd_snap_keys:
                if key not in ytd_keys and period not in ytd_periods.get(key, set()):
                    continue
                row = extracted.get(key)
                if row is None or row.current is None or q == 1:
                    continue
                prior = f"{q - 1}Q{str(year)[2:]}A"
                pv = snap.get(prior, {}).get(key)
                cv = snap[period][key]
                row.current = round(cv - pv, 4) if (pv is not None and cv is not None) else None

    # ── Pass 1.6: restated comparatives (opt-in via cfg['restated_prior']) ────
    # For listed (metric, period) cells the GT tracks the RESTATED series that is
    # printed only in the FOLLOWING year's filing as the prior-year column
    # (discontinued ops, IFRS-16, IAS-29 re-expression). Replaces the cell with
    # extracted[P+1y].prior and recomputes calc ratios for touched periods.
    from src.extract.tiered_extract import apply_restated_priors
    apply_restated_priors(extracted_by_period, metric_defs, cfg)

    # ── Pass 2: score against ground truth ────────────────────────────────────
    for period, extracted in extracted_by_period.items():
        md_stem = file_by_period[period]
        for key, (section, label, tol_type) in metric_map.items():
            period_map = actuales.get((section, label))
            if period_map is None:
                continue  # this (section, label) not found in actuales at all
            actual_val = period_map.get(period)
            if actual_val is None:
                continue  # period not in actuales for this metric

            # Extractable-obs basis: documented data-unavailable cells are removed
            # from the denominator (not counted as PASS/MISS/FAIL). Every exclusion
            # carries a proof string for audit.
            excl = excluded_reason(cfg, key, period)
            if excl is not None:
                results.append({
                    'company': company_name, 'key': key, 'period': period,
                    'extracted': None, 'actual': actual_val,
                    'error': None, 'status': 'EXCLUDED', 'tol_type': tol_type,
                    'file': md_stem, 'tier': 'excluded', 'reason': excl,
                })
                continue

            row = extracted.get(key)
            ext_val = row.current if row else None
            if ext_val is not None and tol_type in ('currency', 'area'):
                ext_val *= currency_scale
            tier = _tier_of(row.source_line) if row else 'miss'

            if ext_val is None:
                results.append({
                    'company': company_name, 'key': key, 'period': period,
                    'extracted': None, 'actual': actual_val,
                    'error': None, 'status': 'MISS', 'tol_type': tol_type,
                    'file': md_stem, 'tier': 'miss',
                })
            else:
                err = compute_error(ext_val, actual_val, tol_type)
                status = classify(err, tol_type, ext_val, actual_val)
                results.append({
                    'company': company_name, 'key': key, 'period': period,
                    'extracted': ext_val, 'actual': actual_val,
                    'error': err, 'status': status, 'tol_type': tol_type,
                    'file': md_stem, 'tier': tier,
                })

    banner = (f"=== {company_name.upper()} — {n_files} source files, "
              f"{n_mapped} mapped to quarters | tiers={','.join(sorted(enabled_tiers))} ===")
    print(f"\n{banner}")
    return results


# ── Output ────────────────────────────────────────────────────────────────────

def print_summary(results: list) -> None:
    by_key: dict = defaultdict(list)
    for r in results:
        by_key[r['key']].append(r)

    C = [28, 9, 11, 11, 11, 11]
    sep = '-' * sum(C)
    hdr = (f"{'metric':<{C[0]}}{'matched':>{C[1]}}{'covered':>{C[2]}}"
           f"{'accurate':>{C[3]}}{'mean_err':>{C[4]}}{'worst_err':>{C[5]}}")
    print(f"\n{hdr}\n{sep}")

    for key in sorted(by_key):
        all_rows = by_key[key]
        tol = all_rows[0]['tol_type']
        n_excluded = sum(1 for r in all_rows if r['status'] == 'EXCLUDED')
        # Extractable-obs denominator: drop documented-unavailable cells entirely.
        rows = [r for r in all_rows if r['status'] != 'EXCLUDED']
        n_total = len(rows)
        if n_total == 0:
            print(f"{key:<{C[0]}}{'—':>{C[1]}}{'—':>{C[2]}}"
                  f"{'—':>{C[3]}}{'—':>{C[4]}}{'—':>{C[5]}}  ← all {n_excluded} EXCLUDED")
            continue
        n_miss = sum(1 for r in rows if r['status'] == 'MISS')
        n_checked = n_total - n_miss
        n_pass = sum(1 for r in rows if r['status'] == 'PASS')

        covered = f"{n_checked}/{n_total}" if n_checked < n_total else f"{n_checked}/{n_total}"
        covered_pct = f"{n_checked/n_total*100:.0f}%" if n_total else '—'
        accurate_pct = f"{n_pass/n_total*100:.0f}%" if n_total else '—'

        errs = [r['error'] for r in rows if r['error'] is not None]
        mean_e = fmt_err(sum(errs) / len(errs) if errs else None, tol)
        worst_e = fmt_err(max(errs) if errs else None, tol)

        # Highlight rows with issues
        flag = ''
        n_fail = sum(1 for r in rows if r['status'] == 'FAIL')
        bits = []
        if n_fail > 0:
            bits.append(f'{n_fail} FAIL')
        if n_miss > 0:
            bits.append(f'{n_miss} miss')
        if n_excluded > 0:
            bits.append(f'{n_excluded} excl')
        if bits:
            flag = '  ← ' + ', '.join(bits)

        print(f"{key:<{C[0]}}{covered:>{C[1]}}{covered_pct:>{C[2]}}"
              f"{accurate_pct:>{C[3]}}{mean_e:>{C[4]}}{worst_e:>{C[5]}}{flag}")

    # Overall extractable-obs accuracy (PASS / non-excluded obs).
    non_excl = [r for r in results if r['status'] != 'EXCLUDED']
    n_excl = len(results) - len(non_excl)
    n_obs = len(non_excl)
    n_pass = sum(1 for r in non_excl if r['status'] == 'PASS')
    print(sep)
    acc = f"{n_pass/n_obs*100:.0f}%" if n_obs else '—'
    print(f"{'TOTAL (extractable-obs)':<{C[0]}}{f'{n_pass}/{n_obs}':>{C[1]}}"
          f"{acc:>{C[2]}}{f'{n_excl} excl':>{C[3]}}")


def print_tier_summary(results: list) -> None:
    """Which tier answered, and how accurate was it — the cascade's value proof."""
    by_tier: dict = defaultdict(list)
    for r in results:
        by_tier[r['tier']].append(r)

    C = [10, 11, 11, 11, 11]
    sep = '-' * sum(C)
    hdr = (f"{'tier':<{C[0]}}{'answered':>{C[1]}}{'accurate':>{C[2]}}"
           f"{'mean_err':>{C[3]}}{'worst_err':>{C[4]}}")
    print("\nPER-TIER BREAKDOWN  (source tag that produced each value)")
    print("  note: 'search' = deterministic command-F line search; 'regex_table' =")
    print("        flattened-table regex; 'table' = header-aware PDF table cells.")
    print(f"{hdr}\n{sep}")

    for tier in ['xbrl', 'bmv', 'search', 'table', 'regex_table', 'prose', 'calc', 'llm', 'other', 'miss']:
        rows = by_tier.get(tier)
        if not rows:
            continue
        answered = sum(1 for r in rows if r['status'] != 'MISS')
        checked = [r for r in rows if r['status'] in ('PASS', 'FAIL')]
        n_pass = sum(1 for r in checked if r['status'] == 'PASS')
        acc = f"{n_pass/len(checked)*100:.0f}%" if checked else '—'
        errs = [r['error'] for r in checked if r['error'] is not None]
        # errors mix tolerance types; report as a unitless mean of relative/abs values
        mean_e = f"{sum(errs)/len(errs):.4f}" if errs else '—'
        worst_e = f"{max(errs):.4f}" if errs else '—'
        label = 'miss (none)' if tier == 'miss' else tier
        print(f"{label:<{C[0]}}{answered:>{C[1]}}{acc:>{C[2]}}{mean_e:>{C[3]}}{worst_e:>{C[4]}}")


def print_failures(results: list) -> None:
    fails = [r for r in results if r['status'] == 'FAIL']
    if not fails:
        return
    print(f"\n{'─'*80}")
    print("ACCURACY FAILURES  (extraction fired but value outside tolerance):")
    print(f"{'metric':<28}{'period':<9}{'extracted':>14}{'actual':>14}{'error':>10}")
    print('─' * 75)
    for r in sorted(fails, key=lambda x: (x['key'], x['period'])):
        tol = r['tol_type']
        ext = f"{r['extracted']:,.2f}" if tol in ('currency', 'area') else \
              f"{r['extracted']:.2f}" if tol == 'pct' else f"{r['extracted']:.0f}"
        act = f"{r['actual']:,.2f}" if tol in ('currency', 'area') else \
              f"{r['actual']:.2f}" if tol == 'pct' else f"{r['actual']:.0f}"
        print(f"{r['key']:<28}{r['period']:<9}{ext:>14}{act:>14}{fmt_err(r['error'], tol):>10}")


def print_misses(results: list) -> None:
    misses: dict = defaultdict(list)
    for r in results:
        if r['status'] == 'MISS':
            misses[r['key']].append(r['period'])
    if not misses:
        return
    print(f"\n{'─'*80}")
    print("COVERAGE MISSES  (extraction returned None for period present in actuales):")
    for key in sorted(misses):
        periods = sorted(set(misses[key]))
        sample = ', '.join(periods[:6])
        tail = f', … ({len(periods)} total)' if len(periods) > 6 else f' ({len(periods)} total)'
        print(f"  {key:<30} {sample}{tail}")


def print_excluded(results: list) -> None:
    """Audit ledger: every cell removed from the denominator, with its proof."""
    excl = [r for r in results if r['status'] == 'EXCLUDED']
    if not excl:
        return
    by_key: dict = defaultdict(list)
    reason_of: dict = {}
    for r in excl:
        by_key[r['key']].append(r['period'])
        reason_of[r['key']] = r.get('reason', '')
    print(f"\n{'─'*80}")
    print(f"EXCLUDED  (documented data-unavailable — removed from denominator, {len(excl)} cells):")
    for key in sorted(by_key):
        periods = sorted(set(by_key[key]))
        sample = ', '.join(periods[:6])
        tail = f', … ({len(periods)} total)' if len(periods) > 6 else f' ({len(periods)})'
        print(f"  {key:<26} {sample}{tail}")
        print(f"    └ {reason_of[key]}")


def print_actuales_keys(company_name: str) -> None:
    """Debug helper: show what (section, label) keys were found in the actuales CSV."""
    comp = COMPANIES[company_name]
    actuales = parse_actuales(comp['actual_file'])
    print(f"\n[debug] Actuales keys for {company_name}:")
    for (sec, lbl) in sorted(actuales.keys()):
        n = len(actuales[(sec, lbl)])
        print(f"  ({sec!r}, {lbl!r})  — {n} periods")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    # Ablation flags: --tiers xbrl,table,prose  and  --llm
    enabled_tiers: set | None = None
    use_llm = False
    rest: list = []
    i = 0
    while i < len(args):
        if args[i] == '--tiers' and i + 1 < len(args):
            enabled_tiers = {t.strip() for t in args[i + 1].split(',') if t.strip()}
            i += 2
        elif args[i].startswith('--tiers='):
            enabled_tiers = {t.strip() for t in args[i].split('=', 1)[1].split(',') if t.strip()}
            i += 1
        elif args[i] == '--llm':
            use_llm = True
            i += 1
        else:
            rest.append(args[i])
            i += 1
    args = rest or ['all']

    if args == ['--keys']:
        for company in COMPANIES:
            print_actuales_keys(company)
        return

    if args[0] == '--keys':
        for company in args[1:] or list(COMPANIES):
            print_actuales_keys(company)
        return

    companies = list(COMPANIES) if args == ['all'] else args

    for company in companies:
        if company not in COMPANIES:
            print(f"Unknown company: {company}. Options: {', '.join(COMPANIES)}, all")
            continue
        results = run_comparison(company, enabled_tiers=enabled_tiers, use_llm=use_llm)
        print_summary(results)
        print_tier_summary(results)
        print_failures(results)
        print_misses(results)
        print_excluded(results)
        print()


if __name__ == '__main__':
    main()
