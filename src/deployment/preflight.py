"""Read-only deployment preflight for the shared document estate.

This module intentionally does not import either ``AcquisitionLedger`` or
``DocumentEstate``: both application classes own migrations.  The preflight
opens SQLite with ``mode=ro`` and restricts itself to registry reads, catalog
queries, and filesystem existence checks.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence

from src.acquisition.registry import (
    DEFAULT_REGISTRY_PATH,
    IssuerRegistryError,
    load_issuer_registry,
)
from src.shared.paths import DOCUMENT_ESTATE_DIR


SUPPORTED_PYTHON_MIN = (3, 13)
SUPPORTED_PYTHON_MAX_EXCLUSIVE = (3, 14)

REQUIRED_ESTATE_TABLES = (
    "documents",
    "artifacts",
    "content_objects",
)
REQUIRED_ACQUISITION_TABLES = (
    "acquisition_schema_migrations",
    "acquisition_runs",
    "acquisition_leases",
    "source_records",
    "source_record_versions",
    "document_projects",
    "acquisition_attempts",
)
REQUIRED_OUTBOX_TABLES = ("outbox",)
REQUIRED_CONSUMER_TABLES = (
    "outbox_consumer_schema_migrations",
    "outbox_subscriptions",
    "outbox_deliveries",
    "outbox_delivery_attempts",
    "document_derivations",
)
PRIMARY_PDF_SOURCE_KINDS = frozenset(
    {"investor_relations", "bmv_issuer_pdf"}
)
PRIMARY_PDF_LIVE_VERIFICATION_MAX_QUARTER_LAG = 4


class PreflightMode(str, Enum):
    """Deployment target whose policy determines finding severity."""

    AUDIT = "audit"
    WORKER = "worker"
    PRODUCTION = "production"


class CheckStatus(str, Enum):
    """Outcome of one preflight check."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class Severity(str, Enum):
    """Operational significance of a check result."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    """One stable, machine-readable diagnostic."""

    name: str
    category: str
    status: CheckStatus
    severity: Severity
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "status": self.status.value,
            "severity": self.severity.value,
            "message": self.message,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Complete preflight result."""

    mode: PreflightMode
    checks: tuple[PreflightCheck, ...]

    @property
    def failed_checks(self) -> tuple[PreflightCheck, ...]:
        return tuple(check for check in self.checks if check.status == CheckStatus.FAIL)

    @property
    def blockers(self) -> tuple[PreflightCheck, ...]:
        return tuple(
            check
            for check in self.failed_checks
            if check.severity == Severity.BLOCKER
        )

    @property
    def exit_code(self) -> int:
        """Return the policy exit code for this mode.

        Audit mode is observational and always exits successfully.  A worker
        refuses error/blocker findings.  Production refuses every error or
        blocker, including portability, schema, and consumer blockers.
        """

        if self.mode == PreflightMode.AUDIT:
            return 0
        return int(
            any(
                check.severity in {Severity.ERROR, Severity.BLOCKER}
                for check in self.failed_checks
            )
        )

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def to_dict(self) -> dict[str, Any]:
        counts = {
            severity.value: sum(
                check.status == CheckStatus.FAIL and check.severity == severity
                for check in self.checks
            )
            for severity in Severity
        }
        return {
            "schema_version": 1,
            "mode": self.mode.value,
            "ok": self.ok,
            "exit_code": self.exit_code,
            "summary": {
                "checks": len(self.checks),
                "failed": len(self.failed_checks),
                "blockers": len(self.blockers),
                "findings_by_severity": counts,
            },
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
        )


def _severity(
    mode: PreflightMode,
    *,
    audit: Severity = Severity.WARNING,
    worker: Severity = Severity.ERROR,
    production: Severity = Severity.BLOCKER,
) -> Severity:
    return {
        PreflightMode.AUDIT: audit,
        PreflightMode.WORKER: worker,
        PreflightMode.PRODUCTION: production,
    }[mode]


def _pass(
    name: str,
    category: str,
    message: str,
    **details: Any,
) -> PreflightCheck:
    return PreflightCheck(
        name=name,
        category=category,
        status=CheckStatus.PASS,
        severity=Severity.INFO,
        message=message,
        details=details,
    )


def _fail(
    name: str,
    category: str,
    severity: Severity,
    message: str,
    **details: Any,
) -> PreflightCheck:
    return PreflightCheck(
        name=name,
        category=category,
        status=CheckStatus.FAIL,
        severity=severity,
        message=message,
        details=details,
    )


