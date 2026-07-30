"""Master-level plausibility gate + USD-reporter share derivation — regression guards.

These lock the two correctness backstops that keep the master coverage matrix trustworthy:
(1) ``build_master._admit_cell`` blanks economically-impossible values that the per-company
arithmetic gate passes (a mis-scaled-but-self-consistent margin/multiple), and (2)
``fundamentals._derive_native`` restates a USD reporter's per-share eps to MXN so shares_out /
market cap / P/E don't inflate ×USDMXN.
"""
import pytest

from scripts.build_master import _admit_cell, _SHARES_POISONED, _guardrail_blank
from src.coverage.fundamentals import _derive_native
from src.extract.xbrl_facts import _USDMXN


@pytest.mark.parametrize("label,unit,value,expected", [
    # in-band → kept
    ("P/E (LTM)", "x", 15.0, 15.0),
    ("EBITDA margin", "pct", 42.0, 42.0),
    ("ROE", "pct", 23.0, 23.0),
    ("Current ratio", "x", 1.8, 1.8),
    # P/E: shown regardless of sign/magnitude (loss-makers negative, near-zero-earnings huge) per the
    # user; only a raw scale artifact (|P/E| > 1000) is rejected.
    ("P/E (LTM)", "x", -68.0, -68.0),      # loss-maker → shown (renders neutral in the heatmap)
    ("P/E (LTM)", "x", 146.0, 146.0),      # near-zero earnings (GBM-style) → shown
    ("P/E (LTM)", "x", 2003.0, None),      # raw scale artifact (>1000) → still blanked
    # out-of-band → blanked (the garbage the identity gate misses)
    ("Net margin", "pct", 1217.0, None),   # Cultiba-style mis-scale
    ("EBITDA margin", "pct", 226.0, None), # CIE-style
    ("ROE", "pct", 307.0, None),           # Kimber-style
    # blank stays blank; never fabricated
    ("P/E (LTM)", "x", None, None),
    # column with no band defined → pass through
    ("Some Unbanded Metric", "x", 999.0, 999.0),
])
def test_admit_cell(label, unit, value, expected):
    assert _admit_cell(label, unit, value) == expected


def test_admit_cell_rejects_nan_inf():
    assert _admit_cell("ROE", "pct", float("nan")) is None
    assert _admit_cell("P/E (LTM)", "x", float("inf")) is None


def test_guardrail_blanks_poisoned_multiples(tmp_path, monkeypatch):
    """A company whose P/S is absurd (Simec-style shares blow-up) has its valuation multiples blanked
    at build time even though each might land in its per-cell band — reuses structural_checks."""
    # A CompanyValues that trips the P/S structural rule (shares/revenue scale).
    class _CV:
        template = "industrial"
        price, shares_out, market_cap = 10.0, 497709.0, 4977090.0   # mktcap = px×shares (consistent)
        revenue = 1000.0                                            # P/S ≈ 4977 → out of band
        net_debt = ev = pe = pbv = ptbv = ev_ebitda = None
        div_yield = ebitda_margin = net_margin = gross_margin = roe = roic = None
        raw: dict = {}

    import src.coverage.reconcile as rec
    monkeypatch.setattr(rec, "read_coverage_values", lambda *a, **k: _CV())
    poisoned = _guardrail_blank("Simec", "simec", "industrial")
    assert poisoned == _SHARES_POISONED
    assert "P/E (LTM)" in poisoned and "EV/EBITDA" in poisoned and "P/BV" in poisoned


def test_guardrail_clean_company_blanks_nothing(monkeypatch):
    class _CV:
        template = "industrial"
        price, shares_out, market_cap = 50.0, 17400.0, 870000.0
        revenue, net_debt, ev = 900000.0, 90000.0, 960000.0
        pe = pbv = ptbv = ev_ebitda = 15.0
        div_yield = 3.0
        ebitda_margin, net_margin, gross_margin = 8.0, 5.0, 25.0
        roe = roic = 20.0
        raw: dict = {}

    import src.coverage.reconcile as rec
    monkeypatch.setattr(rec, "read_coverage_values", lambda *a, **k: _CV())
    assert _guardrail_blank("Walmex", "walmex", "industrial") == set()


def test_derive_native_usd_scales_eps_for_shares():
    """A USD reporter: net_income is MXN-scaled but eps is per-share USD. shares_out must divide by
    the MXN-restated eps (×USDMXN), else it inflates ×USDMXN."""
    ni_mxn, eps_usd = 175_000.0, 10.0
    usd = {"net_income": ni_mxn, "eps": eps_usd}
    _derive_native(usd, currency="USD")
    assert usd["shares_out"] == pytest.approx(ni_mxn / (eps_usd * _USDMXN))

    mxn = {"net_income": ni_mxn, "eps": eps_usd}
    _derive_native(mxn, currency="MXN")
    assert mxn["shares_out"] == pytest.approx(ni_mxn / eps_usd)
    # the USD path yields USDMXN× fewer shares than the naive (buggy) MXN-of-USD division
    assert usd["shares_out"] == pytest.approx(mxn["shares_out"] / _USDMXN)
