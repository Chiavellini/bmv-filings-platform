#!/usr/bin/env python3
"""Project parsed estate documents into Alpha Go's current search index.

The estate retains immutable versions. Alpha Go deliberately exposes one stable
``doc_id`` per ``document_family_id`` and replaces that row's chunks, FTS terms,
and vectors when a corrected version becomes current.

This module is both a CLI and an Alpha-owned callable boundary. A root worker can
invoke it narrowly with one or more ``--document-id`` arguments without importing
Alpha's application internals.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict, dataclass
import hashlib
import io
import json
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.corpus.manifest import (  # noqa: E402
    CorpusManifest,
    Document,
    load_manifest,
    manifest_checksum,
    save_manifest,
)
from src.index.build import add_document_to_index  # noqa: E402
from src.index.embeddings import get_embedder  # noqa: E402
from src.index.store import IndexStore  # noqa: E402
from src.shared.paths import DOCUMENT_ESTATE_DB  # noqa: E402


_SINGLE_DOCUMENT_TYPES = frozenset(
    {"quarterly_release", "annual_report", "regulatory_filing"}
)
_PROJECT_PRIORITY = {
    "alpha-go": 0,
    "earnings": 1,
    "root": 2,
    "soft": 3,
    "soft-xbrl-backfill": 4,
    "alpha-go-news": 5,
}


@dataclass(frozen=True)
class EstateSearchRow:
    document_id: str
    document_family_id: str
    version: int
    supersedes_document_id: str | None
    company: str
    period: str | None
    doc_type: str
    title: str
    language: str | None
    source_url: str | None
    project: str
    role: str
    markdown_path: str
    sha256: str


@dataclass(frozen=True)
class SyncResult:
    eligible: int
    changed: int
    unchanged: int
    indexed: int
    removed: int
    manifest_changed: bool


class ProjectionResolutionError(RuntimeError):
    """An explicitly requested estate event cannot be projected."""


def _estate_artifact_path(estate_path: Path, raw: str | Path) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else estate_path.parent / path


@contextmanager
def _projection_lock(path: Path):
    """Serialize the manifest/index read-modify-write across processes."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _acquisition_metadata(raw: str | None) -> dict:
    try:
        metadata = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    acquisition = metadata.get("acquisition")
    return acquisition if isinstance(acquisition, dict) else {}


def _version_maps(
    conn: sqlite3.Connection,
) -> tuple[dict[str, tuple[str, int, str | None]], dict[str, str]]:
    """Return estate-version metadata and the current document for each family."""

    by_document: dict[str, tuple[str, int, str | None]] = {}
    current: dict[str, tuple[int, str]] = {}
    if _table_exists(conn, "source_record_versions"):
        rows = conn.execute(
            """SELECT document_id,document_family_id,version,supersedes_document_id
               FROM source_record_versions"""
        ).fetchall()
        for row in rows:
            family = row["document_family_id"]
            version = int(row["version"])
            document_id = row["document_id"]
            by_document[document_id] = (
                family,
                version,
                row["supersedes_document_id"],
            )
            previous = current.get(family)
            if previous is None or (version, document_id) > previous:
                current[family] = (version, document_id)

    # Acquisition metadata is a second, read-only source of the same contract. It
    # also makes the bridge tolerant of a catalog copied before the acquisition
    # migration tables were included.
    for row in conn.execute("SELECT document_id,metadata_json FROM documents"):
        if row["document_id"] in by_document:
            continue
        metadata = _acquisition_metadata(row["metadata_json"])
        family = metadata.get("document_family_id")
        if not family:
            continue
        try:
            version = int(metadata.get("version") or 1)
        except (TypeError, ValueError):
            version = 1
        document_id = row["document_id"]
        by_document[document_id] = (
            str(family),
            version,
            metadata.get("supersedes_document_id"),
        )
        previous = current.get(str(family))
        if previous is None or (version, document_id) > previous:
            current[str(family)] = (version, document_id)
    return by_document, {family: value[1] for family, value in current.items()}