def _skip(
    name: str,
    category: str,
    message: str,
    **details: Any,
) -> PreflightCheck:
    return PreflightCheck(
        name=name,
        category=category,
        status=CheckStatus.SKIP,
        severity=Severity.INFO,
        message=message,
        details=details,
    )


def _readonly_connection(database: Path) -> sqlite3.Connection:
    """Open an existing SQLite catalog without creating or migrating it."""

    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    # Table names come exclusively from constants or sqlite_master.
    return {
        str(row["name"])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _resolve_artifact_path(raw: object, estate_root: Path) -> Path | None:
    if raw is None or not str(raw).strip():
        return None
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else estate_root / path


def _portable_object_path(raw: object, estate_root: Path) -> Path | None:
    if raw is None or not str(raw).strip():
        return None
    token = str(raw).strip()
    path = Path(token)
    if path.is_absolute() or PureWindowsPath(token).is_absolute() or ".." in path.parts:
        return None
    return estate_root / path


def _is_absolute(raw: object) -> bool:
    if raw is None:
        return False
    token = str(raw).strip()
    return bool(token) and (
        Path(token).is_absolute() or PureWindowsPath(token).is_absolute()
    )


def _check_python(
    mode: PreflightMode, version: Sequence[int]
) -> PreflightCheck:
    normalized = tuple(int(part) for part in version[:3])
    supported = (
        normalized[:2] >= SUPPORTED_PYTHON_MIN
        and normalized[:2] < SUPPORTED_PYTHON_MAX_EXCLUSIVE
    )
    details = {
        "current": ".".join(map(str, normalized)),
        "supported": ">=3.13,<3.14",
    }
    if supported:
        return _pass(
            "python.supported",
            "runtime",
            f"Python {details['current']} is in the supported range.",
            **details,
        )
    return _fail(
        "python.supported",
        "runtime",
        _severity(mode),
        f"Python {details['current']} is outside the supported range.",
        **details,
    )


def _check_registry(
    mode: PreflightMode,
    registry_path: Path,
    *,
    as_of: date,
) -> list[PreflightCheck]:
    try:
        registry = load_issuer_registry(registry_path)
    except (IssuerRegistryError, OSError, ValueError) as exc:
        return [
            _fail(
                "registry.load",
                "registry",
                _severity(mode),
                f"Canonical issuer registry could not be loaded: {exc}",
                path=str(registry_path),
            ),
            _skip(
                "registry.count",
                "registry",
                "Issuer count was not checked because registry loading failed.",
            ),
            _skip(
                "registry.source_readiness",
                "registry",
                "Source readiness was not checked because registry loading failed.",
            ),
        ]

    active = tuple(issuer for issuer in registry.issuers if issuer.active)
    checks = [
        _pass(
            "registry.load",
            "registry",
            "Canonical issuer registry loaded successfully.",
            path=str(registry_path),
            version=registry.version,
        )
    ]
    if active:
        checks.append(
            _pass(
                "registry.count",
                "registry",
                f"Registry contains {len(active)} active issuers.",
                total_issuers=len(registry.issuers),
                active_issuers=len(active),
            )
        )
    else:
        checks.append(
            _fail(
                "registry.count",
                "registry",
                _severity(mode),
                "Registry contains no active issuers.",
                total_issuers=len(registry.issuers),
                active_issuers=0,
            )
        )

    no_enabled_source: list[str] = []
    missing_pdf: list[str] = []
    no_current_pdf: list[str] = []
    no_live_verified_pdf: list[str] = []
    unverified_pdf_bindings: list[str] = []
    stale_or_invalid_verification_bindings: list[str] = []
    missing_xbrl: list[str] = []
    current_period = f"{as_of.year}-{((as_of.month - 1) // 3) + 1}T"
    current_period_key = _period_key(current_period)
    for issuer in active:
        enabled = tuple(source for source in issuer.sources if source.enabled)
        kinds = {source.kind for source in enabled}
        pdf_sources = tuple(
            source
            for source in enabled
            if source.kind in PRIMARY_PDF_SOURCE_KINDS
        )
        current_pdf_sources = tuple(
            source
            for source in pdf_sources
            if _source_start_key(source) <= current_period_key
        )
        live_verified_sources = tuple(
            source
            for source in current_pdf_sources
            if source.live_verified_period is not None
            and source.live_verified_on is not None
            and source.live_verified_url is not None
            and source.live_verified_on <= as_of
            and 0
            <= _quarter_distance(
                _period_key(source.live_verified_period),
                current_period_key,
            )
            <= PRIMARY_PDF_LIVE_VERIFICATION_MAX_QUARTER_LAG
        )
        if not enabled:
            no_enabled_source.append(issuer.slug)
        if not pdf_sources:
            missing_pdf.append(issuer.slug)
        elif not current_pdf_sources:
            no_current_pdf.append(issuer.slug)
        if not live_verified_sources:
            no_live_verified_pdf.append(issuer.slug)
        for source in pdf_sources:
            if source not in live_verified_sources:
                unverified_pdf_bindings.append(
                    f"{issuer.slug}/{source.key}"
                )
                if (
                    source.live_verified_period is not None
                    and source.live_verified_on is not None
                    and source.live_verified_url is not None
                ):
                    stale_or_invalid_verification_bindings.append(
                        f"{issuer.slug}/{source.key}"
                    )
        if "bmv_xbrl" not in kinds:
            missing_xbrl.append(issuer.slug)

    readiness_details = {
        "as_of": as_of.isoformat(),
        "current_period": current_period,
        "max_live_verification_quarter_lag": (
            PRIMARY_PDF_LIVE_VERIFICATION_MAX_QUARTER_LAG
        ),
        "active_issuers": len(active),
        "issuers_with_primary_pdf_source": len(active) - len(missing_pdf),
        "issuers_with_current_primary_pdf_source": (
            len(active) - len(missing_pdf) - len(no_current_pdf)
        ),
        "issuers_with_live_verified_primary_pdf_source": (
            len(active) - len(no_live_verified_pdf)
        ),
        "issuers_with_bmv_xbrl_source": len(active) - len(missing_xbrl),
        "issuers_without_any_enabled_source": len(no_enabled_source),
        "missing_primary_pdf_count": len(missing_pdf),
        "future_only_primary_pdf_count": len(no_current_pdf),
        "missing_live_verified_primary_pdf_count": len(
            no_live_verified_pdf
        ),
        "unverified_primary_pdf_binding_count": len(
            unverified_pdf_bindings
        ),
        "stale_or_invalid_verification_binding_count": len(
            stale_or_invalid_verification_bindings
        ),
        "missing_bmv_xbrl_count": len(missing_xbrl),
        "missing_primary_pdf_sample": missing_pdf[:20],
        "future_only_primary_pdf_sample": no_current_pdf[:20],
        "missing_live_verified_primary_pdf_sample": (
            no_live_verified_pdf[:20]
        ),
        "unverified_primary_pdf_binding_sample": (
            unverified_pdf_bindings[:20]
        ),
        "stale_or_invalid_verification_binding_sample": (
            stale_or_invalid_verification_bindings[:20]
        ),
        "missing_bmv_xbrl_sample": missing_xbrl[:20],
    }
    if (
        not no_enabled_source
        and not missing_pdf
        and not no_current_pdf
        and not unverified_pdf_bindings
        and not missing_xbrl
    ):
        checks.append(
            _pass(
                "registry.source_readiness",
                "registry",
                "Every active issuer has a current, live-verified primary-PDF "
                "source and an enabled BMV XBRL source.",
                **readiness_details,
            )
        )
    else:
        # An incomplete registry is useful in an audit and can run as a
        # partial-fleet worker, but it must not be certified for production.
        severity = _severity(
            mode,
            audit=Severity.WARNING,
            worker=Severity.WARNING,
            production=Severity.BLOCKER,
        )
        checks.append(
            _fail(
                "registry.source_readiness",
                "registry",
                severity,
                "The active issuer fleet does not have complete, current, "
                "live-verified primary-PDF and BMV XBRL source coverage.",
                **readiness_details,
            )
        )
    return checks


def _period_key(period: str) -> tuple[int, int]:
    year, quarter = period.removesuffix("T").split("-", 1)
    return int(year), int(quarter)


def _source_start_key(source) -> tuple[int, int]:
    bounds = [(source.floor_year, 1)] if source.floor_year is not None else []
    if source.coverage_from_period is not None:
        bounds.append(_period_key(source.coverage_from_period))
    return max(bounds, default=(0, 1))


def _quarter_distance(
    earlier: tuple[int, int],
    later: tuple[int, int],
) -> int:
    return (later[0] * 4 + later[1]) - (earlier[0] * 4 + earlier[1])


def _check_required_tables(
    mode: PreflightMode,
    tables: set[str],
) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    groups = (
        ("estate", REQUIRED_ESTATE_TABLES),
        ("acquisition", REQUIRED_ACQUISITION_TABLES),
        ("outbox", REQUIRED_OUTBOX_TABLES),
        ("consumer", REQUIRED_CONSUMER_TABLES),
    )
    for category, required in groups:
        missing = sorted(set(required) - tables)
        name = f"schema.{category}_tables"
        if missing:
            checks.append(
                _fail(
                    name,
                    "schema",
                    _severity(mode),
                    f"Required {category} tables are missing.",
                    required=list(required),
                    missing=missing,
                )
            )
        else:
            checks.append(
                _pass(
                    name,
                    "schema",
                    f"All required {category} tables are present.",
                    required=list(required),
                )
            )
    return checks


def _check_consumer_schema_contract(
    mode: PreflightMode,
    connection: sqlite3.Connection,
    tables: set[str],
) -> PreflightCheck:
    required_tables = {
        "outbox_consumer_schema_migrations",
        "outbox_deliveries",
        "outbox_delivery_attempts",
    }
    if not required_tables <= tables:
        return _skip(
            "schema.consumer_contract",
            "schema",
            "Consumer schema contract was not checked because tables are missing.",
        )
    version = int(
        connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM outbox_consumer_schema_migrations"
        ).fetchone()[0]
    )
    required_columns = {
        "outbox_deliveries": {
            "event_id",
            "consumer_id",
            "status",
            "attempt_count",
            "replay_count",
            "available_at",
            "lock_token",
            "locked_until",
        },
        "outbox_delivery_attempts": {
            "event_id",
            "consumer_id",
            "replay_number",
            "attempt_number",
            "lock_token",
            "status",
        },
    }
    missing_columns = {
        table: sorted(columns - _columns(connection, table))
        for table, columns in required_columns.items()
        if columns - _columns(connection, table)
    }
    if version < 2 or missing_columns:
        return _fail(
            "schema.consumer_contract",
            "schema",
            _severity(mode),
            "Consumer schema is older than the required delivery contract.",
            current_version=version,
            required_version=2,
            missing_columns=missing_columns,
        )
    return _pass(
        "schema.consumer_contract",
        "schema",
        "Consumer schema contract is current.",
        current_version=version,
        required_version=2,
    )


