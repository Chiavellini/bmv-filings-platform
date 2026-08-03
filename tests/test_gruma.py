from __future__ import annotations

from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent


GRUMA_APPENDIX_TEXT = """
Informacion por Subsidiaria        Trimestre
GRUMA ESTADOS UNIDOS Volumen de Ventas 375 383 (9) (2)
Harina de maiz, tortilla y otros Ventas Netas 851.1 100.0 879.7 100.0 (29) (3)
Costo de Venta 478.0 56.2 488.2 55.5 (10) (2)
Utilidad Bruta 373.1 43.8 391.5 44.5 (18) (5)
Utilidad de Operacion 130.9 15.4 150.7 17.1 (20) (13)
UAFIRDA 172.3 20.2 193.4 22.0 (21) (11)
GIMSA Volumen de Ventas 514 512 2 0
Ventas Netas 438.7 100.0 443.1 100.0 (4) (1)
Utilidad Bruta 119.3 27.2 121.0 27.3 (2) (1)
Utilidad de Operacion 17.2 3.9 32.0 7.2 (15) (46)
UAFIRDA 40.3 9.2 44.6 10.1 (4) (10)
GRUMA EUROPA Volumen de Ventas 111 105 6 6
Ventas Netas 130.9 100.0 114.8 100.0 16 14
Utilidad Bruta 37.7 28.8 32.9 28.6 5 15
Utilidad de Operacion 9.4 7.2 7.6 6.7 2 23
UAFIRDA 13.7 10.5 11.4 9.9 2 20
GRUMA ASIA Y OCEANIA Volumen de Ventas 27 25 2 7
Ventas Netas 77.8 100.0 66.8 100.0 11 17
Utilidad Bruta 24.5 31.4 20.7 31.0 4 18
Utilidad de Operacion 7.8 10.0 5.7 8.5 2 37
UAFIRDA 11.0 14.2 8.6 12.9 2 28
GRUMA CENTROAMERICA Volumen de Ventas 62 59 3 5
Ventas Netas 102.9 100.0 96.6 100.0 6 7
Utilidad Bruta 43.6 42.4 38.6 39.9 5 13
Utilidad de Operacion 18.4 17.9 14.1 14.6 4 31
UAFIRDA 21.5 20.9 16.2 16.8 5 33
OTRAS SUBSIDIARIAS Y / ELIMINACIONES Volumen de Ventas (21) (23) 2 9
Ventas Netas 23.2 100.0 (52.5) 100.0 76 144
Utilidad Bruta 30.6 131.9 11.4 (21.7) 19 168
Utilidad de Operacion 5.4 23.3 7.0 (13.3) (2) (23)
UAFIRDA 3.0 12.9 1.7 (3.2) 1 76
CONSOLIDADO Volumen de Ventas 1,068 1,062 6 1
Ventas Netas 1,624.7 100.0 1,548.5 100.0 76 5
Utilidad Bruta 628.8 38.7 615.9 39.8 13 2
Utilidad de Operacion 189.2 11.6 217.1 14.0 (28) (13)
UAFIRDA 261.9 16.1 276.0 17.8 (14) (5)
"""


def _gruma_metric_defs():
    from src.model.financial_model import METRICS, apply_config, load_config

    cfg = load_config(ROOT / "configs" / "gruma.yaml")
    return apply_config(METRICS, cfg), cfg


def test_gruma_appendix_extractor_recovers_1q26_values():
    from src.extract.gruma import extract_gruma_appendix

    metric_defs, _ = _gruma_metric_defs()
    out = extract_gruma_appendix(GRUMA_APPENDIX_TEXT, metric_defs)

    assert out["revenue"].current == 1624.7
    assert out["volume"].current == 1068
    assert out["gross_profit"].current == 628.8
    assert out["operating_income"].current == 189.2
    assert out["ebitda"].current == 261.9
    assert out["net_sales_usa"].current == 851.1
    assert out["volume_gimsa"].current == 514
    assert out["gross_profit_other"].current == 30.6
    assert out["operating_income_other"].prior == 7.0
    assert out["ebitda_cam"].var_pct == 33


def test_gruma_tiered_custom_extractor_runs_before_regex():
    from src.extract.tiered_extract import PeriodSource, extract_metrics_tiered

    metric_defs, cfg = _gruma_metric_defs()
    out = extract_metrics_tiered(
        PeriodSource(period="2026-1T", text=GRUMA_APPENDIX_TEXT),
        metric_defs,
        cfg,
    )
    assert out["ebitda"].current == 261.9
    assert "[gruma_table]" in out["ebitda"].source_line