def _narrow_version_maps(
    conn: sqlite3.Connection,
    document_ids: Iterable[str],
) -> tuple[dict[str, tuple[str, int, str | None]], dict[str, str]]:
    """Resolve only requested families on event-facing projections."""
    requested = tuple(
        dict.fromkeys(str(document_id) for document_id in document_ids)
    )
    if not requested or not _table_exists(conn, "source_record_versions"):
        return _version_maps(conn)
    placeholders = ",".join("?" for _ in requested)
    requested_rows = conn.execute(
        f"""SELECT document_id,document_family_id,version,
                   supersedes_document_id
            FROM source_record_versions
            WHERE document_id IN ({placeholders})""",
        requested,
    ).fetchall()
    families = {str(row["document_family_id"]) for row in requested_rows}

    missing = set(requested) - {
        str(row["document_id"]) for row in requested_rows
    }
    if missing:
        missing_placeholders = ",".join("?" for _ in missing)
        for row in conn.execute(
            f"""SELECT document_id,metadata_json FROM documents
                WHERE document_id IN ({missing_placeholders})""",
            tuple(sorted(missing)),
        ):
            metadata = _acquisition_metadata(row["metadata_json"])
            family = metadata.get("document_family_id")
            if family:
                families.add(str(family))

    family_rows: list[sqlite3.Row] = []
    if families:
        family_placeholders = ",".join("?" for _ in families)
        family_rows = conn.execute(
            f"""SELECT document_id,document_family_id,version,
                       supersedes_document_id
                FROM source_record_versions
                WHERE document_family_id IN ({family_placeholders})""",
            tuple(sorted(families)),
        ).fetchall()
    by_document: dict[str, tuple[str, int, str | None]] = {}
    current: dict[str, tuple[int, str]] = {}
    for row in family_rows:
        family = str(row["document_family_id"])
        document_id = str(row["document_id"])
        version = int(row["version"])
        by_document[document_id] = (
            family,
            version,
            row["supersedes_document_id"],
        )
        previous = current.get(family)
        if previous is None or (version, document_id) > previous:
            current[family] = (version, document_id)

    # Non-versioned requested documents remain valid singleton families.
    for document_id in missing:
        by_document.setdefault(document_id, (document_id, 1, None))
        current.setdefault(document_id, (1, document_id))
    return by_document, {
        family: value[1] for family, value in current.items()
    }