def _check_object_keys(
    mode: PreflightMode,
    connection: sqlite3.Connection,
    tables: set[str],
) -> list[PreflightCheck]:
    if "content_objects" not in tables:
        return [
            _skip(
                "schema.content_objects_object_key",
                "schema",
                "The object_key column was not checked because content_objects is missing.",
            ),
            _skip(
                "estate.portable_object_keys",
                "portability",
                "Portable object keys were not checked because content_objects is missing.",
            ),
        ]
    columns = _columns(connection, "content_objects")
    if "object_key" not in columns:
        return [
            _fail(
                "schema.content_objects_object_key",
                "schema",
                _severity(mode),
                "content_objects.object_key is missing.",
            ),
            _skip(
                "estate.portable_object_keys",
                "portability",
                "Portable object-key rows were not checked because the column is missing.",
            ),
        ]

    checks = [
        _pass(
            "schema.content_objects_object_key",
            "schema",
            "content_objects.object_key is present.",
        )
    ]
    rows = connection.execute(
        "SELECT object_key FROM content_objects"
    ).fetchall()
    missing = 0
    invalid = 0
    for row in rows:
        value = row["object_key"]
        if value is None or not str(value).strip():
            missing += 1
        elif _portable_object_path(value, Path("/__preflight_root__")) is None:
            invalid += 1
    details = {
        "content_objects": len(rows),
        "missing_object_keys": missing,
        "invalid_object_keys": invalid,
    }
    if missing or invalid:
        checks.append(
            _fail(
                "estate.portable_object_keys",
                "portability",
                _severity(
                    mode,
                    audit=Severity.WARNING,
                    worker=Severity.WARNING,
                    production=Severity.BLOCKER,
                ),
                "Some content objects do not have safe portable object keys.",
                **details,
            )
        )
    else:
        checks.append(
            _pass(
                "estate.portable_object_keys",
                "portability",
                "Every content object has a safe portable object key.",
                **details,
            )
        )
    return checks


