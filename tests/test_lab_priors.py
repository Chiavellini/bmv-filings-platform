"""LAB prior-column plumbing → apply_restated_priors (IAS-29 re-expressed GT).

GT for several LAB cells is the re-expressed series printed only as the NEXT
year's prior column; restated replacement needs lab.py to keep those priors.
Fixture lines are lifted verbatim from the corpus (period noted per case).
"""

from src.extract.lab import (
    _extract_ebitda,
    _extract_legacy_product_total,
    _extract_product_revenue,
    extract_lab_release,
)
from src.extract.extract_metrics import MetricRow, apply_config
from src.extract.tiered_extract import apply_restated_priors
from src.model.financial_model import METRICS


def _defs():
    cfg = {
        "custom_metrics": [
            {"key": "revenue_otc", "label": "OTC", "label_es": "OTC",
             "section": "income", "unit": "currency", "patterns": []},
            {"key": "revenue_personal_care", "label": "PC", "label_es": "Cuidado Personal",
             "section": "income", "unit": "currency", "patterns": []},
        ]
    }
    return apply_config(METRICS, cfg)


# ── grouped product Total rows (2018+ era): OTC cur/prior Δ% PC cur/prior Δ% … ──

def test_grouped_total_row_carries_priors_2020_4t():
    # data/reports/lab/2020-4T.md:451 — priors are GT 4Q19 OTC/PC (1,599.3/1,620.9)
    line = "Total 1,710.2 1,599.3 6.9% 1,757.7 1,620.9 8.4% 3,467.9 3,220.3 7.7%"
    ctx = ["Medicina de Libre Venta (OTC) Cuidado Personal Total", line]
    got = _extract_legacy_product_total(ctx, "4Q20")
    assert got is not None
    (otc_cur, otc_prior), (pc_cur, pc_prior) = got
    assert (otc_cur, otc_prior) == (1710.2, 1599.3)
    assert (pc_cur, pc_prior) == (1757.7, 1620.9)


def test_grouped_total_row_column_order_flip_2020_2t():
    # data/reports/lab/2020-2T.md:305 — columns are (prior, current); the total
    # growth column proves it, and the PRIOR then holds GT 2Q19 (1,479.0/1,798.5).
    line = "Total 1,479.0 1,955.3 32.2% 1,798.5 1,681.0 (6.5%) 3,277.5 3,636.3 10.9%"
    ctx = ["Medicina de Libre Venta (OTC) Cuidado Personal Total", line]
    (otc_cur, otc_prior), (pc_cur, pc_prior) = _extract_legacy_product_total(ctx, "2Q20")
    assert (otc_cur, otc_prior) == (1955.3, 1479.0)
    assert (pc_cur, pc_prior) == (1681.0, 1798.5)


# ── value+margin rows: EBITDA cur margin% prior margin% Δ% ───────────────────

def test_ebitda_value_margin_row_prior():
    # data/reports/lab/2021-3T.md:29
    lines = ["EBITDA( ) 819.0 20.6%  769.4   22.4%     6.5%"]
    assert _extract_ebitda(lines, "3Q21") == (819.0, 769.4)


def test_ebitda_prose_branch_has_no_prior():
    lines = ["EBITDA Ajustado del segundo trimestre 2020 alcanzó Ps. 754.9 millones"]
    assert _extract_ebitda(lines, "2Q20") == (754.9, None)


# ── end-to-end: release text → MetricRow.prior → restated replacement ────────

def test_release_rows_feed_restated_priors():
    defs = _defs()
    text_2022 = "\n".join([
        "Reporte de Resultados 2T-2022",
        "EBITDA( ) 892.0 20.6% 776.4 21.7% 14.9%",
        "Medicina de Libre Venta (OTC) Cuidado Personal Total",
        "Total 2,371.3 1,945.6 21.9% 1,952.3 1,919.8 1.7% 4,323.6 3,865.4 +11.9%",
    ])
    rows_2022 = extract_lab_release(text_2022, defs, "2Q22")
    assert rows_2022["ebitda"].prior == 776.4
    assert rows_2022["revenue_otc"].prior == 1945.6
    assert rows_2022["revenue_personal_care"].prior == 1919.8

    # 2Q21 as-reported values differ from the re-expressed comparatives above
    stale = {
        k: MetricRow(metric=k, label_es=k, current=1.0, prior=None,
                     var_pct=None, unit="currency", source_line="[search] stale")
        for k in ("ebitda", "revenue_otc", "revenue_personal_care")
    }
    by_period = {"2Q21A": stale, "2Q22A": rows_2022}
    cfg = {"restated_prior": {
        "ebitda": ["2Q21A"],
        "revenue_otc": ["2Q21A"],
        "revenue_personal_care": ["2Q21A"],
    }}
    apply_restated_priors(by_period, defs, cfg)
    assert by_period["2Q21A"]["ebitda"].current == 776.4
    assert by_period["2Q21A"]["revenue_otc"].current == 1945.6
    assert by_period["2Q21A"]["revenue_personal_care"].current == 1919.8
    assert "[restated]" in by_period["2Q21A"]["ebitda"].source_line


# ── text_search skip_metrics: prose noise must not answer table-only keys ────

def test_text_search_skip_metrics_blocks_prose_noise():
    from src.extract.text_search import extract_from_text_search
    defs = _defs()
    # data/reports/lab/2019-2T.md:309 — facility blurb that polluted 2Q19 PC
    text = "Más de 80,000 m2 de (Equipo de Cuidado Personal pendiente de instalación) Área de Carga"
    cfg = {"text_search": {"skip_metrics": ["revenue_otc", "revenue_personal_care"]}}
    found = extract_from_text_search(text, defs, cfg, period="2Q19")
    assert "revenue_personal_care" not in found


def test_recent_product_rows_priors_summed_only_when_complete():
    lines = [
        "Medicamentos de libre venta 1,200.0 1,100.0 2,400.0 2,300.0",
        "Cuidado Personal 900.0 850.0 1,800.0 1,700.0",
    ]
    got = _extract_product_revenue(lines, "1Q22")
    assert got["revenue_otc"] == (1200.0, 1100.0)
    assert got["revenue_personal_care"] == (900.0, 850.0)
