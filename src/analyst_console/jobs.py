"""Durable background subprocess jobs for the Analyst Console."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import threading
import uuid
from typing import Any

from .operations import Operation, operation_command, validate_confirmation


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Job:
    id: str
    operation: str
    label: str
    node: str
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    message: str = "En espera"
    params: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["has_artifact"] = bool(self.artifacts)
        payload.pop("artifacts", None)
        return payload


class JobManager:
    """Run allowlisted commands without blocking the web server."""

    def __init__(self, project_root: Path, state_dir: Path, environment: dict[str, str]):
        self.project_root = project_root
        self.state_dir = state_dir
        self.environment = environment
        self.jobs_dir = state_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._writer_lock = threading.Lock()
        self._load_existing()

    def _load_existing(self) -> None:
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**payload)
                if job.status in {"queued", "running"}:
                    job.status = "interrupted"
                    job.finished_at = _now()
                    job.message = "El lanzador se cerró antes de terminar esta tarea."
                    self._write(job)
                self._jobs[job.id] = job
            except (OSError, ValueError, TypeError):
                continue

    def _write(self, job: Job) -> None:
        destination = self.jobs_dir / f"{job.id}.json"
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(job), indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)

    def list(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return [item.public() for item in jobs[:limit]]

    def active(self) -> list[Job]:
        """Return jobs that may still have project or estate files open."""
        with self._lock:
            return [
                item for item in self._jobs.values()
                if item.status in {"queued", "running"}
            ]

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def log_text(self, job_id: str, max_bytes: int = 120_000) -> str:
        path = self.jobs_dir / f"{job_id}.log"
        if not path.is_file():
            return ""
        data = path.read_bytes()
        if len(data) > max_bytes:
            data = b"[Se omitio el inicio del registro]\n" + data[-max_bytes:]
        return data.decode("utf-8", errors="replace")

    def create(
        self,
        key: str,
        params: dict[str, Any] | None,
        confirmation: object = None,
    ) -> dict[str, Any]:
        operation, argv, cwd = operation_command(
            self.project_root,
            key,
            params,
            environment=self.environment,
        )
        validate_confirmation(operation, confirmation)
        job = Job(
            id=uuid.uuid4().hex[:12],
            operation=key,
            label=operation.label,
            node=operation.node,
            params=params or {},
        )
        with self._lock:
            self._jobs[job.id] = job
            self._write(job)
        thread = threading.Thread(
            target=self._run,
            args=(job, operation, argv, cwd),
            name=f"analyst-job-{job.id}",
            daemon=True,
        )
        thread.start()
        return job.public()

    def _run(self, job: Job, operation: Operation, argv: list[str], cwd: Path) -> None:
        guard = self._writer_lock if operation.mutates else _NullLock()
        log_path = self.jobs_dir / f"{job.id}.log"
        with guard:
            with self._lock:
                job.status = "running"
                job.started_at = _now()
                job.message = "En proceso"
                self._write(job)
            before = self._artifact_snapshot()
            try:
                with log_path.open("w", encoding="utf-8") as log:
                    log.write(f"{operation.label}\nInicio {job.started_at}\n\n")
                    log.flush()
                    completed = subprocess.run(
                        argv,
                        cwd=cwd,
                        env=self.environment,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        text=True,
                        check=False,
                    )
                artifacts = self._artifacts_for(job, before) if completed.returncode == 0 else []
                with self._lock:
                    job.exit_code = completed.returncode
                    job.status = "completed" if completed.returncode == 0 else "failed"
                    job.message = (
                        "Listo para abrir" if completed.returncode == 0 and artifacts
                        else "Completado" if completed.returncode == 0
                        else "Requiere atención"
                    )
                    job.artifacts = [str(path) for path in artifacts]
                    job.finished_at = _now()
                    self._write(job)
            except Exception as exc:  # noqa: BLE001 - converted into operator-visible job status
                with log_path.open("a", encoding="utf-8") as log:
                    log.write(f"\n{type(exc).__name__}: {exc}\n")
                with self._lock:
                    job.status = "failed"
                    job.message = f"No se pudo ejecutar: {type(exc).__name__}"
                    job.finished_at = _now()
                    self._write(job)

    def _artifact_snapshot(self) -> dict[Path, int]:
        result: dict[Path, int] = {}
        for base in (self.project_root / "outputs", self.project_root / "soft" / "outputs"):
            if not base.is_dir():
                continue
            for path in base.rglob("*"):
                if path.is_file() and path.suffix.lower() in {".xlsx", ".html", ".md", ".json"}:
                    try:
                        result[path.resolve()] = path.stat().st_mtime_ns
                    except OSError:
                        pass
        return result

    def _artifacts_for(self, job: Job, before: dict[Path, int]) -> list[Path]:
        candidates: list[Path] = []
        if job.operation == "soft_model":
            candidates = list((self.project_root / "soft" / "outputs").glob("*/excel/*.xlsx"))
        elif job.operation == "segments_model":
            candidates = list((self.project_root / "outputs" / "latest").glob("*.xlsx"))
        elif job.operation == "soft_master":
            candidates = [
                self.project_root / "soft" / "outputs" / "_master" / "soft_coverage_master.html",
                self.project_root / "soft" / "outputs" / "_master" / "soft_coverage_dense.html",
            ]

        changed = []
        for path in candidates:
            if not path.is_file():
                continue
            resolved = path.resolve()
            try:
                mtime = resolved.stat().st_mtime_ns
            except OSError:
                continue
            if before.get(resolved) != mtime or job.operation in {
                "soft_model",
                "segments_model",
                "soft_master",
            }:
                changed.append(resolved)
        return sorted(changed, key=lambda item: item.stat().st_mtime_ns, reverse=True)[:4]

    def artifact(self, job_id: str, index: int = 0) -> Path | None:
        job = self.get(job_id)
        if job is None or index < 0 or index >= len(job.artifacts):
            return None
        path = Path(job.artifacts[index]).resolve()
        allowed_roots = [
            (self.project_root / "outputs").resolve(),
            (self.project_root / "soft" / "outputs").resolve(),
        ]
        if not path.is_file() or not any(path.is_relative_to(root) for root in allowed_roots):
            return None
        return path


class _NullLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False