def _check_artifacts_and_blobs(
    mode: PreflightMode,
    connection: sqlite3.Connection,
    tables: set[str],
    estate_root: Path,
) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    if "artifacts" not in tables:
        return [
            _skip(
                "estate.absolute_artifact_paths",
                "portability",
                "Artifact portability was not checked because artifacts is missing.",
            ),
            _skip(
                "estate.missing_files",
                "integrity",
                "Artifact and blob existence was not checked because artifacts is missing.",
            ),
            _skip(
                "estate.original_content_objects",
                "integrity",
                "Original hashes were not checked because artifacts is missing.",
            ),
        ]

    artifact_columns = _columns(connection, "artifacts")
    artifact_paths: list[object] = []
    if "path" in artifact_columns:
        artifact_paths = [
            row["path"]
            for row in connection.execute("SELECT path FROM artifacts")
        ]
        absolute = sum(_is_absolute(path) for path in artifact_paths)
        if absolute:
            checks.append(
                _fail(
                    "estate.absolute_artifact_paths",
                    "portability",
                    _severity(
                        mode,
                        audit=Severity.WARNING,
                        worker=Severity.WARNING,
                        production=Severity.BLOCKER,
                    ),
                    f"{absolute} artifact paths are absolute compatibility paths.",
                    artifact_paths=len(artifact_paths),
                    absolute_artifact_paths=absolute,
                )
            )
        else:
            checks.append(
                _pass(
                    "estate.absolute_artifact_paths",
                    "portability",
                    "No artifact paths are absolute.",
                    artifact_paths=len(artifact_paths),
                    absolute_artifact_paths=0,
                )
            )
    else:
        checks.append(
            _fail(
                "estate.absolute_artifact_paths",
                "schema",
                _severity(mode),
                "artifacts.path is missing.",
            )
        )

    missing_artifacts = sum(
        path is None or not path.is_file()
        for path in (
            _resolve_artifact_path(raw, estate_root) for raw in artifact_paths
        )
    )
    missing_blobs = 0
    blob_count = 0
    if "content_objects" in tables:
        content_columns = _columns(connection, "content_objects")
        selected = [
            column
            for column in ("object_key", "blob_path")
            if column in content_columns
        ]
        if selected:
            query = "SELECT " + ", ".join(
                f'"{column}"' for column in selected
            ) + " FROM content_objects"
            for row in connection.execute(query):
                blob_count += 1
                candidate: Path | None = None
                if (
                    "object_key" in selected
                    and row["object_key"] is not None
                    and str(row["object_key"]).strip()
                ):
                    portable = _portable_object_path(row["object_key"], estate_root)
                    candidate = portable
                elif "blob_path" in selected:
                    candidate = _resolve_artifact_path(
                        row["blob_path"], estate_root
                    )
                if candidate is None or not candidate.is_file():
                    missing_blobs += 1

    if missing_artifacts or missing_blobs:
        checks.append(
            _fail(
                "estate.missing_files",
                "integrity",
                _severity(mode),
                "The catalog references missing artifact or content-object files.",
                artifacts=len(artifact_paths),
                missing_artifacts=missing_artifacts,
                content_objects=blob_count,
                missing_blobs=missing_blobs,
            )
        )
    else:
        checks.append(
            _pass(
                "estate.missing_files",
                "integrity",
                "All catalogued artifact and content-object paths resolve to files.",
                artifacts=len(artifact_paths),
                missing_artifacts=0,
                content_objects=blob_count,
                missing_blobs=0,
            )
        )

    if "content_objects" not in tables:
        checks.append(
            _skip(
                "estate.original_content_objects",
                "integrity",
                "Original hashes were not checked because content_objects is missing.",
            )
        )
    elif not {"role", "sha256"}.issubset(artifact_columns):
        checks.append(
            _fail(
                "estate.original_content_objects",
                "schema",
                _severity(mode),
                "artifacts.role or artifacts.sha256 is missing.",
            )
        )
    elif "sha256" not in _columns(connection, "content_objects"):
        checks.append(
            _fail(
                "estate.original_content_objects",
                "schema",
                _severity(mode),
                "content_objects.sha256 is missing.",
            )
        )
    else:
        row = connection.execute(
            """
            SELECT
                SUM(CASE WHEN a.sha256 IS NULL OR trim(a.sha256)='' THEN 1 ELSE 0 END)
                    AS missing_hash,
                SUM(CASE WHEN a.sha256 IS NOT NULL AND trim(a.sha256)<>''
                          AND co.sha256 IS NULL THEN 1 ELSE 0 END)
                    AS missing_object,
                COUNT(*) AS originals
            FROM artifacts a
            LEFT JOIN content_objects co ON co.sha256=a.sha256
            WHERE a.role='original'
            """
        ).fetchone()
        missing_hash = int(row["missing_hash"] or 0)
        missing_object = int(row["missing_object"] or 0)
        details = {
            "original_artifacts": int(row["originals"] or 0),
            "originals_without_hash": missing_hash,
            "original_hashes_missing_from_content_objects": missing_object,
        }
        if missing_hash or missing_object:
            checks.append(
                _fail(
                    "estate.original_content_objects",
                    "integrity",
                    _severity(mode),
                    "Some original artifacts lack a hash or matching content object.",
                    **details,
                )
            )
        else:
            checks.append(
                _pass(
                    "estate.original_content_objects",
                    "integrity",
                    "Every original artifact hash has a matching content object.",
                    **details,
                )
            )
    return checks


