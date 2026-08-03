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


def test_facts_period_accepts_canonical_and_collision_safe_routes():
    probe = """
import json, sys
from pathlib import Path
sys.path.insert(0, {root!r})
from earnlib import bootstrap as bs
names = [
    "FEMSA_2025-1T_facts.json",
    "FEMSA_abc123def456_2025-1T_facts.json",
    "FEMSA_abc123def456_deadbeefcafe_2025-1T_facts.json",
    "FEMSA_2025-1T_facts__root.json",
]
print(json.dumps([bs.facts_period(Path(name), "FEMSA") for name in names]))
"""
    env = {k: v for k, v in os.environ.items() if k not in _MODE_ENV}
    out = subprocess.run(
        [sys.executable, "-c", probe.format(root=str(EARNINGS_ROOT))],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == ["2025-1T"] * 4


def test_facts_glob_mode_switch(tmp_path):
    probe = """
import json, sys
sys.path.insert(0, {root!r})
from earnlib import bootstrap as bs
paths = bs.facts_glob("walmex", "WALMEX")
print(json.dumps({{"n": len(paths), "first": str(paths[0]) if paths else None,
                   "all_n": len(bs.facts_root_glob()),
                   "exists": bs.facts_exists("walmex", "WALMEX", "2021-2T"),
                   "non_vintage_fy_exists": bs.facts_exists(
                       "walmex", "WALMEX", "2021-FY"
                   ),
                   "soft_only_exists": bs.facts_exists(
                       "walmex", "WALMEX", "2021-3T"
                   )}}))
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
    # bundle. Canonical facts are root-owned catalog artifacts; a Soft-owned
    # facts file for another period must not leak into the V3 universe.
    sys.path.insert(0, str(REPO_ROOT))
    from src.shared.document_estate import DocumentEstate, EstateDocument

    estate_root = tmp_path / "estate"
    source = (
        estate_root / "blobs" / "root-facts-2021-2T.json"
    )
    source.parent.mkdir(parents=True)
    source.write_text('{"facts": {}}', encoding="utf-8")
    view = (
        estate_root
        / "views"
        / "reports"
        / "walmex"
        / "xbrl"
        / source.name
    )
    view = view.with_name("WALMEX_2021-2T_facts.json")
    view.parent.mkdir(parents=True)
    view.hardlink_to(source)

    soft_view = view.with_name("WALMEX_2021-3T_facts.json")
    soft_view.write_text('{"facts": {"soft_only": true}}', encoding="utf-8")
    fy_view = view.with_name("WALMEX_2021-FY_facts.json")
    fy_view.write_text('{"facts": {"annual": true}}', encoding="utf-8")

    with DocumentEstate(estate_root / "catalog.db") as estate:
        estate.upsert_document(
            EstateDocument(
                document_id="root:walmex:2021-2T",
                company="walmex",
                period="2021-2T",
                doc_type="regulatory_filing",
                title="WALMEX 2021-2T",
            )
        )
        estate.add_project_record(
            "root", "root:walmex:2021-2T", "root:walmex:2021-2T"
        )
        estate.add_artifact(
            "root:walmex:2021-2T",
            view,
            project="root",
            role="xbrl_facts",
            portable_root=estate_root,
        )
        estate.upsert_document(
            EstateDocument(
                document_id="soft:walmex:2021-3T",
                company="walmex",
                period="2021-3T",
                doc_type="regulatory_filing",
                title="WALMEX 2021-3T",
            )
        )
        estate.add_project_record(
            "soft", "soft:walmex:2021-3T", "soft:walmex:2021-3T"
        )
        estate.add_artifact(
            "soft:walmex:2021-3T",
            soft_view,
            project="soft",
            role="derived",
            portable_root=estate_root,
        )
        estate.upsert_document(
            EstateDocument(
                document_id="root:walmex:2021-FY",
                company="walmex",
                period="2021-FY",
                doc_type="regulatory_filing",
                title="WALMEX 2021-FY",
            )
        )
        estate.add_project_record(
            "root", "root:walmex:2021-FY", "root:walmex:2021-FY"
        )
        estate.add_artifact(
            "root:walmex:2021-FY",
            fy_view,
            project="root",
            role="xbrl_facts",
            portable_root=estate_root,
        )
        estate.commit()

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
        "all_n": 1,
        "exists": True,
        "non_vintage_fy_exists": False,
        "soft_only_exists": False,
    }


def test_v3_selects_corrected_current_filing_not_short_stale_route(tmp_path):
    sys.path.insert(0, str(REPO_ROOT))
    from src.acquisition.models import FetchedArtifact, SourceRecord
    from src.acquisition.writer import EstateWriter
    from src.consumers.derivatives import XbrlFactsDerivativeConsumer

    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    source = SourceRecord(
        source_key="bmv-xbrl",
        source_record_id="WALMEX:quarterly:2021-2T",
        issuer_slug="walmex",
        document_type="regulatory_filing",
        title="WALMEX corrected 2021-2T filing",
        period_year=2021,
        period_quarter=2,
        rendition="xbrl",
        metadata={"ticker": "WALMEX"},
    )

    def store(revenue: int) -> str:
        with EstateWriter(database, estate_root) as writer:
            return writer.store_fetched(
                FetchedArtifact(
                    source,
                    json.dumps({"revenue": revenue}).encode("utf-8"),
                    role="raw_xbrl",
                    filename="WALMEX_2021-2T.json",
                    media_type="application/json",
                ),
                memberships=({"company": "walmex", "earnings": True},),
            ).document_id

    def extract(path: Path) -> bytes:
        revenue = json.loads(path.read_text(encoding="utf-8"))["revenue"]
        return json.dumps(
            {
                "source": path.name,
                "facts": {
                    "ifrs-full_Revenue": [{"value": revenue}],
                },
            },
            sort_keys=True,
        ).encode("utf-8")

    first_document = store(100)
    with XbrlFactsDerivativeConsumer(
        database, estate_root, extractor=extract
    ) as consumer:
        assert consumer.process(first_document).status == "succeeded"
    second_document = store(200)
    with XbrlFactsDerivativeConsumer(
        database, estate_root, extractor=extract
    ) as consumer:
        assert consumer.process(second_document).status == "succeeded"

    probe = """
import json, sys
sys.path.insert(0, {root!r})
from earnlib import bootstrap as bs
paths = bs.facts_glob("walmex", "WALMEX")
payload = json.loads(paths[0].read_text()) if paths else {{}}
print(json.dumps({{
    "n": len(paths),
    "name": paths[0].name if paths else None,
    "revenue": payload.get("facts", {{}}).get(
        "ifrs-full_Revenue", [{{}}]
    )[0].get("value"),
    "exists": bs.facts_exists("walmex", "WALMEX", "2021-2T"),
}}))
"""
    env = {
        k: v for k, v in os.environ.items()
        if k not in _MODE_ENV
    }
    env.update(
        {
            "EARNINGS_V3": "1",
            "PDFS_DOCUMENT_ESTATE": str(estate_root),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", probe.format(root=str(EARNINGS_ROOT))],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    selected = json.loads(result.stdout.strip().splitlines()[-1])
    assert selected["n"] == 1
    assert selected["exists"] is True
    assert selected["revenue"] == 200
    assert selected["name"] != "WALMEX_2021-2T_facts.json"
