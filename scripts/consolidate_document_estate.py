#!/usr/bin/env python3
"""Physically deduplicate exact-hash estate artifacts with reversible hard links.

The default mode is a read-only audit. ``--apply`` creates a content-addressed blob hard link
for each duplicate hash, then atomically replaces redundant paths with links to that inode.
Every mutation is recorded in both the catalog and a fsynced JSONL journal. ``--rollback RUN_ID``
restores independent copies for one completed or partial run.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import sqlite3
import stat
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.shared.document_estate import (  # noqa: E402
    DocumentEstate,
    file_sha256,
    resolve_artifact_path,
)
from src.shared.paths import DOCUMENT_ESTATE_DB, DOCUMENT_ESTATE_DIR  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        # Some filesystems do not support directory fsync; atomic replace still applies.
        pass


def _current_regular(path: Path) -> os.stat_result:
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise RuntimeError(f"not a regular non-symlink file: {path}")
    return value


def _verified(path: Path, expected_sha: str, expected_size: int) -> os.stat_result:
    before = _current_regular(path)
    if before.st_size != expected_size:
        raise RuntimeError(f"size changed for {path}")
    actual_sha = file_sha256(path)
    after = _current_regular(path)
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise RuntimeError(f"file changed while hashing: {path}")
    if actual_sha != expected_sha:
        raise RuntimeError(f"hash changed for {path}")
    return after


def _duplicate_groups(conn: sqlite3.Connection) -> list[list[sqlite3.Row]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT sha256,path,size_bytes,project,format
        FROM artifacts
        WHERE sha256 IN (SELECT sha256 FROM artifact_duplicates)
        ORDER BY sha256,
          CASE project WHEN 'root' THEN 0 WHEN 'soft' THEN 1
                       WHEN 'alpha-go' THEN 2 ELSE 3 END,
          path
    """).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
    for row in rows:
        grouped[row["sha256"]].append(row)
    return list(grouped.values())


