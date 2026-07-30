"""Audit-v3 switch: artifact routing, engine selection, estate facts glob.

The V2/V3 flags are read at import time, so mode-dependent assertions run in
subprocesses with a controlled environment.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

EARNINGS_ROOT = Path(__file__).resolve().parents[1]
# Derived the same way as earnlib.bootstrap.REPO_ROOT, rather than imported: importing
# bootstrap mutates sys.path and loads the estate bridge, which is exactly what this file
# keeps out of the parent process by probing in subprocesses.
REPO_ROOT = EARNINGS_ROOT.parent
_MODE_ENV = {
    "EARNINGS_V2",
    "EARNINGS_V3",
    "EARNINGS_ESTATE_MODE",
    "PDFS_DOCUMENT_ESTATE",
    "PDFS_ESTATE_BRIDGE",
    "PDFS_REPORTS_DIR",
}

_PROBE = """
import json, sys
sys.path.insert(0, {root!r})
from earnlib import bootstrap as bs
import src
print(json.dumps({{
    "V2": bs.V2, "V3": bs.V3,
    "results": bs.RESULTS_DIR.name,
    "metrics": bs.art_path("metrics").name,
    "metrics_hist": bs.art_path("metrics_hist").name,
    "events": bs.art_path("events").name,
    "events_all": bs.art_path("events_all").name,
    "event_windows": bs.art_path("event_windows").name,
    "surprises": bs.art_path("surprises").name,
    "prices": bs.art_path("prices").name,
    "universe": bs.art_path("universe", ".csv").name,
    "kpi_panel": bs.art_path("kpi_panel").name,
    "src_file": src.__file__,
}}))
"""


def _probe(env_overrides: dict[str, str]) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _MODE_ENV}
    env.update(env_overrides)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE.format(root=str(EARNINGS_ROOT))],
        capture_output=True, text=True, env=env, check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_default_mode_routing_unchanged():
    r = _probe({})
    assert not r["V2"] and not r["V3"]
    assert r["results"] == "results"
    assert r["metrics"] == "metrics.parquet"
    assert r["surprises"] == "surprises.parquet"
    assert r["prices"] == "prices.parquet"
    assert r["src_file"].startswith(str(EARNINGS_ROOT / "vendor" / "alpha-go"))


def test_v2_mode_routing_unchanged():
    r = _probe({"EARNINGS_V2": "1"})
    assert r["V2"] and not r["V3"]
    assert r["results"] == "results_v2"
    assert r["metrics"] == "metrics.parquet"          # v2 never versioned metrics
    assert r["metrics_hist"] == "metrics_hist_v2.parquet"
    assert r["surprises"] == "surprises_v2.parquet"
    assert r["prices"] == "prices_v2.parquet"
    assert r["src_file"].startswith(str(EARNINGS_ROOT / "vendor" / "alpha-go"))


def test_v3_mode_routing_and_engine():
    r = _probe({"EARNINGS_V3": "1"})
    assert r["V2"] and r["V3"]                         # v3 implies v2 corrections
    assert r["results"] == "results_v3"
    for stem in ("metrics", "events", "events_all",
                 "event_windows", "surprises", "kpi_panel"):
        assert r[stem] == f"{stem}_v3.parquet", (stem, r[stem])
    assert r["universe"] == "universe_v3.csv"
    # inherited from v2, not regenerated: prices (snapshot inputs untouched)
    # and metrics_hist (historical re-extraction deferred)
    assert r["prices"] == "prices_v2.parquet"
    assert r["metrics_hist"] == "metrics_hist_v2.parquet"
    # root engine, not the vendored copy
    assert r["src_file"] == str(REPO_ROOT / "src" / "__init__.py")


def test_facts_glob_mode_switch(tmp_path):
    probe = """
import json, sys
sys.path.insert(0, {root!r})
from earnlib import bootstrap as bs
paths = bs.facts_glob("walmex", "WALMEX")
print(json.dumps({{"n": len(paths), "first": str(paths[0]) if paths else None,
                   "any_root_suffix": any(p.name.endswith("_facts__root.json") for p in paths)}}))
"""
    env_base = {k: v for k, v in os.environ.items() if k not in _MODE_ENV}

    # Default mode may have the separately transferred frozen corpus attached,
    # but a Git clone intentionally does not. Either state must import cleanly.
    default_out = subprocess.run(
        [sys.executable, "-c", probe.format(root=str(EARNINGS_ROOT))],
        capture_output=True, text=True, env=env_base, check=True,
    )
    default = json.loads(default_out.stdout.strip().splitlines()[-1])
    if default["n"]:
        assert default["first"].startswith(str(EARNINGS_ROOT / "data" / "soft"))
    else:
        assert default["first"] is None

    # Exercise the v3 estate-selection behavior with a complete synthetic
    # bundle. The view link deliberately resolves into a ``soft/data/reports``
    # tree because that target provenance is part of the v3 selection contract.
    source = (
        tmp_path
        / "soft"
        / "data"
        / "reports"
        / "walmex"
        / "xbrl"
        / "WALMEX_2021-2T_facts.json"
    )
    source.parent.mkdir(parents=True)
    source.write_text('{"facts": {}}', encoding="utf-8")
    estate_root = tmp_path / "estate"
    view = (
        estate_root
        / "views"
        / "reports"
        / "walmex"
        / "xbrl"
        / source.name
    )
    view.parent.mkdir(parents=True)
    view.symlink_to(source)

    v3_env = {
        **env_base,
        "EARNINGS_V3": "1",
        "PDFS_DOCUMENT_ESTATE": str(estate_root),
    }
    v3_out = subprocess.run(
        [sys.executable, "-c", probe.format(root=str(EARNINGS_ROOT))],
        capture_output=True, text=True, env=v3_env, check=True,
    )
    v3 = json.loads(v3_out.stdout.strip().splitlines()[-1])
    assert v3 == {
        "n": 1,
        "first": str(view),
        "any_root_suffix": False,
    }
