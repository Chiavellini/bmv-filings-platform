"""Gap-driven Bloomberg template + native/residual pack merge (offline)."""
from __future__ import annotations

from src.bloomberg.schema import BloombergPack, cell_id, required_rows
from src.coverage.native import filled_cells, merge_packs
from src.coverage.spec import CoverageSpec, Labelled, Peer


def _spec() -> CoverageSpec:
    return CoverageSpec(
        slug="acme", name="Acme", template="industrial",
        blocks=["snapshot_multiples", "historical_multiples", "financial_analysis", "macro_sector"],
        peers=[Peer("peerco", "PEER", "PEER")],
        macro=[Labelled("policy_rate", "Banxico policy rate, %")])


def test_filled_cells_prune_template():
    spec = _spec()
    full = required_rows(spec, base_year=2026)
    # a native pack covering subject price/shares, a peer fundamental, one FY price, and macro
    native = BloombergPack(
        slug="acme",
        subject={"px_last": 50.0, "shares_out": 1000},
        peers={"peerco": {"ebitda_ltm": 2000, "px_last": 30.0}},
        history={2025: 48.0},
        macro={"policy_rate": 8.0})
    filled = filled_cells(native, spec)
    residual = required_rows(spec, base_year=2026, filled=filled)

    assert len(residual) < len(full)                       # template shrank
    resid_ids = {cell_id(r) for r in residual}
    assert "subject:acme:px_last" not in resid_ids         # covered natively → pruned
    assert "peer:peerco:ebitda_ltm" not in resid_ids
    assert "macro:policy_rate:value" not in resid_ids
    assert "history:acme:px_fy_close@2025" not in resid_ids
    # dvd_yield is NOT native → remains in the residual; eps_ntm was dropped entirely
    assert any(r.field == "dvd_yield" for r in residual)
    assert not any(r.field == "eps_ntm" for r in residual)


def test_merge_native_wins_bbg_fills_gaps():
    native = BloombergPack(slug="x", subject={"px_last": 50.0, "shares_out": 1000},
                           macro={"policy_rate": 8.0})
    residual = BloombergPack(slug="x",
                             subject={"px_last": 99.0, "eps_ntm": 3.0, "dvd_yield": 4.0},
                             macro={"policy_rate": 7.0, "usdmxn": 18.0})
    merged = merge_packs(native, residual)
    assert merged.subject["px_last"] == 50.0               # native wins
    assert merged.subject["eps_ntm"] == 3.0                # bbg fills the gap
    assert merged.macro["policy_rate"] == 8.0              # native wins
    assert merged.macro["usdmxn"] == 18.0                  # bbg fills the gap
