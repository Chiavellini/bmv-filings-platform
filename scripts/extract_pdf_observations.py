#!/usr/bin/env python3
"""Extract analyst-chosen metrics from one uploaded PDF into CSV and/or Excel.

The launchpad bridge materializes a private request directory containing the
uploaded ``source.pdf`` and a ``request.json`` contract.  This script turns that
contract into observations using the shared extraction cascade and writes the
results under ``outputs/extractor/<request>/``.  The PDF never leaves the Mac:
no network access is performed and the LLM tier stays off unless ``--llm`` is
passed explicitly.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import re
import shutil
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src.model.financial_model import _N, load_config  # noqa: E402
from src.shared.report_index import infer_period_label  # noqa: E402


LONG_COLUMNS = [
    "period", "metric", "label", "current", "prior", "var_pct",
    "unit", "confidence", "validated", "flagged", "source",
]
SPANISH_HEADERS = {
    "period": "Periodo",
    "metric": "Métrica",
    "label": "Etiqueta",
    "current": "Actual",
    "prior": "Anterior",
    "var_pct": "Var %",
    "unit": "Unidad",
    "confidence": "Confianza",
    "validated": "Validada",
    "flagged": "Señalada",
    "source": "Fuente",
}
LOW_CONFIDENCE = 0.5
_KEY_RE = re.compile(r"[^a-z0-9]+")
_LABEL_RE = re.compile(r"^20\d{2}-(?:[1-4]T|FY)$")


class ExtractionError(RuntimeError):
    """A request could not be processed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Request → configuration
# ---------------------------------------------------------------------------

def slug_key(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = _KEY_RE.sub("_", ascii_text.lower()).strip("_")
    return slug[:40].strip("_") or "metrica"


def custom_metric_entry(text: str, existing: set[str]) -> dict:
    """Return one ``custom_metrics`` config entry for a free-text metric name."""
    base = f"custom_{slug_key(text)}"
    key = base
    counter = 2
    while key in existing:
        key = f"{base}_{counter}"
        counter += 1
    existing.add(key)
    escaped = re.escape(text).replace(r"\ ", r"\s+")
    regex = (
        r"(?im)^\s*" + escaped + r"[^\d\n(\-]{0,40}?(" + _N + r")(?:\s+(" + _N + r"))?"
    )
    return {
        "key": key,
        "label": text,
        "label_es": text,
        "section": "kpi",
        "unit": "currency",
        "aliases": [text],
        "patterns": [{"regex": regex, "multiplier": 1.0, "source": "table"}],
    }


def build_config(request: dict, *, read_tables: bool) -> tuple[dict, list[str]]:
    """Return the per-request config dict and the ordered custom metric keys."""
    config: dict = {}
    config_slug = str(request.get("config_slug") or "").strip()
    if config_slug:
        path = ROOT / "configs" / f"{config_slug}.yaml"
        if path.is_file():
            config = dict(load_config(path) or {})
    catalog_keys = [str(key) for key in request.get("metrics") or []]
    existing = set(catalog_keys)
    for entry in config.get("custom_metrics") or []:
        if isinstance(entry, dict) and entry.get("key"):
            existing.add(str(entry["key"]))
    custom_entries = []
    custom_keys: list[str] = []
    for text in request.get("custom_metrics") or []:
        entry = custom_metric_entry(str(text), existing)
        custom_entries.append(entry)
        custom_keys.append(entry["key"])
    if custom_entries:
        config["custom_metrics"] = list(config.get("custom_metrics") or []) + custom_entries
    if not read_tables:
        config["table_extract"] = {"disabled": True}
    return config, custom_keys


def period_label(request: dict) -> str:
    override = str(request.get("period") or "").strip().upper()
    if override:
        if not _LABEL_RE.fullmatch(override):
            raise ExtractionError(f"Periodo no válido: {override}")
        return override
    filename = str(request.get("filename") or "documento.pdf")
    guessed = infer_period_label(Path(filename).stem)
    if guessed:
        return guessed
    fallback = slug_key(Path(filename).stem)
    return fallback or "documento"


def materialize_work_pdf(request_dir: Path, label: str) -> Path:
    source = request_dir / "source.pdf"
    if not source.is_file():
        raise ExtractionError("No se encontró el PDF de la solicitud.")
    work = request_dir / "work"
    work.mkdir(exist_ok=True)
    target = work / f"{label}.pdf"
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)
    return target


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def _clean(value):
    if value is None:
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    return value


