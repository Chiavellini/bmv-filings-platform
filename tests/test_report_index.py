from __future__ import annotations

import importlib
from pathlib import Path


def _write_pdf(path: Path) -> None:
    path.write_bytes(b"%PDF-1.4\n%test\n")


def _write_md(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_extract_pdf_links_matches_visible_release_label():
    from src.download.downloader import _extract_pdf_links

    html = """
    <html><body>
      <a href="/docs/Walmex_1Q26.pdf">Release</a>
      <a href="/docs/Walmex_1Q26_Webcast.pdf">Webcast</a>
      <a href="/docs/Walmex_1Q26_Transcript.pdf">Transcript</a>
      <a href="/docs/Walmex_1Q26_Infographics.pdf">Infographics</a>
    </body></html>
    """

    links = _extract_pdf_links(
        html,
        "https://www.walmex.mx/en/financial-information/quarterly.html",
        r"(?:reporte|report|trimest|quarter|result|earning).*\.pdf",
    )

    assert links == ["https://www.walmex.mx/docs/Walmex_1Q26.pdf"]


def test_infer_period_label_handles_walmex_encoded_space_before_quarter():
    from src.shared.report_index import infer_period_label

    assert infer_period_label("Earnings_20release_204Q16") == "2016-4T"


def test_infer_period_label_handles_lacomer_bmv_filenames():
    from src.shared.report_index import infer_period_label

    assert infer_period_label("BMV_1T26") == "2026-1T"
    assert infer_period_label("1T24bmv") == "2024-1T"
    assert infer_period_label("4_Trimestre_2023") == "2023-4T"
    assert infer_period_label("La-Comer-2do-Trimestre-2022") == "2022-2T"
    assert infer_period_label("La-comer-3er-Trimestre-2018-82") == "2018-3T"


def test_infer_period_label_handles_herdez_and_soriana_archive_names():
    from src.shared.report_index import infer_period_label

    assert infer_period_label("FIRST_QUARTER_2026_Grupo_Herdez_edffa6b129") == "2026-1T"
    assert infer_period_label("FOURTH_QUARTER_AND_YEAR_2025_RESULTS_GRUPO_HERDEZ") == "2025-4T"
    assert infer_period_label("1_TD_2026_GH_020426_78df86095a") == "2026-1T"
    assert infer_period_label("3Q23InfoDir_inglesV3_VF") == "2023-3T"


def test_infer_period_label_handles_liverpool_xbrl_pdf_names():
    from src.shared.report_index import infer_period_label

    assert infer_period_label("2TXBRL2026") == "2026-2T"
    assert infer_period_label("1TXBRL2025") == "2025-1T"


def test_infer_period_label_handles_orbia_leading_quarter_names():
    from src.shared.report_index import infer_period_label

    assert infer_period_label("orbia-q2-2026-earnings-release_vf1-1") == "2026-2T"
    assert infer_period_label("company_T3_2025_results") == "2025-3T"


def test_infer_period_label_recognizes_annual_reports():
    from src.shared.report_index import infer_period_label

    assert infer_period_label("Walmex_Integrated_Annual_Report_2024") == "2024-FY"
    assert infer_period_label("informe_anual_2023_bimbo") == "2023-FY"
    assert infer_period_label("Grupo-Herdez-Reporte-Anual-2022") == "2022-FY"
    assert infer_period_label("femsa-20-f-2024") == "2024-FY"
    assert infer_period_label("FINAMEX_2021-FY_facts") == "2021-FY"
    # No annual signal or no year → not annual (stays None, dropped as before).
    assert infer_period_label("some_random_memo_2024") is None
    assert infer_period_label("annual_report_no_year") is None


def test_annual_does_not_override_quarterly():
    from src.shared.report_index import infer_period_label

    # A quarter token always wins, even when "annual"/"year" appears in the name.
    assert infer_period_label("FOURTH_QUARTER_AND_YEAR_2025_RESULTS") == "2025-4T"
    assert infer_period_label("Walmex_4Q24_annual_summary") == "2024-4T"


def test_period_sort_key_orders_annual_after_q4():
    from src.shared.report_index import period_sort_key

    ordered = sorted(["2024-FY", "2024-1T", "2024-4T", "2025-1T"], key=period_sort_key)
    assert ordered == ["2024-1T", "2024-4T", "2024-FY", "2025-1T"]


def test_index_report_files_keeps_annual_alongside_quarters(tmp_path):
    from src.shared.report_index import index_report_files, period_sort_key

    _write_pdf(tmp_path / "Walmex_1Q24_Release.pdf")
    _write_pdf(tmp_path / "Walmex_Integrated_Annual_Report_2024.pdf")

    indexed = index_report_files(tmp_path.iterdir())
    assert sorted(indexed.keys(), key=period_sort_key) == ["2024-1T", "2024-FY"]
    assert indexed["2024-FY"].selected_path.name == "Walmex_Integrated_Annual_Report_2024.pdf"


def test_report_index_normalizes_walmex_periods_and_prefers_release(tmp_path):
    from src.shared.report_index import index_report_files, period_sort_key

    _write_md(tmp_path / "Walmex_1Q26_Release.md", "release q1")
    _write_pdf(tmp_path / "Walmex_1Q26_Release.pdf")
    _write_md(tmp_path / "Walmex_1Q26_Webcast.md", "webcast q1")
    _write_md(tmp_path / "Walmex_4Q25_Infographics.md", "infographic q4")
    _write_md(tmp_path / "Walmex_Earnings_Release_4Q25.md", "release q4")
    _write_pdf(tmp_path / "Walmex_4Q25_Results.pdf")

    indexed = index_report_files(tmp_path.iterdir())

    assert sorted(indexed.keys(), key=period_sort_key) == ["2025-4T", "2026-1T"]
    assert indexed["2026-1T"].selected_path.name == "Walmex_1Q26_Release.md"
    assert indexed["2025-4T"].selected_path.name == "Walmex_Earnings_Release_4Q25.md"
    assert indexed["2026-1T"].has_pdf is True
    assert indexed["2026-1T"].has_md is True


def test_pipeline_directory_uses_normalized_period_labels(tmp_path):
    from src.extract.pipeline import _from_directory

    _write_md(tmp_path / "Walmex_1Q26_Release.md", "release q1")
    _write_md(tmp_path / "Walmex_1Q26_Webcast.md", "webcast q1")
    _write_md(tmp_path / "Walmex_Earnings_Release_4Q25.md", "release q4")
    _write_md(tmp_path / "Walmex_4Q25_Results_Postcard.md", "postcard q4")

    docs = _from_directory(tmp_path)

    # _from_directory now returns PeriodSource objects (text + optional facts/pdf).
    assert list(docs.keys()) == ["2025-4T", "2026-1T"]
    assert docs["2026-1T"].text == "release q1"
    assert docs["2025-4T"].text == "release q4"


def test_index_report_files_groups_a_company_directory(tmp_path):
    from src.shared.report_index import index_report_files, period_sort_key

    _write_md(tmp_path / "Walmex_1Q26_Release.md", "release q1")
    _write_pdf(tmp_path / "Walmex_1Q26_Release.pdf")
    _write_md(tmp_path / "Walmex_Earnings_Release_4Q25.md", "release q4")
    _write_pdf(tmp_path / "Walmex_4Q25_Infographics.pdf")

    indexed = index_report_files(tmp_path.iterdir())
    periods = sorted(indexed.keys(), key=period_sort_key)

    assert periods == ["2025-4T", "2026-1T"]
    assert indexed["2026-1T"].has_pdf is True
    assert indexed["2026-1T"].has_md is True
