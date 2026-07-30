"""Master matrix HTML generator — the self-generating, header-stripped page that replaces the old
hand-authored artifact. Offline (no network, no rebuild). Guards the two removals (no masthead / no
"This session" card), the blank-not-fabricated contract, and structural parity with the CSV mirror."""
from scripts.build_master import (
    COLUMNS,
    _band_groups,
    _heat_css,
    write_html_master,
)
from src.coverage.applicability import row_applicability


def _row(name, raw_sector, template, clave, values):
    slug = name.lower().replace(" ", "_")
    # Attach the three-state applicability map the renderer now branches on. has_5y=True keeps the
    # fixture off the filesystem. raw_sector is the UNIVERSE key (banks/industrial), not the pretty one.
    applic = row_applicability(template, raw_sector, clave, slug, has_5y=True)
    pretty = raw_sector.replace("_", " ").title()
    return {"slug": slug, "name": name, "clave": clave, "template": template,
            "sector": pretty, "values": values, "applic": applic}


def _sample_rows():
    # two banks + one industrial + one REIT; a mix of populated and blank (None) cells. Sector-specific
    # cells carry a value so the collapse rule keeps their columns (a column with no value anywhere is
    # dropped — that is the point).
    pe = ("snapshot_multiples", "P/E (LTM)")
    roe = ("financial_analysis", "ROE")
    evb = ("snapshot_multiples", "EV/EBITDA")
    cet1 = ("bank_returns", "CET1 ratio")
    nim = ("bank_returns", "Net interest margin (NIM)")
    pffo = ("reit_snapshot", "P/FFO")
    occ = ("reit_metrics", "Occupancy")
    return [
        _row("Alpha Bank", "banks", "financials", "ALFA",
             {pe: 8.0, roe: 20.0, cet1: 12.5, nim: 5.0}),     # evb collapsed (N/A for a bank)
        _row("Beta Bank", "banks", "financials", "BETA", {pe: 12.0, roe: 10.0, cet1: 14.0}),
        _row("Zeta Corp", "industrial", "industrial", "ZETA", {pe: 15.0, roe: 5.0, evb: 9.0}),
        _row("Gamma Fibra", "fibras", "reit", "GAMA", {pffo: 12.0, occ: 95.0, roe: 8.0}),
    ]


def test_no_masthead_or_session_card(tmp_path):
    out = tmp_path / "m.html"
    write_html_master(_sample_rows(), out)
    doc = out.read_text(encoding="utf-8")
    for forbidden in ("masthead", "eyebrow", 'class="changes"', "This session",
                      "Master Coverage Matrix", "One row per company", 'class="stats"'):
        assert forbidden not in doc, f"removed chrome leaked: {forbidden}"


def test_title_legend_footer_present(tmp_path):
    out = tmp_path / "m.html"
    write_html_master(_sample_rows(), out)
    doc = out.read_text(encoding="utf-8")
    assert "<title>Soft Coverage — Master Matrix</title>" in doc
    assert 'class="legend"' in doc
    assert "<footer>" in doc
    # body-content only — no full-document wrappers (Artifact host injects those)
    assert "<!doctype" not in doc.lower() and "<html" not in doc.lower() and "<body" not in doc.lower()


def test_blank_cells_are_honest_not_fabricated(tmp_path):
    out = tmp_path / "m.html"
    write_html_master(_sample_rows(), out)
    doc = out.read_text(encoding="utf-8")
    # EV/EBITDA is structurally N/A for a bank → COLLAPSED (no such column in the Banks section), never
    # rendered as an empty cell for a bank. The Banks section header must NOT carry an EV/EBITDA column.
    banks_sec = doc.split('data-template="financials"', 1)[1].split("</section>", 1)[0]
    assert 'data-col="EV/EBITDA"' not in banks_sec        # collapsed for banks
    # an applicable-but-unfilled cell (e.g. a bank's P/BV / Div yield) → a flagged GAP, never faked.
    assert '<td class="v gap" data-col=' in doc
    # a populated value carries a data-val hook, an inline heat background and the formatted number
    assert 'class="v" data-col="P/E" data-val="8" style="background:rgb(' in doc
    assert "8.00×" in doc and "20.0%" in doc


