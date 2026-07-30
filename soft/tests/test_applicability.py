"""Applicability map — the three-state (applicable | na_template | na_source) contract that lets
'100% filled' mean 'every applicable-free cell is filled'. Offline, pure (has_5y passed in)."""
import pytest

from scripts.gen_universe import ABSENT, UNIVERSE, slugify
from src.coverage import applicability as ap
from src.coverage.applicability import column_status, row_applicability
from src.coverage.columns import COLUMNS, COLKEY_BY_LABEL

_STATES = {"applicable", "na_template", "na_source"}
_LABELS = [c[2] for c in COLUMNS]


@pytest.fixture(autouse=True)
def _no_residual(monkeypatch):
    """Isolate the RULE tests from the generated configs/residual_na.json reduction override."""
    monkeypatch.setattr(ap, "_RESIDUAL_NA", {})


def test_convention_curation_per_sector():
    """Analyst-convention collapse: computable-but-non-standard metrics are na_template for the sector,
    while that sector's standard comps stay applicable."""
    def na(label, template, sector, clave, slug):
        return row_applicability(template, sector, clave, slug, has_5y=True)[COLKEY_BY_LABEL[label]]

    # Banks: revenue-CAGR/σ + net margin collapse; ROE/CET1/P-BV/Div yield stay.
    for lbl in ("Net margin", "Revenue CAGR 1y", "Revenue CAGR 5y",
                "Net income CAGR 1y", "Net income CAGR 5y", "Revenue growth stability (σ)"):
        assert na(lbl, "financials", "banks", "GFNORTE", "gfnorte") == "na_template"
    for lbl in ("ROE", "CET1 ratio", "P/BV", "Dividend yield"):
        assert na(lbl, "financials", "banks", "GFNORTE", "gfnorte") == "applicable"

    # REITs: operating-company multiples collapse; P/FFO/ROE/Net-debt-EBITDA stay.
    for lbl in ("EV/EBITDA", "ROIC", "EBITDA margin", "EBITDA YoY (latest)",
                "Revenue CAGR 1y", "Revenue CAGR 5y", "Revenue growth stability (σ)", "Current ratio"):
        assert na(lbl, "reit", "fibras", "FUNO", "fibra_uno") == "na_template"
    for lbl in ("P/FFO", "ROE", "Net debt / EBITDA"):
        assert na(lbl, "reit", "fibras", "FUNO", "fibra_uno") == "applicable"


def test_completeness_over_the_whole_grid():
    """Every (column, template, sector) resolves to a valid state — no KeyError, total coverage."""
    for sector, (template, members) in UNIVERSE.items():
        clave, name = members[0]
        slug = name.lower().replace(" ", "_")
        applic = row_applicability(template, sector, clave, slug, has_5y=True)
        assert set(applic) == set(COLKEY_BY_LABEL.values())
        for st in applic.values():
            assert st in _STATES


def test_every_column_applicable_somewhere(monkeypatch):
    """No ACCIDENTAL dead-weight column: each metric is applicable for at least one sector (with 5y
    history), except the two the analyst-convention curation deliberately trims from every sector."""
    # This is a pure template/sector contract. Whether the first representative
    # happens to have a local filing corpus must not change the answer.
    monkeypatch.setattr(ap, "is_absent", lambda clave, slug: False)
    curated_out = {"Revenue growth stability (σ)", "Net income CAGR 1y"}
    ever_applicable = set()
    for sector, (template, members) in UNIVERSE.items():
        clave, name = members[0]
        slug = slugify(name)
        for label in _LABELS:
            if column_status(label, template=template, sector=sector, clave=clave,
                             slug=slug, has_5y=True) == "applicable":
                ever_applicable.add(label)
    missing = set(_LABELS) - ever_applicable - curated_out
    assert not missing, missing


def test_bank_structural_na():
    """Banks: no EBITDA / working-capital / FCF / ROIC constructs → na_template."""
    for label in ["EV/EBITDA", "ROIC", "EBITDA margin", "EBITDA YoY (latest)",
                  "Net debt / EBITDA", "Current ratio", "FCF yield"]:
        assert column_status(label, template="financials", sector="banks",
                             clave="GFNORTE", slug="gfnorte", has_5y=True) == "na_template"


def test_cet1_applicability():
    cet1 = "CET1 ratio"
    # CNBV-mapped bank → applicable
    assert column_status(cet1, template="financials", sector="banks",
                         clave="GFNORTE", slug="gfnorte", has_5y=True) == "applicable"
    # unmapped bank → no free capital source → na_source
    assert column_status(cet1, template="financials", sector="banks",
                         clave="ALTERNA", slug="alterna", has_5y=True) == "na_source"
    # brokers / insurers / industrials / reits → CET1 is not a thing → na_template
    for template, sector, clave, slug in [
        ("financials", "brokers_exchange", "GBM", "gbm"),
        ("financials", "insurers_afores", "Q", "qualitas"),
        ("industrial", "retail_selfservice", "WALMEX", "walmex"),
        ("reit", "fibras", "FUNO", "fibra_uno"),
    ]:
        assert column_status(cet1, template=template, sector=sector,
                             clave=clave, slug=slug, has_5y=True) == "na_template"


