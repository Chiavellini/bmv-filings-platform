"""Emit a blank Bloomberg fill-in template (CSV) from a coverage spec.

The template is a long-format CSV — one row per cell the engine needs from the terminal. The
user fills the ``value`` column and saves it to ``data/bloomberg/<slug>.csv`` (see
:mod:`src.bloomberg.ingest`). Long format keeps emit/ingest symmetric and diff-friendly.
"""
from __future__ import annotations

import csv
from pathlib import Path

from src.bloomberg.schema import TemplateRow, required_rows

# Column order of the emitted/ingested CSV.
COLUMNS = ["entity", "entity_kind", "field", "period", "label", "unit", "bbg_hint", "value"]


def build_rows(spec, base_year: int | None = None, filled: set | None = None) -> list[TemplateRow]:
    return required_rows(spec, base_year=base_year, filled=filled)


def write_template(spec, out_path: str | Path, base_year: int | None = None,
                   filled: set | None = None) -> Path:
    """Write the template CSV for ``spec``. If ``filled`` is given, only the Bloomberg RESIDUAL
    cells (not covered natively) are written. Returns the path."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = build_rows(spec, base_year=base_year, filled=filled)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([r.entity, r.entity_kind, r.field, r.period, r.label, r.unit, r.bbg_hint, ""])
    return out
