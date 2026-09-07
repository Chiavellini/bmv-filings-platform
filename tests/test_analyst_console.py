from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from src.analyst_console.jobs import JobManager
from src.analyst_console.operations import (
    Operation,
    OperationError,
    company_catalog,
    operation_command,
    validate_confirmation,
)
from src.analyst_console.server import ConsoleApplication, EstateDevice, create_server
from src.analyst_console.segments import SegmentRequestStore, metric_catalog


ROOT = Path(__file__).resolve().parents[1]


def _pretend_project_pythons_are_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.is_file

    def is_file(path: Path) -> bool:
        if (
            path.name == "python"
            and path.parent.name == "bin"
            and path.parent.parent.name in {".venv", ".venv312"}
        ):
            return True
        return original(path)

    monkeypatch.setattr(Path, "is_file", is_file)


def test_company_operations_accept_only_catalog_slugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_project_pythons_are_installed(monkeypatch)
    catalog = company_catalog(ROOT)
    assert {"walmex", "herdez"}.issubset({row["slug"] for row in catalog["soft"]})
    assert "herdez" in {row["slug"] for row in catalog["segments"]}
    assert len(catalog["segments"]) >= 175
    assert {"ac", "femsa", "kof", "tiendas_3b"}.issubset(
        {row["slug"] for row in catalog["segments"]}
    )

    operation, argv, cwd = operation_command(
        ROOT, "soft_model", {"company": "walmex"}
    )
    assert operation.key == "soft_model"
    assert argv[-1] == "inputs/walmex.md"
    assert cwd == ROOT / "soft"
    assert all(";" not in token for token in argv)

    with pytest.raises(OperationError, match="Elige una empresa"):
        operation_command(ROOT, "soft_model", {"company": "walmex; touch bad"})

    refresh, refresh_argv, refresh_cwd = operation_command(ROOT, "estate_refresh_all")
    assert refresh.mutates is True
    assert refresh.confirmation == "ACTUALIZAR ESTATE"
    assert refresh_argv[-1] == "scripts/refresh_analyst_estate.py"
    assert refresh_cwd == ROOT


def test_segment_metric_catalog_is_universal_then_company_specific() -> None:
    generic = {row["key"] for row in metric_catalog(ROOT, "bbajio")}
    assert len(generic) == 61
    assert {"revenue", "cfo", "total_assets"}.issubset(generic)
    assert {"clientes_activos", "revenue_sports"}.isdisjoint(generic)

    herdez = {row["key"] for row in metric_catalog(ROOT, "herdez")}
    assert {"revenue", "cfo", "ns_domestic"}.issubset(herdez)
    assert {"clientes_activos", "revenue_sports"}.isdisjoint(herdez)

    expected = {
        "ac": "ac_sales_mexico",
        "becle": "revenue_us_canada",
        "femsa": "femsa_am_revenue",
        "kimber": "sales_yoy_exact",
        "kof": "kof_revenue_brazil",
        "lab": "lab_revenue_mexico",
        "tiendas_3b": "total_stores",
    }
    for company, key in expected.items():
        assert key in {row["key"] for row in metric_catalog(ROOT, company)}


def test_mutating_operations_require_exact_confirmation() -> None:
    operation = Operation(
        "writer", "Writer", "estate", "writes", mutates=True, confirmation="WRITE ESTATE"
    )
    with pytest.raises(OperationError, match="WRITE ESTATE"):
        validate_confirmation(operation, "write estate")
    validate_confirmation(operation, "WRITE ESTATE")