def audit(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    groups = _duplicate_groups(conn)
    missing = invalid = cross_device = already_linked = replacements = reclaimable = 0
    eligible_groups = 0
    for group in groups:
        inodes: dict[tuple[int, int], tuple[sqlite3.Row, os.stat_result]] = {}
        devices = set()
        valid = True
        for row in group:
            path = resolve_artifact_path(row["path"], catalog_dir=db_path.parent)
            try:
                value = _current_regular(path)
            except FileNotFoundError:
                missing += 1
                valid = False
                continue
            except RuntimeError:
                invalid += 1
                valid = False
                continue
            if value.st_size != row["size_bytes"]:
                invalid += 1
                valid = False
                continue
            devices.add(value.st_dev)
            inodes.setdefault((value.st_dev, value.st_ino), (row, value))
        already_linked += len(group) - len(inodes)
        if len(devices) > 1:
            cross_device += 1
            valid = False
        if valid and len(inodes) > 1:
            eligible_groups += 1
            unique = list(inodes.values())
            replacements += len(unique) - 1
            reclaimable += sum(
                row["size_bytes"] if value.st_nlink == 1 else 0
                for row, value in unique[1:]
            )
    conn.close()
    return {
        "duplicate_groups": len(groups),
        "eligible_groups": eligible_groups,
        "replacements": replacements,
        "already_linked_paths": already_linked,
        "missing_paths": missing,
        "invalid_paths": invalid,
        "cross_device_groups": cross_device,
        "reclaimable_bytes": reclaimable,
    }


def verify_content_objects(db_path: Path) -> dict:
    """Hash every canonical blob and verify every consolidated path shares its inode."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    objects = conn.execute(
        "SELECT sha256,blob_path,size_bytes FROM content_objects ORDER BY sha256"
    ).fetchall()
    verified_paths = errors = 0
    for item in objects:
        try:
            blob = Path(item["blob_path"])
            blob_stat = _verified(blob, item["sha256"], item["size_bytes"])
            artifacts = conn.execute(
                "SELECT path FROM artifacts WHERE sha256=?", (item["sha256"],)
            ).fetchall()
            for artifact in artifacts:
                value = _current_regular(
                    resolve_artifact_path(artifact["path"], catalog_dir=db_path.parent)
                )
                if (value.st_dev, value.st_ino) != (blob_stat.st_dev, blob_stat.st_ino):
                    raise RuntimeError(
                        f"artifact is not linked to its content object: {artifact['path']}"
                    )
                verified_paths += 1
        except Exception as exc:
            errors += 1
            print(f"VERIFY ERROR: {exc}", file=sys.stderr)
    conn.close()
    return {
        "content_objects": len(objects),
        "verified_artifact_paths": verified_paths,
        "errors": errors,
    }


def _ensure_blob(
    estate: DocumentEstate,
    blob_dir: Path,
    sha256: str,
    source: Path,
    size_bytes: int,
) -> tuple[Path, os.stat_result]:
    blob = blob_dir / sha256[:2] / sha256
    blob.parent.mkdir(parents=True, exist_ok=True)
    if blob.exists():
        value = _verified(blob, sha256, size_bytes)
    else:
        source_stat = _verified(source, sha256, size_bytes)
        temp = blob.with_name(f".{blob.name}.tmp-{uuid.uuid4().hex}")
        os.link(source, temp)
        os.replace(temp, blob)
        _fsync_dir(blob.parent)
        value = _current_regular(blob)
        if (value.st_dev, value.st_ino) != (source_stat.st_dev, source_stat.st_ino):
            raise RuntimeError(f"blob hard-link identity mismatch for {source}")
    estate.conn.execute("""
        INSERT INTO content_objects(sha256,blob_path,size_bytes,st_dev,st_ino,verified_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(sha256) DO UPDATE SET
            blob_path=excluded.blob_path,size_bytes=excluded.size_bytes,
            st_dev=excluded.st_dev,st_ino=excluded.st_ino,verified_at=excluded.verified_at
    """, (sha256, str(blob), size_bytes, value.st_dev, value.st_ino, _now()))
    estate.conn.commit()
    return blob, value


def apply(
    db_path: Path,
    blob_dir: Path,
    *,
    max_replacements: int | None = None,
) -> tuple[str, dict]:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    journal = db_path.parent / "journals" / f"{run_id}.jsonl"
    with DocumentEstate(db_path) as estate:
        groups = _duplicate_groups(estate.conn)
        estate.conn.execute("""
            INSERT INTO consolidation_runs(run_id,started_at,status,journal_path)
            VALUES(?,?,?,?)
        """, (run_id, _now(), "running", str(journal)))
        estate.conn.commit()
        _jsonl(journal, {"event": "run_started", "run_id": run_id, "at": _now()})

        completed = reclaimed = errors = ordinal = 0
        stop = False
        for group in groups:
            if stop:
                break
            sha256 = group[0]["sha256"]
            size_bytes = group[0]["size_bytes"]
            verified: list[tuple[sqlite3.Row, Path, os.stat_result]] = []
            try:
                for row in group:
                    path = resolve_artifact_path(row["path"], catalog_dir=db_path.parent)
                    verified.append((row, path, _verified(path, sha256, size_bytes)))
                devices = {value.st_dev for _row, _path, value in verified}
                if len(devices) != 1:
                    raise RuntimeError(f"cross-device duplicate group {sha256}")
                source = verified[0][1]
                blob, blob_stat = _ensure_blob(
                    estate, blob_dir, sha256, source, size_bytes,
                )
            except Exception as exc:
                errors += 1
                _jsonl(journal, {
                    "event": "group_skipped", "sha256": sha256, "error": str(exc), "at": _now(),
                })
                continue

            seen_inodes = {(blob_stat.st_dev, blob_stat.st_ino)}
            for row, path, old in verified:
                inode = (old.st_dev, old.st_ino)
                if inode in seen_inodes:
                    continue
                seen_inodes.add(inode)
                if max_replacements is not None and completed >= max_replacements:
                    stop = True
                    break
                ordinal += 1
                estate.conn.execute("""
                    INSERT INTO consolidation_actions(
                        run_id,ordinal,sha256,artifact_path,blob_path,size_bytes,
                        old_dev,old_inode,old_nlink,old_mode,old_mtime_ns,status
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """, (run_id, ordinal, sha256, str(path), str(blob), size_bytes,
                      old.st_dev, old.st_ino, old.st_nlink, stat.S_IMODE(old.st_mode),
                      old.st_mtime_ns, "planned"))
                estate.conn.execute(
                    "UPDATE consolidation_runs SET planned_actions=? WHERE run_id=?",
                    (ordinal, run_id),
                )
                estate.conn.commit()
                _jsonl(journal, {
                    "event": "replacement_planned", "run_id": run_id, "ordinal": ordinal,
                    "sha256": sha256, "artifact_path": str(path), "blob_path": str(blob),
                    "old_dev": old.st_dev, "old_inode": old.st_ino,
                    "old_nlink": old.st_nlink, "size_bytes": size_bytes, "at": _now(),
                })

                temp = path.with_name(f".{path.name}.estate-link-{run_id}-{ordinal}")
                try:
                    os.link(blob, temp)
                    linked = _current_regular(temp)
                    if (linked.st_dev, linked.st_ino) != (blob_stat.st_dev, blob_stat.st_ino):
                        raise RuntimeError("temporary hard link does not match blob inode")
                    current = _verified(path, sha256, size_bytes)
                    if (current.st_dev, current.st_ino) != (old.st_dev, old.st_ino):
                        raise RuntimeError("artifact changed after planning")
                    os.replace(temp, path)
                    _fsync_dir(path.parent)
                    final = _current_regular(path)
                    if (final.st_dev, final.st_ino) != (blob_stat.st_dev, blob_stat.st_ino):
                        raise RuntimeError("atomic replacement did not retain blob inode")
                    reclaimed_now = size_bytes if old.st_nlink == 1 else 0
                    completed += 1
                    reclaimed += reclaimed_now
                    estate.conn.execute("""
                        UPDATE consolidation_actions SET status='linked'
                        WHERE run_id=? AND ordinal=?
                    """, (run_id, ordinal))
                    estate.conn.execute("""
                        UPDATE artifacts SET st_dev=?,st_ino=?,mtime_ns=?
                        WHERE path=?
                    """, (final.st_dev, final.st_ino, final.st_mtime_ns, str(path)))
                    estate.conn.execute("""
                        UPDATE consolidation_runs
                        SET completed_actions=?,reclaimed_bytes=? WHERE run_id=?
                    """, (completed, reclaimed, run_id))
                    estate.conn.commit()
                    _jsonl(journal, {
                        "event": "replacement_linked", "run_id": run_id, "ordinal": ordinal,
                        "artifact_path": str(path), "reclaimed_bytes": reclaimed_now, "at": _now(),
                    })
                except Exception as exc:
                    errors += 1
                    try:
                        if temp.exists():
                            temp.unlink()
                    except OSError:
                        pass
                    estate.conn.execute("""
                        UPDATE consolidation_actions SET status='error',error=?
                        WHERE run_id=? AND ordinal=?
                    """, (str(exc), run_id, ordinal))
                    estate.conn.commit()
                    _jsonl(journal, {
                        "event": "replacement_error", "run_id": run_id, "ordinal": ordinal,
                        "artifact_path": str(path), "error": str(exc), "at": _now(),
                    })
                    stop = True
                    break

        status_value = "complete" if errors == 0 else "partial"
        estate.conn.execute("""
            UPDATE consolidation_runs
            SET completed_at=?,status=?,completed_actions=?,reclaimed_bytes=?,error=?
            WHERE run_id=?
        """, (_now(), status_value, completed, reclaimed,
              f"{errors} error(s)" if errors else None, run_id))
        estate.conn.commit()
        summary = {
            "run_id": run_id, "status": status_value, "completed_actions": completed,
            "reclaimed_bytes": reclaimed, "errors": errors, "journal": str(journal),
        }
        _jsonl(journal, {"event": "run_completed", **summary, "at": _now()})
        return run_id, summary


def rollback(db_path: Path, run_id: str) -> dict:
    with DocumentEstate(db_path) as estate:
        run = estate.conn.execute(
            "SELECT * FROM consolidation_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if not run:
            raise RuntimeError(f"unknown consolidation run: {run_id}")
        journal = Path(run["journal_path"])
        rows = estate.conn.execute("""
            SELECT * FROM consolidation_actions
            WHERE run_id=? AND status='linked' ORDER BY ordinal DESC
        """, (run_id,)).fetchall()
        restored = errors = 0
        for row in rows:
            target, blob = Path(row["artifact_path"]), Path(row["blob_path"])
            temp = target.with_name(f".{target.name}.estate-rollback-{run_id}-{row['ordinal']}")
            try:
                target_stat = _verified(target, row["sha256"], row["size_bytes"])
                blob_stat = _verified(blob, row["sha256"], row["size_bytes"])
                if (target_stat.st_dev, target_stat.st_ino) != (blob_stat.st_dev, blob_stat.st_ino):
                    raise RuntimeError("target no longer points to the recorded blob inode")
                shutil.copy2(blob, temp)
                os.chmod(temp, row["old_mode"])
                os.utime(temp, ns=(row["old_mtime_ns"], row["old_mtime_ns"]))
                if file_sha256(temp) != row["sha256"]:
                    raise RuntimeError("rollback copy hash mismatch")
                os.replace(temp, target)
                _fsync_dir(target.parent)
                final = _current_regular(target)
                estate.conn.execute("""
                    UPDATE consolidation_actions SET status='rolled_back',error=NULL
                    WHERE run_id=? AND ordinal=?
                """, (run_id, row["ordinal"]))
                estate.conn.execute("""
                    UPDATE artifacts SET st_dev=?,st_ino=?,mtime_ns=?
                    WHERE path=?
                """, (final.st_dev, final.st_ino, final.st_mtime_ns, str(target)))
                estate.conn.commit()
                restored += 1
                _jsonl(journal, {
                    "event": "replacement_rolled_back", "run_id": run_id,
                    "ordinal": row["ordinal"], "artifact_path": str(target), "at": _now(),
                })
            except Exception as exc:
                errors += 1
                try:
                    if temp.exists():
                        temp.unlink()
                except OSError:
                    pass
                _jsonl(journal, {
                    "event": "rollback_error", "run_id": run_id, "ordinal": row["ordinal"],
                    "artifact_path": str(target), "error": str(exc), "at": _now(),
                })
        estate.conn.execute("""
            UPDATE consolidation_runs SET status=?,error=? WHERE run_id=?
        """, ("rolled_back" if errors == 0 else "rollback_partial",
              f"{errors} rollback error(s)" if errors else None, run_id))
        estate.conn.commit()
        return {"run_id": run_id, "restored": restored, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DOCUMENT_ESTATE_DB)
    parser.add_argument("--blob-dir", type=Path, default=DOCUMENT_ESTATE_DIR / "blobs")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", metavar="RUN_ID")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument(
        "--max-replacements", type=int,
        help="pilot cap; omit to consolidate every eligible redundant inode",
    )
    args = parser.parse_args(argv)
    db_path, blob_dir = args.db.resolve(), args.blob_dir.resolve()
    if args.rollback:
        result = rollback(db_path, args.rollback)
    elif args.verify:
        result = verify_content_objects(db_path)
    elif args.apply:
        _run_id, result = apply(
            db_path, blob_dir, max_replacements=args.max_replacements,
        )
    else:
        result = audit(db_path)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("errors", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