def _check_deliveries(
    mode: PreflightMode,
    connection: sqlite3.Connection,
    tables: set[str],
) -> list[PreflightCheck]:
    checks: list[PreflightCheck] = []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if "outbox" in tables and {
        "published_at",
        "available_at",
    }.issubset(_columns(connection, "outbox")):
        pending_events = int(
            connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE published_at IS NULL"
            ).fetchone()[0]
        )
        ready_events = int(
            connection.execute(
                """SELECT COUNT(*) FROM outbox
                   WHERE published_at IS NULL AND available_at<=?""",
                (now,),
            ).fetchone()[0]
        )
        future_events = pending_events - ready_events
        details = {
            "unpublished_events": pending_events,
            "ready_unpublished_events": ready_events,
            "future_unpublished_events": future_events,
        }
        if ready_events:
            checks.append(
                _fail(
                    "outbox.pending_events",
                    "consumer",
                    _severity(
                        mode,
                        audit=Severity.WARNING,
                        worker=Severity.WARNING,
                        production=Severity.BLOCKER,
                    ),
                    f"Outbox contains {ready_events} ready unpublished events.",
                    **details,
                )
            )
        else:
            checks.append(
                _pass(
                    "outbox.pending_events",
                    "consumer",
                    "Outbox has no ready unpublished events.",
                    **details,
                )
            )
    else:
        checks.append(
            _skip(
                "outbox.pending_events",
                "consumer",
                "Pending outbox events were not counted because the outbox schema is incomplete.",
            )
        )

    if not {
        "outbox",
        "outbox_subscriptions",
        "outbox_deliveries",
    } <= tables:
        checks.append(
            _skip(
                "consumer.delivery_backlog",
                "consumer",
                "Active delivery states were not counted because consumer schema is incomplete.",
            )
        )
        return checks

    columns = _columns(connection, "outbox_deliveries")
    if "status" not in columns:
        checks.append(
            _fail(
                "consumer.delivery_backlog",
                "schema",
                _severity(mode),
                "outbox_deliveries.status is missing.",
            )
        )
        return checks

    statuses = {
        str(row["status"]).strip().lower(): int(row["count"])
        for row in connection.execute(
            """
            SELECT d.status, COUNT(*) AS count
            FROM outbox_deliveries d
            JOIN outbox o ON o.event_id=d.event_id
            JOIN outbox_subscriptions s
              ON s.consumer_id=d.consumer_id
             AND s.event_type=o.event_type
             AND s.enabled=1
            GROUP BY d.status
            """
        )
    }
    ready = int(
        connection.execute(
            """SELECT COUNT(*)
               FROM outbox_deliveries d
               JOIN outbox o ON o.event_id=d.event_id
               JOIN outbox_subscriptions s
                 ON s.consumer_id=d.consumer_id
                AND s.event_type=o.event_type
                AND s.enabled=1
               WHERE d.status IN ('pending','retryable')
                 AND d.available_at<=?""",
            (now,),
        ).fetchone()[0]
    )
    expired = int(
        connection.execute(
            """SELECT COUNT(*)
               FROM outbox_deliveries d
               JOIN outbox o ON o.event_id=d.event_id
               JOIN outbox_subscriptions s
                 ON s.consumer_id=d.consumer_id
                AND s.event_type=o.event_type
                AND s.enabled=1
               WHERE d.status='running' AND d.locked_until<=?""",
            (now,),
        ).fetchone()[0]
    )
    missing_receipts = int(
        connection.execute(
            """SELECT COUNT(*)
               FROM outbox o
               JOIN outbox_subscriptions s
                 ON s.event_type=o.event_type AND s.enabled=1
               LEFT JOIN outbox_deliveries d
                 ON d.event_id=o.event_id
                AND d.consumer_id=s.consumer_id
               WHERE d.event_id IS NULL"""
        ).fetchone()[0]
    )
    dead = statuses.get("dead", 0) + statuses.get("dead_letter", 0)
    details = {
        "ready_deliveries": ready,
        "expired_running_deliveries": expired,
        "missing_delivery_receipts": missing_receipts,
        "dead_deliveries": dead,
        "statuses": statuses,
    }
    if dead or expired or missing_receipts:
        checks.append(
            _fail(
                "consumer.delivery_backlog",
                "consumer",
                _severity(
                    mode,
                    audit=Severity.WARNING,
                    worker=Severity.ERROR,
                    production=Severity.BLOCKER,
                ),
                "Consumer delivery ledger contains dead, expired, or missing receipts.",
                **details,
            )
        )
    elif ready:
        checks.append(
            _fail(
                "consumer.delivery_backlog",
                "consumer",
                _severity(
                    mode,
                    audit=Severity.WARNING,
                    worker=Severity.WARNING,
                    production=Severity.ERROR,
                ),
                f"Consumer delivery ledger contains {ready} ready deliveries.",
                **details,
            )
        )
    else:
        checks.append(
            _pass(
                "consumer.delivery_backlog",
                "consumer",
                "Consumer delivery ledger has no ready, expired, missing, or dead receipts.",
                **details,
            )
        )
    return checks