def _wait_for(manager: JobManager, job_id: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job and job.status not in {"queued", "running"}:
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_job_manager_runs_argv_and_persists_log(tmp_path: Path, monkeypatch) -> None:
    operation = Operation("test", "Test operation", "test", "test operation")

    def command(*_args, **_kwargs):
        return operation, ["/usr/bin/printf", "job finished\n"], tmp_path

    monkeypatch.setattr("src.analyst_console.jobs.operation_command", command)
    manager = JobManager(ROOT, tmp_path / "state", {})
    created = manager.create("test", {})
    job = _wait_for(manager, created["id"])

    assert job.status == "completed"
    assert job.exit_code == 0
    assert job.summary is None
    assert job.detail is None
    assert "job finished" in manager.log_text(job.id)
    assert (tmp_path / "state" / "jobs" / f"{job.id}.json").is_file()
    assert "summary" in job.public() and "detail" in job.public()


def test_job_manager_reads_extract_summary_and_failure_detail(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    summary_dir = project / "outputs" / "extractor" / "0123456789abcdef"
    summary_dir.mkdir(parents=True)
    (summary_dir / "a_observaciones.xlsx").write_bytes(b"xlsx")
    summary = {"found": 2, "requested": 3, "missing": [{"key": "ebitda", "label": "EBITDA"}]}
    (summary_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    commands = {
        "pdf_extract": ["/usr/bin/printf", "Periodo: 2025-2T\n"],
        "boom": ["/bin/sh", "-c", 'echo "working…"; echo "ERROR: No se pudo leer el PDF" >&2; exit 1'],
        "silent": ["/bin/sh", "-c", "exit 3"],
    }

    def command(_root, key, *_args, **_kwargs):
        return Operation(key, key, "models", key), commands[key], tmp_path

    monkeypatch.setattr("src.analyst_console.jobs.operation_command", command)
    manager = JobManager(project, tmp_path / "state", {})

    extract = _wait_for(manager, manager.create("pdf_extract", {"request": "0123456789abcdef"})["id"])
    assert extract.status == "completed"
    assert extract.summary == summary
    assert extract.message == "2 de 3 métricas encontradas"
    assert extract.public()["summary"]["found"] == 2

    failed = _wait_for(manager, manager.create("boom", {})["id"])
    assert failed.status == "failed"
    assert failed.detail == "No se pudo leer el PDF"
    assert failed.message == "Requiere atención"

    silent = _wait_for(manager, manager.create("silent", {})["id"])
    assert silent.status == "failed"
    assert silent.detail is None

    # Job files written before the summary/detail fields existed still load.
    legacy = {
        "id": "legacy000001", "operation": "estate_check", "label": "Revisar", "node": "estate",
        "status": "completed", "created_at": "2026-01-01T00:00:00+00:00", "started_at": None,
        "finished_at": None, "exit_code": 0, "message": "Completado", "params": {}, "artifacts": [],
    }
    (tmp_path / "state" / "jobs" / "legacy000001.json").write_text(json.dumps(legacy), encoding="utf-8")
    reloaded = JobManager(project, tmp_path / "state", {})
    assert reloaded.get("legacy000001") is not None
    assert reloaded.get("legacy000001").summary is None


@pytest.fixture
def console_server(tmp_path: Path):
    server = create_server(project_root=ROOT, state_dir=tmp_path / "state", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_console_serves_launchpad_and_bootstrap(console_server: str) -> None:
    with urlopen(f"{console_server}/", timeout=3) as response:
        html = response.read().decode("utf-8")
    assert "Todo lo que necesitas" not in html
    assert "Cada herramienta conserva su propio espacio" not in html
    assert "Lanzadores de proyectos" in html
    assert 'href="./app.css?v=12"' in html
    assert 'src="./app.js?v=12"' in html
    assert html.count('class="module-option" data-dialog=') == 2
    assert 'data-open="latest_extract"' in html
    assert 'id="extractor-file" type="file" accept="application/pdf,.pdf" multiple' in html
    assert 'id="extractor-files"' in html
    assert "EXTRACTOR" in html
    assert "FÁBRICA DE MODELOS" not in html
    assert 'data-dialog="extractor-dialog"' in html
    assert "Extraer de un PDF" in html
    assert 'id="extractor-file" type="file"' in html
    assert 'name="extractor-format"' in html
    # Closing a dialog with × must never submit its form (it used to launch a job).
    assert html.count('class="dialog-close" value="cancel" aria-label="Cerrar" type="button"') == 4
    assert 'aria-label="Cerrar">' not in html
    assert 'content="http://127.0.0.1:8765"' in html
    assert 'href="./" aria-label="Inicio del lanzador BMV"' in html
    assert "connect-src http://127.0.0.1:8765" in html
    assert "#analyst-launchpad" in html
    assert "Pipeline" not in html
    assert "Earnings Study" not in html
    assert "Generar hoja de segmentos" in html
    assert "Tipo de modelo" not in html
    assert "Clave canónica" not in html
    assert "Opciones de descarga" not in html
    assert "Buscar una métrica" in html
    assert "ACTIVIDAD RECIENTE" not in html
    assert "Qué está haciendo el lanzador" not in html
    assert 'data-confirm-operation="estate_refresh_all"' in html
    assert "Actualizar biblioteca completa" in html

    with urlopen(f"{console_server}/api/bootstrap", timeout=3) as response:
        payload = json.load(response)
    assert set(payload["nodes"]) == {"soft", "models"}
    assert len(payload["companies"]["soft"]) > 100
    assert payload["alpha"]["url"] == "http://127.0.0.1:8501"

    with urlopen(f"{console_server}/api/segments/setup?company=soriana", timeout=3) as response:
        setup = json.load(response)
    assert setup["preset"]["company"] == "Organizacion Soriana"
    assert setup["preset"]["ir_url"].startswith("https://")
    assert any(row["key"] == "revenue" for row in setup["metrics"])
    assert setup["preset"]["sections"][0]["rows"][0] == {
        "kind": "metric",
        "label": "Total Income",
        "key": "revenue",
    }


def test_segments_request_creates_strict_markdown_and_analyst_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pretend_project_pythons_are_installed(monkeypatch)
    store = SegmentRequestStore(ROOT, tmp_path / "state")
    request_id = store.prepare(
        {
            "template_company": "soriana",
            "sections": [
                {
                    "name": "Resultados",
                    "rows": [
                        {"kind": "metric", "label": "Ingresos", "key": "revenue"},
                        {"kind": "yoy", "label": "ignorado", "key": "bad"},
                    ],
                }
            ],
        }
    )
    request_dir = tmp_path / "state" / "requests" / request_id
    assert "- Ingresos {revenue}" in (request_dir / "input.md").read_text(encoding="utf-8")
    assert "Issuer-Slug: soriana" in (request_dir / "input.md").read_text(encoding="utf-8")
    assert "- YoY" in (request_dir / "input.md").read_text(encoding="utf-8")
    assert (request_dir / "analyst_metrics.csv").read_text(encoding="utf-8").splitlines() == [
        "section,label,key",
        "Resultados,Ingresos,revenue",
        "Resultados,YoY,",
    ]

    operation, argv, cwd = operation_command(
        ROOT,
        "segments_model",
        {"request": request_id},
        environment={"ANALYST_CONSOLE_STATE_DIR": str(tmp_path / "state")},
    )
    assert operation.key == "segments_model"
    assert argv[1:3] == ["scripts/build_segments.py", str(request_dir / "input.md")]
    assert "--analyst-metrics" in argv
    assert "--estate-only" in argv
    assert "--force-download" not in argv
    assert cwd == ROOT


def test_console_blocks_cross_site_or_unmarked_posts(console_server: str) -> None:
    request = Request(
        f"{console_server}/api/jobs",
        data=b'{"operation":"estate_check"}',
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(HTTPError) as blocked:
        urlopen(request, timeout=3)
    assert blocked.value.code == 403

    request = Request(
        f"{console_server}/api/jobs",
        data=b'{"operation":"not-an-operation"}',
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Analyst-Console": "1",
            "Origin": "https://example.com",
        },
    )
    with pytest.raises(HTTPError) as blocked_origin:
        urlopen(request, timeout=3)
    assert blocked_origin.value.code == 403


def test_github_pages_origin_can_reach_local_bridge(console_server: str) -> None:
    pages_origin = "https://chiavellini.github.io"
    request = Request(
        f"{console_server}/api/bootstrap",
        headers={"Origin": pages_origin, "X-Analyst-Console": "1"},
    )
    with urlopen(request, timeout=3) as response:
        assert response.headers["Access-Control-Allow-Origin"] == pages_origin
        assert json.load(response)["alpha"]["url"] == "http://127.0.0.1:8501"

    preflight = Request(
        f"{console_server}/api/jobs",
        method="OPTIONS",
        headers={
            "Origin": pages_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-analyst-console",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    with urlopen(preflight, timeout=3) as response:
        assert response.status == 204
        assert response.headers["Access-Control-Allow-Origin"] == pages_origin
        assert response.headers["Access-Control-Allow-Private-Network"] == "true"
        assert "X-Analyst-Console" in response.headers["Access-Control-Allow-Headers"]

    rejected_operation = Request(
        f"{console_server}/api/jobs",
        data=b'{"operation":"not-an-operation"}',
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Analyst-Console": "1",
            "Origin": pages_origin,
        },
    )
    with pytest.raises(HTTPError) as rejected:
        urlopen(rejected_operation, timeout=3)
    assert rejected.value.code == 400
    assert rejected.value.headers["Access-Control-Allow-Origin"] == pages_origin


def test_console_rejects_non_loopback_binding(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="local"):
        create_server(host="0.0.0.0", state_dir=tmp_path, port=0)


def test_estate_disconnect_requires_phrase_and_no_active_jobs(tmp_path: Path) -> None:
    class Jobs:
        def active(self):
            return [object()]

    class Alpha:
        pass

    device = EstateDevice(ROOT, {}, Jobs(), Alpha())
    with pytest.raises(OperationError, match="EXPULSAR USB"):
        device.disconnect("expulsar usb")
    with pytest.raises(OperationError, match="tarea en proceso"):
        device.disconnect("EXPULSAR USB")


def test_estate_connect_explains_when_usb_is_absent(monkeypatch) -> None:
    class Jobs:
        def active(self):
            return []

    class Alpha:
        pass

    device = EstateDevice(ROOT, {}, Jobs(), Alpha())
    monkeypatch.setattr(
        device,
        "status",
        lambda: {
            "connected": False,
            "volume_name": "Estate",
            "message": "USB Estate desconectado",
        },
    )
    monkeypatch.setattr(device, "_matching_mounted_estate", lambda: None)
    monkeypatch.setattr("src.analyst_console.server.subprocess.run", lambda *_a, **_kw: None)
    monkeypatch.setattr("src.analyst_console.server.time.sleep", lambda *_a: None)
    with pytest.raises(OperationError, match="No encontré el USB Estate"):
        device.connect()


def test_mass_refresh_requires_estate_and_exclusive_alpha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = ConsoleApplication(ROOT, tmp_path / "state")
    monkeypatch.setattr(application.jobs, "active", lambda: [])

    monkeypatch.setattr(
        application.estate,
        "status",
        lambda: {"connected": False, "detail": "USB ausente"},
    )
    with pytest.raises(OperationError, match="USB ausente"):
        application.prepare_estate_refresh("ACTUALIZAR ESTATE")

    monkeypatch.setattr(
        application.estate,
        "status",
        lambda: {"connected": True, "detail": None},
    )

    class Alpha:
        stopped = False

        def status(self):
            return {"running": True, "managed": True}

        def stop(self):
            self.stopped = True

        @staticmethod
        def _port_open():
            return False

    alpha = Alpha()
    application.alpha = alpha
    with pytest.raises(OperationError, match="ACTUALIZAR ESTATE"):
        application.prepare_estate_refresh("actualizar estate")
    assert alpha.stopped is False

    application.prepare_estate_refresh("ACTUALIZAR ESTATE")
    assert alpha.stopped is True
