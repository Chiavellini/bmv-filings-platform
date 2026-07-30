"""Peer/industry cross-section — compute comparable valuation multiples for the subject and
each peer from a common set of inputs, then summarise (median) across the peer group.

The subject's fundamentals come from filings ([filing]); each peer's come from the Bloomberg
pack ([bbg]). Both are reduced to the same ``CompanyInputs`` shape so the multiple math is
identical and auditable.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass


def safe_div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None:
        return None
    if den == 0 or den != den or num != num:
        return None
    return num / den


@dataclass
class CompanyInputs:
    """Everything needed to compute one company's headline multiples."""
    name: str
    px_last: float | None = None
    shares_out: float | None = None
    net_debt: float | None = None
    minority_interest: float | None = 0.0
    # fundamentals (LTM)
    sales: float | None = None
    ebitda: float | None = None
    net_income: float | None = None
    equity: float | None = None
    fcf: float | None = None
    eps_ntm: float | None = None
    dvd_yield: float | None = None
    tangible_book: float | None = None   # banks: for P/TBV
    # REITs / FIBRAs:
    ffo: float | None = None             # funds from operations (total)
    affo: float | None = None            # adjusted FFO (total)
    nav_ps: float | None = None          # net asset value per share/CBFI
    distribution_yield: float | None = None

    @property
    def market_cap(self) -> float | None:
        if self.px_last is None or self.shares_out is None:
            return None
        return self.px_last * self.shares_out

    @property
    def ev(self) -> float | None:
        mc = self.market_cap
        if mc is None or self.net_debt is None:
            return None
        return mc + self.net_debt + (self.minority_interest or 0.0)


# The multiples every company row exposes (key, label, unit).
MULTIPLES = [
    ("pe_ltm", "P/E (LTM)", "x"),
    ("ev_ebitda", "EV/EBITDA", "x"),
    ("ev_sales", "EV/Sales", "x"),
    ("pbv", "P/BV", "x"),
    ("pfcf", "P/FCF", "x"),
    ("dvd_yield", "Dividend yield", "pct"),
]


def company_multiples(c: CompanyInputs) -> dict[str, float | None]:
    """Compute the headline (industrial) multiples for one company."""
    mc, ev = c.market_cap, c.ev
    return {
        "pe_ltm": safe_div(mc, c.net_income),
        "ev_ebitda": safe_div(ev, c.ebitda),
        "ev_sales": safe_div(ev, c.sales),
        "pbv": safe_div(mc, c.equity),
        "pfcf": safe_div(mc, c.fcf),
        "dvd_yield": c.dvd_yield,
    }


# Bank multiples — NO EV/EBITDA (EV is meaningless for banks); book-value driven.
MULTIPLES_BANK = [
    ("pe_ltm", "P/E (LTM)", "x"),
    ("pbv", "P/BV", "x"),
    ("ptbv", "P/TBV", "x"),
    ("dvd_yield", "Dividend yield", "pct"),
]


def company_multiples_bank(c: CompanyInputs) -> dict[str, float | None]:
    """Compute bank multiples for one company (equity/tangible-book driven, no EV)."""
    mc = c.market_cap
    return {
        "pe_ltm": safe_div(mc, c.net_income),
        "pbv": safe_div(mc, c.equity),
        "ptbv": safe_div(mc, c.tangible_book),
        "dvd_yield": c.dvd_yield,
    }


# REIT / FIBRA multiples — FFO/NAV/distribution driven (EV/EBITDA secondary).
MULTIPLES_REIT = [
    ("p_ffo", "P/FFO", "x"),
    ("p_affo", "P/AFFO", "x"),
    ("ev_ebitda", "EV/EBITDA", "x"),
    ("dist_yield", "Distribution yield", "pct"),
]


def company_multiples_reit(c: CompanyInputs) -> dict[str, float | None]:
    """Compute REIT multiples for one company."""
    mc, ev = c.market_cap, c.ev
    return {
        "p_ffo": safe_div(mc, c.ffo),
        "p_affo": safe_div(mc, c.affo),
        "ev_ebitda": safe_div(ev, c.ebitda),
        "dist_yield": c.distribution_yield,
    }