def _check_optional_path(
    mode: PreflightMode,
    *,
    name: str,
    label: str,
    path: Path | None,
    expected: str,
) -> PreflightCheck:
    if path is None:
        return _skip(
            name,
            "alpha_go",
            f"{label} is optional and was not configured.",
        )
    exists = path.is_file() if expected == "file" else path.is_dir()
    details = {"path": str(path), "expected": expected}
    if exists:
        return _pass(
            name,
            "alpha_go",
            f"{label} exists.",
            **details,
        )
    return _fail(
        name,
        "alpha_go",
        _severity(
            mode,
            audit=Severity.WARNING,
            worker=Severity.WARNING,
            production=Severity.BLOCKER,
        ),
        f"{label} does not exist as a {expected}.",
        **details,
    )


def run_preflight(
    *,
    mode: str | PreflightMode = PreflightMode.AUDIT,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
    estate_root: str | Path = DOCUMENT_ESTATE_DIR,
    database_path: str | Path | None = None,
    alpha_index_path: str | Path | None = None,
    alpha_corpus_path: str | Path | None = None,
    python_version: Sequence[int] | None = None,
    as_of: date | None = None,
) -> PreflightReport:
    """Run deployment checks without mutating the registry, catalog, or files."""

    selected_mode = mode if isinstance(mode, PreflightMode) else PreflightMode(mode)
    root = Path(estate_root).expanduser().resolve()
    database = (
        Path(database_path).expanduser().resolve()
        if database_path is not None
        else root / "catalog.db"
    )
    registry = Path(registry_path).expanduser().resolve()
    alpha_index = (
        Path(alpha_index_path).expanduser().resolve()
        if alpha_index_path is not None
        else None
    )
    alpha_corpus = (
        Path(alpha_corpus_path).expanduser().resolve()
        if alpha_corpus_path is not None
        else None
    )
    checks: list[PreflightCheck] = [
        _check_python(
            selected_mode,
            python_version if python_version is not None else sys.version_info[:3],
        )
    ]
    checks.extend(
        _check_registry(
            selected_mode,
            registry,
            as_of=as_of or date.today(),
        )
    )

    connection: sqlite3.Connection | None = None
    if not database.is_file():
        checks.append(
            _fail(
                "estate.database",
                "schema",
                _severity(selected_mode),
                "Estate database does not exist as a file.",
                path=str(database),
            )
        )
        for category in ("estate", "acquisition", "outbox", "consumer"):
            checks.append(
                _skip(
                    f"schema.{category}_tables",
                    "schema",
                    "Required tables were not checked because the database is unavailable.",
                )
            )
        checks.append(
            _skip(
                "schema.consumer_contract",
                "schema",
                "Consumer schema contract was not checked because the database is unavailable.",
            )
        )
        checks.extend(
            [
                _skip(
                    "schema.content_objects_object_key",
                    "schema",
                    "object_key was not checked because the database is unavailable.",
                ),
                _skip(
                    "estate.portable_object_keys",
                    "portability",
                    "Portable object keys were not checked because the database is unavailable.",
                ),
                _skip(
                    "estate.absolute_artifact_paths",
                    "portability",
                    "Artifact paths were not checked because the database is unavailable.",
                ),
                _skip(
                    "estate.missing_files",
                    "integrity",
                    "Files were not checked because the database is unavailable.",
                ),
                _skip(
                    "estate.original_content_objects",
                    "integrity",
                    "Original hashes were not checked because the database is unavailable.",
                ),
                _skip(
                    "outbox.pending_events",
                    "consumer",
                    "Outbox events were not counted because the database is unavailable.",
                ),
                _skip(
                    "consumer.delivery_backlog",
                    "consumer",
                    "Delivery states were not counted because the database is unavailable.",
                ),
            ]
        )
    else:
        try:
            connection = _readonly_connection(database)
            tables = _tables(connection)
            checks.append(
                _pass(
                    "estate.database",
                    "schema",
                    "Estate database opened in read-only mode.",
                    path=str(database),
                    sqlite_query_only=bool(
                        connection.execute("PRAGMA query_only").fetchone()[0]
                    ),
                )
            )
            checks.extend(_check_required_tables(selected_mode, tables))
            checks.append(
                _check_consumer_schema_contract(
                    selected_mode, connection, tables
                )
            )
            checks.extend(
                _check_object_keys(selected_mode, connection, tables)
            )
            checks.extend(
                _check_artifacts_and_blobs(
                    selected_mode, connection, tables, root
                )
            )
            checks.extend(_check_deliveries(selected_mode, connection, tables))
        except sqlite3.Error as exc:
            checks.append(
                _fail(
                    "estate.database",
                    "schema",
                    _severity(selected_mode),
                    f"Estate database could not be inspected read-only: {exc}",
                    path=str(database),
                )
            )
        finally:
            if connection is not None:
                connection.close()

    checks.extend(
        [
            _check_optional_path(
                selected_mode,
                name="alpha_go.index_path",
                label="Alpha Go index path",
                path=alpha_index,
                expected="file",
            ),
            _check_optional_path(
                selected_mode,
                name="alpha_go.corpus_path",
                label="Alpha Go corpus path",
                path=alpha_corpus,
                expected="directory",
            ),
        ]
    )
    return PreflightReport(selected_mode, tuple(checks))