def test_gruma_outline_maps_repeated_volume_rows_and_formulas():
    import yaml
    from src.excel.segments_sheet import build_outline_workbook, parse_outline

    spec = yaml.safe_load((ROOT / "configs" / "gruma_segments.yaml").read_text())
    rows = parse_outline(spec["outline"], spec["sections"], spec["mapping"])
    keys = [row.key for row in rows if row.key]
    assert "volume_usa" in keys
    assert "volume_other" in keys

    df = pd.DataFrame([
        {
            "period": "2025-1T",
            "revenue": 1548.5,
            "volume": 1062,
            "net_sales_usa": 879.7,
            "volume_usa": 383,
            "gross_profit": 615.9,
            "gross_profit_usa": 391.5,
            "gross_profit_gimsa": 121.0,
            "gross_profit_europe": 32.9,
            "gross_profit_ao": 20.7,
            "gross_profit_cam": 38.6,
            "gross_profit_other": 11.4,
        },
        {
            "period": "2026-1T",
            "revenue": 1624.7,
            "volume": 1068,
            "net_sales_usa": 851.1,
            "volume_usa": 375,
            "gross_profit": 628.8,
            "gross_profit_usa": 373.1,
            "gross_profit_gimsa": 119.3,
            "gross_profit_europe": 37.7,
            "gross_profit_ao": 24.5,
            "gross_profit_cam": 43.6,
            "gross_profit_other": 30.6,
        },
    ])
    unit_map = {key: "currency" for key in df.columns if key != "period"}
    unit_map.update({"volume": "volume", "volume_usa": "volume"})
    ws = build_outline_workbook("GRUMAB: GRUMA", rows, df, units="US$mn", unit_map=unit_map).active

    total_sales = _find(ws, "Total sales")
    total_volume = _find(ws, "Volume (thousand tons)")
    avg_price = _find(ws, "Average Price (Per thousand ton)")
    assert ws[f"H{avg_price}"].value == f'=IFERROR(H{total_sales}/H{total_volume},"N/A")'
    assert ws[f"H{avg_price + 1}"].value == f'=IFERROR(H{avg_price}/C{avg_price}-1,"N/A")'

    gross_profit = _find(ws, "Total Gross Profit")
    margin = gross_profit + 2
    assert ws[f"H{margin}"].value == f'=IFERROR(H{gross_profit}/H{total_sales},"N/A")'
    assert ws[f"H{margin + 1}"].value == f'=IFERROR((H{margin}-C{margin})*10000,"N/A")'

    usa_gp = _find_after(ws, "Gruma USA", gross_profit)
    usa_share = usa_gp + 3
    assert ws[f"H{usa_share}"].value == f"=+H{usa_gp}/H${gross_profit}"

    check = _find_after(ws, "Check", usa_gp)
    assert ws[f"H{check}"].value.startswith("=IF(COUNT(")
    assert f"H{gross_profit}-SUM(" in ws[f"H{check}"].value

    fx = _find(ws, "FX Effect")
    assert ws[f"B{fx}"].comment is not None


def test_gruma_aspnet_quarter_filter_crawler_prefers_release_links():
    from src.download.downloader import _crawl_aspnet_quarter_filter_links

    html = _aspnet_html()
    session = _AspnetSession()
    links = _crawl_aspnet_quarter_filter_links(
        session,
        html,
        "https://www.gruma.com/reportes-trimestrales.aspx",
        max_reports=10,
        file_pattern=r"gruma[-_]?e.*\.pdf",
        verify_ssl=True,
        delay_ms=0,
        diag=[],
    )

    assert links == [
        "https://www.gruma.com/media/1q26-gruma-e.pdf",
        "https://www.gruma.com/media/4q25-gruma-e.pdf",
    ]
    assert ("2026", "1") in session.posts
    assert ("2025", "4") in session.posts


def _find(ws, label: str) -> int:
    return _find_after(ws, label, 1)


def _find_after(ws, label: str, start: int) -> int:
    for row in range(start + 1, ws.max_row + 1):
        if ws[f"B{row}"].value == label:
            return row
    raise AssertionError(f"{label!r} not found after row {start}")


def _aspnet_html() -> str:
    return """
    <html><body>
      <form method="post" action="/reportes-trimestrales.aspx">
        <input type="hidden" name="__VIEWSTATE" value="abc" />
        <input type="hidden" name="__VIEWSTATEGENERATOR" value="def" />
        <select name="ctl$ddlAnio">
          <option value="2026">2026</option>
          <option value="2025">2025</option>
        </select>
        <select name="ctl$ddlTrimestre">
          <option value="4">4</option>
          <option value="1">1</option>
        </select>
        <a id="ctl_lbFiltrar" href="javascript:__doPostBack('ctl$lbFiltrar','')">Filtrar</a>
      </form>
    </body></html>
    """


class _Resp:
    def __init__(self, text: str, url: str):
        self.text = text
        self.url = url

    def raise_for_status(self):
        return None


class _AspnetSession:
    def __init__(self):
        self.posts: list[tuple[str, str]] = []

    def post(self, url, data=None, timeout=None, verify=None, headers=None):
        year = data["ctl$ddlAnio"]
        quarter = data["ctl$ddlTrimestre"]
        self.posts.append((year, quarter))
        if (year, quarter) == ("2026", "1"):
            body = """
            <a href="/media/1q26-gruma-e.pdf">Reporte de Resultados</a>
            <a href="/media/reportes_gruma_bmv_1t26_usd.pdf">BMV</a>
            """
        elif (year, quarter) == ("2025", "4"):
            body = """
            <a href="/media/4q25-gruma-e.pdf">Reporte de Resultados</a>
            <a href="/media/reportes_gruma_bmv_4t25_usd.pdf">BMV</a>
            """
        else:
            body = "<p>No report</p>"
        return _Resp(body, url)