@dataclass
class CrossSection:
    subject: str                                   # subject display name
    order: list[str]                               # column order: subject then peers
    multiples: dict[str, dict[str, float | None]]  # multiple_key -> {company_name: value}
    median: dict[str, float | None]                # multiple_key -> peer-group median

    def subject_vs_median(self, mkey: str) -> float | None:
        """Subject premium/(discount) to the peer median, as a ratio (1.0 = in line)."""
        return safe_div(self.multiples.get(mkey, {}).get(self.subject), self.median.get(mkey))


def build_cross_section(subject: CompanyInputs, peers: list[CompanyInputs],
                        multiples=MULTIPLES, multiples_fn=company_multiples) -> CrossSection:
    """Assemble the subject + peers multiples table and the peer-median column.

    Pass ``multiples=MULTIPLES_BANK, multiples_fn=company_multiples_bank`` for banks.
    """
    companies = [subject] + peers
    order = [c.name for c in companies]
    is_bank = multiples_fn is company_multiples_bank  # banks get tighter plausibility bands
    mults: dict[str, dict[str, float | None]] = {mkey: {} for mkey, _l, _u in multiples}
    for c in companies:
        mm = multiples_fn(c)
        is_subject = c.name == subject.name
        for mkey, _l, _u in multiples:
            v = mm.get(mkey)
            # A peer multiple reconstructed from a loss-maker (negative earnings/book), a
            # duplicate/zero XBRL fact, or a mis-resolved (~10× wrong) live price comes out negative
            # or absurdly large — "not meaningful". Blank it (renders as n/m) so it neither misleads
            # the table nor poisons the median. The subject column is the analytical focus and is
            # shown as-is even when extreme.
            mults[mkey][c.name] = v if is_subject else admit(mkey, v, bank=is_bank)

    median: dict[str, float | None] = {}
    for mkey, _l, _u in multiples:
        peer_vals = [mults[mkey][p.name] for p in peers if mults[mkey][p.name] is not None]
        median[mkey] = statistics.median(peer_vals) if peer_vals else None

    return CrossSection(subject=subject.name, order=order, multiples=mults, median=median)


# Plausible bands for a peer multiple to be "meaningful". Outside → treated as n/m (blanked in the
# table and excluded from the peer median). Bands are deliberately generous — they catch sign
# errors / scale artifacts / extreme outliers (e.g. a 146× P/E off a mis-scaled EPS), not merely
# richly-valued names. Percentage columns (dividend / distribution yield) have no band.
_PLAUSIBLE_BANDS = {
    "pe_ltm": (0, 80),
    "ev_ebitda": (0, 40), "ev_sales": (0, 15),
    "pbv": (0, 25), "ptbv": (0, 25), "pfcf": (0, 120),
    "p_ffo": (0, 60), "p_affo": (0, 60),
}
# Banks trade in a much tighter band than the general market — a P/BV of 9.5× or a P/E of 52× for a
# Mexican bank is a data artifact (mis-resolved price / bad share count), not a rich valuation.
_BANK_BANDS = {
    "pe_ltm": (0, 40),
    "pbv": (0.1, 5), "ptbv": (0.1, 5),
}
_PCT_MKEYS = {"dvd_yield", "dist_yield", "distribution_yield"}


def plausible(mkey: str, v: float | None, bank: bool = False) -> bool:
    """Whether a multiple value is meaningful enough to show/aggregate for its key."""
    if v is None or v != v:  # None or NaN
        return False
    if mkey in _PCT_MKEYS:
        return True
    bands = _BANK_BANDS if (bank and mkey in _BANK_BANDS) else _PLAUSIBLE_BANDS
    lo, hi = bands.get(mkey, (0.0, float("inf")))
    return lo < v < hi


def admit(mkey: str, v: float | None, bank: bool = False) -> float | None:
    """Return ``v`` if it is a meaningful multiple for ``mkey``, else ``None`` (→ n/m)."""
    return v if plausible(mkey, v, bank=bank) else None