def format_human(report: PreflightReport) -> str:
    """Render a compact operator-facing report."""

    state = "READY" if report.ok else "BLOCKED"
    lines = [f"Deployment preflight: {report.mode.value} [{state}]"]
    for check in report.checks:
        lines.append(
            f"[{check.status.value.upper():4}] "
            f"{check.severity.value.upper():7} "
            f"{check.name}: {check.message}"
        )
        if check.details:
            details = ", ".join(
                f"{key}={value}"
                for key, value in sorted(check.details.items())
                if not isinstance(value, (list, dict)) or value
            )
            if details:
                lines.append(f"       {details}")
    lines.append(
        f"Summary: {len(report.checks)} checks, "
        f"{len(report.failed_checks)} findings, "
        f"{len(report.blockers)} blockers, exit={report.exit_code}"
    )
    return "\n".join(lines)


def first_environment_path(
    environment: Mapping[str, str],
    names: Iterable[str],
) -> str | None:
    """Return the first non-empty path from an ordered environment contract."""

    for name in names:
        value = environment.get(name)
        if value and value.strip():
            return value.strip()
    return None


__all__ = [
    "CheckStatus",
    "PreflightCheck",
    "PreflightMode",
    "PreflightReport",
    "REQUIRED_ACQUISITION_TABLES",
    "REQUIRED_CONSUMER_TABLES",
    "REQUIRED_ESTATE_TABLES",
    "REQUIRED_OUTBOX_TABLES",
    "SUPPORTED_PYTHON_MAX_EXCLUSIVE",
    "SUPPORTED_PYTHON_MIN",
    "Severity",
    "first_environment_path",
    "format_human",
    "run_preflight",
]