def _family_map(estate_path: Path, document_ids: Iterable[str]) -> dict[str, str]:
    requested = {str(document_id) for document_id in document_ids if document_id}
    if not requested:
        return {}
    conn = sqlite3.connect(f"file:{estate_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        version_by_document, _current = _narrow_version_maps(conn, requested)
        return {
            document_id: version_by_document.get(
                document_id, (document_id, 1, None)
            )[0]
            for document_id in requested
        }
    finally:
        conn.close()


def _rows(
    estate_path: Path,
    document_ids: Iterable[str] = (),
) -> list[EstateSearchRow]:
    """Select one existing Markdown artifact for each current estate family.

    When a historical document ID is requested, the current version of its
    family wins. This prevents an out-of-order retry from rolling Alpha's search
    projection back to stale text.
    """

    conn = sqlite3.connect(f"file:{estate_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        requested = {str(document_id) for document_id in document_ids if document_id}
        version_by_document, current_by_family = (
            _narrow_version_maps(conn, requested)
            if requested
            else _version_maps(conn)
        )
        if requested:
            placeholders = ",".join("?" for _ in requested)
            all_document_ids = {
                row["document_id"]
                for row in conn.execute(
                    f"""SELECT document_id FROM documents
                        WHERE document_id IN ({placeholders})""",
                    tuple(sorted(requested)),
                )
            }
            candidate_ids: set[str] = set()
            for document_id in requested & all_document_ids:
                family = version_by_document.get(
                    document_id, (document_id, 1, None)
                )[0]
                candidate_ids.add(current_by_family.get(family, document_id))
        else:
            all_document_ids = {
                row["document_id"]
                for row in conn.execute("SELECT document_id FROM documents")
            }
            versioned = set(version_by_document)
            candidate_ids = set(current_by_family.values())
            candidate_ids.update(all_document_ids - versioned)

        if not candidate_ids:
            return []
        candidate_clause = ""
        candidate_params: tuple[str, ...] = ()
        if requested:
            placeholders = ",".join("?" for _ in candidate_ids)
            candidate_clause = f" AND d.document_id IN ({placeholders})"
            candidate_params = tuple(sorted(candidate_ids))
        artifact_rows = conn.execute(
            f"""SELECT d.document_id,d.company,d.period,d.doc_type,d.title,d.language,
                       d.source_url,a.project,a.role,a.path AS markdown_path,a.sha256
                FROM documents d JOIN artifacts a ON a.document_id=d.document_id
                WHERE a.format IN ('md','markdown')
                  {candidate_clause}
                ORDER BY d.document_id,
                         CASE a.role WHEN 'search_text' THEN 0 ELSE 1 END,
                         CASE a.project
                             WHEN 'root' THEN 0
                             WHEN 'soft' THEN 1
                             WHEN 'earnings' THEN 2
                             WHEN 'soft-xbrl-backfill' THEN 3
                             ELSE 4
                         END,
                         a.path""",
            candidate_params,
        ).fetchall()
        selected: dict[str, EstateSearchRow] = {}
        for row in artifact_rows:
            document_id = row["document_id"]
            if document_id not in candidate_ids or document_id in selected:
                continue
            markdown_path = _estate_artifact_path(
                estate_path,
                row["markdown_path"],
            )
            if not markdown_path.is_file():
                continue
            family, version, supersedes = version_by_document.get(
                document_id, (document_id, 1, None)
            )
            selected[document_id] = EstateSearchRow(
                document_id=document_id,
                document_family_id=family,
                version=version,
                supersedes_document_id=supersedes,
                company=row["company"],
                period=row["period"],
                doc_type=row["doc_type"],
                title=row["title"],
                language=row["language"],
                source_url=row["source_url"],
                project=row["project"],
                role=row["role"],
                markdown_path=str(markdown_path),
                sha256=row["sha256"],
            )
        # The catalog intentionally preserves every project record, including
        # independently parsed copies of the same quarterly release. Search must
        # not count those parser copies as separate documents. For document
        # classes that have one canonical filing per company/period, keep one
        # non-empty representation and prefer Alpha's original-backed copy.
        # News and relevant events are deliberately excluded: several genuinely
        # distinct articles/events can share a publication date or no period.
        canonical: dict[tuple[str, str, str], EstateSearchRow] = {}
        passthrough: list[EstateSearchRow] = []

        def quality(row: EstateSearchRow) -> tuple[int, int, int, str]:
            path = Path(row.markdown_path)
            try:
                empty = path.stat().st_size == 0
            except OSError:
                empty = True
            return (
                int(empty),
                _PROJECT_PRIORITY.get(row.project, 99),
                0 if row.role == "search_text" else 1,
                row.markdown_path,
            )

        for row in selected.values():
            if row.doc_type in _SINGLE_DOCUMENT_TYPES and row.period:
                key = (row.company, row.period, row.doc_type)
                previous = canonical.get(key)
                if previous is None or quality(row) < quality(previous):
                    canonical[key] = row
            else:
                passthrough.append(row)

        return sorted(
            [*canonical.values(), *passthrough],
            key=lambda row: (
                row.company,
                row.period or "",
                row.document_family_id,
            ),
        )
    finally:
        conn.close()


def _memberships(estate_path: Path, estate_document_id: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{estate_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute(
        """SELECT company,industry FROM memberships
           WHERE document_id=? ORDER BY company""",
        (estate_document_id,),
    )]
    conn.close()
    return rows


def _projection_metadata(
    estate_path: Path,
    document_ids: Iterable[str],
) -> tuple[dict[str, list[dict]], dict[str, tuple[Path, str]]]:
    """Load memberships and original-source paths in two bounded queries.

    A fleet reconciliation projects thousands of rows. Opening SQLite twice per
    document made a no-op audit take minutes; this map keeps the same selection
    contract while making the scan proportional to catalog size.
    """
    requested = {str(document_id) for document_id in document_ids}
    memberships: dict[str, list[dict]] = {}
    originals: dict[str, tuple[Path, str]] = {}
    if not requested:
        return memberships, originals

    conn = sqlite3.connect(f"file:{estate_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        for row in conn.execute(
            """SELECT document_id,company,industry FROM memberships
               ORDER BY document_id,company"""
        ):
            document_id = str(row["document_id"])
            if document_id in requested:
                memberships.setdefault(document_id, []).append(
                    {"company": row["company"], "industry": row["industry"]}
                )
        for row in conn.execute(
            """SELECT document_id,path,format FROM artifacts
               WHERE format IN ('pdf','html','htm')
                  OR (role='original' AND format IN ('md','markdown','txt','text'))
               ORDER BY document_id,
                        CASE role WHEN 'original' THEN 0 ELSE 1 END,
                        CASE format WHEN 'pdf' THEN 0 ELSE 1 END,path"""
        ):
            document_id = str(row["document_id"])
            if document_id not in requested or document_id in originals:
                continue
            path = _estate_artifact_path(estate_path, row["path"])
            if path.is_file():
                originals[document_id] = (path, str(row["format"]))
    finally:
        conn.close()
    return memberships, originals


def _sibling_pdf(markdown_path: Path) -> Path | None:
    candidate = markdown_path.with_suffix(".pdf")
    return candidate if candidate.exists() else None


def _original_artifact(
    estate_path: Path, estate_document_id: str
) -> tuple[Path | None, str | None]:
    conn = sqlite3.connect(f"file:{estate_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT path,format FROM artifacts
        WHERE document_id=?
          AND (format IN ('pdf','html','htm')
               OR (role='original' AND format IN ('md','markdown','txt','text')))
        ORDER BY CASE role WHEN 'original' THEN 0 ELSE 1 END,
                 CASE format WHEN 'pdf' THEN 0 ELSE 1 END,path LIMIT 1
    """, (estate_document_id,)).fetchone()
    conn.close()
    if not row:
        return None, None
    path = _estate_artifact_path(estate_path, row["path"])
    if not path.is_file():
        return None, None
    return path, row["format"]


def _stable_search_doc_id(document_family_id: str, estate_document_id: str) -> str:
    # Preserve Alpha's established public IDs while moving their provenance to
    # the estate. This keeps saved document selections and deep links stable.
    if estate_document_id.startswith("alpha-go:"):
        return estate_document_id.removeprefix("alpha-go:")
    stable_key = document_family_id or estate_document_id
    digest = hashlib.sha256(stable_key.encode("utf-8")).hexdigest()
    return f"estate/family/{digest}"


def _manifest_family(
    document: Document,
    family_by_estate_document: dict[str, str],
) -> str | None:
    if not document.extra.get("shared_estate"):
        return None
    family = document.extra.get("estate_document_family_id")
    if family:
        return str(family)
    estate_document_id = document.extra.get("estate_document_id")
    if not estate_document_id:
        return None
    return family_by_estate_document.get(
        str(estate_document_id), str(estate_document_id)
    )


def _document_from_row(
    estate: Path,
    row: EstateSearchRow,
    *,
    memberships: list[dict] | None = None,
    original: tuple[Path | None, str | None] | None = None,
) -> Document:
    markdown = Path(row.markdown_path)
    if original is None:
        original_path, original_format = _original_artifact(
            estate, row.document_id
        )
    else:
        original_path, original_format = original
    pdf = (
        original_path
        if original_format == "pdf"
        else _sibling_pdf(markdown)
    )
    if pdf and not original_path:
        original_path, original_format = pdf, "pdf"
    if memberships is None:
        memberships = _memberships(estate, row.document_id)
    is_news_link = row.doc_type == "news_article" and bool(row.source_url)
    source_path = row.source_url if is_news_link else str(original_path or markdown)
    source_format = "news" if is_news_link else (original_format or "markdown")
    return Document(
        doc_id=_stable_search_doc_id(
            row.document_family_id, row.document_id
        ),
        company=row.company,
        period=row.period,
        doc_type=row.doc_type,
        title=row.title,
        source_url=row.source_url,
        pdf_path=str(pdf) if pdf else None,
        markdown_path=str(markdown),
        language=row.language or "unknown",
        industry=(memberships[0].get("industry") if memberships else None),
        memberships=memberships
        or [{"company": row.company, "industry": None}],
        extra={
            "shared_estate": True,
            "estate_document_id": row.document_id,
            "estate_document_family_id": row.document_family_id,
            "estate_document_version": row.version,
            "estate_supersedes_document_id": row.supersedes_document_id,
            "origin_project": row.project,
            "origin_role": row.role,
        },
        source_path=source_path,
        source_format=source_format,
        original_path=str(original_path) if original_path else None,
        original_format=original_format,
        content_sha256=row.sha256,
    )


def _load_runtime(
    config_path: Path,
    db_path: Path,
) -> tuple[dict, object, str, int]:
    """Load and validate the requested runtime without opening the index writable."""

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    embedder, model_name = get_embedder(config)
    runtime_dim = int(embedder.dim)
    if runtime_dim <= 0:
        raise RuntimeError(f"embedding runtime returned invalid dimension {runtime_dim}")

    if not db_path.exists():
        return config, embedder, model_name, runtime_dim

    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "meta"):
            raise RuntimeError(
                f"Alpha Go index has no metadata table: {db_path}"
            )
        metadata = {
            row["key"]: row["value"]
            for row in conn.execute(
                "SELECT key,value FROM meta WHERE key IN "
                "('embedding_model','embedding_dim')"
            )
        }
        indexed_model = metadata.get("embedding_model")
        if indexed_model and indexed_model != model_name:
            raise RuntimeError(
                "embedding model mismatch: "
                f"index={indexed_model!r}, runtime={model_name!r}"
            )
        indexed_dim = metadata.get("embedding_dim")
        if indexed_dim and int(indexed_dim) not in (0, runtime_dim):
            raise RuntimeError(
                "embedding dimension mismatch: "
                f"index={indexed_dim}, runtime={runtime_dim}"
            )
        if _table_exists(conn, "embeddings"):
            stored_dims = {
                int(row["dim"])
                for row in conn.execute("SELECT DISTINCT dim FROM embeddings")
            }
            if stored_dims and stored_dims != {runtime_dim}:
                raise RuntimeError(
                    "stored embedding dimension mismatch: "
                    f"index={sorted(stored_dims)}, runtime={runtime_dim}"
                )
    finally:
        conn.close()
    return config, embedder, model_name, runtime_dim


def _delete_index_documents(store: IndexStore, document_ids: Iterable[str]) -> int:
    ids = sorted({str(document_id) for document_id in document_ids if document_id})
    if not ids:
        return 0
    conn = store.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        removed = 0
        for document_id in ids:
            chunk_ids = [
                row["chunk_id"]
                for row in conn.execute(
                    "SELECT chunk_id FROM chunks WHERE doc_id=?", (document_id,)
                )
            ]
            for chunk_id in chunk_ids:
                conn.execute(
                    "DELETE FROM embeddings WHERE chunk_id=?", (chunk_id,)
                )
                conn.execute(
                    "DELETE FROM chunks_fts WHERE chunk_id=?", (chunk_id,)
                )
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (document_id,))
            conn.execute(
                "DELETE FROM document_companies WHERE doc_id=?", (document_id,)
            )
            removed += conn.execute(
                "DELETE FROM documents WHERE doc_id=?", (document_id,)
            ).rowcount
        store.set_meta("documents", str(store.count("documents")))
        store.set_meta("chunks", str(store.count("chunks")))
        conn.commit()
        return removed
    except Exception:
        conn.rollback()
        raise


def _index_write_needed(
    db_path: Path,
    rows: Iterable[EstateSearchRow],
    documents: dict[str, Document],
    replacements: dict[str, set[str]],
    removals: set[str],
    *,
    manifest_changed: bool,
) -> bool:
    """Read-only no-op check so duplicate events do not even rewrite index metadata."""

    if not db_path.exists():
        return bool(list(rows))
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "documents"):
            return True
        for row in rows:
            document_id = _stable_search_doc_id(
                row.document_family_id, row.document_id
            )
            existing = conn.execute(
                "SELECT content_sha256 FROM documents WHERE doc_id=?",
                (document_id,),
            ).fetchone()
            if (
                existing is None
                or existing["content_sha256"]
                != documents[document_id].content_sha256
            ):
                return True
            for obsolete_id in replacements.get(document_id, ()):
                if conn.execute(
                    "SELECT 1 FROM documents WHERE doc_id=?", (obsolete_id,)
                ).fetchone():
                    return True
        for removed_id in removals:
            if conn.execute(
                "SELECT 1 FROM documents WHERE doc_id=?", (removed_id,)
            ).fetchone():
                return True
        # A real manifest transition should advance the index's provenance even
        # when its searchable row was already repaired by an earlier attempt.
        return manifest_changed
    finally:
        conn.close()


def _sync_shared_estate_locked(
    *,
    estate: Path = DOCUMENT_ESTATE_DB,
    corpus: Path = ROOT / "data" / "corpus",
    db: Path | None = None,
    config: Path = ROOT / "configs" / "alpha_go.yaml",
    apply: bool = False,
    document_ids: Iterable[str] = (),
) -> SyncResult:
    """Synchronize current estate families into Alpha's manifest and optional index.

    ``document_ids`` is an event-facing filter. It may contain either a current
    or superseded immutable estate ID; the current version of each selected
    family is projected. The callable performs no writes unless ``apply`` is
    true.
    """

    estate = Path(estate).resolve()
    corpus = Path(corpus)
    db = Path(db) if db is not None else None
    config = Path(config)
    requested_ids = tuple(dict.fromkeys(
        str(document_id) for document_id in document_ids if document_id
    ))
    manifest = load_manifest(corpus)
    rows = _rows(estate, requested_ids)
    memberships_by_document, originals_by_document = _projection_metadata(
        estate, (row.document_id for row in rows)
    )
    requested_families = _family_map(estate, requested_ids)
    resolved_families = {row.document_family_id for row in rows}
    unresolved_ids = [
        document_id
        for document_id in requested_ids
        if requested_families.get(document_id, document_id)
        not in resolved_families
    ]
    if unresolved_ids:
        raise ProjectionResolutionError(
            "explicitly requested estate documents did not resolve to a "
            "current Markdown search artifact: "
            + ", ".join(unresolved_ids[:20])
        )

    estate_ids = {
        str(document.extra.get("estate_document_id"))
        for document in manifest.documents
        if document.extra.get("shared_estate")
        and document.extra.get("estate_document_id")
    }
    estate_ids.update(row.document_id for row in rows)
    family_by_estate_document = _family_map(estate, estate_ids)

    by_id = {document.doc_id: document for document in manifest.documents}
    removed_doc_ids: set[str] = set()
    target_replacements: dict[str, set[str]] = {}

    # A broad/manual reconciliation makes the shared portion authoritative:
    # remove stale parser copies and old projection IDs that are no longer one
    # of the canonical estate rows. A narrow event remains confined to the
    # selected family so concurrent incremental updates cannot delete peers.
    if not requested_ids:
        selected_doc_ids = {
            _stable_search_doc_id(row.document_family_id, row.document_id)
            for row in rows
        }
        for document in list(by_id.values()):
            if (
                document.extra.get("shared_estate")
                and (
                    document.doc_id not in selected_doc_ids
                    or not Path(document.markdown_path).is_file()
                )
            ):
                by_id.pop(document.doc_id, None)
                removed_doc_ids.add(document.doc_id)

    changed_documents: list[Document] = []
    for row in rows:
        current = _document_from_row(
            estate,
            row,
            memberships=memberships_by_document.get(row.document_id, []),
            original=originals_by_document.get(
                row.document_id, (None, None)
            ),
        )
        family = row.document_family_id
        obsolete = {
            document.doc_id
            for document in by_id.values()
            if document.doc_id != current.doc_id
            and _manifest_family(document, family_by_estate_document) == family
        }
        for obsolete_id in obsolete:
            by_id.pop(obsolete_id, None)
        removed_doc_ids.update(obsolete)
        target_replacements[current.doc_id] = obsolete

        previous = by_id.get(current.doc_id)
        if previous != current or obsolete:
            changed_documents.append(current)
        by_id[current.doc_id] = current

    projected_manifest = CorpusManifest(list(by_id.values()))
    manifest_changed = (
        manifest_checksum(projected_manifest) != manifest_checksum(manifest)
    )
    changed_ids = {document.doc_id for document in changed_documents}
    unchanged = sum(
        1
        for row in rows
        if _stable_search_doc_id(row.document_family_id, row.document_id)
        not in changed_ids
    )

    print(f"Shared-estate current-family candidates: {len(rows)}")
    if requested_ids:
        print(
            f"Event document filter: requested={len(requested_ids)} "
            f"resolved={len(rows)}"
        )
    if not apply:
        for row in rows[:20]:
            print(
                row.project,
                row.company,
                row.period,
                f"v{row.version}",
                row.markdown_path,
            )
        return SyncResult(
            eligible=len(rows),
            changed=len(changed_documents),
            unchanged=unchanged,
            indexed=0,
            removed=len(removed_doc_ids),
            manifest_changed=manifest_changed,
        )

    runtime = None
    db_path = None
    index_write_needed = False
    if db is not None:
        db_path = db if db.is_absolute() else ROOT / db
        config_path = config if config.is_absolute() else ROOT / config
        # This read-only guard happens before the manifest or index is touched.
        runtime = _load_runtime(config_path, db_path)
        index_write_needed = _index_write_needed(
            db_path,
            rows,
            by_id,
            target_replacements,
            removed_doc_ids,
            manifest_changed=manifest_changed,
        )

    indexed = 0
    removed_from_index = 0
    store: IndexStore | None = None
    if db_path is not None and runtime is not None and index_write_needed:
        runtime_config, embedder, model_name, runtime_dim = runtime
        store = IndexStore(db_path)
        store.connect()
        store.migrate()
        try:
            consumed_obsolete: set[str] = set()
            for row in rows:
                document_id = _stable_search_doc_id(
                    row.document_family_id, row.document_id
                )
                document = by_id[document_id]
                obsolete = target_replacements.get(document_id, set())
                existing = store.connect().execute(
                    "SELECT content_sha256 FROM documents WHERE doc_id=?",
                    (document_id,),
                ).fetchone()
                obsolete_present = {
                    obsolete_id
                    for obsolete_id in obsolete
                    if store.connect().execute(
                        "SELECT 1 FROM documents WHERE doc_id=?",
                        (obsolete_id,),
                    ).fetchone()
                }
                if (
                    existing is not None
                    and existing["content_sha256"] == document.content_sha256
                    and not obsolete_present
                    and document_id not in changed_ids
                ):
                    continue
                result = add_document_to_index(
                    store,
                    document,
                    Path(document.markdown_path).read_text(encoding="utf-8"),
                    runtime_config,
                    embedder=embedder,
                    replace_doc_ids=obsolete_present,
                )
                if result["embedding_dim"] not in (0, runtime_dim):
                    raise RuntimeError(
                        "embedder returned an unexpected dimension: "
                        f"{result['embedding_dim']} != {runtime_dim}"
                    )
                consumed_obsolete.update(obsolete_present)
                indexed += 1
            removed_from_index = _delete_index_documents(
                store, removed_doc_ids - consumed_obsolete
            )
            store.set_meta("embedding_model", model_name)
            store.set_meta("embedding_dim", str(runtime_dim))
            store.commit()
        except Exception:
            store.close()
            raise

    # Index replacement completes first. A failed embedding therefore leaves the
    # old manifest and old searchable version together.
    if manifest_changed:
        save_manifest(projected_manifest, corpus)
    if store is not None:
        store.set_meta("manifest_checksum", manifest_checksum(projected_manifest))
        store.commit()
        store.close()

    print(
        f"Manifest changed={manifest_changed}; indexed={indexed}; "
        f"removed_index_rows={removed_from_index}; total={len(by_id)}"
    )
    return SyncResult(
        eligible=len(rows),
        changed=len(changed_documents),
        unchanged=unchanged,
        indexed=indexed,
        removed=len(removed_doc_ids),
        manifest_changed=manifest_changed,
    )


def sync_shared_estate(
    *,
    estate: Path = DOCUMENT_ESTATE_DB,
    corpus: Path = ROOT / "data" / "corpus",
    db: Path | None = None,
    config: Path = ROOT / "configs" / "alpha_go.yaml",
    apply: bool = False,
    document_ids: Iterable[str] = (),
) -> SyncResult:
    """Synchronize with one process-wide target lock for write invocations."""
    arguments = {
        "estate": estate,
        "corpus": corpus,
        "db": db,
        "config": config,
        "apply": apply,
        "document_ids": document_ids,
    }
    if not apply:
        return _sync_shared_estate_locked(**arguments)
    if db is not None:
        target = Path(db)
        if not target.is_absolute():
            target = ROOT / target
        lock_path = target.parent / f".{target.name}.projection.lock"
    else:
        target_corpus = Path(corpus).resolve()
        lock_path = target_corpus.parent / (
            f".{target_corpus.name}.projection.lock"
        )
    with _projection_lock(lock_path):
        return _sync_shared_estate_locked(**arguments)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--estate", type=Path, default=DOCUMENT_ESTATE_DB)
    parser.add_argument("--corpus", type=Path, default=ROOT / "data" / "corpus")
    parser.add_argument("--db", type=Path, help="incrementally update this Alpha Go index")
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "configs" / "alpha_go.yaml",
        help="runtime/index config (the certified default is never rewritten)",
    )
    parser.add_argument(
        "--document-id",
        action="append",
        default=[],
        help="estate document event to project; repeat for a bounded batch",
    )
    parser.add_argument("--apply", action="store_true", help="write manifest and optional index")
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit one machine-readable result object",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.json:
        diagnostics = io.StringIO()
        try:
            with redirect_stdout(diagnostics):
                result = sync_shared_estate(
                    estate=args.estate,
                    corpus=args.corpus,
                    db=args.db,
                    config=args.config,
                    apply=args.apply,
                    document_ids=args.document_id,
                )
        except ProjectionResolutionError as exc:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "unresolved",
                        "error": str(exc),
                        "diagnostics": diagnostics.getvalue(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 3
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "succeeded",
                    "result": asdict(result),
                    "diagnostics": diagnostics.getvalue(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    sync_shared_estate(
        estate=args.estate,
        corpus=args.corpus,
        db=args.db,
        config=args.config,
        apply=args.apply,
        document_ids=args.document_id,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
