"""Scheduler-independent orchestration for keeping the quarterly estate current.

Cron, launchd, GitHub Actions, or a long-running worker may invoke this service;
none of those concerns live here.  Planning is read-only.  Synchronization uses
only the root acquisition ledger and estate writer, never an application's
corpus or search index.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import calendar
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from src.acquisition.adapters import (
    BmvXbrlAdapter,
    IRFetchIssue,
    InvestorRelationsAdapter,
    WaybackBackfillAdapter,
    normalize_period,
)
from src.acquisition.bmv_issuer import BmvIssuerPdfAdapter
from src.acquisition.models import (
    AcquisitionSource,
    FetchedArtifact,
    IssuerSpec,
    SourceRecord,
)
from src.acquisition.registry import IssuerRegistry
from src.acquisition.ledger import AcquisitionLeaseLost
from src.acquisition.writer import ArtifactValidationError
from src.shared.company_aliases import (
    expand_company_aliases,
    load_company_aliases,
)
from src.shared.report_index import infer_period_label, period_sort_key


PDF_DOCUMENT_TYPE = "quarterly_release"
XBRL_DOCUMENT_TYPE = "regulatory_filing"
PDF_SOURCE_KINDS = frozenset({"investor_relations", "bmv_issuer_pdf"})
SUPPORTED_SOURCE_KINDS = PDF_SOURCE_KINDS | frozenset({"bmv_xbrl"})


class AcquisitionLedgerProtocol(Protocol):
    """Durable run/item operations required by the orchestration layer."""

    def start_run(
        self,
        *,
        trigger: str = "manual",
        scope: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> str: ...

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "succeeded",
        error: str | None = None,
    ) -> None: ...

    def discover(self, source_record: SourceRecord, *, run_id: str | None = None) -> Any: ...

    def renew_lease(
        self,
        run_id: str,
        *,
        owner: str | None = None,
        lease_ttl_seconds: int | None = None,
    ) -> str: ...

    def mark_retryable(
        self,
        source_record: SourceRecord,
        *,
        run_id: str,
        error: str,
        attempt_id: str | None = None,
    ) -> Any: ...

    def mark_rejected(
        self,
        source_record: SourceRecord,
        *,
        run_id: str,
        reason: str,
        attempt_id: str | None = None,
    ) -> Any: ...


class EstateWriterProtocol(Protocol):
    """The sole durable artifact write boundary."""

    def store_fetched(
        self,
        fetched_artifact: FetchedArtifact,
        *,
        run_id: str | None = None,
        memberships: Iterable[Mapping[str, Any]] = (),
    ) -> Any: ...


class CoverageProtocol(Protocol):
    def known_periods(self, issuer_slug: str, document_type: str) -> set[str]: ...


@dataclass(frozen=True, slots=True)
class SourcePlan:
    issuer_slug: str
    source_key: str
    source_kind: str
    document_type: str
    known_periods: tuple[str, ...]
    missing_periods: tuple[str, ...]
    recheck_periods: tuple[str, ...]
    readiness: str = "unverified"

    @property
    def desired_periods(self) -> set[str]:
        return set(self.missing_periods) | set(self.recheck_periods)


@dataclass(frozen=True, slots=True)
class IssuerPlan:
    issuer: IssuerSpec
    sources: tuple[SourcePlan, ...]
    gaps: tuple[str, ...] = ()
    primary_pdf_missing_periods: tuple[str, ...] = ()

    def source(self, key: str) -> SourcePlan:
        for source in self.sources:
            if source.source_key == key:
                return source
        raise KeyError(f"{self.issuer.slug}: no plan for source {key!r}")


@dataclass(frozen=True, slots=True)
class SyncFailure:
    issuer_slug: str
    source_key: str
    source_record_id: str
    error: str


@dataclass(frozen=True, slots=True)
class CoverageGap:
    issuer_slug: str
    source_key: str
    document_type: str
    reason: str
    periods: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceDiagnostic:
    """Structured per-layer evidence attached to an applied run."""

    issuer_slug: str
    source_key: str
    layer: str
    candidates: int = 0
    selected: int = 0
    note: str = ""


@dataclass(frozen=True, slots=True)
class QuarterlySyncFreshness:
    """Fail-closed receipt for one issuer's most recent successful sync."""

    issuer_slug: str
    fresh: bool
    reason: str
    max_age_seconds: float
    run_id: str | None = None
    completed_at: str | None = None
    age_seconds: float | None = None
    detail: str | None = None
    input_document_ids: tuple[str, ...] = ()
    input_artifact_ids: tuple[str, ...] = ()
    catalog_document_ids: tuple[str, ...] = ()
    run_document_ids: tuple[str, ...] = ()
    input_watermark: str | None = None


@dataclass(frozen=True, slots=True)
class RunReport:
    run_id: str | None
    mode: str
    plans: tuple[IssuerPlan, ...]
    discovered: int = 0
    stored: int = 0
    unchanged: int = 0
    failures: tuple[SyncFailure, ...] = ()
    coverage_gaps: tuple[CoverageGap, ...] = ()
    diagnostics: tuple[SourceDiagnostic, ...] = ()
    coverage_strict: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures and not (
            self.coverage_strict and self.coverage_gaps
        )

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1


