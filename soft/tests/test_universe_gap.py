"""universe_gap — roster-vs-coverage reconciliation. Offline: synthetic filings + roster, no network."""
from scripts.universe_gap import (
    RosterEntry,
    classify,
    gap_candidates,
    is_trust_vehicle,
    reconcile,
    render_md,
)
from src.download.bmv_xbrl import XbrlFiling


def _f(ticker, razon, period, kind="quarterly"):
    return XbrlFiling(ticker, razon, "01/01/2025 00:00", period, kind, "http://x/z.zip")


def test_classify_operating_vs_vehicles():
    assert classify("Fibra Uno Administración", "FUNO")[0] == "reit"
    assert classify("Grupo Financiero Banorte", "GFNORTE")[0] == "financials"
    assert classify("Grupo Bimbo, S.A.B. de C.V.", "BIMBO") == ("industrial", "misc_industrial")
    # CKD/CERPI identified by ticker SUFFIX (CK/CC/PI), not by trustee name.
    assert classify("Banco Invex Fideicomiso F/1234", "AMICK") == ("trust", "ckd_cerpi")
    # ETF/tracker by explicit list.
    assert classify("Nacional Financiera", "NAFTRAC") == ("etf", "tracker")


def test_trustee_name_no_longer_flags_real_banks():
    """Regression: the old '_TRUSTEE_MARKERS' rule mis-flagged Santander/BBVA (issued under
    'INSTITUCIÓN DE BANCA MÚLTIPLE') as trust vehicles. Ticker suffix is the reliable signal."""
    razon = "Banco Santander México, Institución de Banca Múltiple"
    assert is_trust_vehicle(razon, "BSMX") is False
    assert classify(razon, "BSMX") == ("financials", "banks")


def test_gap_candidates_excludes_covered():
    filings = [_f("WALMEX", "Wal-Mart", "2025-1T"), _f("NEWCO", "Nueva SAB", "2025-1T")]
    cands = gap_candidates(filings, covered={"WALMEX"})
    assert [c.clave for c in cands] == ["NEWCO"]


def test_reconcile_four_buckets_and_alias():
    roster = [
        RosterEntry("BSMX", "Banco Santander", "add_xbrl", "banks", "financials", "yes", ""),
        RosterEntry("ELEKTRA", "Grupo Elektra", "add_yahoo", "dept_specialty", "industrial", "no", ""),
        RosterEntry("WALMEX", "", "covered", "", "", "", ""),
        RosterEntry("LIVEPOL", "", "covered", "", "", "", "LIVERPOL"),   # covered via ALIAS
    ]
    filings = [
        _f("BSMX", "Banco Santander México", "2025-1T"),   # missing + in archive → A
        _f("FOOCK", "Some CKD trust", "2025-1T"),           # trust (CK suffix) → excluded from ops
        _f("TELMEX", "Teléfonos de México", "2025-1T"),     # operating, off-roster → secondary
    ]
    covered = {"WALMEX", "LIVERPOL"}
    rec = reconcile(filings, roster=roster, covered=covered)
    assert [e.clave for e in rec.bucket_a] == ["BSMX"]
    assert [e.clave for e in rec.bucket_b] == ["ELEKTRA"]
    assert {e.clave for e in rec.bucket_c} == {"WALMEX", "LIVEPOL"}      # LIVEPOL matched by alias
    assert [c.clave for c in rec.off_roster_ops] == ["TELMEX"]           # trust FOOCK dropped


def test_render_md_smoke():
    roster = [RosterEntry("BSMX", "Banco Santander", "add_xbrl", "banks", "financials", "yes", "")]
    rec = reconcile([_f("BSMX", "Banco Santander México", "2025-1T")], roster=roster, covered=set())
    md = render_md(rec, covered_n=136, qcount={"BSMX": 1})
    assert "Bucket A" in md
    assert "| BSMX | Banco Santander | 1 | banks | financials |" in md
