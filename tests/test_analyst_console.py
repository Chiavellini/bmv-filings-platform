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
from src.analyst_console.server import EstateDevice, create_server
from src.analyst_console.segments import SegmentRequestStore


ROOT = Path(__file__).resolve().parents[1]


def test_company_operations_accept_only_catalog_slugs() -> None:
    catalog = company_catalog(ROOT)
    assert {"walmex", "herdez"}.issubset({row["slug"] for row in catalog["soft"]})
    assert "herdez" in {row["slug"] for row in catalog["segments"]}

    operation, argv, cwd = operation_command(
        ROOT, "soft_model", {"company": "walmex"}
    )
    assert operation.key == "soft_model"
    assert argv[-1] == "inputs/walmex.md"
    assert cwd == ROOT / "soft"
    assert all(";" not in token for token in argv)

    with pytest.raises(OperationError, match="Elige una empresa"):
        operation_command(ROOT, "soft_model", {"company": "walmex; touch bad"})


def test_mutating_operations_require_exact_confirmation() -> None:
    operation = Operation(
        "writer", "Writer", "estate", "writes", mutates=True, confirmation="WRITE ESTATE"
    )
    with pytest.raises(OperationError, match="WRITE ESTATE"):
        validate_confirmation(operation, "write estate")
    validate_confirmation(operation, "WRITE ESTATE")


def test_job_manager_runs_argv_and_persists_log(tmp_path: Path, monkeypatch) -> None:
    operation = Operation("test", "Test operation", "test", "test operation")

    def command(*_args, **_kwargs):
        return operation, ["/usr/bin/printf", "job finished\n"], tmp_path

    monkeypatch.setattr("src.analyst_console.jobs.operation_command", command)
    manager = JobManager(ROOT, tmp_path / "state", {})
    created = manager.create("test", {})

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = manager.get(created["id"])
        if job and job.status not in {"queued", "running"}:
            break
        time.sleep(0.02)

    assert job is not None
    assert job.status == "completed"
    assert job.exit_code == 0
    assert "job finished" in manager.log_text(job.id)
    assert (tmp_path / "state" / "jobs" / f"{job.id}.json").is_file()


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
    assert 'href="./app.css?v=5"' in html
    assert 'src="./app.js?v=5"' in html
    assert 'content="http://127.0.0.1:8765"' in html
    assert 'href="./" aria-label="Inicio del lanzador BMV"' in html
    assert "connect-src http://127.0.0.1:8765" in html
    assert "#analyst-launchpad" in html
    assert "Pipeline" not in html
    assert "Earnings Study" not in html

    with urlopen(f"{console_server}/api/bootstrap", timeout=3) as response:
        payload = json.load(response)
    assert set(payload["nodes"]) == {"soft", "models"}
    assert len(payload["companies"]["soft"]) > 100
    assert payload["alpha"]["url"] == "http://127.0.0.1:8501"

    with urlopen(f"{console_server}/api/segments/setup?company=soriana", timeout=3) as response:
        setup = json.load(response)
    assert setup["preset"]["company"] == "Soriana"
    assert setup["preset"]["ir_url"].startswith("https://")
    assert any(row["key"] == "revenue" for row in setup["metrics"])
    assert setup["preset"]["sections"][0]["rows"][0] == {
        "kind": "metric",
        "label": "Total Income",
        "key": "revenue",
    }


def test_segments_request_creates_strict_markdown_and_analyst_contract(tmp_path: Path) -> None:
    store = SegmentRequestStore(ROOT, tmp_path / "state")
    request_id = store.prepare(
        {
            "template_company": "soriana",
            "company": "Soriana",
            "ticker": "SORIANA",
            "ir_url": "https://example.com/inversionistas",
            "max_reports": 40,
            "force_download": True,
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
    assert "--force-download" in argv
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
