"""Independent reconciliation — find faulty calculations the arithmetic gate can't.

The per-company ``math_audit`` proves internal arithmetic *identities* (margin == profit/revenue).
It cannot catch a value that is wrong because its **inputs** are wrong (a ×1000 revenue, a USD eps,
a wrong share count) — the identity still holds. This module checks the engine's outputs against
sources that are *independent* of that arithmetic:

  * **structural** (network-free): relationships that must hold for any real company —
    ``net_margin <= ebitda_margin`` (net income can't exceed EBITDA), margins in [-100,100],
    ``market_cap == price*shares``, the EV bridge, a sane P/S band. These catch the whole class of
    scale/currency errors — e.g. Herdez's 10×-inflated net income (net margin 63.9% vs EBITDA
    margin 16.5%) — with no external data.
  * **golden** (network-free): hand-verified ranges for large, liquid names (eval/reconcile_golden.yaml).
  * **oracle** (live): Yahoo's own computed P/E, P/BV, ROE, margins, shares — an independent second
    opinion. Populated by :func:`src.download.market_data.fetch_key_stats`; degrades gracefully when
    the endpoint is unreachable.

Output is a list of :class:`Finding` (only failures), ranked by severity — a *diagnostic*. It never
mutates the workbook.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Finding:
    company: str
    metric: str
    layer: str                 # "structural" | "golden" | "oracle"
    ours: float | None
    reference: str             # oracle value / golden range / structural rule
    detail: str
    severity: float            # rank key — larger = worse
    root_cause: str = ""
    kind: str = "real"         # "real" (actionable defect) | "noise" (source artifact) | "expected"
    kind_reason: str = ""      # why it was classed noise/expected (shown in the suppressed summary)


@dataclass
class CompanyValues:
    """Normalized figures pulled from a per-company coverage CSV (None where absent)."""
    template: str = ""
    price: float | None = None
    shares_out: float | None = None       # millions
    market_cap: float | None = None       # MXN millions
    net_debt: float | None = None
    minority_interest: float | None = None
    ev: float | None = None
    pe: float | None = None
    pbv: float | None = None
    ptbv: float | None = None
    ev_ebitda: float | None = None
    div_yield: float | None = None
    revenue: float | None = None          # LTM, MXN millions
    ebitda_margin: float | None = None    # %
    net_margin: float | None = None       # %
    gross_margin: float | None = None     # %
    roe: float | None = None              # %
    roic: float | None = None             # %
    raw: dict = field(default_factory=dict)   # {(block,label): value}


# --------------------------------------------------------------------------------------------------
# CSV → normalized values
# --------------------------------------------------------------------------------------------------
def _f(s: str) -> float | None:
    s = (s or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def coverage_csv_path(name: str, slug: str) -> Path:
    return ROOT / "outputs" / name / "csv" / f"{slug}_coverage.csv"


def read_coverage_values(name: str, slug: str, template: str = "") -> CompanyValues | None:
    """Parse ``outputs/<Name>/csv/<slug>_coverage.csv`` into a :class:`CompanyValues`. None if absent."""
    p = coverage_csv_path(name, slug)
    if not p.exists():
        return None
    raw: dict[tuple[str, str], float | None] = {}
    for row in csv.reader(p.open(encoding="utf-8")):
        if len(row) < 3 or row[0] == "block":
            continue
        raw[(row[0], row[1])] = _f(row[2])

    def g(*keys):
        for blk, lbl in keys:
            if (blk, lbl) in raw:
                v = raw[(blk, lbl)]
                if v is not None:
                    return v
        return None

    return CompanyValues(
        template=template,
        price=g(("snapshot_multiples", "Price"), ("bank_snapshot", "Price"), ("reit_snapshot", "Price")),
        shares_out=g(("snapshot_multiples", "Shares out (mn)"), ("bank_snapshot", "Shares out (mn)"),
                     ("reit_snapshot", "Shares/CBFIs out (mn)")),
        market_cap=g(("snapshot_multiples", "Market cap"), ("bank_snapshot", "Market cap"),
                     ("reit_snapshot", "Market cap")),
        net_debt=g(("snapshot_multiples", "Net debt")),
        minority_interest=g(("snapshot_multiples", "Minority interest")),
        ev=g(("snapshot_multiples", "Enterprise value"), ("reit_snapshot", "Enterprise value")),
        pe=g(("snapshot_multiples", "P/E (LTM)"), ("bank_snapshot", "P/E (LTM)")),
        pbv=g(("snapshot_multiples", "P/BV"), ("bank_snapshot", "P/BV")),
        ptbv=g(("bank_snapshot", "P/TBV")),
        ev_ebitda=g(("snapshot_multiples", "EV/EBITDA"), ("reit_snapshot", "EV/EBITDA")),
        div_yield=g(("snapshot_multiples", "Dividend yield"), ("bank_snapshot", "Dividend yield"),
                    ("reit_snapshot", "Distribution yield")),
        revenue=g(("financial_analysis", "Revenue (LTM)")),
        ebitda_margin=g(("financial_analysis", "EBITDA margin")),
        net_margin=g(("financial_analysis", "Net margin")),
        gross_margin=g(("financial_analysis", "Gross margin")),
        roe=g(("financial_analysis", "ROE"), ("bank_returns", "ROE")),
        roic=g(("financial_analysis", "ROIC")),
        raw=raw,
    )


# --------------------------------------------------------------------------------------------------
# Layer 1 — structural consistency (network-free)
# --------------------------------------------------------------------------------------------------
def _rel(a: float, b: float) -> float:
    """Relative divergence |a-b|/max(|b|,eps)."""
    return abs(a - b) / max(abs(b), 1e-9)


def structural_checks(company: str, v: CompanyValues) -> list[Finding]:
    out: list[Finding] = []

    def flag(metric, ours, ref, detail, severity, root="", kind="real", kind_reason=""):
        out.append(Finding(company, metric, "structural", ours, ref, detail, severity, root,
                           kind, kind_reason))

    # net income cannot materially exceed EBITDA (both as % of revenue). The Herdez-class catcher.
    # Skip banks/reits (no EBITDA margin concept).
    if v.template not in ("financials", "reit") and v.net_margin is not None and v.ebitda_margin is not None:
        if v.net_margin > 0 and v.net_margin > v.ebitda_margin + 8.0:
            flag("Net margin", v.net_margin, f"<= EBITDA mgn {v.ebitda_margin:.1f}%",
                 f"net margin {v.net_margin:.1f}% exceeds EBITDA margin {v.ebitda_margin:.1f}% "
                 f"— net income likely mis-scaled", 100 + (v.net_margin - v.ebitda_margin),
                 root="net_income scale")

    # margins bounded
    for label, val in (("EBITDA margin", v.ebitda_margin), ("Net margin", v.net_margin),
                       ("Gross margin", v.gross_margin)):
        if val is not None and abs(val) > 100.0:
            flag(label, val, "[-100, 100]%", f"{label} {val:.1f}% is impossible", 50 + abs(val) - 100)

    # an absurdly small share count is a units error (every listed company has ≫1mn shares). px×shares
    # stays internally consistent so the market-cap identity won't catch it, yet every market-cap-derived
    # multiple is off by ~1e6 → treat as a shares scale defect so the build guardrail poisons them.
    if v.shares_out is not None and 0 < v.shares_out < 1.0:
        flag("Shares out (mn)", v.shares_out, ">= 1mn",
             f"shares out {v.shares_out:.6g}mn is implausibly small — likely a unit error", 90,
             root="shares/price")

    # market cap identity (engine computes px*shares; a break means a data inconsistency)
    if v.price is not None and v.shares_out is not None and v.market_cap is not None:
        exp = v.price * v.shares_out
        if _rel(v.market_cap, exp) > 0.02:
            flag("Market cap", v.market_cap, f"price×shares={exp:,.0f}",
                 f"market cap {v.market_cap:,.0f} != price×shares {exp:,.0f}", 40, root="shares/price")

    # EV bridge (industrial): EV = market cap + net debt + minority interest. Minority interest is a
    # real EV component (the engine adds it — peers.py CompanyInputs.ev), so a bridge check that omits
    # it false-flags every company with a minority stake (AC, Herdez, Televisa, …). When the coverage
    # CSV carries the minority-interest row, tie out exactly; when it doesn't (older CSV, pre-rebuild),
    # the residual EV−mktcap−net_debt should itself be a plausible minority interest (≥0, a modest
    # fraction of market cap) — treat that as 'expected', and only flag a negative or oversized residual.
    if v.ev is not None and v.market_cap is not None and v.net_debt is not None:
        mi = getattr(v, "minority_interest", None)
        if mi is not None:
            exp = v.market_cap + v.net_debt + mi
            if _rel(v.ev, exp) > 0.05:
                flag("Enterprise value", v.ev, f"mktcap+netdebt+minority={exp:,.0f}",
                     f"EV {v.ev:,.0f} != mktcap+net_debt+minority {exp:,.0f}", 35)
        else:
            resid = v.ev - v.market_cap - v.net_debt          # = minority interest, if consistent
            mc = abs(v.market_cap) or 1e-9
            if resid < -0.02 * mc or resid > 0.6 * mc:
                flag("Enterprise value", v.ev, f"mktcap+netdebt={v.market_cap + v.net_debt:,.0f}",
                     f"EV {v.ev:,.0f} vs mktcap+net_debt {v.market_cap + v.net_debt:,.0f} — "
                     f"residual {resid:,.0f} implausible as minority interest", 35)
            elif abs(resid) > 0.02 * mc:
                flag("Enterprise value", v.ev, f"mktcap+netdebt+minority≈{v.ev:,.0f}",
                     f"EV includes ~{resid:,.0f} minority interest (not itemized in CSV)", 5,
                     kind="expected",
                     kind_reason="EV−mktcap−net_debt consistent with minority interest")

    # a shown positive P/E on a loss-maker is inconsistent → the P/E cell should be blanked. This is an
    # action ("blank this cell"), not a mis-valued number, so it is 'expected' rather than a real defect.
    if v.pe is not None and v.net_margin is not None and v.net_margin < 0:
        flag("P/E", v.pe, "blank (loss-maker)",
             f"P/E {v.pe:.1f}x shown but net margin is {v.net_margin:.1f}% (loss)", 45,
             root="earnings sign", kind="expected", kind_reason="loss-maker P/E should be blank")

    # P/S sanity — market cap should be a sane multiple of revenue (catches bad shares or revenue)
    if v.market_cap is not None and v.revenue not in (None, 0) and v.template not in ("financials",):
        ps = v.market_cap / v.revenue
        if not (0.05 <= ps <= 40):
            flag("P/Sales", ps, "[0.05, 40]",
                 f"market cap / revenue = {ps:.2f} out of sane band "
                 f"(mktcap {v.market_cap:,.0f} / rev {v.revenue:,.0f})", 60,
                 root="shares/revenue scale")

    return out


# --------------------------------------------------------------------------------------------------
# Layer 4 — golden reference set (network-free)
# --------------------------------------------------------------------------------------------------
_GOLDEN_FIELDS = {  # golden key -> (CompanyValues attr, display metric)
    "shares_out_mn": ("shares_out", "Shares out (mn)"),
    "pe": ("pe", "P/E"),
    "net_margin": ("net_margin", "Net margin"),
    "roe": ("roe", "ROE"),
}


def golden_checks(company: str, v: CompanyValues, entry: dict) -> list[Finding]:
    """entry: {shares_out_mn: [lo,hi], pe: [lo,hi], net_margin: [lo,hi], roe: [lo,hi]}."""
    out: list[Finding] = []
    for gkey, (attr, metric) in _GOLDEN_FIELDS.items():
        rng = entry.get(gkey)
        val = getattr(v, attr)
        if not rng or val is None:
            continue
        lo, hi = rng
        if not (lo <= val <= hi):
            dev = (lo - val) if val < lo else (val - hi)
            out.append(Finding(company, metric, "golden", val, f"[{lo}, {hi}]",
                               f"{metric} {val:.2f} outside verified range [{lo}, {hi}]",
                               70 + abs(dev), root_cause="golden range"))
    return out


# --------------------------------------------------------------------------------------------------
# Layer 2 — external oracle (Yahoo). yahoo is the normalized dict from fetch_key_stats (or None).
# --------------------------------------------------------------------------------------------------
# metric -> (CompanyValues attr, yahoo key, relative tolerance)
_ORACLE_MAP = [
    ("Shares out (mn)", "shares_out", "shares_out", 0.25),
    ("Market cap", "market_cap", "market_cap", 0.25),
    ("P/E", "pe", "trailing_pe", 0.35),
    ("P/BV", "pbv", "price_to_book", 0.35),
    ("EV/EBITDA", "ev_ebitda", "ev_ebitda", 0.35),
    ("ROE", "roe", "roe", 0.35),
    ("Net margin", "net_margin", "profit_margin", 0.35),
    ("Dividend yield", "div_yield", "dividend_yield", 0.40),
]


def oracle_checks(company: str, v: CompanyValues, yahoo: dict | None) -> list[Finding]:
    if not yahoo:
        return []
    out: list[Finding] = []
    # root-cause: does our share count disagree with Yahoo's? (drives most multiple errors)
    shares_off = None
    if v.shares_out and yahoo.get("shares_out"):
        r = _rel(v.shares_out, yahoo["shares_out"])
        if r > 0.25:
            shares_off = f"shares_out {v.shares_out:,.0f}mn vs Yahoo {yahoo['shares_out']:,.0f}mn"
    for metric, attr, ykey, tol in _ORACLE_MAP:
        ours = getattr(v, attr)
        ref = yahoo.get(ykey)
        if ours is None or ref is None or ref == 0:
            continue  # ref==0 is a Yahoo blank (e.g. sharesOutstanding null), not a real value
        # multiples must be positive on both sides to compare meaningfully
        if ykey in ("trailing_pe", "price_to_book", "ev_ebitda", "shares_out", "market_cap") \
                and (ref <= 0 or ours <= 0):
            continue
        r = _rel(ours, ref)
        if r > tol:
            root = shares_off if (shares_off and metric not in ("Shares out (mn)",)) else ""
            kind, kind_reason = _oracle_kind(metric, ours, ref, v.template)
            out.append(Finding(company, metric, "oracle", ours,
                               f"Yahoo {ref:.4g}", f"{metric} {ours:.4g} vs Yahoo {ref:.4g} "
                               f"({r*100:.0f}% off)", 20 + r * 100, root_cause=root,
                               kind=kind, kind_reason=kind_reason))
    return out


# Yahoo is authoritative for PRICE-BASED ratios (P/BV, ROE, margins, div yield — currency-neutral) but
# NOT for: (a) absolute BMV share counts / market cap (it reports a single series or float, e.g. KOF
# 525mn vs real 16,806mn), and (b) EV/EBITDA & P/E when its own denominator is garbage (trailing
# EBITDA/EPS spikes → EV/EBITDA of 144x, 270x). Those disagreements say nothing about OUR value, so we
# class them 'noise' and lean on the network-free structural + hand-verified golden layers for the real
# share/scale defects. Reuses validate._MULTIPLE_RANGES so the plausible bands stay in one place.
def _oracle_kind(metric: str, ours: float, ref: float, template: str = "") -> tuple[str, str]:
    from src.coverage.validate import _MULTIPLE_RANGES
    if metric in ("Shares out (mn)", "Market cap"):
        return "noise", "Yahoo unreliable for BMV absolute share/market-cap counts"
    # Dividend yield: ours is the TRAILING-12m actual cash yield; Yahoo reports an INDICATED/forward
    # yield — a systematic methodology gap (a non-payer this year reads 0 while Yahoo shows last year's
    # rate; a special dividend inflates trailing). When ours is in a sane band it's not our defect.
    if metric == "Dividend yield" and 0 <= ours <= 30:
        return "noise", "trailing-actual yield vs Yahoo's indicated/forward yield (methodology differs)"
    # Bank/broker net margin: financials' "revenue" base (net interest income vs total income) differs
    # from Yahoo's, so the ratio isn't comparable — not an engine defect.
    if metric == "Net margin" and template == "financials" and 0 <= ours <= 100:
        return "noise", "financials net margin uses a different revenue base than Yahoo"
    band = {"EV/EBITDA": _MULTIPLE_RANGES.get("EV/EBITDA"),
            "P/E": _MULTIPLE_RANGES.get("P/E (LTM)"),
            "P/BV": _MULTIPLE_RANGES.get("P/BV")}.get(metric)
    if band:
        lo, hi = band
        if lo <= ours <= hi and not (lo <= ref <= hi):
            return "noise", f"ours in [{lo},{hi}], Yahoo {ref:.4g} is the outlier (denominator artifact)"
    # ROE: opposite sign to Yahoo (or Yahoo wildly outside ±100) with ours sane ⇒ Yahoo resolved a
    # different instrument / wrong equity base — not our defect.
    if metric == "ROE" and -100 <= ours <= 100 and (ours * ref < 0 or abs(ref) > 100):
        return "noise", f"ours in ±100%, Yahoo {ref:.4g} is the outlier (wrong instrument / equity base)"
    return "real", ""


# --------------------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------------------
def reconcile_company(name: str, slug: str, clave: str, template: str, *,
                      golden: dict | None = None, yahoo: dict | None = None,
                      values: "CompanyValues | None" = None) -> list[Finding]:
    # ``values`` lets a caller inject freshly-rebuilt figures (matching the shipped master) instead of
    # reading the possibly-stale per-company CSV — used by reconcile_master --rebuild.
    v = values if values is not None else read_coverage_values(name, slug, template)
    if v is None:
        return []
    findings = structural_checks(name, v)
    if golden:
        findings += golden_checks(name, v, golden)
    findings += oracle_checks(name, v, yahoo)
    return findings
