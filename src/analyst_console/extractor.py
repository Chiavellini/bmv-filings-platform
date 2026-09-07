"""Private PDF uploads and validated extraction requests for the launchpad."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import unicodedata
import uuid
from typing import Any

from .operations import OperationError, company_catalog
from .segments import metric_catalog


MAX_UPLOAD_BYTES = 60 * 1024 * 1024
MAX_SOURCES = 12
MAX_METRICS = 200
MAX_CUSTOM_METRICS = 40
MAX_CUSTOM_LENGTH = 80
FORMATS = ("both", "csv", "xlsx")
STALE_UPLOAD_SECONDS = 24 * 3600

_REQUEST_RE = re.compile(r"^[0-9a-f]{16}$")
_PERIOD_RE = re.compile(r"^20\d{2}-(?:[1-4]T|FY)$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_FILENAME_KEEP_RE = re.compile(r"[^A-Za-z0-9._ -]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sanitize_filename(name: object) -> str:
    """Reduce a browser-supplied name to a boring ``*.pdf`` basename."""
    raw = unicodedata.normalize("NFKC", str(name or "")).replace("\\", "/")
    base = raw.rsplit("/", 1)[-1]
    base = _FILENAME_KEEP_RE.sub(" ", base)
    base = re.sub(r"\s+", " ", base).strip(" .-")
    stem, dot, suffix = base.rpartition(".")
    if dot and suffix.lower() == "pdf":
        base = stem
    base = base.strip(" .-")[:100].strip(" .-")
    return f"{base or 'documento'}.pdf"


def period_guess_for(filename: str) -> str | None:
    from src.shared.report_index import infer_period_label

    return infer_period_label(Path(filename).stem)


def _custom_metrics(raw: object) -> list[str]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, list):
        raise OperationError("Las métricas adicionales no son válidas.")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = re.sub(r"\s+", " ", str(item or "")).strip()
        if not text:
            continue
        if len(text) > MAX_CUSTOM_LENGTH or _CONTROL_RE.search(text):
            raise OperationError(f'La métrica adicional "{text[:40]}" no es válida.')
        marker = text.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(text)
    if len(result) > MAX_CUSTOM_METRICS:
        raise OperationError(
            f"Agrega como máximo {MAX_CUSTOM_METRICS} métricas adicionales."
        )
    return result


class ExtractorRequestStore:
    """Keep uploaded PDFs private and turn UI requests into a strict contract."""

    def __init__(self, project_root: Path, state_dir: Path):
        self.project_root = project_root.resolve()
        self.root = state_dir.resolve() / "extractor"
        self.root.mkdir(parents=True, exist_ok=True)

    # -- uploads -----------------------------------------------------------

    def _prune_stale_uploads(self) -> None:
        cutoff = time.time() - STALE_UPLOAD_SECONDS
        try:
            candidates = list(self.root.iterdir())
        except OSError:
            return
        for folder in candidates:
            try:
                if not folder.is_dir() or not _REQUEST_RE.fullmatch(folder.name):
                    continue
                if (folder / "request.json").exists() or (folder / "claimed_by.json").exists():
                    continue
                if folder.stat().st_mtime > cutoff:
                    continue
                for child in folder.rglob("*"):
                    if child.is_file():
                        child.unlink()
                for child in sorted(folder.rglob("*"), reverse=True):
                    if child.is_dir():
                        child.rmdir()
                folder.rmdir()
            except OSError:
                continue

    def save_upload(self, filename: object, data: bytes) -> dict[str, Any]:
        if not data:
            raise OperationError("El archivo está vacío.")
        if len(data) > MAX_UPLOAD_BYTES:
            raise OperationError("El PDF supera el límite de 60 MB.")
        if not data.startswith(b"%PDF-"):
            raise OperationError("El archivo no es un PDF válido.")
        self._prune_stale_uploads()
        safe_name = sanitize_filename(filename)
        upload_id = uuid.uuid4().hex[:16]
        folder = self.root / upload_id
        folder.mkdir(mode=0o700)
        (folder / "source.pdf").write_bytes(data)
        metadata = {
            "upload": upload_id,
            "filename": safe_name,
            "size": len(data),
            "period_guess": period_guess_for(safe_name),
            "created_at": _now(),
        }
        (folder / "upload.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return {key: metadata[key] for key in ("upload", "filename", "size", "period_guess")}

    def _upload_dir(self, upload_id: object) -> Path:
        value = str(upload_id or "").strip()
        if not _REQUEST_RE.fullmatch(value):
            raise OperationError("Primero sube un PDF.")
        folder = (self.root / value).resolve()
        if not folder.is_relative_to(self.root) or not (folder / "source.pdf").is_file():
            raise OperationError("El PDF subido ya no está disponible. Súbelo de nuevo.")
        return folder

    # -- requests ----------------------------------------------------------

    @staticmethod
    def _upload_metadata(folder: Path) -> dict[str, Any]:
        try:
            metadata = json.loads((folder / "upload.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return metadata if isinstance(metadata, dict) else {}

    def _sources(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw = payload.get("uploads")
        if raw is None:
            raw = [{"upload": payload.get("upload"), "period": payload.get("period")}]
        if not isinstance(raw, list) or not raw:
            raise OperationError("Primero sube un PDF.")
        if len(raw) > MAX_SOURCES:
            raise OperationError(f"Sube como máximo {MAX_SOURCES} archivos por solicitud.")
        sources: list[dict[str, Any]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise OperationError("La lista de archivos no es válida.")
            folder = self._upload_dir(entry.get("upload"))
            if any(source["upload"] == folder.name for source in sources):
                raise OperationError("Hay un archivo repetido en la solicitud.")
            upload = self._upload_metadata(folder)
            filename = str(upload.get("filename") or "documento.pdf")
            period = str(entry.get("period") or "").strip().upper()
            if period and not _PERIOD_RE.fullmatch(period):
                raise OperationError(
                    f"El periodo de {filename} debe tener el formato 2025-2T o 2025-FY."
                )
            sources.append({
                "upload": folder.name,
                "filename": filename,
                "period": period or str(upload.get("period_guess") or ""),
                "period_guess": upload.get("period_guess"),
                "folder": folder,
            })
        if len(sources) > 1:
            seen: dict[str, str] = {}
            for source in sources:
                if not source["period"]:
                    raise OperationError(
                        f"No se detectó el periodo de {source['filename']}; escríbelo (ej. 2025-2T)."
                    )
                previous = seen.get(source["period"])
                if previous:
                    raise OperationError(
                        f"{previous} y {source['filename']} apuntan al mismo periodo "
                        f"({source['period']}); asigna periodos distintos."
                    )
                seen[source["period"]] = source["filename"]
        return sources

    def prepare(self, payload: dict[str, Any]) -> str:
        sources = self._sources(payload)

        company = str(payload.get("company") or "").strip()
        config_slug = ""
        if company:
            allowed = {row["slug"] for row in company_catalog(self.project_root)["segments"]}
            if company not in allowed:
                raise OperationError("Elige una empresa de la lista disponible.")
            from src.model.company_config import resolve_company_config_slug

            config_slug = resolve_company_config_slug(self.project_root, company)
            if not (self.project_root / "configs" / f"{config_slug}.yaml").is_file():
                config_slug = ""

        raw_metrics = payload.get("metrics")
        if raw_metrics is None:
            raw_metrics = []
        if not isinstance(raw_metrics, list):
            raise OperationError("La lista de métricas no es válida.")
        valid_keys = {row["key"] for row in metric_catalog(self.project_root, company)}
        metrics: list[str] = []
        for raw_key in raw_metrics:
            key = str(raw_key or "").strip()
            if key not in valid_keys:
                raise OperationError(
                    f'La métrica "{key[:40]}" no está disponible para esta empresa.'
                )
            if key not in metrics:
                metrics.append(key)
        if len(metrics) > MAX_METRICS:
            raise OperationError("La solicitud tiene demasiadas métricas.")

        custom = _custom_metrics(payload.get("custom_metrics"))
        if not metrics and not custom:
            raise OperationError("Elige al menos una métrica o escribe una adicional.")

        period = str(payload.get("period") or "").strip().upper()
        if len(sources) == 1 and not payload.get("uploads"):
            if period and not _PERIOD_RE.fullmatch(period):
                raise OperationError("El periodo debe tener el formato 2025-2T o 2025-FY.")
            sources[0]["period"] = period or str(sources[0]["period_guess"] or "")
        else:
            period = sources[0]["period"]

        output_format = str(payload.get("format") or "both").strip().lower()
        if output_format not in FORMATS:
            raise OperationError("Elige CSV, Excel o ambos.")

        read_tables = payload.get("read_tables", True)
        if not isinstance(read_tables, bool):
            raise OperationError("La opción de tablas no es válida.")

        request_id = uuid.uuid4().hex[:16]
        request_dir = self.root / request_id
        request_dir.mkdir(mode=0o700)
        first = sources[0]
        request = {
            "sources": [
                {"upload": source["upload"], "filename": source["filename"], "period": source["period"]}
                for source in sources
            ],
            "upload": first["upload"],
            "filename": first["filename"],
            "period": first["period"] if len(sources) > 1 else period,
            "period_guess": first["period_guess"],
            "company": company,
            "config_slug": config_slug,
            "metrics": metrics,
            "custom_metrics": custom,
            "format": output_format,
            "read_tables": read_tables,
            "created_at": _now(),
        }
        (request_dir / "request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        claim = json.dumps({"request": request_id, "claimed_at": _now()}) + "\n"
        for source in sources:
            (source["folder"] / "claimed_by.json").write_text(claim, encoding="utf-8")
        return request_id


def resolve_extractor_request(state_dir: Path, request_id: str) -> tuple[Path, dict[str, Any]]:
    if not _REQUEST_RE.fullmatch(request_id):
        raise OperationError("La solicitud del Extractor no es válida.")
    root = (state_dir / "extractor").resolve()
    request_dir = (root / request_id).resolve()
    if not request_dir.is_relative_to(root):
        raise OperationError("La solicitud del Extractor no es válida.")
    metadata_path = request_dir / "request.json"
    if not metadata_path.is_file():
        raise OperationError("La solicitud del Extractor ya no está disponible.")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationError("La solicitud del Extractor está dañada.") from exc
    if not isinstance(metadata, dict):
        raise OperationError("La solicitud del Extractor está dañada.")
    sources = metadata.get("sources")
    if isinstance(sources, list) and sources:
        for source in sources:
            upload_id = str((source or {}).get("upload") or "") if isinstance(source, dict) else ""
            if not _REQUEST_RE.fullmatch(upload_id):
                raise OperationError("La solicitud del Extractor está dañada.")
            if not (root / upload_id / "source.pdf").is_file():
                raise OperationError(
                    "Uno de los PDF de la solicitud ya no está disponible. Súbelo de nuevo."
                )
    elif not (request_dir / "source.pdf").is_file():
        raise OperationError("La solicitud del Extractor ya no está disponible.")
    return request_dir, metadata


def latest_extract(project_root: Path) -> Path | None:
    folder = project_root / "outputs" / "extractor"
    if not folder.is_dir():
        return None
    candidates = [
        path for path in folder.glob("*/*")
        if path.is_file() and path.suffix.lower() in {".xlsx", ".csv"}
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)
