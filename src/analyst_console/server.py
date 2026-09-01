"""Loopback-only web server for the BMV Analyst Console launchpad."""
from __future__ import annotations

import argparse
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from estate_bridge import BRIDGE_ENV, ESTATE_ROOT_ENV, REPORTS_VIEW_ENV, load_estate_bridge
from estate_volume import ESTATE_ID_ENV, inspect_estate_environment, read_estate_id

from .jobs import JobManager
from .operations import (
    OPERATIONS,
    OperationError,
    company_catalog,
    validate_confirmation,
)
from .segments import SegmentRequestStore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = Path(__file__).resolve().with_name("static")
DEFAULT_STATE_DIR = PROJECT_ROOT / "data" / "analyst_console"
ALPHA_PORT = 8501
GITHUB_PAGES_ORIGIN = "https://chiavellini.github.io"


def _runtime_environment(project_root: Path) -> dict[str, str]:
    """Return child-process configuration without mutating ``os.environ``."""
    environment = os.environ.copy()
    path = project_root / ".env"
    if path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key:
                environment.setdefault(key, value.strip().strip('"').strip("'"))
    return environment


def _runtime_bridge(project_root: Path, environment: dict[str, str]):
    """Resolve the estate bridge against the launcher's private environment."""
    bridge = load_estate_bridge(
        project_root=project_root,
        config_path=environment.get(BRIDGE_ENV),
    )
    raw_root = environment.get(ESTATE_ROOT_ENV)
    if not raw_root:
        return bridge
    root = Path(raw_root).expanduser()
    if not root.is_absolute():
        root = project_root / root
    root = root.resolve()

    def remap(path: Path) -> Path:
        try:
            return root / path.relative_to(bridge.estate_root)
        except ValueError:
            return path

    reports = environment.get(REPORTS_VIEW_ENV)
    reports_path = Path(reports).expanduser().resolve() if reports else remap(bridge.reports_view_dir)
    return replace(
        bridge,
        estate_root=root,
        catalog_path=remap(bridge.catalog_path),
        objects_dir=remap(bridge.objects_dir),
        reports_view_dir=reports_path,
        uploads_dir=remap(bridge.uploads_dir),
        models_dir=remap(bridge.models_dir),
        alpha_go_embedding_model_path=remap(bridge.alpha_go_embedding_model_path),
        alpha_go_index_path=remap(bridge.alpha_go_index_path),
        alpha_go_corpus_dir=remap(bridge.alpha_go_corpus_dir),
        manifest_path=remap(bridge.manifest_path),
    )


