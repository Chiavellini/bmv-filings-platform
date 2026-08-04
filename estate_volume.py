"""Fail-closed identity checks for a mounted portable document estate.

The filesystem path is configuration; the sentinel UUID is identity.  Runtime
writers must validate both so a missing USB mount cannot silently redirect
writes into an ordinary directory created at the expected mount path.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import uuid


MOUNT_ROOT_ENV = "PDFS_ESTATE_MOUNT_ROOT"
ESTATE_ID_ENV = "PDFS_ESTATE_ID"
SENTINEL_NAME = ".bmv-estate-volume.json"


class EstateVolumeError(RuntimeError):
    """The configured estate mount is absent, unsafe, or has the wrong ID."""


@dataclass(frozen=True)
class EstateVolumeStatus:
    estate_root: Path
    mount_root: Path | None
    expected_estate_id: str | None
    observed_estate_id: str | None
    problems: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return not self.problems

    def require_healthy(self) -> None:
        if self.problems:
            raise EstateVolumeError("; ".join(self.problems))


def _normalized_uuid(raw: str, *, field: str) -> str:
    try:
        return str(uuid.UUID(raw.strip())).upper()
    except (AttributeError, ValueError) as exc:
        raise EstateVolumeError(f"{field} must be a UUID") from exc


def read_estate_id(estate_root: str | Path) -> str:
    """Read and validate the bundle sentinel without mutating the estate."""
    root = Path(estate_root).expanduser().resolve()
    sentinel = root / SENTINEL_NAME
    try:
        payload = json.loads(sentinel.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EstateVolumeError(f"estate sentinel is missing: {sentinel}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise EstateVolumeError(f"estate sentinel is unreadable: {sentinel}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EstateVolumeError(f"estate sentinel must contain a JSON object: {sentinel}")
    if payload.get("schema_version") != 1:
        raise EstateVolumeError(
            f"unsupported estate sentinel schema in {sentinel}: "
            f"{payload.get('schema_version')!r}"
        )
    return _normalized_uuid(str(payload.get("estate_id", "")), field="sentinel estate_id")


def inspect_estate_volume(
    estate_root: str | Path,
    *,
    mount_root: str | Path | None = None,
    expected_estate_id: str | None = None,
    require_mountpoint: bool = True,
) -> EstateVolumeStatus:
    """Inspect one configured estate and return every identity problem."""
    root = Path(estate_root).expanduser().resolve()
    problems: list[str] = []
    mount: Path | None = None
    expected: str | None = None
    observed: str | None = None

    if mount_root is not None and str(mount_root).strip():
        candidate = Path(mount_root).expanduser()
        if not candidate.is_absolute():
            problems.append(f"{MOUNT_ROOT_ENV} must be an absolute path")
        else:
            mount = candidate.resolve()
            if not mount.is_dir():
                problems.append(f"estate mount root does not exist: {mount}")
            elif require_mountpoint and not os.path.ismount(mount):
                problems.append(f"estate mount root is not a mounted filesystem: {mount}")
            try:
                root.relative_to(mount)
            except ValueError:
                problems.append(f"estate root is outside configured mount root: {root}")

    if expected_estate_id is not None and expected_estate_id.strip():
        try:
            expected = _normalized_uuid(expected_estate_id, field=ESTATE_ID_ENV)
        except EstateVolumeError as exc:
            problems.append(str(exc))

    if (mount is None) != (expected is None):
        problems.append(
            f"{MOUNT_ROOT_ENV} and {ESTATE_ID_ENV} must be configured together"
        )

    if not root.is_dir():
        problems.append(f"estate root does not exist: {root}")
    elif mount is not None or expected is not None:
        try:
            observed = read_estate_id(root)
        except EstateVolumeError as exc:
            problems.append(str(exc))
        if expected is not None and observed is not None and observed != expected:
            problems.append(
                f"estate ID mismatch: expected {expected}, observed {observed}"
            )

    return EstateVolumeStatus(
        estate_root=root,
        mount_root=mount,
        expected_estate_id=expected,
        observed_estate_id=observed,
        problems=tuple(dict.fromkeys(problems)),
    )


def inspect_estate_environment(
    estate_root: str | Path,
    environment: dict[str, str] | os._Environ[str] | None = None,
    *,
    require_mountpoint: bool = True,
) -> EstateVolumeStatus:
    env = os.environ if environment is None else environment
    return inspect_estate_volume(
        estate_root,
        mount_root=env.get(MOUNT_ROOT_ENV),
        expected_estate_id=env.get(ESTATE_ID_ENV),
        require_mountpoint=require_mountpoint,
    )


__all__ = [
    "ESTATE_ID_ENV",
    "MOUNT_ROOT_ENV",
    "SENTINEL_NAME",
    "EstateVolumeError",
    "EstateVolumeStatus",
    "inspect_estate_environment",
    "inspect_estate_volume",
    "read_estate_id",
]
