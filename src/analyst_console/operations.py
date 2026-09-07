"""Allowlisted project operations exposed by the local Analyst Console.

This module deliberately returns argv arrays instead of shell strings.  The web
surface can choose a documented project action, but it can never submit an
arbitrary command or filesystem path.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class OperationError(ValueError):
    """The requested operation or its parameters are not allowed."""


@dataclass(frozen=True, slots=True)
class Operation:
    key: str
    label: str
    node: str
    description: str
    mutates: bool = False
    confirmation: str | None = None
    company_source: str | None = None

    def public(self) -> dict[str, Any]:
        return asdict(self)


OPERATIONS = {
    item.key: item
    for item in (
        Operation(
            "estate_check",
            "Revisar conexión",
            "estate",
            "Verifica que el Estate portátil esté conectado y listo.",
        ),
        Operation(
            "estate_audit",
            "Auditar cobertura",
            "estate",
            "Revisa la cobertura actual sin descargar ni modificar datos.",
        ),
        Operation(
            "estate_refresh_all",
            "Actualizar biblioteca completa",
            "estate",
            "Actualiza documentos trimestrales, derivados, índice Alpha y noticias dirigidas.",
            mutates=True,
            confirmation="ACTUALIZAR ESTATE",
        ),
        Operation(
            "soft_coverage_audit",
            "Revisar cobertura Soft",
            "estate",
            "Actualiza el reporte de cobertura trimestral del universo Soft.",
        ),
        Operation(
            "soft_expand_plan",
            "Buscar trimestres faltantes",
            "estate",
            "Prepara una lista reanudable de trimestres disponibles en el archivo.",
        ),
        Operation(
            "soft_expand_apply",
            "Ampliar el Estate",
            "estate",
            "Descarga al Estate los trimestres faltantes encontrados.",
            mutates=True,
            confirmation="AMPLIAR ESTATE",
        ),
        Operation(
            "soft_model",
            "Generar modelo de cobertura",
            "models",
            "Crea el modelo Soft de la empresa seleccionada.",
            company_source="soft",
        ),
        Operation(
            "segments_model",
            "Generar modelo de segmentos",
            "models",
            "Crea el modelo de Segmentos listo para el analista.",
            company_source="segments",
        ),
        Operation(
            "pdf_extract",
            "Extraer observaciones de un PDF",
            "models",
            "Extrae las métricas elegidas de un PDF subido y genera CSV y/o Excel.",
        ),
        Operation(
            "soft_master",
            "Actualizar matriz de cobertura",
            "soft",
            "Regenera las matrices Soft con los modelos disponibles.",
        ),
    )
}


def company_catalog(project_root: Path) -> dict[str, list[dict[str, str]]]:
    """Return the only company values accepted by model operations."""

    def rows(folder: Path) -> list[dict[str, str]]:
        result = []
        for path in sorted(folder.glob("*.md")):
            if path.name.startswith("_"):
                continue
            display = path.stem.replace("_", " ").title()
            try:
                first = next(
                    line[2:].strip()
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("# ")
                )
                if first:
                    display = first
            except (OSError, StopIteration):
                pass
            result.append({"slug": path.stem, "name": display})
        return result

    from src.acquisition.registry import load_issuer_registry

    registry = load_issuer_registry(project_root / "configs" / "issuers.yaml")
    segment_companies = [
        {
            "slug": issuer.slug,
            "name": f"{issuer.ticker} · {issuer.name}",
        }
        for issuer in registry.issuers
        if issuer.active
    ]
    segment_companies.sort(key=lambda row: row["name"].casefold())

    return {
        "soft": rows(project_root / "soft" / "inputs"),
        # Segment extraction is backed by the canonical BMV issuer registry,
        # not by the small set of hand-written Markdown examples in inputs/.
        "segments": segment_companies,
    }


def operation_command(
    project_root: Path,
    key: str,
    params: dict[str, Any] | None = None,
    *,
    environment: dict[str, str] | None = None,
) -> tuple[Operation, list[str], Path]:
    """Resolve an operation into a safe argv list and working directory."""
    params = params or {}
    operation = OPERATIONS.get(key)
    if operation is None:
        raise OperationError(f"Operación desconocida: {key}")

    root_python = project_root / ".venv" / "bin" / "python"
    root_cli = project_root / ".venv" / "bin"
    soft_python = project_root / "soft" / ".venv" / "bin" / "python"
    if key == "estate_check":
        argv = [str(root_python), "scripts/check_estate_connection.py"]
        cwd = project_root
    elif key == "estate_audit":
        argv = [str(root_cli / "refresh-quarterly-estate"), "audit", "--json"]
        cwd = project_root
    elif key == "estate_refresh_all":
        argv = [str(root_python), "scripts/refresh_analyst_estate.py"]
        cwd = project_root
    elif key == "soft_coverage_audit":
        argv = [str(root_python), "scripts/audit_soft_quarterly_coverage.py"]
        cwd = project_root
    elif key == "soft_expand_plan":
        argv = [str(root_python), "scripts/expand_soft_quarterlies.py"]
        cwd = project_root
    elif key == "soft_expand_apply":
        argv = [str(root_python), "scripts/expand_soft_quarterlies.py", "--apply"]
        cwd = project_root
    elif key in {"soft_model", "segments_model"}:
        if key == "segments_model" and params.get("request"):
            from .segments import resolve_prepared_request

            raw_state_dir = (environment or {}).get("ANALYST_CONSOLE_STATE_DIR")
            if not raw_state_dir:
                raise OperationError("El lanzador no tiene un directorio de trabajo configurado.")
            markdown, analyst_csv, metadata = resolve_prepared_request(
                Path(raw_state_dir).resolve(), str(params.get("request") or "")
            )
            argv = [
                str(root_python),
                "scripts/build_segments.py",
                str(markdown),
                "--analyst-metrics",
                str(analyst_csv),
                "--analyst-company",
                str(metadata.get("ticker") or metadata.get("company") or ""),
                "--estate-only",
            ]
            cwd = project_root
        else:
            source = operation.company_source or ""
            allowed = {row["slug"] for row in company_catalog(project_root)[source]}
            company = str(params.get("company") or "")
            if company not in allowed:
                raise OperationError("Elige una empresa de la lista disponible.")
            if key == "soft_model":
                argv = [str(soft_python), "scripts/build_coverage.py", f"inputs/{company}.md"]
                cwd = project_root / "soft"
            else:
                argv = [str(root_python), "scripts/build_segments.py", f"inputs/{company}.md"]
                cwd = project_root
    elif key == "pdf_extract":
        from .extractor import resolve_extractor_request

        raw_state_dir = (environment or {}).get("ANALYST_CONSOLE_STATE_DIR")
        if not raw_state_dir:
            raise OperationError("El lanzador no tiene un directorio de trabajo configurado.")
        request_dir, _metadata = resolve_extractor_request(
            Path(raw_state_dir).resolve(), str(params.get("request") or "")
        )
        argv = [
            str(root_python),
            "scripts/extract_pdf_observations.py",
            str(request_dir),
            "--output-dir",
            f"outputs/extractor/{request_dir.name}",
        ]
        cwd = project_root
    elif key == "soft_master":
        argv = [
            str(soft_python),
            "scripts/build_master.py",
            "--no-network",
            "--skip-existing",
            "--core",
        ]
        cwd = project_root / "soft"
    else:  # pragma: no cover - OPERATIONS and this resolver are kept exhaustive
        raise OperationError(f"La operación no está implementada: {key}")

    executable = Path(argv[0])
    if not executable.is_file():
        raise OperationError(
            f"{operation.node.title()} todavía no está instalado en esta Mac. "
            f"Se esperaba {executable}."
        )
    return operation, argv, cwd


def validate_confirmation(operation: Operation, value: object) -> None:
    if operation.confirmation and value != operation.confirmation:
        raise OperationError(
            f'Escribe "{operation.confirmation}" para confirmar esta acción.'
        )