def check_quarterly_sync_freshness(
    db_path: str | Path,
    issuer_slug: str,
    max_age: timedelta | int | float,
    *,
    now: datetime | None = None,
) -> QuarterlySyncFreshness:
    """Read a successful recurring-sync receipt without mutating the catalog.

    Only a completed, strict-coverage ``quarterly_acquisition`` run whose JSON
    scope explicitly includes ``issuer_slug`` and is not a Wayback backfill can
    prove freshness.
    Missing databases/tables/receipts, malformed timestamps, future timestamps,
    and stale receipts all return ``fresh=False`` with a stable reason code.
    The SQLite connection uses ``mode=ro`` so this check cannot create or migrate
    a catalog as a side effect of onboarding.
    """

    slug = str(issuer_slug).strip()
    if not slug:
        raise ValueError("issuer_slug must be non-empty")
    if isinstance(max_age, timedelta):
        max_age_seconds = max_age.total_seconds()
    elif isinstance(max_age, bool):
        raise TypeError("max_age must be a timedelta or number of seconds")
    else:
        max_age_seconds = float(max_age)
    if not math.isfinite(max_age_seconds) or max_age_seconds < 0:
        raise ValueError("max_age must be a finite non-negative duration")

    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    checked_at = checked_at.astimezone(timezone.utc)
    database = Path(db_path).expanduser().resolve()

    def receipt(reason: str, **values: Any) -> QuarterlySyncFreshness:
        return QuarterlySyncFreshness(
            issuer_slug=slug,
            fresh=reason == "fresh",
            reason=reason,
            max_age_seconds=max_age_seconds,
            **values,
        )

    if not database.is_file():
        return receipt(
            "catalog_missing",
            detail=f"catalog does not exist: {database}",
        )

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        table = connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='acquisition_runs'"""
        ).fetchone()
        if table is None:
            return receipt(
                "acquisition_runs_table_missing",
                detail="catalog has no acquisition_runs table",
            )
        rows = connection.execute(
            """SELECT run_id,scope_json,status,completed_at
               FROM acquisition_runs
               ORDER BY started_at DESC,rowid DESC"""
        ).fetchall()
    except sqlite3.Error as exc:
        return receipt(
            "catalog_unreadable",
            detail=f"{type(exc).__name__}: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()

    matching: sqlite3.Row | None = None
    for row in rows:
        try:
            scope = json.loads(str(row["scope_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(scope, dict):
            continue
        issuers = scope.get("issuers")
        if (
            scope.get("module") == "quarterly_acquisition"
            and isinstance(issuers, list)
            and slug in issuers
            and scope.get("strict_coverage") is True
            and not bool(scope.get("wayback_backfill", False))
        ):
            matching = row
            break
    if matching is None:
        return receipt(
            "no_succeeded_sync_receipt",
            detail=(
                "no succeeded quarterly_acquisition sync scope includes "
                f"{slug}"
            ),
        )

    status = str(matching["status"])
    if status != "succeeded":
        completed_text = matching["completed_at"]
        return receipt(
            "latest_sync_not_succeeded",
            run_id=str(matching["run_id"]),
            completed_at=(
                str(completed_text) if completed_text is not None else None
            ),
            detail=(
                "latest matching strict quarterly_acquisition run has status "
                f"{status!r}; an older success cannot prove current freshness"
            ),
        )

    completed_text = matching["completed_at"]
    try:
        completed = datetime.fromisoformat(str(completed_text))
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        completed = completed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return receipt(
            "invalid_receipt_timestamp",
            run_id=str(matching["run_id"]),
            completed_at=(
                str(completed_text) if completed_text is not None else None
            ),
            detail="successful sync receipt has an invalid completed_at",
        )

    age_seconds = (checked_at - completed).total_seconds()
    common = {
        "run_id": str(matching["run_id"]),
        "completed_at": completed.isoformat(timespec="seconds"),
        "age_seconds": age_seconds,
    }
    if age_seconds < 0:
        return receipt(
            "receipt_in_future",
            detail="successful sync receipt is later than the check time",
            **common,
        )
    if age_seconds > max_age_seconds:
        return receipt(
            "stale_receipt",
            detail=(
                f"successful sync receipt age {age_seconds:.0f}s exceeds "
                f"maximum {max_age_seconds:.0f}s"
            ),
            **common,
        )
    return receipt("fresh", **common)


def check_quarterly_publication_freshness(
    db_path: str | Path,
    issuer_slug: str,
    max_age: timedelta | int | float,
    input_paths: Iterable[str | Path] | str | Path,
    *,
    estate_root: str | Path | None = None,
    now: datetime | None = None,
) -> QuarterlySyncFreshness:
    """Bind a fresh sync receipt to the exact files used for publication.

    ``check_quarterly_sync_freshness`` proves only that discovery completed. A
    publishable extraction additionally needs to prove that every *effective*
    input is a hash-verified catalog artifact for this issuer, belongs to the
    current acquisition document version, and that the newest selected period
    for each document type was actually observed by that successful run.

    Callers must pass files after their directory/period precedence has been
    resolved, not every immutable historical file present in a view.  Local or
    copied caches deliberately fail: matching bytes are not durable lineage.
    The returned watermark is a deterministic digest of the run, document ids,
    artifact ids, and catalog hashes and can be recorded in build provenance.
    This function opens SQLite read-only and never repairs paths or schema.
    """

    base = check_quarterly_sync_freshness(
        db_path,
        issuer_slug,
        max_age,
        now=now,
    )
    if not base.fresh:
        return base

    if isinstance(input_paths, (str, Path)):
        raw_paths: tuple[str | Path, ...] = (input_paths,)
    else:
        raw_paths = tuple(input_paths)
    paths = tuple(
        dict.fromkeys(Path(value).expanduser().absolute() for value in raw_paths)
    )
    if not paths:
        return replace(
            base,
            fresh=False,
            reason="publication_inputs_missing",
            detail="no effective extraction input files were supplied",
        )

    database = Path(db_path).expanduser().resolve()
    root = (
        Path(estate_root).expanduser().absolute()
        if estate_root is not None
        else database.parent
    )
    required_tables = {
        "documents",
        "artifacts",
        "source_records",
        "source_record_versions",
        "acquisition_attempts",
    }
    slug = str(issuer_slug).strip()

    def failed(reason: str, detail: str, **values: Any) -> QuarterlySyncFreshness:
        return replace(base, fresh=False, reason=reason, detail=detail, **values)

    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"{database.as_uri()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        missing_tables = required_tables - tables
        if missing_tables:
            return failed(
                "publication_lineage_schema_missing",
                "catalog lacks publication-lineage tables: "
                + ", ".join(sorted(missing_tables)),
            )
        has_memberships = "memberships" in tables

        bound: list[dict[str, str]] = []
        for path in paths:
            if not path.is_file():
                return failed(
                    "publication_input_missing",
                    f"effective extraction input is not a file: {path}",
                )
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            input_sha256 = digest.hexdigest()

            path_keys = [str(path)]
            try:
                relative = path.relative_to(root)
            except ValueError:
                pass
            else:
                path_keys.append(relative.as_posix())
            placeholders = ",".join("?" for _ in path_keys)
            artifact = connection.execute(
                f"""SELECT a.artifact_id,a.document_id,a.path,a.sha256,
                            d.company,d.period,d.doc_type
                     FROM artifacts a
                     JOIN documents d ON d.document_id=a.document_id
                     WHERE a.path IN ({placeholders})
                     ORDER BY CASE WHEN a.path=? THEN 0 ELSE 1 END
                     LIMIT 1""",
                (*path_keys, path_keys[0]),
            ).fetchone()
            if artifact is None:
                # Acquisition compatibility views are materialized as hardlinks,
                # copies, or symlinks and therefore need not have their own row in
                # ``artifacts``.  Hash-only lookup is safe exclusively inside the
                # estate-owned view namespace and only when issuer, period,
                # document type, and *current* acquisition lineage resolve to one
                # document. An identical copy elsewhere remains untrusted.
                views_root = root / "views"
                try:
                    view_relative = path.relative_to(views_root)
                except ValueError:
                    return failed(
                        "publication_input_not_cataloged",
                        "effective extraction input has no exact catalog artifact "
                        f"lineage and is outside the trusted estate view: {path}",
                    )
                if len(view_relative.parts) < 3 or view_relative.parts[1] != slug:
                    return failed(
                        "publication_view_scope_mismatch",
                        f"estate view input is outside views/<namespace>/{slug}: {path}",
                    )
                namespace = view_relative.parts[0]
                compatible_types = {
                    "reports": ("quarterly_release", "annual_report"),
                    "parsed": ("quarterly_release", "annual_report"),
                    "regulatory": ("regulatory_filing",),
                }.get(namespace)
                if compatible_types is None:
                    return failed(
                        "publication_view_namespace_untrusted",
                        f"unsupported estate compatibility-view namespace: {namespace}",
                    )
                inferred_period = infer_period_label(path.stem)
                if inferred_period is None:
                    return failed(
                        "publication_view_period_unresolved",
                        f"cannot infer a reporting period from estate view input: {path}",
                    )
                type_placeholders = ",".join("?" for _ in compatible_types)
                issuer_predicate = "d.company=?"
                issuer_values: tuple[str, ...] = (slug,)
                if has_memberships:
                    issuer_predicate = (
                        "(d.company=? OR EXISTS (SELECT 1 FROM memberships m "
                        "WHERE m.document_id=d.document_id AND m.company=?))"
                    )
                    issuer_values = (slug, slug)
                candidates = connection.execute(
                    f"""SELECT DISTINCT a.artifact_id,a.document_id,a.path,
                                a.sha256,d.company,d.period,d.doc_type
                         FROM artifacts a
                         JOIN documents d ON d.document_id=a.document_id
                         JOIN source_record_versions srv
                           ON srv.document_id=d.document_id
                         JOIN source_records sr
                           ON sr.source_key=srv.source_key
                          AND sr.source_record_id=srv.source_record_id
                         WHERE a.sha256=?
                           AND d.period=?
                           AND d.doc_type IN ({type_placeholders})
                           AND sr.current_document_id=d.document_id
                           AND {issuer_predicate}
                         ORDER BY a.artifact_id""",
                    (
                        input_sha256,
                        inferred_period,
                        *compatible_types,
                        *issuer_values,
                    ),
                ).fetchall()
                candidate_documents = {
                    str(row["document_id"]) for row in candidates
                }
                if not candidates:
                    return failed(
                        "publication_view_lineage_missing",
                        "estate view input hash has no compatible current catalog "
                        f"lineage for {slug} {inferred_period}: {path}",
                    )
                if len(candidate_documents) != 1:
                    return failed(
                        "publication_view_lineage_ambiguous",
                        "estate view input hash resolves to multiple current catalog "
                        "documents: " + ", ".join(sorted(candidate_documents)),
                    )
                artifact = candidates[0]

            if input_sha256 != str(artifact["sha256"]):
                return failed(
                    "publication_input_hash_mismatch",
                    f"effective extraction input differs from its catalog hash: {path}",
                )

            document_id = str(artifact["document_id"])
            issuer_match = str(artifact["company"]) == slug
            if not issuer_match and has_memberships:
                issuer_match = connection.execute(
                    """SELECT 1 FROM memberships
                       WHERE document_id=? AND company=? LIMIT 1""",
                    (document_id, slug),
                ).fetchone() is not None
            if not issuer_match:
                return failed(
                    "publication_input_issuer_mismatch",
                    f"catalog artifact {artifact['artifact_id']} is not owned by {slug}",
                )

            lineage = connection.execute(
                """SELECT sr.current_document_id
                   FROM source_record_versions srv
                   JOIN source_records sr
                     ON sr.source_key=srv.source_key
                    AND sr.source_record_id=srv.source_record_id
                   WHERE srv.document_id=?""",
                (document_id,),
            ).fetchall()
            if not lineage:
                return failed(
                    "publication_input_without_acquisition_lineage",
                    f"catalog document {document_id} was not created by acquisition",
                )
            if not any(str(row["current_document_id"]) == document_id for row in lineage):
                return failed(
                    "publication_input_superseded",
                    f"catalog document {document_id} is not the current source version",
                )

            observed = connection.execute(
                """SELECT 1 FROM acquisition_attempts
                   WHERE run_id=? AND document_id=? AND status='stored'
                   LIMIT 1""",
                (base.run_id, document_id),
            ).fetchone() is not None
            bound.append(
                {
                    "artifact_id": str(artifact["artifact_id"]),
                    "document_id": document_id,
                    "sha256": str(artifact["sha256"]),
                    "period": str(artifact["period"] or ""),
                    "doc_type": str(artifact["doc_type"]),
                    "observed": "1" if observed else "0",
                }
            )

        selected_doc_types = sorted({row["doc_type"] for row in bound})
        type_placeholders = ",".join("?" for _ in selected_doc_types)
        issuer_predicate = "d.company=?"
        if has_memberships:
            issuer_predicate = (
                "(d.company=? OR EXISTS (SELECT 1 FROM memberships m "
                "WHERE m.document_id=d.document_id AND m.company=?))"
            )
        issuer_values: tuple[str, ...] = (slug, slug) if has_memberships else (slug,)
        current_catalog = connection.execute(
            f"""SELECT DISTINCT d.document_id,d.period,d.doc_type
                 FROM documents d
                 JOIN source_record_versions srv
                   ON srv.document_id=d.document_id
                 JOIN source_records sr
                   ON sr.source_key=srv.source_key
                  AND sr.source_record_id=srv.source_record_id
                 WHERE sr.current_document_id=d.document_id
                   AND {issuer_predicate}
                   AND d.doc_type IN ({type_placeholders})""",
            (*issuer_values, *selected_doc_types),
        ).fetchall()

        catalog_documents: set[str] = set()
        for doc_type in selected_doc_types:
            typed_catalog = [
                row for row in current_catalog
                if str(row["doc_type"]) == doc_type and row["period"]
            ]
            if not typed_catalog:
                return failed(
                    "publication_catalog_watermark_missing",
                    f"catalog has no current {doc_type} period for {slug}",
                )
            catalog_period = max(
                (str(row["period"]) for row in typed_catalog),
                key=period_sort_key,
            )
            selected_period = max(
                (row["period"] for row in bound if row["doc_type"] == doc_type),
                key=period_sort_key,
            )
            catalog_documents.update(
                str(row["document_id"])
                for row in typed_catalog
                if str(row["period"]) == catalog_period
            )
            if selected_period != catalog_period:
                return failed(
                    "publication_view_behind_catalog",
                    f"effective {doc_type} inputs stop at {selected_period}, but "
                    f"the current catalog watermark is {catalog_period}",
                    input_document_ids=tuple(
                        sorted({row["document_id"] for row in bound})
                    ),
                    input_artifact_ids=tuple(
                        sorted({row["artifact_id"] for row in bound})
                    ),
                    catalog_document_ids=tuple(sorted(catalog_documents)),
                )

        newest_documents: set[str] = set()
        for doc_type in selected_doc_types:
            typed = [row for row in bound if row["doc_type"] == doc_type]
            if any(not row["period"] for row in typed):
                return failed(
                    "publication_input_period_missing",
                    f"{doc_type} publication input lacks a catalog period",
                )
            newest_period = max(
                (row["period"] for row in typed),
                key=period_sort_key,
            )
            newest = [row for row in typed if row["period"] == newest_period]
            unobserved = sorted(
                {row["document_id"] for row in newest if row["observed"] != "1"}
            )
            if unobserved:
                return failed(
                    "publication_watermark_not_observed",
                    "successful sync did not observe the newest selected "
                    f"{doc_type} document(s) for {newest_period}: "
                    + ", ".join(unobserved),
                    input_document_ids=tuple(
                        sorted({row["document_id"] for row in bound})
                    ),
                    input_artifact_ids=tuple(
                        sorted({row["artifact_id"] for row in bound})
                    ),
                    catalog_document_ids=tuple(sorted(catalog_documents)),
                )
            newest_documents.update(row["document_id"] for row in newest)

        watermark_lines = [str(base.run_id)] + [
            f"catalog\0{document_id}"
            for document_id in sorted(catalog_documents)
        ] + [
            "\0".join(
                (row["document_id"], row["artifact_id"], row["sha256"])
            )
            for row in sorted(
                bound,
                key=lambda item: (
                    item["document_id"], item["artifact_id"], item["sha256"]
                ),
            )
        ]
        watermark = hashlib.sha256("\n".join(watermark_lines).encode()).hexdigest()
        return replace(
            base,
            input_document_ids=tuple(
                sorted({row["document_id"] for row in bound})
            ),
            input_artifact_ids=tuple(
                sorted({row["artifact_id"] for row in bound})
            ),
            catalog_document_ids=tuple(sorted(catalog_documents)),
            run_document_ids=tuple(sorted(newest_documents)),
            input_watermark=watermark,
        )
    except (OSError, sqlite3.Error) as exc:
        return failed(
            "publication_lineage_unreadable",
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        if connection is not None:
            connection.close()


class EmptyCoverage:
    """Useful for isolated tests; production CLI always uses EstateCoverage."""

    def known_periods(self, issuer_slug: str, document_type: str) -> set[str]:
        return set()


class EstateCoverage:
    """Read current coverage from the root estate without mutating it.

    The aliases keep the migration compatible with the existing estate naming:
    old ``quarterly_release`` rows satisfy PDF coverage, while old
    ``regulatory_filing`` rows satisfy only XBRL/regulatory coverage.
    """

    _TYPE_ALIASES = {
        PDF_DOCUMENT_TYPE: (PDF_DOCUMENT_TYPE, "quarterly_report"),
        XBRL_DOCUMENT_TYPE: (XBRL_DOCUMENT_TYPE, "quarterly_xbrl"),
    }

    def __init__(
        self,
        db_path: str | Path,
        *,
        estate_root: str | Path | None = None,
        object_exists: Callable[[str], bool] | None = None,
    ):
        self.db_path = Path(db_path)
        self.estate_root = (
            Path(estate_root).expanduser().resolve()
            if estate_root is not None
            else None
        )
        self.object_exists = object_exists

    def _local_path_exists(
        self,
        value: object,
        *,
        require_contained: bool = False,
    ) -> bool:
        if not value:
            return False
        path = Path(str(value))
        if path.is_absolute():
            if require_contained:
                return False
            return path.is_file()
        if self.estate_root is None:
            return False
        root = self.estate_root.resolve()
        try:
            candidate = (root / path).resolve()
            candidate.relative_to(root)
        except (OSError, ValueError):
            return False
        return candidate.is_file()

    @staticmethod
    def _valid_object_key(value: object) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        path = PurePosixPath(value)
        return (
            not path.is_absolute()
            and ".." not in path.parts
            and bool(path.parts)
            and path.parts[0] == "blobs"
        )

    def known_periods(self, issuer_slug: str, document_type: str) -> set[str]:
        if not self.db_path.exists():
            return set()
        types = self._TYPE_ALIASES.get(document_type, (document_type,))
        placeholders = ",".join("?" for _ in types)
        if document_type == PDF_DOCUMENT_TYPE:
            artifact_clause = "a.role='original' AND lower(a.format)='pdf'"
        elif document_type == XBRL_DOCUMENT_TYPE:
            artifact_clause = """
                lower(a.format) IN ('json', 'gz')
                AND (
                    a.role IN ('raw_xbrl', 'original')
                    OR (
                        a.role='derived'
                        AND lower(a.path) NOT LIKE '%_facts.json'
                    )
                )
            """
        else:
            artifact_clause = "1=1"
        connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            content_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(content_objects)")
            }
            object_key_sql = (
                "co.object_key AS object_key"
                if "object_key" in content_columns
                else "NULL AS object_key"
            )
            rows = connection.execute(
                f"""
                SELECT d.period,a.path,a.sha256,co.blob_path,{object_key_sql}
                FROM documents d
                JOIN artifacts a ON a.document_id=d.document_id
                LEFT JOIN content_objects co ON co.sha256=a.sha256
                WHERE (
                        lower(d.company)=lower(?)
                        OR EXISTS (
                            SELECT 1 FROM memberships m
                            WHERE m.document_id=d.document_id
                              AND lower(m.company)=lower(?)
                        )
                      )
                  AND d.doc_type IN ({placeholders})
                  AND d.period IS NOT NULL
                  AND d.period <> ''
                  AND ({artifact_clause})
                """,
                (issuer_slug, issuer_slug, *types),
            ).fetchall()
        finally:
            connection.close()
        periods: set[str] = set()
        for row in rows:
            period = normalize_period(str(row["period"]))
            if period is None:
                continue
            object_key = row["object_key"]
            valid_object_key = self._valid_object_key(object_key)
            if self._local_path_exists(row["path"]):
                periods.add(period)
            elif self._local_path_exists(row["blob_path"]):
                periods.add(period)
            elif (
                valid_object_key
                and self.object_exists is not None
                and self.object_exists(str(object_key))
            ):
                periods.add(period)
            elif valid_object_key and self._local_path_exists(
                object_key,
                require_contained=True,
            ):
                periods.add(period)
        return periods


class QuarterlyAcquisitionService:
    """Plan or execute one bounded refresh over a registry selection."""

    def __init__(
        self,
        registry: IssuerRegistry,
        *,
        coverage: CoverageProtocol,
        ledger: AcquisitionLedgerProtocol | None = None,
        writer: EstateWriterProtocol | None = None,
        bmv: BmvXbrlAdapter | None = None,
        bmv_issuer: BmvIssuerPdfAdapter | None = None,
        ir: InvestorRelationsAdapter | None = None,
        wayback: WaybackBackfillAdapter | None = None,
        company_aliases: Mapping[str, Sequence[str]] | None = None,
        default_floor_year: int = 2016,
        default_xbrl_floor_year: int = 2021,
        recheck_periods: int = 2,
    ):
        if recheck_periods < 0:
            raise ValueError("recheck_periods cannot be negative")
        self.registry = registry
        self.coverage = coverage
        self.ledger = ledger
        self.writer = writer
        self.bmv = bmv or BmvXbrlAdapter()
        self.bmv_issuer = bmv_issuer or BmvIssuerPdfAdapter()
        self.ir = ir or InvestorRelationsAdapter()
        self.wayback = wayback or WaybackBackfillAdapter()
        self.company_aliases = (
            dict(company_aliases)
            if company_aliases is not None
            else load_company_aliases()
        )
        self.default_floor_year = default_floor_year
        self.default_xbrl_floor_year = default_xbrl_floor_year
        self.recheck_count = recheck_periods

    def select_issuers(self, only: Sequence[str] | None = None) -> tuple[IssuerSpec, ...]:
        """Resolve ``--only`` identifiers while preserving registry order."""

        if not only:
            return tuple(issuer for issuer in self.registry.issuers if issuer.active)
        selected = {self.registry.find(identifier).slug for identifier in only}
        return tuple(issuer for issuer in self.registry.issuers if issuer.slug in selected)

    def plan(
        self,
        *,
        only: Sequence[str] | None = None,
        as_of: date | None = None,
        floor_year: int | None = None,
    ) -> tuple[IssuerPlan, ...]:
        """Build a local, read-only coverage plan; it performs no discovery."""

        run_date = as_of or date.today()
        plans: list[IssuerPlan] = []
        for issuer in self.select_issuers(only):
            source_plans: list[SourcePlan] = []
            for source in issuer.sources:
                if not source.enabled or source.kind not in SUPPORTED_SOURCE_KINDS:
                    continue
                document_type = (
                    PDF_DOCUMENT_TYPE
                    if source.kind in PDF_SOURCE_KINDS
                    else XBRL_DOCUMENT_TYPE
                )
                known = self.coverage.known_periods(issuer.slug, document_type)
                floor = (
                    floor_year
                    or source.floor_year
                    or (
                        self.default_xbrl_floor_year
                        if source.kind == "bmv_xbrl"
                        else self.default_floor_year
                    )
                )
                closed = _within_listing_window(
                    expected_quarterly_periods(
                        floor,
                        run_date,
                        fiscal_year_end_month=issuer.fiscal_year_end_month,
                    ),
                    issuer,
                )
                if source.coverage_from_period is not None:
                    activation = period_sort_key(source.coverage_from_period)
                    closed = {
                        period
                        for period in closed
                        if period_sort_key(period) >= activation
                    }
                expected = {
                    period
                    for period in closed
                    if _period_due(
                        period,
                        run_date,
                        issuer.filing_grace_days,
                        issuer.fiscal_year_end_month,
                    )
                }
                # Discovery still checks the newest closed periods before the
                # grace deadline, so promptly published filings are ingested.
                latest = latest_periods(closed, self.recheck_count)
                source_plans.append(
                    SourcePlan(
                        issuer_slug=issuer.slug,
                        source_key=source.key,
                        source_kind=source.kind,
                        document_type=document_type,
                        known_periods=_sorted_periods(known),
                        missing_periods=_sorted_periods(expected - known),
                        recheck_periods=_sorted_periods(latest),
                        readiness="verified" if known else "unverified",
                    )
                )
            gaps: list[str] = []
            pdf_plans = [
                source
                for source in source_plans
                if source.document_type == PDF_DOCUMENT_TYPE
            ]
            known_pdf = self.coverage.known_periods(
                issuer.slug, PDF_DOCUMENT_TYPE
            )
            due_pdf = _due_periods_for_issuer(
                issuer,
                floor_year=floor_year or self.default_floor_year,
                as_of=run_date,
            )
            primary_pdf_missing = due_pdf - known_pdf
            if not pdf_plans:
                gaps.append("primary_pdf_source_unconfigured")
            source_window_missing = {
                period
                for source in pdf_plans
                for period in source.missing_periods
            }
            if pdf_plans and primary_pdf_missing - source_window_missing:
                gaps.append("primary_pdf_history_outside_source_window")
            plans.append(
                IssuerPlan(
                    issuer=issuer,
                    sources=tuple(source_plans),
                    gaps=tuple(gaps),
                    primary_pdf_missing_periods=_sorted_periods(
                        primary_pdf_missing
                    ),
                )
            )
        return tuple(plans)

    def sync(
        self,
        *,
        only: Sequence[str] | None = None,
        as_of: date | None = None,
        trigger: str = "manual",
        wayback_backfill: bool = False,
        backfill_from_year: int | None = None,
        strict_coverage: bool = False,
    ) -> RunReport:
        """Discover, fetch, and atomically store one refresh.

        Exceptions are contained to one issuer/source or one exact BMV filing.
        A run with any applied failure is durably marked failed and returns a
        non-zero ``exit_code`` to its CLI caller.
        """

        if self.ledger is None or self.writer is None:
            raise RuntimeError("sync requires both an acquisition ledger and estate writer")

        plans = self.plan(
            only=only,
            as_of=as_of,
            floor_year=backfill_from_year if wayback_backfill else None,
        )
        scope = {
            "module": "quarterly_acquisition",
            "issuers": [plan.issuer.slug for plan in plans],
            "wayback_backfill": bool(wayback_backfill),
            "recheck_periods": self.recheck_count,
            "backfill_from_year": backfill_from_year,
            "strict_coverage": bool(strict_coverage),
        }
        run_id = self.ledger.start_run(trigger=trigger, scope=scope)
        discovered_count = 0
        stored_count = 0
        unchanged_count = 0
        failures: list[SyncFailure] = []
        coverage_gaps: list[CoverageGap] = []
        diagnostics: list[SourceDiagnostic] = []

        try:
            for issuer_plan in plans:
                if "primary_pdf_source_unconfigured" in issuer_plan.gaps:
                    self._record_coverage_gap(
                        issuer_plan.issuer,
                        source_key="coverage_gate",
                        document_type=PDF_DOCUMENT_TYPE,
                        reason="primary_pdf_source_unconfigured",
                        periods=set(issuer_plan.primary_pdf_missing_periods),
                        coverage_gaps=coverage_gaps,
                    )
                elif (
                    "primary_pdf_history_outside_source_window"
                    in issuer_plan.gaps
                ):
                    served_periods = {
                        period
                        for source_plan in issuer_plan.sources
                        if source_plan.document_type == PDF_DOCUMENT_TYPE
                        for period in source_plan.missing_periods
                    }
                    self._record_coverage_gap(
                        issuer_plan.issuer,
                        source_key="coverage_gate",
                        document_type=PDF_DOCUMENT_TYPE,
                        reason="primary_pdf_history_outside_source_window",
                        periods=(
                            set(issuer_plan.primary_pdf_missing_periods)
                            - served_periods
                        ),
                        coverage_gaps=coverage_gaps,
                    )
            with tempfile.TemporaryDirectory(prefix="quarterly-acquisition-") as temporary:
                staging_root = Path(temporary)
                for issuer_plan in plans:
                    issuer = issuer_plan.issuer
                    for source_plan in issuer_plan.sources:
                        self.ledger.renew_lease(run_id)
                        source = issuer.source(source_plan.source_key)
                        staging = staging_root / issuer.slug / source.key
                        staging.mkdir(parents=True, exist_ok=True)
                        if source.kind == "bmv_xbrl":
                            counts = self._sync_bmv(
                                run_id,
                                issuer,
                                source,
                                source_plan,
                                staging,
                                failures,
                                coverage_gaps,
                            )
                        elif source.kind == "bmv_issuer_pdf":
                            counts = self._sync_bmv_issuer(
                                run_id,
                                issuer,
                                source,
                                source_plan,
                                staging,
                                failures,
                                coverage_gaps,
                            )
                        else:
                            counts = self._sync_ir(
                                run_id,
                                issuer,
                                source,
                                source_plan,
                                staging,
                                failures,
                                coverage_gaps,
                                diagnostics,
                                wayback_backfill=wayback_backfill,
                                backfill_from_year=backfill_from_year,
                            )
                        discovered_count += counts[0]
                        stored_count += counts[1]
                        unchanged_count += counts[2]
        except BaseException as exc:
            # Do not strand the durable run/lease on an unexpected orchestration
            # failure. Adapter/item exceptions are normally contained below.
            self.ledger.finish_run(
                run_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        blocking_coverage = strict_coverage and bool(coverage_gaps)
        any_blocker = bool(failures) or blocking_coverage
        status = (
            "partial"
            if any_blocker and (stored_count or unchanged_count)
            else "failed"
            if any_blocker
            else "succeeded"
        )
        error_parts: list[str] = []
        if failures:
            error_parts.append(f"{len(failures)} acquisition item(s) failed")
        if blocking_coverage:
            error_parts.append(f"{len(coverage_gaps)} strict coverage gap(s)")
        error = "; ".join(error_parts) + "; inspect the ledger" if error_parts else None
        self.ledger.finish_run(run_id, status=status, error=error)
        return RunReport(
            run_id=run_id,
            mode="backfill" if wayback_backfill else "sync",
            plans=plans,
            discovered=discovered_count,
            stored=stored_count,
            unchanged=unchanged_count,
            failures=tuple(failures),
            coverage_gaps=tuple(coverage_gaps),
            diagnostics=tuple(diagnostics),
            coverage_strict=strict_coverage,
        )

    def _sync_bmv(
        self,
        run_id: str,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        plan: SourcePlan,
        staging: Path,
        failures: list[SyncFailure],
        coverage_gaps: list[CoverageGap],
    ) -> tuple[int, int, int]:
        try:
            records = self.bmv.discover(
                issuer,
                source,
                wanted_periods=plan.desired_periods,
            )
        except Exception as exc:  # one archive failure must not block IR/other issuers
            self._record_source_failure(run_id, issuer, source, exc, failures)
            self._append_coverage_gap(
                issuer,
                source_key=source.key,
                document_type=plan.document_type,
                reason="source_discovery_failed",
                periods=set(plan.missing_periods),
                coverage_gaps=coverage_gaps,
            )
            self._record_unobserved_rechecks(
                run_id,
                issuer,
                source,
                plan,
                observed_periods=set(),
                failures=failures,
                coverage_gaps=coverage_gaps,
                source_failed=True,
            )
            return 0, 0, 0

        discovered = stored = unchanged = 0
        candidate_periods = {
            period
            for item in records
            if (period := normalize_period(item.record.period)) is not None
        }
        unresolved = set(plan.missing_periods) - candidate_periods
        if unresolved:
            self._record_coverage_gap(
                issuer,
                source_key=source.key,
                document_type=plan.document_type,
                reason="due_periods_not_discovered",
                periods=unresolved,
                coverage_gaps=coverage_gaps,
            )
        for index, candidate in enumerate(records):
            record = candidate.record
            self.ledger.discover(record, run_id=run_id)
            discovered += 1
            item_staging = staging / f"{index:04d}"
            item_staging.mkdir(parents=True, exist_ok=True)
            try:
                artifact = self.bmv.fetch(candidate, item_staging, source=source)
                created = self._store(run_id, issuer, artifact)
                stored += int(created)
                unchanged += int(not created)
            except AcquisitionLeaseLost:
                raise
            except ArtifactValidationError as exc:
                self._record_rejection(run_id, record, exc, failures)
            except Exception as exc:
                self._record_failure(run_id, record, exc, failures)
        self._record_unobserved_rechecks(
            run_id,
            issuer,
            source,
            plan,
            observed_periods=candidate_periods,
            failures=failures,
            coverage_gaps=coverage_gaps,
        )
        return discovered, stored, unchanged

    def _sync_bmv_issuer(
        self,
        run_id: str,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        plan: SourcePlan,
        staging: Path,
        failures: list[SyncFailure],
        coverage_gaps: list[CoverageGap],
    ) -> tuple[int, int, int]:
        """Discover and store official BMV quarterly narrative PDFs."""

        try:
            records = self.bmv_issuer.discover(
                issuer,
                source,
                wanted_periods=plan.desired_periods,
            )
        except Exception as exc:
            self._record_source_failure(run_id, issuer, source, exc, failures)
            self._append_coverage_gap(
                issuer,
                source_key=source.key,
                document_type=plan.document_type,
                reason="source_discovery_failed",
                periods=set(plan.missing_periods),
                coverage_gaps=coverage_gaps,
            )
            self._record_unobserved_rechecks(
                run_id,
                issuer,
                source,
                plan,
                observed_periods=set(),
                failures=failures,
                coverage_gaps=coverage_gaps,
                source_failed=True,
            )
            return 0, 0, 0

        discovered = stored = unchanged = 0
        observed_periods = {
            period
            for item in records
            if (period := normalize_period(item.record.period)) is not None
        }
        unresolved = set(plan.missing_periods) - observed_periods
        if unresolved:
            self._record_coverage_gap(
                issuer,
                source_key=source.key,
                document_type=plan.document_type,
                reason="due_periods_not_discovered",
                periods=unresolved,
                coverage_gaps=coverage_gaps,
            )
        for index, candidate in enumerate(records):
            record = candidate.record
            self.ledger.discover(record, run_id=run_id)
            discovered += 1
            item_staging = staging / f"{index:04d}"
            try:
                artifact = self.bmv_issuer.fetch(
                    candidate,
                    item_staging,
                    source=source,
                )
                created = self._store(run_id, issuer, artifact)
                stored += int(created)
                unchanged += int(not created)
            except AcquisitionLeaseLost:
                raise
            except ArtifactValidationError as exc:
                self._record_rejection(run_id, record, exc, failures)
            except Exception as exc:
                self._record_failure(run_id, record, exc, failures)
        self._record_unobserved_rechecks(
            run_id,
            issuer,
            source,
            plan,
            observed_periods=observed_periods,
            failures=failures,
            coverage_gaps=coverage_gaps,
        )
        return discovered, stored, unchanged

    def _sync_ir(
        self,
        run_id: str,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        plan: SourcePlan,
        staging: Path,
        failures: list[SyncFailure],
        coverage_gaps: list[CoverageGap],
        diagnostics: list[SourceDiagnostic],
        *,
        wayback_backfill: bool,
        backfill_from_year: int | None,
    ) -> tuple[int, int, int]:
        source_failed = False
        issues: tuple[IRFetchIssue, ...] = ()
        candidate_periods: set[str] = set()
        try:
            report_fetcher = getattr(
                self.ir,
                "fetch_incremental_report",
                None,
            )
            if callable(report_fetcher):
                fetch_report = report_fetcher(
                    issuer,
                    source,
                    staging / "live",
                    known_periods=set(plan.known_periods),
                    recheck_periods=set(plan.recheck_periods),
                    desired_periods=plan.desired_periods,
                )
                artifacts = tuple(fetch_report.artifacts)
                issues = tuple(fetch_report.issues)
                candidate_periods = {
                    period
                    for value in fetch_report.candidate_periods
                    if (period := normalize_period(value)) is not None
                }
                diagnostics.extend(
                    SourceDiagnostic(
                        issuer_slug=issuer.slug,
                        source_key=source.key,
                        layer=layer.layer,
                        candidates=layer.candidates,
                        selected=layer.selected,
                        note=layer.note,
                    )
                    for layer in fetch_report.layers
                )
            else:
                artifacts = self.ir.fetch_incremental(
                    issuer,
                    source,
                    staging / "live",
                    known_periods=set(plan.known_periods),
                    recheck_periods=set(plan.recheck_periods),
                    desired_periods=plan.desired_periods,
                )
        except Exception as exc:
            self._record_source_failure(run_id, issuer, source, exc, failures)
            source_failed = True
            artifacts = ()

        discovered = stored = unchanged = 0
        for issue in issues:
            self.ledger.discover(issue.record, run_id=run_id)
            discovered += 1
            self._record_ir_issue(run_id, issue, failures)
        observed_periods = {
            period
            for artifact in artifacts
            if (period := normalize_period(artifact.source.period)) is not None
        }
        for artifact in artifacts:
            self.ledger.discover(artifact.source, run_id=run_id)
            discovered += 1
            try:
                created = self._store(run_id, issuer, artifact)
                stored += int(created)
                unchanged += int(not created)
            except AcquisitionLeaseLost:
                raise
            except ArtifactValidationError as exc:
                self._record_rejection(run_id, artifact.source, exc, failures)
            except Exception as exc:
                self._record_failure(run_id, artifact.source, exc, failures)

        # Archive.org is intentionally outside the recurring path. Merely
        # constructing a service or calling normal sync cannot invoke it.
        if wayback_backfill and plan.missing_periods:
            try:
                recovered = self.wayback.fetch_missing(
                    issuer,
                    source,
                    staging / "wayback",
                    missing_periods=set(plan.missing_periods),
                    from_year=backfill_from_year,
                )
            except Exception as exc:
                self._record_source_failure(
                    run_id,
                    issuer,
                    source,
                    exc,
                    failures,
                    source_key=f"{source.key}:wayback",
                )
                recovered = ()
                source_failed = True
            for artifact in recovered:
                period = normalize_period(artifact.source.period)
                if period is not None:
                    observed_periods.add(period)
                self.ledger.discover(artifact.source, run_id=run_id)
                discovered += 1
                try:
                    created = self._store(run_id, issuer, artifact)
                    stored += int(created)
                    unchanged += int(not created)
                except AcquisitionLeaseLost:
                    raise
                except ArtifactValidationError as exc:
                    self._record_rejection(run_id, artifact.source, exc, failures)
                except Exception as exc:
                    self._record_failure(run_id, artifact.source, exc, failures)
        unresolved = set(plan.missing_periods) - observed_periods
        if unresolved:
            if source_failed:
                self._append_coverage_gap(
                    issuer,
                    source_key=source.key,
                    document_type=plan.document_type,
                    reason="due_periods_unresolved_after_source_failure",
                    periods=unresolved,
                    coverage_gaps=coverage_gaps,
                )
            else:
                self._record_coverage_gap(
                    issuer,
                    source_key=source.key,
                    document_type=plan.document_type,
                    reason="due_periods_not_discovered",
                    periods=unresolved,
                    coverage_gaps=coverage_gaps,
                )
        self._record_unobserved_rechecks(
            run_id,
            issuer,
            source,
            plan,
            observed_periods=candidate_periods | observed_periods,
            failures=failures,
            coverage_gaps=coverage_gaps,
            source_failed=source_failed,
        )
        return discovered, stored, unchanged

    def _store(
        self,
        run_id: str,
        issuer: IssuerSpec,
        artifact: FetchedArtifact,
    ) -> bool:
        memberships = tuple(
            {
                "company": company,
                "industry": issuer.sector,
                "alpha_go": issuer.memberships.alpha_go,
                "soft": issuer.memberships.soft,
                "earnings": issuer.memberships.earnings,
            }
            for company in expand_company_aliases(
                issuer.slug,
                self.company_aliases,
            )
        )
        result = self.writer.store_fetched(
            artifact,
            run_id=run_id,
            memberships=memberships,
        )
        return bool(getattr(result, "created", True))

    def _record_coverage_gap(
        self,
        issuer: IssuerSpec,
        *,
        source_key: str,
        document_type: str,
        reason: str,
        periods: set[str],
        coverage_gaps: list[CoverageGap],
    ) -> None:
        self._append_coverage_gap(
            issuer,
            source_key=source_key,
            document_type=document_type,
            reason=reason,
            periods=periods,
            coverage_gaps=coverage_gaps,
        )

    @staticmethod
    def _append_coverage_gap(
        issuer: IssuerSpec,
        *,
        source_key: str,
        document_type: str,
        reason: str,
        periods: set[str],
        coverage_gaps: list[CoverageGap],
    ) -> CoverageGap:
        gap = CoverageGap(
            issuer_slug=issuer.slug,
            source_key=source_key,
            document_type=document_type,
            reason=reason,
            periods=_sorted_periods(periods),
        )
        coverage_gaps.append(gap)
        return gap

    def _record_source_failure(
        self,
        run_id: str,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        exc: Exception,
        failures: list[SyncFailure],
        *,
        source_key: str | None = None,
    ) -> None:
        record = SourceRecord(
            source_key=source_key or source.key,
            source_record_id=f"{issuer.slug}:source-check:{date.today().isoformat()}",
            issuer_slug=issuer.slug,
            url=source.url,
            document_type=(
                PDF_DOCUMENT_TYPE
                if source.kind in PDF_SOURCE_KINDS
                else XBRL_DOCUMENT_TYPE
            ),
            title=f"{issuer.name} source check",
            metadata={"adapter": source.kind, "synthetic_probe": True},
        )
        self.ledger.discover(record, run_id=run_id)
        self._record_failure(run_id, record, exc, failures)

    def _record_recheck_failure(
        self,
        run_id: str,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        document_type: str,
        period: str,
        failures: list[SyncFailure],
    ) -> None:
        normalized = normalize_period(period)
        if normalized is None:
            raise ValueError(f"invalid recheck period: {period!r}")
        year, quarter = normalized.split("-")
        record = SourceRecord(
            source_key=source.key,
            source_record_id=(
                f"{issuer.slug}:recheck:{document_type}:{normalized}"
            ),
            issuer_slug=issuer.slug,
            url=source.url,
            document_type=document_type,
            title=f"{issuer.name} {normalized} trailing recheck",
            period_year=int(year),
            period_quarter=int(quarter[0]),
            metadata={
                "adapter": source.kind,
                "synthetic_probe": True,
                "recheck_period": normalized,
            },
        )
        self.ledger.discover(record, run_id=run_id)
        self._record_failure(
            run_id,
            record,
            RuntimeError(
                "trailing recheck observed no candidate or artifact for "
                f"{normalized}"
            ),
            failures,
        )

    def _record_unobserved_rechecks(
        self,
        run_id: str,
        issuer: IssuerSpec,
        source: AcquisitionSource,
        plan: SourcePlan,
        *,
        observed_periods: set[str],
        failures: list[SyncFailure],
        coverage_gaps: list[CoverageGap],
        source_failed: bool = False,
    ) -> None:
        """Make a failed trailing restatement probe operationally visible."""

        known_rechecks = set(plan.recheck_periods) & set(plan.known_periods)
        observed = {
            period
            for value in observed_periods
            if (period := normalize_period(value)) is not None
        }
        unobserved = known_rechecks - observed
        if not unobserved:
            return
        self._append_coverage_gap(
            issuer,
            source_key=source.key,
            document_type=plan.document_type,
            reason=(
                "recheck_periods_unresolved_after_source_failure"
                if source_failed
                else "recheck_periods_not_observed"
            ),
            periods=unobserved,
            coverage_gaps=coverage_gaps,
        )
        if source_failed:
            return
        for period in _sorted_periods(unobserved):
            self._record_recheck_failure(
                run_id,
                issuer,
                source,
                plan.document_type,
                period,
                failures,
            )

    def _record_ir_issue(
        self,
        run_id: str,
        issue: IRFetchIssue,
        failures: list[SyncFailure],
    ) -> None:
        if issue.retryable:
            self.ledger.mark_retryable(
                issue.record,
                run_id=run_id,
                error=issue.error,
            )
        else:
            self.ledger.mark_rejected(
                issue.record,
                run_id=run_id,
                reason=issue.error,
            )
        failures.append(
            SyncFailure(
                issuer_slug=issue.record.issuer_slug,
                source_key=issue.record.source_key,
                source_record_id=issue.record.source_record_id,
                error=issue.error,
            )
        )

    def _record_failure(
        self,
        run_id: str,
        record: SourceRecord,
        exc: Exception,
        failures: list[SyncFailure],
    ) -> None:
        error = f"{type(exc).__name__}: {exc}"
        self.ledger.mark_retryable(record, run_id=run_id, error=error)
        failures.append(
            SyncFailure(
                issuer_slug=record.issuer_slug,
                source_key=record.source_key,
                source_record_id=record.source_record_id,
                error=error,
            )
        )

    def _record_rejection(
        self,
        run_id: str,
        record: SourceRecord,
        exc: Exception,
        failures: list[SyncFailure],
    ) -> None:
        error = f"{type(exc).__name__}: {exc}"
        self.ledger.mark_rejected(record, run_id=run_id, reason=error)
        failures.append(
            SyncFailure(
                issuer_slug=record.issuer_slug,
                source_key=record.source_key,
                source_record_id=record.source_record_id,
                error=error,
            )
        )


def expected_quarterly_periods(
    floor_year: int,
    as_of: date,
    *,
    fiscal_year_end_month: int = 12,
) -> set[str]:
    """All completed fiscal quarters through ``as_of``.

    The period year is the fiscal year label.  For the common December year end
    this is identical to calendar ``YYYY-NT`` behavior.
    """

    if fiscal_year_end_month not in range(1, 13):
        raise ValueError("fiscal_year_end_month must be 1..12")
    periods: set[str] = set()
    # A non-December fiscal year can have FY-(as_of.year + 1) quarters ending
    # during the current calendar year.
    for fiscal_year in range(floor_year, as_of.year + 2):
        for quarter in range(1, 5):
            period = f"{fiscal_year}-{quarter}T"
            if _fiscal_period_end(period, fiscal_year_end_month) <= as_of:
                periods.add(period)
    return periods


def latest_periods(periods: Iterable[str], count: int) -> set[str]:
    if count <= 0:
        return set()
    ordered = sorted(
        {
            period for value in periods if (period := normalize_period(value)) is not None
        },
        key=period_sort_key,
    )
    return set(ordered[-count:])


def _due_periods_for_issuer(
    issuer: IssuerSpec,
    *,
    floor_year: int,
    as_of: date,
) -> set[str]:
    closed = _within_listing_window(
        expected_quarterly_periods(
            floor_year,
            as_of,
            fiscal_year_end_month=issuer.fiscal_year_end_month,
        ),
        issuer,
    )
    return {
        period
        for period in closed
        if _period_due(
            period,
            as_of,
            issuer.filing_grace_days,
            issuer.fiscal_year_end_month,
        )
    }


def _within_listing_window(periods: Iterable[str], issuer: IssuerSpec) -> set[str]:
    selected: set[str] = set()
    for value in periods:
        period = normalize_period(value)
        if period is None:
            continue
        period_end = _fiscal_period_end(period, issuer.fiscal_year_end_month)
        prior_end = _shift_month_end(period_end, -3)
        period_start = prior_end + timedelta(days=1)
        if issuer.listed_from and period_end < issuer.listed_from:
            continue
        if issuer.listed_to and period_start > issuer.listed_to:
            continue
        selected.add(period)
    return selected


def _period_due(
    period: str,
    as_of: date,
    grace_days: int,
    fiscal_year_end_month: int,
) -> bool:
    quarter_end = _fiscal_period_end(period, fiscal_year_end_month)
    return as_of >= quarter_end + timedelta(days=grace_days)


def _fiscal_period_end(period: str, fiscal_year_end_month: int) -> date:
    year_text, quarter_token = period.split("-", 1)
    fiscal_year = int(year_text)
    quarter = int(quarter_token[0])
    months_before_year_end = (4 - quarter) * 3
    absolute_month = fiscal_year * 12 + (fiscal_year_end_month - 1) - months_before_year_end
    calendar_year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    return date(calendar_year, month, calendar.monthrange(calendar_year, month)[1])


def _shift_month_end(value: date, months: int) -> date:
    absolute_month = value.year * 12 + (value.month - 1) + months
    year, zero_based_month = divmod(absolute_month, 12)
    month = zero_based_month + 1
    return date(year, month, calendar.monthrange(year, month)[1])


def _sorted_periods(periods: Iterable[str]) -> tuple[str, ...]:
    normalized = {
        period for value in periods if (period := normalize_period(value)) is not None
    }
    return tuple(sorted(normalized, key=period_sort_key))


__all__ = [
    "AcquisitionLedgerProtocol",
    "CoverageProtocol",
    "CoverageGap",
    "EmptyCoverage",
    "EstateCoverage",
    "EstateWriterProtocol",
    "IssuerPlan",
    "PDF_DOCUMENT_TYPE",
    "QuarterlyAcquisitionService",
    "QuarterlySyncFreshness",
    "RunReport",
    "SourceDiagnostic",
    "SourcePlan",
    "SyncFailure",
    "XBRL_DOCUMENT_TYPE",
    "check_quarterly_publication_freshness",
    "check_quarterly_sync_freshness",
    "expected_quarterly_periods",
    "latest_periods",
]