def load_long_rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    import pandas as pd

    frame = pd.read_csv(path)
    if "metric" not in frame.columns:
        return []
    rows = []
    for record in frame.to_dict("records"):
        rows.append({column: _clean(record.get(column)) for column in LONG_COLUMNS})
    return rows


def write_csv(rows: list[dict], path: Path) -> None:
    import csv

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LONG_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in LONG_COLUMNS})


def _number_format(unit: object) -> str:
    unit = str(unit or "")
    if unit == "pct":
        return "0.0"
    if unit in {"ratio", "per_share"}:
        return "0.00"
    return "#,##0.0"


def write_xlsx(path: Path, rows: list[dict], requested: list[dict], info: dict) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    bold = Font(bold=True)
    shade = PatternFill("solid", fgColor="FFF3E0")
    workbook = Workbook()

    sheet = workbook.active
    sheet.title = "Observaciones"
    sheet.append([SPANISH_HEADERS[column] for column in LONG_COLUMNS])
    for cell in sheet[1]:
        cell.font = bold
    for row in rows:
        sheet.append([row.get(column) for column in LONG_COLUMNS])
        index = sheet.max_row
        numfmt = _number_format(row.get("unit"))
        for column_name in ("current", "prior"):
            sheet.cell(row=index, column=LONG_COLUMNS.index(column_name) + 1).number_format = numfmt
        sheet.cell(row=index, column=LONG_COLUMNS.index("var_pct") + 1).number_format = "0.0"
        sheet.cell(row=index, column=LONG_COLUMNS.index("confidence") + 1).number_format = "0.00"
        confidence = row.get("confidence")
        try:
            low = confidence is not None and float(confidence) < LOW_CONFIDENCE
        except (TypeError, ValueError):
            low = False
        if low or row.get("flagged") is True:
            for cell in sheet[index]:
                cell.fill = shade
    sheet.freeze_panes = "A2"
    if rows:
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(LONG_COLUMNS))}{sheet.max_row}"
    widths = {"period": 11, "metric": 26, "label": 34, "current": 15, "prior": 15,
              "var_pct": 9, "unit": 10, "confidence": 11, "validated": 10,
              "flagged": 10, "source": 60}
    for position, column in enumerate(LONG_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(position)].width = widths[column]

    summary = workbook.create_sheet("Resumen")
    header_rows = [
        ("Archivo", info.get("filename")),
        ("Periodo", info.get("period")),
        ("Empresa", info.get("company") or "Genérica"),
        ("Generado", info.get("generated_at")),
        ("Métricas solicitadas", len(requested)),
        ("Métricas encontradas", sum(1 for item in requested if item["found"])),
    ]
    for label, value in header_rows:
        summary.append([label, value])
        summary.cell(row=summary.max_row, column=1).font = bold
    summary.append([])
    summary.append(["Clave", "Etiqueta", "Origen", "Estado", "Valor", "Periodo", "Confianza"])
    for cell in summary[summary.max_row]:
        cell.font = bold
    for item in requested:
        summary.append([
            item["key"],
            item["label"],
            item["origin"],
            "Encontrada" if item["found"] else "No encontrada",
            item.get("value"),
            item.get("period"),
            item.get("confidence"),
        ])
        index = summary.max_row
        summary.cell(row=index, column=5).number_format = _number_format(item.get("unit"))
        summary.cell(row=index, column=7).number_format = "0.00"
        if not item["found"]:
            for cell in summary[index]:
                cell.fill = shade
    for position, width in enumerate((28, 40, 12, 14, 16, 11, 11), start=1):
        summary.column_dimensions[get_column_letter(position)].width = width
    summary.cell(row=1, column=2).alignment = Alignment(wrap_text=False)

    workbook.save(path)


def summarize_requested(
    request: dict, custom_keys: list[str], rows: list[dict], label_lookup: dict[str, str],
) -> list[dict]:
    by_key: dict[str, dict] = {}
    for row in rows:
        key = str(row.get("metric"))
        previous = by_key.get(key)
        if previous is None or (row.get("confidence") or 0) > (previous.get("confidence") or 0):
            by_key[key] = row
    requested = []
    for key in request.get("metrics") or []:
        found = by_key.get(key)
        requested.append({
            "key": key,
            "label": label_lookup.get(key, key),
            "origin": "catálogo",
            "found": found is not None,
            "value": found.get("current") if found else None,
            "period": found.get("period") if found else None,
            "confidence": found.get("confidence") if found else None,
            "unit": found.get("unit") if found else None,
        })
    for key, text in zip(custom_keys, request.get("custom_metrics") or []):
        found = by_key.get(key)
        requested.append({
            "key": key,
            "label": str(text),
            "origin": "adicional",
            "found": found is not None,
            "value": found.get("current") if found else None,
            "period": found.get("period") if found else None,
            "confidence": found.get("confidence") if found else None,
            "unit": found.get("unit") if found else None,
        })
    return requested


