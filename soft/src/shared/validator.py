"""
validator.py — Cross-validation and confidence scoring for extracted financial metrics.

After extraction, call validate() to:
  1. Run accounting identity checks (balance sheet, gross profit, EBITDA, etc.)
  2. Run sanity checks (revenue > 0, YoY changes < 500%)
  3. Assign confidence scores to each metric

Rules are only applied when ALL required operands are present with non-None values.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ValidationResult:
    rule: str
    passed: bool
    expected: float | None
    actual: float | None
    delta_pct: float | None    # abs deviation as a fraction (e.g. 0.003 = 0.3%)
    message: str
    metrics_involved: list[str]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def validate(
    metrics: dict,   # dict[str, MetricRow]
    tolerance: float = 0.005,  # default 0.5%
    skip_rules: set | None = None,
) -> list[ValidationResult]:
    """
    Run all applicable cross-validation rules on an extracted metrics dict.

    Returns a list of ValidationResult objects, one per applicable rule.
    Rules are skipped (not returned) when required operands are missing.

    ``skip_rules`` (config: ``validator.skip_rules``) drops the named rules from
    the result — for issuers whose PRINTED figures are definitionally outside a
    rule (e.g. Orbia's free cash flow subtracts interest and lease payments, so
    ``fcf_derivation`` fails on correct extractions every quarter).
    """
    results: list[ValidationResult] = []

    def get(key: str) -> float | None:
        row = metrics.get(key)
        return row.current if row is not None else None

    # -----------------------------------------------------------------------
    # Balance sheet identity: total_assets ≈ total_liabilities + equity
    # -----------------------------------------------------------------------
    ta, tl, eq = get("total_assets"), get("total_liabilities"), get("equity")
    if all(v is not None for v in [ta, tl, eq]):
        rhs = tl + eq
        delta = abs(ta - rhs) / max(abs(ta), 1)
        results.append(ValidationResult(
            rule="balance_sheet_identity",
            passed=delta <= tolerance,
            expected=ta,
            actual=rhs,
            delta_pct=delta,
            message=f"total_assets={ta:,.0f} vs liabilities+equity={rhs:,.0f} (Δ={delta:.2%})",
            metrics_involved=["total_assets", "total_liabilities", "equity"],
        ))

    # -----------------------------------------------------------------------
    # Gross profit identity: gross_profit ≈ revenue - cogs
    # -----------------------------------------------------------------------
    rev, cogs, gp = get("revenue"), get("cogs"), get("gross_profit")
    if all(v is not None for v in [rev, cogs, gp]):
        computed_gp = rev - cogs
        delta = abs(gp - computed_gp) / max(abs(gp), 1)
        results.append(ValidationResult(
            rule="gross_profit_identity",
            passed=delta <= tolerance,
            expected=gp,
            actual=computed_gp,
            delta_pct=delta,
            message=f"gross_profit={gp:,.0f} vs revenue-cogs={computed_gp:,.0f} (Δ={delta:.2%})",
            metrics_involved=["revenue", "cogs", "gross_profit"],
        ))

    # -----------------------------------------------------------------------
    # EBITDA derivation: ebitda ≈ operating_income + depreciation
    # Tolerance is wider (1%) because D&A may include items not in OpIncome
    # -----------------------------------------------------------------------
    ebitda, oi, da = get("ebitda"), get("operating_income"), get("depreciation")
    if all(v is not None for v in [ebitda, oi, da]):
        computed = oi + da
        delta = abs(ebitda - computed) / max(abs(ebitda), 1)
        ebitda_tol = max(tolerance, 0.01)
        results.append(ValidationResult(
            rule="ebitda_derivation",
            passed=delta <= ebitda_tol,
            expected=ebitda,
            actual=computed,
            delta_pct=delta,
            message=f"ebitda={ebitda:,.0f} vs oi+da={computed:,.0f} (Δ={delta:.2%})",
            metrics_involved=["ebitda", "operating_income", "depreciation"],
        ))

    # -----------------------------------------------------------------------
    # Net income identity: net_income ≈ ebt - tax_expense
    # Advisory (any operand could be the wrong one) — feeds confidence scoring
    # so an inconsistent P&L bottom line (e.g. net_income=1 vs ebt-tax=999) is
    # marked low-confidence rather than shown as a confident value.
    # -----------------------------------------------------------------------
    ni, ebt_v, tax_v = get("net_income"), get("ebt"), get("tax_expense")
    mi_v = get("minority_interest")
    if all(v is not None for v in [ni, ebt_v, tax_v]):
        # Consolidated net income = ebt − tax (− minority interest, whose sign
        # convention varies by report). Accept the closest of the candidates so a
        # sizeable minority interest doesn't false-flag a correct bottom line, while
        # a genuinely wrong value (e.g. 1 vs 999) still fails all of them.
        candidates = [ebt_v - tax_v]
        if mi_v is not None:
            candidates += [ebt_v - tax_v - mi_v, ebt_v - tax_v + mi_v]
        computed = min(candidates, key=lambda c: abs(ni - c))
        delta = abs(ni - computed) / max(abs(ni), abs(computed), 1)
        results.append(ValidationResult(
            rule="net_income_identity",
            passed=delta <= max(tolerance, 0.02),
            expected=ni,
            actual=computed,
            delta_pct=delta,
            message=f"net_income={ni:,.0f} vs ebt-tax{'-mi' if mi_v is not None else ''}={computed:,.0f} (Δ={delta:.2%})",
            metrics_involved=["net_income", "ebt", "tax_expense"],
        ))

    # -----------------------------------------------------------------------
    # Net debt identity: net_debt ≈ total_debt - cash
    # -----------------------------------------------------------------------
    nd, td, cash = get("net_debt"), get("total_debt"), get("cash")
    if all(v is not None for v in [nd, td, cash]):
        computed = td - cash
        delta = abs(nd - computed) / max(abs(nd), 1)
        results.append(ValidationResult(
            rule="net_debt_identity",
            passed=delta <= tolerance,
            expected=nd,
            actual=computed,
            delta_pct=delta,
            message=f"net_debt={nd:,.0f} vs total_debt-cash={computed:,.0f} (Δ={delta:.2%})",
            metrics_involved=["net_debt", "total_debt", "cash"],
        ))

    # -----------------------------------------------------------------------
    # Free cash flow derivation: fcf ≈ cfo - capex
    # -----------------------------------------------------------------------
    fcf, cfo, capex = get("free_cash_flow"), get("cfo"), get("capex")
    if all(v is not None for v in [fcf, cfo, capex]):
        computed = cfo - capex
        delta = abs(fcf - computed) / max(abs(fcf), 1)
        results.append(ValidationResult(
            rule="fcf_derivation",
            passed=delta <= tolerance,
            expected=fcf,
            actual=computed,
            delta_pct=delta,
            message=f"fcf={fcf:,.0f} vs cfo-capex={computed:,.0f} (Δ={delta:.2%})",
            metrics_involved=["free_cash_flow", "cfo", "capex"],
        ))

    # -----------------------------------------------------------------------
    # Margin consistency: ebitda_margin ≈ ebitda / revenue × 100
    # Tolerance: 0.1 percentage points (i.e. 0.001 relative to 100% scale)
    # -----------------------------------------------------------------------
    margin, ebitda2, rev2 = get("ebitda_margin"), get("ebitda"), get("revenue")
    if all(v is not None for v in [margin, ebitda2, rev2]) and rev2 != 0:
        computed = ebitda2 / rev2 * 100
        delta_pp = abs(margin - computed)   # in percentage points
        results.append(ValidationResult(
            rule="margin_consistency",
            passed=delta_pp <= 0.2,   # ≤0.2 pp acceptable (rounding in reports)
            expected=margin,
            actual=computed,
            delta_pct=delta_pp / 100,
            message=f"ebitda_margin={margin:.2f}% vs ebitda/revenue={computed:.2f}% (Δ={delta_pp:.2f}pp)",
            metrics_involved=["ebitda_margin", "ebitda", "revenue"],
        ))

    # -----------------------------------------------------------------------
    # Sanity: revenue > 0
    # -----------------------------------------------------------------------
    rev3 = get("revenue")
    if rev3 is not None:
        results.append(ValidationResult(
            rule="sanity_revenue_positive",
            passed=rev3 > 0,
            expected=None,
            actual=rev3,
            delta_pct=None,
            message=f"revenue={rev3:,.0f} {'> 0 ✓' if rev3 > 0 else '≤ 0 — SUSPECT'}",
            metrics_involved=["revenue"],
        ))

    # -----------------------------------------------------------------------
    # Sanity: P&L cost / expense lines are reported as positive magnitudes.
    # A negative value here is a parse/sign error (e.g. a misaligned column),
    # and the offending metric is unambiguous — safe to quarantine.
    # -----------------------------------------------------------------------
    for k in ("cogs", "gross_profit", "operating_expense", "depreciation",
              "interest_expense", "tax_expense"):
        v = get(k)
        if v is not None and v < 0:
            results.append(ValidationResult(
                rule=f"sanity_nonneg_{k}",
                passed=False,
                expected=None,
                actual=v,
                delta_pct=None,
                message=f"{k}={v:,.0f} is negative — likely a parse/sign error",
                metrics_involved=[k],
            ))

    # -----------------------------------------------------------------------
    # Sanity: capex cannot plausibly exceed a few times quarterly revenue.
    # Catches unit artifacts (e.g. "$668 million" mis-scaled to 668,000).
    # -----------------------------------------------------------------------
    capex_s, rev_s = get("capex"), get("revenue")
    if capex_s is not None and rev_s is not None and rev_s > 0 and capex_s > rev_s * 3:
        results.append(ValidationResult(
            rule="sanity_capex_magnitude",
            passed=False,
            expected=rev_s,
            actual=capex_s,
            delta_pct=(capex_s - rev_s) / max(abs(rev_s), 1),
            message=f"capex={capex_s:,.0f} exceeds 3x revenue {rev_s:,.0f} — likely a unit artifact",
            metrics_involved=["capex"],
        ))

    # -----------------------------------------------------------------------
    # Sanity: YoY revenue change < 500% (catches extraction artifacts)
    # -----------------------------------------------------------------------
    rev_row = metrics.get("revenue")
    if rev_row is not None and rev_row.current is not None and rev_row.prior is not None:
        if rev_row.prior != 0:
            yoy = abs(rev_row.current - rev_row.prior) / abs(rev_row.prior)
            results.append(ValidationResult(
                rule="sanity_yoy_change",
                passed=yoy < 5.0,
                expected=None,
                actual=yoy,
                delta_pct=yoy,
                message=f"Revenue YoY change = {yoy:.0%} {'— OK' if yoy < 5.0 else '— SUSPECT (>500%)'}",
                metrics_involved=["revenue"],
            ))

    # -----------------------------------------------------------------------
    # Revenue segment sum: sum of sub-revenues ≈ total revenue (2% tolerance)
    # Uses well-known SPORT segment keys; generic keys as fallback
    # -----------------------------------------------------------------------
    segment_keys = [
        "revenue_memberships", "revenue_sports", "revenue_sponsorships",
        "ingresos_membresias", "ingresos_deportivos", "ingresos_patrocinios",
    ]
    segment_values = [get(k) for k in segment_keys if get(k) is not None]
    rev4 = get("revenue")
    if len(segment_values) >= 2 and rev4 is not None:
        seg_sum = sum(segment_values)
        delta = abs(rev4 - seg_sum) / max(abs(rev4), 1)
        results.append(ValidationResult(
            rule="revenue_segment_sum",
            passed=delta <= 0.02,
            expected=rev4,
            actual=seg_sum,
            delta_pct=delta,
            message=f"revenue={rev4:,.0f} vs segment_sum={seg_sum:,.0f} (Δ={delta:.2%})",
            metrics_involved=["revenue"] + segment_keys,
        ))

    # -----------------------------------------------------------------------
    # Quarantine: no single revenue segment may exceed consolidated revenue.
    # Catches column-misalignment / wrong-row extractions (a segment that
    # accidentally captured the consolidated total or a YTD figure).
    # -----------------------------------------------------------------------
    seg_rev_keys = ["revenue_mexico", "revenue_cam", "revenue_memberships",
                    "revenue_sports", "revenue_sponsorships"]
    if rev4 is not None and rev4 > 0:
        for k in seg_rev_keys:
            sv = get(k)
            if sv is not None and sv > rev4 * 1.001:
                results.append(ValidationResult(
                    rule="segment_not_exceeds_total",
                    passed=False,
                    expected=rev4,
                    actual=sv,
                    delta_pct=(sv - rev4) / max(abs(rev4), 1),
                    message=f"{k}={sv:,.0f} exceeds consolidated revenue {rev4:,.0f} — likely mis-extraction",
                    metrics_involved=[k, "revenue"],
                ))

    if skip_rules:
        results = [r for r in results if r.rule not in skip_rules]
    return results


def flagged_metrics(results: list) -> set:
    """Metric keys involved in any FAILED validation rule (for quarantine/flagging)."""
    flagged: set = set()
    for r in results:
        if not r.passed:
            flagged.update(r.metrics_involved)
    return flagged


# Rules whose failure unambiguously implicates ONE metric (the implausible value),
# safe to quarantine. Identity / segment-sum / YoY rules implicate several operands
# without saying which is wrong, so they are intentionally excluded here — they stay
# advisory (confidence scoring) rather than gating.
def offending_metric(result) -> str | None:
    """The single metric to quarantine for a failed rule, or None if ambiguous.

    Used by the cascade's validator gate to drop a data-implausible value (so it
    becomes an honest MISS) without dropping the correct operands it was checked
    against — e.g. a segment that exceeds consolidated revenue is dropped, but
    `revenue` itself is not.
    """
    if getattr(result, "passed", True):
        return None
    rule = getattr(result, "rule", "")
    if rule == "segment_not_exceeds_total":
        # metrics_involved = [segment_key, "revenue"]; the segment is the offender.
        involved = getattr(result, "metrics_involved", [])
        return involved[0] if involved else None
    if rule == "sanity_revenue_positive":
        return "revenue"
    # Sign / magnitude sanity rules implicate exactly one metric (the implausible
    # value), so they are safe to quarantine into an honest MISS.
    if rule.startswith("sanity_nonneg_") or rule == "sanity_capex_magnitude":
        involved = getattr(result, "metrics_involved", [])
        return involved[0] if involved else None
    return None


# ---------------------------------------------------------------------------
# Confidence scoring
# ---------------------------------------------------------------------------

_IDENTITY_RULES = {
    "balance_sheet_identity": {"total_assets", "total_liabilities", "equity"},
    "gross_profit_identity": {"revenue", "cogs", "gross_profit"},
    "ebitda_derivation": {"ebitda", "operating_income", "depreciation"},
    "net_income_identity": {"net_income", "ebt", "tax_expense"},
    "net_debt_identity": {"net_debt", "total_debt", "cash"},
    "fcf_derivation": {"free_cash_flow", "cfo", "capex"},
    "margin_consistency": {"ebitda_margin", "ebitda", "revenue"},
}


def score_confidence(
    metrics: dict,   # dict[str, MetricRow]
    validation_results: list[ValidationResult],
) -> dict[str, float]:
    """
    Assign a confidence score (0.0–1.0) to each metric.

    Scoring rules (by extraction tier, encoded in the source_line prefix):
      0.95/1.0 — [xbrl] structured IFRS fact (1.0 if a passing identity check involves it)
      0.95/1.0 — [statement] deterministic custom-extractor read of a printed statement/
                 segment table (authoritative like [xbrl]/[bmv]; 1.0 if a passing
                 identity check involves it) — used by gmexico/soriana/herdez/orbia
      0.95/1.0 — [bmv] CNBV/BMV [210000]/[310000] statement read (authoritative;
                 pipeline exempts it from the magnitude gate like [xbrl])
      0.9      — [note] CNBV [800200] income-note read (header-aligned, deterministic)
      0.9      — [qcapex]/[gruma_table] deterministic custom-extractor table reads
      0.9      — [calc] derived calculation
      1.0      — [table] extraction + passed all applicable identity checks
      0.8      — [table] extraction, no applicable identity checks ran
      0.9/0.75 — [search] command-F alias line extraction
      0.85/0.7 — [regex_table] flattened-table regex (lower than true cell read)
      0.6      — [prose] extraction (rounded) + passed applicable checks
      0.4      — [prose] extraction, no applicable checks
      0.3/0.5  — [llm] fallback (0.5 if it passes an applicable cross-check)
      0.2      — any extraction method, FAILED at least one identity check

    Degradation markers (set by the table tier, ride in source_line like tier
    tags) cap the score AFTER tier scoring — they mark reads whose usual
    reliability assumptions did not hold:
      ≤0.5 — [scale_unverified]: monetary table cell whose scale had no positive
             evidence (no XBRL overlap, no caption) — possible 1000× artifact.
      ≤0.6 — [positional]: a period header existed but could not be aligned, so
             the value is a positional guess that may sit in the wrong column.
    """
    failed_rules: set[str] = {r.rule for r in validation_results if not r.passed}
    scores: dict[str, float] = {}

    for key, row in metrics.items():
        source_line = getattr(row, "source_line", "")
        # Source tier embedded in source_line as a "[tier]" prefix by each extractor.
        src = "table"
        if "[xbrl]" in source_line:
            src = "xbrl"
        elif "[statement]" in source_line:
            src = "statement"
        elif "[bmv]" in source_line:
            src = "bmv"
        elif "[note]" in source_line:
            src = "note"
        elif "[qcapex]" in source_line or "[gruma_table]" in source_line:
            src = "custom_table"
        elif "[search]" in source_line:
            src = "search"
        elif "[prose]" in source_line:
            src = "prose"
        elif "[llm]" in source_line:
            src = "llm"
        elif "[regex_table]" in source_line:
            src = "regex_table"
        elif "[calc]" in source_line:
            scores[key] = 0.9  # derived calculation
            continue

        # Check if any identity rule involving this metric failed
        applicable_failed = any(
            rule in failed_rules and key in metrics_set
            for rule, metrics_set in _IDENTITY_RULES.items()
            for metrics_set in [_IDENTITY_RULES[rule]]
            if key in metrics_set
        )

        applicable_ran = any(
            key in metrics_set
            for metrics_set in _IDENTITY_RULES.values()
        )

        if applicable_failed:
            scores[key] = 0.2
        elif src == "xbrl":
            scores[key] = 1.0 if applicable_ran else 0.95
        elif src == "statement":
            # Deterministic custom-extractor read of a printed statement — authoritative
            # like [xbrl] (pipeline.py never drops [statement]); previously fell through
            # to the [table] band (0.8) and was under-graded.
            scores[key] = 1.0 if applicable_ran else 0.95
        elif src == "bmv":
            # Authoritative like [xbrl]/[statement] (magnitude-gate exempt in
            # pipeline.py); previously fell through to the [table] band.
            scores[key] = 1.0 if applicable_ran else 0.95
        elif src == "note":
            scores[key] = 0.9
        elif src == "custom_table":
            scores[key] = 0.9
        elif src == "llm":
            scores[key] = 0.5 if applicable_ran else 0.3
        elif src == "table" and applicable_ran:
            scores[key] = 1.0
        elif src == "table":
            scores[key] = 0.8
        elif src == "search" and applicable_ran:
            scores[key] = 0.9
        elif src == "search":
            scores[key] = 0.75
        elif src == "regex_table" and applicable_ran:
            scores[key] = 0.85
        elif src == "regex_table":
            scores[key] = 0.7
        elif applicable_ran:
            scores[key] = 0.6
        else:
            scores[key] = 0.4

        # Degradation markers cap the tier score (identity checks can't restore
        # trust in a value whose unit or column may be wrong).
        if "[scale_unverified]" in source_line:
            scores[key] = min(scores[key], 0.5)
        if "[positional]" in source_line:
            scores[key] = min(scores[key], 0.6)

    return scores


def validation_summary(results: list[ValidationResult]) -> str:
    """Return a human-readable summary of validation results."""
    if not results:
        return "  No validation rules applicable (insufficient metrics extracted)."
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    lines = [f"\n  Validation: {passed}/{total} rules passed\n"]
    for r in results:
        icon = "✓" if r.passed else "✗"
        lines.append(f"  {icon}  [{r.rule}]  {r.message}")
    return "\n".join(lines)
