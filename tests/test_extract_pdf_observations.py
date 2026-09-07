"""scripts/extract_pdf_observations.py — one uploaded PDF → CSV/XLSX observations."""
from __future__ import annotations

import csv
import importlib
import json
from pathlib import Path

import pytest

pytest.importorskip("pdfplumber")
openpyxl = pytest.importorskip("openpyxl")

cli = importlib.import_module("scripts.extract_pdf_observations")


def _tiny_pdf(lines: list[str]) -> bytes:
    """Build a valid one-page PDF by hand so tests never depend on the corpus."""

    def escape(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = (
        "BT /F1 12 Tf 14 TL 72 720 Td "
        + " ".join(f"({escape(line)}) Tj T*" for line in lines)
        + " ET"
    )
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content.encode('latin-1'))} >>\nstream\n{content}\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return out


LINES = [
    "Resultados del trimestre",
    "Ventas magicas 1,234 1,100",
    "Ingresos totales 5,000 4,500",
]


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Outputs must live under <ROOT>/outputs; point ROOT at the sandbox so tests
    # never write into the repository's real outputs/ tree.
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    return tmp_path


def _request_dir(sandbox: Path, *, lines=LINES, **overrides) -> Path:
    request_dir = sandbox / "state" / "extractor" / "0123456789abcdef"
    request_dir.mkdir(parents=True)
    (request_dir / "source.pdf").write_bytes(_tiny_pdf(lines))
    request = {
        "upload": request_dir.name,
        "filename": "Reporte_2T25.pdf",
        "company": "",
        "config_slug": "",
        "metrics": ["revenue"],
        "custom_metrics": ["Ventas magicas"],
        "period": "",
        "period_guess": "2025-2T",
        "format": "both",
        "read_tables": False,
    }
    request.update(overrides)
    (request_dir / "request.json").write_text(json.dumps(request), encoding="utf-8")
    return request_dir


def _rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _summary_rows(path: Path) -> list[list]:
    workbook = openpyxl.load_workbook(path)
    assert workbook.sheetnames == ["Observaciones", "Resumen"]
    return [list(row) for row in workbook["Resumen"].iter_rows(values_only=True)]


def test_custom_metric_entries_are_safe_regex_definitions() -> None:
    existing = {"revenue"}
    entry = cli.custom_metric_entry("Ventas mismas tiendas (%)", existing)
    assert entry["key"] == "custom_ventas_mismas_tiendas"
    assert entry["aliases"] == ["Ventas mismas tiendas (%)"]
    assert entry["section"] == "kpi"
    duplicate = cli.custom_metric_entry("Ventas mismas tiendas (%)", existing)
    assert duplicate["key"] == "custom_ventas_mismas_tiendas_2"
    accented = cli.custom_metric_entry("Número de tiendas", existing)
    assert accented["key"] == "custom_numero_de_tiendas"
    assert "(" in entry["patterns"][0]["regex"]
    assert "\\(" in entry["patterns"][0]["regex"]


def test_catalog_and_custom_metrics_are_extracted(sandbox: Path) -> None:
    request_dir = _request_dir(sandbox)
    output_dir = sandbox / "outputs" / "extractor" / request_dir.name
    summary = cli.run_request(request_dir, output_dir)

    assert summary["period"] == "2025-2T"
    assert summary["found"] == 2
    assert summary["requested"] == 2
    assert summary["missing"] == []
    csv_path = output_dir / "reporte_2t25_observaciones.csv"
    xlsx_path = output_dir / "reporte_2t25_observaciones.xlsx"
    assert [Path(item) for item in summary["outputs"]] == [csv_path, xlsx_path]

    rows = {row["metric"]: row for row in _rows(csv_path)}
    assert set(rows) == {"custom_ventas_magicas", "revenue"}
    assert float(rows["custom_ventas_magicas"]["current"]) == 1234.0
    assert float(rows["custom_ventas_magicas"]["prior"]) == 1100.0
    assert float(rows["revenue"]["current"]) == 5000.0
    assert rows["revenue"]["period"] == "2025-2T"
    assert rows["revenue"]["label"] == "Ingresos Totales"

    resumen = _summary_rows(xlsx_path)
    assert ["Periodo", "2025-2T"] in [row[:2] for row in resumen]
    states = {row[0]: row[3] for row in resumen if row and row[0] in {"revenue", "custom_ventas_magicas"}}
    assert states == {"revenue": "Encontrada", "custom_ventas_magicas": "Encontrada"}
    origins = {row[0]: row[2] for row in resumen if row and row[0] in states}
    assert origins == {"revenue": "catálogo", "custom_ventas_magicas": "adicional"}

    config = (request_dir / "extractor.yaml").read_text(encoding="utf-8")
    assert "custom_ventas_magicas" in config
    assert "disabled: true" in config
    assert (request_dir / "work" / "2025-2T.pdf").is_file()

    written = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert written["found"] == 2 and written["requested"] == 2
    assert written["periods"] == ["2025-2T"]
    assert written["sources"] == ["Reporte_2T25.pdf"]
    assert written["missing"] == []