def _persist_estate_location(
    project_root: Path,
    environment: dict[str, str],
    mount_root: Path,
    estate_root: Path,
) -> None:
    """Persist a verified relocated USB without touching unrelated .env keys."""
    replacements = {
        "PDFS_ESTATE_MOUNT_ROOT": str(mount_root),
        "PDFS_DOCUMENT_ESTATE": str(estate_root),
        "PDFS_ALPHA_CORPUS": str(estate_root / "projections" / "alpha-go"),
        "PDFS_ALPHA_INDEX": str(estate_root / "indexes" / "alpha_go.db"),
    }
    path = project_root / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    written: set[str] = set()
    output: list[str] = []
    for line in lines:
        key = line.partition("=")[0].strip() if "=" in line else ""
        if key in replacements:
            output.append(f"{key}={replacements[key]}")
            written.add(key)
        else:
            output.append(line)
    for key, value in replacements.items():
        if key not in written:
            output.append(f"{key}={value}")
    temporary = path.with_suffix(".env.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.replace(path)
    environment.update(replacements)


def _latest(paths: list[Path]) -> Path | None:
    present = [path.resolve() for path in paths if path.is_file()]
    return max(present, key=lambda path: path.stat().st_mtime_ns) if present else None


def _relative_time(path: Path | None) -> str | None:
    if path is None:
        return None
    seconds = max(0, int(time.time() - path.stat().st_mtime))
    if seconds < 60:
        return "ahora"
    if seconds < 3600:
        return f"hace {seconds // 60} min"
    if seconds < 86400:
        return f"hace {seconds // 3600} h"
    return f"hace {seconds // 86400} d"


class AlphaLauncher:
    """Own the optional local Alpha Go Streamlit process."""

    def __init__(self, project_root: Path, state_dir: Path, environment: dict[str, str]):
        self.project_root = project_root
        self.alpha_root = project_root / "alpha-go"
        self.python = self.alpha_root / ".venv312" / "bin" / "python"
        self.state_dir = state_dir
        self.environment = environment
        self.pid_path = state_dir / "alpha.pid"
        self.log_path = state_dir / "alpha.log"
        self._lock = threading.Lock()

    @staticmethod
    def _port_open() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", ALPHA_PORT), timeout=0.15):
                return True
        except OSError:
            return False

    def _pid(self) -> int | None:
        try:
            pid = int(self.pid_path.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return pid
        except (OSError, ValueError):
            return None

    def status(self) -> dict[str, Any]:
        pid = self._pid()
        ready = self._port_open()
        return {
            "installed": self.python.is_file(),
            "running": ready or bool(pid),
            "ready": ready,
            "managed": bool(pid),
            "url": f"http://127.0.0.1:{ALPHA_PORT}",
        }

    def launch(self) -> dict[str, Any]:
        with self._lock:
            if self._port_open() or self._pid():
                return self.status()
            if not self.python.is_file():
                raise OperationError("Alpha Go todavía no está instalado en esta Mac.")
            self.state_dir.mkdir(parents=True, exist_ok=True)
            log = self.log_path.open("a", encoding="utf-8")
            process = subprocess.Popen(
                [
                    str(self.python),
                    "-m",
                    "streamlit",
                    "run",
                    "app/streamlit_app.py",
                    "--server.address",
                    "127.0.0.1",
                    "--server.port",
                    str(ALPHA_PORT),
                    "--browser.gatherUsageStats",
                    "false",
                ],
                cwd=self.alpha_root,
                env=self.environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            log.close()
            self.pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
        # Wait for the web listener, not the heavier search model. Streamlit
        # binds its port early, so the destination opens reliably without
        # turning a full model load into a blocking web request.
        for _attempt in range(60):
            if self._port_open():
                return self.status()
            if process.poll() is not None:
                raise OperationError("Alpha Go no pudo iniciar. Revisa el detalle de la tarea.")
            time.sleep(0.2)
        return self.status()

    def stop(self) -> bool:
        with self._lock:
            pid = self._pid()
            if pid is None:
                return False
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.pid_path.unlink(missing_ok=True)
            return True


class EstateDevice:
    """Connect, validate, and safely eject the USB-native document estate."""

    def __init__(
        self,
        project_root: Path,
        environment: dict[str, str],
        jobs: JobManager,
        alpha: AlphaLauncher,
    ):
        self.project_root = project_root
        self.environment = environment
        self.jobs = jobs
        self.alpha = alpha
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        bridge = _runtime_bridge(self.project_root, self.environment)
        volume = inspect_estate_environment(bridge.estate_root, self.environment)
        problems: list[str] = []
        if not bridge.estate_root.is_dir():
            problems.append(f"No se encontró el Estate en {bridge.estate_root}.")
        if not bridge.catalog_path.is_file():
            problems.append("El catálogo del Estate no está disponible.")
        problems.extend(volume.problems)
        connected = not problems
        mount_root = volume.mount_root or Path(
            self.environment.get("PDFS_ESTATE_MOUNT_ROOT", bridge.estate_root.parent)
        )
        return {
            "connected": connected,
            "message": "Estate conectado" if connected else "USB Estate desconectado",
            "detail": None if connected else (problems[0] if problems else None),
            "volume_name": mount_root.name or "Estate",
            "alpha_index": bridge.alpha_go_index_path.is_file(),
        }

    def _matching_mounted_estate(self) -> tuple[Path, Path] | None:
        configured = _runtime_bridge(self.project_root, self.environment)
        expected = str(self.environment.get(ESTATE_ID_ENV) or "").strip().upper()
        estate_name = configured.estate_root.name
        volumes = Path("/Volumes")
        if not volumes.is_dir():
            return None
        for mount in sorted(volumes.iterdir()):
            for candidate in (mount / estate_name, mount):
                sentinel = candidate / ".bmv-estate-volume.json"
                if not sentinel.is_file():
                    continue
                try:
                    observed = read_estate_id(candidate)
                except Exception:  # noqa: BLE001 - ignore unrelated or malformed USB volumes
                    continue
                if expected and observed != expected:
                    continue
                if (candidate / "catalog.db").is_file():
                    return mount.resolve(), candidate.resolve()
        return None

    def connect(self) -> dict[str, Any]:
        with self._lock:
            current = self.status()
            if current["connected"]:
                return current

            match = self._matching_mounted_estate()
            if match is None:
                volume_name = current["volume_name"]
                subprocess.run(
                    ["diskutil", "mount", volume_name],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
                for _attempt in range(15):
                    match = self._matching_mounted_estate()
                    if match is not None:
                        break
                    time.sleep(0.2)
            if match is None:
                raise OperationError(
                    "No encontré el USB Estate. Conéctalo a la Mac, espera a que aparezca "
                    "en Finder y vuelve a intentar."
                )

            mount_root, estate_root = match
            _persist_estate_location(
                self.project_root,
                self.environment,
                mount_root,
                estate_root,
            )
            result = self.status()
            if not result["connected"]:
                raise OperationError(
                    result.get("detail") or "El USB fue encontrado, pero el Estate no pasó la validación."
                )
            return result

    @staticmethod
    def _disk_info(target: str | Path) -> dict[str, Any]:
        completed = subprocess.run(
            ["diskutil", "info", "-plist", str(target)],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise OperationError("macOS no pudo identificar el USB Estate.")
        try:
            return plistlib.loads(completed.stdout)
        except Exception as exc:  # noqa: BLE001 - system plist boundary
            raise OperationError("macOS devolvió información inválida para el USB Estate.") from exc

    def disconnect(self, confirmation: object) -> dict[str, Any]:
        if confirmation != "EXPULSAR USB":
            raise OperationError('Escribe "EXPULSAR USB" para confirmar.')
        with self._lock:
            if self.jobs.active():
                raise OperationError(
                    "Hay una tarea en proceso. Espera a que termine antes de expulsar el USB."
                )
            status = self.status()
            if not status["connected"]:
                return status

            alpha_status = self.alpha.status()
            if alpha_status["running"]:
                if not alpha_status["managed"]:
                    raise OperationError(
                        "Cierra Alpha Go antes de expulsar el USB Estate."
                    )
                self.alpha.stop()
                for _attempt in range(25):
                    if not self.alpha._port_open():
                        break
                    time.sleep(0.2)
                if self.alpha._port_open():
                    raise OperationError(
                        "Alpha Go todavía está usando el Estate. Intenta de nuevo en unos segundos."
                    )

            bridge = _runtime_bridge(self.project_root, self.environment)
            mount_root = Path(self.environment["PDFS_ESTATE_MOUNT_ROOT"]).resolve()
            info = self._disk_info(mount_root)
            if info.get("Internal") or info.get("BusProtocol") != "USB":
                raise OperationError(
                    "Se bloqueó la expulsión porque el Estate no está en un dispositivo USB externo."
                )
            physical_stores = info.get("APFSPhysicalStores") or []
            if physical_stores:
                physical = physical_stores[0].get("APFSPhysicalStore")
                physical_info = self._disk_info(physical)
                whole = physical_info.get("ParentWholeDisk") or physical_info.get("DeviceIdentifier")
            else:
                whole = info.get("ParentWholeDisk") or info.get("DeviceIdentifier")
            if not whole:
                raise OperationError("No se pudo determinar qué USB expulsar de forma segura.")

            subprocess.run(["sync"], check=True)
            completed = subprocess.run(
                ["diskutil", "eject", f"/dev/{whole}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise OperationError(
                    "macOS no pudo expulsar el USB. Cierra cualquier archivo abierto e intenta de nuevo."
                    + (f" ({detail})" if detail else "")
                )
            return {
                "connected": False,
                "message": "USB Estate expulsado de forma segura",
                "detail": None,
                "volume_name": status["volume_name"],
                "alpha_index": False,
            }


class ConsoleApplication:
    def __init__(self, project_root: Path, state_dir: Path):
        self.project_root = project_root.resolve()
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.environment = _runtime_environment(self.project_root)
        self.environment.setdefault("PYTHONUNBUFFERED", "1")
        self.environment["ANALYST_CONSOLE_STATE_DIR"] = str(self.state_dir)
        self.jobs = JobManager(self.project_root, self.state_dir, self.environment)
        self.segments = SegmentRequestStore(self.project_root, self.state_dir)
        self.alpha = AlphaLauncher(self.project_root, self.state_dir, self.environment)
        self.estate = EstateDevice(
            self.project_root,
            self.environment,
            self.jobs,
            self.alpha,
        )

    def prepare_estate_refresh(self, confirmation: object) -> None:
        """Make the USB index exclusively available to the fleet refresh."""
        operation = OPERATIONS["estate_refresh_all"]
        validate_confirmation(operation, confirmation)
        if self.jobs.active():
            raise OperationError(
                "Hay una tarea en proceso. Espera a que termine antes de actualizar el Estate."
            )
        status = self.estate.status()
        if not status["connected"]:
            raise OperationError(
                status.get("detail")
                or "Conecta el USB Estate antes de actualizar la biblioteca."
            )

        alpha_status = self.alpha.status()
        if not alpha_status["running"]:
            return
        if not alpha_status["managed"]:
            raise OperationError(
                "Cierra Alpha Go antes de actualizar el Estate; está usando su índice."
            )
        self.alpha.stop()
        for _attempt in range(25):
            if not self.alpha._port_open():
                return
            time.sleep(0.2)
        raise OperationError(
            "Alpha Go todavía está usando el Estate. Intenta de nuevo en unos segundos."
        )

    def bootstrap(self) -> dict[str, Any]:
        estate = self.estate.status()
        core = self.project_root / "soft" / "outputs" / "_master" / "soft_coverage_master.html"
        dense = self.project_root / "soft" / "outputs" / "_master" / "soft_coverage_dense.html"
        segment = _latest(list((self.project_root / "outputs" / "latest").glob("*.xlsx")))
        active = sum(job["status"] in {"queued", "running"} for job in self.jobs.list(100))
        return {
            "estate": estate,
            "alpha": self.alpha.status(),
            "nodes": {
                "soft": {
                    "core_available": core.is_file(),
                    "dense_available": dense.is_file(),
                    "updated": _relative_time(_latest([core, dense])),
                },
                "models": {
                    "latest_available": segment is not None,
                    "updated": _relative_time(segment),
                },
            },
            "companies": company_catalog(self.project_root),
            "operations": [item.public() for item in OPERATIONS.values()],
            "jobs": self.jobs.list(),
            "active_jobs": active,
        }

    def known_artifact(self, target: str) -> Path | None:
        paths: list[Path]
        if target == "soft_core":
            paths = [
                self.project_root / "soft" / "outputs" / "_master" / "soft_coverage_master.html"
            ]
        elif target == "soft_dense":
            paths = [
                self.project_root / "soft" / "outputs" / "_master" / "soft_coverage_dense.html"
            ]
        elif target == "latest_model":
            paths = list((self.project_root / "outputs" / "latest").glob("*.xlsx"))
        else:
            return None
        return _latest(paths)

    def open_native(self, path: Path) -> None:
        allowed = (
            (self.project_root / "outputs").resolve(),
            (self.project_root / "soft" / "outputs").resolve(),
        )
        resolved = path.resolve()
        if not resolved.is_file() or not any(resolved.is_relative_to(root) for root in allowed):
            raise OperationError("Ese resultado no está disponible en el lanzador.")
        subprocess.Popen(
            ["open", str(resolved)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


class AnalystConsoleServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, application: ConsoleApplication):
        super().__init__(address, handler)
        self.application = application


class ConsoleHandler(BaseHTTPRequestHandler):
    server: AnalystConsoleServer

    def log_message(self, fmt: str, *args: object) -> None:
        # Keep the launcher quiet; job output has its own durable log.
        if self.server.application.environment.get("ANALYST_CONSOLE_HTTP_LOG") == "1":
            super().log_message(fmt, *args)

    def _json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    def _error(self, message: str, status: HTTPStatus = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"error": message}, status)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise OperationError("La longitud de la solicitud no es válida.") from exc
        if length > 1_048_576:
            raise OperationError("La solicitud es demasiado grande.")
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise OperationError("La solicitud debe contener JSON válido.") from exc
        if not isinstance(payload, dict):
            raise OperationError("La solicitud debe contener un objeto.")
        return payload

    def _allowed_origins(self) -> set[str]:
        configured = str(
            self.server.application.environment.get(
                "ANALYST_CONSOLE_PAGES_ORIGIN", GITHUB_PAGES_ORIGIN
            )
        ).rstrip("/")
        return {
            configured,
            f"http://127.0.0.1:{self.server.server_port}",
            f"http://localhost:{self.server.server_port}",
        }

    def _cors_headers(self) -> None:
        origin = str(self.headers.get("Origin") or "").rstrip("/")
        if origin and origin in self._allowed_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        origin = str(self.headers.get("Origin") or "").rstrip("/")
        if not origin or origin not in self._allowed_origins():
            self._error("Este origen no puede usar el puente local.", HTTPStatus.FORBIDDEN)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, X-Analyst-Console"
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _post_allowed(self) -> bool:
        if self.headers.get("X-Analyst-Console") != "1":
            self._error("Esta acción sólo está disponible desde el lanzador local.", HTTPStatus.FORBIDDEN)
            return False
        origin = self.headers.get("Origin")
        if origin:
            if origin.rstrip("/") not in self._allowed_origins():
                self._error("Esta acción local fue bloqueada.", HTTPStatus.FORBIDDEN)
                return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        route = urlparse(self.path)
        if route.path == "/api/bootstrap":
            try:
                self._json(self.server.application.bootstrap())
            except Exception as exc:  # noqa: BLE001 - local diagnostics boundary
                self._error(f"No se pudo revisar el estado de los proyectos: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route.path == "/api/segments/setup":
            try:
                company = parse_qs(route.query).get("company", [""])[0]
                self._json(self.server.application.segments.setup(company))
            except OperationError as exc:
                self._error(str(exc))
            except Exception as exc:  # noqa: BLE001 - local diagnostics boundary
                self._error(
                    f"No se pudo preparar el formulario de Segmentos: {exc}",
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        if route.path.startswith("/api/jobs/"):
            pieces = route.path.strip("/").split("/")
            job_id = pieces[2] if len(pieces) >= 3 else ""
            job = self.server.application.jobs.get(job_id)
            if job is None:
                self._error("No se encontró la tarea.", HTTPStatus.NOT_FOUND)
            elif len(pieces) == 4 and pieces[3] == "log":
                self._json({"log": self.server.application.jobs.log_text(job_id)})
            else:
                self._json(job.public())
            return
        self._serve_static(route.path)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if not self._post_allowed():
            return
        route = urlparse(self.path)
        try:
            body = self._body()
            if route.path == "/api/jobs":
                operation_key = str(body.get("operation") or "")
                if operation_key == "estate_refresh_all":
                    self.server.application.prepare_estate_refresh(
                        body.get("confirmation")
                    )
                job = self.server.application.jobs.create(
                    operation_key,
                    body.get("params") if isinstance(body.get("params"), dict) else {},
                    body.get("confirmation"),
                )
                self._json(job, HTTPStatus.ACCEPTED)
                return
            if route.path == "/api/segments/jobs":
                request_id = self.server.application.segments.prepare(body)
                job = self.server.application.jobs.create(
                    "segments_model", {"request": request_id}
                )
                self._json(job, HTTPStatus.ACCEPTED)
                return
            if route.path == "/api/estate/connect":
                self._json(self.server.application.estate.connect(), HTTPStatus.ACCEPTED)
                return
            if route.path == "/api/estate/disconnect":
                self._json(
                    self.server.application.estate.disconnect(body.get("confirmation")),
                    HTTPStatus.ACCEPTED,
                )
                return
            if route.path == "/api/alpha/launch":
                self._json(self.server.application.alpha.launch(), HTTPStatus.ACCEPTED)
                return
            if route.path == "/api/open":
                path = self.server.application.known_artifact(str(body.get("target") or ""))
                if path is None:
                    raise OperationError("Este proyecto todavía no tiene un resultado para abrir.")
                self.server.application.open_native(path)
                self._json({"opened": True})
                return
            if route.path.startswith("/api/jobs/") and route.path.endswith("/open"):
                job_id = route.path.strip("/").split("/")[2]
                path = self.server.application.jobs.artifact(job_id, int(body.get("index", 0)))
                if path is None:
                    raise OperationError("Esta tarea no tiene un resultado para abrir.")
                self.server.application.open_native(path)
                self._json({"opened": True})
                return
            self._error("No se encontró la ruta.", HTTPStatus.NOT_FOUND)
        except OperationError as exc:
            self._error(str(exc))
        except Exception as exc:  # noqa: BLE001 - keep the local UI usable on unexpected failures
            self._error(f"No se pudo completar la acción: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def _serve_static(self, route: str) -> None:
        name = "index.html" if route in {"", "/"} else route.lstrip("/")
        requested = (STATIC_ROOT / name).resolve()
        if not requested.is_relative_to(STATIC_ROOT.resolve()) or not requested.is_file():
            requested = STATIC_ROOT / "index.html"
        data = requested.read_bytes()
        content_type = mimetypes.guess_type(requested.name)[0] or "application/octet-stream"
        if requested.suffix == ".webmanifest":
            content_type = "application/manifest+json"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)


def create_server(
    *,
    project_root: Path = PROJECT_ROOT,
    state_dir: Path = DEFAULT_STATE_DIR,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> AnalystConsoleServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("El lanzador es local y sólo puede usar la interfaz loopback.")
    application = ConsoleApplication(project_root, state_dir)
    return AnalystConsoleServer((host, port), ConsoleHandler, application)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--open", action="store_true", help="abrir el lanzador en Safari")
    args = parser.parse_args(argv)
    server = create_server(host=args.host, port=args.port, state_dir=args.state_dir)
    url = f"http://{args.host}:{server.server_port}"
    print(f"BMV Analyst Console: {url}", flush=True)
    if args.open:
        subprocess.Popen(["open", "-a", "Safari", url])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