def test_reduced_cell_with_value_still_counts_filled():
    """Reduction never hides a real value: a na_source cell WITH a value renders/counts as filled."""
    from scripts.build_master import cell_state, coverage_counts
    from src.coverage.columns import COLKEYS
    ck = ("financial_analysis", "ROE")
    row = {"values": {ck: 15.0}, "applic": {ck: "na_source"}}
    assert cell_state(row, ck) == "filled"
    blank = {"values": {ck: None}, "applic": {ck: "na_source"}}
    assert cell_state(blank, ck) == "na"
    # coverage: mark every column na_source; only ROE has a value → counted filled, the rest excluded.
    full = {"values": {ck: 15.0}, "applic": {c: "na_source" for c in COLKEYS}}
    nf, na, ng = coverage_counts([full])
    assert nf == 1 and na == 1 and ng == 0


def test_na_excluded_from_coverage_denominator(tmp_path):
    from scripts.build_master import coverage_counts
    rows = _sample_rows()
    n_filled, n_applicable, n_gap = coverage_counts(rows)
    # Bank structural-N/A columns (EV/EBITDA, ROIC, EBITDA mgn, ...) must NOT count as applicable.
    # Two banks × 8 na_template each are excluded; only their applicable cells form the denominator.
    assert n_applicable < len(rows) * len(COLUMNS)
    # filled + gap == applicable (N/A is neither)
    assert n_filled + n_gap == n_applicable
    # every populated cell counts as filled: Alpha 4 (pe/roe/cet1/nim) + Beta 3 (pe/roe/cet1)
    # + Zeta 3 (pe/roe/evb) + Gamma 3 (pffo/occ/roe) = 13
    assert n_filled == 13


def test_three_template_sections_with_own_columns(tmp_path):
    out = tmp_path / "m.html"
    rows = _sample_rows()
    write_html_master(rows, out)
    doc = out.read_text(encoding="utf-8")
    # one <tr class="co" ...> per company across all sections
    assert doc.count('<tr class="co"') == len(rows)
    # a Banks section and an Industrial section each exist as their own <section>
    assert '<section class="tsection" data-template="financials">' in doc
    assert '<section class="tsection" data-template="industrial">' in doc
    banks = doc.split('data-template="financials"', 1)[1].split("</section>", 1)[0]
    indus = doc.split('data-template="industrial"', 1)[1].split("</section>", 1)[0]
    # Banks collapse EV/EBITDA/FCF and SHOW their own metrics; Industrial keeps EV/EBITDA, no CET1.
    assert 'data-col="EV/EBITDA"' not in banks and 'data-col="FCF yield"' not in banks
    assert 'data-col="CET1"' in banks and 'data-col="NIM"' in banks
    assert 'data-col="EV/EBITDA"' in indus and 'data-col="CET1"' not in indus
    # each data row has exactly (its section's kept-column count) value cells — the metric sub-headers
    # (each `<th class="sub" data-col=...>`) count the kept columns; the two rail headers don't match.
    first_bank_row = banks.split('<tr class="co"', 1)[1].split("</tr>", 1)[0]
    n_bank_cols = banks.split("<tbody>", 1)[0].count('<th class="sub" data-col=')
    assert n_bank_cols > 0 and first_bank_row.count('class="v') == n_bank_cols