def test_period_override_and_filename_inference(sandbox: Path) -> None:
    request_dir = _request_dir(sandbox, period="2024-FY")
    output_dir = sandbox / "outputs" / "extractor" / "override"
    assert cli.run_request(request_dir, output_dir)["period"] == "2024-FY"
    assert {row["period"] for row in _rows(output_dir / "reporte_2t25_observaciones.csv")} == {"2024-FY"}

    inferred = _request_dir(
        sandbox / "b", filename="foo_3T24.pdf", period="", period_guess=None
    )
    assert cli.run_request(inferred, sandbox / "outputs" / "extractor" / "b")["period"] == "2024-3T"

    unknown = _request_dir(sandbox / "c", filename="informe.pdf", period="", period_guess=None)
    assert cli.run_request(unknown, sandbox / "outputs" / "extractor" / "c")["period"] == "informe"

    bad = _request_dir(sandbox / "d", period="Q2 2025")
    with pytest.raises(cli.ExtractionError, match="Periodo"):
        cli.run_request(bad, sandbox / "outputs" / "extractor" / "d")


def test_output_format_selection(sandbox: Path) -> None:
    csv_only = _request_dir(sandbox, format="csv")
    csv_dir = sandbox / "outputs" / "extractor" / "csv"
    cli.run_request(csv_only, csv_dir)
    assert sorted(path.suffix for path in csv_dir.iterdir()) == [".csv", ".json"]

    xlsx_only = _request_dir(sandbox / "x", format="xlsx")
    xlsx_dir = sandbox / "outputs" / "extractor" / "xlsx"
    cli.run_request(xlsx_only, xlsx_dir)
    assert sorted(path.suffix for path in xlsx_dir.iterdir()) == [".json", ".xlsx"]


def _upload_dir(root: Path, upload_id: str, filename: str, lines: list[str], period_guess) -> None:
    folder = root / upload_id
    folder.mkdir(parents=True)
    (folder / "source.pdf").write_bytes(_tiny_pdf(lines))
    (folder / "upload.json").write_text(
        json.dumps({"upload": upload_id, "filename": filename, "size": 1, "period_guess": period_guess}),
        encoding="utf-8",
    )