def test_absent_company_only_yahoo_columns(monkeypatch):
    """A no-XBRL (ABSENT) name can source only the 6 Yahoo key-stat metrics; the rest are na_source.

    ``is_absent`` is data-derived (declared ABSENT minus cached-XBRL slugs) and would flip if the
    fixture company ever gets its filings primed — so force the genuinely-absent case here to keep
    the CONTRACT test filesystem-independent."""
    monkeypatch.setattr(ap, "is_absent", lambda clave, slug: True)
    applic = row_applicability("industrial", "misc_industrial", "ALFA", "alfa", has_5y=True)
    yahoo = {"P/E (LTM)", "EV/EBITDA", "P/BV", "ROE", "Net margin", "Dividend yield"}
    # industrial → structurally N/A regardless of XBRL availability (checked before the no-XBRL
    # fallback, same as CET1): P/TBV (financials-only tangible-book concept), P/FFO (REIT-only
    # IAS 40 fair-value mechanism).
    template_na = {"CET1 ratio", "P/TBV", "P/FFO",
                   # bank + REIT sector-specific metrics are na_template for an operating company
                   "Net interest margin (NIM)", "Efficiency ratio", "Cost of risk",
                   "Occupancy", "NOI margin",
                   # niche growth columns trimmed from the standard industrial comp set
                   "Revenue growth stability (σ)", "Net income CAGR 1y"}
    for label in _LABELS:
        ck = COLKEY_BY_LABEL[label]
        if label in template_na:
            assert applic[ck] == "na_template"
        elif label in yahoo:
            assert applic[ck] == "applicable"
        else:
            assert applic[ck] == "na_source"


def test_cached_xbrl_company_is_never_absent(monkeypatch):
    """A declared-ABSENT clave that HAS cached raw XBRL (e.g. FEMSA) is treated as a normal XBRL
    company — its non-Yahoo cells are applicable, not na_source. Guards the stale-label auto-fix."""
    import scripts.gen_universe as gu
    assert "FEMSA" in ABSENT                                   # still declared absent...
    monkeypatch.setattr(gu, "has_cached_xbrl", lambda slug: slug == "femsa")
    assert gu.is_absent("FEMSA", "femsa") is False            # ...but data-derived says otherwise
    assert gu.is_absent("ALFA", "alfa") is True               # no cached XBRL → still absent
    # ROIC is not a Yahoo-fillable metric; for a real absent name it's na_source, but FEMSA (cached
    # XBRL) must resolve it as applicable.
    st = column_status("ROIC", template="industrial", sector="beverages",
                       clave="FEMSA", slug="femsa", has_5y=True, has_2y=True)
    assert st == "applicable"


def test_residual_na_override(monkeypatch):
    """A residual-N/A entry reclassifies an otherwise-applicable cell to na_source (the reduction lever)."""
    monkeypatch.setattr(ap, "_RESIDUAL_NA", {"walmex": {"ROE": "source-gap"}})
    assert column_status("ROE", template="industrial", sector="retail_selfservice",
                         clave="WALMEX", slug="walmex", has_5y=True) == "na_source"
    # a different company is unaffected
    assert column_status("ROE", template="industrial", sector="retail_selfservice",
                         clave="CHDRAUI", slug="chedraui", has_5y=True) == "applicable"


def test_residual_na_does_not_override_structural(monkeypatch):
    """Structural na_template still wins over a residual entry (never re-labels a real N/A)."""
    monkeypatch.setattr(ap, "_RESIDUAL_NA", {"gfnorte": {"EV/EBITDA": "source-gap"}})
    assert column_status("EV/EBITDA", template="financials", sector="banks",
                         clave="GFNORTE", slug="gfnorte", has_5y=True) == "na_template"


def test_short_history_marks_cagr_na_source(monkeypatch):
    """<5 annual filings → the 5y CAGR/σ columns become na_source (not a GAP); the 1y column
    needs only ≥2 filings, so it stays applicable here."""
    monkeypatch.setattr(ap, "_annual_periods", lambda slug: 2)
    applic = row_applicability("industrial", "retail_selfservice", "WALMEX", "walmex")
    for label in ["Revenue CAGR 5y", "Net income CAGR 5y"]:
        assert applic[COLKEY_BY_LABEL[label]] == "na_source"
    # Rev σ + NI CAGR 1y are now na_template for industrials (convention trim), not history-gated.
    assert applic[COLKEY_BY_LABEL["Revenue growth stability (σ)"]] == "na_template"
    assert applic[COLKEY_BY_LABEL["Net income CAGR 1y"]] == "na_template"
    # Rev CAGR 1y needs only ≥2 annual filings → applicable at 2; a non-history column too.
    assert applic[COLKEY_BY_LABEL["Revenue CAGR 1y"]] == "applicable"
    assert applic[COLKEY_BY_LABEL["ROE"]] == "applicable"


def test_one_year_cagr_needs_two_filings(monkeypatch):
    """<2 annual filings → even the 1-year revenue-YoY column is na_source."""
    monkeypatch.setattr(ap, "_annual_periods", lambda slug: 1)
    applic = row_applicability("industrial", "retail_selfservice", "WALMEX", "walmex")
    assert applic[COLKEY_BY_LABEL["Revenue CAGR 1y"]] == "na_source"