def test_collapsed_columns_and_section_bands(tmp_path):
    """Non-applicable columns are omitted (collapsed), not dimmed; the old dim-strip/moving-header are
    gone; the REIT-only / bank-only bands appear only in their section."""
    out = tmp_path / "m.html"
    write_html_master(_sample_rows(), out)
    doc = out.read_text(encoding="utf-8")
    # the removed dim overlay must be fully gone
    assert 'class="sechdr' not in doc and "data-dim=" not in doc
    # sticky metric headers still carry a data-col hook
    assert '<th class="sub" data-col="P/E">' in doc
    # bank-only band label appears only inside the financials section
    assert "Risk (banks)" in doc
    indus = doc.split('data-template="industrial"', 1)[1].split("</section>", 1)[0]
    assert "Risk (banks)" not in indus


def test_filter_panel_and_hooks_present(tmp_path):
    """The client-side filter panel is industry/sector (+reset); sections carry data-template and rows
    carry data-sector so the JS can toggle whole sections and rows."""
    out = tmp_path / "m.html"
    write_html_master(_sample_rows(), out)
    doc = out.read_text(encoding="utf-8")
    for ctrl in ('id="f-template"', 'id="f-sector"', 'id="f-reset"', 'id="f-count"'):
        assert ctrl in doc, f"missing filter control: {ctrl}"
    # the metric-threshold controls of the old flat table are gone
    assert 'id="f-metric"' not in doc and 'id="f-min"' not in doc
    # industry dropdown offers the templates present; sections + rows carry the toggle hooks
    assert '<option value="financials">Banks &amp; Financials</option>' in doc
    assert 'data-template="financials"' in doc and 'data-sector="Banks"' in doc
    assert 'data-val="8"' in doc
    # the filter script is embedded (pure DOM, no external src)
    assert "getElementById('f-reset')" in doc and "<script src" not in doc


def test_rev_cagr_1y_derives_from_latest_fy_yoy():
    """Rev CAGR 1y is recovered from the latest FY revenue-YoY row when the explicit cell is absent
    (a CSV predating the column), and the explicit cell wins when present."""
    from scripts.build_master import pick_col
    # no explicit 'Revenue CAGR 1y' row → derive from the max-year FY YoY (2025 → 4.46)
    stale = {"growth": {"FY2023: Revenue YoY": 2.77, "FY2024: Revenue YoY": 11.03,
                        "FY2025: Revenue YoY": 4.46, "Revenue CAGR 5y": 6.03,
                        "FY2024: Net income YoY": 8.0, "FY2025: Net income YoY": -3.5}}
    assert pick_col(stale, "growth", "Revenue CAGR 1y") == 4.46
    # NI CAGR 1y derives from the max-year FY Net income YoY (2025 → -3.5, a valid negative)
    assert pick_col(stale, "growth", "Net income CAGR 1y") == -3.5
    # explicit cell present → used verbatim (no derivation)
    fresh = dict(stale); fresh["growth"] = dict(stale["growth"], **{"Revenue CAGR 1y": 7.47})
    assert pick_col(fresh, "growth", "Revenue CAGR 1y") == 7.47
    # no FY YoY rows at all → stays None (thin corpus, honest blank)
    assert pick_col({"growth": {"Revenue CAGR 5y": 6.0}}, "growth", "Revenue CAGR 1y") is None


def test_heat_direction_and_bounds():
    # good='low' → the minimum value is green, the max is red; median is yellow-ish.
    assert _heat_css(1.0, 1.0, 5.0, 10.0, "low").startswith("rgb(99, 190, 123")   # green
    assert _heat_css(10.0, 1.0, 5.0, 10.0, "low").startswith("rgb(248, 105, 107")  # red
    # good='high' inverts: min red, max green
    assert _heat_css(1.0, 1.0, 5.0, 10.0, "high").startswith("rgb(248, 105, 107")
    assert _heat_css(10.0, 1.0, 5.0, 10.0, "high").startswith("rgb(99, 190, 123")
    # degenerate column (all equal) → yellow, never a crash
    assert _heat_css(5.0, 5.0, 5.0, 5.0, "high") == "rgb(255, 235, 132)"
