"""Soft's production builders consume the deployment-selected shared reports view."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from scripts import build_coverage
from src.bloomberg.schema import BloombergPack
from src.coverage import applicability, native, reit_ffo
from src.coverage.fundamentals import (
    Fundamentals,
    _period_label,
    _selected_canonical_fact_files,
    _xbrl_period_sources,
)
from src.coverage.spec import CoverageSpec

SOFT_ROOT = Path(__file__).resolve().parents[1]


def test_pdfs_reports_dir_reaches_build_module_in_fresh_process(tmp_path: Path):
    shared = tmp_path / "estate" / "views" / "reports"
    env = {**os.environ, "PDFS_REPORTS_DIR": str(shared)}
    probe = (
        "from src.shared.paths import REPORTS_DIR;"
        "from scripts.build_coverage import REPORTS_DIR as BUILD_REPORTS_DIR;"
        "print(REPORTS_DIR); print(BUILD_REPORTS_DIR)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=SOFT_ROOT, env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(shared.resolve()), str(shared.resolve())]


def test_build_one_passes_shared_company_directory_to_fundamentals(
    tmp_path: Path, monkeypatch,
):
    shared = tmp_path / "estate" / "views" / "reports"
    captured: dict[str, Path] = {}

    def stop_after_path(slug, reports_dir, *args, **kwargs):
        captured["reports_dir"] = Path(reports_dir)
        raise RuntimeError("path captured")

    monkeypatch.setattr(build_coverage, "REPORTS_DIR", shared)
    monkeypatch.setattr(build_coverage, "load_fundamentals", stop_after_path)
    result = build_coverage.build_one(
        SOFT_ROOT / "inputs" / "walmex.md", no_network=True,
    )

    assert captured["reports_dir"] == shared / "walmex"
    assert result.error == "RuntimeError: path captured"


def test_native_pack_uses_same_shared_root_for_subject_xbrl(tmp_path: Path, monkeypatch):
    shared = tmp_path / "estate" / "views" / "reports"
    calls: list[Path] = []
    monkeypatch.setattr(native, "REPORTS_DIR", shared)
    monkeypatch.setattr(native, "_clave_and_ticker", lambda slug: (None, None))
    monkeypatch.setattr(
        native, "_native_shares_out",
        lambda reports_dir, shares_per_unit=None: calls.append(Path(reports_dir)) or None,
    )
    spec = CoverageSpec(
        slug="estate_test", name="Estate Test", ticker=None, currency="MXN",
        units="millions", template="industrial", blocks=[], peers=[],
    )
    fund = Fundamentals(
        slug=spec.slug, frame=None, periods=["2025-1T"], current_period="2025-1T",
        ltm={"revenue": 100.0},
    )

    pack = native.build_native_pack(
        spec, fund, with_prices=False, with_macro=False, with_peers=False, offline=True,
    )

    assert isinstance(pack, BloombergPack)
    assert calls == [shared / spec.slug]


def test_canonical_root_facts_are_discovered_without_reentrant_artifact(
    tmp_path: Path,
):
    reports = tmp_path / "reports"
    xdir = reports / "ac" / "xbrl"
    xdir.mkdir(parents=True)
    facts = {
        "ifrs-full_Revenue": [
            {
                "value": 1250.0, "instant": None,
                "period_start": "2025-01-01", "period_end": "2025-03-31",
                "unit": "MXN", "decimals": "0", "dimensions": None,
            }
        ]
    }
    canonical = xdir / "AC_2025-1T_facts.json"
    canonical.write_text(json.dumps({"source": "root", "facts": facts}), encoding="utf-8")

    docs = _xbrl_period_sources(
        "AC", reports / "ac", max_reports=40, offline=True, facts_only=True,
    )

    assert _period_label(canonical.name) == "2025-1T"
    assert _period_label("AC_91b2d4e7_2025-1T_facts.json") == "2025-1T"
    assert list(docs) == ["2025-1T"]
    assert docs["2025-1T"].facts == facts
    assert not (xdir / "AC_2025-1T_facts_facts.json").exists()


def test_offline_discovery_preserves_raw_json_fallback(tmp_path: Path, monkeypatch):
    reports = tmp_path / "reports"
    xdir = reports / "ac" / "xbrl"
    xdir.mkdir(parents=True)
    raw = xdir / "AC_2024-4T.json"
    raw.write_text("{}", encoding="utf-8")
    fallback = {"ifrs-full_Revenue": [{"value": 1000.0}]}

    from src.extract import pipeline
    monkeypatch.setattr(pipeline, "_load_facts", lambda path: fallback if path == raw else None)

    docs = _xbrl_period_sources(
        "AC", reports / "ac", max_reports=40, offline=True, facts_only=True,
    )

    assert list(docs) == ["2024-4T"]
    assert docs["2024-4T"].facts == fallback


def test_invalid_canonical_facts_fall_back_to_same_period_raw(
    tmp_path: Path, monkeypatch,
):
    reports = tmp_path / "reports"
    xdir = reports / "ac" / "xbrl"
    xdir.mkdir(parents=True)
    raw = xdir / "AC_2024-4T.json"
    raw.write_text("{}", encoding="utf-8")
    (xdir / "AC_2024-4T_facts.json").write_text(
        '{"facts": {}}', encoding="utf-8"
    )
    fallback = {"ifrs-full_Revenue": [{"value": 1000.0}]}

    from src.extract import pipeline
    monkeypatch.setattr(
        pipeline,
        "_load_facts",
        lambda path: fallback if path == raw else None,
    )

    docs = _xbrl_period_sources(
        "AC", reports / "ac", max_reports=40, offline=True, facts_only=True,
    )

    assert docs["2024-4T"].facts == fallback


def test_canonical_facts_count_for_source_availability_and_history(
    tmp_path: Path, monkeypatch,
):
    from scripts import gen_universe

    reports = tmp_path / "reports"
    xdir = reports / "femsa" / "xbrl"
    xdir.mkdir(parents=True)
    for year in (2021, 2022, 2023, 2024, 2025):
        (xdir / f"FEMSA_{year}-4T_facts.json").write_text(
            json.dumps({"facts": {"ifrs-full_Revenue": [{"value": year}]}}),
            encoding="utf-8",
        )

    monkeypatch.setattr(gen_universe, "REPORTS_DIR", reports)
    monkeypatch.setattr(applicability, "REPORTS_DIR", reports)
    monkeypatch.setattr(reit_ffo, "REPORTS_DIR", reports)

    assert gen_universe.has_cached_xbrl("femsa")
    assert applicability._annual_periods("femsa") == 5
    latest = reit_ffo._latest_annual_filing("femsa")
    assert latest == (xdir / "FEMSA_2025-4T_facts.json", 2025)


def test_catalog_latest_version_wins_over_filename_and_mtime(
    tmp_path: Path, monkeypatch,
):
    estate = tmp_path / "estate"
    reports = estate / "views" / "reports"
    xdir = reports / "ac" / "xbrl"
    xdir.mkdir(parents=True)
    old = xdir / "AC_2025-4T_facts.json"
    current = xdir / "AC_docv2_2025-4T_facts.json"
    concept = "ifrs_mx-cor_20141205_NumeroDeAccionesEnCirculacion"

    def payload(revenue: float, shares: float) -> str:
        return json.dumps({
            "facts": {
                "ifrs-full_Revenue": [{
                    "value": revenue, "period_start": "2025-10-01",
                    "period_end": "2025-12-31", "instant": None,
                }],
                concept: [{
                    "value": shares, "instant": "2025-12-31",
                    "period_start": None, "period_end": None,
                }],
            }
        })

    old.write_text(payload(100.0, 100e6), encoding="utf-8")
    current.write_text(payload(200.0, 200e6), encoding="utf-8")
    # Make the stale short filename newer: catalog versioning must still win.
    os.utime(current, ns=(1_000_000_000, 1_000_000_000))
    os.utime(old, ns=(2_000_000_000, 2_000_000_000))

    with sqlite3.connect(estate / "catalog.db") as connection:
        connection.executescript("""
            CREATE TABLE documents(document_id TEXT PRIMARY KEY, period TEXT);
            CREATE TABLE source_record_versions(
                document_id TEXT PRIMARY KEY,
                document_family_id TEXT,
                version INTEGER,
                stored_at TEXT
            );
            CREATE TABLE artifacts(
                document_id TEXT,
                project TEXT,
                role TEXT,
                path TEXT,
                sha256 TEXT,
                created_at TEXT
            );
        """)
        connection.executemany(
            "INSERT INTO documents VALUES(?,?)",
            [("doc-v1", "2025-4T"), ("doc-v2", "2025-4T")],
        )
        connection.executemany(
            "INSERT INTO source_record_versions VALUES(?,?,?,?)",
            [
                ("doc-v1", "ac:quarterly:2025-4T", 1, "2025-01-01"),
                ("doc-v2", "ac:quarterly:2025-4T", 2, "2025-02-01"),
            ],
        )
        connection.executemany(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?)",
                [
                    ("doc-v1", "root", "xbrl_facts",
                     "views/reports/ac/xbrl/AC_2025-4T_facts.json",
                     hashlib.sha256(old.read_bytes()).hexdigest(), "2025-01-01"),
                    ("doc-v2", "root", "xbrl_facts",
                     "views/reports/ac/xbrl/AC_docv2_2025-4T_facts.json",
                     hashlib.sha256(current.read_bytes()).hexdigest(), "2025-02-01"),
                ],
            )

    docs = _xbrl_period_sources(
        "AC", reports / "ac", max_reports=40, offline=True, facts_only=True,
    )
    assert docs["2025-4T"].facts["ifrs-full_Revenue"][0]["value"] == 200.0
    assert native._native_shares_out(reports / "ac") == 200.0
    monkeypatch.setattr(reit_ffo, "REPORTS_DIR", reports)
    assert reit_ffo._latest_annual_filing("ac") == (current, 2025)

    with sqlite3.connect(estate / "catalog.db") as connection:
        connection.execute(
            "UPDATE artifacts SET sha256=? WHERE document_id='doc-v2'",
            ("0" * 64,),
        )
    assert _selected_canonical_fact_files(reports / "ac") == []
    assert _xbrl_period_sources(
        "AC", reports / "ac", max_reports=40, offline=True, facts_only=True,
    ) == {}


def test_standalone_facts_fallback_uses_newest_mtime(tmp_path: Path):
    reports = tmp_path / "standalone" / "reports" / "ac"
    xdir = reports / "xbrl"
    xdir.mkdir(parents=True)
    old = xdir / "AC_2025-1T_facts.json"
    current = xdir / "AC_docv2_2025-1T_facts.json"
    old.write_text(json.dumps({"facts": {"old": []}}), encoding="utf-8")
    current.write_text(json.dumps({"facts": {"current": []}}), encoding="utf-8")
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(current, ns=(2_000_000_000, 2_000_000_000))

    assert _selected_canonical_fact_files(reports) == [current]


def test_managed_catalog_schema_or_hash_failure_never_falls_back(
    tmp_path: Path,
):
    estate = tmp_path / "estate"
    reports = estate / "views" / "reports" / "ac"
    xdir = reports / "xbrl"
    xdir.mkdir(parents=True)
    facts = xdir / "AC_2025-1T_facts.json"
    facts.write_text(json.dumps({"facts": {"stale": [{"value": 100}]}}), encoding="utf-8")
    with sqlite3.connect(estate / "catalog.db") as connection:
        # Its presence makes this a managed estate, but the version contract is
        # malformed. Serving the filesystem artifact would silently resurrect
        # an unverified/superseded version.
        connection.execute("CREATE TABLE documents(document_id TEXT)")

    assert _selected_canonical_fact_files(reports) == []
    assert _xbrl_period_sources(
        "AC", reports, max_reports=40, offline=True, facts_only=True,
    ) == {}
