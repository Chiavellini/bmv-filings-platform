"""Portable document-estate export and read-only verification primitives."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
from typing import Any

from estate_volume import read_estate_id


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def portable_relative(raw: str) -> bool:
    path = PurePosixPath(raw)
    return bool(raw) and not path.is_absolute() and all(
        part not in ("", ".", "..") for part in path.parts
    )


ALPHA_INDEX_REQUIRED_TABLES = (
    "documents",
    "chunks",
    "chunks_fts",
    "embeddings",
    "meta",
)


def inspect_alpha_index(
    path: str | Path,
    *,
    expected_documents: int | None = None,
    expected_dim: int | None = None,
) -> list[str]:
    """Return why an Alpha Go index is not production-complete; empty means healthy.

    Existence is not completeness. A half-built index is a readable SQLite file
    with the right schema, so a release gate that stops at ``is_file()`` promotes
    it happily — which is how a 10-document index reached the production path in
    front of a 3,695-document projection. Everything here is read-only.
    """
    index = Path(path).expanduser()
    if not index.is_file():
        return [f"Alpha Go index does not exist: {index}"]
    try:
        connection = sqlite3.connect(f"file:{index.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return [f"Alpha Go index cannot be opened: {index}: {exc}"]
    problems: list[str] = []
    try:
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            present = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        except sqlite3.DatabaseError as exc:
            return [f"Alpha Go index is not a readable database: {index}: {exc}"]
        if integrity != "ok":
            problems.append(f"Alpha Go index quick_check returned {integrity!r}")
        missing = [t for t in ALPHA_INDEX_REQUIRED_TABLES if t not in present]
        if missing:
            problems.append(
                "Alpha Go index is missing tables: " + ", ".join(missing)
            )
            return problems
        counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("documents", "chunks", "chunks_fts", "embeddings")
        }
        metadata = dict(connection.execute("SELECT key, value FROM meta"))

        if counts["documents"] == 0:
            problems.append(f"Alpha Go index has no documents: {index}")
        if len({counts["chunks"], counts["chunks_fts"], counts["embeddings"]}) != 1:
            problems.append(f"Alpha Go index row counts disagree: {counts}")

        model = (metadata.get("embedding_model") or "").strip()
        if not model:
            problems.append(
                "Alpha Go index records no embedding_model; its vector space is unidentified"
            )
        elif model == "hashing":
            problems.append(
                "Alpha Go index was built with the lexical hashing fallback, not a "
                "semantic model; it is not search-ready"
            )
        raw_dim = (metadata.get("embedding_dim") or "").strip()
        if not raw_dim:
            problems.append("Alpha Go index records no embedding_dim")
        else:
            try:
                declared = int(raw_dim)
            except ValueError:
                problems.append(f"Alpha Go index embedding_dim is not an integer: {raw_dim!r}")
            else:
                divergent = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM embeddings WHERE dim != ?", (declared,)
                    ).fetchone()[0]
                )
                if divergent:
                    problems.append(
                        f"Alpha Go index has {divergent} embedding(s) whose dim "
                        f"differs from the declared {declared}"
                    )
                if expected_dim is not None and declared != int(expected_dim):
                    problems.append(
                        f"Alpha Go index embedding_dim={declared} but the runtime "
                        f"model produces {expected_dim}"
                    )
        if expected_documents is not None and counts["documents"] != int(
            expected_documents
        ):
            problems.append(
                f"Alpha Go index covers {counts['documents']} of "
                f"{expected_documents} projected documents"
            )
    finally:
        connection.close()
    return problems


def _catalog_connection(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True
        )
    else:
        connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def verify_portable_estate(
    estate_root: str | Path,
    *,
    expected_estate_id: str | None = None,
    full_hashes: bool = False,
    require_alpha_index: bool = False,
) -> dict[str, Any]:
    """Verify a bundle without writing to it."""
    root = Path(estate_root).expanduser().resolve()
    failures: list[str] = []

    def require(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    try:
        estate_id = read_estate_id(root)
    except RuntimeError as exc:
        estate_id = None
        failures.append(str(exc))
    if expected_estate_id:
        require(
            estate_id == expected_estate_id.strip().upper(),
            f"estate ID mismatch: expected {expected_estate_id}, observed {estate_id}",
        )

    manifest_path = root / "manifest.json"
    catalog_path = root / "catalog.db"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        manifest = {}
        failures.append(f"manifest is unreadable: {exc}")

    catalog_sha = file_sha256(catalog_path) if catalog_path.is_file() else None
    require(catalog_sha is not None, f"catalog is missing: {catalog_path}")
    require(manifest.get("estate_id") == estate_id, "manifest estate ID mismatch")
    require(
        manifest.get("catalog", {}).get("sha256") == catalog_sha,
        "manifest catalog SHA-256 mismatch",
    )
    require(
        portable_relative(str(manifest.get("catalog", {}).get("path", ""))),
        "manifest catalog path is not portable",
    )

    quick_check = None
    foreign_key_violations: list[sqlite3.Row] = []
    counts: dict[str, int] = {}
    object_rows: list[sqlite3.Row] = []
    artifact_rows: list[sqlite3.Row] = []
    if catalog_path.is_file():
        try:
            connection = _catalog_connection(catalog_path, read_only=True)
            try:
                quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
                foreign_key_violations = list(
                    connection.execute("PRAGMA foreign_key_check")
                )
                counts = {
                    table: int(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
                    for table in ("documents", "artifacts", "content_objects")
                }
                object_rows = list(
                    connection.execute(
                        "SELECT sha256,size_bytes,object_key FROM content_objects ORDER BY sha256"
                    )
                )
                artifact_rows = list(
                    connection.execute(
                        "SELECT path,sha256,size_bytes FROM artifacts ORDER BY path"
                    )
                )
            finally:
                connection.close()
        except sqlite3.Error as exc:
            failures.append(f"catalog verification failed: {exc}")
    require(quick_check == "ok", f"SQLite quick_check returned {quick_check!r}")
    require(not foreign_key_violations, "SQLite foreign-key violations found")

    for key, value in counts.items():
        manifest_key = "content_objects" if key == "content_objects" else key
        require(
            manifest.get("counts", {}).get(manifest_key) == value,
            f"manifest {manifest_key} count mismatch",
        )

    missing_objects = 0
    bad_object_paths = 0
    bad_object_sizes = 0
    bad_object_hashes = 0
    object_stats: dict[str, os.stat_result] = {}
    expected_keys: set[str] = set()
    unique_bytes = 0
    for row in object_rows:
        sha = str(row["sha256"])
        size = int(row["size_bytes"])
        key = str(row["object_key"] or "")
        unique_bytes += size
        expected_key = f"blobs/{sha[:2]}/{sha}"
        if not portable_relative(key) or key != expected_key:
            bad_object_paths += 1
            continue
        expected_keys.add(key)
        path = root / key
        try:
            stat = path.stat()
        except FileNotFoundError:
            missing_objects += 1
            continue
        object_stats[sha] = stat
        if stat.st_size != size:
            bad_object_sizes += 1
        if full_hashes and file_sha256(path) != sha:
            bad_object_hashes += 1

    blob_keys = {
        path.relative_to(root).as_posix()
        for path in (root / "blobs").glob("*/*")
        if path.is_file()
    }
    require(blob_keys == expected_keys, "blob tree and catalog object sets differ")
    require(missing_objects == 0, f"missing objects: {missing_objects}")
    require(bad_object_paths == 0, f"invalid object keys: {bad_object_paths}")
    require(bad_object_sizes == 0, f"object size mismatches: {bad_object_sizes}")
    require(bad_object_hashes == 0, f"object hash mismatches: {bad_object_hashes}")

    missing_artifacts = 0
    bad_artifact_paths = 0
    bad_artifact_sizes = 0
    broken_hard_links = 0
    for row in artifact_rows:
        raw = str(row["path"])
        if not portable_relative(raw):
            bad_artifact_paths += 1
            continue
        path = root / raw
        try:
            stat = path.stat()
        except FileNotFoundError:
            missing_artifacts += 1
            continue
        if stat.st_size != int(row["size_bytes"]):
            bad_artifact_sizes += 1
        object_stat = object_stats.get(str(row["sha256"]))
        if object_stat is None or (stat.st_dev, stat.st_ino) != (
            object_stat.st_dev,
            object_stat.st_ino,
        ):
            broken_hard_links += 1
    require(missing_artifacts == 0, f"missing artifacts: {missing_artifacts}")
    require(bad_artifact_paths == 0, f"invalid artifact paths: {bad_artifact_paths}")
    require(bad_artifact_sizes == 0, f"artifact size mismatches: {bad_artifact_sizes}")
    require(broken_hard_links == 0, f"broken artifact hard links: {broken_hard_links}")

    alpha_index = root / "indexes" / "alpha_go.db"
    alpha_quick_check = None
    alpha_documents = None
    if require_alpha_index:
        require(alpha_index.is_file(), f"Alpha Go index is missing: {alpha_index}")
        if alpha_index.is_file():
            try:
                connection = _catalog_connection(alpha_index, read_only=True)
                try:
                    alpha_quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
                    alpha_documents = int(
                        connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
                    )
                finally:
                    connection.close()
            except sqlite3.Error as exc:
                failures.append(f"Alpha Go index verification failed: {exc}")
            require(alpha_quick_check == "ok", "Alpha Go index quick_check failed")
            require(bool(alpha_documents), "Alpha Go index contains no documents")

    return {
        "verified": not failures,
        "estate_root": str(root),
        "estate_id": estate_id,
        "catalog_sha256": catalog_sha,
        "sqlite_quick_check": quick_check,
        "foreign_key_violations": len(foreign_key_violations),
        "counts": counts,
        "unique_object_bytes": unique_bytes,
        "full_hashes": full_hashes,
        "missing_objects": missing_objects,
        "bad_object_hashes": bad_object_hashes,
        "hard_linked_artifacts": len(artifact_rows) - broken_hard_links,
        "alpha_index_quick_check": alpha_quick_check,
        "alpha_index_documents": alpha_documents,
        "failures": failures,
    }


def _source_path(source_root: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (source_root / path).resolve()


def _export_artifact_path(
    source_root: Path,
    row: sqlite3.Row,
) -> str:
    raw = str(row["path"])
    if portable_relative(raw):
        return PurePosixPath(raw).as_posix()
    source = Path(raw).expanduser().resolve()
    try:
        relative = source.relative_to(source_root).as_posix()
    except ValueError:
        suffix = source.suffix.lower()
        relative = (
            f"views/legacy/{row['project']}/{row['artifact_id']}{suffix}"
        )
    if not portable_relative(relative):
        raise ValueError(f"could not make artifact path portable: {raw}")
    return relative


def export_portable_estate(
    source_root: str | Path,
    destination: str | Path,
    *,
    estate_id: str,
    include_runtime: bool = True,
) -> dict[str, Any]:
    """Export to a sibling temporary directory, verify, then rename atomically."""
    source = Path(source_root).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"refusing to overwrite destination: {target}")
    temporary = target.with_name(f".{target.name}.copying-{estate_id[:8]}")
    if temporary.exists():
        raise FileExistsError(f"refusing to overwrite temporary export: {temporary}")
    catalog_source = source / "catalog.db"
    if not catalog_source.is_file():
        raise FileNotFoundError(f"source catalog is missing: {catalog_source}")

    temporary.mkdir(parents=True)
    for relative in ("blobs", "views", "indexes", "journals", "projections"):
        (temporary / relative).mkdir()

    catalog_target = temporary / "catalog.db"
    source_connection = _catalog_connection(catalog_source, read_only=True)
    target_connection = sqlite3.connect(catalog_target)
    try:
        source_connection.backup(target_connection)
    finally:
        source_connection.close()
        target_connection.close()

    connection = _catalog_connection(catalog_target, read_only=False)
    try:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("source catalog snapshot failed quick_check")
        if list(connection.execute("PRAGMA foreign_key_check")):
            raise RuntimeError("source catalog snapshot has foreign-key violations")
        artifact_rows = list(
            connection.execute(
                "SELECT artifact_id,project,path,sha256,size_bytes FROM artifacts ORDER BY path"
            )
        )
        object_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(content_objects)")
        }
        if "object_key" not in object_columns:
            connection.execute("ALTER TABLE content_objects ADD COLUMN object_key TEXT")
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_content_objects_object_key
                   ON content_objects(object_key) WHERE object_key IS NOT NULL"""
            )
        object_rows = list(
            connection.execute(
                "SELECT sha256,size_bytes,blob_path,object_key FROM content_objects ORDER BY sha256"
            )
        )
        if not object_rows:
            first_by_sha: dict[str, sqlite3.Row] = {}
            for row in artifact_rows:
                first_by_sha.setdefault(str(row["sha256"]), row)
            object_rows = [
                {
                    "sha256": sha,
                    "size_bytes": int(row["size_bytes"]),
                    "blob_path": str(row["path"]),
                    "object_key": None,
                }
                for sha, row in sorted(first_by_sha.items())
            ]

        object_stats: dict[str, os.stat_result] = {}
        object_entries: list[dict[str, Any]] = []
        artifact_sources: dict[str, Path] = {}
        for row in artifact_rows:
            artifact_sources.setdefault(
                str(row["sha256"]), _source_path(source, str(row["path"]))
            )
        for row in object_rows:
            sha = str(row["sha256"])
            key = f"blobs/{sha[:2]}/{sha}"
            candidates: list[Path] = []
            raw_key = row["object_key"]
            if raw_key and portable_relative(str(raw_key)):
                candidates.append(source / str(raw_key))
            raw_blob = row["blob_path"]
            if raw_blob:
                candidates.append(_source_path(source, str(raw_blob)))
            if sha in artifact_sources:
                candidates.append(artifact_sources[sha])
            source_object = next((path for path in candidates if path.is_file()), None)
            if source_object is None:
                raise FileNotFoundError(f"source object is missing: {sha}")
            destination_object = temporary / key
            destination_object.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_object, destination_object)
            if destination_object.stat().st_size != int(row["size_bytes"]):
                raise RuntimeError(f"object size changed during export: {sha}")
            if file_sha256(destination_object) != sha:
                raise RuntimeError(f"object hash changed during export: {sha}")
            object_stats[sha] = destination_object.stat()
            object_entries.append(
                {"sha256": sha, "size_bytes": int(row["size_bytes"]), "object_key": key}
            )

        connection.execute("DELETE FROM content_objects")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for entry in object_entries:
            stat = object_stats[entry["sha256"]]
            connection.execute(
                """INSERT INTO content_objects(
                       sha256,blob_path,size_bytes,st_dev,st_ino,verified_at,object_key
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    entry["sha256"],
                    entry["object_key"],
                    entry["size_bytes"],
                    stat.st_dev,
                    stat.st_ino,
                    now,
                    entry["object_key"],
                ),
            )

        used_paths: set[str] = set()
        for row in artifact_rows:
            relative = _export_artifact_path(source, row)
            if relative in used_paths:
                raise RuntimeError(f"artifact export path collision: {relative}")
            used_paths.add(relative)
            sha = str(row["sha256"])
            object_path = temporary / f"blobs/{sha[:2]}/{sha}"
            artifact_path = temporary / relative
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            if artifact_path != object_path:
                os.link(object_path, artifact_path)
            stat = artifact_path.stat()
            connection.execute(
                "UPDATE artifacts SET path=?,st_dev=?,st_ino=? WHERE artifact_id=?",
                (relative, stat.st_dev, stat.st_ino, row["artifact_id"]),
            )
        connection.execute("DELETE FROM hash_cache")
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='consolidation_actions'"
        ).fetchone():
            connection.execute("DELETE FROM consolidation_actions")
            connection.execute("DELETE FROM consolidation_runs")
        connection.commit()
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("VACUUM")
    finally:
        connection.close()

    if include_runtime:
        for relative in ("indexes", "models", "projections"):
            source_path = source / relative
            if source_path.is_dir():
                shutil.copytree(
                    source_path,
                    temporary / relative,
                    dirs_exist_ok=True,
                    copy_function=shutil.copy2,
                )

    counts: dict[str, int] = {}
    connection = _catalog_connection(catalog_target, read_only=True)
    try:
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("documents", "artifacts", "content_objects")
        }
    finally:
        connection.close()
    catalog_sha = file_sha256(catalog_target)
    exported_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (temporary / ".bmv-estate-volume.json").write_text(
        json.dumps(
            {"created_at": exported_at, "estate_id": estate_id, "schema_version": 1},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (temporary / "manifest.json").write_text(
        json.dumps(
            {
                "estate_id": estate_id,
                "exported_at": exported_at,
                "catalog": {
                    "path": "catalog.db",
                    "sha256": catalog_sha,
                    "size_bytes": catalog_target.stat().st_size,
                },
                "counts": {
                    **counts,
                    "view_links": counts["artifacts"],
                },
                "objects": object_entries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    verification = verify_portable_estate(
        temporary,
        expected_estate_id=estate_id,
        full_hashes=True,
    )
    if not verification["verified"]:
        raise RuntimeError("portable export verification failed: " + "; ".join(verification["failures"]))
    os.replace(temporary, target)
    return {**verification, "estate_root": str(target)}


__all__ = [
    "export_portable_estate",
    "file_sha256",
    "portable_relative",
    "verify_portable_estate",
]
