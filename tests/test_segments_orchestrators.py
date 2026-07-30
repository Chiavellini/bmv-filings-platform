"""
test_segments_orchestrators.py — excel phase: the orchestrators that tie
extraction → workbook (generate_company_segments / generate_segments_sheet).

pipeline.run is monkeypatched to a hand-made DataFrame, so these stay offline
and test the wiring + spec resolution, not extraction.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.shared.paths import CONFIGS_DIR
from src.extract import pipeline
from src.excel import segments_sheet
from src.excel.segments_sheet import (
    SegmentRow, generate_company_segments, generate_segments_sheet, _load_segments_spec,
)


def _fake_df(*_a, **_k):
    rows = []
    for year, base in ((2024, 200), (2025, 240)):
        for q in (1, 2, 3, 4):
            rows.append({"period": f"{year}-{q}T",
                         "revenue": base + q, "revenue_mexico": base // 2 + q,
                         "ebitda": base // 4})
    return pd.DataFrame(rows)


@pytest.fixture
def patched_run(monkeypatch):
    monkeypatch.setattr(pipeline, "run", _fake_df)


def test_generate_company_segments_outline(monkeypatch, patched_run):
    wb = generate_company_segments(str(CONFIGS_DIR / "walmex.yaml"), "ignored-source")
    ws = wb.active
    assert wb.sheetnames == ["Segments"]
    assert ws["A2"].value == "WALMEX: Walmart de México y Centroamérica"

    # The consolidated revenue row ("Walmart de México y CAM") fills from the fake df.
    def find(label):
        for r in range(6, ws.max_row + 1):
            if ws[f"B{r}"].value == label:
                return r
        return None
    r = find("Walmart de México y CAM")
    assert r is not None
    # 2024 Q1 revenue = 201 in the fake df → prior-year first quarter column C.
    assert ws[f"C{r}"].value == 201


def test_generate_segments_sheet_flat(patched_run):
    wb = generate_segments_sheet(
        "ignored-source",
        [SegmentRow("Revenue", "revenue"), SegmentRow("EBITDA", "ebitda")],
        config=None, title="ACME: Co",
    )
    ws = wb.active
    assert ws["A2"].value == "ACME: Co"
    assert ws["B6"].value == "Revenues"           # flat builder section header
    assert ws["B8"].value == "Consolidated"


def test_load_segments_spec_explicit_path():
    spec = _load_segments_spec({}, spec_path=str(CONFIGS_DIR / "walmex_segments.yaml"))
    assert spec and spec.get("outline") and "Revenues" in spec.get("sections", [])


def test_load_segments_spec_via_config_segments_file():
    cfg = {"segments_file": "configs/walmex_segments.yaml"}
    spec = _load_segments_spec(cfg, config_path=str(CONFIGS_DIR / "walmex.yaml"))
    assert spec and spec.get("outline")


def test_load_segments_spec_inline():
    cfg = {"segments": {"outline": "Revenues\n", "sections": ["Revenues"], "mapping": {}}}
    spec = _load_segments_spec(cfg)
    assert spec["outline"] == "Revenues\n"


def test_load_segments_spec_absent_returns_none():
    assert _load_segments_spec({}) is None
