"""Single configuration bridge between the codebase and a local estate bundle.

Users connect a transferred document estate by changing only ``estate_root`` in
``estate.json``.  Every path inside the bundle is relative to that root, so the
bundle can live anywhere on another computer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from estate_volume import inspect_estate_environment


BRIDGE_VERSION = 1
BRIDGE_ENV = "PDFS_ESTATE_BRIDGE"
ESTATE_ROOT_ENV = "PDFS_DOCUMENT_ESTATE"
REPORTS_VIEW_ENV = "PDFS_REPORTS_DIR"


class EstateBridgeError(RuntimeError):
    """The bridge file is missing, malformed, unsupported, or unsafe."""


def _expanded_path(raw: str | os.PathLike[str], *, relative_to: Path) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = relative_to / path
    return path.resolve()


def _bundle_path(root: Path, raw: object, *, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise EstateBridgeError(f"{field} must be a non-empty path string")
    path = _expanded_path(raw, relative_to=root)
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise EstateBridgeError(
            f"{field} must remain inside estate_root: {raw!r}"
        ) from exc
    return path


@dataclass(frozen=True)
class EstateBridge:
    """Resolved paths for one portable estate bundle."""

    config_path: Path
    estate_root: Path
    catalog_path: Path
    objects_dir: Path
    reports_view_dir: Path
    uploads_dir: Path
    models_dir: Path
    alpha_go_embedding_model_path: Path
    alpha_go_index_path: Path
    manifest_path: Path

    def resolve_object(self, object_key: str) -> Path:
        """Resolve a portable object key without allowing directory traversal."""
        return _bundle_path(self.estate_root, object_key, field="object_key")

    def connect_catalog(
        self,
        *,
        read_only: bool = False,
        timeout: float = 30.0,
    ) -> sqlite3.Connection:
        """Open the estate catalog with consistent SQLite settings."""
        status = inspect_estate_environment(self.estate_root)
        if not status.healthy:
            raise EstateBridgeError("; ".join(status.problems))
        if read_only:
            uri = f"file:{self.catalog_path.as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=timeout)
        else:
            connection = sqlite3.connect(self.catalog_path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def register_alpha_upload(
        self,
        *,
        alpha_doc_id: str,
        company: str,
        period: str | None,
        doc_type: str,
        title: str,
        language: str | None,
        memberships: Iterable[dict[str, object]],
        markdown_path: str | Path,
        original_path: str | Path,
        original_filename: str,
    ) -> str:
        """Register one successful Alpha Go upload in the shared estate.

        Alpha Go owns the live search index, while the estate catalog is the
        repository-wide source of truth. This method is the single write bridge
        between them: the document, company memberships, project identity, and
        searchable/original artifacts are committed in one SQLite transaction.
        """
        estate_document_id = f"alpha-go:{alpha_doc_id}"
        markdown = Path(markdown_path).expanduser().resolve()
        original = Path(original_path).expanduser().resolve()
        artifacts = (
            [(markdown, "original")]
            if markdown == original
            else [(markdown, "search_text"), (original, "original")]
        )
        missing = [str(path) for path, _role in artifacts if not path.is_file()]
        if missing:
            raise EstateBridgeError(
                "cannot register missing upload artifact(s): " + ", ".join(missing)
            )

        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        metadata = json.dumps(
            {
                "alpha_doc_id": alpha_doc_id,
                "original_filename": original_filename,
                "uploaded": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        membership_rows = list(memberships) or [
            {"company": company, "industry": None}
        ]

        with self.connect_catalog() as connection:
            required = {"documents", "artifacts", "project_records", "memberships"}
            present = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            absent = sorted(required - present)
            if absent:
                raise EstateBridgeError(
                    "estate catalog is missing required table(s): " + ", ".join(absent)
                )

            for path, _role in artifacts:
                owner = connection.execute(
                    "SELECT document_id FROM artifacts WHERE path=?", (str(path),)
                ).fetchone()
                if owner and owner["document_id"] != estate_document_id:
                    raise EstateBridgeError(
                        f"upload artifact is already owned by {owner['document_id']}: {path}"
                    )

            connection.execute(
                """
                INSERT INTO documents(
                    document_id,company,period,doc_type,title,language,source_url,
                    published_at,metadata_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET
                    company=excluded.company,
                    period=excluded.period,
                    doc_type=excluded.doc_type,
                    title=excluded.title,
                    language=excluded.language,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    estate_document_id,
                    company,
                    period,
                    doc_type,
                    title,
                    language,
                    None,
                    None,
                    metadata,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM memberships WHERE document_id=?",
                (estate_document_id,),
            )
            for membership in membership_rows:
                connection.execute(
                    """
                    INSERT INTO memberships(document_id,company,industry)
                    VALUES(?,?,?)
                    """,
                    (
                        estate_document_id,
                        str(membership.get("company") or company),
                        membership.get("industry"),
                    ),
                )
            connection.execute(
                """
                INSERT INTO project_records(project,project_record_id,document_id)
                VALUES('alpha-go',?,?)
                ON CONFLICT(project,project_record_id)
                DO UPDATE SET document_id=excluded.document_id
                """,
                (alpha_doc_id, estate_document_id),
            )

            current_paths = [str(path) for path, _role in artifacts]
            placeholders = ",".join("?" for _ in current_paths)
            connection.execute(
                f"""
                DELETE FROM artifacts
                WHERE document_id=? AND project='alpha-go'
                  AND path NOT IN ({placeholders})
                """,
                (estate_document_id, *current_paths),
            )
            for path, role in artifacts:
                stat = path.stat()
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                artifact_id = hashlib.sha256(
                    f"{estate_document_id}\0{path}".encode()
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO artifacts(
                        artifact_id,document_id,project,role,format,path,sha256,
                        size_bytes,mtime_ns,st_dev,st_ino,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(artifact_id) DO UPDATE SET
                        role=excluded.role,
                        format=excluded.format,
                        sha256=excluded.sha256,
                        size_bytes=excluded.size_bytes,
                        mtime_ns=excluded.mtime_ns,
                        st_dev=excluded.st_dev,
                        st_ino=excluded.st_ino
                    """,
                    (
                        artifact_id,
                        estate_document_id,
                        "alpha-go",
                        role,
                        path.suffix.lower().lstrip(".") or "file",
                        str(path),
                        digest.hexdigest(),
                        stat.st_size,
                        stat.st_mtime_ns,
                        stat.st_dev,
                        stat.st_ino,
                        now,
                    ),
                )

            if "document_projects" in present:
                connection.execute(
                    """
                    INSERT INTO document_projects(document_id,project,added_at)
                    VALUES(?,'alpha-go',?)
                    ON CONFLICT(document_id,project)
                    DO UPDATE SET added_at=excluded.added_at
                    """,
                    (estate_document_id, now),
                )
        return estate_document_id

    def validate(
        self,
        *,
        require_catalog: bool = True,
        require_alpha_index: bool = False,
    ) -> list[str]:
        """Return human-readable problems without mutating the bundle."""
        problems: list[str] = []
        if not self.estate_root.is_dir():
            problems.append(f"estate root does not exist: {self.estate_root}")
        if require_catalog and not self.catalog_path.is_file():
            problems.append(f"catalog does not exist: {self.catalog_path}")
        if require_alpha_index and not self.alpha_go_index_path.is_file():
            problems.append(
                f"Alpha Go index does not exist: {self.alpha_go_index_path}"
            )
        problems.extend(inspect_estate_environment(self.estate_root).problems)
        return list(dict.fromkeys(problems))

    def as_dict(self) -> dict[str, str | int]:
        """Serializable diagnostics for setup screens and support reports."""
        return {
            "bridge_version": BRIDGE_VERSION,
            "config_path": str(self.config_path),
            "estate_root": str(self.estate_root),
            "catalog": str(self.catalog_path),
            "objects": str(self.objects_dir),
            "reports_view": str(self.reports_view_dir),
            "uploads": str(self.uploads_dir),
            "models": str(self.models_dir),
            "alpha_go_embedding_model": str(
                self.alpha_go_embedding_model_path
            ),
            "alpha_go_index": str(self.alpha_go_index_path),
            "manifest": str(self.manifest_path),
        }


def _read_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EstateBridgeError(f"could not read estate bridge {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EstateBridgeError(f"estate bridge must contain a JSON object: {path}")
    version = payload.get("bridge_version", BRIDGE_VERSION)
    if version != BRIDGE_VERSION:
        raise EstateBridgeError(
            f"unsupported estate bridge version {version!r}; "
            f"expected {BRIDGE_VERSION}"
        )
    return payload


def load_estate_bridge(
    *,
    project_root: str | Path | None = None,
    config_path: str | Path | None = None,
) -> EstateBridge:
    """Load the one bridge file and resolve all paths relative to the estate.

    Resolution order:

    1. an explicit ``config_path``;
    2. ``PDFS_ESTATE_BRIDGE``;
    3. ``estate.json`` beside this module.

    ``PDFS_DOCUMENT_ESTATE`` remains a highest-priority root override for
    automation and backwards compatibility.
    """
    code_root = (
        Path(project_root).expanduser().resolve()
        if project_root is not None
        else Path(__file__).resolve().parent
    )
    raw_config = config_path or os.environ.get(BRIDGE_ENV)
    if raw_config:
        selected_config = _expanded_path(raw_config, relative_to=code_root)
    else:
        candidates = [
            *(ancestor / "estate.json" for ancestor in (code_root, *code_root.parents)),
            Path(__file__).resolve().with_name("estate.json"),
        ]
        selected_config = next(
            (candidate for candidate in candidates if candidate.is_file()),
            code_root / "estate.json",
        )
    config = _read_config(selected_config)

    raw_root = os.environ.get(ESTATE_ROOT_ENV) or config.get(
        "estate_root", "data/document_estate"
    )
    if not isinstance(raw_root, str) or not raw_root.strip():
        raise EstateBridgeError("estate_root must be a non-empty path string")
    estate_root = _expanded_path(raw_root, relative_to=selected_config.parent)

    catalog_path = _bundle_path(
        estate_root, config.get("catalog", "catalog.db"), field="catalog"
    )
    objects_dir = _bundle_path(
        estate_root, config.get("objects", "blobs"), field="objects"
    )
    reports_view_raw = os.environ.get(REPORTS_VIEW_ENV) or config.get(
        "reports_view", "views/reports"
    )
    if os.environ.get(REPORTS_VIEW_ENV):
        reports_view_dir = _expanded_path(
            reports_view_raw, relative_to=selected_config.parent
        )
    else:
        reports_view_dir = _bundle_path(
            estate_root, reports_view_raw, field="reports_view"
        )
    uploads_dir = _bundle_path(
        estate_root, config.get("uploads", "user"), field="uploads"
    )
    models = config.get("models", {})
    if not isinstance(models, dict):
        raise EstateBridgeError("models must be a JSON object")
    models_dir = _bundle_path(
        estate_root, models.get("root", "models"), field="models.root"
    )
    alpha_go_embedding_model_path = _bundle_path(
        estate_root,
        models.get(
            "alpha_go_embedding",
            "models/paraphrase-multilingual-MiniLM-L12-v2",
        ),
        field="models.alpha_go_embedding",
    )
    indexes = config.get("indexes", {})
    if not isinstance(indexes, dict):
        raise EstateBridgeError("indexes must be a JSON object")
    alpha_go_index_path = _bundle_path(
        estate_root,
        indexes.get("alpha_go", "indexes/alpha_go.db"),
        field="indexes.alpha_go",
    )
    manifest_path = _bundle_path(
        estate_root, config.get("manifest", "manifest.json"), field="manifest"
    )
    return EstateBridge(
        config_path=selected_config,
        estate_root=estate_root,
        catalog_path=catalog_path,
        objects_dir=objects_dir,
        reports_view_dir=reports_view_dir,
        uploads_dir=uploads_dir,
        models_dir=models_dir,
        alpha_go_embedding_model_path=alpha_go_embedding_model_path,
        alpha_go_index_path=alpha_go_index_path,
        manifest_path=manifest_path,
    )


__all__ = [
    "BRIDGE_ENV",
    "BRIDGE_VERSION",
    "ESTATE_ROOT_ENV",
    "EstateBridge",
    "EstateBridgeError",
    "load_estate_bridge",
]