def test_several_pdfs_yield_one_row_per_period(sandbox: Path) -> None:
    root = sandbox / "state" / "extractor"
    _upload_dir(root, "aaaaaaaaaaaaaaa1", "Reporte_1T25.pdf",
                ["Ingresos totales 4,000 3,500", "Ventas magicas 100 90"], "2025-1T")
    _upload_dir(root, "aaaaaaaaaaaaaaa2", "Reporte_2T25.pdf",
                ["Ingresos totales 5,000 4,500"], "2025-2T")
    _upload_dir(root, "aaaaaaaaaaaaaaa3", "informe.pdf", ["Ingresos totales 6,000 5,000"], None)
    request_dir = root / "bbbbbbbbbbbbbbb1"
    request_dir.mkdir()
    request = {
        "sources": [
            {"upload": "aaaaaaaaaaaaaaa1", "filename": "Reporte_1T25.pdf", "period": "2025-1T"},
            {"upload": "aaaaaaaaaaaaaaa2", "filename": "Reporte_2T25.pdf", "period": "2025-2T"},
            {"upload": "aaaaaaaaaaaaaaa3", "filename": "informe.pdf", "period": "2025-FY"},
        ],
        "company": "", "config_slug": "", "metrics": ["revenue"],
        "custom_metrics": ["Ventas magicas"], "format": "both", "read_tables": False,
    }
    (request_dir / "request.json").write_text(json.dumps(request), encoding="utf-8")
    output_dir = sandbox / "outputs" / "extractor" / request_dir.name

    summary = cli.run_request(request_dir, output_dir)
    assert summary["periods"] == ["2025-1T", "2025-2T", "2025-FY"]
    assert summary["sources"] == ["Reporte_1T25.pdf", "Reporte_2T25.pdf", "informe.pdf"]
    assert summary["found"] == 2
    assert sorted(path.name for path in (request_dir / "work").glob("*.pdf")) == [
        "2025-1T.pdf", "2025-2T.pdf", "2025-FY.pdf",
    ]
    csv_path = output_dir / "reporte_1t25_y_2_mas_observaciones.csv"
    rows = _rows(csv_path)
    revenue = {row["period"]: float(row["current"]) for row in rows if row["metric"] == "revenue"}
    assert revenue == {"2025-1T": 4000.0, "2025-2T": 5000.0, "2025-FY": 6000.0}
    magic = [row for row in rows if row["metric"] == "custom_ventas_magicas"]
    assert [row["period"] for row in magic] == ["2025-1T"]

    resumen = _summary_rows(output_dir / "reporte_1t25_y_2_mas_observaciones.xlsx")
    assert ["Archivos", "Reporte_1T25.pdf, Reporte_2T25.pdf, informe.pdf"] in [row[:2] for row in resumen]
    assert ["Periodos", "2025-1T, 2025-2T, 2025-FY"] in [row[:2] for row in resumen]
    states = {row[0]: row[3] for row in resumen if row and row[0] in {"revenue", "custom_ventas_magicas"}}
    assert states == {"revenue": "Encontrada (3/3 periodos)", "custom_ventas_magicas": "Encontrada (1/3 periodos)"}
    latest = {row[0]: row[5] for row in resumen if row and row[0] == "revenue"}
    assert latest == {"revenue": "2025-FY"}

    # A missing source PDF or a period collision is refused before extraction.
    (root / "aaaaaaaaaaaaaaa3" / "source.pdf").unlink()
    with pytest.raises(cli.ExtractionError, match="informe.pdf ya no está disponible"):
        cli.run_request(request_dir, output_dir)
    request["sources"] = request["sources"][:2]
    request["sources"][1]["period"] = "2025-1T"
    (request_dir / "request.json").write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(cli.ExtractionError, match="mismo periodo"):
        cli.run_request(request_dir, output_dir)


def test_zero_hits_still_produce_readable_outputs(sandbox: Path, capsys) -> None:
    request_dir = _request_dir(sandbox, metrics=[], custom_metrics=["Nada de nada"])
    output_dir = sandbox / "outputs" / "extractor" / request_dir.name
    assert cli.main([str(request_dir), "--output-dir", str(output_dir)]) == 0
    captured = capsys.readouterr()
    assert "Sin evidencia: custom_nada_de_nada" in captured.out

    csv_path = output_dir / "reporte_2t25_observaciones.csv"
    assert csv_path.read_text(encoding="utf-8").splitlines() == [",".join(cli.LONG_COLUMNS)]
    resumen = _summary_rows(output_dir / "reporte_2t25_observaciones.xlsx")
    assert any(row[0] == "custom_nada_de_nada" and row[3] == "No encontrada" for row in resumen if row)


def test_main_refuses_outputs_outside_the_outputs_tree(sandbox: Path, capsys) -> None:
    request_dir = _request_dir(sandbox)
    assert cli.main([str(request_dir), "--output-dir", str(sandbox / "elsewhere")]) == 1
    assert "outputs/" in capsys.readouterr().err
    assert cli.main([str(sandbox / "missing"), "--output-dir", str(sandbox / "outputs" / "x")]) == 1
