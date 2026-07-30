"""
test_verify_extraction.py — the --apply verdict-merge path (data/verified writer).

This is the bridge between subagent verification verdicts and the [verified]
override layer the pipeline applies at highest precedence: a bug here silently
corrupts every manually-verified cell, so the classification rules (numeric →
pinned · null → BLANK · "UNRESOLVED" → UNRESOLVED) and the CSV round-trip into
pipeline._load_verified are pinned down.
"""
from __future__ import annotations

import csv
import json

import pytest

from scripts import verify_extraction as ve


def _apply(monkeypatch, tmp_path, verdicts, slug="testco"):
    monkeypatch.setattr(ve, "VERIFIED_DIR", tmp_path)
    vpath = tmp_path / "verdicts.json"
    vpath.write_text(json.dumps(verdicts), encoding="utf-8")
    ve.cmd_apply(slug, str(vpath))
    return tmp_path / f"{slug}.csv"


def _rows(csv_path):
    with csv_path.open(encoding="utf-8") as fh:
        return {(r["period"], r["key"]): r for r in csv.DictReader(fh)}


def test_apply_numeric_verdict_pins_value(monkeypatch, tmp_path):
    out = _apply(monkeypatch, tmp_path, [
        {"period": "2024-1T", "key": "revenue", "value": 1234.5, "note": "checked p3"},
    ])
    rows = _rows(out)
    assert rows[("2024-1T", "revenue")]["value"] == "1234.5"
    assert rows[("2024-1T", "revenue")]["note"] == "checked p3"


@pytest.mark.parametrize("raw", [None, "", "null"])
def test_apply_null_verdict_becomes_blank(monkeypatch, tmp_path, raw):
    out = _apply(monkeypatch, tmp_path, [
        {"period": "2024-1T", "key": "capex", "value": raw, "note": "not disclosed"},
    ])
    assert _rows(out)[("2024-1T", "capex")]["value"] == "BLANK"


@pytest.mark.parametrize("verdict", [
    {"period": "2024-1T", "key": "ebitda", "value": "UNRESOLVED", "note": "scan illegible"},
    {"period": "2024-1T", "key": "ebitda", "value": 99.0, "status": "unresolved", "note": "scan illegible"},
])
def test_apply_unresolved_verdict(monkeypatch, tmp_path, verdict):
    out = _apply(monkeypatch, tmp_path, [verdict])
    assert _rows(out)[("2024-1T", "ebitda")]["value"] == "UNRESOLVED"


def test_apply_merges_and_overwrites_existing(monkeypatch, tmp_path):
    first = _apply(monkeypatch, tmp_path, [
        {"period": "2024-1T", "key": "revenue", "value": 100, "note": "v1"},
        {"period": "2024-2T", "key": "revenue", "value": 200, "note": ""},
    ])
    assert len(_rows(first)) == 2
    second = _apply(monkeypatch, tmp_path, [
        {"period": "2024-1T", "key": "revenue", "value": 150, "note": "corrected"},
    ])
    rows = _rows(second)
    assert len(rows) == 2                     # merged, not truncated
    assert rows[("2024-1T", "revenue")]["value"] == "150"
    assert rows[("2024-2T", "revenue")]["value"] == "200"


def test_apply_round_trips_into_pipeline_loader(monkeypatch, tmp_path):
    """What --apply writes is exactly what pipeline._load_verified reads back."""
    from src.extract import pipeline as pl

    _apply(monkeypatch, tmp_path, [
        {"period": "2024-1T", "key": "revenue", "value": 1234.5, "note": "ok"},
        {"period": "2024-1T", "key": "capex", "value": None, "note": "nd"},
        {"period": "2024-1T", "key": "ebitda", "value": "UNRESOLVED", "note": "bad scan"},
    ], slug="testco")

    monkeypatch.setattr("src.shared.paths.PROJECT_ROOT", tmp_path)
    # _load_verified builds the path from PROJECT_ROOT/data/verified/<stem>.csv
    (tmp_path / "data" / "verified").mkdir(parents=True)
    (tmp_path / "testco.csv").rename(tmp_path / "data" / "verified" / "testco.csv")

    loaded = pl._load_verified(tmp_path / "configs" / "testco.yaml")
    assert loaded[("2024-1T", "revenue")] == (1234.5, "ok")
    assert loaded[("2024-1T", "capex")] == (None, "nd")
    assert loaded[("2024-1T", "ebitda")][0] is pl.UNRESOLVED


def test_load_verified_malformed_file_warns_loudly(monkeypatch, tmp_path, capsys):
    """A malformed verified CSV must not silently disable all overrides."""
    from src.extract import pipeline as pl

    monkeypatch.setattr("src.shared.paths.PROJECT_ROOT", tmp_path)
    vdir = tmp_path / "data" / "verified"
    vdir.mkdir(parents=True)
    (vdir / "badco.csv").write_bytes(b"\xff\xfe garbage \x00not,a,csv")

    out = pl._load_verified(tmp_path / "configs" / "badco.yaml")
    assert out == {}
    err = capsys.readouterr().err
    assert "DISABLED" in err