def _catalog_labels(config_slug: str) -> dict[str, str]:
    from src.model.financial_model import METRICS, apply_config

    defs = list(METRICS)
    if config_slug:
        path = ROOT / "configs" / f"{config_slug}.yaml"
        if path.is_file():
            defs = apply_config(METRICS, load_config(path) or {})
    return {metric.key: metric.label_es or metric.label for metric in defs}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_request(
    request_dir: Path,
    output_dir: Path,
    *,
    use_llm: bool = False,
    read_tables: bool | None = None,
) -> dict:
    request_dir = request_dir.resolve()
    metadata_path = request_dir / "request.json"
    if not metadata_path.is_file():
        raise ExtractionError("No se encontró request.json en la solicitud.")
    try:
        request = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExtractionError("request.json está dañado.") from exc
    if not isinstance(request, dict):
        raise ExtractionError("request.json está dañado.")

    output_dir = output_dir if output_dir.is_absolute() else ROOT / output_dir
    output_dir = output_dir.resolve()
    outputs_root = (ROOT / "outputs").resolve()
    if not output_dir.is_relative_to(outputs_root):
        raise ExtractionError("El directorio de salida debe estar dentro de outputs/.")

    output_format = str(request.get("format") or "both").lower()
    if output_format not in {"both", "csv", "xlsx"}:
        raise ExtractionError(f"Formato no válido: {output_format}")
    tables = bool(request.get("read_tables", True)) if read_tables is None else read_tables

    label = period_label(request)
    work_pdf = materialize_work_pdf(request_dir, label)
    config, custom_keys = build_config(request, read_tables=tables)
    config_path = request_dir / "extractor.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    metric_keys = [str(key) for key in request.get("metrics") or []] + custom_keys

    from src.extract.pipeline import run as run_pipeline

    long_csv = request_dir / "work" / "observations_long.csv"
    long_csv.unlink(missing_ok=True)
    try:
        run_pipeline(
            work_pdf,
            metrics=metric_keys,
            config=config_path,
            output_csv=long_csv,
            long_format=True,
            use_llm=use_llm,
            verbose=False,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as job failure text
        raise ExtractionError(f"No se pudo leer el PDF: {type(exc).__name__}: {exc}") from exc

    rows = load_long_rows(long_csv)
    rows.sort(key=lambda row: (str(row.get("period")), str(row.get("metric"))))
    requested = summarize_requested(
        request, custom_keys, rows, _catalog_labels(str(request.get("config_slug") or "")),
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = slug_key(Path(str(request.get("filename") or "documento.pdf")).stem) or "documento"
    written: list[Path] = []
    if output_format in {"both", "csv"}:
        csv_path = output_dir / f"{stem}_observaciones.csv"
        write_csv(rows, csv_path)
        written.append(csv_path)
    if output_format in {"both", "xlsx"}:
        xlsx_path = output_dir / f"{stem}_observaciones.xlsx"
        write_xlsx(
            xlsx_path,
            rows,
            requested,
            {
                "filename": request.get("filename"),
                "period": label,
                "company": request.get("company"),
                "generated_at": _now(),
            },
        )
        written.append(xlsx_path)

    return {
        "period": label,
        "observations": len(rows),
        "requested": len(requested),
        "found": sum(1 for item in requested if item["found"]),
        "missing": [item["key"] for item in requested if not item["found"]],
        "outputs": [str(path) for path in written],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request_dir", type=Path, help="private request directory")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="destination inside outputs/ (created when missing)")
    parser.add_argument("--no-tables", action="store_true",
                        help="skip the pdfplumber table tier regardless of the request")
    parser.add_argument("--llm", action="store_true",
                        help="enable the Tier-4 LLM fallback (needs ANTHROPIC_API_KEY)")
    args = parser.parse_args(argv)
    try:
        summary = run_request(
            args.request_dir,
            args.output_dir,
            use_llm=args.llm,
            read_tables=False if args.no_tables else None,
        )
    except ExtractionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Periodo: {summary['period']}")
    print(f"Observaciones: {summary['observations']}")
    print(f"Métricas encontradas: {summary['found']} de {summary['requested']}")
    if summary["missing"]:
        print("Sin evidencia: " + ", ".join(summary["missing"]))
    for path in summary["outputs"]:
        print(f"Saved → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
