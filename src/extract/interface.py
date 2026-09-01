#!/usr/bin/env python3
"""
interface.py — Clean entry point for the financial metric extraction pipeline.

Accepts fuzzy metric names (e.g. "revenue", "ebitda margin", "active clients",
"churn") and maps them to canonical keys automatically.

─────────────────────────────────────────────────────────────────────────────
Python API
─────────────────────────────────────────────────────────────────────────────
    from src.extract.interface import extract

    # All metrics, output to CSV
    df = extract("https://www.sportsworld.com.mx/inversionistas/",
                 out="results.csv")

    # Specific metrics, fuzzy names OK
    df = extract("https://...", metrics="revenue, ebitda, active clients, churn")

    # Local files
    df = extract("reportes/", metrics=["revenue", "ebitda margin"])

─────────────────────────────────────────────────────────────────────────────
CLI
─────────────────────────────────────────────────────────────────────────────
    python3 interface.py "https://www.sportsworld.com.mx/inversionistas/"
    python3 interface.py "https://..." --metrics "revenue, ebitda, churn"
    python3 interface.py "https://..." --metrics "revenue, ebitda" --out results.csv
    python3 interface.py reportes/  --metrics "active clients, clubs, churn"
    python3 interface.py --search "margin"
    python3 interface.py --list
    python3 interface.py --list --section income
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# Comprehensive alias dictionary
# Maps normalized user phrases → ordered list of canonical metric keys
# ---------------------------------------------------------------------------

ALIASES: dict[str, list[str]] = {
    # ── REVENUE ────────────────────────────────────────────────────────────
    "revenue":                          ["revenue"],
    "sales":                            ["revenue"],
    "top line":                         ["revenue"],
    "turnover":                         ["revenue"],
    "total sales":                      ["revenue"],
    "net sales":                        ["revenue"],
    "gross revenue":                    ["revenue"],
    "net revenue":                      ["revenue"],
    "ingresos":                         ["revenue"],
    "ingresos totales":                 ["revenue"],
    "total revenue":                    ["revenue"],
    # Revenue sub-lines
    "membership revenue":               ["rev_membership", "revenue_memberships"],
    "memberships":                      ["rev_membership", "revenue_memberships"],
    "member fees":                      ["rev_membership"],
    "subscription revenue":             ["rev_membership"],
    "maintenance revenue":              ["rev_maintenance"],
    "maintenance fees":                 ["rev_maintenance"],
    "maintenance and membership":       ["revenue_memberships"],
    "membership maintenance":           ["revenue_memberships"],
    "sports revenue":                   ["rev_sports_only", "revenue_sports"],
    "sports fees":                      ["rev_sports_only"],
    "athletic revenue":                 ["rev_sports_only"],
    "other business revenue":           ["rev_other_business"],
    "other revenue":                    ["rev_other_business", "rev_total_other"],
    "total other income":               ["rev_total_other"],
    "sponsorship revenue":              ["revenue_sponsorships"],
    "sponsorships":                     ["revenue_sponsorships"],
    "other activities":                 ["revenue_sponsorships"],

    # ── GROSS PROFIT ───────────────────────────────────────────────────────
    "gross profit":                     ["gross_profit"],
    "gross income":                     ["gross_profit"],
    "gross margin":                     ["gross_margin"],
    "gross profit margin":              ["gross_margin"],
    "contribution margin":              ["gross_profit"],
    "cogs":                             ["cogs"],
    "cost of goods sold":               ["cogs"],
    "cost of sales":                    ["cogs"],
    "cost of revenue":                  ["cogs"],
    "direct costs":                     ["cogs"],

    # ── EBITDA ─────────────────────────────────────────────────────────────
    "ebitda":                           ["ebitda"],
    "uafida":                           ["ebitda"],
    "ebitda con ifrs":                  ["ebitda"],
    "ebitda post ifrs":                 ["ebitda"],
    "ebitda with ifrs":                 ["ebitda"],
    "ebitda pre ifrs":                  ["ebitda_sin_ifrs"],
    "ebitda sin ifrs":                  ["ebitda_sin_ifrs"],
    "ebitda ex ifrs":                   ["ebitda_sin_ifrs"],
    "ebitda without ifrs":              ["ebitda_sin_ifrs"],
    "ebitda before ifrs":               ["ebitda_sin_ifrs"],
    "ebitda margin":                    ["ebitda_margin", "ebitda_margin_con_ifrs"],
    "ebitda margin con ifrs":           ["ebitda_margin_con_ifrs"],
    "ebitda margin sin ifrs":           ["ebitda_margin_sin_ifrs"],
    "ebitda margin pre ifrs":           ["ebitda_margin_sin_ifrs"],
    "same club ebitda":                 ["same_club_ebitda"],
    "same store ebitda":                ["same_club_ebitda"],
    "like for like ebitda":             ["same_club_ebitda"],
    "comps ebitda":                     ["same_club_ebitda"],

    # ── OPERATING INCOME ───────────────────────────────────────────────────
    "operating income":                 ["operating_income", "operating_profit_con_ifrs"],
    "ebit":                             ["operating_income"],
    "operating profit":                 ["operating_income", "operating_profit_con_ifrs"],
    "operating earnings":               ["operating_income"],
    "operating margin":                 ["operating_margin"],
    "ebit margin":                      ["operating_margin"],
    "operating profit margin":          ["operating_margin"],
    "operating income margin":          ["operating_margin"],
    "operating profit con ifrs":        ["operating_profit_con_ifrs"],
    "operating profit sin ifrs":        ["operating_profit_sin_ifrs"],
    "operating profit pre ifrs":        ["operating_profit_sin_ifrs"],

    # ── NET INCOME ─────────────────────────────────────────────────────────
    "net income":                       ["net_income"],
    "net profit":                       ["net_income"],
    "net earnings":                     ["net_income"],
    "bottom line":                      ["net_income"],
    "earnings":                         ["net_income"],
    "net margin":                       ["net_margin"],
    "net profit margin":                ["net_margin"],
    "profit margin":                    ["net_margin"],
    "ebt":                              ["ebt"],
    "earnings before tax":              ["ebt"],
    "pretax income":                    ["ebt"],
    "pre tax profit":                   ["ebt"],
    "tax":                              ["tax_expense"],
    "income tax":                       ["tax_expense"],
    "tax expense":                      ["tax_expense"],

    # ── D&A ────────────────────────────────────────────────────────────────
    "d&a":                              ["depreciation", "da_con_ifrs"],
    "depreciation":                     ["depreciation", "da_con_ifrs"],
    "amortization":                     ["depreciation"],
    "depreciation and amortization":    ["depreciation", "da_con_ifrs"],
    "d&a con ifrs":                     ["da_con_ifrs"],
    "d&a post ifrs":                    ["da_con_ifrs"],
    "d&a sin ifrs":                     ["da_sin_ifrs"],
    "d&a pre ifrs":                     ["da_sin_ifrs"],
    "d&a ex leases":                    ["da_sin_ifrs", "da_ex_leases_sin_ifrs"],

    # ── INTEREST ───────────────────────────────────────────────────────────
    "interest expense":                 ["interest_expense"],
    "interest":                         ["interest_expense"],
    "financial costs":                  ["interest_expense"],
    "interest income":                  ["interest_income"],
    "interest coverage":                ["interest_coverage"],
    "coverage ratio":                   ["interest_coverage"],

    # ── PER SHARE ──────────────────────────────────────────────────────────
    "eps":                              ["eps"],
    "earnings per share":               ["eps"],
    "upa":                              ["eps"],
    "shares outstanding":               ["shares_outstanding"],
    "shares":                           ["shares_outstanding"],
    "dividends per share":              ["dividends_per_share"],
    "dps":                              ["dividends_per_share"],

    # ── OPEX ───────────────────────────────────────────────────────────────
    "sga":                              ["sga"],
    "selling general and administrative": ["sga"],
    "sg&a":                             ["sga"],
    "administrative costs":             ["admin_costs"],
    "admin costs":                      ["admin_costs"],
    "corporate costs":                  ["admin_costs"],
    "rd":                               ["rd_expense"],
    "r&d":                              ["rd_expense"],
    "research and development":         ["rd_expense"],
    "operating costs":                  ["operating_expense", "total_opex_sin_ifrs"],
    "total operating costs":            ["total_costs_con_ifrs", "total_costs_sin_ifrs"],
    "total opex":                       ["total_opex_con_ifrs", "total_opex_sin_ifrs"],
    "club costs":                       ["club_opex_con_ifrs", "club_opex_sin_ifrs"],
    "club opex":                        ["club_opex_con_ifrs"],
    "club opex con ifrs":               ["club_opex_con_ifrs"],
    "club opex sin ifrs":               ["club_opex_sin_ifrs"],
    "club opex pre ifrs":               ["club_opex_sin_ifrs"],
    "club sales expense":               ["club_sales_expense"],
    "sales expense":                    ["club_sales_expense"],
    "marginal contribution":            ["marginal_contrib_con_ifrs"],
    "marginal contribution con ifrs":   ["marginal_contrib_con_ifrs"],
    "marginal contribution sin ifrs":   ["marginal_contrib_sin_ifrs"],
    "marginal contribution pre ifrs":   ["marginal_contrib_sin_ifrs"],
    "lease payments":                   ["lease_payments_is"],

    # ── MARGINS (generic) ──────────────────────────────────────────────────
    "margins":                          ["ebitda_margin", "gross_margin", "net_margin",
                                         "operating_margin"],
    "all margins":                      ["ebitda_margin", "gross_margin", "net_margin",
                                         "operating_margin", "ebitda_margin_sin_ifrs"],

    # ── BALANCE SHEET ──────────────────────────────────────────────────────
    "cash":                             ["cash"],
    "cash equivalents":                 ["cash"],
    "cash position":                    ["cash"],
    "cash reserves":                    ["cash"],
    "efectivo":                         ["cash"],
    "accounts receivable":              ["accounts_receivable"],
    "receivables":                      ["accounts_receivable"],
    "ar":                               ["accounts_receivable"],
    "inventory":                        ["inventory"],
    "stock":                            ["inventory"],
    "current assets":                   ["current_assets"],
    "liquid assets":                    ["current_assets"],
    "ppe":                              ["ppe_net"],
    "property plant and equipment":     ["ppe_net"],
    "fixed assets":                     ["ppe_net"],
    "tangible assets":                  ["ppe_net"],
    "property":                         ["ppe_net"],
    "intangibles":                      ["intangibles"],
    "intangible assets":                ["intangibles"],
    "goodwill":                         ["goodwill"],
    "right of use":                     ["right_of_use"],
    "rou assets":                       ["right_of_use"],
    "right of use assets":              ["right_of_use"],
    "total assets":                     ["total_assets"],
    "assets":                           ["total_assets"],
    "asset base":                       ["total_assets"],
    "accounts payable":                 ["accounts_payable"],
    "payables":                         ["accounts_payable"],
    "ap":                               ["accounts_payable"],
    "short term debt":                  ["short_term_debt"],
    "current debt":                     ["short_term_debt"],
    "current liabilities":              ["current_liabilities"],
    "long term debt":                   ["long_term_debt"],
    "non current debt":                 ["long_term_debt"],
    "lease liabilities":                ["lease_liabilities"],
    "total debt":                       ["total_debt"],
    "financial debt":                   ["total_debt"],
    "debt":                             ["net_debt", "total_debt"],
    "total liabilities":                ["total_liabilities"],
    "liabilities":                      ["total_liabilities"],
    "equity":                           ["equity"],
    "shareholders equity":              ["equity"],
    "book value":                       ["equity"],
    "net book value":                   ["equity"],
    "retained earnings":                ["retained_earnings"],
    "net debt":                         ["net_debt"],
    "net borrowings":                   ["net_debt"],
    "net leverage":                     ["net_debt"],
    "deuda neta":                       ["net_debt"],
    "non current assets":               ["non_current_assets"],
    "non current liabilities":          ["non_current_liabilities"],
    "prepaid":                          ["prepaid"],
    "prepaid expenses":                 ["prepaid"],

    # ── CASH FLOW ──────────────────────────────────────────────────────────
    "operating cash flow":              ["cfo"],
    "cash from operations":             ["cfo"],
    "ocf":                              ["cfo"],
    "cash flow from operations":        ["cfo"],
    "capex":                            ["capex"],
    "capital expenditures":             ["capex"],
    "capital expenditure":              ["capex"],
    "capital investment":               ["capex"],
    "investments in fixed assets":      ["capex"],
    "free cash flow":                   ["free_cash_flow"],
    "fcf":                              ["free_cash_flow"],
    "distributable cash":               ["free_cash_flow"],
    "investing cash flow":              ["cfi"],
    "cash from investing":              ["cfi"],
    "financing cash flow":              ["cff"],
    "cash from financing":              ["cff"],
    "dividends paid":                   ["dividends_paid"],
    "dividends":                        ["dividends_paid"],
    "buybacks":                         ["share_repurchases"],
    "share repurchases":                ["share_repurchases"],
    "share buybacks":                   ["share_repurchases"],
    "net change in cash":               ["net_change_cash"],

    # ── RATIOS ─────────────────────────────────────────────────────────────
    "net debt to ebitda":               ["net_debt_to_ebitda"],
    "nd ebitda":                        ["net_debt_to_ebitda"],
    "leverage ratio":                   ["net_debt_to_ebitda", "debt_to_equity"],
    "leverage":                         ["net_debt_to_ebitda"],
    "debt to equity":                   ["debt_to_equity"],
    "de ratio":                         ["debt_to_equity"],
    "gearing":                          ["debt_to_equity"],
    "current ratio":                    ["current_ratio"],
    "liquidity ratio":                  ["current_ratio"],
    "working capital ratio":            ["current_ratio"],
    "roe":                              ["return_on_equity"],
    "return on equity":                 ["return_on_equity"],
    "equity return":                    ["return_on_equity"],
    "roa":                              ["return_on_assets"],
    "return on assets":                 ["return_on_assets"],
    "asset return":                     ["return_on_assets"],
    "asset turnover":                   ["asset_turnover"],
    "turnover ratio":                   ["asset_turnover"],
    "roic":                             ["return_on_equity"],  # closest available

    # ── OPERATIONAL KPIs ───────────────────────────────────────────────────
    "active clients":                   ["clientes_activos"],
    "active members":                   ["clientes_activos"],
    "active users":                     ["clientes_activos"],
    "members":                          ["clientes_activos"],
    "subscribers":                      ["clientes_activos"],
    "user base":                        ["clientes_activos"],
    "total members":                    ["clientes_activos"],
    "member base":                      ["clientes_activos"],
    "net churn":                        ["net_churn"],
    "net attrition":                    ["net_churn"],
    "churn":                            ["net_churn", "gross_churn"],
    "churn rate":                       ["net_churn", "gross_churn"],
    "attrition":                        ["net_churn", "gross_churn"],
    "attrition rate":                   ["net_churn", "gross_churn"],
    "gross churn":                      ["gross_churn"],
    "gross attrition":                  ["gross_churn"],
    "membership loss":                  ["gross_churn"],
    "foot traffic":                     ["monthly_visits"],
    "aforo":                            ["monthly_visits"],
    "footfall":                         ["monthly_visits"],
    "visits":                           ["monthly_visits", "visits_per_member"],
    "visitor count":                    ["monthly_visits"],
    "monthly visits":                   ["monthly_visits"],
    "attendance":                       ["monthly_visits"],
    "capacity utilization":             ["monthly_visits"],
    "visit frequency":                  ["visits_per_member"],
    "visits per member":                ["visits_per_member"],
    "visits per client":                ["visits_per_member"],
    "engagement":                       ["visits_per_member"],
    "clubs":                            ["clubs_count"],
    "club count":                       ["clubs_count"],
    "number of clubs":                  ["clubs_count"],
    "locations":                        ["clubs_count"],
    "stores":                           ["clubs_count"],
    "units":                            ["clubs_count"],
    "gym count":                        ["clubs_count"],
    "club openings":                    ["clubs_sw_openings"],
    "openings":                         ["clubs_sw_openings"],
    "new clubs":                        ["clubs_sw_openings"],
    "club closures":                    ["clubs_sw_closures"],
    "closures":                         ["clubs_sw_closures"],
    "club closings":                    ["clubs_sw_closures"],
    "clients per club":                 ["active_clients_per_club"],
    "members per club":                 ["active_clients_per_club"],
    "members per location":             ["active_clients_per_club"],
    "club density":                     ["active_clients_per_club"],
}


# ---------------------------------------------------------------------------
# Resolved metric result
# ---------------------------------------------------------------------------

@dataclass
class Resolved:
    query:        str              # user's original input
    keys:         list[str]        # canonical key(s) — primary first
    confidence:   float            # 0.0–1.0
    method:       str              # how it was resolved: "exact", "alias", "substring", "fuzzy"
    alternatives: list[str] = field(default_factory=list)   # additional matched keys


# ---------------------------------------------------------------------------
# MetricResolver
# ---------------------------------------------------------------------------

class MetricResolver:
    """Maps fuzzy user-supplied metric names to canonical keys.

    Precedence (highest to lowest confidence):
      1. Exact canonical key match
      2. Alias dictionary lookup (exact)
      3. Alias dictionary partial match
      4. Substring search in key / label / label_es
      5. difflib approximate match
    """

    def __init__(self, metric_defs):
        self._defs = {m.key: m for m in metric_defs}
        self._all_keys = list(self._defs.keys())

    # ── Public API ─────────────────────────────────────────────────────────

    def resolve(self, query: str) -> Resolved | None:
        """Resolve a single query string. Returns None if no match found."""
        norm = _normalize(query)

        # 1. Exact canonical key
        if norm.replace(" ", "_") in self._defs:
            key = norm.replace(" ", "_")
            return Resolved(query, [key], 1.0, "exact")

        # 2. ALIASES exact lookup
        if norm in ALIASES:
            keys = [k for k in ALIASES[norm] if k in self._defs]
            if keys:
                primary, *alts = keys
                return Resolved(query, [primary], 0.95, "alias", alternatives=alts)

        # 3. ALIASES partial lookup (alias is contained in query or vice versa)
        alias_hits: list[tuple[float, list[str]]] = []
        for alias, keys in ALIASES.items():
            if alias in norm or norm in alias:
                valid = [k for k in keys if k in self._defs]
                if valid:
                    overlap = len(set(alias.split()) & set(norm.split()))
                    score = 0.85 * overlap / max(len(alias.split()), len(norm.split()))
                    alias_hits.append((score, valid))
        if alias_hits:
            alias_hits.sort(reverse=True)
            best_score, best_keys = alias_hits[0]
            primary, *alts = best_keys
            # Collect alternatives from other alias hits
            for _, more_keys in alias_hits[1:]:
                alts += [k for k in more_keys if k not in [primary] + alts]
            return Resolved(query, [primary], max(0.6, best_score), "alias", alternatives=alts[:3])

        # 4. Substring search in key, label, label_es
        substr_hits: list[tuple[float, str]] = []
        tokens = set(norm.split())
        for key, mdef in self._defs.items():
            haystack = f"{key} {mdef.label} {mdef.label_es}".lower()
            haystack_tokens = set(haystack.replace("_", " ").split())
            coverage = len(tokens & haystack_tokens) / max(len(tokens), 1)
            if coverage > 0:
                substr_hits.append((coverage, key))
        if substr_hits:
            substr_hits.sort(reverse=True)
            top_score, top_key = substr_hits[0]
            if top_score >= 0.5:
                alts = [k for _, k in substr_hits[1:6] if k != top_key]
                return Resolved(query, [top_key], 0.6 + 0.1 * top_score,
                                "substring", alternatives=alts)

        # 5. difflib approximate matching
        choices = self._all_keys + list(ALIASES.keys())
        best_key, best_score = _difflib_best(norm, choices)
        if best_score >= 0.55:
            canonical = best_key if best_key in self._defs else (
                [k for k in ALIASES.get(best_key, []) if k in self._defs] or [None]
            )[0]
            if canonical:
                return Resolved(query, [canonical], best_score * 0.7, "fuzzy")

        return None

    def resolve_many(self, queries: list[str]) -> list[Resolved | None]:
        return [self.resolve(q) for q in queries]

    def search(self, pattern: str) -> list[tuple[str, str, str, str]]:
        """Return (key, label, label_es, section) for metrics matching pattern."""
        norm = _normalize(pattern)
        results = []
        for key, mdef in self._defs.items():
            haystack = f"{key} {mdef.label} {mdef.label_es}".lower()
            if norm in haystack.replace("_", " "):
                results.append((key, mdef.label, mdef.label_es, mdef.section))
        return results

    def all_keys_for(self, resolved_list: list[Resolved | None]) -> list[str]:
        """Flatten resolved results to unique ordered canonical key list."""
        seen: set[str] = set()
        out: list[str] = []
        for r in resolved_list:
            if r is None:
                continue
            for k in [r.keys[0]] + r.alternatives:
                if k and k not in seen and k in self._defs:
                    seen.add(k)
                    out.append(k)
        return out


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _print_resolution_table(queries: list[str],
                             resolved: list[Resolved | None],
                             resolver: MetricResolver,
                             out=sys.stdout) -> None:
    """Print a clean metric mapping table to the terminal."""
    rows = []
    for q, r in zip(queries, resolved):
        mdef = resolver._defs.get(r.keys[0]) if r else None
        if r is None:
            rows.append((f'"{q}"', "—", "No match", "✗"))
        elif r.alternatives:
            label = mdef.label if mdef else r.keys[0]
            alt_str = " + " + ", ".join(r.alternatives[:2])
            rows.append((f'"{q}"', r.keys[0] + alt_str, label, "✓"))
        else:
            label = mdef.label if mdef else r.keys[0]
            rows.append((f'"{q}"', r.keys[0], label, "✓"))

    try:
        from tabulate import tabulate
        table = tabulate(rows,
                         headers=["Input", "Canonical Key(s)", "Label", ""],
                         tablefmt="simple",
                         colalign=("left", "left", "left", "center"))
    except ImportError:
        col_w = [max(len(h), max(len(r[i]) for r in rows))
                 for i, h in enumerate(["Input", "Canonical Key(s)", "Label", ""])]
        sep = "  ".join("─" * w for w in col_w)
        hdr = "  ".join(h.ljust(col_w[i]) for i, h in enumerate(["Input", "Canonical Key(s)", "Label", ""]))
        table = "\n".join([hdr, sep] + ["  ".join(str(r[i]).ljust(col_w[i]) for i in range(4)) for r in rows])

    matched = sum(1 for r in resolved if r is not None)
    total_keys = len({k for r in resolved if r for k in [r.keys[0]] + r.alternatives})
    unmatched = [q for q, r in zip(queries, resolved) if r is None]

    print("\n  Metric Resolution", file=out)
    print("  " + "─" * 62, file=out)
    for line in table.split("\n"):
        print("  " + line, file=out)
    print(f"\n  {matched}/{len(queries)} terms resolved → {total_keys} canonical key(s)", file=out)
    if unmatched:
        print(f"  Unresolved: {', '.join(repr(q) for q in unmatched)}", file=out)
        print("  Tip: run --search TERM to explore available metrics", file=out)
    print(file=out)


def _print_search_results(pattern: str, results: list, out=sys.stdout) -> None:
    if not results:
        print(f"\n  No metrics match '{pattern}'.", file=out)
        print("  Run --list to see all available metrics.", file=out)
        return
    print(f"\n  Metrics matching '{pattern}' ({len(results)} found)", file=out)
    print("  " + "─" * 62, file=out)
    try:
        from tabulate import tabulate
        table = tabulate(results, headers=["Key", "Label", "Spanish Label", "Section"],
                         tablefmt="simple")
        for line in table.split("\n"):
            print("  " + line, file=out)
    except ImportError:
        for key, label, label_es, section in results:
            print(f"  {key:<36}  {label[:40]:<40}  {section}", file=out)
    print(file=out)


def _print_list(metric_defs, section: str | None = None, out=sys.stdout) -> None:
    from collections import defaultdict
    by_section: dict[str, list] = defaultdict(list)
    for m in metric_defs:
        if section is None or m.section == section:
            by_section[m.section].append(m)
    section_order = ["income", "balance", "cashflow", "ratio", "kpi"]
    print(file=out)
    for sec in section_order:
        if sec not in by_section:
            continue
        print(f"  {'─' * 62}", file=out)
        print(f"  {sec.upper()}  ({len(by_section[sec])} metrics)", file=out)
        print(f"  {'─' * 62}", file=out)
        for m in by_section[sec]:
            calc = f"  [= {m.calc}]" if getattr(m, "calc", None) else ""
            print(f"  {m.key:<36}  {m.label[:40]}{calc}", file=out)
    total = sum(len(v) for v in by_section.values())
    print(f"\n  Total: {total} metrics\n", file=out)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    s = s.lower()
    s = re.sub(r"[_\-/]", " ", s)
    s = re.sub(r"[^a-z0-9 &%]", "", s)
    return " ".join(s.split())


def _difflib_best(query: str, choices: list[str]) -> tuple[str, float]:
    best_k, best_s = "", 0.0
    for c in choices:
        score = SequenceMatcher(None, query, _normalize(c)).ratio()
        if score > best_s:
            best_k, best_s = c, score
    return best_k, best_s


def _parse_metric_input(raw: str | list[str] | None) -> list[str] | None:
    """Accept 'rev, ebitda, churn' or ['rev', 'ebitda', 'churn'] or None."""
    if raw is None:
        return None
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[,;|]", raw) if p.strip()]
        return parts or None
    return [p.strip() for p in raw if p.strip()] or None


def _load_metric_defs(config: str | Path | None):
    """Load the universal base plus rows for the selected company only."""
    from src.shared.paths import CONFIGS_DIR as here_configs
    if config is not None:
        cfg_path = Path(config)
        if not cfg_path.exists():
            # Try known shorthand: "sport" → configs/sport.yaml
            candidate = here_configs / f"{config}.yaml"
            if candidate.exists():
                cfg_path = candidate
            else:
                print(f"Warning: config '{config}' not found, using defaults.", file=sys.stderr)
                cfg_path = None
    else:
        # Auto-detect: neutral generic config first — sport.yaml is a company
        # config and must never leak its overrides into unconfigured runs.
        cfg_path = None
        for name in ("generic.yaml", "sport.yaml"):
            candidate = here_configs / name
            if candidate.exists():
                cfg_path = candidate
                break

    from src.model.financial_model import METRICS
    base = list(METRICS)

    if cfg_path:
        try:
            from src.model.financial_model import apply_config, load_config
            cfg = load_config(cfg_path)
            # apply_config merges overrides and custom_metrics.  Do not append
            # SPORT_EXTENDED_METRICS here: those rows used to leak into every
            # company and made the Launchpad catalog look SPORT-specific.
            base = apply_config(METRICS, cfg)
            from src.model.analyst_model_metrics import apply_analyst_model_metrics
            base = apply_analyst_model_metrics(base, cfg_path.stem)
        except Exception as e:
            print(f"Warning: could not load config {cfg_path}: {e}", file=sys.stderr)

    return base


# ---------------------------------------------------------------------------
# Core extract() — Python API
# ---------------------------------------------------------------------------

def extract(
    source: str | Path,
    metrics: str | list[str] | None = None,
    *,
    out: str | Path | None = None,
    config: str | Path | None = None,
    validate: bool = True,
    long: bool = False,
    verbose: bool = True,
    fmt: str = "simple",
) -> "pd.DataFrame":
    """Extract financial metrics from an IR link, directory, or report file.

    Args:
        source:   One of:
                    - HTTP/HTTPS URL  → download PDFs from IR page and extract
                    - Path to directory → process all PDFs/MDs found inside
                    - Path to a .md or .pdf file → extract from that file
        metrics:  Optional — which metrics to include. Accepts fuzzy names:
                    "revenue, ebitda, churn"          (comma-separated string)
                    ["revenue", "ebitda", "churn"]    (list)
                    None                              → all available metrics
        out:      If given, save result CSV to this path.
        config:   YAML config path or shorthand ("sport", "generic").
                  Auto-detects configs/sport.yaml if not specified.
        validate: Run cross-validation checks (default True).
        long:     If True, CSV output is long format (one row per metric per period)
                  with confidence and source columns.
        verbose:  Print progress and metric resolution table (default True).
        fmt:      Terminal table format: "simple" | "grid" | "pipe".

    Returns:
        pandas DataFrame — wide format: one row per period, one column per metric.

    Examples:
        df = extract("https://www.sportsworld.com.mx/inversionistas/")
        df = extract("https://...", metrics="revenue, ebitda, churn", out="out.csv")
        df = extract("reportes/", metrics=["active clients", "clubs", "visits"])
        df = extract("reportes/2026-1T.md", metrics="ebitda margin")
    """
    import pandas as pd

    # Load metric definitions
    metric_defs = _load_metric_defs(config)
    resolver = MetricResolver(metric_defs)

    # Resolve metrics
    queries = _parse_metric_input(metrics)
    if queries is not None:
        resolved = resolver.resolve_many(queries)
        if verbose:
            _print_resolution_table(queries, resolved, resolver)
        canonical_keys = resolver.all_keys_for(resolved)
        if not canonical_keys:
            print("Error: no metrics could be resolved. Use --search TERM to explore.",
                  file=sys.stderr)
            return pd.DataFrame()
    else:
        canonical_keys = None  # means "all"
        if verbose:
            n = len(metric_defs)
            print(f"\n  Extracting all {n} available metrics...\n")

    # Run pipeline
    from src.extract.pipeline import run as _pipeline_run
    df = _pipeline_run(
        source,
        metrics=canonical_keys,
        config=config,
        output_csv=Path(out) if out else None,
        output_dir=Path(".") / "downloads",
        do_validate=validate,
        long_format=long,
        fmt=fmt,
        verbose=verbose,
    )

    return df


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        prog="interface.py",
        description="Extract financial metrics from any IR website or local reports.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 interface.py "https://www.sportsworld.com.mx/inversionistas/"
  python3 interface.py "https://..." --metrics "revenue, ebitda, churn" --out results.csv
  python3 interface.py reportes/ --metrics "active clients, clubs, visits" --out kpis.csv
  python3 interface.py reportes/2026-1T.md
  python3 interface.py --search "margin"
  python3 interface.py --list --section income
  python3 interface.py --list

Metric names are fuzzy — these all work:
  "revenue"      "top line"    "sales"      "ingresos"
  "ebitda"       "uafida"      "ebitda pre ifrs"
  "churn"        "churn rate"  "attrition"
  "active users" "members"     "subscribers"
  "capex"        "d&a"         "free cash flow"
  "leverage"     "net debt"    "roe"        "roa"
        """,
    )

    ap.add_argument("source", nargs="?", default=None,
                    help="IR URL, local directory, or report file (.md/.pdf)")
    ap.add_argument("--url", metavar="URL",
                    help="IR website URL (alternative to positional source)")
    ap.add_argument("--dir", metavar="DIR",
                    help="Local directory of PDFs/MDs")
    ap.add_argument("--metrics", metavar="TERMS",
                    help='Comma-separated fuzzy metric names, e.g. "revenue, ebitda, churn"')
    ap.add_argument("--out", metavar="FILE",
                    help="Save output as CSV to this path")
    ap.add_argument("--long", action="store_true",
                    help="Long CSV format: one row per metric per period (includes confidence)")
    ap.add_argument("--config", metavar="YAML",
                    help='Config file or shorthand: "sport", "generic", or path to .yaml')
    ap.add_argument("--no-validate", action="store_true",
                    help="Skip cross-validation")
    ap.add_argument("--fmt", default="simple",
                    choices=["simple", "grid", "pipe", "latex"],
                    help="Terminal table format (default: simple)")
    ap.add_argument("--search", metavar="TERM",
                    help="Search available metrics by keyword and exit")
    ap.add_argument("--list", action="store_true",
                    help="List all available metric keys and exit")
    ap.add_argument("--section", metavar="SECTION",
                    choices=["income", "balance", "cashflow", "ratio", "kpi"],
                    help="Filter --list or --search by section")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress resolution table and progress output")
    args = ap.parse_args()

    metric_defs = _load_metric_defs(args.config)

    # ── --list ──────────────────────────────────────────────────────────────
    if args.list:
        _print_list(metric_defs, section=args.section)
        return

    # ── --search ────────────────────────────────────────────────────────────
    if args.search:
        resolver = MetricResolver(metric_defs)
        results = resolver.search(args.search)
        if args.section:
            results = [(k, l, le, s) for k, l, le, s in results if s == args.section]
        _print_search_results(args.search, results)
        return

    # ── extract ─────────────────────────────────────────────────────────────
    source = args.url or args.dir or args.source
    if source is None:
        ap.error("Provide a SOURCE (URL, directory, or file) or use --search / --list.")

    extract(
        source,
        metrics=args.metrics,
        out=args.out,
        config=args.config,
        validate=not args.no_validate,
        long=args.long,
        verbose=not args.quiet,
        fmt=args.fmt,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
