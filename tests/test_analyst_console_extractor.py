"""Private PDF uploads and extraction requests exposed by the launchpad bridge."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from src.analyst_console import extractor as extractor_module
from src.analyst_console.extractor import (
    ExtractorRequestStore,
    latest_extract,
    resolve_extractor_request,
    sanitize_filename,
)
from src.analyst_console.operations import OperationError, operation_command
from src.analyst_console.server import create_server


ROOT = Path(__file__).resolve().parents[1]
PDF = b"%PDF-1.4\n% not a real document but enough for the upload gate\n"
PAGES_ORIGIN = "https://chiavellini.github.io"


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


def _base_request(upload: str) -> dict:
    return {
        "upload": upload,
        "company": "",
        "metrics": ["revenue"],
        "custom_metrics": [],
        "period": "",
        "format": "both",
        "read_tables": True,
    }


def test_sanitize_filename_keeps_only_a_boring_pdf_basename() -> None:
    assert sanitize_filename("../../etc/passwd.pdf") == "passwd.pdf"
    assert sanitize_filename("C:\\Users\\x\\Reporte 2T25.PDF") == "Reporte 2T25.pdf"
    assert sanitize_filename("weird<>|name.pdf") == "weird name.pdf"
    assert sanitize_filename("informe") == "informe.pdf"
    assert sanitize_filename("") == "documento.pdf"
    assert sanitize_filename("...") == "documento.pdf"
    assert len(sanitize_filename("x" * 400 + ".pdf")) <= 104


def test_save_upload_validates_bytes_and_keeps_the_file_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ExtractorRequestStore(ROOT, tmp_path / "state")
    with pytest.raises(OperationError, match="vacío"):
        store.save_upload("a.pdf", b"")
    with pytest.raises(OperationError, match="PDF válido"):
        store.save_upload("a.pdf", b"hello world")
    monkeypatch.setattr(extractor_module, "MAX_UPLOAD_BYTES", 8)
    with pytest.raises(OperationError, match="60 MB"):
        store.save_upload("a.pdf", PDF)
    monkeypatch.setattr(extractor_module, "MAX_UPLOAD_BYTES", 4096)

    result = store.save_upload("../Reporte_2T25.pdf", PDF)
    assert result == {
        "upload": result["upload"],
        "filename": "Reporte_2T25.pdf",
        "size": len(PDF),
        "period_guess": "2025-2T",
    }
    folder = tmp_path / "state" / "extractor" / result["upload"]
    assert (folder / "source.pdf").read_bytes() == PDF
    assert folder.stat().st_mode & 0o777 == 0o700
    metadata = json.loads((folder / "upload.json").read_text(encoding="utf-8"))
    assert metadata["filename"] == "Reporte_2T25.pdf"
    assert not (folder / "request.json").exists()


def test_stale_uploads_without_a_request_are_pruned(tmp_path: Path) -> None:
    store = ExtractorRequestStore(ROOT, tmp_path / "state")
    stale = store.save_upload("old.pdf", PDF)["upload"]
    stale_dir = tmp_path / "state" / "extractor" / stale
    old = time.time() - 3 * 24 * 3600
    os.utime(stale_dir, (old, old))
    kept = store.save_upload("kept.pdf", PDF)["upload"]
    kept_dir = tmp_path / "state" / "extractor" / kept
    store.prepare(_base_request(kept))
    os.utime(kept_dir, (old, old))

    store.save_upload("new.pdf", PDF)
    assert not stale_dir.exists()
    assert kept_dir.exists()


def test_prepare_validates_the_request_contract(tmp_path: Path) -> None:
    store = ExtractorRequestStore(ROOT, tmp_path / "state")
    upload = store.save_upload("foo_3T24.pdf", PDF)["upload"]
    base = _base_request(upload)

    with pytest.raises(OperationError, match="Primero sube"):
        store.prepare({**base, "upload": "../etc"})
    with pytest.raises(OperationError, match="Primero sube"):
        store.prepare({**base, "upload": "0123456789abcdef"[::-1] + "!"})
    with pytest.raises(OperationError, match="no está disponible"):
        store.prepare({**base, "metrics": ["not_a_metric"]})
    with pytest.raises(OperationError, match="Elige una empresa"):
        store.prepare({**base, "company": "walmex; touch bad"})
    with pytest.raises(OperationError, match="CSV, Excel"):
        store.prepare({**base, "format": "pdf"})
    with pytest.raises(OperationError, match="2025-2T"):
        store.prepare({**base, "period": "Q2 2025"})
    with pytest.raises(OperationError, match="al menos una"):
        store.prepare({**base, "metrics": [], "custom_metrics": []})
    with pytest.raises(OperationError, match="como máximo"):
        store.prepare({**base, "custom_metrics": [f"metrica {i}" for i in range(41)]})
    with pytest.raises(OperationError, match="no es válida"):
        store.prepare({**base, "custom_metrics": ["control\x00char"]})
    with pytest.raises(OperationError, match="tablas"):
        store.prepare({**base, "read_tables": "yes"})

    request_id = store.prepare({
        **base,
        "company": "soriana",
        "metrics": ["revenue", "revenue", "ebitda"],
        "custom_metrics": ["Ventas mismas tiendas", "  ventas MISMAS tiendas ", "", "Clientes"],
        "period": "2024-fy",
        "format": "csv",
        "read_tables": False,
    })
    assert request_id == upload
    request = json.loads(
        (tmp_path / "state" / "extractor" / upload / "request.json").read_text(encoding="utf-8")
    )
    assert request["metrics"] == ["revenue", "ebitda"]
    assert request["custom_metrics"] == ["Ventas mismas tiendas", "Clientes"]
    assert request["period"] == "2024-FY"
    assert request["period_guess"] == "2024-3T"
    assert request["format"] == "csv"
    assert request["read_tables"] is False
    assert request["company"] == "soriana"
    assert request["filename"] == "foo_3T24.pdf"
    expected_slug = "soriana" if (ROOT / "configs" / "soriana.yaml").is_file() else ""
    assert request["config_slug"] == expected_slug


def test_company_specific_metrics_only_validate_for_that_company(tmp_path: Path) -> None:
    store = ExtractorRequestStore(ROOT, tmp_path / "state")
    upload = store.save_upload("informe.pdf", PDF)["upload"]
    with pytest.raises(OperationError, match="no está disponible"):
        store.prepare({**_base_request(upload), "metrics": ["ns_domestic"]})
    store.prepare({**_base_request(upload), "company": "herdez", "metrics": ["ns_domestic"]})


def test_resolve_request_and_operation_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pretend_project_pythons_are_installed(monkeypatch)
    state = tmp_path / "state"
    with pytest.raises(OperationError, match="no es válida"):
        resolve_extractor_request(state, "../x")
    with pytest.raises(OperationError, match="no está disponible"):
        resolve_extractor_request(state, "0123456789abcdef")

    store = ExtractorRequestStore(ROOT, state)
    upload = store.save_upload("Reporte_2T25.pdf", PDF)["upload"]
    with pytest.raises(OperationError, match="ya no está disponible"):
        resolve_extractor_request(state, upload)
    request_id = store.prepare(_base_request(upload))
    request_dir, metadata = resolve_extractor_request(state, request_id)
    assert request_dir == (state / "extractor" / request_id).resolve()
    assert metadata["metrics"] == ["revenue"]

    with pytest.raises(OperationError, match="directorio de trabajo"):
        operation_command(ROOT, "pdf_extract", {"request": request_id})

    operation, argv, cwd = operation_command(
        ROOT,
        "pdf_extract",
        {"request": request_id},
        environment={"ANALYST_CONSOLE_STATE_DIR": str(state)},
    )
    assert operation.key == "pdf_extract"
    assert operation.node == "models"
    assert operation.mutates is False
    assert operation.confirmation is None
    assert argv[1:] == [
        "scripts/extract_pdf_observations.py",
        str(request_dir),
        "--output-dir",
        f"outputs/extractor/{request_id}",
    ]
    assert cwd == ROOT
    assert all(";" not in token for token in argv)


def test_latest_extract_prefers_the_newest_output(tmp_path: Path) -> None:
    assert latest_extract(tmp_path) is None
    folder = tmp_path / "outputs" / "extractor" / "0123456789abcdef"
    folder.mkdir(parents=True)
    older = folder / "a_observaciones.csv"
    newer = folder / "a_observaciones.xlsx"
    older.write_text("period\n", encoding="utf-8")
    newer.write_bytes(b"xlsx")
    stamp = time.time() - 600
    os.utime(older, (stamp, stamp))
    assert latest_extract(tmp_path) == newer


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


def _upload(base: str, data: bytes, *, headers: dict | None = None, filename: str = "Reporte_2T25.pdf"):
    request = Request(
        f"{base}/api/extractor/upload?filename={filename}",
        data=data,
        method="POST",
        headers=headers if headers is not None else {
            "Content-Type": "application/pdf",
            "X-Analyst-Console": "1",
            "Origin": PAGES_ORIGIN,
        },
    )
    return urlopen(request, timeout=3)


def test_bridge_accepts_private_uploads_and_creates_extract_jobs(
    console_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with urlopen(f"{console_server}/api/bootstrap", timeout=3) as response:
        payload = json.load(response)
    assert set(payload["nodes"]) == {"soft", "models"}
    assert isinstance(payload["nodes"]["models"]["extractor_latest_available"], bool)
    assert "extractor_updated" in payload["nodes"]["models"]
    assert any(item["key"] == "pdf_extract" for item in payload["operations"])

    with pytest.raises(HTTPError) as unmarked:
        _upload(console_server, PDF, headers={"Content-Type": "application/pdf"})
    assert unmarked.value.code == 403

    with pytest.raises(HTTPError) as not_pdf:
        _upload(console_server, b"hello")
    assert not_pdf.value.code == 400
    assert "PDF" in json.load(not_pdf.value)["error"]

    monkeypatch.setattr("src.analyst_console.server.MAX_UPLOAD_BYTES", 16)
    with pytest.raises(HTTPError) as too_large:
        _upload(console_server, PDF)
    assert too_large.value.code == 413
    monkeypatch.setattr("src.analyst_console.server.MAX_UPLOAD_BYTES", 4096)

    with _upload(console_server, PDF) as response:
        assert response.status == 201
        assert response.headers["Access-Control-Allow-Origin"] == PAGES_ORIGIN
        upload = json.load(response)
    assert upload["period_guess"] == "2025-2T"
    assert upload["filename"] == "Reporte_2T25.pdf"

    created: list[tuple[str, dict]] = []

    def fake_create(self, key, params, confirmation=None):
        created.append((key, params))
        return {"id": "job123", "operation": key, "label": "Extraer", "status": "queued"}

    monkeypatch.setattr("src.analyst_console.jobs.JobManager.create", fake_create)
    job_request = Request(
        f"{console_server}/api/extractor/jobs",
        data=json.dumps({
            "upload": upload["upload"],
            "company": "",
            "metrics": ["revenue"],
            "custom_metrics": ["Ventas mismas tiendas"],
            "period": "",
            "format": "both",
            "read_tables": True,
        }).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Analyst-Console": "1",
            "Origin": PAGES_ORIGIN,
        },
    )
    with urlopen(job_request, timeout=3) as response:
        assert response.status == 202
        assert json.load(response)["operation"] == "pdf_extract"
    assert created == [("pdf_extract", {"request": upload["upload"]})]

    rejected = Request(
        f"{console_server}/api/extractor/jobs",
        data=json.dumps({"upload": upload["upload"], "metrics": []}).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Analyst-Console": "1",
            "Origin": PAGES_ORIGIN,
        },
    )
    with pytest.raises(HTTPError) as empty:
        urlopen(rejected, timeout=3)
    assert empty.value.code == 400

    preflight = Request(
        f"{console_server}/api/extractor/upload",
        method="OPTIONS",
        headers={
            "Origin": PAGES_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-analyst-console",
        },
    )
    with urlopen(preflight, timeout=3) as response:
        assert response.status == 204
        assert response.headers["Access-Control-Allow-Origin"] == PAGES_ORIGIN
