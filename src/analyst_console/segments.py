"""Validated Segments requests created by the local analyst launchpad."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import re
from urllib.parse import urlparse
import uuid
from typing import Any

from .operations import OperationError


DERIVED_ROWS = {
    "yoy": "YoY",
    "margin": "Margin",
    "bps_change": "bps change",
    "pct_consolidated": "As % of Consolidated",
    "pct_total": "As % of Total",
    "check": "Check",
    "two_year": "2-year comp",
    "fx_effect": "FX Effect",
    "avg_store_size": "Avg Store Size",
    "effective_tax_rate": "Effective Tax Rate",
}

_REQUEST_RE = re.compile(r"^[0-9a-f]{16}$")
_PIN_RE = re.compile(r"^(.*?)\s*\{([A-Za-z][A-Za-z0-9_]*)\}\s*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _text(value: object, field: str, *, maximum: int = 160) -> str:
    result = str(value or "").strip()
    if not result:
        raise OperationError(f"Falta {field}.")
    if len(result) > maximum or _CONTROL_RE.search(result):
        raise OperationError(f"{field.capitalize()} no es válido.")
    return result


def _metric_defs(project_root: Path, company_slug: str = ""):
    from src.excel.segments_sheet import load_metric_defs

    config = project_root / "configs" / f"{company_slug}.yaml"
    return load_metric_defs(str(config) if company_slug and config.is_file() else None)


def metric_catalog(project_root: Path, company_slug: str = "") -> list[dict[str, str]]:
    """Return the canonical metric choices valid for a company build."""
    return [
        {
            "key": metric.key,
            "label": metric.label_es or metric.label,
            "label_en": metric.label,
            "section": metric.section,
            "unit": metric.unit,
        }
        for metric in sorted(_metric_defs(project_root, company_slug), key=lambda row: row.key)
    ]


def _derived_kind(label: str) -> str | None:
    normalized = re.sub(r"\s+", " ", label.strip().lower())
    for kind, canonical in DERIVED_ROWS.items():
        if normalized == canonical.lower():
            return kind
    if normalized.startswith("yoy "):
        return "yoy"
    if re.match(r"^as % of (?:consolidated|total)$", normalized):
        return "pct_consolidated" if normalized.endswith("consolidated") else "pct_total"
    return None


def _parse_preset(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    name = path.stem.replace("_", " ").title()
    ir_url = ""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# "):
            name = line[2:].strip()
        elif not ir_url:
            match = re.search(r"https?://\S+", line)
            if match:
                ir_url = match.group(0)
        if line.startswith("## "):
            current = {"name": line[3:].strip(), "rows": []}
            sections.append(current)
            continue
        match = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+)$", line)
        if match and current is not None:
            label = match.group(1).strip()
            pinned = _PIN_RE.match(label)
            if pinned:
                current["rows"].append(
                    {"kind": "metric", "label": pinned.group(1).strip(), "key": pinned.group(2)}
                )
            else:
                kind = _derived_kind(label)
                current["rows"].append(
                    {"kind": kind or "metric", "label": label, "key": ""}
                )
    return {
        "company": name,
        "ticker": "",
        "ir_url": ir_url,
        "max_reports": 100,
        "force_download": False,
        "sections": sections or [{"name": "Resultados", "rows": []}],
    }


class SegmentRequestStore:
    """Materialize a UI request as the strict Markdown + CSV input contract."""

    def __init__(self, project_root: Path, state_dir: Path):
        self.project_root = project_root.resolve()
        self.root = state_dir.resolve() / "requests"
        self.root.mkdir(parents=True, exist_ok=True)

    def setup(self, company_slug: str = "") -> dict[str, Any]:
        allowed = {
            path.stem: path
            for path in (self.project_root / "inputs").glob("*.md")
            if not path.name.startswith("_")
        }
        if company_slug and company_slug not in allowed:
            raise OperationError("La plantilla de empresa no existe.")
        preset = _parse_preset(allowed[company_slug]) if company_slug else {
            "company": "",
            "ticker": "",
            "ir_url": "",
            "max_reports": 100,
            "force_download": False,
            "sections": [{"name": "Resultados", "rows": []}],
        }
        config_path = self.project_root / "configs" / f"{company_slug}.yaml"
        if company_slug and config_path.is_file():
            from src.model.financial_model import load_config

            company_config = load_config(config_path).get("company", {}) or {}
            preset["ticker"] = str(company_config.get("ticker") or "")
        return {
            "preset": preset,
            "metrics": metric_catalog(self.project_root, company_slug),
            "derived_rows": [
                {"kind": kind, "label": label} for kind, label in DERIVED_ROWS.items()
            ],
        }

    def prepare(self, payload: dict[str, Any]) -> str:
        requested_slug = str(payload.get("template_company") or "").strip()
        known_templates = {
            path.stem: path for path in (self.project_root / "inputs").glob("*.md")
            if not path.name.startswith("_")
        }
        if requested_slug not in known_templates:
            raise OperationError("Elige una empresa de la lista disponible.")

        preset = _parse_preset(known_templates[requested_slug])
        company = _text(preset.get("company"), "el nombre de la empresa")
        ticker = str(preset.get("ticker") or "").strip()
        config_path = self.project_root / "configs" / f"{requested_slug}.yaml"
        if config_path.is_file():
            from src.model.financial_model import load_config

            company_config = load_config(config_path).get("company", {}) or {}
            ticker = str(company_config.get("ticker") or ticker)
        ir_url = _text(
            preset.get("ir_url"),
            "la fuente histórica configurada para esta empresa",
            maximum=500,
        )
        parsed_url = urlparse(ir_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise OperationError("La empresa seleccionada no tiene una fuente histórica válida.")

        raw_sections = payload.get("sections")
        if not isinstance(raw_sections, list) or not 1 <= len(raw_sections) <= 40:
            raise OperationError("Agrega al menos una sección de métricas.")

        company_slug = requested_slug
        valid_keys = {row["key"] for row in metric_catalog(self.project_root, company_slug)}
        sections: list[dict[str, Any]] = []
        row_count = 0
        for raw_section in raw_sections:
            if not isinstance(raw_section, dict):
                raise OperationError("Una sección de métricas no es válida.")
            section_name = _text(raw_section.get("name"), "el nombre de una sección")
            raw_rows = raw_section.get("rows")
            if not isinstance(raw_rows, list) or not raw_rows:
                raise OperationError(f'La sección "{section_name}" no tiene filas.')
            rows: list[dict[str, str]] = []
            for raw_row in raw_rows:
                if not isinstance(raw_row, dict):
                    raise OperationError(f'Hay una fila inválida en "{section_name}".')
                kind = str(raw_row.get("kind") or "metric")
                label = _text(raw_row.get("label"), "la etiqueta de una métrica")
                if kind == "metric":
                    key = _text(raw_row.get("key"), f'la métrica financiera de "{label}"')
                    if key not in valid_keys:
                        raise OperationError(
                            f'La métrica seleccionada para "{label}" no está disponible para esta empresa.'
                        )
                elif kind in DERIVED_ROWS:
                    key = ""
                    label = DERIVED_ROWS[kind]
                else:
                    raise OperationError(f'El tipo de fila "{kind}" no es válido.')
                rows.append({"kind": kind, "label": label, "key": key})
                row_count += 1
            sections.append({"name": section_name, "rows": rows})
        if row_count > 600:
            raise OperationError("La solicitud tiene demasiadas filas.")
        if not any(row["kind"] == "metric" for section in sections for row in section["rows"]):
            raise OperationError("Agrega por lo menos una métrica financiera.")

        request_id = uuid.uuid4().hex[:16]
        request_dir = self.root / request_id
        request_dir.mkdir(mode=0o700)
        markdown_path = request_dir / "input.md"
        csv_path = request_dir / "analyst_metrics.csv"
        metadata_path = request_dir / "request.json"

        markdown = [f"# {company}", f"IR: {ir_url}", f"Analyst-Company: {ticker or company}", ""]
        for section in sections:
            markdown.append(f'## {section["name"]}')
            for row in section["rows"]:
                pin = f' {{{row["key"]}}}' if row["kind"] == "metric" else ""
                markdown.append(f'- {row["label"]}{pin}')
            markdown.append("")
        markdown_path.write_text("\n".join(markdown), encoding="utf-8")

        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["section", "label", "key"])
            writer.writeheader()
            for section in sections:
                for row in section["rows"]:
                    writer.writerow(
                        {"section": section["name"], "label": row["label"], "key": row["key"]}
                    )

        metadata = {
            "company": company,
            "ticker": ticker,
            "slug": company_slug,
            "template_company": requested_slug,
            "max_reports": 100,
            "force_download": False,
            "sections": sections,
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return request_id


def resolve_prepared_request(state_dir: Path, request_id: str) -> tuple[Path, Path, dict[str, Any]]:
    if not _REQUEST_RE.fullmatch(request_id):
        raise OperationError("La solicitud de Segmentos no es válida.")
    request_dir = (state_dir / "requests" / request_id).resolve()
    if not request_dir.is_relative_to((state_dir / "requests").resolve()):
        raise OperationError("La solicitud de Segmentos no es válida.")
    markdown_path = request_dir / "input.md"
    csv_path = request_dir / "analyst_metrics.csv"
    metadata_path = request_dir / "request.json"
    if not all(path.is_file() for path in (markdown_path, csv_path, metadata_path)):
        raise OperationError("La solicitud de Segmentos ya no está disponible.")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationError("La solicitud de Segmentos está dañada.") from exc
    return markdown_path, csv_path, metadata
